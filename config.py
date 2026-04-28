from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

APP_ENV = os.getenv("APP_ENV", "development").lower()

SITE_URL = (
    os.environ["SITE_URL_PROD"]
    if APP_ENV in {"production", "prod"}
    else os.environ["SITE_URL_DEV"]
).rstrip("/")
