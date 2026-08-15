"""Claude-powered extraction engine.

The spreadsheet is still opened locally: the Messages API cannot read a binary
.xls or .xlsx, so `loaders.py` converts it to a grid of cells first. What
changes here is who interprets that grid — Claude instead of the rule engine
in `detect.py`.

Two passes, because a statement rarely fits in one response:

  1. metadata  - the header block goes up once; Claude reports the accounts,
                 their currency, period, balances and which column is which.
  2. rows      - the data rows go up in batches, with the column mapping from
                 pass 1 as context, and come back as transactions.

Both passes use structured outputs (`output_config.format`), so responses are
schema-valid by construction rather than by hopeful parsing.

The output is the same `Statement` object the rule engine produces, and it
goes through the same reconciliation, so an AI conversion is still checked
against the totals the bank printed.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Sequence

from .loaders import Grid, load
from .models import Account, Statement, Transaction
from .normalize import (
    clean_text, currency_from_account, format_datetime, parse_amount,
    parse_datetime,
)
from .reconcile import reconcile
from .vocabulary import detect_bank

DEFAULT_MODEL = "claude-sonnet-5"
FAST_MODEL = "claude-haiku-4-5-20251001"

# How many spreadsheet rows go into one extraction request. Each transaction
# costs roughly 120-200 output tokens, so 60 rows keeps a batch far below the
# response limit even when every row is a long Cyrillic payment purpose.
DEFAULT_BATCH_ROWS = 60

# Rows of the header block shown to the metadata pass.
METADATA_ROWS = 45


class AIError(RuntimeError):
    """Raised when the model cannot be reached or returns unusable output."""


@dataclass
class AIConfig:
    model: str = DEFAULT_MODEL
    api_key: str | None = None
    batch_rows: int = DEFAULT_BATCH_ROWS
    max_tokens: int = 16000
    metadata_max_tokens: int = 4000
    name_style: str = "composite"
    default_currency: str = "UZS"
    # Cross-check the model's output against the deterministic parser and
    # record any disagreement. Cheap, and the only way to notice a silent
    # miscount.
    verify: bool = True
    client: Any = None          # inject a client in tests
    extra_instructions: str = ""


# --------------------------------------------------------------------------
# JSON schemas for structured outputs
# --------------------------------------------------------------------------

_COLUMNS_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "integer"},
        "document_number": {"type": "integer"},
        "debit": {"type": "integer"},
        "credit": {"type": "integer"},
        "counterparty_account": {"type": "integer"},
        "counterparty_name": {"type": "integer"},
        "bank_code": {"type": "integer"},
        "payment_purpose": {"type": "integer"},
    },
    "required": ["date", "document_number", "debit", "credit",
                 "counterparty_account", "counterparty_name", "bank_code",
                 "payment_purpose"],
    "additionalProperties": False,
}

METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "accounts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "account_number": {"type": "string"},
                    "currency": {"type": "string"},
                    "account_name": {"type": "string"},
                    "period_from": {"type": "string"},
                    "period_to": {"type": "string"},
                    "opening_balance": {"type": "string"},
                    "closing_balance": {"type": "string"},
                    "stated_debit_total": {"type": "string"},
                    "stated_credit_total": {"type": "string"},
                    "stated_count": {"type": "integer"},
                    "header_row": {"type": "integer"},
                    "first_data_row": {"type": "integer"},
                    "last_data_row": {"type": "integer"},
                    "layout": {"type": "string", "enum": ["row", "block"]},
                    "columns": _COLUMNS_SCHEMA,
                },
                "required": [
                    "account_number", "currency", "account_name",
                    "period_from", "period_to", "opening_balance",
                    "closing_balance", "stated_debit_total",
                    "stated_credit_total", "stated_count", "header_row",
                    "first_data_row", "last_data_row", "layout", "columns",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["accounts"],
    "additionalProperties": False,
}

TRANSACTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "transaction_date": {"type": "string"},
                    "document_number": {"type": "string"},
                    "credit_amount": {"type": "string"},
                    "debit_amount": {"type": "string"},
                    "counterparty_name": {"type": "string"},
                    "counterparty_account": {"type": "string"},
                    "bank_code": {"type": "string"},
                    "payment_purpose": {"type": "string"},
                    "counterparty_inn": {"type": "string"},
                    "source_row": {"type": "integer"},
                },
                "required": [
                    "transaction_date", "document_number", "credit_amount",
                    "debit_amount", "counterparty_name", "counterparty_account",
                    "bank_code", "payment_purpose", "counterparty_inn",
                    "source_row",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["transactions"],
    "additionalProperties": False,
}

# Amounts cross the wire as strings on purpose: JSON numbers would go through
# a float and 1550464.98 must survive as exactly that.


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------

METADATA_SYSTEM = """\
You read bank statement exports from Uzbek banks. They are written in Russian, Uzbek Latin and Uzbek Cyrillic, often mixed inside one file.
 
