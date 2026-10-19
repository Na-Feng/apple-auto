from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.logger import logger
from core.models import AccountModel

# 2048-bit SRP Modulus for Apple SRP-6a
srpN_hex = (
    "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050"
    "A37329CBB4A099ED8193E0757767A13DD52312AB4B03310DCD7F48A9DA04FD50"
    "E8083969EDB767B0CF6095179A163AB3661A05FBD5FAAAE82918A9962F0B93B8"
    "55F97993EC975EEAA80D740ADBF4FF747359D041D5C33EA71D281E446B14773B"
    "CA97B43A23FB801676BD207A436C6481F1D2B9078717461A5B9D32E688F87748"
    "544523B524B0D57D5EA77A2775D2ECFA032CFBDBF52FB3786160279004E57AE"
    "6AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DBFB"
    "B694B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F9E4AFF73"
)
srpN = int(srpN_hex, 16)
srpG = 2
srpNLenBytes = 256

def srpPadInt(n: int) -> bytes:
    b = n.to_bytes((n.bit_length() + 7) // 8 or 1, byteorder="big")
    if len(b) >= srpNLenBytes:
        return b
    return b.rjust(srpNLenBytes, b"\x00")

n_bytes = srpN.to_bytes(srpNLenBytes, byteorder="big")
g_bytes = srpPadInt(srpG)
srpK = int.from_bytes(hashlib.sha256(n_bytes + g_bytes).digest(), byteorder="big")

def deriveApplePassword(password: str, salt: bytes, iterations: int, protocol: str) -> bytes:
    pass_hash = hashlib.sha256(password.encode("utf-8")).digest()
    key_input = pass_hash if protocol == "s2k" else pass_hash.hex().encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", key_input, salt, iterations, 32)


def parse_credential_line(line: str) -> Optional[Tuple[str, str, str]]:
    """
    Parse a credential line formatted as:
    apple_id----password----code_url
    Supports separators: '----', '---', '|', ',', '\t', whitespace.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    delimiters = ["----", "---", "|", ",", "\t"]
    for delim in delimiters:
        if delim in line:
            parts = [p.strip() for p in line.split(delim)]
            if len(parts) >= 2:
                apple_id = parts[0]
                password = parts[1]
                code_url = parts[2] if len(parts) > 2 else ""
                if "@" in apple_id and password:
                    return apple_id, password, code_url

    # Whitespace fallback
    parts = line.split()
    if len(parts) >= 2 and "@" in parts[0]:
        return parts[0], parts[1], parts[2] if len(parts) > 2 else ""

    return None


class AppleProtocolLogin:
    """
    Automated Apple Protocol Login client.
    Handles SRP-6a authentication, device 2FA push, HTTP polling for 2FA verification codes,
    trust token issuance, and iCloud WebServices session establishment.
    """

    def __init__(
        self,
        apple_id: str,
        client_id: str = "d39ba9916b7251055b22c7f910e2ea796ee65e98b2ddecea8f5dde8d9d1a815d",
    ):
        self.apple_id = apple_id.strip().lower()
        self.client_id = client_id
        self.frame_id = f"auth-{secrets.token_hex(16)}"
        self.user_agent = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3.1 Safari/605.1.15"
        )
        self.cookies: Dict[str, str] = {}
        self.scnt = ""
        self.session_id = ""
        self.session_token = ""
        self.trust_token = ""
        self.account_country = "USA"
        self.auth_attributes = ""
        self.auth_host = "idmsa.apple.com"
        self.setup_host = "setup.icloud.com"
        self.home_url = "https://www.icloud.com"

        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def update_from_headers(self, headers_list):
        for header, val in headers_list:
            h_lower = header.lower()
            if h_lower == "set-cookie":
                parts = val.split(";")[0].strip()
                if "=" in parts:
                    k, v = parts.split("=", 1)
                    k_str = k.strip()
                    v_str = v.strip()
                    self.cookies[k_str] = v_str
                    if k_str == "X-APPLE-DS-WEB-SESSION-TOKEN":
                        self.session_token = v_str
                    elif k_str.startswith("DES") or k_str == "X-APPLE-WEBAUTH-HSA-TRUST":
                        self.trust_token = v_str
            elif h_lower == "scnt":
                self.scnt = val.strip()
            elif h_lower == "x-apple-id-session-id":
                self.session_id = val.strip()
            elif h_lower == "x-apple-session-token":
                self.session_token = val.strip()
            elif h_lower == "x-apple-twosv-trust-token":
                self.trust_token = val.strip()
            elif h_lower == "x-apple-id-account-country":
                self.account_country = val.strip()
            elif h_lower == "x-apple-auth-attributes":
                self.auth_attributes = val.strip()

    def get_cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def get_srp_headers(self) -> Dict[str, str]:
        origin = f"https://{self.auth_host}"
        headers = {
            "Accept": "application/json, text/javascript",
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": f"{origin}/",
            "User-Agent": self.user_agent,
            "X-Apple-Widget-Key": self.client_id,
            "X-Apple-OAuth-Client-Id": self.client_id,
            "X-Apple-OAuth-Client-Type": "firstPartyAuth",
            "X-Apple-OAuth-Redirect-URI": self.home_url,
            "X-Apple-OAuth-Require-Grant-Code": "true",
            "X-Apple-OAuth-Response-Mode": "web_message",
            "X-Apple-OAuth-Response-Type": "code",
            "X-Apple-OAuth-State": self.frame_id,
            "X-Apple-Frame-Id": self.frame_id,
            "X-Requested-With": "XMLHttpRequest",
            "X-Apple-Mandate-Security-Upgrade": "0",
            "X-Apple-I-Require-UE": "true",
            "X-Apple-I-FD-Client-Info": f'{{"U":"{self.user_agent}","L":"zh-CN","Z":"GMT+08:00","V":"1.1","F":""}}',
        }
        if self.scnt:
            headers["scnt"] = self.scnt
        if self.session_id:
            headers["X-Apple-ID-Session-Id"] = self.session_id
        if self.session_token:
            headers["X-Apple-Session-Token"] = self.session_token
        if self.auth_attributes:
            headers["X-Apple-Auth-Attributes"] = self.auth_attributes
        if self.cookies:
            headers["Cookie"] = self.get_cookie_header()
        return headers

    def execute_login(
        self,
        password: str,
        code_url: str = "",
        on_step: Optional[Callable[[str, str], None]] = None,
        max_poll_seconds: int = 45,
    ) -> Dict[str, Any]:
        """
        Executes full protocol login sequence.
        on_step callback takes (step_title, detail).
        """
        def report(step: str, detail: str = ""):
            if on_step:
                try:
                    on_step(step, detail)
                except Exception:
                    pass
            logger.info(f"[{self.apple_id}] {step}: {detail}")

        report("Step 1/10", "发起 Apple 授权握手 (authStart)...")
        u1 = (
            f"https://{self.auth_host}/appleauth/auth/authorize/signin?"
            f"frame_id={self.frame_id}&skVersion=7&iframeId={self.frame_id}&"
            f"client_id={self.client_id}&redirect_uri={self.home_url}&"
            f"response_type=code&response_mode=web_message&state={self.frame_id}&"
            f"language=zh_CN&authVersion=latest"
        )
        req1 = urllib.request.Request(u1, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        with urllib.request.urlopen(req1, timeout=15, context=self.ctx) as resp:
            self.update_from_headers(resp.getheaders())

        report("Step 2/10", "预热账号识别 (authFederate)...")
        u2 = f"https://{self.auth_host}/appleauth/auth/federate?isRememberMeEnabled=true"
        b2 = json.dumps({"accountName": self.apple_id, "rememberMe": True}).encode("utf-8")
        req2 = urllib.request.Request(u2, data=b2, headers=self.get_srp_headers())
        with urllib.request.urlopen(req2, timeout=15, context=self.ctx) as resp:
            self.update_from_headers(resp.getheaders())

        report("Step 3/10", "启动 SRP-6a 安全协商 (signin/init)...")
        a = secrets.randbits(256)
        A = pow(srpG, a, srpN)
        A_b64 = base64.b64encode(srpPadInt(A)).decode()

        u3 = f"https://{self.auth_host}/appleauth/auth/signin/init"
        b3 = json.dumps({"a": A_b64, "accountName": self.apple_id, "protocols": ["s2k", "s2k_fo"]}).encode("utf-8")
        req3 = urllib.request.Request(u3, data=b3, headers=self.get_srp_headers())
        with urllib.request.urlopen(req3, timeout=15, context=self.ctx) as resp:
            self.update_from_headers(resp.getheaders())
            init_res = json.loads(resp.read().decode("utf-8"))

        salt = base64.b64decode(init_res["salt"])
        serverB = base64.b64decode(init_res["b"])
        B = int.from_bytes(serverB, byteorder="big")
        iteration = int(init_res["iteration"])
        protocol = init_res["protocol"]
        c_token = init_res["c"]

        derived = deriveApplePassword(password, salt, iteration, protocol)
        inner = hashlib.sha256(b":" + derived).digest()
        x = int.from_bytes(hashlib.sha256(salt + inner).digest(), byteorder="big")
        u = int.from_bytes(hashlib.sha256(srpPadInt(A) + srpPadInt(B)).digest(), byteorder="big")

        diff = (B - srpK * pow(srpG, x, srpN)) % srpN
        S = pow(diff, a + u * x, srpN)
        K = hashlib.sha256(srpPadInt(S)).digest()

        hg = hashlib.sha256(srpPadInt(srpG)).digest()
        hn = hashlib.sha256(n_bytes).digest()
        hxor = bytes(_x ^ _y for _x, _y in zip(hg, hn))

        h = hashlib.sha256()
        h.update(hxor)
        h.update(hashlib.sha256(self.apple_id.encode("utf-8")).digest())
        h.update(salt)
        h.update(srpPadInt(A))
        h.update(srpPadInt(B))
        h.update(K)
        M1 = h.digest()

        h2 = hashlib.sha256()
        h2.update(srpPadInt(A))
        h2.update(M1)
        h2.update(K)
        M2 = h2.digest()

        report("Step 4/10", "提交密码凭据验证 (signin/complete)...")
        u4 = f"https://{self.auth_host}/appleauth/auth/signin/complete?isRememberMeEnabled=true"
        b4 = json.dumps({
            "accountName": self.apple_id,
            "m1": base64.b64encode(M1).decode(),
            "m2": base64.b64encode(M2).decode(),
            "c": c_token,
            "rememberMe": True,
            "trustTokens": [],
        }).encode("utf-8")

        req4 = urllib.request.Request(u4, data=b4, headers=self.get_srp_headers())

        needs_2fa = False
        try:
            with urllib.request.urlopen(req4, timeout=15, context=self.ctx) as resp:
                self.update_from_headers(resp.getheaders())
                report("Step 4/10", f"密码验证通过，无需 2FA (HTTP {resp.status})")
        except urllib.error.HTTPError as e:
            self.update_from_headers(e.headers.items())
            if e.code == 409:
                needs_2fa = True
                report("Step 4/10", "Apple 触发 2FA 双重认证挑战 (HTTP 409)")
            elif e.code == 401:
                raise RuntimeError("Apple ID 或密码错误，请核对登录凭证")
            elif e.code == 403:
                raise RuntimeError("Apple 账号被风控或限制登录 (HTTP 403)")
            else:
                err_text = e.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Apple 密码验证异常 HTTP {e.code}: {err_text[:200]}")

        if needs_2fa:
            # Check old code on url before requesting new code
            initial_code = ""
            if code_url:
                try:
                    c_req = urllib.request.Request(code_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(c_req, timeout=5, context=self.ctx) as c_resp:
                        body = c_resp.read().decode("utf-8", errors="replace")
                        m = re.search(r"\b(\d{6})\b", body)
                        if m:
                            initial_code = m.group(1)
                            report("Step 5/10", f"取码接口当前旧码: {initial_code}")
                except Exception:
                    pass

            report("Step 5/10", "请求受信任设备发送 2FA 验证码...")
            u5 = f"https://{self.auth_host}/appleauth/auth/verify/trusteddevice/securitycode"
            req5 = urllib.request.Request(u5, headers=self.get_srp_headers(), method="PUT")
            try:
                with urllib.request.urlopen(req5, timeout=15, context=self.ctx) as resp:
                    self.update_from_headers(resp.getheaders())
                    report("Step 5/10", f"已向受信任设备请求验证码 (HTTP {resp.status})")
            except urllib.error.HTTPError as e:
                self.update_from_headers(e.headers.items())

            extracted_code = ""
            if code_url:
                report("Step 6/10", f"轮询取码接口等待全新 6 位验证码 (超时 {max_poll_seconds} 秒)...")
                start_t = time.time()
                while time.time() - start_t < max_poll_seconds:
                    try:
                        c_req = urllib.request.Request(code_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(c_req, timeout=8, context=self.ctx) as c_resp:
                            body = c_resp.read().decode("utf-8", errors="replace")
                            m = re.search(r"\b(\d{6})\b", body)
                            if m:
                                current_code = m.group(1)
                                if initial_code and current_code != initial_code:
                                    extracted_code = current_code
                                    report("Step 6/10", f"成功捕获全新验证码: {extracted_code}")
                                    break
                                elif not initial_code:
                                    extracted_code = current_code
                                    report("Step 6/10", f"成功捕获验证码: {extracted_code}")
                                    break
                    except Exception:
                        pass
                    time.sleep(2)

                if not extracted_code and initial_code:
                    report("Step 6/10", f"超时未更新，尝试使用现有验证码: {initial_code}")
                    extracted_code = initial_code

            if not extracted_code:
                raise RuntimeError(f"未能从取码接口自动拉取到 6 位验证码 (URL: {code_url})")

            report("Step 7/10", f"向 Apple 提交验证码 {extracted_code}...")
            submit_url = f"https://{self.auth_host}/appleauth/auth/verify/trusteddevice/securitycode"
            submit_body = json.dumps({"securityCode": {"code": extracted_code}}).encode("utf-8")
            req7 = urllib.request.Request(submit_url, data=submit_body, headers=self.get_srp_headers())
            try:
                with urllib.request.urlopen(req7, timeout=15, context=self.ctx) as resp:
                    self.update_from_headers(resp.getheaders())
                    report("Step 7/10", f"2FA 设备二次验证通过！(HTTP {resp.status})")
            except urllib.error.HTTPError as e:
                self.update_from_headers(e.headers.items())
                report("Step 7/10", f"设备通道返回 HTTP {e.code}，尝试受信任手机短信通道...")
                submit_url_phone = f"https://{self.auth_host}/appleauth/auth/verify/phone/securitycode"
                req7_phone = urllib.request.Request(
                    submit_url_phone,
                    data=json.dumps({"securityCode": {"code": extracted_code}, "mode": "sms", "phoneNumber": {"id": 1}}).encode(),
                    headers=self.get_srp_headers(),
                )
                try:
                    with urllib.request.urlopen(req7_phone, timeout=15, context=self.ctx) as p_resp:
                        self.update_from_headers(p_resp.getheaders())
                        report("Step 7/10", f"短信 2FA 验证成功！(HTTP {p_resp.status})")
                except urllib.error.HTTPError as pe:
                    self.update_from_headers(pe.headers.items())
                    err_text = pe.read().decode("utf-8", errors="replace")
                    raise RuntimeError(f"2FA 验证码提交失败 (HTTP {pe.code}): {err_text[:200]}")

            report("Step 8/10", "建立设备长期信任 (2sv/trust)...")
            u8 = f"https://{self.auth_host}/appleauth/auth/2sv/trust"
            req8 = urllib.request.Request(u8, headers=self.get_srp_headers())
            try:
                with urllib.request.urlopen(req8, timeout=15, context=self.ctx) as resp:
                    self.update_from_headers(resp.getheaders())
                    report("Step 8/10", f"信任建立成功 (HTTP {resp.status})")
            except Exception as e:
                report("Step 8/10", f"2sv/trust 响应: {e}")

        session_token = self.session_token or self.cookies.get("X-APPLE-DS-WEB-SESSION-TOKEN") or self.cookies.get("aasp", "")
        trust_token = self.trust_token
        if not trust_token:
            for k, v in self.cookies.items():
                if k.startswith("DES") or k == "X-APPLE-WEBAUTH-HSA-TRUST":
                    trust_token = v
                    break

        report("Step 9/10", "换取 iCloud WebServices 完整会话 (accountLogin)...")
        u9 = f"https://{self.setup_host}/setup/ws/1/accountLogin"
        b9 = json.dumps({
            "apple_id": self.apple_id,
            "accountCountryCode": self.account_country or "USA",
            "dsWebAuthToken": session_token,
            "extended_login": True,
            "trustToken": trust_token,
        }).encode("utf-8")
        req9 = urllib.request.Request(u9, data=b9, headers={
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": self.home_url,
            "Referer": f"{self.home_url}/",
            "Cookie": self.get_cookie_header(),
        })
        try:
            with urllib.request.urlopen(req9, timeout=15, context=self.ctx) as resp:
                self.update_from_headers(resp.getheaders())
                login_res = json.loads(resp.read().decode("utf-8"))
                dsid = login_res.get("dsInfo", {}).get("dsid")
                report("Step 9/10", f"accountLogin 成功！DSID: {dsid}")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"accountLogin 换取会话失败 (HTTP {e.code}): {err_body[:200]}")

        report("Step 10/10", "校验会话并解析 HME 别名服务端点 (validate)...")
        client_uuid = secrets.token_hex(16)
        req_uuid = secrets.token_hex(16)
        u10 = (
            f"https://{self.setup_host}/setup/ws/1/validate?"
            f"clientBuildNumber=2622Build20&clientMasteringNumber=2622Build20&"
            f"clientId={client_uuid}&requestId={req_uuid}"
        )
        req10 = urllib.request.Request(u10, data=b"", headers={
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Content-Type": "text/plain;charset=UTF-8",
            "Origin": self.home_url,
            "Referer": f"{self.home_url}/",
            "Cookie": self.get_cookie_header(),
        })
        with urllib.request.urlopen(req10, timeout=15, context=self.ctx) as resp:
            self.update_from_headers(resp.getheaders())
            val_info = json.loads(resp.read().decode("utf-8"))

        ds_info = val_info.get("dsInfo", {})
        webservices = val_info.get("webservices", {})
        premium_url = webservices.get("premiummailsettings", {}).get("url", "")
        parsed_host = urllib.parse.urlparse(premium_url).netloc or "p68-maildomainws.icloud.com"

        report("完成", f"协议登录成功！DSID: {ds_info.get('dsid')}, Host: {parsed_host}")

        return {
            "apple_id": ds_info.get("appleId", self.apple_id),
            "dsid": str(ds_info.get("dsid", "")),
            "host": parsed_host,
            "client_id": self.client_id,
            "client_build_number": "2622Build20",
            "client_mastering_number": "2622Build20",
            "cookie": self.get_cookie_header(),
            "origin": self.home_url,
            "referer": f"{self.home_url}/",
            "user_agent": self.user_agent,
            "is_icloud_plus": ds_info.get("isHideMyEmailSubscriptionActive", False),
            "can_create_hme": ds_info.get("isHideMyEmailFeatureAvailable", False),
            "selected_forward_to": ds_info.get("primaryEmail", ""),
        }
