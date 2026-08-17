"""API smoke tests. Skipped when FastAPI is not installed."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

# Tests use a non-production key; deployments must provide BSCONV_API_KEY.
TEST_API_KEY = "test-api-key"
os.environ.setdefault("BSCONV_API_KEY", TEST_API_KEY)
# Keep the module-level client's metrics out of the repo working directory.
os.environ.setdefault(
    "BSCONV_METRICS_FILE",
    str(Path(tempfile.gettempdir()) / "bsconv_test_metrics.json"),
)
from bsconv.api import _choose_auto_mode, app  # noqa: E402

SAMPLES = Path(os.environ.get("BSCONV_SAMPLES", Path(__file__).parent.parent / "samples"))
client = TestClient(app, headers={"X-API-Key": TEST_API_KEY})


def _upload(name: str, endpoint: str = "/convert", **params):
    """Upload a sample using an explicit mode. Offline mode is the default
    path for parsing tests; AI mode requires an API key and network."""
    path = SAMPLES / name
    if not path.exists():
        pytest.skip(f"sample {name} not available")
    params.setdefault("mode", "offline")
    with open(path, "rb") as fh:
        return client.post(endpoint, params=params,
                           files={"file": (name, fh.read())})


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_requests_require_api_key(monkeypatch):
    monkeypatch.setenv("BSCONV_API_KEY", TEST_API_KEY)
    unauthenticated_client = TestClient(app)
    assert unauthenticated_client.get("/health").status_code == 401
    assert unauthenticated_client.get(
        "/health", headers={"X-API-Key": "wrong"}
    ).status_code == 401


def test_requests_fail_closed_when_api_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("BSCONV_API_KEY", raising=False)
    assert client.get("/health").status_code == 503


def test_openapi_declares_api_key_security():
    schema = client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["APIKeyHeader"]["name"] == "X-API-Key"
    assert schema["paths"]["/convert/transactions"]["post"]["security"] == [
        {"APIKeyHeader": []}
    ]


def test_formats_lists_inputs():
    body = client.get("/formats").json()
    assert ".xlsx" in body["input_formats"]
    assert body["known_banks"]


def test_convert_single_account():
    response = _upload("aloqabank.xlsx")
    assert response.status_code == 200
    body = response.json()
    assert body["account"]["transaction_count"] == 127
    assert body["reconciliation"]["status"] == "pass"
    assert len(body["accounts"]) == 1


def test_convert_transactions_returns_flat_offline_schema():
    response = _upload("aloqabank.xlsx", endpoint="/convert/transactions")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "source_file", "bank", "layout", "client_account_count",
        "warnings", "transactions",
    }
    assert body["client_account_count"] == 1
    assert len(body["transactions"]) == 127
    assert set(body["transactions"][0]) == {
        "client_account", "transaction_date", "document_number",
        "credit_amount", "debit_amount", "counterparty_name",
        "counterparty_account", "bank_code", "payment_purpose",
    }
    assert body["transactions"][0]["client_account"]


def test_convert_multi_account():
    response = _upload("ipotekabank-v1.xlsx")
    assert response.status_code == 200
    body = response.json()
    assert body["account_count"] == 3
    counts = [a["transaction_count"] for a in body["accounts"]]
    assert counts == [149, 24, 199]
    assert {a["currency"] for a in body["accounts"]} == {"UZS", "USD"}


def test_convert_html_disguised_as_xls():
    response = _upload("xalqbank.xls")
    assert response.status_code == 200
    assert response.json()["account"]["transaction_count"] == 70


def test_strict_flags_the_partial_export():
    response = _upload("ipakyuli.xlsx", strict=True)
    assert response.status_code == 422
    assert response.json()["reconciliation"]["status"] == "fail"


def test_formats_advertises_the_modes():
    body = client.get("/formats").json()
    assert set(body["modes"]) == {"ai", "offline", "auto"}
    assert body["default_mode"] is None


def test_mode_is_required():
    path = SAMPLES / "aloqabank.xlsx"
    if not path.exists():
        pytest.skip("sample not available")
    with open(path, "rb") as fh:
        response = client.post("/convert", files={"file": ("aloqabank.xlsx", fh.read())})
    assert response.status_code == 422
    assert "mode" in response.json()["detail"].lower()


def test_model_alias_is_accepted():
    path = SAMPLES / "aloqabank.xlsx"
    if not path.exists():
        pytest.skip("sample not available")
    with open(path, "rb") as fh:
        response = client.post(
            "/convert",
            params={"model": "offline"},
            files={"file": (path.name, fh.read())},
        )
    assert response.status_code == 200
    assert response.json()["mode"] == "offline"


def test_openapi_does_not_expose_name_style_and_currency():
    schema = client.get("/openapi.json").json()
    convert_params = schema["paths"]["/convert"]["post"]["parameters"]
    names = {p["name"] for p in convert_params}
    assert "name_style" not in names
    assert "currency" not in names


def test_auto_mode_prefers_offline_for_simple_files(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    path = SAMPLES / "aloqabank.xlsx"
    if not path.exists():
        pytest.skip("sample not available")
    with open(path, "rb") as fh:
        assert _choose_auto_mode(fh.read(), path.name) == "offline"


def test_ai_mode_without_a_key_returns_a_clear_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = SAMPLES / "tangebank.xlsx"
    if not path.exists():
        pytest.skip("sample not available")
    with open(path, "rb") as fh:
        response = client.post("/convert", params={"mode": "ai"},
                               files={"file": ("tangebank.xlsx", fh.read())})
    assert response.status_code == 502
    assert "API key" in response.json()["detail"]


def test_offline_mode_runs_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = SAMPLES / "tangebank.xlsx"
    if not path.exists():
        pytest.skip("sample not available")
    with open(path, "rb") as fh:
        response = client.post("/convert", params={"mode": "offline"},
                               files={"file": ("tangebank.xlsx", fh.read())})
    assert response.status_code == 200
    assert response.json()["account"]["transaction_count"] == 24


def test_rejects_garbage():
    response = client.post("/convert", params={"mode": "offline"},
                           files={"file": ("x.bin", b"\x00\x01not a file")})
    assert response.status_code in (415, 422)


def test_rejects_empty_file():
    response = client.post("/convert", params={"mode": "offline"},
                           files={"file": ("empty.xlsx", b"")})
    assert response.status_code == 400


def _metrics_client(monkeypatch, tmp_path):
    """A client whose metrics are isolated to a scratch file for this test."""
    monkeypatch.setenv("BSCONV_METRICS_FILE", str(tmp_path / "metrics.json"))
    return TestClient(app, headers={"X-API-Key": TEST_API_KEY})


def test_health_and_metrics_calls_are_not_counted(monkeypatch, tmp_path):
    isolated = _metrics_client(monkeypatch, tmp_path)
    isolated.get("/health")
    isolated.get("/health")
    body = isolated.get("/metrics").json()
    assert body["today"] == {"total": 0, "success": 0, "error": 0}


def test_metrics_counts_successful_and_failed_calls(monkeypatch, tmp_path):
    isolated = _metrics_client(monkeypatch, tmp_path)
    isolated.get("/formats")  # success
    isolated.post("/convert", params={"mode": "offline"},
                  files={"file": ("empty.xlsx", b"")})  # 400, counted as error
    body = isolated.get("/metrics").json()
    assert body["today"] == {"total": 2, "success": 1, "error": 1}
    assert body["totals"] == body["today"]


def test_metrics_ignores_unmatched_and_unauthenticated_scanner_traffic(monkeypatch, tmp_path):
    isolated = _metrics_client(monkeypatch, tmp_path)
    isolated.get("/wp-admin")  # 404, not a tracked application endpoint
    unauthenticated = TestClient(app)
    unauthenticated.get("/formats")  # 401, no key: rejected before real handling
    body = isolated.get("/metrics").json()
    assert body["today"] == {"total": 0, "success": 0, "error": 0}


def test_metrics_requires_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("BSCONV_METRICS_FILE", str(tmp_path / "metrics.json"))
    unauthenticated_client = TestClient(app)
    assert unauthenticated_client.get("/metrics").status_code == 401
