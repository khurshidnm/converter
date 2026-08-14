"""Canonical data model for parsed bank statements."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Any


def amount_to_json(value: Decimal) -> float | int:
    """Serialise an amount the way the reference file does.

    bank_statement.json writes whole numbers without a decimal point (0, 300)
    and fractional ones as floats (116999945.4). Emitting 0.0 everywhere would
    be valid JSON but a different style, so integral values become ints.
    """
    if value == value.to_integral_value():
        return int(value)
    return float(value)


@dataclass
class Transaction:
    """One posting on one account, in the target JSON shape."""

    transaction_date: str = ""          # "DD.MM.YYYY HH:MM:SS"
    document_number: str = ""
    credit_amount: Decimal = Decimal("0")
    debit_amount: Decimal = Decimal("0")
    counterparty_name: str = ""
    counterparty_account: str = ""
    bank_code: str = ""                 # MFO / BIC
    payment_purpose: str = ""

    # Retained for diagnostics and optional enrichment; never emitted in
    # "sample" output mode.
    counterparty_inn: str = ""
    operation_code: str = ""
    source_row: int = -1

    def to_json(self, *, extended: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "transaction_date": self.transaction_date,
            "document_number": self.document_number,
            "credit_amount": amount_to_json(self.credit_amount),
            "debit_amount": amount_to_json(self.debit_amount),
            "counterparty_name": self.counterparty_name,
            "counterparty_account": self.counterparty_account,
            "bank_code": self.bank_code,
            "payment_purpose": self.payment_purpose,
        }
        if extended:
            out["counterparty_inn"] = self.counterparty_inn
            out["operation_code"] = self.operation_code
        return out


@dataclass
class Reconciliation:
    """Result of checking a parsed account against the statement's own totals."""

    stated_debit: Decimal | None = None
    stated_credit: Decimal | None = None
    stated_count: int | None = None
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None

    computed_debit: Decimal = Decimal("0")
    computed_credit: Decimal = Decimal("0")
    computed_count: int = 0

    checks: dict[str, str] = field(default_factory=dict)   # name -> pass/fail/skipped
    messages: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(v != "fail" for v in self.checks.values())

    @property
    def performed(self) -> bool:
        return any(v != "skipped" for v in self.checks.values())

    def to_json(self) -> dict[str, Any]:
        def f(x: Decimal | None) -> float | None:
            return None if x is None else float(x)

        return {
            "status": "pass" if self.passed else "fail",
            "checks": self.checks,
            "messages": self.messages,
            "stated": {
                "debit": f(self.stated_debit),
                "credit": f(self.stated_credit),
                "count": self.stated_count,
                "opening_balance": f(self.opening_balance),
                "closing_balance": f(self.closing_balance),
            },
            "computed": {
                "debit": f(self.computed_debit),
                "credit": f(self.computed_credit),
                "count": self.computed_count,
            },
        }


@dataclass
class Account:
    """One account section of a statement file."""

    account_number: str = ""
    currency: str = "UZS"
    account_name: str = ""
    client_inn: str = ""
    period_from: str = ""
    period_to: str = ""
    transactions: list[Transaction] = field(default_factory=list)
    reconciliation: Reconciliation = field(default_factory=Reconciliation)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_debit(self) -> Decimal:
        return sum((t.debit_amount for t in self.transactions), Decimal("0"))

    @property
    def total_credit(self) -> Decimal:
        return sum((t.credit_amount for t in self.transactions), Decimal("0"))

    def to_json(self, *, extended: bool = False) -> dict[str, Any]:
        """The target schema. Key order matches the reference sample exactly."""
        out: dict[str, Any] = {
            "account_number": self.account_number,
            "currency": self.currency,
            "transaction_count": len(self.transactions),
            "total_credit": amount_to_json(self.total_credit),
            "total_debit": amount_to_json(self.total_debit),
            "transactions": [t.to_json(extended=extended) for t in self.transactions],
        }
        if extended:
            out = {
                "account_number": self.account_number,
                "account_name": self.account_name,
                "client_inn": self.client_inn,
                "currency": self.currency,
                "period_from": self.period_from,
                "period_to": self.period_to,
                "opening_balance": (
                    None if self.reconciliation.opening_balance is None
                    else float(self.reconciliation.opening_balance)
                ),
                "closing_balance": (
                    None if self.reconciliation.closing_balance is None
                    else float(self.reconciliation.closing_balance)
                ),
                "transaction_count": len(self.transactions),
                "total_credit": float(self.total_credit),
                "total_debit": float(self.total_debit),
                "transactions": [t.to_json(extended=True) for t in self.transactions],
            }
        return out


@dataclass
class Statement:
    """Everything parsed out of a single uploaded file."""

    source_file: str = ""
    bank: str = "unknown"
    layout: str = ""
    accounts: list[Account] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def active_accounts(self) -> list[Account]:
        return [a for a in self.accounts if a.transactions]

    def report(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "bank": self.bank,
            "layout": self.layout,
            "account_count": len(self.accounts),
            "active_account_count": len(self.active_accounts),
            "warnings": self.warnings,
            "accounts": [
                {
                    "account_number": a.account_number,
                    "currency": a.currency,
                    "account_name": a.account_name,
                    "period": f"{a.period_from} - {a.period_to}".strip(" -"),
                    "transaction_count": len(a.transactions),
                    "total_debit": float(a.total_debit),
                    "total_credit": float(a.total_credit),
                    "reconciliation": a.reconciliation.to_json(),
                    "warnings": a.warnings,
                }
                for a in self.accounts
            ],
        }
