from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AccountModel:
    id: str
    name: str
    apple_id: str
    dsid: str
    client_id: str
    client_build_number: str
    client_mastering_number: str
    host: str
    cookie: str
    origin: str = "https://www.icloud.com"
    referer: str = "https://www.icloud.com/"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    )
    lang_code: str = "zh-cn"
    channel: str = "icloud_web"  # "icloud_web", "apple_account", or "dual"
    scnt: str = ""
    api_key: str = ""
    status: str = "active"  # "active", "cooling_down", "need_login", "disabled"
    cooldown_until: int = 0
    cooldown_stage: int = 0
    apple_cooldown_until: int = 0
    icloud_cooldown_until: int = 0
    selected_forward_to: str = ""
    alias_count: int = 0
    local_alias_count: int = 0
    last_checked_at: int = 0
    last_error: str = ""
    password: str = ""
    code_url: str = ""
    target_alias_count: int = 750
    proxy_url: str = ""
    autopilot_status: str = "idle"  # "idle", "running", "waiting_cooldown", "paused", "completed"
    autopilot_cooldown_until: int = 0
    autopilot_last_error: str = ""
    apple_auth_expired: bool = False
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))

    @property
    def can_reacquire_apple(self) -> bool:
        return bool(self.password and self.code_url)

    @property
    def has_apple_account(self) -> bool:
        if self.channel not in ("dual", "apple_account"):
            return False
        # If credentials can be auto-reacquired, channel is always ready
        if self.can_reacquire_apple:
            return True
        # Otherwise requires live scnt/api_key and not marked expired
        return bool(self.scnt or self.api_key) and not getattr(self, "apple_auth_expired", False)

    @property
    def has_icloud_web(self) -> bool:
        return self.channel in ("dual", "icloud_web") and bool(self.host and "maildomainws" in self.host)

    @property
    def is_apple_cooling(self) -> bool:
        return self.apple_cooldown_until > int(time.time())

    @property
    def is_icloud_cooling(self) -> bool:
        return self.icloud_cooldown_until > int(time.time())

    @property
    def is_cooling_down(self) -> bool:
        now = int(time.time())
        if self.channel == "dual":
            # In dual-channel mode, account is ONLY cooling down if BOTH channels are actively cooling down!
            # If one channel is not cooling and can be used (e.g. apple is ready or can reacquire), it is NOT cooling down!
            apple_busy = self.is_apple_cooling or not self.has_apple_account
            icloud_busy = self.is_icloud_cooling or not self.has_icloud_web
            return apple_busy and icloud_busy
        elif self.channel == "apple_account":
            return self.is_apple_cooling or (self.cooldown_until > now)
        return self.is_icloud_cooling or (self.cooldown_until > now)

    @property
    def is_available(self) -> bool:
        return self.status in ("active", "cooling_down") and not self.is_cooling_down

    @property
    def autopilot_remaining_seconds(self) -> int:
        now = int(time.time())
        return max(0, self.autopilot_cooldown_until - now) if self.autopilot_cooldown_until > now else 0

    @property
    def current_total_aliases(self) -> int:
        return max(self.alias_count, self.local_alias_count)

    @property
    def remaining_needed(self) -> int:
        return max(0, self.target_alias_count - self.current_total_aliases)

    @property
    def progress_percent(self) -> int:
        if self.target_alias_count <= 0:
            return 0
        return min(100, int(self.current_total_aliases / self.target_alias_count * 100))

    def to_dict(self, mask_cookie: bool = True) -> dict[str, Any]:
        data = {
            "id": self.id,
            "name": self.name,
            "apple_id": self.apple_id,
            "dsid": self.dsid,
            "client_id": self.client_id,
            "client_build_number": self.client_build_number,
            "client_mastering_number": self.client_mastering_number,
            "host": self.host,
            "origin": self.origin,
            "referer": self.referer,
            "user_agent": self.user_agent,
            "lang_code": self.lang_code,
            "channel": self.channel,
            "has_apple_account": self.has_apple_account,
            "has_icloud_web": self.has_icloud_web,
            "status": self.status,
            "is_cooling_down": self.is_cooling_down,
            "cooldown_until": self.cooldown_until,
            "cooldown_remaining_seconds": max(0, self.cooldown_until - int(time.time())),
            "apple_cooldown_remaining": max(0, self.apple_cooldown_until - int(time.time())),
            "icloud_cooldown_remaining": max(0, self.icloud_cooldown_until - int(time.time())),
            "apple_auth_expired": bool(getattr(self, "apple_auth_expired", False)),
            "cooldown_stage": self.cooldown_stage,
            "selected_forward_to": self.selected_forward_to,
            "alias_count": self.alias_count,
            "local_alias_count": self.local_alias_count,
            "last_checked_at": self.last_checked_at,
            "last_error": self.last_error,
            "target_alias_count": self.target_alias_count,
            "proxy_url": self.proxy_url,
            "autopilot_status": self.autopilot_status,
            "autopilot_cooldown_until": self.autopilot_cooldown_until,
            "autopilot_remaining_seconds": max(0, self.autopilot_cooldown_until - int(time.time())),
            "autopilot_last_error": self.autopilot_last_error,
            "current_total_aliases": max(self.alias_count, self.local_alias_count),
            "remaining_needed": max(0, self.target_alias_count - max(self.alias_count, self.local_alias_count)),
            "progress_percent": min(100, int((max(self.alias_count, self.local_alias_count) / self.target_alias_count * 100))) if self.target_alias_count > 0 else 0,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if mask_cookie:
            data["cookie_masked"] = (self.cookie[:15] + "..." + self.cookie[-10:]) if len(self.cookie) > 30 else "***"
        else:
            data["cookie"] = self.cookie
        return data


@dataclass
class AliasModel:
    id: str
    account_id: str
    email: str
    anonymous_id: str
    label: str = ""
    note: str = ""
    forward_to: str = ""
    is_active: bool = True
    channel: str = "icloud_web"
    origin: str = "created"  # "created" or "synced"
    created_at: int = field(default_factory=lambda: int(time.time()))
    account_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "email": self.email,
            "anonymous_id": self.anonymous_id,
            "label": self.label,
            "note": self.note,
            "forward_to": self.forward_to,
            "is_active": self.is_active,
            "channel": self.channel,
            "origin": self.origin,
            "created_at": self.created_at,
        }


