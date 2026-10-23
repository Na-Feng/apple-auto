from __future__ import annotations

import json
import mimetypes
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

from core.logger import logger
from server.api_handlers import ApiHandler

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class RequestHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler that routes API requests and serves static web UI."""

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")

    def _dispatch(self, method: str) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        # 1. API routes
        if path.startswith("/api/"):
            body_json = None
            if method in ("POST", "PUT"):
                content_len = int(self.headers.get("Content-Length", 0))
                if content_len > 0:
                    raw_body = self.rfile.read(content_len).decode("utf-8", errors="replace")
                    try:
                        body_json = json.loads(raw_body)
                    except Exception:
                        body_json = {"raw": raw_body}

            status, resp_data, headers = ApiHandler.handle_request(method, path, query, body_json)
            self.send_response(status)
            self._send_cors_headers()
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()

            if isinstance(resp_data, (dict, list)):
                out = json.dumps(resp_data, ensure_ascii=False)
                self.wfile.write(out.encode("utf-8"))
            elif isinstance(resp_data, str):
                self.wfile.write(resp_data.encode("utf-8"))
            return

        # 2. Static Web Assets
        self._serve_static(path)

    def _serve_static(self, path: str) -> None:
        if path in ("", "/"):
            file_name = "index.html"
        else:
            file_name = path.lstrip("/")

        target_file = (WEB_DIR / file_name).resolve()

        # Security check: prevent directory traversal
        if not str(target_file).startswith(str(WEB_DIR)):
            self.send_error(403, "Access Denied")
            return

        if not target_file.is_file():
            # Fallback to index.html for SPA routing
            target_file = WEB_DIR / "index.html"

        if not target_file.is_file():
            self.send_error(404, "Web File Not Found")
            return

        mime_type, _ = mimetypes.guess_type(str(target_file))
        if not mime_type:
            mime_type = "application/octet-stream"

        try:
            with open(target_file, "rb") as f:
                content = f.read()

            self.send_response(200)
            self.send_header("Content-Type", f"{mime_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, f"Error reading static file: {e}")

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress built-in noisy HTTP access logs
        pass


def _start_keepalive_worker() -> None:
    """Background daemon thread that periodically renews active Apple Account sessions every 180 seconds."""
    import time
    import threading
    from core.db import db
    from core.client_apple_account import AppleAccountClient

    def _loop():
        time.sleep(10)
        while True:
            try:
                accounts = db.get_all_accounts()
                for acc in accounts:
                    if acc.has_apple_account and acc.status in ("active", "cooling_down") and acc.autopilot_status != "completed":
                        try:
                            client = AppleAccountClient(acc)
                            client.keepalive()
                        except Exception as e:
                            logger.warning(f"账号 [{acc.name}] 周期保活心跳异常: {e}", acc.id)

                # Ensure Resin subscriptions maintain healthy nodes without ever deleting jittery nodes
                try:
                    from core.resin_manager import resin_manager
                    resin_manager.sync_subscriptions_if_empty()
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"保活轮询工作线程异常: {e}")
            time.sleep(180)

    t = threading.Thread(target=_loop, daemon=True, name="apple-keepalive-worker")
    t.start()
    logger.info("Apple Account 动态保活守护进程已就绪 (心跳周期: 180 秒)")


def run_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, RequestHandler)
    display_host = "localhost" if host == "0.0.0.0" else host
    logger.info(f"============================================================")
    logger.info(f"iCloud 别名管理平台已启动！")
    logger.info(f"本地控制台访问: http://{display_host}:{port}")
    logger.info(f"局域网访问:     http://0.0.0.0:{port}")
    logger.info(f"============================================================")

    # Launch background keepalive worker for Apple Account channel
    _start_keepalive_worker()

    # Automatically start embedded Resin Proxy Gateway if present
    try:
        from core.resin_manager import resin_manager
        if resin_manager.is_installed():
            resin_manager.start()
            from core.db import db
            for sub in db.get_all_subscriptions():
                resin_manager.add_subscription(sub["name"], sub["url"])
    except Exception as e:
        logger.warning(f"Resin 网关自启动与订阅恢复异常: {e}")

    # Automatically resume any ongoing autopilot task if server restarted
    try:
        from core.scheduler import scheduler, account_autopilot
        scheduler.resume_active_job_if_any()
        account_autopilot.resume_all_active()
    except Exception as e:
        logger.warning(f"自动恢复活跃任务检查异常: {e}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务接收到停止信号，正在平稳退出...")
    finally:
        httpd.server_close()
        logger.info("服务已停止。")
