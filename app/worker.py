import asyncio
import difflib
import hashlib
import logging
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup, Comment

from app.database import get_db
from app.notifier import send_telegram_alert

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "template", "canvas")
_VOLATILE_ATTRS = {"nonce", "integrity", "data-nonce", "data-csrf", "csrf"}
_NOISE_TOKENS = (
    "adsbygoogle",
    "advertisement",
    "cookie-banner",
    "cookieconsent",
    "onetrust",
)
_SNIPPET_LIMIT = 8000
_locks: dict[int, asyncio.Lock] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _lock_for(monitor_id: int) -> asyncio.Lock:
    lock = _locks.get(monitor_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[monitor_id] = lock
    return lock


def clean_html(raw_html: str, selector: str | None) -> tuple[str, bool]:
    """Return normalized visible text and whether a requested selector matched."""
    soup = BeautifulSoup(raw_html, "html.parser")
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()

    noisy: list[object] = []
    for tag in soup.find_all(True):
        attrs = getattr(tag, "attrs", None)
        if not isinstance(attrs, dict):
            continue
        for attr in list(attrs):
            if attr.lower() in _VOLATILE_ATTRS or "csrf" in attr.lower():
                del attrs[attr]
        classes = attrs.get("class", [])
        if isinstance(classes, str):
            classes = [classes]
        ident = " ".join([*classes, str(attrs.get("id", ""))]).lower()
        if any(token in ident for token in _NOISE_TOKENS):
            noisy.append(tag)
    for tag in noisy:
        tag.decompose()

    if selector:
        try:
            target = soup.select_one(selector)
        except Exception as exc:
            raise ValueError(f"invalid css selector: {exc}") from exc
        if target is None:
            return "", False
        node = target
    else:
        node = soup

    lines: list[str] = []
    for line in node.get_text(separator="\n", strip=True).splitlines():
        collapsed = re.sub(r"\s+", " ", line).strip()
        if collapsed:
            lines.append(collapsed)
    return "\n".join(lines), True


def compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def remember_keyword(text: str, snippet: str, keyword: str | None) -> str:
    """Keep the keyword inside the stored snippet so later checks can see it."""
    needle = (keyword or "").strip()
    if not needle or needle.lower() not in text.lower() or needle.lower() in snippet.lower():
        return snippet
    return f"{needle}\n{snippet}"[:_SNIPPET_LIMIT]


def _diff(old: str, new: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile="old",
        tofile="new",
        n=1,
    )
    return "".join(lines)


async def check_monitor(monitor_id: int, *, require_active: bool = True) -> None:
    async with _lock_for(monitor_id):
        try:
            await _check_monitor(monitor_id, require_active=require_active)
        except Exception:
            logger.exception("Monitor %s check failed", monitor_id)


async def _check_monitor(monitor_id: int, *, require_active: bool) -> None:
    query = "SELECT * FROM monitors WHERE id = ?"
    params: tuple[int, ...] = (monitor_id,)
    if require_active:
        query += " AND is_active = 1"
    with get_db() as conn:
        row = conn.execute(query, params).fetchone()
    if not row:
        return

    monitor = dict(row)
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=20.0, follow_redirects=True) as client:
            response = await client.get(monitor["url"])
            response.raise_for_status()
            current_raw = response.text
    except Exception as exc:
        logger.warning("Error fetching %s (monitor %s): %s", monitor["url"], monitor["id"], exc)
        return

    selector = monitor["css_selector"] or None
    try:
        current_text, found = clean_html(current_raw, selector)
    except ValueError as exc:
        logger.warning("Monitor %s: %s", monitor["id"], exc)
        return

    if selector and not found:
        logger.warning("Monitor %s selector matched nothing: %s", monitor["id"], selector)
        with get_db() as conn:
            conn.execute(
                "UPDATE monitors SET last_checked_at = ? WHERE id = ?",
                (utc_now(), monitor["id"]),
            )
            conn.commit()
        return

    new_hash = compute_hash(current_text)
    old_hash = monitor["last_content_hash"]
    old_snippet = monitor["last_content_snippet"] or ""
    snippet = remember_keyword(current_text, current_text[:_SNIPPET_LIMIT], monitor["keyword"])
    triggered = False
    summary = ""
    diff_text = ""

    if old_hash is None:
        with get_db() as conn:
            conn.execute(
                """
                UPDATE monitors
                SET last_checked_at = ?, last_content_hash = ?, last_content_snippet = ?
                WHERE id = ?
                """,
                (utc_now(), new_hash, snippet, monitor["id"]),
            )
            conn.commit()
        logger.info("Seeded baseline for monitor %s", monitor["id"])
        return

    match_type = monitor["match_type"]
    if match_type in ("selector_diff", "text_diff"):
        if new_hash != old_hash:
            triggered = True
            summary = "Page content modified"
            diff_text = _diff(old_snippet, snippet)
            if not diff_text.strip():
                diff_text = "Content changed outside the stored snippet."
    elif match_type == "keyword_present":
        keyword = (monitor["keyword"] or "").strip()
        if keyword and keyword.lower() in current_text.lower() and keyword.lower() not in old_snippet.lower():
            triggered = True
            summary = f"Keyword detected: '{keyword}'"
    elif match_type == "keyword_missing":
        keyword = (monitor["keyword"] or "").strip()
        if keyword and keyword.lower() not in current_text.lower() and keyword.lower() in old_snippet.lower():
            triggered = True
            summary = f"Keyword no longer present: '{keyword}'"

    notified_status = "SKIPPED"
    if triggered:
        sent = await send_telegram_alert(monitor["name"], monitor["url"], summary, diff_text)
        if sent is True:
            notified_status = "SENT"
        elif sent is None:
            notified_status = "SKIPPED"
        else:
            notified_status = "FAILED"

    with get_db() as conn:
        conn.execute(
            """
            UPDATE monitors
            SET last_checked_at = ?, last_content_hash = ?, last_content_snippet = ?
            WHERE id = ?
            """,
            (utc_now(), new_hash, snippet, monitor["id"]),
        )
        if triggered:
            conn.execute(
                """
                INSERT INTO change_logs (monitor_id, summary, diff_text, notified_status)
                VALUES (?, ?, ?, ?)
                """,
                (monitor["id"], summary, diff_text, notified_status),
            )
        conn.commit()