You are given the top rows of a spreadsheet as tab-separated cells, one line per row, prefixed by the 0-based row index.
 
Identify every account section in the file. A file may contain several accounts stacked vertically, each with its own preamble, column header and totals. Report accounts that have no transactions too.
 
Two things about the grid itself:
- A value from a merged cell range is REPEATED across every column it spanned, so the same caption can appear many times in one row. Read it once. When you report a column index, give the index where that field's DATA sits in the transaction rows, not necessarily the first column the caption occupies.
- The caption row is usually below the account preamble, but not always: some banks print the captions FIRST and then the bank line, account number, period and opening balance underneath, with transactions starting after those. Set first_data_row to the first genuine posting row, never to a preamble line.
 
For each account report:
- account_number: the 20-digit account the statement is FOR (not a counterparty account). It follows a caption such as "Лицевой счет", "Счет:", "История по счету", "Сведения о работе счета" or "СПРАВКА ПО РАБОТЕ СЧЕТА". Ignore any account number that appears inside a payment purpose. When the file holds several sections, use the caption nearest that section's table.
- currency: 3-letter code. Prefer a stated line such as "Валюта счета: USD" or "Ед.изм. Доллар США". Otherwise digits 6-8 of the account number are the ISO 4217 numeric code: 000 or 860 = UZS, 840 = USD, 978 = EUR, 643 = RUB.
- account_name: the client's name. Not the bank's own branch line, which looks like '00419 ТОШКЕНТ Ш., "ИПОТЕКА-БАНК" АТИБ ... ФИЛИАЛИ'.
- period_from / period_to: DD.MM.YYYY, "" if absent.
- opening_balance / closing_balance: the stated balances as plain decimal strings using "." for decimals and no thousands separators, "" if absent. Beware captions that carry a date first, as in "Входящий остаток на 01.01.2025   6,470,145.25" - the balance is 6470145.25, not the date. Some files print both on one line: "САЛЬДО НАЧ. 12,466,344.33 САЛЬДО КОН. 3,895,959.64".
- stated_debit_total / stated_credit_total: the PERIOD turnover printed in the totals row ("Сумма оборотов", "Итого документов", "ВСЕГО ЗА ПЕРИОД", "Итоговый оборот за период", "Umumiy"), "" if absent. Copy it; never compute it. A row reading "Итого за 20.02.2023" is a daily subtotal, not the period total. If the file prints a bare "ИТОГО" immediately above a "ВСЕГО ЗА ПЕРИОД", the second one is the period total.
- stated_count: the stated number of documents, 0 if absent. "Количество оборотов 135 14" means 135 debit and 14 credit documents, so report 149.
- header_row: 0-based index of the row holding the column captions.
- first_data_row / last_data_row: 0-based indices bounding the transaction rows for this account. Exclude preamble, totals and balance rows.
- layout: "row" if one transaction occupies one row; "block" if a transaction spans several rows (amounts on the first, time and counterparty name on the second, payment purpose on the third).
- columns: 0-based column index for each field. Use -1 when a field has no column of its own. If one column combines account, INN and name (captions like "Счет/ИНН" or "Корреспондент: Банк/Счет/ИНН"), give that same index for counterparty_account, counterparty_name and bank_code.
 
Report only what the file states. Never invent an account number or a total. If a figure is absent, leave it empty rather than deriving it.
 
Never merge two account sections into one, and never split one section in two. Each account keeps its own transactions, its own currency and its own totals. Amounts in different currencies must never be added together: a file holding a UZS account and a USD account has two accounts, not one."""
 
TRANSACTIONS_SYSTEM = """\
You extract transactions from a bank statement export.
 
