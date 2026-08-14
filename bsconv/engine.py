"""The parser itself: grid + detected structure -> Statement."""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal

from .detect import (
    Section, build_sections, detect_layout, extract_control_totals,
    extract_metadata, find_headers, is_control_row, is_numbering_row,
    is_section_header_row, row_has_data,
)
from .loaders import Grid, load
from .models import Account, Statement, Transaction
from .normalize import (
    clean_text, currency_from_account, currency_from_text, format_datetime,
    normalize_document_number, parse_amount, parse_counterparty_blob,
    parse_datetime, parse_time, strip_trailing_inn,
)
from .reconcile import reconcile
from .vocabulary import detect_bank

NAME_STYLE_CLEAN = "clean"
NAME_STYLE_COMPOSITE = "composite"

# The reference file (bank_statement.json) writes counterparty_name as
# "account/INN/NAME", so that is the default for every bank. Pass
# name_style="clean" for the bare name instead.
DEFAULT_NAME_STYLE = NAME_STYLE_COMPOSITE


# --------------------------------------------------------------------------
# transaction construction
# --------------------------------------------------------------------------

def _compose_name(fields: dict[str, str], style: str) -> str:
    """Render counterparty_name either cleanly or in "acct/inn/name" form."""
    name = fields.get("name", "")
    if style != NAME_STYLE_COMPOSITE:
        return name
    parts = [fields.get("account", ""), fields.get("inn", ""), name]
    return "/".join(p for p in parts if p) or name


def _counterparty(grid: Grid, r: int, section: Section) -> dict[str, str]:
    """Resolve counterparty account / INN / name / bank code for a row."""
    header = section.header
    fields = {"account": "", "inn": "", "name": "", "bank_code": "", "purpose": ""}

    blob_col = header.col("corr_blob")
    if blob_col is not None:
        raw_blob = grid.cell(r, blob_col)
        # clean_text() would collapse the newlines the splitter relies on.
        parsed = parse_counterparty_blob(
            raw_blob if isinstance(raw_blob, str) else clean_text(raw_blob)
        )
        fields.update({k: v for k, v in parsed.items() if k != "purpose"})
        fields["purpose"] = parsed.get("purpose", "")

    acct_col = header.col("corr_account")
    if acct_col is not None:
        acct_raw = clean_text(grid.cell(r, acct_col))
        parsed = parse_counterparty_blob(acct_raw)
        fields["account"] = parsed["account"] or re.sub(r"\D", "", acct_raw) or fields["account"]
        fields["inn"] = fields["inn"] or parsed["inn"]
        if parsed["name"] and not fields["name"]:
            fields["name"] = parsed["name"]

    name_col = header.col("corr_name")
    if name_col is not None:
        raw = clean_text(grid.cell(r, name_col))
        if raw:
            name, inn = strip_trailing_inn(raw)
            fields["name"] = name or raw
            fields["inn"] = fields["inn"] or inn

    inn_col = header.col("inn")
    if inn_col is not None:
        inn = re.sub(r"\D", "", clean_text(grid.cell(r, inn_col)))
        if inn and inn != "000000000":
            fields["inn"] = inn

    mfo_col = header.col("mfo")
    if mfo_col is not None:
        mfo = clean_text(grid.cell(r, mfo_col))
        mfo = re.sub(r"\D", "", mfo)
        if mfo:
            # MFO codes are five digits; some exports drop the leading zero.
            fields["bank_code"] = mfo.zfill(5) if len(mfo) < 5 else mfo

    return fields


def _row_datetime(grid: Grid, r: int, section: Section) -> _dt.datetime | None:
    header = section.header
    for role in ("date", "doc_date"):
        col = header.col(role)
        if col is None:
            continue
        dt = parse_datetime(grid.cell(r, col))
        if dt:
            return dt
    # Fall back to any date-looking cell in the row.
    for c in range(grid.ncols):
        if c in {header.col("purpose"), header.col("debit"), header.col("credit")}:
            continue
        dt = parse_datetime(grid.cell(r, c))
        if dt and 1990 <= dt.year <= 2100:
            return dt
    return None


