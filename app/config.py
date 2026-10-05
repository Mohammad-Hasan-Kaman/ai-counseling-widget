# -*- coding: utf-8 -*-
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

PROFILES_JSON = DATA_DIR / "consultants_profile_full.json"
MAPPING_JSON = DATA_DIR / "mapping.json"
APPOINTMENTS_DB = DATA_DIR / "appointments.db"
USER_RECORDS_DB = DATA_DIR / "user_records.db"
AI_KNOWLEDGE_DB = DATA_DIR / "ai_knowledge.db"

ADMIN_PANEL_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")

SUPER_ADMIN_USERNAME = os.getenv("SUPER_ADMIN_USERNAME", "superadmin")
SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD", ADMIN_PANEL_PASSWORD)

CRAWLER_INTERVAL_HOURS = 2
CRAWLER_DELAY_SECONDS = 1
CRAWLER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
CRAWLER_ENABLED = os.getenv("CRAWLER_ENABLED", "1") == "1"
# if appointment data is older than this, the dashboard shows a staleness warning (seconds)
CRAWL_STALE_AFTER_SECONDS = int(os.getenv("CRAWL_STALE_AFTER_SECONDS", str(6 * 3600)))

ADMIN_SESSION_TTL = 4 * 3600
WIDGET_SESSION_TTL = 24 * 3600
MAX_MESSAGE_LENGTH = 2000

# behind an HTTPS reverse proxy: the panel cookie is only sent over HTTPS
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"

CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:8000").split(",") if o.strip()]
WIDGET_ORIGIN = os.getenv("WIDGET_ORIGIN", "https://nikravan.org")
