from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Dict, Optional, Tuple

CORE_SESSION_COOKIE_NAMES = (
    "X-APPLE-DS-WEB-SESSION-TOKEN",
    "X-APPLE-WEBAUTH-USER",
    "X-APPLE-WEBAUTH-TOKEN",
)

MAILDOMAINWS_HOST_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?-maildomainws\.icloud\.com(?:\.cn)?",
    re.ASCII,
)


class SessionParseError(Exception):
    """Raised when cURL, HAR or cookie import parsing fails."""
    pass


def parse_curl_command(curl_text: str) -> Dict[str, Any]:
    """Parse a cURL command copied from browser DevTools into structured iCloud session config."""
    text = curl_text.strip()
    if not text:
        raise SessionParseError("cURL 内容为空，请先在浏览器控制台复制 cURL (bash)")

    # 1. Extract URL
    url = _extract_url(text)
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()

    # Determine channel
    if "account.apple.com" in host or "appleid.apple.com" in host:
        return _parse_apple_account_curl(text, url, parsed, host)

    if not ("maildomainws.icloud.com" in host or "setup.icloud.com" in host or "icloud.com" in host):
        raise SessionParseError(
            f"URL 域名不是有效的 iCloud 域名（{host}）。"
            "请在 iCloud+ 网页中打开“隐藏我的电子邮件”页面，并在 Network 中找到包含 list 或 hme 的请求复制 cURL。"
        )

    # 2. Extract Query Parameters
    query = urllib.parse.parse_qs(parsed.query)
    dsid = _get_query_param(query, "dsid")
    client_id = _get_query_param(query, "clientId", default="d39ba9916b7251055b22c7f910e2ea796ee65e98b2ddecea8f5dde8d9d1a815d")
    client_build_number = _get_query_param(query, "clientBuildNumber", default="2622Build20")
    client_mastering_number = _get_query_param(query, "clientMasteringNumber", default="2622Build20")

    if not dsid:
        raise SessionParseError("cURL URL 中缺少必需的 dsid 参数，请确认复制的是 /v2/hme/list 请求")

    # 3. Extract Cookie
    cookie = _extract_cookie(text)
    if not cookie:
        raise SessionParseError("cURL 中未找到 Cookie，请确认复制完整命令并包含 -b 或 -H 'Cookie: ...'")

    # Validate essential session cookies
    missing_cookies = [c for c in CORE_SESSION_COOKIE_NAMES if c not in cookie]
    if missing_cookies:
        # Give warning or check if at least one token exists
        pass

    # 4. Regional defaults
    is_cn = host.endswith(".cn")
    lang_code = "zh-cn" if is_cn else "zh-tw"
    origin = "https://www.icloud.com.cn" if is_cn else "https://www.icloud.com"
    referer = f"{origin}/"

    # 5. Extract optional headers
    origin_header = _extract_header(text, "Origin") or origin
    referer_header = _extract_header(text, "Referer") or referer
    user_agent = _extract_header(text, "User-Agent") or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    )

    return {
        "channel": "icloud_web",
        "host": host,
        "dsid": dsid,
        "client_id": client_id,
        "client_build_number": client_build_number,
        "client_mastering_number": client_mastering_number,
        "cookie": cookie,
        "origin": origin_header,
        "referer": referer_header,
        "user_agent": user_agent,
        "lang_code": lang_code,
    }


def _parse_apple_account_curl(text: str, url: str, parsed: urllib.parse.ParseResult, host: str) -> Dict[str, Any]:
    cookie = _extract_cookie(text)
    scnt = _extract_header(text, "scnt") or ""
    api_key = _extract_header(text, "X-Apple-Api-Key") or ""
    user_agent = _extract_header(text, "User-Agent") or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/149.0.0.0 Safari/537.36"
    )

    return {
        "channel": "apple_account",
        "host": host,
        "dsid": "apple_account",
        "client_id": "af1139274f266b22b68c2a3e7ad932cb3c0bbe854e13a79af78dcc73136882c3",
        "client_build_number": "latest",
        "client_mastering_number": "latest",
        "cookie": cookie,
        "scnt": scnt,
        "api_key": api_key,
        "origin": "https://account.apple.com",
        "referer": "https://account.apple.com/",
        "user_agent": user_agent,
        "lang_code": "zh-cn",
    }