def _build_transaction(
    grid: Grid, r: int, section: Section, name_style: str
) -> Transaction | None:
    header = section.header
    d_col, c_col = header.col("debit"), header.col("credit")
    debit = parse_amount(grid.cell(r, d_col)) if d_col is not None else Decimal("0")
    credit = parse_amount(grid.cell(r, c_col)) if c_col is not None else Decimal("0")
    if debit == 0 and credit == 0:
        return None

    dt = _row_datetime(grid, r, section)
    fields = _counterparty(grid, r, section)

    purpose_col = header.col("purpose")
    purpose = clean_text(grid.cell(r, purpose_col)) if purpose_col is not None else ""
    if not purpose:
        # Xalq Bank's HTML export has no purpose column; the purpose rides
        # along in the correspondent cell after the name.
        purpose = fields.get("purpose", "")

    doc_col = header.col("doc_number")
    doc = normalize_document_number(grid.cell(r, doc_col)) if doc_col is not None else ""

    op_col = header.col("op_code")
    op = clean_text(grid.cell(r, op_col)) if op_col is not None else ""
    if op.endswith(".0"):
        op = op[:-2]

    return Transaction(
        transaction_date=format_datetime(dt),
        document_number=doc,
        credit_amount=credit,
        debit_amount=debit,
        counterparty_name=_compose_name(fields, name_style),
        counterparty_account=fields["account"],
        bank_code=fields["bank_code"],
        payment_purpose=purpose,
        counterparty_inn=fields["inn"],
        operation_code=op,
        source_row=r + 1,
    )


# --------------------------------------------------------------------------
# layout parsers
# --------------------------------------------------------------------------

def _parse_row_layout(
    grid: Grid, section: Section, name_style: str
) -> list[Transaction]:
    out: list[Transaction] = []
    for r in range(section.body_start, section.body_end + 1):
        if grid.is_blank(r) or is_numbering_row(grid, r, section.header):
            continue
        if is_control_row(grid, r, section.header):
            continue
        if not row_has_data(grid, r, section.header):
            continue
        tx = _build_transaction(grid, r, section, name_style)
        if tx is not None:
            out.append(tx)
    return out


def _parse_block_layout(
    grid: Grid, section: Section, name_style: str
) -> list[Transaction]:
    """One transaction spans several rows.

    Line 1: date, document number, operation code, correspondent header, amounts
    Line 2: time, correspondent name
    Line 3+: payment purpose (may wrap over further lines)
    """
    header = section.header
    date_col = header.col("date") or 0
    name_col = header.col("corr_blob") or header.col("corr_name") or header.col("purpose")
    out: list[Transaction] = []

    r = section.body_start
    while r <= section.body_end:
        if grid.is_blank(r) or is_control_row(grid, r, header) or not row_has_data(
            grid, r, section.header
        ):
            r += 1
            continue

        tx = _build_transaction(grid, r, section, name_style)
        if tx is None:
            r += 1
            continue

        # Consume continuation lines until the next row that starts a block.
        #
        # The limit is deliberately the footer, not body_end: body_end is the
        # last row carrying an *amount*, so the final transaction of a section
        # keeps its name and purpose on rows beyond it. Stopping at body_end
        # silently dropped both for one transaction per section.
        limit = min(max(section.body_end, section.footer_end), grid.nrows - 1)
        k = r + 1
        name_parts: list[str] = []
        purpose_parts: list[str] = []
        time_value = None
        while k <= limit:
            if (row_has_data(grid, k, section.header)
                    or is_control_row(grid, k, header)
                    or is_section_header_row(grid, k)):
                break
            if grid.is_blank(k):
                k += 1
                continue
            first = clean_text(grid.cell(k, date_col))
            t = parse_time(first)
            if t is not None and time_value is None:
                time_value = t
            if name_col is not None:
                text = clean_text(grid.cell(k, name_col))
                if text:
                    (name_parts if not name_parts and not purpose_parts
                     else purpose_parts).append(text)
            k += 1

        if name_parts:
            blob = " ".join(name_parts)
            name, inn = strip_trailing_inn(blob)
            fields = {
                "account": tx.counterparty_account,
                "inn": tx.counterparty_inn or inn,
                "name": name or blob,
            }
            tx.counterparty_name = _compose_name(fields, name_style)
            tx.counterparty_inn = fields["inn"]
        if purpose_parts:
            tx.payment_purpose = clean_text(" ".join(purpose_parts))
        if time_value is not None and tx.transaction_date:
            base = parse_datetime(tx.transaction_date)
            if base is not None:
                tx.transaction_date = format_datetime(
                    _dt.datetime.combine(base.date(), time_value)
                )
        out.append(tx)
        r = k
    return out


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------

