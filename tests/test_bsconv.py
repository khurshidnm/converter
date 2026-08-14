"""Regression suite.

Unit tests run anywhere. The corpus tests run only when a directory of real
statements is available; point BSCONV_SAMPLES at it, or drop files into
./samples next to the package.

    pytest -q
    BSCONV_SAMPLES=/path/to/statements pytest -q
"""

from __future__ import annotations

import datetime as dt
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from bsconv import parse_file
from bsconv.normalize import (
    currency_from_account, parse_amount, parse_counterparty_blob, parse_datetime,
    strip_trailing_inn,
)
from bsconv.vocabulary import match_role, normalize_label

SAMPLES = Path(os.environ.get("BSCONV_SAMPLES", Path(__file__).parent.parent / "samples"))

# Expected results per sample file, verified against the control totals the
# banks print on the statements themselves.
EXPECTED: dict[str, dict] = {
    "aab.xlsx":            {"accounts": 1, "txns": [137]},
    "agrobank.xls":        {"accounts": 1, "txns": [134]},
    "aloqabank.xls":       {"accounts": 1, "txns": [350]},
    "aloqabank.xlsx":      {"accounts": 1, "txns": [127]},
    "davrbank.xlsx":       {"accounts": 1, "txns": [97]},
    "hamkorbank.xls":      {"accounts": 1, "txns": [101]},
    "ipakyuli.xlsx":       {"accounts": 1, "txns": [105], "reconciles": False},
    "ipotekabank-v1.xlsx": {"accounts": 3, "txns": [149, 24, 199]},
    "ipotekabank-v2.xls":  {"accounts": 3, "txns": [79, 14, 144]},
    "mikrokredit.xls":     {"accounts": 1, "txns": [249]},
    "mkb.xls":             {"accounts": 1, "txns": [26]},
    "tangebank.xlsx":      {"accounts": 1, "txns": [24]},
    "xalqbank.xls":        {"accounts": 1, "txns": [70]},
    "xalqbank.xlsx":       {"accounts": 1, "txns": [484]},
}

SAMPLE_FILES = sorted(EXPECTED)
REFERENCE_KEYS = [
    "transaction_date", "document_number", "credit_amount", "debit_amount",
    "counterparty_name", "counterparty_account", "bank_code", "payment_purpose",
]


# --------------------------------------------------------------------------
# amount parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("1 234,56", "1234.56"),
    ("1,234.56", "1234.56"),
    ("3 378 943 223,69", "3378943223.69"),
    ("1,071,000.00", "1071000.00"),
    (".00", "0.00"),
    ("0,00", "0.00"),
    ("55 274.30", "55274.30"),
    ("", "0"),
    (None, "0"),
    ("-", "0"),
    ("(1 234.56)", "-1234.56"),
    (1735000, "1735000"),
    (1550464.98, "1550464.98"),
    ("2 100,00", "2100.00"),
    ("201,600,000.00", "201600000.00"),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == Decimal(expected)


def test_parse_amount_never_loses_precision():
    # Float round-tripping must not turn 1550464.98 into 1550464.9800000002.
    assert str(parse_amount(1550464.98)) == "1550464.98"


# --------------------------------------------------------------------------
# date parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("06.01.2025 08:54:22", dt.datetime(2025, 1, 6, 8, 54, 22)),
    ("20.02.2023", dt.datetime(2023, 2, 20)),
    ("31.03.20", dt.datetime(2020, 3, 31)),
    ("05.01.26", dt.datetime(2026, 1, 5)),
    (43476.0, dt.datetime(2019, 1, 11)),
    ("12.01.2026 08:52:51", dt.datetime(2026, 1, 12, 8, 52, 51)),
])
def test_parse_datetime(raw, expected):
    assert parse_datetime(raw) == expected


def test_parse_datetime_rejects_non_dates():
    assert parse_datetime("Итого за период") is None
    assert parse_datetime(3.5) is None          # too small to be a serial
    assert parse_datetime("") is None


# --------------------------------------------------------------------------
# field decomposition
# --------------------------------------------------------------------------

def test_tagged_counterparty_blob():
    out = parse_counterparty_blob(
        "МФО:00419 Счет:16401000605703819001 ИНН:310841562 SPACEZERO"
    )
    assert out["bank_code"] == "00419"
    assert out["account"] == "16401000605703819001"
    assert out["inn"] == "310841562"


