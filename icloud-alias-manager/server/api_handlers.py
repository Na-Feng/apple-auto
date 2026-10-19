from __future__ import annotations

import csv
import io
import json
import time
import urllib.parse
import uuid
import threading
from typing import Any, Dict, List, Optional, Tuple

from core.client_apple_account import AppleAccountClient
from core.client_icloud_web import ICloudWebClient
from core.curl_parser import parse_any_session_input, SessionParseError
from core.db import db
from core.logger import logger
from core.models import AccountModel, AliasModel
from core.protocol_login import AppleProtocolLogin, parse_credential_line
from core.scheduler import scheduler, get_client

_protocol_jobs_lock = threading.Lock()
_protocol_jobs: Dict[str, Dict[str, Any]] = {}


class ApiHandler:
    """REST API business router and handler."""

    @staticmethod
    def handle_request(method: str, path: str, query: Dict[str, list], body_json: Optional[Dict[str, Any]]) -> Tuple[int, Any, Dict[str, str]]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        
        # Route matching
        try:
            # 1. Accounts
            if path == "/api/accounts" and method == "GET":
                accounts = [a.to_dict() for a in db.get_all_accounts()]
                return 200, {"ok": True, "data": accounts}, headers

            if path == "/api/accounts/parse" and method == "POST":
                raw_text = (body_json or {}).get("text", "")
                parsed = parse_any_session_input(raw_text)
                return 200, {"ok": True, "data": parsed}, headers

            if path == "/api/accounts/import" and method == "POST":
                return ApiHandler._import_account(body_json or {})

            if path == "/api/accounts/protocol-import" and method == "POST":
                return ApiHandler._start_protocol_import(body_json or {})

            if path == "/api/accounts/protocol-import/status" and method == "GET":
                job_id = query.get("job_id", [""])[0]
                return ApiHandler._get_protocol_import_status(job_id)

            if path.startswith("/api/accounts/") and path.endswith("/check") and method == "POST":
                acc_id = path.split("/")[3]
                return ApiHandler._check_account(acc_id)

            if path.startswith("/api/accounts/") and path.endswith("/sync") and method == "POST":
                acc_id = path.split("/")[3]
                return ApiHandler._sync_account_aliases(acc_id)

            if path.startswith("/api/accounts/") and path.endswith("/clear-cooldown") and method == "POST":
                acc_id = path.split("/")[3]
                db.clear_account_cooldown(acc_id)
                logger.info("已手动解除账号限流冷却状态", acc_id)
                return 200, {"ok": True, "message": "冷却已解除"}, headers

            if path.startswith("/api/accounts/") and method == "DELETE":
                acc_id = path.split("/")[3]
                ok = db.delete_account(acc_id)
                logger.info(f"已删除账号：{acc_id}")
                return 200, {"ok": ok, "message": "账号已删除" if ok else "账号不存在"}, headers

            # 2. Batch Tasks
            if path == "/api/tasks/start" and method == "POST":
                data = body_json or {}
                count = int(data.get("count", 5))
                strategy = str(data.get("strategy", "round_robin"))
                target_account = str(data.get("target_account_id", ""))
                base_label = str(data.get("base_label", "HME"))
                note = str(data.get("note", ""))

                job = scheduler.start_batch(
                    total_requested=count,
                    strategy=strategy,
                    target_account_id=target_account,
                    base_label=base_label,
                    note=note,
                )
                return 200, {"ok": True, "data": job.to_dict()}, headers

            if path == "/api/tasks/current" and method == "GET":
                job = db.get_active_batch_job()
                if not job:
                    return 200, {"ok": True, "data": None}, headers
                items = db.get_job_items(job.id)
                job_dict = job.to_dict()
                job_dict["items"] = items
                return 200, {"ok": True, "data": job_dict}, headers

            if path == "/api/tasks/pause" and method == "POST":
                ok = scheduler.pause_batch()
                return 200, {"ok": ok, "message": "任务已暂停" if ok else "未找到可暂停的任务"}, headers

            if path == "/api/tasks/resume" and method == "POST":
                ok = scheduler.resume_batch()
                return 200, {"ok": ok, "message": "任务已恢复" if ok else "未找到可恢复的任务"}, headers

            if path == "/api/tasks/cancel" and method == "POST":
                ok = scheduler.cancel_batch()
                return 200, {"ok": ok, "message": "任务已取消" if ok else "未找到可取消的任务"}, headers

            # 3. Aliases
            if path == "/api/aliases" and method == "GET":
                acc_id = query.get("account_id", [None])[0]
                search = query.get("search", [None])[0]
                limit = int(query.get("limit", [50])[0])
                offset = int(query.get("offset", [0])[0])
                aliases, total = db.get_aliases(account_id=acc_id, search=search, limit=limit, offset=offset)
                return 200, {"ok": True, "data": {"list": aliases, "total": total}}, headers

            if path == "/api/aliases/create" and method == "POST":
                data = body_json or {}
                acc_id = data.get("account_id")
                label = data.get("label", "")
                note = data.get("note", "")
                alias, err = scheduler.create_single_alias(acc_id, label, note)
                if not alias:
                    return 400, {"ok": False, "error": err}, headers
                return 200, {"ok": True, "data": alias}, headers

            if path.startswith("/api/aliases/") and method == "DELETE":
                alias_id = path.split("/")[3]
                remote_delete = query.get("remote", ["0"])[0] == "1"
                alias_record = db.delete_alias(alias_id)
                if not alias_record:
                    return 404, {"ok": False, "error": "别名不存在"}, headers

                if remote_delete and alias_record.get("anonymous_id"):
                    acc = db.get_account(alias_record["account_id"])
                    if acc:
                        try:
                            client = get_client(acc)
                            client.delete_alias(alias_record["anonymous_id"])
                            logger.info(f"已在 iCloud 远端同步删除别名：{alias_record['email']}")
                        except Exception as e:
                            logger.warning(f"远端删除失败：{e}")

                return 200, {"ok": True, "message": "别名已删除"}, headers

            # 4. Export
            if path == "/api/export" and method == "GET":
                fmt = query.get("format", ["txt"])[0].lower()
                acc_id = query.get("account_id", [None])[0]
                return ApiHandler._export_aliases(fmt, acc_id)

            # 5. Dashboard Stats & Logs
            if path == "/api/stats" and method == "GET":
                stats = db.get_summary_stats()
                return 200, {"ok": True, "data": stats}, headers

            if path == "/api/logs" and method == "GET":
                limit = int(query.get("limit", [100])[0])
                logs = db.get_logs(limit)
                return 200, {"ok": True, "data": logs}, headers

            return 404, {"ok": False, "error": f"API 路由不存在: {method} {path}"}, headers

        except SessionParseError as e:
            return 400, {"ok": False, "error": f"解析失败：{str(e)}"}, headers
        except Exception as e:
            logger.error(f"API 请求异常 [{method} {path}]: {str(e)}")
            return 500, {"ok": False, "error": f"服务器内部异常：{str(e)}"}, headers

    @staticmethod
    def _import_account(data: Dict[str, Any]) -> Tuple[int, Any, Dict[str, str]]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        # If raw cURL was passed in
        if "curl" in data and not data.get("cookie"):
            try:
                parsed = parse_any_session_input(data["curl"])
                data.update(parsed)
            except Exception as e:
                return 400, {"ok": False, "error": f"cURL 解析失败: {e}"}, headers

        name = data.get("name", "").strip() or f"Account-{int(time.time()) % 1000}"
        dsid = str(data.get("dsid", "")).strip()
        cookie = str(data.get("cookie", "")).strip()
        host = str(data.get("host", "")).strip()

        if not cookie or not host:
            return 400, {"ok": False, "error": "缺少必填参数：cookie 或 host"}, headers

        acc_id = str(data.get("id") or uuid.uuid4())[:8]
        account = AccountModel(
            id=acc_id,
            name=name,
            apple_id=data.get("apple_id", ""),
            dsid=dsid or "apple_account",
            client_id=data.get("client_id", "d39ba9916b7251055b22c7f910e2ea796ee65e98b2ddecea8f5dde8d9d1a815d"),
            client_build_number=data.get("client_build_number", "2622Build20"),
            client_mastering_number=data.get("client_mastering_number", "2622Build20"),
            host=host,
            cookie=cookie,
            origin=data.get("origin", "https://www.icloud.com"),
            referer=data.get("referer", "https://www.icloud.com/"),
            user_agent=data.get("user_agent", ""),
            lang_code=data.get("lang_code", "zh-cn"),
            channel=data.get("channel", "icloud_web"),
            scnt=data.get("scnt", ""),
            api_key=data.get("api_key", ""),
            status="active",
        )

        db.save_account(account)
        logger.success(f"已成功添加 iCloud 账号 [{account.name}] (Host: {account.host})", account.id)

        # Automatically run connection check in background
        try:
            client = get_client(account)
            chk = client.check_session()
            db.update_account_check_result(
                account.id,
                alias_count=chk.get("alias_count", 0),
                selected_forward_to=chk.get("selected_forward_to", ""),
                status="active",
            )
            account.alias_count = chk.get("alias_count", 0)
            account.selected_forward_to = chk.get("selected_forward_to", "")
        except Exception as e:
            logger.warning(f"账号添加成功，但连通性测试未通过：{e}", account.id)

        return 200, {"ok": True, "data": account.to_dict()}, headers

    @staticmethod
    def _start_protocol_import(data: Dict[str, Any]) -> Tuple[int, Any, Dict[str, str]]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        raw_text = str(data.get("text") or data.get("raw") or "").strip()
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

        parsed_items: List[Tuple[str, str, str]] = []
        for line in lines:
            res = parse_credential_line(line)
            if res:
                parsed_items.append(res)

        if not parsed_items:
            return 400, {
                "ok": False,
                "error": "未识别到有效的格式。格式示例：\n账号----密码----取码URL",
            }, headers

        job_id = str(uuid.uuid4())[:8]
        items_state = []
        for apple_id, _, code_url in parsed_items:
            items_state.append({
                "apple_id": apple_id,
                "code_url": code_url,
                "status": "pending",
                "step": "排队中",
                "detail": "等待发起协议协商",
                "account_id": "",
                "alias_count": 0,
                "error": "",
            })

        job_data = {
            "id": job_id,
            "status": "running",
            "total": len(parsed_items),
            "finished": 0,
            "success": 0,
            "failed": 0,
            "items": items_state,
            "created_at": int(time.time()),
        }

        with _protocol_jobs_lock:
            _protocol_jobs[job_id] = job_data

        def _worker():
            for idx, (apple_id, password, code_url) in enumerate(parsed_items):
                item_ref = job_data["items"][idx]
                item_ref["status"] = "running"
                item_ref["step"] = "正在握手"
                item_ref["detail"] = "启动 Apple SRP-6a 协商"

                def _on_step(step_title: str, step_detail: str):
                    item_ref["step"] = step_title
                    item_ref["detail"] = step_detail

                try:
                    proto = AppleProtocolLogin(apple_id)
                    session_data = proto.execute_login(password, code_url, on_step=_on_step)

                    acc_id = str(uuid.uuid4())[:8]
                    name = apple_id.split("@")[0]
                    account = AccountModel(
                        id=acc_id,
                        name=name,
                        apple_id=session_data["apple_id"],
                        dsid=session_data["dsid"],
                        client_id=session_data["client_id"],
                        client_build_number=session_data["client_build_number"],
                        client_mastering_number=session_data["client_mastering_number"],
                        host=session_data["host"],
                        cookie=session_data["cookie"],
                        origin=session_data["origin"],
                        referer=session_data["referer"],
                        user_agent=session_data.get("user_agent", ""),
                        status="active",
                        selected_forward_to=session_data.get("selected_forward_to", ""),
                    )

                    try:
                        client = get_client(account)
                        chk = client.check_session()
                        account.alias_count = chk.get("alias_count", 0)
                        if chk.get("selected_forward_to"):
                            account.selected_forward_to = chk["selected_forward_to"]
                    except Exception as e:
                        logger.warning(f"协议导入账号后连通性复核略过: {e}", account.id)

                    db.save_account(account)
                    logger.success(f"已通过协议自动登录并添加账号 [{account.name}] ({apple_id})", account.id)

                    item_ref["status"] = "success"
                    item_ref["step"] = "导入就绪"
                    item_ref["detail"] = f"已就绪，当前远端别名数: {account.alias_count}"
                    item_ref["account_id"] = account.id
                    item_ref["alias_count"] = account.alias_count
                    job_data["success"] += 1
                except Exception as e:
                    err_msg = str(e)
                    item_ref["status"] = "failed"
                    item_ref["step"] = "导入失败"
                    item_ref["detail"] = err_msg
                    item_ref["error"] = err_msg
                    job_data["failed"] += 1
                    logger.error(f"协议导入账号失败 [{apple_id}]: {err_msg}")
                finally:
                    job_data["finished"] += 1

            job_data["status"] = "completed"

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

        return 200, {"ok": True, "data": job_data}, headers

    @staticmethod
    def _get_protocol_import_status(job_id: str) -> Tuple[int, Any, Dict[str, str]]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if not job_id:
            return 400, {"ok": False, "error": "缺少 job_id 参数"}, headers
        with _protocol_jobs_lock:
            job = _protocol_jobs.get(job_id)
        if not job:
            return 404, {"ok": False, "error": "任务不存在或已过期"}, headers
        return 200, {"ok": True, "data": job}, headers

    @staticmethod
    def _check_account(account_id: str) -> Tuple[int, Any, Dict[str, str]]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        acc = db.get_account(account_id)
        if not acc:
            return 404, {"ok": False, "error": "账号不存在"}, headers

        client = get_client(acc)
        try:
            res = client.check_session()
            alias_count = res.get("alias_count", 0)
            forward_to = res.get("selected_forward_to", "")
            db.update_account_check_result(account_id, alias_count, forward_to, status="active")
            logger.success(f"账号 [{acc.name}] 连通性测试通过！远端别名数: {alias_count}, 转发收件箱: {forward_to}", account_id)
            return 200, {"ok": True, "data": res}, headers
        except Exception as e:
            db.update_account_status(account_id, status="need_login", last_error=str(e))
            logger.error(f"账号 [{acc.name}] 连通性测试失败：{e}", account_id)
            return 400, {"ok": False, "error": str(e)}, headers

    @staticmethod
    def _sync_account_aliases(account_id: str) -> Tuple[int, Any, Dict[str, str]]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        acc = db.get_account(account_id)
        if not acc:
            return 404, {"ok": False, "error": "账号不存在"}, headers

        client = get_client(acc)
        try:
            remotes = client.list_aliases()
            added_count = 0
            for r in remotes:
                alias = AliasModel(
                    id=str(uuid.uuid4())[:12],
                    account_id=acc.id,
                    email=r["email"],
                    anonymous_id=r["anonymous_id"],
                    label=r["label"],
                    note=r["note"],
                    forward_to=r["forward_to"],
                    is_active=r["is_active"],
                    channel=acc.channel,
                    origin="synced",
                    created_at=r["created_at"],
                )
                if db.save_alias(alias):
                    added_count += 1

            db.update_account_check_result(account_id, len(remotes), acc.selected_forward_to, status="active")
            logger.success(f"账号 [{acc.name}] 同步成功！远端共 {len(remotes)} 个别名，本地新增入库 {added_count} 个", account_id)
            return 200, {"ok": True, "data": {"total_remote": len(remotes), "new_saved": added_count}}, headers
        except Exception as e:
            logger.error(f"账号 [{acc.name}] 同步别名失败：{e}", account_id)
            return 400, {"ok": False, "error": str(e)}, headers

    @staticmethod
    def _export_aliases(fmt: str, account_id: Optional[str]) -> Tuple[int, Any, Dict[str, str]]:
        aliases, _ = db.get_aliases(account_id=account_id, limit=10000)

        if fmt == "txt":
            emails = [a["email"] for a in aliases]
            content = "\n".join(emails)
            headers = {
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Disposition": 'attachment; filename="icloud_aliases.txt"',
            }
            return 200, content, headers

        if fmt == "csv":
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=[
                "email", "label", "note", "forward_to", "is_active", "account_name", "created_at"
            ], extrasaction="ignore")
            writer.writeheader()
            for a in aliases:
                row = dict(a)
                row["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a["created_at"]))
                writer.writerow(row)
            headers = {
                "Content-Type": "text/csv; charset=utf-8",
                "Content-Disposition": 'attachment; filename="icloud_aliases.csv"',
            }
            return 200, output.getvalue(), headers

        # JSON
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Disposition": 'attachment; filename="icloud_aliases.json"',
        }
        return 200, json.dumps(aliases, ensure_ascii=False, indent=2), headers