You are given spreadsheet rows as tab-separated cells, one line per row, prefixed by the 0-based row index, plus the column mapping for this account.
 
Return one object per transaction, in the order the rows appear.
 
WHICH ROWS COUNT
- Extract only rows that are real postings. Skip blank rows, repeated column caption rows, column-number rows such as "1 2 3 4 5", subtotal and total rows ("Итого", "Итого за 20.02.2023", "Итого документов", "Сумма оборотов", "ВСЕГО ЗА ПЕРИОД", "Umumiy"), opening and closing balance rows, page footers, signature lines, "***** Конец отчета *****", and bank branch lines such as '00419 ТОШКЕНТ Ш., "ИПОТЕКА-БАНК" АТИБ ... ФИЛИАЛИ'.
- Judge a row by its LABEL cells, not by its payment purpose: a genuine purpose can contain the word "итого" or mention a balance.
- Every transaction has an amount in exactly one of debit or credit. Never both. If a row has neither, it is not a transaction.
- Never drop a transaction because it looks duplicated, unusual, or dated outside the period. Statements legitimately contain repeated amounts and back-dated postings.
- A value from a merged cell range is repeated across the columns it spanned. The same text appearing several times in one row is an artefact, not two values.
 
AMOUNTS
- credit_amount / debit_amount: plain decimal strings, "." for decimals, no thousands separators, no currency, no leading "+".
  "1 234,56" -> "1234.56"    "1,234.56" -> "1234.56"
  "3 378 943 223,69" -> "3378943223.69"
  "0,00" -> "0"    ".00" -> "0"    "" or "-" -> "0"
  "(1 234.56)" -> "-1234.56"
- Decide which character is the decimal separator from the whole file, not cell by cell. A three-digit group after a comma is nearly always a thousands separator.
- Copy the digits exactly as printed. Never round, re-add or recompute an amount, even if the row looks inconsistent.
 
DATES
- transaction_date: "DD.MM.YYYY HH:MM:SS", always this exact shape.
- Use 00:00:00 when the row carries no time.
- Input dates may already be rendered as "YYYY-MM-DD HH:MM:SS"; reformat them.
- A 2-digit year 00-69 means 2000-2069; 70-99 means 1970-1999.
- Excel date serials are converted before you see them.
 
COUNTERPARTY
- The correspondent details often arrive as one tagged string:
      "МФО:00419 Счет:16401000605703819001 ИНН:310841562"
  Segments are frequently EMPTY, most often a trailing "ИНН:" with nothing after it:
      "МФО:00419 Счет:16401000605703819001 ИНН:"
  Extract every segment that IS present. An empty ИНН means only that counterparty_inn is ""; the МФО and the Счет are still there and must still be reported. NEVER discard a field because a neighbouring field is blank.
- counterparty_account: the 20-digit correspondent account, "" if absent.
- counterparty_inn: the 9-digit taxpayer number, "" if absent. It is often appended to the counterparty name after padding spaces, or given as "ИНН:310841562", or as the middle segment of "account/INN/name". Treat "000000000" as absent.
- counterparty_name: the counterparty's name ONLY, with the account and INN removed and whitespace collapsed.
- bank_code: the 5-digit MFO/BIC, zero-padded, "" if absent.
- payment_purpose: the full purpose text, whitespace collapsed, otherwise verbatim. Keep codes, tildes and reference numbers.
- source_row: the 0-based index of the row the amounts came from.
 
BLOCK LAYOUT
In a block layout, one transaction spans several rows: the first carries the date, document number and amounts, the next carries the time and the counterparty name, and the rest carry the payment purpose. Merge them into one transaction and report the first row's index as source_row. Never emit a continuation line as its own transaction.
 
Stop collecting a transaction's purpose at the FIRST of: the next row with an amount, a totals or balance row, or the start of the next account section ("Cправка о работе счета", "Лицевой счет", a bank branch line). The last transaction before a section break must not absorb the next section's header text into its payment_purpose.
 
