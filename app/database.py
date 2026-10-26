import sqlite3
from contextlib import contextmanager
from typing import Iterator

from app.config import DB_DIR

DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "webmon.db"


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                css_selector TEXT DEFAULT NULL,
                match_type TEXT NOT NULL CHECK(
                    match_type IN (
                        'selector_diff',
                        'keyword_present',
                        'keyword_missing',
                        'text_diff'
                    )
                ),
                keyword TEXT DEFAULT NULL,
                interval_minutes INTEGER NOT NULL DEFAULT 60,
                is_active INTEGER NOT NULL DEFAULT 1,
                last_checked_at TIMESTAMP,
                last_content_hash TEXT,
                last_content_snippet TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS change_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                summary TEXT NOT NULL,
                diff_text TEXT,
                notified_status TEXT DEFAULT 'SENT'
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_change_logs_monitor ON change_logs(monitor_id, detected_at);"
        )
        conn.commit()


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        yield conn
    finally:
        conn.close()
