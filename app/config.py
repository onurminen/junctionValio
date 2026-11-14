from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PRODUCT_DATA_FILE = DATA_DIR / "valio_aimo_junction26_product_data.json"

VALIO_BASE_URL = os.getenv("VALIO_BASE_URL", "").strip()
VALIO_API_KEY = os.getenv("VALIO_API_KEY", "").strip()
APP_ENV = os.getenv("APP_ENV", "development").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./trusted_trade.db")
