"""FastAPI service wrapping the converter.

    uvicorn bsconv.api:app --host 0.0.0.0 --port 8000

Endpoints
    GET  /health          liveness probe
    GET  /metrics         daily/all-time call counts, for the Telegram bot
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

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from starlette.concurrency import run_in_threadpool

from .engine import (
    DEFAULT_NAME_STYLE, NAME_STYLE_CLEAN, NAME_STYLE_COMPOSITE, parse_file,
)
from .loaders import UnsupportedFileError
from .models import Statement
from .vocabulary import BANK_FINGERPRINTS

MAX_BYTES = 32 * 1024 * 1024
API_KEY_ENV = "BSCONV_API_KEY"
REQUIRE_API_KEY_ENV = "BSCONV_REQUIRE_API_KEY"
CORS_ORIGINS_ENV = "BSCONV_CORS_ORIGINS"
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
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

METRICS_FILE_ENV = "BSCONV_METRICS_FILE"
_METRICS_LOCK = threading.Lock()
# An allowlist, not a blocklist: this is a public endpoint, so an allowlist
# keeps internet scanner noise on random/unknown paths (404s, no API key)
# out of the counts the Telegram bot reports as API health.
_METRICS_TRACKED_PATHS = {"/formats", "/convert", "/convert/transactions", "/convert/batch"}
_METRICS_RETENTION_DAYS = 90
_EMPTY_DAY = {"total": 0, "success": 0, "error": 0}


def _metrics_path() -> Path:
    return Path(os.environ.get(METRICS_FILE_ENV, "bsconv_metrics.json"))


def _load_metrics() -> dict[str, Any]:
    path = _metrics_path()
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"days": {}}


def _save_metrics(data: dict[str, Any]) -> None:
    path = _metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def _record_call(success: bool) -> None:
    """Tally one convert/-family call into today's bucket in the metrics file.

    Runs in a worker thread (see _track_metrics below) since it does blocking
    file I/O; the lock only needs to guard against concurrent worker threads,
    not the event loop itself.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _METRICS_LOCK:
        data = _load_metrics()
        days = data.setdefault("days", {})
        day = days.setdefault(today, dict(_EMPTY_DAY))
        day["total"] += 1
        day["success" if success else "error"] += 1
        if len(days) > _METRICS_RETENTION_DAYS:
            for key in sorted(days)[:-_METRICS_RETENTION_DAYS]:
                del days[key]
        _save_metrics(data)


def _auth_required() -> bool:
    return os.environ.get(REQUIRE_API_KEY_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def require_api_key(api_key: str | None = Depends(api_key_header)) -> None:
    """Require the configured API key for every application endpoint.

    Enforcement is temporarily opt-in via BSCONV_REQUIRE_API_KEY: the check
    below is left intact and unchanged so auth can be turned back on for
    everyone by setting that variable, with no code change needed.
    """
    if not _auth_required():
        return
    expected = os.environ.get(API_KEY_ENV, "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="API authentication is not configured on the server.",
        )
    if not api_key or not secrets.compare_digest(api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


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
    allow_origins=[
        origin.strip()
        for origin in os.environ.get(CORS_ORIGINS_ENV, "").split(",")
        if origin.strip()
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _track_metrics(request: Request, call_next):
    """Count calls to the tracked application endpoints as success or error.

    A 401 (missing/invalid X-API-Key) is not counted either way: it never
    reached real request handling, so it's a rejected knock at the door, not
    a use of the service - counting it would let unauthenticated scanner
    traffic on real paths (e.g. POST /convert with no key) inflate the error
    rate the Telegram bot reports.
    """
    if request.url.path not in _METRICS_TRACKED_PATHS:
        return await call_next(request)
    try:
        response = await call_next(request)
    except Exception:
        await run_in_threadpool(_record_call, False)
        raise
    if response.status_code != 401:
        await run_in_threadpool(_record_call, 200 <= response.status_code < 400)
    return response


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
def health(_: None = Depends(require_api_key)) -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
def metrics(_: None = Depends(require_api_key)) -> dict[str, Any]:
    """Daily and all-time call counts, consumed by the Telegram bot's reports."""
    data = _load_metrics()
    days = data.get("days", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals = dict(_EMPTY_DAY)
    for day in days.values():
        totals["total"] += day["total"]
        totals["success"] += day["success"]
        totals["error"] += day["error"]
    return {
        "today": days.get(today, dict(_EMPTY_DAY)),
        "totals": totals,
        "days": days,
    }


@app.get("/formats")
def formats(_: None = Depends(require_api_key)) -> dict[str, Any]:
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
    _: None = Depends(require_api_key),
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
    _: None = Depends(require_api_key),
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
    _: None = Depends(require_api_key),
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
