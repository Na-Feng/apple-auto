from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from core.logger import logger
from core.models import AccountModel

# Try to use curl_cffi for Chrome/Safari TLS fingerprint impersonation
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


class HmeClientError(Exception):
    """Base error for iCloud HME client."""
    def __init__(self, message: str, raw_response: Any = None):
        super().__init__(message)
        self.raw_response = raw_response


class RateLimitError(HmeClientError):
    """Raised when iCloud rate limit / address creation limit is reached."""
    def __init__(self, message: str, retry_after: int = 1800, raw_response: Any = None):
        super().__init__(message, raw_response)
        self.retry_after = retry_after


class MaxLimitReachedError(HmeClientError):
    """Raised when the account has reached Apple's hard maximum quota (e.g. 750 aliases, Code -41012 or max_limit_reached)."""
    pass


class AuthExpiredError(HmeClientError):
    """Raised when iCloud session has expired (HTTP 401/403/421)."""
    pass


class CandidateExpiredError(HmeClientError):
    """Raised when candidate address is expired (-41003)."""
    pass


class NetworkError(HmeClientError):
    """Raised on connection timeout or network glitch."""
    pass


class ICloudWebClient:
    """iCloud Hide My Email (HME) Web Client supporting curl_cffi & urllib fallback."""

    def __init__(self, account: AccountModel):
        self.account = account

    def _build_url(self, path: str) -> str:
        query = urllib.parse.urlencode({
            "clientBuildNumber": self.account.client_build_number,
            "clientMasteringNumber": self.account.client_mastering_number,
            "clientId": self.account.client_id,
            "dsid": self.account.dsid,
        })
        host = self.account.host.strip("/")
        return f"https://{host}{path}?{query}"

    def _build_headers(self) -> Dict[str, str]:
        return {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Content-Type": "text/plain;charset=UTF-8",
            "Origin": self.account.origin,
            "Referer": self.account.referer,
            "User-Agent": self.account.user_agent,
            "Cookie": self.account.cookie,
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 25) -> Dict[str, Any]:
        url = self._build_url(path)
        headers = self._build_headers()
        data_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None

        status_code = 0
        response_text = ""

        # Strategy 1: curl_cffi with Chrome impersonation
        proxy = self.account.proxy_url.strip() if self.account.proxy_url else None
        proxies_to_try = [proxy]
        if proxy and "@" in proxy:
            import urllib.parse
            parsed_p = urllib.parse.urlsplit(proxy)
            general_pool = f"{parsed_p.scheme}://{parsed_p.hostname}:{parsed_p.port}"
            if general_pool != proxy:
                proxies_to_try.append(general_pool)

        if HAS_CURL_CFFI:
            for p_attempt, cur_proxy in enumerate(proxies_to_try):
                try:
                    resp = cffi_requests.request(
                        method=method,
                        url=url,
                        headers=headers,
                        data=data_bytes,
                        timeout=timeout,
                        impersonate="chrome124",
                        proxy=cur_proxy,
                    )
                    status_code = resp.status_code
                    response_text = resp.text
                    break
                except Exception as e:
                    if p_attempt < len(proxies_to_try) - 1:
                        logger.warning(f"iCloud Web 专有代理抖动 ({e})，正在自动切换至健康代理池重试...", self.account.id)
                        time.sleep(1)
                        continue
                    logger.warning(f"curl_cffi request failed, trying urllib fallback: {e}", self.account.id)
                    status_code = 0

        # Strategy 2: urllib standard library fallback
        if status_code == 0:
            req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
            try:
                if proxy:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
                    resp_ctx = opener.open(req, timeout=timeout)
                else:
                    resp_ctx = urllib.request.urlopen(req, timeout=timeout)
                with resp_ctx as resp:
                    status_code = resp.status
                    response_text = resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                status_code = e.code
                response_text = e.read().decode("utf-8", errors="replace")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise NetworkError(f"连接 iCloud 服务器超时或失败：{e}")

        # Parse JSON response
        try:
            parsed = json.loads(response_text)
        except Exception:
            parsed = {"raw": response_text}

        # Check HTTP status codes
        if status_code == 429:
            raise RateLimitError(f"HTTP 429 命中频率限制：{response_text[:200]}", retry_after=1800, raw_response=parsed)
        if status_code in (401, 403, 421):
            raise AuthExpiredError(f"HTTP {status_code} 登录态失效，需要重新导入：{response_text[:200]}", raw_response=parsed)
        if status_code >= 400:
            self._classify_and_raise(parsed, status_code)

        # Check iCloud envelope {"success": true, "result": ...}
        if isinstance(parsed, dict) and parsed.get("success") is not True:
            self._classify_and_raise(parsed, status_code)

        return parsed

    def _classify_and_raise(self, parsed: Any, status_code: int) -> None:
        """Classify iCloud error messages into domain exceptions."""
        error_info = {}
        error_code = ""
        error_msg = ""
        if isinstance(parsed, dict):
            error_info = parsed.get("error") or {}
            if isinstance(error_info, dict):
                error_code = str(error_info.get("errorCode", ""))
                error_msg = str(error_info.get("errorMessage", ""))
            if not error_msg:
                error_msg = str(parsed.get("errorMessage") or parsed.get("error") or parsed)
        else:
            error_msg = str(parsed)

        combined = f"{error_code} {error_msg}".lower()

        # Hard Max Capacity Limit check (750 aliases reached)
        if error_code == "-41012" or "maximum number of email addresses" in combined:
            raise MaxLimitReachedError(f"已达到 Apple 官方别名总额度上限 (750个满额)：{error_msg}", raw_response=parsed)

        # Rate Limit check
        if error_code == "-41015" or any(w in combined for w in [
            "limit", "reached the limit of addresses", "创建上限", "too many", "try again later", "暂时限制"
        ]):
            raise RateLimitError(f"已达到当前 iCloud 别名创建上限：{error_msg or error_code}", retry_after=1800, raw_response=parsed)

        # Candidate Expired check
        if error_code == "-41003":
            raise CandidateExpiredError(f"候选地址已过期失效：{error_msg}", raw_response=parsed)

        # Auth Expired check
        if any(w in combined for w in ["session", "auth", "expired", "401", "403", "unauthorized"]):
            raise AuthExpiredError(f"iCloud 登录已失效：{error_msg}", raw_response=parsed)

        raise HmeClientError(f"iCloud 接口错误 (HTTP {status_code}, Code {error_code}): {error_msg}", raw_response=parsed)

    # ==========================
    # BUSINESS METHODS
    # ==========================

    def check_session(self) -> Dict[str, Any]:
        """Test session connectivity, fetch settings and alias count."""
        resp = self._request("GET", "/v2/hme/list")
        result = resp.get("result") or {}
        emails = result.get("hmeEmails") or []
        forward_to = result.get("selectedForwardTo") or ""
        forward_list = result.get("forwardToEmails") or []
        return {
            "valid": True,
            "alias_count": len(emails),
            "selected_forward_to": forward_to,
            "forward_to_emails": forward_list,
        }

    def generate_alias(self) -> str:
        """Step 1: Generate candidate email address (e.g. xxxxx@icloud.com)."""
        resp = self._request("POST", "/v1/hme/generate", {"langCode": self.account.lang_code})
        result = resp.get("result") or {}
        candidate = result.get("hme")
        if not candidate or not isinstance(candidate, str):
            raise HmeClientError("生成候选别名返回空地址", raw_response=resp)
        return candidate.strip()

    def reserve_alias(self, candidate_email: str, label: str = "", note: str = "") -> Dict[str, Any]:
        """Step 2: Reserve (occupy) the candidate address."""
        if not label:
            label = f"Auto-{time.strftime('%m%d-%H%M%S')}"

        resp = self._request("POST", "/v1/hme/reserve", {
            "hme": candidate_email,
            "label": label,
            "note": note or "",
        })
        result = resp.get("result") or {}
        alias_info = result.get("hme") or {}
        if not isinstance(alias_info, dict) or not alias_info.get("anonymousId"):
            raise HmeClientError("保留别名响应异常，缺少 anonymousId", raw_response=resp)

        return {
            "email": alias_info.get("hme", candidate_email),
            "anonymous_id": alias_info.get("anonymousId", ""),
            "label": alias_info.get("label", label),
            "note": alias_info.get("note", note),
            "forward_to": alias_info.get("forwardToEmail", ""),
            "is_active": alias_info.get("isActive", True),
        }

    def create_alias(self, label: str = "", note: str = "") -> Dict[str, Any]:
        """Convenience method: generate + reserve in one go."""
        candidate = self.generate_alias()
        time.sleep(0.5)  # slight pause between generate and reserve
        return self.reserve_alias(candidate, label=label, note=note)

    def list_aliases(self) -> List[Dict[str, Any]]:
        """Fetch all remote aliases belonging to this account on iCloud."""
        resp = self._request("GET", "/v2/hme/list")
        result = resp.get("result") or {}
        raw_list = result.get("hmeEmails") or []
        parsed = []
        for item in raw_list:
            parsed.append({
                "email": item.get("hme", ""),
                "anonymous_id": item.get("anonymousId", ""),
                "label": item.get("label", ""),
                "note": item.get("note", ""),
                "forward_to": item.get("forwardToEmail", ""),
                "is_active": item.get("isActive", True),
                "created_at": item.get("createTimestamp", 0) // 1000 if item.get("createTimestamp") else int(time.time()),
            })
        return parsed

    def delete_alias(self, anonymous_id: str) -> bool:
        """Remotely delete an alias from iCloud."""
        resp = self._request("POST", "/v1/hme/delete", {"anonymousId": anonymous_id})
        return resp.get("success", False) is True

    def deactivate_alias(self, anonymous_id: str) -> bool:
        """Remotely deactivate an alias on iCloud."""
        resp = self._request("POST", "/v1/hme/deactivate", {"anonymousId": anonymous_id})
        return resp.get("success", False) is True

    def activate_alias(self, anonymous_id: str) -> bool:
        """Remotely reactivate an alias on iCloud."""
        resp = self._request("POST", "/v1/hme/activate", {"anonymousId": anonymous_id})
        return resp.get("success", False) is True

    def update_alias(self, anonymous_id: str, label: str, note: str = "") -> bool:
        """Update alias label and note on iCloud."""
        resp = self._request("POST", "/v1/hme/updateMetaData", {
            "anonymousId": anonymous_id,
            "label": label,
            "note": note or "",
        })
        return resp.get("success", False) is True
