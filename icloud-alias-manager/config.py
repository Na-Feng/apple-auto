from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Server configuration
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8080"))

# Cooldown & Pacing configuration
INTER_CREATE_DELAY_SECONDS = float(os.environ.get("INTER_CREATE_DELAY_SECONDS", "2.5"))
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = int(os.environ.get("DEFAULT_COOLDOWN_SECONDS", "1800"))  # 30 min
MAX_RATE_LIMIT_COOLDOWN_SECONDS = int(os.environ.get("MAX_COOLDOWN_SECONDS", "7200"))          # 120 min

# Database configuration
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = DATA_DIR / "icloud_alias.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
