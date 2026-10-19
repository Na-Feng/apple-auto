from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any, Dict, List, Optional

from core.client_icloud_web import AuthExpiredError, HmeClientError, NetworkError, RateLimitError
from core.logger import logger
from core.models import AccountModel

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


class AppleAccountClient:
    """Client for Apple Account management channel (/account/manage/email/private/*)."""

    def __init__(self, account: AccountModel):
        self.account = account

    def _build_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://account.apple.com",
            "Referer": "https://account.apple.com/",
            "User-Agent": self.account.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/149.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Cookie": self.account.cookie,
            "X-Apple-I-Request-Context": "ca",
            "X-Apple-I-TimeZone": "Asia/Shanghai",
        }
        if self.account.scnt:
            headers["scnt"] = self.account.scnt
        if self.account.api_key:
            headers["X-Apple-Api-Key"] = self.account.api_key
        return headers

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 25) -> Dict[str, Any]:
        url = f"https://account.apple.com{path}"
        headers = self._build_headers()
        data_str = json.dumps(payload) if payload is not None else None

        if not HAS_CURL_CFFI:
            raise HmeClientError("Apple Account 接口需要安装 curl_cffi 支持")

        try:
            resp = cffi_requests.request(
                method=method,
                url=url,
                headers=headers,
                data=data_str,
                timeout=timeout,
                impersonate="chrome124",
            )
        except Exception as e:
            raise NetworkError(f"请求 Apple Account 超时或网络异常：{e}")

        if resp.status_code == 429:
            raise RateLimitError("Apple Account 别名创建达到频率上限", retry_after=1800)
        if resp.status_code in (401, 403, 421):
            raise AuthExpiredError("Apple Account 凭证失效 (401/403/421)")

        try:
            parsed = resp.json()
        except Exception:
            parsed = {"text": resp.text}

        if resp.status_code >= 400:
            msg = str(parsed)
            if "limit" in msg.lower() or "too many" in msg.lower():
                raise RateLimitError(f"Apple Account 别名创建达到上限：{msg}")
            raise HmeClientError(f"Apple Account 返回 HTTP {resp.status_code}: {msg}")

        return parsed

    def generate_candidate(self) -> str:
        """Step 1: POST /account/manage/email/private/add -> {"emailAddress": "xxxxx@icloud.com"}"""
        resp = self._request("POST", "/account/manage/email/private/add", payload={})
        email = resp.get("emailAddress", "")
        if not email:
            raise HmeClientError("Apple Account 未返回候选邮箱", raw_response=resp)
        return email

    def complete_reserve(self, candidate_email: str, label: str = "", note: str = "") -> Dict[str, Any]:
        """Step 2: PUT /account/manage/email/private/add/complete -> {"emailAddress", "id", "active"}"""
        if not label:
            label = f"Auto-{time.strftime('%m%d-%H%M%S')}"

        payload = {
            "emailAddress": candidate_email,
            "label": label,
            "note": note or "",
        }
        resp = self._request("PUT", "/account/manage/email/private/add/complete", payload=payload)
        return {
            "email": resp.get("emailAddress", candidate_email),
            "anonymous_id": resp.get("id", ""),
            "label": resp.get("label", label),
            "note": resp.get("note", note),
            "forward_to": resp.get("forwardToEmail", ""),
            "is_active": resp.get("active", True),
        }

    def create_alias(self, label: str = "", note: str = "") -> Dict[str, Any]:
        candidate = self.generate_candidate()
        time.sleep(0.5)
        return self.complete_reserve(candidate, label=label, note=note)
