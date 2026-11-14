from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PRODUCT_DATA_FILE = DATA_DIR / "valio_aimo_junction26_product_data.json"

load_dotenv(BASE_DIR / ".env")

VALIO_BASE_URL = os.getenv("VALIO_BASE_URL", "https://junction26.valioaimo.fi").strip()
VALIO_API_KEY = os.getenv("VALIO_API_KEY", "").strip()
APP_ENV = os.getenv("APP_ENV", "production").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./trusted_trade.db")
TRUSTED_TRADE_AGENT_CUSTOMERS = os.getenv(
    "TRUSTED_TRADE_AGENT_CUSTOMERS", "demo-agent:customer-001"
).strip()
TRUSTED_TRADE_AGENT_TOKENS = os.getenv("TRUSTED_TRADE_AGENT_TOKENS", "").strip()
TRUSTED_TRADE_ALLOWED_CATEGORIES = os.getenv("TRUSTED_TRADE_ALLOWED_CATEGORIES", "").strip()
TRUSTED_TRADE_APPROVERS = os.getenv("TRUSTED_TRADE_APPROVERS", "human-approver").strip()
TRUSTED_TRADE_MAX_LINE_QUANTITY = os.getenv("TRUSTED_TRADE_MAX_LINE_QUANTITY", "").strip()
TRUSTED_TRADE_MAX_ORDER_VALUE = os.getenv("TRUSTED_TRADE_MAX_ORDER_VALUE", "").strip()
TRUSTED_TRADE_REQUIRE_HUMAN_APPROVAL = os.getenv(
    "TRUSTED_TRADE_REQUIRE_HUMAN_APPROVAL", "true"
).strip().lower() not in {"0", "false", "no"}
TRUSTED_TRADE_APPROVER_TOKENS = os.getenv("TRUSTED_TRADE_APPROVER_TOKENS", "").strip()
TRUSTED_TRADE_APPROVAL_TTL_HOURS = os.getenv(
    "TRUSTED_TRADE_APPROVAL_TTL_HOURS", "24"
).strip()
