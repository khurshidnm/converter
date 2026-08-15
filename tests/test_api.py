"""API smoke tests. Skipped when FastAPI is not installed."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from bsconv.api import _choose_auto_mode, app  # noqa: E402

SAMPLES = Path(os.environ.get("BSCONV_SAMPLES", Path(__file__).parent.parent / "samples"))
client = TestClient(app)


def _upload(name: str, **params):
    """Upload a sample using an explicit mode. Offline mode is the default
    path for parsing tests; AI mode requires an API key and network."""
    path = SAMPLES / name
    if not path.exists():
        pytest.skip(f"sample {name} not available")
    params.setdefault("mode", "offline")
    with open(path, "rb") as fh:
        return client.post("/convert", params=params,
                           files={"file": (name, fh.read())})


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


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