@dataclass
class BatchJobModel:
    id: str
    total_requested: int
    created_count: int = 0
    failed_count: int = 0
    strategy: str = "round_robin"  # "round_robin", "fill_first", "specific"
    target_account_id: str = ""
    base_label: str = ""
    note: str = ""
    status: str = "queued"  # "queued", "running", "paused", "waiting_cooldown", "completed", "failed", "cancelled"
    current_account_name: str = ""
    last_error: str = ""
    auto_pilot: bool = True
    cooldown_resume_at: int = 0
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict[str, Any]:
        now = int(time.time())
        cooldown_remaining = max(0, self.cooldown_resume_at - now) if (self.status == "waiting_cooldown" and self.cooldown_resume_at > 0) else 0
        return {
            "id": self.id,
            "total_requested": self.total_requested,
            "created_count": self.created_count,
            "failed_count": self.failed_count,
            "strategy": self.strategy,
            "target_account_id": self.target_account_id,
            "base_label": self.base_label,
            "note": self.note,
            "status": self.status,
            "current_account_name": self.current_account_name,
            "last_error": self.last_error,
            "auto_pilot": bool(self.auto_pilot),
            "cooldown_resume_at": self.cooldown_resume_at,
            "cooldown_remaining": cooldown_remaining,
            "progress_percent": int((self.created_count / self.total_requested * 100)) if self.total_requested > 0 else 0,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
