"""Tests for the Claude engine, driven by a fake client.

No API key and no network are needed: a stub client returns canned responses,
which is enough to verify prompt construction, batching, schema use, response
normalisation, reconciliation and the cross-check against the rule engine.

Set ANTHROPIC_API_KEY and BSCONV_LIVE=1 to additionally run the live test at
the bottom against the real API.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from bsconv.ai import (
    AIConfig, AIError, METADATA_SCHEMA, TRANSACTIONS_SCHEMA, convert_with_claude,
    extract_metadata, extract_transactions, render_rows,
)
from bsconv.loaders import Grid, load

SAMPLES = Path(os.environ.get("BSCONV_SAMPLES", Path(__file__).parent.parent / "samples"))


# --------------------------------------------------------------------------
# fake client
# --------------------------------------------------------------------------

class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Response:
    def __init__(self, payload: dict, stop_reason: str = "end_turn") -> None:
        self.content = [_Block(json.dumps(payload, ensure_ascii=False))]
        self.stop_reason = stop_reason


class _Messages:
    def __init__(self, owner: "FakeClient") -> None:
        self._owner = owner

    def create(self, **kwargs):
        self._owner.calls.append(kwargs)
        return self._owner.next_response(kwargs)


class FakeClient:
    """Records requests and replays scripted responses."""

    def __init__(self, metadata: dict, batches: list[dict] | None = None,
                 stop_reason: str = "end_turn") -> None:
        self.metadata = metadata
        self.batches = list(batches or [])
        self.calls: list[dict] = []
        self.stop_reason = stop_reason
        self.messages = _Messages(self)

    def next_response(self, kwargs: dict) -> _Response:
        schema = kwargs["output_config"]["format"]["schema"]
        if schema is METADATA_SCHEMA or "accounts" in schema.get("properties", {}):
            return _Response(self.metadata, self.stop_reason)
        payload = self.batches.pop(0) if self.batches else {"transactions": []}
        return _Response(payload, self.stop_reason)


def _account_meta(**overrides) -> dict:
    meta = {
        "account_number": "20208000505569397001",
        "currency": "UZS",
        "account_name": "TEST MCHJ",
        "period_from": "01.08.2025",
        "period_to": "31.08.2025",
        "opening_balance": "1000",
        "closing_balance": "700",
        "stated_debit_total": "300",
        "stated_credit_total": "0",
        "stated_count": 1,
        "header_row": 0,
        "first_data_row": 1,
        "last_data_row": 2,
        "layout": "row",
        "columns": {
            "date": 0, "document_number": 1, "debit": 2, "credit": 3,
            "counterparty_account": 4, "counterparty_name": 5,
            "bank_code": 6, "payment_purpose": 7,
        },
    }
    meta.update(overrides)
    return meta


def _tx(**overrides) -> dict:
    tx = {
        "transaction_date": "01.08.2025 16:07:00",
        "document_number": "261200",
        "credit_amount": "0",
        "debit_amount": "300",
        "counterparty_name": "TOShKENT Sh. AT ALOQABANK",
        "counterparty_account": "16401000905569397001",
        "bank_code": "00401",
        "payment_purpose": "За перевод электронных платежей",
        "counterparty_inn": "309882946",
        "source_row": 1,
    }
    tx.update(overrides)
    return tx


def _grid(rows: int = 3) -> Grid:
    data = [["Дата", "№ док", "Дебет", "Кредит", "Счет", "Имя", "МФО", "Назначение"]]
    for i in range(rows):
        data.append([f"0{i + 1}.08.2025", str(100 + i), "300", "0",
                     "16401000905569397001", "ALOQABANK", "00401", "комиссия"])
    return Grid(rows=data, sheet_name="t", source_format="xlsx")


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def test_render_rows_is_indexed_and_tab_separated():
    text = render_rows(_grid(2), 0, 3)
    lines = text.split("\n")
    assert lines[0].startswith("0\tДата\t")
    assert lines[1].startswith("1\t01.08.2025\t")
    assert "\t" in lines[1]


def test_render_rows_truncates_huge_cells():
    grid = Grid(rows=[["x" * 5000, "1"]], sheet_name="t", source_format="xlsx")
    text = render_rows(grid, 0, 1, max_cell=50)
    assert "…" in text
    assert len(text) < 200


def test_render_rows_skips_blank_rows():
    grid = Grid(rows=[["a"], [None, None], ["b"]], sheet_name="t",
                source_format="xlsx")
    text = render_rows(grid, 0, 3)
    assert text.split("\n") == ["0\ta", "2\tb"]


# --------------------------------------------------------------------------
# metadata pass
# --------------------------------------------------------------------------

def test_metadata_pass_uses_structured_outputs():
    client = FakeClient({"accounts": [_account_meta()]})
    config = AIConfig(client=client)
    accounts = extract_metadata(_grid(), config)

    assert accounts[0]["account_number"] == "20208000505569397001"
    call = client.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["format"]["schema"] is METADATA_SCHEMA
    assert "system" in call and call["max_tokens"] > 0


def test_metadata_pass_raises_when_no_accounts_found():
    config = AIConfig(client=FakeClient({"accounts": []}))
    with pytest.raises(AIError, match="no account sections"):
        extract_metadata(_grid(), config)


# --------------------------------------------------------------------------
# transaction pass
# --------------------------------------------------------------------------

def test_amounts_are_parsed_exactly_not_via_float():
    client = FakeClient({"accounts": [_account_meta()]},
                        [{"transactions": [_tx(debit_amount="1 550 464,98")]}])
    txs = extract_transactions(_grid(), _account_meta(), AIConfig(client=client))
    assert txs[0].debit_amount == Decimal("1550464.98")
    assert str(txs[0].debit_amount) == "1550464.98"


def test_rows_with_no_amount_are_dropped():
    client = FakeClient(
        {"accounts": [_account_meta()]},
        [{"transactions": [_tx(debit_amount="0", credit_amount="0"), _tx()]}],
    )
    txs = extract_transactions(_grid(), _account_meta(), AIConfig(client=client))
    assert len(txs) == 1


def test_a_posting_cannot_be_both_debit_and_credit():
    client = FakeClient(
        {"accounts": [_account_meta()]},
        [{"transactions": [_tx(debit_amount="500", credit_amount="20")]}],
    )
    txs = extract_transactions(_grid(), _account_meta(), AIConfig(client=client))
    assert txs[0].debit_amount == Decimal("500")
    assert txs[0].credit_amount == Decimal("0")


def test_duplicate_source_rows_are_dropped():
    client = FakeClient(
        {"accounts": [_account_meta()]},
        [{"transactions": [_tx(source_row=1), _tx(source_row=1)]}],
    )
    txs = extract_transactions(_grid(), _account_meta(), AIConfig(client=client))
    assert len(txs) == 1


def test_composite_name_style_is_applied():
    client = FakeClient({"accounts": [_account_meta()]}, [{"transactions": [_tx()]}])
    txs = extract_transactions(_grid(), _account_meta(),
                               AIConfig(client=client, name_style="composite"))
    assert txs[0].counterparty_name == (
        "16401000905569397001/309882946/TOShKENT Sh. AT ALOQABANK"
    )


def test_clean_name_style_is_applied():
    client = FakeClient({"accounts": [_account_meta()]}, [{"transactions": [_tx()]}])
    txs = extract_transactions(_grid(), _account_meta(),
                               AIConfig(client=client, name_style="clean"))
    assert txs[0].counterparty_name == "TOShKENT Sh. AT ALOQABANK"


def test_bank_code_is_zero_padded():
    client = FakeClient({"accounts": [_account_meta()]},
                        [{"transactions": [_tx(bank_code="401")]}])
    txs = extract_transactions(_grid(), _account_meta(), AIConfig(client=client))
    assert txs[0].bank_code == "00401"


def test_rows_are_batched():
    """20 data rows at 5 per batch is 4 requests, and every row is covered."""
    grid = _grid(20)
    meta = _account_meta(first_data_row=1, last_data_row=20)
    batches = [
        {"transactions": [_tx(source_row=r) for r in range(start, start + 5)]}
        for start in range(1, 21, 5)
    ]
    client = FakeClient({"accounts": [meta]}, batches)
    txs = extract_transactions(grid, meta, AIConfig(client=client, batch_rows=5))

    assert len(client.calls) == 4
    assert len(txs) == 20
    assert sorted(t.source_row for t in txs) == list(range(1, 21))


def test_truncated_response_is_an_error_not_silent_data_loss():
    client = FakeClient({"accounts": [_account_meta()]},
                        [{"transactions": [_tx()]}], stop_reason="max_tokens")
    with pytest.raises(AIError, match="max_tokens"):
        extract_metadata(_grid(), AIConfig(client=client))


def test_refusal_is_reported():
    client = FakeClient({"accounts": [_account_meta()]}, stop_reason="refusal")
    with pytest.raises(AIError, match="declined"):
        extract_metadata(_grid(), AIConfig(client=client))


def test_missing_api_key_gives_an_actionable_message(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AIError, match="rules"):
        extract_metadata(_grid(), AIConfig())


# --------------------------------------------------------------------------
# end to end, still fake
# --------------------------------------------------------------------------

def _sample(name: str) -> Path:
    path = SAMPLES / name
    if not path.exists():
        pytest.skip(f"sample {name} not available")
    return path


def test_convert_produces_the_reference_schema_and_reconciles():
    client = FakeClient({"accounts": [_account_meta()]}, [{"transactions": [_tx()]}])
    statement = convert_with_claude(
        _sample("aloqabank.xlsx"), "aloqabank.xlsx",
        AIConfig(client=client, verify=False),
    )
    account = statement.active_accounts[0]
    payload = account.to_json()

    assert list(payload) == ["account_number", "currency", "transaction_count",
                             "total_credit", "total_debit", "transactions"]
    assert payload["transaction_count"] == 1
    assert payload["total_debit"] == 300
    # opening 1000 + credit 0 - debit 300 == stated closing 700
    assert account.reconciliation.checks["balance"] == "pass"
    assert account.reconciliation.checks["turnover"] == "pass"


def test_currency_falls_back_to_the_account_number():
    meta = _account_meta(account_number="20208840805703819001", currency="")
    client = FakeClient({"accounts": [meta]}, [{"transactions": [_tx()]}])
    statement = convert_with_claude(
        _sample("aloqabank.xlsx"), "aloqabank.xlsx",
        AIConfig(client=client, verify=False),
    )
    assert statement.accounts[0].currency == "USD"


def test_cross_check_flags_a_transaction_count_disagreement():
    """Same account, but the model reports 1 transaction and the parser 127."""
    meta = _account_meta(account_number="20208000105674759001")
    client = FakeClient({"accounts": [meta]}, [{"transactions": [_tx()]}])
    statement = convert_with_claude(
        _sample("aloqabank.xlsx"), "aloqabank.xlsx",
        AIConfig(client=client, verify=True),
    )
    joined = " ".join(statement.warnings)
    assert "rule engine" in joined
    assert "127" in joined


def test_cross_check_flags_a_missing_account():
    """The model reports an account the parser never saw, and vice versa."""
    client = FakeClient({"accounts": [_account_meta()]}, [{"transactions": [_tx()]}])
    statement = convert_with_claude(
        _sample("aloqabank.xlsx"), "aloqabank.xlsx",
        AIConfig(client=client, verify=True),
    )
    joined = " ".join(statement.warnings)
    assert "found by the rule engine but not by the model" in joined
    assert "reported by the model but not by the rule engine" in joined


def test_reconciliation_catches_a_wrong_total():
    """A hallucinated amount must not pass silently."""
    meta = _account_meta(stated_debit_total="300")
    client = FakeClient({"accounts": [meta]},
                        [{"transactions": [_tx(debit_amount="30000")]}])
    statement = convert_with_claude(
        _sample("aloqabank.xlsx"), "aloqabank.xlsx",
        AIConfig(client=client, verify=False),
    )
    rec = statement.accounts[0].reconciliation
    assert rec.checks["turnover"] == "fail"
    assert not rec.passed


# --------------------------------------------------------------------------
# live test, opt in
# --------------------------------------------------------------------------

@pytest.mark.skipif(
    not (os.environ.get("BSCONV_LIVE") and os.environ.get("ANTHROPIC_API_KEY")),
    reason="set BSCONV_LIVE=1 and ANTHROPIC_API_KEY to run against the real API",
)
def test_live_conversion_matches_the_rule_engine():
    from bsconv import parse_file

    name = "tangebank.xlsx"          # smallest sample: 24 transactions
    path = _sample(name)
    ai = convert_with_claude(path, name, AIConfig(verify=False))
    rules = parse_file(path, name)

    ai_account = ai.active_accounts[0]
    ref = rules.active_accounts[0]
    assert ai_account.account_number == ref.account_number
    assert len(ai_account.transactions) == len(ref.transactions)
    assert ai_account.total_debit == ref.total_debit
    assert ai_account.total_credit == ref.total_credit
