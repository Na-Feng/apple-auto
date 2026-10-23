from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any, Dict, List, Optional

from core.client_icloud_web import AuthExpiredError, HmeClientError, MaxLimitReachedError, NetworkError, RateLimitError
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
        api_key = self.account.api_key or "cbf645300ce50a17eb62615467b6cac9697ed54de83b620460ac2a0c51e44c27"
        if api_key == "af1139274f266b22b68c2a3e7ad932cb3c0bbe854e13a79af78dcc73136882c3":
            # af1139... is the signin OAuth client_id; use the active management API key
            api_key = "cbf645300ce50a17eb62615467b6cac9697ed54de83b620460ac2a0c51e44c27"

        # Filter out empty cookie key-values
        clean_cookie_parts = []
        if self.account.cookie:
            for part in self.account.cookie.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k.strip() and v.strip():
                        clean_cookie_parts.append(f"{k.strip()}={v.strip()}")
        cookie_hdr = "; ".join(clean_cookie_parts)

        headers = {
            "Host": "account.apple.com",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://account.apple.com",
            "Referer": "https://account.apple.com/",
            "User-Agent": self.account.user_agent or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Cookie": cookie_hdr,
            "X-Apple-Request-Context": "ca",
            "X-Apple-I-Request-Context": "ca",
            "X-Apple-I-TimeZone": "Asia/Shanghai",
            "X-Apple-I-FD-Client-Info": '{"platform":"macOS"}',
            "X-Apple-Api-Key": api_key,
        }
        if self.account.scnt:
            headers["scnt"] = self.account.scnt
        return headers

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 25) -> Dict[str, Any]:
        url = f"https://account.apple.com{path}" if not path.startswith("http") else path
        headers = self._build_headers()
        data_str = json.dumps(payload) if payload is not None else None

        if not HAS_CURL_CFFI:
            raise HmeClientError("Apple Account 接口需要安装 curl_cffi 支持")

        proxy = self.account.proxy_url.strip() if self.account.proxy_url else None
        proxies_to_try = [proxy]
        if proxy and "@" in proxy:
            import urllib.parse
            parsed_p = urllib.parse.urlsplit(proxy)
            general_pool = f"{parsed_p.scheme}://{parsed_p.hostname}:{parsed_p.port}"
            if general_pool != proxy:
                proxies_to_try.append(general_pool)

        resp = None
        last_net_err = None
        for attempt, current_proxy in enumerate(proxies_to_try):
            try:
                resp = cffi_requests.request(
                    method=method,
                    url=url,
                    headers=headers,
                    data=data_str,
                    timeout=timeout,
                    impersonate="chrome124",
                    proxy=current_proxy,
                )
                break
            except Exception as e:
                last_net_err = e
                # Network jitter on current proxy: rotate sticky lease to drift to an un-jittered IP without deleting node
                if current_proxy and "127.0.0.1:2260" in current_proxy:
                    try:
                        from core.resin_manager import resin_manager
                        resin_manager.rotate_account_session(self.account.id)
                    except Exception:
                        pass

                if attempt < len(proxies_to_try) - 1:
                    logger.warning(f"账号 [{self.account.name}] 专有代理节点网络抖动/超时 ({e})，正在自动切换至健康出口 IP 重试...", self.account.id)
                    time.sleep(1)
                    continue
                proxy_info = f" (通过代理: {current_proxy})" if current_proxy else ""
                raise NetworkError(f"请求 Apple Account 超时或网络异常{proxy_info}：{e}")

        # Auto-capture fresh scnt from response header if present
        updated_db = False
        new_scnt = resp.headers.get("scnt") or resp.headers.get("Scnt")
        if new_scnt and new_scnt.strip() and new_scnt != self.account.scnt:
            self.account.scnt = new_scnt.strip()
            updated_db = True

        # Auto-capture updated set-cookie headers (caw-at, awat, etc.)
        set_cookies = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else []
        if not set_cookies and "set-cookie" in resp.headers:
            set_cookies = [resp.headers["set-cookie"]]

        if set_cookies:
            cookies_dict = {}
            for part in (self.account.cookie or "").split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    if k.strip() and v.strip():
                        cookies_dict[k.strip()] = v.strip()
            for sc in set_cookies:
                for part in sc.split(";"):
                    if "=" in part:
                        k, v = part.strip().split("=", 1)
                        if k.strip() and v.strip():
                            cookies_dict[k.strip()] = v.strip()
            new_cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
            if new_cookie_str != self.account.cookie:
                self.account.cookie = new_cookie_str
                updated_db = True

        if updated_db:
            try:
                from core.db import db
                db.save_account(self.account)
            except Exception:
                pass

        if resp.status_code == 429:
            raise RateLimitError("Apple Account 别名创建达到频率上限", retry_after=1800)
        if resp.status_code in (401, 403, 421):
            raise AuthExpiredError(f"Apple Account 凭证失效 (HTTP {resp.status_code})")

        try:
            parsed = resp.json()
        except Exception:
            parsed = {"text": resp.text}

        if isinstance(parsed, dict) and (parsed.get("ineligibilityReason") == "max_limit_reached" or parsed.get("maxLimitReached") is True):
            raise MaxLimitReachedError("Apple Account 别名创建已达官方总上限 (750个满额)")

        if resp.status_code >= 400 or (isinstance(parsed, dict) and parsed.get("ineligibilityReason") == "rate_limit_exceeded"):
            msg = str(parsed)
            if "limit" in msg.lower() or "too many" in msg.lower() or "rate_limit_exceeded" in msg:
                raise RateLimitError(f"Apple Account 别名创建达到上限：{msg}", retry_after=1800)
            raise HmeClientError(f"Apple Account 返回 HTTP {resp.status_code}: {msg}")

        return parsed

    def keepalive(self) -> bool:
        """
        Actively renews the 15-minute lease and scnt via GET /account/manage/gs/ws/token.
        Keeps the Apple Account management session continuously alive without re-login.
        """
        try:
            resp = self._request("GET", "/account/manage/gs/ws/token", timeout=12)
            ttl = resp.get("timeOutInterval", 15)
            logger.info(f"账号 [{self.account.name}] Apple Account 活跃会话成功续期 (租期: {ttl} 分钟)", self.account.id)
            return True
        except AuthExpiredError:
            logger.warning(f"账号 [{self.account.name}] Apple Account 会话已彻底过期，需要重新认证", self.account.id)
            if self.account.password and self.account.code_url:
                return self._auto_reacquire()
            return False
        except Exception as e:
            logger.warning(f"账号 [{self.account.name}] Apple Account 续期心跳异常: {e}", self.account.id)
            return False

    def _auto_reacquire(self) -> bool:
        if not self.account.password or not self.account.code_url:
            return False
        try:
            from core.apple_acquirer import acquire_apple_account_session
            logger.info(f"账号 [{self.account.name}] 正在全自动重新采集 Apple Account 凭证...", self.account.id)
            res = acquire_apple_account_session(
                self.account.apple_id,
                self.account.password,
                self.account.code_url,
                proxy_url=self.account.proxy_url,
            )
            cookies_dict = {}
            for part in (self.account.cookie or "").split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    if k.strip() and v.strip():
                        cookies_dict[k.strip()] = v.strip()
            for part in (res.get("apple_account_cookie") or "").split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    if k.strip() and v.strip():
                        cookies_dict[k.strip()] = v.strip()
            self.account.cookie = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
            self.account.scnt = res.get("scnt") or self.account.scnt
            self.account.api_key = res.get("api_key") or self.account.api_key
            self.account.apple_cooldown_until = 0
            self.account.apple_auth_expired = False
            self.account.status = "active"
            self.account.last_error = ""
            from core.db import db
            db.save_account(self.account)
            db.set_apple_auth_expired(self.account.id, False)
            db.update_account_status(self.account.id, "active", "")
            logger.success(f"账号 [{self.account.name}] Apple Account 凭证已全自动无感重新就绪！", self.account.id)
            return True
        except Exception as e:
            logger.warning(f"账号 [{self.account.name}] 自动化凭证重新采集失败: {e}", self.account.id)
            return False

    def generate_candidate(self) -> str:
        """Step 1: POST /account/manage/email/private/add -> email"""
        last_err = None
        for attempt in range(2):
            try:
                resp = self._request("POST", "/account/manage/email/private/add", payload={})
                email = resp.get("emailAddress") or resp.get("hme")
                if not email and isinstance(resp.get("result"), dict):
                    email = resp["result"].get("emailAddress") or resp["result"].get("hme")
                if email:
                    return email
            except RateLimitError:
                raise
            except AuthExpiredError as e:
                last_err = e
                if attempt == 0:
                    logger.info(f"账号 [{self.account.name}] 触发会话保鲜，正在调用动态续期或全自动重采...", self.account.id)
                    if self.keepalive() or self._auto_reacquire():
                        continue
                break
            except NetworkError as e:
                last_err = e
                if attempt == 0:
                    logger.warning(f"账号 [{self.account.name}] 生成候选遇网络抖动，1秒后重试...", self.account.id)
                    time.sleep(1)
                    continue
                break
            except Exception as e:
                last_err = e
                break

        if last_err:
            raise last_err
        raise HmeClientError("Apple Account 未能返回有效候选邮箱")

    def complete_reserve(self, candidate_email: str, label: str = "", note: str = "") -> Dict[str, Any]:
        """Step 2: PUT /account/manage/email/private/add/complete"""
        if not label:
            label = f"Auto-{time.strftime('%m%d-%H%M%S')}"

        payload = {"emailAddress": candidate_email, "label": label, "note": note or ""}

        last_err = None
        for attempt in range(2):
            try:
                resp = self._request("PUT", "/account/manage/email/private/add/complete", payload=payload)
                email = resp.get("emailAddress") or resp.get("email") or candidate_email
                anon_id = resp.get("id") or resp.get("anonymous_id") or ""
                return {
                    "email": email,
                    "anonymous_id": anon_id,
                    "label": resp.get("label", label),
                    "note": resp.get("note", note),
                    "forward_to": resp.get("forwardToEmail") or resp.get("forward_to", ""),
                    "is_active": resp.get("active", True),
                }
            except RateLimitError:
                raise
            except AuthExpiredError as e:
                last_err = e
                if attempt == 0:
                    logger.info(f"账号 [{self.account.name}] 提交预留时凭据过期，尝试自动续期或重采...", self.account.id)
                    if self.keepalive() or self._auto_reacquire():
                        continue
                break
            except NetworkError as e:
                last_err = e
                if attempt == 0:
                    logger.warning(f"账号 [{self.account.name}] 生成候选遇网络抖动，1秒后重试...", self.account.id)
                    time.sleep(1)
                    continue
                break
            except Exception as e:
                last_err = e
                break

        if last_err:
            raise last_err
        raise HmeClientError("Apple Account 别名预留提交失败")

    def check_session(self) -> Dict[str, Any]:
        """Check if Apple Account session is actually valid against real endpoint."""
        if not self.account.cookie:
            raise AuthExpiredError("Apple Account Cookie 为空，需重新导入")

        try:
            # Perform a lightweight ping to verify actual management authentication
            resp = self._request("GET", "/account/manage/email/private", timeout=8)
            alias_list = resp.get("privateEmailList") or []
            alias_count = len(alias_list)
            is_max = resp.get("maxLimitReached", False)
            fwd = resp.get("forwardToEmailAddress") or self.account.selected_forward_to or "Apple ID 关联邮箱"

            if alias_count > 0:
                self.account.alias_count = alias_count
                from core.db import db
                db.save_account(self.account)

            return {
                "ok": True,
                "alias_count": alias_count,
                "max_limit_reached": is_max,
                "selected_forward_to": fwd,
            }
        except AuthExpiredError:
            raise
        except Exception as e:
            # If 404 or other non-auth, it may still be valid
            if "401" in str(e) or "403" in str(e):
                raise AuthExpiredError(f"Apple Account 凭证失效：{e}")
            return {
                "ok": True,
                "alias_count": self.account.alias_count,
                "selected_forward_to": self.account.selected_forward_to or "Apple ID 关联邮箱",
            }

    def list_aliases(self) -> List[Dict[str, Any]]:
        """Fetch all remote aliases belonging to this account on Apple Account management channel."""
        for attempt in range(2):
            try:
                resp = self._request("GET", "/account/manage/email/private", timeout=15)
                raw_list = resp.get("privateEmailList") or []
                parsed = []
                for item in raw_list:
                    parsed.append({
                        "email": item.get("emailAddress", ""),
                        "anonymous_id": item.get("id", ""),
                        "label": item.get("label", ""),
                        "note": item.get("note", ""),
                        "forward_to": item.get("forwardToEmail", ""),
                        "is_active": item.get("active", True),
                        "created_at": int(time.time()),
                    })
                logger.info(f"账号 [{self.account.name}] 成功通过 Apple Account 官方管理接口同步 {len(parsed)} 个别名", self.account.id)
                return parsed
            except AuthExpiredError as e:
                if attempt == 0 and self.account.can_reacquire_apple:
                    logger.info(f"账号 [{self.account.name}] 同步时凭证过期，正在自动重新认证换证...", self.account.id)
                    if self._auto_reacquire():
                        continue
                logger.warning(f"Apple Account 列表拉取凭据失效: {e}", self.account.id)
                raise
            except Exception as e:
                if ("401" in str(e) or "403" in str(e)) and attempt == 0 and self.account.can_reacquire_apple:
                    logger.info(f"账号 [{self.account.name}] 同步时凭据过期 (HTTP 401/403)，正在自动重新认证换证...", self.account.id)
                    if self._auto_reacquire():
                        continue
                logger.warning(f"Apple Account 列表拉取异常: {e}", self.account.id)
                raise
        return []

    # Aliases for unified interface with scheduler
    def generate_alias(self) -> str:
        return self.generate_candidate()

    def reserve_alias(self, candidate_email: str, label: str = "", note: str = "") -> Dict[str, Any]:
        return self.complete_reserve(candidate_email, label=label, note=note)

    def create_alias(self, label: str = "", note: str = "") -> Dict[str, Any]:
        candidate = self.generate_candidate()
        time.sleep(0.5)
        return self.complete_reserve(candidate, label=label, note=note)
