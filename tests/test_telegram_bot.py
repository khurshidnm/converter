"""Tests for the Telegram monitoring bot. Network-free (httpx.MockTransport)."""

from __future__ import annotations

import asyncio

import pytest

httpx = pytest.importorskip("httpx")

from bsconv.telegram_bot import (  # noqa: E402
    BotConfig, _handle_update, _health_tick, check_health, format_daily_report,
    format_status,
)


def _config(**overrides) -> BotConfig:
    env = {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "TELEGRAM_CHAT_ID": "111,222",
        "BSCONV_API_BASE_URL": "http://api.test",
        "BSCONV_API_KEY": "secret",
    }
    env.update(overrides)
    return BotConfig(env)


def test_config_parses_multiple_chat_ids_and_defaults():
    config = _config()
    assert config.chat_ids == ["111", "222"]
    assert config.health_interval == 60
    assert config.daily_report_hour == 9


def test_check_health_ok():
    def handler(request):
        assert request.headers["x-api-key"] == "secret"
        return httpx.Response(200, json={"status": "ok"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await check_health(client, _config())

    is_up, detail = asyncio.run(run())
    assert is_up is True
    assert detail == ""


def test_check_health_failure_status():
    def handler(request):
        return httpx.Response(503)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await check_health(client, _config())

    is_up, detail = asyncio.run(run())
    assert is_up is False
    assert "503" in detail


def test_check_health_connection_error():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await check_health(client, _config())

    is_up, detail = asyncio.run(run())
    assert is_up is False
    assert "ConnectError" in detail


def test_health_tick_alerts_on_down_then_recovery():
    sent = []
    up = {"value": True}

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200 if up["value"] else 503)
        sent.append(request.read().decode())
        return httpx.Response(200, json={"ok": True})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            config = _config()
            state = {"is_up": True}

            await _health_tick(client, config, state)  # still up: no alert
            assert sent == []

            up["value"] = False
            await _health_tick(client, config, state)  # goes down: alert
            assert state["is_up"] is False
            assert any("DOWN" in body for body in sent)

            sent.clear()
            up["value"] = True
            await _health_tick(client, config, state)  # recovers: alert
            assert state["is_up"] is True
            assert any("UP" in body for body in sent)

    asyncio.run(run())


def test_format_daily_report_includes_counts_and_day():
    metrics = {
        "today": {"total": 5, "success": 4, "error": 1},
        "totals": {"total": 50, "success": 45, "error": 5},
    }
    text = format_daily_report(metrics, day="2026-08-17")
    assert "2026-08-17" in text
    assert "5" in text
    assert "45" in text


def test_format_status_reports_down_state_without_metrics():
    text = format_status(None, is_up=False)
    assert "DOWN" in text
    assert "unavailable" in text.lower()


def test_handle_update_replies_only_to_allowed_chat():
    replies = []

    def handler(request):
        if request.url.path.endswith("/metrics"):
            return httpx.Response(200, json={"today": {"total": 1, "success": 1, "error": 0}})
        replies.append(request.read().decode())
        return httpx.Response(200, json={"ok": True})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            config = _config()
            state = {"is_up": True}

            await _handle_update(
                {"update_id": 1, "message": {"chat": {"id": 111}, "text": "/status"}},
                client, config, state,
            )
            assert replies  # 111 is in the allowlist

            replies.clear()
            await _handle_update(
                {"update_id": 2, "message": {"chat": {"id": 999}, "text": "/status"}},
                client, config, state,
            )
            assert replies == []  # 999 is not allowed, silently ignored

    asyncio.run(run())
