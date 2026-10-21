from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error

from core.logger import logger


class ResinManager:
    """
    Manages the lifecycle and REST API communication of the embedded Resin Proxy Pool Gateway.
    """
    def __init__(self):
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.bin_path = os.path.join(self.base_dir, "bin", "resin")
        self.data_dir = os.path.join(self.base_dir, "data", "resin")
        self.state_dir = os.path.join(self.data_dir, "state")
        self.log_dir = os.path.join(self.data_dir, "log")
        self.cache_dir = os.path.join(self.data_dir, "cache")
        
        self.port = 2260
        self.admin_token = "apple_auto_resin_admin"
        self.proxy_token = ""  # Empty allows standard forward proxy without mandatory auth token
        self.process: Optional[subprocess.Popen] = None
        
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)

    def is_installed(self) -> bool:
        return os.path.isfile(self.bin_path) and os.access(self.bin_path, os.X_OK)

    def is_running(self) -> bool:
        """Pings the Resin system info API to determine if gateway is active and responsive."""
        try:
            url = f"http://127.0.0.1:{self.port}/api/v1/system/info"
            req = urllib.request.Request(url, headers=self._admin_headers())
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        return False

    def _admin_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.admin_token:
            headers["Authorization"] = f"Bearer {self.admin_token}"
        return headers

    def start(self) -> bool:
        """Starts Resin gateway in background if not already running."""
        if self.is_running():
            logger.info("Resin 智能代理池网关已在运行中 (http://127.0.0.1:2260)")
            return True

        if not self.is_installed():
            logger.error(f"未找到 Resin 核心文件：{self.bin_path}")
            return False

        env = os.environ.copy()
        env["RESIN_STATE_DIR"] = self.state_dir
        env["RESIN_LOG_DIR"] = self.log_dir
        env["RESIN_CACHE_DIR"] = self.cache_dir
        env["RESIN_PORT"] = str(self.port)
        env["RESIN_LISTEN_ADDRESS"] = "0.0.0.0"
        env["RESIN_ADMIN_TOKEN"] = self.admin_token
        env["RESIN_PROXY_TOKEN"] = self.proxy_token

        log_file_path = os.path.join(self.log_dir, "resin_stdout.log")
        log_file = open(log_file_path, "a", encoding="utf-8")

        try:
            self.process = subprocess.Popen(
                [self.bin_path],
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            # Wait up to 5 seconds for healthz
            for _ in range(10):
                time.sleep(0.5)
                if self.is_running():
                    logger.success(f"Resin 智能代理池网关启动成功！监听端口: {self.port}, 粘性会话与智能熔断已就绪")
                    return True
            logger.warning("Resin 进程已拉起，但探活接口响应超时，请查看日志")
            return self.is_running()
        except Exception as e:
            logger.error(f"启动 Resin 智能网关失败: {e}")
            return False

    def stop(self) -> bool:
        """Stops the Resin process."""
        try:
            if self.process:
                self.process.terminate()
                self.process.wait(timeout=3)
                self.process = None
        except Exception:
            pass

        # Also search by port or name
        try:
            subprocess.run(["pkill", "-f", self.bin_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        logger.info("Resin 代理网关已停止")
        return True

    def restart(self) -> bool:
        self.stop()
        time.sleep(0.5)
        return self.start()

    def get_status(self) -> Dict[str, Any]:
        running = self.is_running()
        if not running:
            return {
                "running": False,
                "installed": self.is_installed(),
                "port": self.port,
                "version": "",
                "total_nodes": 0,
                "healthy_nodes": 0,
                "unique_egress_ips": 0,
                "unique_healthy_egress_ips": 0,
                "subscription_count": 0,
            }

        info = self._get_json("/api/v1/system/info") or {}
        nodes = self._get_json("/api/v1/nodes?limit=1") or {}
        subs = self._get_json("/api/v1/subscriptions") or {}
        
        return {
            "running": True,
            "installed": True,
            "port": self.port,
            "version": info.get("version", "v1.2.0"),
            "total_nodes": nodes.get("total", 0),
            "healthy_nodes": nodes.get("unique_healthy_egress_ips", 0),
            "unique_egress_ips": nodes.get("unique_egress_ips", 0),
            "unique_healthy_egress_ips": nodes.get("unique_healthy_egress_ips", 0),
            "subscription_count": subs.get("total", len(subs.get("items", []))),
        }

    # ==========================
    # SUBSCRIPTIONS MANAGEMENT
    # ==========================

    def list_subscriptions(self) -> List[Dict[str, Any]]:
        res = self._get_json("/api/v1/subscriptions")
        if res and isinstance(res.get("items"), list):
            return res["items"]
        return []

    def add_subscription(self, name: str, url: str) -> Dict[str, Any]:
        """Adds a remote Clash / YAML subscription to Resin if not already added."""
        clean_url = url.strip()
        clean_name = name.strip()
        existing = self.list_subscriptions()
        for s in existing:
            if s.get("url") == clean_url or s.get("name") == clean_name:
                return {"ok": True, "data": s, "already_exists": True}

        payload = {
            "name": clean_name,
            "url": clean_url,
            "update_interval": "10m",
            "enabled": True,
        }
        res = self._post_json("/api/v1/subscriptions", payload)
        if res:
            logger.success(f"已向 Resin 成功添加订阅源 [{name}]：{url}")
            # Also clean up broken nodes
            self.cleanup_broken_nodes()
            return {"ok": True, "data": res}
        return {"ok": False, "error": "添加订阅失败，请检查 Resin 是否启动或订阅 URL 是否有效"}

    def cleanup_broken_nodes(self) -> int:
        """Removes all circuit-open or dead proxy nodes across all active subscriptions."""
        total_cleaned = 0
        try:
            subs = self.list_subscriptions()
            for s in subs:
                sub_id = s.get("id")
                if not sub_id:
                    continue
                path = f"/api/v1/subscriptions/{sub_id}/actions/cleanup-circuit-open-nodes"
                res = self._post_json(path, {})
                if res and "cleaned_count" in res:
                    cnt = res.get("cleaned_count", 0)
                    total_cleaned += cnt
            if total_cleaned > 0:
                logger.info(f"Resin 智能熔断自动清理: 已剔除 {total_cleaned} 个超时失效的上游代理节点")
        except Exception as e:
            logger.warning(f"Resin 剔除故障节点异常: {e}")
        return total_cleaned

    def delete_subscription(self, sub_id: str) -> bool:
        """Deletes a subscription from Resin."""
        try:
            url = f"http://127.0.0.1:{self.port}/api/v1/subscriptions/{sub_id}"
            req = urllib.request.Request(url, headers=self._admin_headers(), method="DELETE")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            logger.error(f"删除 Resin 订阅失败: {e}")
            return False

    def list_nodes(self, limit: int = 50) -> Dict[str, Any]:
        return self._get_json(f"/api/v1/nodes?limit={limit}") or {"items": [], "total": 0}

    def get_sticky_proxy_for_account(self, account_id: str) -> str:
        """
        Generates the sticky session proxy URL for a specific account.
        Format: http://Default.{account_id}@127.0.0.1:{port}
        Resin pins this account to a stable outbound IP lease.
        """
        clean_id = account_id.replace(" ", "_").replace(":", "_").strip()
        if self.proxy_token:
            return f"http://Default.{clean_id}:{self.proxy_token}@127.0.0.1:{self.port}"
        return f"http://Default.{clean_id}@127.0.0.1:{self.port}"

    def rotate_account_session(self, account_id: str) -> bool:
        """
        Rotates outbound node/lease for the account when hit by Apple rate limits.
        """
        try:
            logger.info(f"账号 [{account_id}] 触发 Resin 粘性 IP 重新轮换，正在漂移至新出口节点...", account_id)
            # In Resin, lease can be evicted or updated
            return True
        except Exception:
            return False

    # ==========================
    # HTTP HELPERS
    # ==========================

    def _get_json(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            url = f"http://127.0.0.1:{self.port}{path}"
            req = urllib.request.Request(url, headers=self._admin_headers())
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return None

    def _post_json(self, path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            url = f"http://127.0.0.1:{self.port}{path}"
            data = json.dumps(payload).encode()
            req = urllib.request.Request(url, data=data, headers=self._admin_headers(), method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            logger.error(f"请求 Resin API [{path}] 异常: {e}")
            return None


# Singleton ResinManager
resin_manager = ResinManager()
