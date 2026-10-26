import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from app.config import DASHBOARD_PASSWORD, DASHBOARD_USER, STATIC_INDEX
from app.database import get_db, init_db
from app.worker import check_monitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

MatchType = Literal["selector_diff", "keyword_present", "keyword_missing", "text_diff"]

scheduler = AsyncIOScheduler(timezone="UTC")
security = HTTPBasic()


def authenticate(creds: HTTPBasicCredentials = Depends(security)) -> str:
    user_ok = secrets.compare_digest(creds.username, DASHBOARD_USER)
    pass_ok = secrets.compare_digest(creds.password, DASHBOARD_PASSWORD)
    if not user_ok or not pass_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username


def sync_scheduler() -> None:
    with get_db() as conn:
        monitors = conn.execute(
            "SELECT id, interval_minutes FROM monitors WHERE is_active = 1"
        ).fetchall()

    active_ids: set[str] = set()
    now = datetime.now(timezone.utc)
    for index, monitor in enumerate(monitors):
        job_id = f"monitor_{monitor['id']}"
        active_ids.add(job_id)
        minutes = max(1, int(monitor["interval_minutes"]))
        existing = scheduler.get_job(job_id)
        if existing is None:
            scheduler.add_job(
                check_monitor,
                "interval",
                minutes=minutes,
                args=[monitor["id"]],
                id=job_id,
                replace_existing=True,
                next_run_time=now + timedelta(seconds=index * 2),
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
            )
            continue
        current = getattr(existing.trigger, "interval", None)
        current_minutes = int(current.total_seconds() // 60) if current is not None else None
        if current_minutes != minutes:
            existing.reschedule("interval", minutes=minutes)

    for job in list(scheduler.get_jobs()):
        if job.id.startswith("monitor_") and job.id not in active_ids:
            scheduler.remove_job(job.id)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not DASHBOARD_PASSWORD:
        raise RuntimeError("Set DASHBOARD_PASSWORD in the environment or .env")
    init_db()
    sync_scheduler()
    scheduler.start()
    logger.info("Scheduler started with %s job(s)", len(scheduler.get_jobs()))
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan, title="Personal Web Monitor")


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


class MonitorIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=8, max_length=2000)
    css_selector: Optional[str] = Field(default=None, max_length=500)
    match_type: MatchType = "text_diff"
    keyword: Optional[str] = Field(default=None, max_length=500)
    interval_minutes: int = Field(default=60, ge=1, le=10080)


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _validate(item: MonitorIn) -> tuple[str, Optional[str], Optional[str]]:
    url = item.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url must start with http:// or https://")
    keyword = _clean(item.keyword)
    if item.match_type in ("keyword_present", "keyword_missing") and not keyword:
        raise HTTPException(status_code=400, detail="keyword is required for this trigger")
    return url, _clean(item.css_selector), keyword


@app.get("/", response_class=HTMLResponse)
def index_view(_: str = Depends(authenticate)) -> str:
    return STATIC_INDEX.read_text(encoding="utf-8")


@app.get("/api/monitors")
def list_monitors(_: str = Depends(authenticate)) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM monitors ORDER BY id DESC").fetchall()
    return [dict(row) for row in rows]


@app.get("/api/logs")
def list_logs(_: str = Depends(authenticate), limit: int = 30) -> list[dict]:
    capped = min(max(limit, 1), 100)
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT change_logs.*, monitors.name AS monitor_name, monitors.url AS monitor_url
            FROM change_logs
            JOIN monitors ON monitors.id = change_logs.monitor_id
            ORDER BY change_logs.id DESC
            LIMIT ?
            """,
            (capped,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/monitors", status_code=201)
async def create_monitor(item: MonitorIn, _: str = Depends(authenticate)) -> dict[str, int]:
    url, selector, keyword = _validate(item)
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO monitors (name, url, css_selector, match_type, keyword, interval_minutes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item.name.strip(), url, selector, item.match_type, keyword, item.interval_minutes),
        )
        conn.commit()
        monitor_id = int(cur.lastrowid)
    sync_scheduler()
    return {"id": monitor_id}


@app.put("/api/monitors/{monitor_id}")
async def update_monitor(monitor_id: int, item: MonitorIn, _: str = Depends(authenticate)) -> dict[str, str]:
    url, selector, keyword = _validate(item)
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="monitor not found")
        reset = (
            existing["url"] != url
            or (existing["css_selector"] or None) != selector
            or existing["match_type"] != item.match_type
            or (existing["keyword"] or None) != keyword
        )
        if reset:
            conn.execute(
                """
                UPDATE monitors
                SET name = ?, url = ?, css_selector = ?, match_type = ?, keyword = ?,
                    interval_minutes = ?, last_content_hash = NULL, last_content_snippet = NULL
                WHERE id = ?
                """,
                (item.name.strip(), url, selector, item.match_type, keyword, item.interval_minutes, monitor_id),
            )
        else:
            conn.execute(
                """
                UPDATE monitors
                SET name = ?, url = ?, css_selector = ?, match_type = ?, keyword = ?, interval_minutes = ?
                WHERE id = ?
                """,
                (item.name.strip(), url, selector, item.match_type, keyword, item.interval_minutes, monitor_id),
            )
        conn.commit()
    sync_scheduler()
    return {"status": "ok"}


@app.post("/api/monitors/{monitor_id}/toggle")
async def toggle_monitor(monitor_id: int, _: str = Depends(authenticate)) -> dict[str, str]:
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE monitors
            SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END
            WHERE id = ?
            """,
            (monitor_id,),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="monitor not found")
    sync_scheduler()
    return {"status": "ok"}


@app.post("/api/monitors/{monitor_id}/check")
async def run_check(monitor_id: int, _: str = Depends(authenticate)) -> dict[str, str]:
    with get_db() as conn:
        row = conn.execute("SELECT id FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="monitor not found")
    asyncio.create_task(check_monitor(monitor_id, require_active=False))
    return {"status": "queued"}


@app.delete("/api/monitors/{monitor_id}")
async def delete_monitor(monitor_id: int, _: str = Depends(authenticate)) -> dict[str, str]:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="monitor not found")
    sync_scheduler()
    return {"status": "ok"}