def test_slash_counterparty_blob():
    out = parse_counterparty_blob(
        '20214000205122905002/306712636/АО "HAYOT BIRJA" ВТОР СЧЕТ'
    )
    assert out["account"] == "20214000205122905002"
    assert out["inn"] == "306712636"
    assert out["name"] == 'АО "HAYOT BIRJA" ВТОР СЧЕТ'


def test_multiline_blob_splits_name_and_purpose():
    out = parse_counterparty_blob(
        "МФО:00014 Счет:23402000300100001010 ИНН:201122919\n"
        "Молия вазирлиги\n"
        "08101~2001108604~Ижтимоий солиқ"
    )
    assert out["account"] == "23402000300100001010"
    assert out["name"] == "Молия вазирлиги"
    assert out["purpose"].startswith("08101~")


@pytest.mark.parametrize("raw,name,inn", [
    ("Казначейство Министерство Финансов РУз             201122919",
     "Казначейство Министерство Финансов РУз", "201122919"),
    ("Казначейство Мин. Фин. РУз. 201122919              201122919",
     "Казначейство Мин. Фин. РУз", "201122919"),
    ("JUST FOR OFFICE MCHJ 309629894", "JUST FOR OFFICE MCHJ", "309629894"),
    ('Начисленные комиссионные ООО "SUPPORT TAKSI"',
     'Начисленные комиссионные ООО "SUPPORT TAKSI"', ""),
])
def test_strip_trailing_inn(raw, name, inn):
    assert strip_trailing_inn(raw) == (name, inn)


@pytest.mark.parametrize("account,currency", [
    ("20208000505703819001", "UZS"),
    ("20208840805703819001", "USD"),
    ("20208978105703819001", "EUR"),
    ("23106000705703819001", "UZS"),
])
def test_currency_from_account(account, currency):
    assert currency_from_account(account) == currency


# --------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label,role", [
    ("Оборот Дебет", "debit"),
    ("Обороты по дебету", "debit"),
    ("Сумма дебет", "debit"),
    ("Debet", "debit"),
    ("Оборот Кредит", "credit"),
    ("Kredit", "credit"),
    ("Назначение платежа", "purpose"),
    ("To'lov tafsilotlari", "purpose"),
    ("Наименование корреспондента", "corr_name"),
    ("Счет корреспондента", "corr_account"),
    ("Cчет/ИНН", "corr_blob"),
    ("МФО", "mfo"),
    ("Дата проводки", "date"),
    ("Sana", "date"),
])
def test_match_role(label, role):
    assert match_role(label) == role


def test_latin_homoglyph_headers_still_match():
    # "Cчет" written with a Latin C must map to the same role as the Cyrillic
    # spelling. Folding happens during role matching, not in normalize_label,
    # so that free text like "ASIA ALLIANCE BANK" survives intact.
    assert match_role("Cчет/ИНН") == match_role("Счет/ИНН") == "corr_blob"
    assert "asia alliance" in normalize_label('АТ "ASIA ALLIANCE BANK" БАНКИ')


# --------------------------------------------------------------------------
# corpus
# --------------------------------------------------------------------------

def _sample(name: str) -> Path:
    path = SAMPLES / name
    if not path.exists():
        pytest.skip(f"sample {name} not available")
    return path


@pytest.mark.parametrize("name", SAMPLE_FILES)
def test_sample_parses_expected_accounts(name):
    statement = parse_file(_sample(name), name)
    expected = EXPECTED[name]
    active = statement.active_accounts
    assert len(active) == expected["accounts"], (
        f"{name}: expected {expected['accounts']} active accounts, got "
        f"{[a.account_number for a in active]}"
    )
    assert [len(a.transactions) for a in active] == expected["txns"]


@pytest.mark.parametrize("name", SAMPLE_FILES)
def test_sample_reconciles(name):
    statement = parse_file(_sample(name), name)
    should_pass = EXPECTED[name].get("reconciles", True)
    for account in statement.accounts:
        rec = account.reconciliation
        if should_pass:
            assert rec.passed, f"{name} / {account.account_number}: {rec.messages}"
        # A statement expected to fail must actually have run its checks;
        # a silent "skipped" everywhere would make the test vacuous.
    if not should_pass:
        assert any(not a.reconciliation.passed for a in statement.accounts)


@pytest.mark.parametrize("name", SAMPLE_FILES)
def test_sample_output_matches_reference_schema(name):
    statement = parse_file(_sample(name), name)
    for account in statement.active_accounts:
        payload = account.to_json()
        assert list(payload) == [
            "account_number", "currency", "transaction_count",
            "total_credit", "total_debit", "transactions",
        ]
        assert payload["transaction_count"] == len(payload["transactions"])
        assert json.dumps(payload, ensure_ascii=False)  # must be serialisable
        for tx in payload["transactions"]:
            assert list(tx) == REFERENCE_KEYS
            assert isinstance(tx["credit_amount"], (int, float))
            assert isinstance(tx["debit_amount"], (int, float))
            assert isinstance(tx["document_number"], str)


