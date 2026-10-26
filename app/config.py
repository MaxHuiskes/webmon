import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
DB_DIR = Path(os.getenv("DB_DIR", str(ROOT / "data")))
if not DB_DIR.is_absolute():
    DB_DIR = ROOT / DB_DIR

STATIC_INDEX = ROOT / "static" / "index.html"
