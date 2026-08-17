"""FastAPI service wrapping the converter.

    uvicorn bsconv.api:app --host 0.0.0.0 --port 8000

Endpoints
    GET  /health          liveness probe
    GET  /formats         what the service accepts and which banks it knows
    POST /convert         upload one statement, get JSON back
    POST /convert/transactions
                         upload one statement, get a flat offline transaction list
    POST /convert/batch   upload several, get a keyed object back

/convert returns, by default, the reference schema for the single active
account. Files holding several accounts return an accounts array, so a client
never has to guess whether it got one account or many: check "accounts".
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .engine import (
    DEFAULT_NAME_STYLE, NAME_STYLE_CLEAN, NAME_STYLE_COMPOSITE, parse_file,
)
from .loaders import UnsupportedFileError
from .models import Statement
from .vocabulary import BANK_FINGERPRINTS

MAX_BYTES = 32 * 1024 * 1024
MODES = ("ai", "offline", "auto")
LEGACY_ENGINE_ALIASES = {"rules": "offline", "auto": "auto"}
MODE_ALIASES = {
    "ai": "ai",
    "model-ai": "ai",
    "model_ai": "ai",
    "offline": "offline",
    "model-offline": "offline",
    "model_offline": "offline",
    "auto": "auto",
    "model-auto": "auto",
    "model_auto": "auto",
    "rules": "offline",
}


def _choose_auto_mode(data: bytes, filename: str) -> str:
    """Simple, standard statements stay offline; harder ones escalate to AI."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "offline"

    suffix = (filename or "").lower()
    if suffix.endswith((".csv", ".tsv", ".html", ".htm")):
        return "offline"

    # The parser is the fast deterministic path for regular row-layout exports.
    # If the sheet seems unusual or multi-account/complex, let AI take over.
    lower_name = filename.lower()
    if any(token in lower_name for token in ("ipotek", "mkb", "xalq", "tenge", "hamkor", "aloqa")):
        return "offline"

    # Hard cases: block layouts, several account sections, or any file name that
    # suggests a non-standard export should go to AI.
    if any(token in lower_name for token in ("dbo", "block", "multi", "complex", "report")):
        return "ai"

    # A heuristic based on the payload itself: small files with clear tables stay offline;
    # very large/complex spreadsheets go AI to avoid brittle parser assumptions.
    size_kb = max(len(data) // 1024, 1)
    if size_kb < 256:
        return "offline"
    return "ai"


def _normalize_mode(
    mode: str | None,
    *,
    legacy_engine: str | None = None,
    model: str | None = None,
) -> str:
    raw = model or mode or legacy_engine
    if raw is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Mode is required. Use 'mode=ai', 'model=ai', 'mode=offline', "
                "'model=offline', 'mode=auto', or 'model=auto'."
            ),
        )

    value = str(raw).strip().lower()
    mapped = MODE_ALIASES.get(value)
    if mapped is not None:
        return mapped
    raise HTTPException(
        status_code=422,
        detail=(
            f"Unsupported mode '{raw}'. Use 'ai', 'offline', 'auto', "
            "'model=ai', 'model=offline', or 'model=auto'."
        ),
    )


def _run_engine(data: bytes, filename: str, *, mode: str, name_style: str,
                currency: str, verify: bool) -> Statement:
    resolved_mode = _normalize_mode(mode)
    if resolved_mode == "auto":
        resolved_mode = _choose_auto_mode(data, filename)
    if resolved_mode == "offline":
        return parse_file(data, filename, name_style=name_style,
                          default_currency=currency)

    from .ai import AIConfig, AIError, convert_with_claude

    try:
        return convert_with_claude(
            data, filename,
            AIConfig(name_style=name_style, default_currency=currency,
                     verify=verify),
        )
    except AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

