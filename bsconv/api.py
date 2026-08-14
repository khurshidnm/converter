"""FastAPI service wrapping the converter.

    uvicorn bsconv.api:app --host 0.0.0.0 --port 8000

Endpoints
    GET  /health          liveness probe
    GET  /formats         what the service accepts and which banks it knows
    POST /convert         upload one statement, get JSON back
    POST /convert/batch   upload several, get a keyed object back

/convert returns, by default, the reference schema for the single active
account. Files holding several accounts return an accounts array, so a client
never has to guess whether it got one account or many: check "accounts".
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from .engine import (
    DEFAULT_NAME_STYLE, NAME_STYLE_CLEAN, NAME_STYLE_COMPOSITE, parse_file,
)
from .loaders import UnsupportedFileError
from .models import Statement
from .vocabulary import BANK_FINGERPRINTS

MAX_BYTES = 32 * 1024 * 1024
ENGINES = ("ai", "rules", "auto")
DEFAULT_ENGINE = os.environ.get("BSCONV_ENGINE", "ai")


def _run_engine(data: bytes, filename: str, *, engine: str, name_style: str,
                currency: str, verify: bool) -> Statement:
    if engine == "auto":
        engine = "ai" if os.environ.get("ANTHROPIC_API_KEY") else "rules"
    if engine == "rules":
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
    description="Converts Uzbek bank statement exports to a normalised JSON schema.",
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
        "engines": list(ENGINES),
        "default_engine": DEFAULT_ENGINE,
        "known_banks": sorted({name for name, _ in BANK_FINGERPRINTS}),
        "note": "Bank recognition is informational. Parsing is driven by "
                "column-label detection, so unlisted banks using the usual "
                "Russian/Uzbek headers convert without code changes.",
    }


@app.post("/convert")
async def convert(
    file: UploadFile = File(..., description="Statement file"),
    extended: bool = Query(False, description="Include name, period, balances, INN"),
    include_empty: bool = Query(False, description="Include zero-activity accounts"),
    name_style: str = Query(DEFAULT_NAME_STYLE,
                            pattern=f"^({NAME_STYLE_CLEAN}|{NAME_STYLE_COMPOSITE})$"),
    currency: str = Query("UZS", min_length=3, max_length=3),
    strict: bool = Query(False, description="422 if reconciliation fails"),
    engine: str = Query(DEFAULT_ENGINE, pattern="^(ai|rules|auto)$",
                        description="ai (default) | rules | auto"),
    verify: bool = Query(True, description="cross-check AI output vs rules"),
) -> JSONResponse:
    data = await _read(file)
    try:
        statement = _run_engine(
            data, file.filename or "upload", engine=engine,
            name_style=name_style, currency=currency.upper(), verify=verify,
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
    body["engine"] = engine
    if not body["accounts"]:
        raise HTTPException(
            status_code=422,
            detail=f"No account sections found in {file.filename}.",
        )
    status = 200
    if strict and body["reconciliation"]["status"] == "fail":
        status = 422
    return JSONResponse(content=body, status_code=status)


@app.post("/convert/batch")
async def convert_batch(
    files: list[UploadFile] = File(...),
    extended: bool = Query(False),
    include_empty: bool = Query(False),
    name_style: str = Query(DEFAULT_NAME_STYLE,
                            pattern=f"^({NAME_STYLE_CLEAN}|{NAME_STYLE_COMPOSITE})$"),
    currency: str = Query("UZS", min_length=3, max_length=3),
    engine: str = Query(DEFAULT_ENGINE, pattern="^(ai|rules|auto)$"),
    verify: bool = Query(True),
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for upload in files:
        key = upload.filename or f"file{len(results) + 1}"
        try:
            data = await _read(upload)
            statement = _run_engine(
                data, key, engine=engine, name_style=name_style,
                currency=currency.upper(), verify=verify,
            )
            results[key] = _payload(
                statement, extended=extended, include_empty=include_empty
            )
        except HTTPException as exc:
            results[key] = {"error": exc.detail}
        except Exception as exc:  # noqa: BLE001
            results[key] = {"error": f"{type(exc).__name__}: {exc}"}
    return {"count": len(results), "results": results}
