from __future__ import annotations

import logging
import re
import sys
import threading
from datetime import datetime
from typing import Any, Callable

# Sensitive tokens to redact from logs
SENSITIVE_PATTERNS = [
    re.compile(r'(X-APPLE-DS-WEB-SESSION-TOKEN["\s:=]+)[^\s,;"]+', re.IGNORECASE),
    re.compile(r'(X-APPLE-WEBAUTH-TOKEN["\s:=]+)[^\s,;"]+', re.IGNORECASE),
    re.compile(r'(X-APPLE-WEBAUTH-USER["\s:=]+)[^\s,;"]+', re.IGNORECASE),
    re.compile(r'(X-Apple-Api-Key["\s:=]+)[^\s,;"]+', re.IGNORECASE),
    re.compile(r'(scnt["\s:=]+)[^\s,;"]+', re.IGNORECASE),
    re.compile(r'(password["\s:=]+)[^\s,;"]+', re.IGNORECASE),
    re.compile(r'(Cookie["\s:=]+)[^\r\n]+', re.IGNORECASE),
]

def redact_sensitive(text: str) -> str:
    """Mask sensitive tokens, passwords, and cookies in log text."""
    if not isinstance(text, str):
        return str(text)
    sanitized = text
    for pattern in SENSITIVE_PATTERNS:
        sanitized = pattern.sub(r'\1<REDACTED>', sanitized)
    return sanitized


class SafeLogger:
    """Thread-safe application logger that records to console and DB callback."""

    def __init__(self, name: str = "iCloudAlias"):
        self.name = name
        self._lock = threading.Lock()
        self._db_callback: Callable[[str, str, str | None], None] | None = None

    def set_db_callback(self, callback: Callable[[str, str, str | None], None]) -> None:
        self._db_callback = callback

    def _log(self, level: str, message: str, account_id: str | None = None) -> None:
        clean_msg = redact_sensitive(message)
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prefix = f"[{time_str}] [{level.upper()}]"
        
        # Color formatting for terminal
        color_code = {
            "INFO": "\033[36m",     # Cyan
            "SUCCESS": "\033[32m",  # Green
            "WARNING": "\033[33m",  # Yellow
            "ERROR": "\033[31m",    # Red
        }.get(level.upper(), "")
        reset_code = "\033[0m" if color_code else ""

        with self._lock:
            print(f"{color_code}{prefix} {clean_msg}{reset_code}", flush=True)

        if self._db_callback:
            try:
                self._db_callback(level.upper(), clean_msg, account_id)
            except Exception:
                pass

    def info(self, message: str, account_id: str | None = None) -> None:
        self._log("INFO", message, account_id)

    def success(self, message: str, account_id: str | None = None) -> None:
        self._log("SUCCESS", message, account_id)

    def warning(self, message: str, account_id: str | None = None) -> None:
        self._log("WARNING", message, account_id)

    def error(self, message: str, account_id: str | None = None) -> None:
        self._log("ERROR", message, account_id)


# Global singleton logger
logger = SafeLogger()