@pytest.mark.parametrize("name", SAMPLE_FILES)
def test_sample_transaction_sanity(name):
    statement = parse_file(_sample(name), name)
    for account in statement.active_accounts:
        assert account.account_number, f"{name}: account number not detected"
        assert len(account.account_number) == 20
        for tx in account.transactions:
            # No posting is both a debit and a credit, and none is empty.
            assert not (tx.debit_amount and tx.credit_amount)
            assert tx.debit_amount or tx.credit_amount
            assert tx.debit_amount >= 0 and tx.credit_amount >= 0


@pytest.mark.parametrize("name", SAMPLE_FILES)
def test_sample_dates_are_within_period(name):
    statement = parse_file(_sample(name), name)
    for account in statement.active_accounts:
        if not (account.period_from and account.period_to):
            continue
        start = parse_datetime(account.period_from)
        end = parse_datetime(account.period_to) + dt.timedelta(days=1)
        outside = [
            tx.transaction_date for tx in account.transactions
            if tx.transaction_date
            and not (start <= parse_datetime(tx.transaction_date) <= end)
        ]
        # Value-dated postings can legitimately sit outside; flag only a flood.
        assert len(outside) <= max(3, len(account.transactions) // 10), (
            f"{name}: {len(outside)} dates outside {account.period_from}"
            f"-{account.period_to}, e.g. {outside[:3]}"
        )


def test_multi_currency_file_assigns_currency_per_account():
    statement = parse_file(_sample("ipotekabank-v2.xls"), "ipotekabank-v2.xls")
    by_currency = {a.currency for a in statement.active_accounts}
    assert "USD" in by_currency and "UZS" in by_currency


def test_default_name_style_matches_reference_file():
    """The reference file writes counterparty_name as account/INN/name."""
    for name in ("aloqabank.xlsx", "ipotekabank-v1.xlsx", "xalqbank.xls"):
        statement = parse_file(_sample(name), name)
        for account in statement.active_accounts:
            for tx in account.transactions[:20]:
                if tx.counterparty_account:
                    assert tx.counterparty_name.startswith(
                        tx.counterparty_account + "/"
                    ), f"{name}: {tx.counterparty_name!r}"


def test_clean_name_style_still_available():
    statement = parse_file(_sample("aloqabank.xlsx"), "aloqabank.xlsx",
                           name_style="clean")
    tx = statement.active_accounts[0].transactions[0]
    assert "/" not in tx.counterparty_name.split()[0] or not tx.counterparty_account


def test_whole_amounts_serialise_as_ints_like_the_reference():
    statement = parse_file(_sample("aloqabank.xlsx"), "aloqabank.xlsx")
    payload = statement.active_accounts[0].to_json()
    kinds = {type(t["debit_amount"]) for t in payload["transactions"]}
    assert int in kinds, "whole amounts must serialise as 0 / 300, not 0.0"
    for tx in payload["transactions"]:
        for key in ("debit_amount", "credit_amount"):
            value = tx[key]
            assert isinstance(value, (int, float))
            if isinstance(value, float):
                assert value != int(value), "fractional values only for floats"


def test_every_sample_produces_the_same_shape():
    """The whole point: one universal format regardless of bank."""
    shapes = set()
    tx_shapes = set()
    for name in SAMPLE_FILES:
        statement = parse_file(_sample(name), name)
        for account in statement.active_accounts:
            payload = account.to_json()
            shapes.add(tuple(payload))
            for tx in payload["transactions"]:
                tx_shapes.add(tuple(tx))
    assert len(shapes) == 1, shapes
    assert len(tx_shapes) == 1, tx_shapes
    assert list(shapes.pop()) == [
        "account_number", "currency", "transaction_count",
        "total_credit", "total_debit", "transactions",
    ]
    assert list(tx_shapes.pop()) == REFERENCE_KEYS


def test_ipakyuli_partial_export_is_flagged_not_hidden():
    """The file's own footer states more turnover than its rows contain."""
    statement = parse_file(_sample("ipakyuli.xlsx"), "ipakyuli.xlsx")
    account = statement.active_accounts[0]
    rec = account.reconciliation
    assert rec.checks["turnover"] == "fail"
    assert any("turnover mismatch" in m for m in rec.messages)
