"""Check a parsed account against the control figures printed on the statement.

Three independent checks, each skipped when the source does not state the
figure it needs:

  turnover  computed debit/credit sums == "Сумма оборотов" / "Итого документов"
  count     number of parsed transactions == stated document count
  balance   opening + credit - debit == closing

A failure never blocks conversion; it is reported so a human can look.
"""

from __future__ import annotations

from decimal import Decimal

from .models import Account, Reconciliation

# Statements round to 2dp; allow a hair of slack for float round-tripping.
TOLERANCE = Decimal("0.01")


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= TOLERANCE


def reconcile(account: Account, totals: dict[str, object]) -> Reconciliation:
    rec = Reconciliation(
        stated_debit=totals.get("stated_debit"),        # type: ignore[arg-type]
        stated_credit=totals.get("stated_credit"),      # type: ignore[arg-type]
        stated_count=totals.get("stated_count"),        # type: ignore[arg-type]
        opening_balance=totals.get("opening_balance"),  # type: ignore[arg-type]
        closing_balance=totals.get("closing_balance"),  # type: ignore[arg-type]
        computed_debit=account.total_debit,
        computed_credit=account.total_credit,
        computed_count=len(account.transactions),
    )

    # --- turnover ---------------------------------------------------------
    if rec.stated_debit is None and rec.stated_credit is None:
        rec.checks["turnover"] = "skipped"
        rec.messages.append("Statement does not print period turnover totals.")
    else:
        ok = True
        if rec.stated_debit is not None and not _close(
            rec.computed_debit, rec.stated_debit
        ):
            ok = False
            rec.messages.append(
                f"Debit turnover mismatch: parsed {rec.computed_debit} vs "
                f"stated {rec.stated_debit} "
                f"(difference {rec.computed_debit - rec.stated_debit})."
            )
        if rec.stated_credit is not None and not _close(
            rec.computed_credit, rec.stated_credit
        ):
            ok = False
            rec.messages.append(
                f"Credit turnover mismatch: parsed {rec.computed_credit} vs "
                f"stated {rec.stated_credit} "
                f"(difference {rec.computed_credit - rec.stated_credit})."
            )
        rec.checks["turnover"] = "pass" if ok else "fail"

    # --- document count ---------------------------------------------------
    if rec.stated_count is None:
        rec.checks["count"] = "skipped"
    else:
        split = totals.get("stated_count_split")
        computed_debit_rows = sum(1 for t in account.transactions if t.debit_amount)
        computed_credit_rows = sum(1 for t in account.transactions if t.credit_amount)
        if isinstance(split, tuple):
            ok = (computed_debit_rows, computed_credit_rows) == split
            if not ok:
                rec.messages.append(
                    f"Document count mismatch: parsed "
                    f"{computed_debit_rows} debit / {computed_credit_rows} credit "
                    f"vs stated {split[0]} / {split[1]}."
                )
        else:
            ok = rec.computed_count == rec.stated_count
            if not ok:
                rec.messages.append(
                    f"Document count mismatch: parsed {rec.computed_count} "
                    f"vs stated {rec.stated_count}."
                )
        rec.checks["count"] = "pass" if ok else "fail"

    # --- balance roll-forward --------------------------------------------
    if rec.opening_balance is None or rec.closing_balance is None:
        rec.checks["balance"] = "skipped"
    else:
        expected = rec.opening_balance + rec.computed_credit - rec.computed_debit
        ok = _close(expected, rec.closing_balance)
        if not ok:
            rec.messages.append(
                f"Balance roll-forward mismatch: opening {rec.opening_balance} "
                f"+ credit {rec.computed_credit} - debit {rec.computed_debit} "
                f"= {expected}, but statement closes at {rec.closing_balance} "
                f"(difference {expected - rec.closing_balance})."
            )
        rec.checks["balance"] = "pass" if ok else "fail"

    if not rec.performed:
        rec.messages.append(
            "No control figures found; conversion could not be verified "
            "against the statement."
        )
    return rec
