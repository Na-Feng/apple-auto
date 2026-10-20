from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Callable, Dict, Optional

from core.logger import logger

ACQUIRE_CLI_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "acquire_cli")


def acquire_apple_account_session(
    email: str,
    password: str,
    code_url: str,
    proxy_url: str = "",
    on_progress: Optional[Callable[[str, str], None]] = None,
    timeout: int = 180,
) -> Dict[str, Any]:
    """
    Executes automated headless browser acquisition to fetch live Apple Account credentials.
    Returns:
        {
            "ok": True,
            "apple_account_cookie": "...",
            "scnt": "...",
            "session_token": "...",
            "api_key": "...",
            "user_agent": "...",
        }
    """
    if not os.path.isfile(ACQUIRE_CLI_PATH):
        raise RuntimeError(f"未找到自动化采集工具二进制文件：{ACQUIRE_CLI_PATH}")

    args = [ACQUIRE_CLI_PATH, email.strip(), password, code_url.strip()]
    if proxy_url:
        clean_proxy = proxy_url.strip()
        if "@" in clean_proxy:
            import urllib.parse
            parsed = urllib.parse.urlsplit(clean_proxy)
            clean_proxy = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        args.append(clean_proxy)

    logger.info(f"启动自动化无痕浏览器获取 [{email}] 的 Apple Account 真实凭证...", email)

    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stdout_lines = []

    def _read_stderr():
        for line in process.stderr:
            line_str = line.strip()
            if not line_str:
                continue
            if line_str.startswith("[PROGRESS]"):
                parts = line_str[len("[PROGRESS]"):].strip().split(":", 1)
                step = parts[0].strip() if len(parts) > 0 else "progress"
                detail = parts[1].strip() if len(parts) > 1 else ""
                if on_progress:
                    try:
                        on_progress(step, detail)
                    except Exception:
                        pass
                logger.info(f"[{email}] Apple凭证采集: {step} - {detail}")

    import threading
    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stderr_thread.start()

    try:
        stdout_data, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        raise RuntimeError(f"自动化获取 Apple 凭证超时（限时 {timeout} 秒）")

    stderr_thread.join(timeout=2)

    for line in stdout_data.splitlines():
        line_str = line.strip()
        if line_str.startswith("{") and line_str.endswith("}"):
            try:
                res = json.loads(line_str)
                if res.get("ok"):
                    return res
                else:
                    raise RuntimeError(res.get("error") or "未知采集错误")
            except json.JSONDecodeError:
                continue

    raise RuntimeError(f"自动化工具未返回有效凭据结果：{stdout_data[:300]}")
