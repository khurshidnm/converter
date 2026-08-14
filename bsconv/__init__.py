"""bsconv - universal Uzbek bank statement to JSON converter.

    from bsconv import parse_file

    statement = parse_file("aloqabank.xlsx")
    for account in statement.active_accounts:
        print(account.account_number, account.to_json())

Handles .xlsx, .xls (BIFF), .xls that is really HTML, .htm and .csv, across
single-table, multi-account and multi-row-block layouts, in Russian, Uzbek
Latin and Uzbek Cyrillic.
"""

from .engine import (
    DEFAULT_NAME_STYLE, NAME_STYLE_CLEAN, NAME_STYLE_COMPOSITE, parse_file,
    parse_grid,
)
from .loaders import Grid, UnsupportedFileError, load
from .models import Account, Reconciliation, Statement, Transaction
from .reconcile import reconcile

__version__ = "1.0.0"

__all__ = [
    "parse_file",
    "parse_grid",
    "load",
    "Grid",
    "Statement",
    "Account",
    "Transaction",
    "Reconciliation",
    "reconcile",
    "UnsupportedFileError",
    "DEFAULT_NAME_STYLE",
    "NAME_STYLE_CLEAN",
    "NAME_STYLE_COMPOSITE",
    "__version__",
]