def parse_har_data(data: dict) -> Dict[str, Any]:
    entries = data.get("log", {}).get("entries", [])
    if not isinstance(entries, list):
        raise SessionParseError("HAR 的 log.entries 格式异常")

    for entry in entries:
        req = entry.get("request", {})
        url = req.get("url", "")
        if "maildomainws.icloud.com" in url and ("/v2/hme/list" in url or "/v1/hme/" in url):
            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(parsed.query)
            
            headers = {h.get("name", "").lower(): h.get("value", "") for h in req.get("headers", [])}
            cookie_str = headers.get("cookie", "")
            if not cookie_str and "cookies" in req:
                cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in req["cookies"])

            is_cn = parsed.netloc.lower().endswith(".cn")
            return {
                "channel": "icloud_web",
                "host": parsed.netloc.lower(),
                "dsid": _get_query_param(query, "dsid"),
                "client_id": _get_query_param(query, "clientId", default="d39ba9916b7251055b22c7f910e2ea796ee65e98b2ddecea8f5dde8d9d1a815d"),
                "client_build_number": _get_query_param(query, "clientBuildNumber", default="2622Build20"),
                "client_mastering_number": _get_query_param(query, "clientMasteringNumber", default="2622Build20"),
                "cookie": cookie_str,
                "origin": headers.get("origin", "https://www.icloud.com.cn" if is_cn else "https://www.icloud.com"),
                "referer": headers.get("referer", "https://www.icloud.com.cn/" if is_cn else "https://www.icloud.com/"),
                "user_agent": headers.get("user-agent", ""),
                "lang_code": "zh-cn" if is_cn else "zh-tw",
            }

    raise SessionParseError("未在 HAR 中找到 iCloud 隐藏邮箱接口请求 (/v2/hme/list)")


def parse_any_session_input(raw_text: str) -> Dict[str, Any]:
    """Auto-detects format (cURL, HAR JSON, or direct JSON) and parses."""
    stripped = raw_text.strip()
    if not stripped:
        raise SessionParseError("输入内容为空")

    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
            if "log" in data:
                return parse_har_data(data)
            # Direct JSON config
            if "host" in data and "cookie" in data:
                return data
        except json.JSONDecodeError:
            pass

    return parse_curl_command(stripped)


# Internal helper functions
def _extract_url(curl: str) -> str:
    # Match curl 'https://...' or curl "https://..."
    match = re.search(r"""curl\s+(?:--location\s+)?['"](?P<url>https?://[^'"]+)['"]""", curl, re.IGNORECASE)
    if match:
        return match.group("url")
    match = re.search(r"""curl\s+(?:--location\s+)?(?P<url>https?://[^\s\\]+)""", curl, re.IGNORECASE)
    if match:
        return match.group("url")
    # Search for https:// within text
    match = re.search(r"""(?P<url>https?://[a-zA-Z0-9.-]+maildomainws\.icloud\.com(?:\.cn)?[^\s'"\\]*)""", curl, re.IGNORECASE)
    if match:
        return match.group("url")
    raise SessionParseError("无法从 cURL 文本中解析出目标 URL")


def _get_query_param(query_dict: Dict[str, list], param_name: str, default: str = "") -> str:
    val = query_dict.get(param_name)
    if val and len(val) > 0 and val[0].strip():
        return val[0].strip()
    return default


def _extract_cookie(curl: str) -> str:
    # Match -b '...' or --cookie '...'
    match = re.search(r"""(?:-b|--cookie)\s+['"](?P<cookie>[^'"]+)['"]""", curl)
    if match:
        return match.group("cookie").strip()
    # Match -H 'Cookie: ...'
    match = re.search(r"""-H\s+['"]Cookie:\s*(?P<cookie>[^'"]+)['"]""", curl, re.IGNORECASE)
    if match:
        return match.group("cookie").strip()
    # Match unquoted
    match = re.search(r"""-H\s+Cookie:\s*(?P<cookie>[^\r\n\\]+)""", curl, re.IGNORECASE)
    if match:
        return match.group("cookie").strip()
    return ""


def _extract_header(curl: str, header_name: str) -> Optional[str]:
    pattern = rf"""-H\s+['"]{re.escape(header_name)}:\s*(?P<val>[^'"]+)['"]"""
    match = re.search(pattern, curl, re.IGNORECASE)
    if match:
        return match.group("val").strip()
    return None