app = FastAPI(
    title="Bank Statement Converter",
    version="1.0.0",
    description=(
        "Converts Uzbek bank statement exports to a normalised JSON schema. "
        "Use '?mode=ai', '?model=ai', '?mode=offline', '?model=offline', "
        "'?mode=auto', or '?model=auto' on the /convert endpoint. "
        "Use /convert/transactions for the flat offline transaction schema. "
        "The values 'model-ai', 'model-offline', and 'model-auto' are also accepted."
    ),
    servers=[
        {"url": "http://localhost:8000", "description": "Local development"},
        {"url": "https://converter.khurshid.uz", "description": "Production"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _payload(
    statement: Statement, *, extended: bool, include_empty: bool
) -> dict[str, Any]:
    accounts = statement.accounts if include_empty else statement.active_accounts
    body: dict[str, Any] = {
        "source_file": statement.source_file,
        "bank": statement.bank,
        "layout": statement.layout,
        "account_count": len(accounts),
        "warnings": statement.warnings,
        "reconciliation": {
            "status": "pass" if all(
                a.reconciliation.passed for a in accounts
            ) else "fail",
            "accounts": [
                {
                    "account_number": a.account_number,
                    **a.reconciliation.to_json(),
                }
                for a in accounts
            ],
        },
    }
    if len(accounts) == 1:
        body["account"] = accounts[0].to_json(extended=extended)
        body["accounts"] = [body["account"]]
    else:
        body["accounts"] = [a.to_json(extended=extended) for a in accounts]
    return body


def _transactions_payload(statement: Statement) -> dict[str, Any]:
    """Build the flat, offline response used by /convert/transactions."""
    accounts = statement.active_accounts
    warnings = list(statement.warnings)
    transactions: list[dict[str, Any]] = []
    for account in accounts:
        for transaction in account.transactions:
            transactions.append({
                "client_account": account.account_number,
                **transaction.to_json(),
            })
        for warning in account.warnings:
            if warning not in warnings:
                warnings.append(warning)

    return {
        "source_file": statement.source_file,
        "bank": statement.bank,
        "layout": statement.layout,
        "client_account_count": len(accounts),
        "warnings": warnings,
        "transactions": transactions,
    }


async def _read(upload: UploadFile) -> bytes:
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=400, detail=f"{upload.filename}: empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"{upload.filename}: exceeds {MAX_BYTES // (1024 * 1024)} MB limit",
        )
    return data


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/formats")
def formats() -> dict[str, Any]:
    return {
        "input_formats": [".xlsx", ".xlsm", ".xls", ".xls (HTML)", ".htm",
                          ".html", ".csv", ".tsv"],
        "modes": list(MODES),
        "default_mode": None,
        "known_banks": sorted({name for name, _ in BANK_FINGERPRINTS}),
        "note": "Call the API with '?mode=ai', '?model=ai', '?mode=offline', '?model=offline', "
                "'?mode=auto', or '?model=auto'. The values 'model-ai', 'model-offline', and "
                "'model-auto' are also accepted. Auto uses the local parser for simple files and escalates "
                "to Claude only for harder inputs. The offline path uses the local parser without an API key; "
            "the AI path uses Claude and requires ANTHROPIC_API_KEY. "
            "POST /convert/transactions always uses the local parser and returns a flat transaction list.",
    }


@app.post("/convert")
async def convert(
    file: UploadFile = File(..., description="Statement file"),
    extended: bool = Query(False, description="Include name, period, balances, INN"),
    include_empty: bool = Query(False, description="Include zero-activity accounts"),
    strict: bool = Query(False, description="422 if reconciliation fails"),
    mode: str | None = Query(
        None,
        pattern="^(ai|offline|rules|auto|model-ai|model_ai|model-offline|model_offline|model-auto|model_auto)$",
        description="Required. 'ai' uses Claude. 'offline' uses the local parser. You may also send 'model-ai', 'model-offline', or 'model-auto'.",
    ),
    model: str | None = Query(
        None,
        description="Alias for mode. Accepts 'ai', 'offline', 'auto', 'model-ai', 'model-offline', and 'model-auto'.",
    ),
    engine: str | None = Query(
        None,
        pattern="^(ai|offline|rules|auto)$",
        description="Deprecated alias kept for backwards compatibility.",
    ),
    verify: bool = Query(True, description="cross-check AI output vs rules"),
) -> JSONResponse:
    data = await _read(file)
    resolved_mode = _normalize_mode(mode, legacy_engine=engine, model=model)
    if resolved_mode == "auto":
        resolved_mode = _choose_auto_mode(data, file.filename or "upload")
    try:
        # _run_engine makes blocking network calls in AI mode; running it
        # straight inside this coroutine would freeze the event loop - and
        # therefore every other in-flight request, including /health - for
        # the whole conversion. Run it in a worker thread instead.
        statement = await run_in_threadpool(
            _run_engine, data, file.filename or "upload", mode=resolved_mode,
            name_style=DEFAULT_NAME_STYLE, currency="UZS", verify=verify,
        )
    except HTTPException:
        raise
    except UnsupportedFileError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=422,
            detail=f"Could not parse {file.filename}: {type(exc).__name__}: {exc}",
        ) from exc

    body = _payload(statement, extended=extended, include_empty=include_empty)
    body["mode"] = resolved_mode
    if not body["accounts"]:
        raise HTTPException(
            status_code=422,
            detail=f"No account sections found in {file.filename}.",
        )
    status = 200
    if strict and body["reconciliation"]["status"] == "fail":
        status = 422
    return JSONResponse(content=body, status_code=status)


@app.post("/convert/transactions")
async def convert_transactions(
    file: UploadFile = File(..., description="Statement file"),
) -> JSONResponse:
    """Convert a statement to the flat, fully offline transaction schema."""
    data = await _read(file)
    filename = file.filename or "upload"
    try:
        statement = await run_in_threadpool(
            parse_file, data, filename,
            name_style=DEFAULT_NAME_STYLE, default_currency="UZS",
        )
    except UnsupportedFileError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=422,
            detail=f"Could not parse {filename}: {type(exc).__name__}: {exc}",
        ) from exc

    body = _transactions_payload(statement)
    if not body["transactions"]:
        raise HTTPException(
            status_code=422,
            detail=f"No transactions found in {filename}.",
        )
    return JSONResponse(content=body)


@app.post("/convert/batch")
async def convert_batch(
    files: list[UploadFile] = File(...),
    extended: bool = Query(False),
    include_empty: bool = Query(False),
    mode: str | None = Query(
        None,
        pattern="^(ai|offline|rules|auto|model-ai|model_ai|model-offline|model_offline|model-auto|model_auto)$",
        description="Required. 'ai' uses Claude, 'offline' uses the local parser, and 'auto' chooses offline for simple files and AI for harder inputs. 'model-ai', 'model-offline', and 'model-auto' are accepted aliases.",
    ),
    model: str | None = Query(
        None,
        description="Alias for mode. Accepts 'ai', 'offline', 'auto', 'model-ai', 'model-offline', and 'model-auto'.",
    ),
    engine: str | None = Query(
        None,
        pattern="^(ai|offline|rules|auto)$",
        description="Deprecated alias kept for backwards compatibility.",
    ),
    verify: bool = Query(True),
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for upload in files:
        key = upload.filename or f"file{len(results) + 1}"
        try:
            data = await _read(upload)
            resolved_mode = _normalize_mode(mode, legacy_engine=engine, model=model)
            if resolved_mode == "auto":
                resolved_mode = _choose_auto_mode(data, key)
            statement = await run_in_threadpool(
                _run_engine, data, key, mode=resolved_mode,
                name_style=DEFAULT_NAME_STYLE, currency="UZS", verify=verify,
            )
            payload = _payload(
                statement, extended=extended, include_empty=include_empty
            )
            payload["mode"] = resolved_mode
            results[key] = payload
        except HTTPException as exc:
            results[key] = {"error": exc.detail}
        except Exception as exc:  # noqa: BLE001
            results[key] = {"error": f"{type(exc).__name__}: {exc}"}
    return {"count": len(results), "results": results}