def parse_grid(
    grid: Grid,
    *,
    name_style: str = DEFAULT_NAME_STYLE,
    default_currency: str = "UZS",
) -> tuple[list[Account], list[str], str]:
    """Parse one worksheet into accounts. Returns (accounts, warnings, layout)."""
    warnings: list[str] = []
    headers = find_headers(grid)
    if not headers:
        return [], ["No transaction table header found in this sheet."], ""

    sections = build_sections(grid, headers)
    accounts: list[Account] = []
    layouts: set[str] = set()

    for section in sections:
        section.layout = detect_layout(grid, section)
        layouts.add(section.layout)
        meta = extract_metadata(grid, section)
        totals = extract_control_totals(grid, section)

        if section.layout == "block":
            transactions = _parse_block_layout(grid, section, name_style)
        else:
            transactions = _parse_row_layout(grid, section, name_style)

        account_number = meta.get("account_number", "")
        currency = (
            currency_from_account(account_number)
            or currency_from_text(meta.get("preamble", ""))
            or default_currency
        )

        account = Account(
            account_number=account_number,
            currency=currency,
            account_name=meta.get("account_name", ""),
            client_inn=meta.get("client_inn", ""),
            period_from=meta.get("period_from", ""),
            period_to=meta.get("period_to", ""),
            transactions=transactions,
        )
        if not account_number:
            account.warnings.append(
                "Account number not found in the section preamble."
            )
        missing_dates = sum(1 for t in transactions if not t.transaction_date)
        if missing_dates:
            account.warnings.append(
                f"{missing_dates} transaction(s) have no parseable date."
            )
        account.reconciliation = reconcile(account, totals)
        accounts.append(account)

    return accounts, warnings, "+".join(sorted(layouts))


def parse_file(
    source,
    filename: str = "",
    *,
    name_style: str = DEFAULT_NAME_STYLE,
    default_currency: str = "UZS",
) -> Statement:
    """Parse a statement file (path or bytes) into a Statement."""
    grids = load(source, filename)
    statement = Statement(source_file=filename or str(source))

    # Only the header block: further down, correspondent-bank names would
    # make every statement look like it came from whichever bank the client
    # last paid.
    # Ipoteka prints its branch line as a section footer rather than a header,
    # so the tail is sampled too - but only the last few rows, which are never
    # transactions.
    bank_hint = ""
    if grids:
        grid = grids[0]
        rows = sorted(set(
            list(range(min(grid.nrows, 12)))
            + list(range(max(0, grid.nrows - 3), grid.nrows))
        ))
        bank_hint = " ".join(grid.row_text(r) for r in rows)
    statement.bank = detect_bank(bank_hint)

    layouts: set[str] = set()
    for grid in grids:
        accounts, warnings, layout = parse_grid(
            grid, name_style=name_style, default_currency=default_currency
        )
        statement.accounts.extend(accounts)
        # Only surface "no table here" for the first sheet; workbooks routinely
        # carry empty Лист2 / Лист3 sheets.
        if warnings and grid is grids[0]:
            statement.warnings.extend(warnings)
        if layout:
            layouts.add(layout)

    statement.layout = "+".join(sorted(layouts))
    if not statement.accounts:
        statement.warnings.append(
            "No account sections could be parsed from this file."
        )

    # Deduplicate identical account numbers appearing across sheets.
    seen: dict[str, Account] = {}
    merged: list[Account] = []
    for account in statement.accounts:
        key = account.account_number or f"__anon{len(merged)}"
        if key in seen and not account.transactions:
            continue
        seen[key] = account
        merged.append(account)
    statement.accounts = merged
    return statement
