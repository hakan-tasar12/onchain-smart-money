"""Low-level Telegram Bot API client.

Shared by the one-way alert sender (``src/alerts.py``) and the interactive bot
(``src/bot.py``). Pure HTTP via ``requests`` — no third-party bot framework, so
the surface stays small and easy to audit. The bot token is read from the
``TELEGRAM_BOT_TOKEN`` environment variable on every call.
"""
import logging
import os

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
_TIMEOUT = 15


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "")


def _post(method: str, payload: dict, timeout: int = _TIMEOUT) -> dict:
    token = _token()
    if not token:
        log.error("TELEGRAM_BOT_TOKEN missing")
        return {"ok": False}
    try:
        r = requests.post(f"{API_BASE}/bot{token}/{method}", json=payload, timeout=timeout)
        return r.json()
    except Exception as e:  # network / JSON errors must never crash the caller
        log.error("Telegram %s error: %s", method, e)
        return {"ok": False}


def send_message(chat_id, text: str, reply_markup: dict | None = None,
                 parse_mode: str = "HTML", disable_preview: bool = True) -> dict:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _post("sendMessage", payload)


def answer_callback_query(callback_query_id: str, text: str | None = None) -> dict:
    """Acknowledge a button tap so Telegram stops the client-side spinner."""
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    return _post("answerCallbackQuery", payload)


def get_updates(offset: int | None = None, timeout: int = 30) -> list[dict]:
    """Long-poll for new updates. The HTTP timeout must exceed the poll timeout."""
    payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    resp = _post("getUpdates", payload, timeout=timeout + 10)
    return resp.get("result", []) if resp.get("ok") else []


def set_my_commands(commands: list[tuple[str, str]]) -> dict:
    """Register the slash-command menu Telegram shows in the input bar."""
    payload = {"commands": [{"command": c, "description": d} for c, d in commands]}
    return _post("setMyCommands", payload)


def get_me() -> dict:
    return _post("getMe", {})


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """Build an inline-keyboard markup from rows of ``(label, callback_data)``."""
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }
