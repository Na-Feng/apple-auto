from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import SERVER_HOST, SERVER_PORT
from core.db import db
from core.logger import logger
from server.app import run_server


def print_banner(host: str, port: int) -> None:
    banner = f"""
========================================================================
   _ _____ _                 _      _    _ _             
  (_) ____| |               | |    / \\  | (_)            
   _| |   | | ___  _   _  __| |   / _ \\ | |_  __ _ ___   
  | | |   | |/ _ \\| | | |/ _` |  / ___ \\| | |/ _` / __|  
  | | |___| | (_) | |_| | (_| | / /   \\ \\ | | (_| \\__ \\  
  |_|\\____|_|\\___/ \\__,_|\\__,_|/_/     \\_\\|_|_\\__,_|___/  
                                                         
  iCloud 隐私别名邮箱多账号智能自动化管理平台
  Multi-Account Automated Hide My Email Management Platform
========================================================================
  [+] 数据库状态: 已就绪 (SQLite WAL 模式)
  [+] 服务监听地址: http://{host}:{port}
  [+] 本地控制台:   http://localhost:{port}
========================================================================
"""
    print(banner)


def main():
    parser = argparse.ArgumentParser(description="iCloud 别名邮箱多账号自动管理平台")
    parser.add_argument("--host", default=SERVER_HOST, help="HTTP 监听 Host (默认 0.0.0.0)")
    parser.add_argument("--port", type=int, default=SERVER_PORT, help="HTTP 监听端口 (默认 8080)")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开默认浏览器")

    args = parser.parse_args()

    # Pre-init DB
    stats = db.get_summary_stats()
    print_banner(args.host, args.port)
    logger.info(f"系统当前收录: {stats['total_accounts']} 个 iCloud 账号, {stats['total_aliases']} 个本地别名记录")

    # Optionally open browser
    if not args.no_browser and args.host in ("0.0.0.0", "127.0.0.1", "localhost"):
        try:
            target_url = f"http://localhost:{args.port}"
            import threading
            def _open():
                import time
                time.sleep(1)
                webbrowser.open(target_url)
            threading.Thread(target=_open, daemon=True).start()
        except Exception:
            pass

    # Start server
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
