"""Telegram bot that monitors the bsconv API.

    python -m bsconv.telegram_bot

Runs three background loops:
    - health checks GET /health on an interval and alerts the configured
      chat(s) when the API goes down, and again when it recovers
    - a daily report, built from GET /metrics, posted once a day
    - a command listener answering /status and /stats on demand

Environment
    TELEGRAM_BOT_TOKEN        required - token from @BotFather
    TELEGRAM_CHAT_ID          required - chat id(s) to notify, comma-separated
    BSCONV_API_BASE_URL       default "http://localhost:8000"
    BSCONV_API_KEY            X-API-Key sent to the monitored API
    BSCONV_HEALTH_INTERVAL    seconds between health checks, default 60
    BSCONV_DAILY_REPORT_HOUR  UTC hour (0-23) to send the daily report, default 9
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
_EMPTY_DAY = {"total": 0, "success": 0, "error": 0}


class BotConfig:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = env if env is not None else os.environ
        self.bot_token = env["TELEGRAM_BOT_TOKEN"]
        self.chat_ids = [c.strip() for c in env["TELEGRAM_CHAT_ID"].split(",") if c.strip()]
        self.api_base_url = env.get("BSCONV_API_BASE_URL", "http://localhost:8000").rstrip("/")
        self.api_key = env.get("BSCONV_API_KEY", "")
        self.health_interval = float(env.get("BSCONV_HEALTH_INTERVAL", "60"))
        self.daily_report_hour = int(env.get("BSCONV_DAILY_REPORT_HOUR", "9"))


def _headers(config: BotConfig) -> dict[str, str]:
    return {"X-API-Key": config.api_key} if config.api_key else {}


async def _send_to_chat(client: httpx.AsyncClient, config: BotConfig, chat_id: str, text: str) -> None:
    url = TELEGRAM_API.format(token=config.bot_token, method="sendMessage")
    try:
        await client.post(url, data={"chat_id": chat_id, "text": text}, timeout=10)
    except httpx.HTTPError as exc:
        # Best-effort notification: a delivery failure must not crash the
        # monitor loops, but it should still be visible in the process logs.
        logger.warning("Failed to send Telegram message to chat %s: %s", chat_id, exc)


async def send_message(client: httpx.AsyncClient, config: BotConfig, text: str) -> None:
    for chat_id in config.chat_ids:
        await _send_to_chat(client, config, chat_id, text)


async def check_health(client: httpx.AsyncClient, config: BotConfig) -> tuple[bool, str]:
    url = f"{config.api_base_url}/health"
    try:
        response = await client.get(url, headers=_headers(config), timeout=10)
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}"
    return True, ""


async def fetch_metrics(client: httpx.AsyncClient, config: BotConfig) -> dict[str, Any] | None:
    url = f"{config.api_base_url}/metrics"
    try:
        response = await client.get(url, headers=_headers(config), timeout=10)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # A common cause is BSCONV_API_KEY not being set for the bot while
        # the API requires it - log it so that failure isn't silent.
        logger.warning("Failed to fetch metrics from %s: %s", url, exc)
        return None
    return response.json()


def format_daily_report(metrics: dict[str, Any], *, day: str) -> str:
    today = metrics.get("today", _EMPTY_DAY)
    totals = metrics.get("totals", _EMPTY_DAY)
    return (
        f"Daily report - {day}\n"
        f"Calls: {today['total']}\n"
        f"Successful: {today['success']}\n"
        f"Errors: {today['error']}\n"
        f"\n"
        f"All-time - calls: {totals['total']}, "
        f"success: {totals['success']}, errors: {totals['error']}"
    )


def format_status(metrics: dict[str, Any] | None, is_up: bool) -> str:
    state = "UP" if is_up else "DOWN"
    if metrics is None:
        return f"API status: {state}\nMetrics unavailable."
    today = metrics.get("today", _EMPTY_DAY)
    return (
        f"API status: {state}\n"
        f"Today - calls: {today['total']}, "
        f"success: {today['success']}, errors: {today['error']}"
    )


async def _health_tick(client: httpx.AsyncClient, config: BotConfig, state: dict[str, Any]) -> None:
    is_up, detail = await check_health(client, config)
    was_up = state.get("is_up", True)
    if is_up and not was_up:
        await send_message(client, config, "API is back UP.")
    elif not is_up and was_up:
        await send_message(client, config, f"API appears DOWN: {detail}")
    state["is_up"] = is_up


async def health_check_loop(client: httpx.AsyncClient, config: BotConfig, state: dict[str, Any]) -> None:
    while True:
        await _health_tick(client, config, state)
        await asyncio.sleep(config.health_interval)


async def _send_daily_report_once(client: httpx.AsyncClient, config: BotConfig, *, day: str) -> None:
    metrics = await fetch_metrics(client, config)
    if metrics is not None:
        await send_message(client, config, format_daily_report(metrics, day=day))


async def daily_report_loop(client: httpx.AsyncClient, config: BotConfig) -> None:
    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=config.daily_report_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        await _send_daily_report_once(client, config, day=target.strftime("%Y-%m-%d"))


async def _handle_update(
    update: dict[str, Any], client: httpx.AsyncClient, config: BotConfig, state: dict[str, Any]
) -> None:
    message = update.get("message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()
    if chat_id not in set(config.chat_ids):
        return  # only respond to the configured chat(s)
    if text in ("/status", "/stats"):
        metrics = await fetch_metrics(client, config)
        reply = format_status(metrics, state.get("is_up", True))
        await _send_to_chat(client, config, chat_id, reply)


async def command_loop(client: httpx.AsyncClient, config: BotConfig, state: dict[str, Any]) -> None:
    offset = 0
    url = TELEGRAM_API.format(token=config.bot_token, method="getUpdates")
    while True:
        try:
            response = await client.get(url, params={"timeout": 30, "offset": offset}, timeout=35)
            response.raise_for_status()
            updates = response.json().get("result", [])
        except httpx.HTTPError:
            await asyncio.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            await _handle_update(update, client, config, state)


async def run(config: BotConfig | None = None) -> None:
    config = config or BotConfig()
    state: dict[str, Any] = {"is_up": True}
    async with httpx.AsyncClient() as client:
        await asyncio.gather(
            health_check_loop(client, config, state),
            daily_report_loop(client, config),
            command_loop(client, config, state),
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()
