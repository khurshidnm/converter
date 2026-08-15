"""Command line interface.

    python -m bsconv statement.xlsx -o out/
    python -m bsconv *.xls *.xlsx -o out/ --report
    python -m bsconv statement.xls --stdout
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .engine import (
    DEFAULT_NAME_STYLE, NAME_STYLE_CLEAN, NAME_STYLE_COMPOSITE, parse_file,
)
from .loaders import UnsupportedFileError
from .models import Statement

ENGINE_AI = "ai"
ENGINE_RULES = "rules"
ENGINE_AUTO = "auto"

TICK = {"pass": "ok", "fail": "FAIL", "skipped": "--"}


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name).strip("_")


def _stem_for(statement: Statement) -> str:
    # Keep the extension in the stem: a folder routinely holds both
    # aloqabank.xls and aloqabank.xlsx, and one must not overwrite the other.
    source = Path(statement.source_file)
    stem = _safe(source.stem + ("_" + source.suffix.lstrip(".") if source.suffix else ""))
    return stem or "statement"


def statement_payload(
    statement: Statement, *, extended: bool = False, include_empty: bool = False
):
    """The universal payload for one statement file.

    One account -> exactly the reference object (bank_statement.json).
    Several accounts -> a list of those same objects, because one file cannot
    describe two accounts without either losing data or inventing a wrapper.
    """
    accounts = statement.accounts if include_empty else statement.active_accounts
    payload = [a.to_json(extended=extended) for a in accounts]
    if len(payload) == 1:
        return payload[0]
    return payload


def _write_statement_file(
    statement: Statement, out_dir: Path, *, extended: bool, include_empty: bool,
    split: bool,
) -> list[Path]:
    stem = _stem_for(statement)
    accounts = statement.accounts if include_empty else statement.active_accounts

    if split:
        written: list[Path] = []
        multi = len(accounts) > 1
        for i, account in enumerate(accounts, 1):
            parts = [stem]
            if multi:
                parts += [account.account_number or f"account{i}", account.currency]
            path = out_dir / (_safe("_".join(parts)) + ".json")
            path.write_text(
                json.dumps(account.to_json(extended=extended),
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            written.append(path)
        return written

    path = out_dir / (stem + ".json")
    path.write_text(
        json.dumps(
            statement_payload(statement, extended=extended,
                              include_empty=include_empty),
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return [path]


def _print_summary(statement: Statement) -> bool:
    ok = True
    name = Path(statement.source_file).name
    print(f"{name}  [bank={statement.bank}, layout={statement.layout or 'n/a'}, "
          f"{len(statement.active_accounts)}/{len(statement.accounts)} accounts active]")
    for warning in statement.warnings:
        print(f"  ! {warning}")
    for account in statement.accounts:
        rec = account.reconciliation
        if not account.transactions and rec.stated_count in (None, 0):
            continue
        flags = " ".join(f"{k}={TICK[v]}" for k, v in sorted(rec.checks.items()))
        print(f"  {account.account_number or '(unknown account)':22} "
              f"{account.currency}  {len(account.transactions):>5} txns  "
              f"debit {float(account.total_debit):>20,.2f}  "
              f"credit {float(account.total_credit):>20,.2f}   {flags}")
        if not rec.passed:
            ok = False
            for message in rec.messages:
                print(f"      ! {message}")
        for warning in account.warnings:
            print(f"      ~ {warning}")
    return ok


def _choose_auto_engine(path: Path) -> str:
    """Keep simple files offline and escalate harder ones to AI."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return ENGINE_RULES

    name = path.name.lower()
    if any(token in name for token in ("ipotek", "mkb", "xalq", "tenge", "hamkor", "aloqa")):
        return ENGINE_RULES
    if any(token in name for token in ("dbo", "block", "multi", "complex", "report")):
        return ENGINE_AI

    size_kb = max(path.stat().st_size // 1024, 1)
    if size_kb < 256:
        return ENGINE_RULES
    return ENGINE_AI


def _convert(path: Path, args) -> Statement:
    """Run the chosen engine. Auto decides between offline and AI by file complexity."""
    engine = args.engine
    if engine == ENGINE_AUTO:
        engine = _choose_auto_engine(path)

    if engine == ENGINE_RULES:
        return parse_file(path, path.name, name_style=args.name_style,
                          default_currency=args.currency)

    from .ai import AIConfig, convert_with_claude

    config = AIConfig(
        name_style=args.name_style,
        default_currency=args.currency,
        verify=not args.no_verify,
    )
    if args.model:
        config.model = args.model
    if args.batch_rows:
        config.batch_rows = args.batch_rows
    return convert_with_claude(path, path.name, config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bsconv",
        description="Convert Uzbek bank statements (xlsx/xls/html/csv) to JSON.",
    )
    parser.add_argument("files", nargs="+", help="statement files to convert")
    parser.add_argument("-o", "--out", default="out",
                        help="output directory (default: ./out)")
    parser.add_argument("--stdout", action="store_true",
                        help="print JSON to stdout instead of writing files")
    parser.add_argument("--extended", action="store_true",
                        help="include account name, period, balances and INN")
    parser.add_argument("--include-empty", action="store_true",
                        help="also emit accounts with zero transactions")
    parser.add_argument("--split", action="store_true",
                        help="write one file per account instead of one per "
                             "statement (each file is then always a single "
                             "reference object)")
    parser.add_argument("--name-style", choices=[NAME_STYLE_CLEAN, NAME_STYLE_COMPOSITE],
                        default=DEFAULT_NAME_STYLE,
                        help="counterparty_name as a clean name (default) or "
                             "account/INN/name")
    parser.add_argument("--currency", default="UZS",
                        help="fallback currency when the file does not state one")
    parser.add_argument("--engine", choices=[ENGINE_AI, ENGINE_RULES, ENGINE_AUTO],
                        default=ENGINE_AI,
                        help="ai (default): Claude reads the sheet; rules: "
                             "deterministic parser, no API key needed; auto: "
                             "ai when ANTHROPIC_API_KEY is set, else rules")
    parser.add_argument("--model", default=None,
                        help="model for the AI engine (default: claude-sonnet-5)")
    parser.add_argument("--batch-rows", type=int, default=None,
                        help="spreadsheet rows per AI request (default: 60)")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip cross-checking the AI output against the "
                             "rule engine")
    parser.add_argument("--report", action="store_true",
                        help="write a <file>.report.json next to the output")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero if any reconciliation check fails")
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    if not args.stdout:
        out_dir.mkdir(parents=True, exist_ok=True)

    all_ok = True
    failures = 0
    for raw_path in args.files:
        path = Path(raw_path)
        if not path.exists():
            print(f"{path}: not found", file=sys.stderr)
            failures += 1
            continue
        try:
            statement = _convert(path, args)
        except UnsupportedFileError as exc:
            print(f"{path.name}: {exc}", file=sys.stderr)
            failures += 1
            all_ok = False
            continue
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"{path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failures += 1
            all_ok = False
            continue

        if args.stdout:
            print(json.dumps(
                statement_payload(statement, extended=args.extended,
                                  include_empty=args.include_empty),
                ensure_ascii=False, indent=2,
            ))
        else:
            written = _write_statement_file(
                statement, out_dir,
                extended=args.extended, include_empty=args.include_empty,
                split=args.split,
            )
            if args.report:
                report_path = out_dir / (_safe(path.stem + "_" + path.suffix.lstrip(".")) + ".report.json")
                report_path.write_text(
                    json.dumps(statement.report(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            if not args.quiet:
                for w in written:
                    print(f"  -> {w}")

        if not args.quiet:
            all_ok &= _print_summary(statement)
            print()

    if failures:
        return 2
    if args.strict and not all_ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
