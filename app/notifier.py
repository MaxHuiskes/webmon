import html
import logging

import httpx

from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)
TELEGRAM_LIMIT = 4096


def _esc(value: str) -> str:
    return html.escape(value or "", quote=False)


async def send_telegram_alert(
    monitor_name: str,
    url: str,
    change_summary: str,
    diff_text: str = "",
) -> bool | None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not configured. Skipping notification.")
        return None

    text = (
        "🚨 <b>Web Change Detected!</b>\n\n"
        f"<b>Target:</b> {_esc(monitor_name)}\n"
        f"<b>URL:</b> {_esc(url)}\n"
        f"<b>Trigger:</b> {_esc(change_summary)}\n"
    )
    if diff_text:
        clean_diff = (diff_text[:1200] + "...") if len(diff_text) > 1200 else diff_text
        text += f"\n<pre>{_esc(clean_diff)}</pre>"
    if len(text) > TELEGRAM_LIMIT:
        text = text[: TELEGRAM_LIMIT - 1] + "…"

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(api_url, json=payload)
            resp.raise_for_status()
        return True
    except Exception as exc:
        logger.error("Failed to dispatch Telegram message: %s", exc)
        return False
