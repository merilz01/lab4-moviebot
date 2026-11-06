import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
KINOPOISK_TOKEN = os.getenv("KINOPOISK_TOKEN")
ZEXKORA_TOKEN = os.getenv("ZEXKORA_TOKEN")

DB_PATH = BASE_DIR / "movies.db"


def validate_config():
    missing = [
        name
        for name, value in (
            ("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
            ("KINOPOISK_TOKEN", KINOPOISK_TOKEN),
        )
        if not value
    ]
    if missing:
        raise ValueError("Заполни .env: " + ", ".join(missing))