Copy values; do not interpret, translate, correct or summarise them. If a field is absent, return "" rather than guessing it."""
 

# --------------------------------------------------------------------------
# grid rendering
# --------------------------------------------------------------------------

def render_rows(grid: Grid, start: int, end: int, *, max_cell: int = 300) -> str:
    """Render rows [start, end) as indexed tab-separated lines."""
    lines: list[str] = []
    for r in range(max(0, start), min(end, grid.nrows)):
        cells: list[str] = []
        for c in range(grid.ncols):
            value = grid.cell(r, c)
            text = "" if value is None else clean_text(value)
            if len(text) > max_cell:
                text = text[:max_cell] + "…"
            cells.append(text)
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue
        lines.append(f"{r}\t" + "\t".join(cells))
    return "\n".join(lines)


def _data_row_indices(grid: Grid, first: int, last: int) -> list[int]:
    out = []
    for r in range(max(0, first), min(last + 1, grid.nrows)):
        if not grid.is_blank(r):
            out.append(r)
    return out


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------

def _make_client(config: AIConfig):
    if config.client is not None:
        return config.client

    # Check the key before the import: a missing key is the far more common
    # setup problem, and it is the more actionable of the two messages.
    key = config.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise AIError(
            "No API key. Set ANTHROPIC_API_KEY, pass AIConfig(api_key=...), "
            "or use the rule engine with --engine rules."
        )
    try:
        from anthropic import Anthropic
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise AIError(
            "The anthropic package is required for the AI engine. "
            "Install it with: pip install anthropic"
        ) from exc
    # The SDK's own default timeout is generous (minutes). A conversion makes
    # several sequential calls, so a single stalled one - a network hiccup
    # reaching the API, an overloaded model - should fail fast with a clear
    # AIError rather than leave the request (and the worker thread serving
    # it) hanging far longer than any real batch takes.
    return Anthropic(api_key=key, timeout=120.0)


def _call(config: AIConfig, *, system: str, user: str, schema: dict,
          max_tokens: int) -> dict:
    """One structured-output request. Returns the parsed JSON object."""
    client = _make_client(config)
    try:
        response = client.messages.create(
            model=config.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except Exception as exc:  # noqa: BLE001 - surface transport errors plainly
        raise AIError(f"Claude request failed: {type(exc).__name__}: {exc}") from exc

    stop = getattr(response, "stop_reason", None)
    if stop == "max_tokens":
        raise AIError(
            "Response hit max_tokens and is truncated. Lower batch_rows or "
            "raise max_tokens."
        )
    if stop == "refusal":
        raise AIError("The model declined to process this content.")

    text = ""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text += getattr(block, "text", "")
    if not text.strip():
        raise AIError("Empty response from the model.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIError(f"Model returned invalid JSON: {exc}") from exc


# --------------------------------------------------------------------------
# passes
# --------------------------------------------------------------------------

def extract_metadata(grid: Grid, config: AIConfig) -> list[dict]:
    """Pass 1: accounts, their control figures and the column mapping."""
    head = render_rows(grid, 0, METADATA_ROWS)
    tail_start = max(METADATA_ROWS, grid.nrows - 15)
    tail = render_rows(grid, tail_start, grid.nrows)
    body = (
        f"The sheet has {grid.nrows} rows and {grid.ncols} columns.\n\n"
        f"First rows:\n{head}\n\n"
        f"Last rows (rows {tail_start}-{grid.nrows - 1}):\n{tail}\n"
    )
    if grid.nrows > METADATA_ROWS + 15:
        body += (
            "\nRows between those two blocks are transaction rows of the same "
            "shape. If several account sections exist, their captions appear "
            "in the omitted range too; report the ones you can see and set "
            "last_data_row to the last row you can justify."
        )
    if config.extra_instructions:
        body += "\n\n" + config.extra_instructions

    data = _call(config, system=METADATA_SYSTEM, user=body,
                 schema=METADATA_SCHEMA, max_tokens=config.metadata_max_tokens)
    accounts = data.get("accounts") or []
    if not accounts:
        raise AIError("The model found no account sections in this file.")
    return accounts


def extract_transactions(grid: Grid, meta: dict, config: AIConfig) -> list[Transaction]:
    """Pass 2: the rows of one account, in batches."""
    first = int(meta.get("first_data_row", 0))
    last = int(meta.get("last_data_row", grid.nrows - 1))
    if last < first:
        return []

    rows = _data_row_indices(grid, first, last)
    if not rows:
        return []

    columns = meta.get("columns") or {}
    layout = meta.get("layout", "row")
    mapping = json.dumps(columns, ensure_ascii=False)

    out: list[Transaction] = []
    seen_rows: set[int] = set()
    step = max(1, config.batch_rows)

    for i in range(0, len(rows), step):
        chunk = rows[i:i + step]
        # A block layout must not be cut between an amount row and its
        # continuation lines, so extend the chunk to the next amount row.
        end_row = chunk[-1]
        if layout == "block":
            end_row = min(end_row + 3, last)
        text = render_rows(grid, chunk[0], end_row + 1)
        if not text.strip():
            continue

        body = (
            f"Account {meta.get('account_number', '')} "
            f"({meta.get('currency', '')}), layout: {layout}.\n"
            f"Column mapping (0-based, -1 means absent): {mapping}\n\n"
            f"Extract transactions from these rows. Report only transactions "
            f"whose amount row index is between {chunk[0]} and {chunk[-1]} "
            f"inclusive.\n\n{text}"
        )
        data = _call(config, system=TRANSACTIONS_SYSTEM, user=body,
                     schema=TRANSACTIONS_SCHEMA, max_tokens=config.max_tokens)

        for item in data.get("transactions", []):
            tx = _to_transaction(item, config)
            if tx is None:
                continue
            # Overlapping context between batches can repeat a row.
            if tx.source_row in seen_rows:
                continue
            seen_rows.add(tx.source_row)
            out.append(tx)

    return out


def _to_transaction(item: dict, config: AIConfig) -> Transaction | None:
    """Validate and normalise one model-produced transaction."""
    debit = parse_amount(item.get("debit_amount"))
    credit = parse_amount(item.get("credit_amount"))
    if debit == 0 and credit == 0:
        return None
    # A posting is one or the other. If the model reports both, keep the
    # larger and let reconciliation catch the consequence.
    if debit and credit:
        if debit >= credit:
            credit = Decimal("0")
        else:
            debit = Decimal("0")

    dt = parse_datetime(item.get("transaction_date"))
    account = re.sub(r"\D", "", str(item.get("counterparty_account") or ""))
    inn = re.sub(r"\D", "", str(item.get("counterparty_inn") or ""))
    name = clean_text(item.get("counterparty_name"))
    if config.name_style == "composite":
        name = "/".join(p for p in (account, inn, name) if p) or name

    bank_code = re.sub(r"\D", "", str(item.get("bank_code") or ""))
    if bank_code and len(bank_code) < 5:
        bank_code = bank_code.zfill(5)

    return Transaction(
        transaction_date=format_datetime(dt),
        document_number=clean_text(item.get("document_number")),
        credit_amount=credit,
        debit_amount=debit,
        counterparty_name=name,
        counterparty_account=account,
        bank_code=bank_code,
        payment_purpose=clean_text(item.get("payment_purpose")),
        counterparty_inn=inn,
        source_row=int(item.get("source_row", -1)),
    )


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------

def _decimal_or_none(text: object):
    text = clean_text(text)
    if not text:
        return None
    value = parse_amount(text)
    return value


def convert_grid(grid: Grid, config: AIConfig) -> list[Account]:
    accounts: list[Account] = []
    for meta in extract_metadata(grid, config):
        transactions = extract_transactions(grid, meta, config)
        account_number = re.sub(r"\D", "", str(meta.get("account_number") or ""))
        currency = (
            clean_text(meta.get("currency")).upper()
            or currency_from_account(account_number)
            or config.default_currency
        )
        account = Account(
            account_number=account_number,
            currency=currency,
            account_name=clean_text(meta.get("account_name")),
            period_from=clean_text(meta.get("period_from")),
            period_to=clean_text(meta.get("period_to")),
            transactions=transactions,
        )
        totals: dict[str, object] = {}
        for key, source in (("opening_balance", "opening_balance"),
                            ("closing_balance", "closing_balance"),
                            ("stated_debit", "stated_debit_total"),
                            ("stated_credit", "stated_credit_total")):
            value = _decimal_or_none(meta.get(source))
            if value is not None:
                totals[key] = value
        if meta.get("stated_count"):
            totals["stated_count"] = int(meta["stated_count"])
        account.reconciliation = reconcile(account, totals)
        accounts.append(account)
    return accounts


def _needs_reference_fallback(ai_statement: Statement, reference: Statement) -> bool:
    """Hard cases like multi-account Ipoteka files are not safe to trust when
    the model omits later account sections or drifts from the canonical totals.
    """
    if not reference.active_accounts:
        return False

    # A multi-account export is only acceptable when the AI result reaches the
    # same account set and totals as the deterministic reference.
    if len(reference.active_accounts) > 1:
        ai_by_account = {a.account_number: a for a in ai_statement.active_accounts}
        ref_by_account = {a.account_number: a for a in reference.active_accounts}

        only_rules = sorted(set(ref_by_account) - set(ai_by_account))
        only_ai = sorted(set(ai_by_account) - set(ref_by_account))
        if only_rules or only_ai:
            return True

        for number, ref_account in ref_by_account.items():
            ai_account = ai_by_account.get(number)
            if ai_account is None:
                return True
            if len(ai_account.transactions) != len(ref_account.transactions):
                return True
            if abs(ai_account.total_debit - ref_account.total_debit) > Decimal("0.01"):
                return True
            if abs(ai_account.total_credit - ref_account.total_credit) > Decimal("0.01"):
                return True

    return False


def convert_with_claude(
    source, filename: str = "", config: AIConfig | None = None
) -> Statement:
    """Convert a statement using Claude, then reconcile and cross-check it."""
    config = config or AIConfig()
    grids = load(source, filename)
    statement = Statement(source_file=filename or str(source), layout="ai")

    if grids:
        head = " ".join(grids[0].row_text(r) for r in range(min(grids[0].nrows, 12)))
        statement.bank = detect_bank(head)

    for grid in grids:
        try:
            statement.accounts.extend(convert_grid(grid, config))
        except AIError as exc:
            if grid is grids[0]:
                raise
            statement.warnings.append(f"Sheet {grid.sheet_name}: {exc}")

    warnings: list[str] = []
    if config.verify:
        warnings.extend(_cross_check(source, filename, statement, config))

    try:
        from .engine import parse_file as rules_parse
        reference = rules_parse(source, filename, name_style=config.name_style,
                                default_currency=config.default_currency)
    except Exception:  # pragma: no cover - only used as a fallback
        reference = None

    if reference is not None and _needs_reference_fallback(statement, reference):
        reference.warnings = list(warnings)
        reference.warnings.append(
            "AI output deviated from the deterministic reference; using the rule engine for this file."
        )
        return reference

    statement.warnings.extend(warnings)
    return statement


def _cross_check(source, filename: str, statement: Statement,
                 config: AIConfig) -> list[str]:
    """Compare the model's output with the deterministic parser.

    The rule engine is cheap and independent, so disagreement is worth
    surfacing even when both pass reconciliation.
    """
    from .engine import parse_file as rules_parse

    warnings: list[str] = []
    try:
        reference = rules_parse(source, filename, name_style=config.name_style,
                                default_currency=config.default_currency)
    except Exception as exc:  # noqa: BLE001 - a cross-check must never break the run
        return [f"Cross-check skipped: rule engine failed ({type(exc).__name__}: {exc})."]

    ai_by_account = {a.account_number: a for a in statement.active_accounts}
    ref_by_account = {a.account_number: a for a in reference.active_accounts}

    only_rules = sorted(set(ref_by_account) - set(ai_by_account))
    only_ai = sorted(set(ai_by_account) - set(ref_by_account))
    for number in only_rules:
        warnings.append(
            f"Account {number} was found by the rule engine but not by the model."
        )
    for number in only_ai:
        warnings.append(
            f"Account {number} was reported by the model but not by the rule engine."
        )

    for number, ai_account in ai_by_account.items():
        ref = ref_by_account.get(number)
        if ref is None:
            continue
        if len(ai_account.transactions) != len(ref.transactions):
            warnings.append(
                f"Account {number}: model extracted {len(ai_account.transactions)} "
                f"transactions, rule engine {len(ref.transactions)}."
            )
        for label, a, b in (
            ("debit", ai_account.total_debit, ref.total_debit),
            ("credit", ai_account.total_credit, ref.total_credit),
        ):
            if abs(a - b) > Decimal("0.01"):
                warnings.append(
                    f"Account {number}: {label} total differs — model {a}, "
                    f"rule engine {b} (difference {a - b})."
                )
    return warnings
