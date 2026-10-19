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
    channel: str = "icloud_web"  # "icloud_web" or "apple_account"
    scnt: str = ""
    api_key: str = ""
    status: str = "active"  # "active", "cooling_down", "need_login", "disabled"
    cooldown_until: int = 0
    cooldown_stage: int = 0
    selected_forward_to: str = ""
    alias_count: int = 0
    local_alias_count: int = 0
    last_checked_at: int = 0
    last_error: str = ""
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))

    @property
    def is_cooling_down(self) -> bool:
        return self.cooldown_until > int(time.time())

    @property
    def is_available(self) -> bool:
        return self.status == "active" and not self.is_cooling_down

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
            "status": self.status,
            "is_cooling_down": self.is_cooling_down,
            "cooldown_until": self.cooldown_until,
            "cooldown_remaining_seconds": max(0, self.cooldown_until - int(time.time())),
            "cooldown_stage": self.cooldown_stage,
            "selected_forward_to": self.selected_forward_to,
            "alias_count": self.alias_count,
            "local_alias_count": self.local_alias_count,
            "last_checked_at": self.last_checked_at,
            "last_error": self.last_error,
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
    status: str = "queued"  # "queued", "running", "paused", "completed", "failed", "cancelled"
    current_account_name: str = ""
    last_error: str = ""
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict[str, Any]:
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
            "progress_percent": int((self.created_count / self.total_requested * 100)) if self.total_requested > 0 else 0,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
