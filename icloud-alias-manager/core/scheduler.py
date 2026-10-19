from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from core.client_apple_account import AppleAccountClient
from core.client_icloud_web import (
    AuthExpiredError,
    CandidateExpiredError,
    HmeClientError,
    ICloudWebClient,
    NetworkError,
    RateLimitError,
)
from core.db import db
from core.logger import logger
from core.models import AccountModel, AliasModel, BatchJobModel


def get_client(account: AccountModel):
    if account.channel == "apple_account":
        return AppleAccountClient(account)
    return ICloudWebClient(account)


class BatchScheduler:
    """Multi-account automated Hide My Email batch scheduler."""

    def __init__(self):
        self._lock = threading.RLock()
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self.inter_create_delay = 2.5  # Seconds between creations to avoid burst limits

    def is_running(self) -> bool:
        with self._lock:
            return self._worker_thread is not None and self._worker_thread.is_alive()

    def start_batch(
        self,
        total_requested: int,
        strategy: str = "round_robin",
        target_account_id: str = "",
        base_label: str = "HME",
        note: str = "",
    ) -> BatchJobModel:
        with self._lock:
            # Check if there is already an active job
            active = db.get_active_batch_job()
            if active and active.status in ("queued", "running"):
                raise RuntimeError(f"已有正在运行的批量创建任务 (ID: {active.id})，请先等待完成或取消")

            job_id = str(uuid.uuid4())[:8]
            job = BatchJobModel(
                id=job_id,
                total_requested=total_requested,
                strategy=strategy,
                target_account_id=target_account_id,
                base_label=base_label or "HME",
                note=note or "",
                status="queued",
            )
            db.create_batch_job(job)

            # Pre-create batch items for intent tracking
            for i in range(total_requested):
                item_id = f"{job_id}-{i+1:03d}"
                label = f"{base_label}{i+1:02d}" if total_requested > 1 else base_label
                db.add_batch_item(item_id, job_id, i + 1, target_account_id, label)

            logger.info(f"创建批量任务成功：共 {total_requested} 个别名，策略：{strategy}")

            self._stop_event.clear()
            self._pause_event.clear()
            self._worker_thread = threading.Thread(target=self._run_worker, args=(job,), daemon=True)
            self._worker_thread.start()
            return job

    def pause_batch(self) -> bool:
        with self._lock:
            job = db.get_active_batch_job()
            if not job or job.status != "running":
                return False
            self._pause_event.set()
            job.status = "paused"
            db.update_batch_job(job)
            logger.warning(f"批量任务已暂停 (ID: {job.id})")
            return True

    def resume_batch(self) -> bool:
        with self._lock:
            job = db.get_active_batch_job()
            if not job or job.status != "paused":
                return False
            self._pause_event.clear()
            job.status = "running"
            db.update_batch_job(job)
            logger.info(f"批量任务已恢复执行 (ID: {job.id})")
            return True

    def cancel_batch(self) -> bool:
        with self._lock:
            job = db.get_active_batch_job()
            if not job or job.status in ("completed", "cancelled", "failed"):
                return False
            self._stop_event.set()
            job.status = "cancelled"
            db.update_batch_job(job)
            logger.warning(f"批量任务已取消 (ID: {job.id})")
            return True

    def _run_worker(self, job: BatchJobModel) -> None:
        job.status = "running"
        db.update_batch_job(job)

        items = db.get_job_items(job.id)
        account_cycle_idx = 0

        logger.info(f"开始执行批量创建任务 (ID: {job.id})，共 {len(items)} 项")

        for item in items:
            if self._stop_event.is_set():
                job.status = "cancelled"
                break

            # Handle pause
            while self._pause_event.is_set() and not self._stop_event.is_set():
                time.sleep(1)

            if self._stop_event.is_set():
                job.status = "cancelled"
                break

            # If item already succeeded, skip
            if item.get("status") == "success":
                continue

            item_id = item["id"]
            position = item["position"]
            label = item["label"]

            # Select account according to strategy
            account, err = self._select_account(job.strategy, job.target_account_id, account_cycle_idx)
            if not account:
                logger.error(f"无法选择可用账号：{err}")
                job.last_error = err
                job.status = "paused"  # Pause so user can re-import or wait for cooldown
                db.update_batch_job(job)
                break

            account_cycle_idx += 1
            job.current_account_name = account.name
            db.update_batch_job(job)

            # Attempt creation with intent & retry logic
            created_email, err_msg = self._create_item_with_account(item_id, account, label, job.note)
            if created_email:
                job.created_count += 1
                logger.success(f"[{position}/{job.total_requested}] 账号 [{account.name}] 成功创建别名：{created_email} (标签: {label})")
            else:
                logger.warning(f"[{position}/{job.total_requested}] 账号 [{account.name}] 创建失败：{err_msg}")
                # If rate limit or auth error, try one more time on another available account
                account_fallback, _ = self._select_account(job.strategy, "", account_cycle_idx)
                if account_fallback and account_fallback.id != account.id:
                    logger.info(f"自动故障转移，切换至备用账号 [{account_fallback.name}] 重试...")
                    created_email, err_msg = self._create_item_with_account(item_id, account_fallback, label, job.note)
                    if created_email:
                        job.created_count += 1
                        logger.success(f"[{position}/{job.total_requested}] 故障转移成功！创建别名：{created_email}")
                    else:
                        job.failed_count += 1
                else:
                    job.failed_count += 1

            db.update_batch_job(job)

            # Safety delay between creations
            if not self._stop_event.is_set() and position < job.total_requested:
                time.sleep(self.inter_create_delay)

        # Mark finished
        if not self._stop_event.is_set():
            if job.created_count >= job.total_requested:
                job.status = "completed"
                logger.success(f"批量创建任务已圆满完成！共成功创建 {job.created_count} 个别名")
            elif job.created_count > 0:
                job.status = "completed"
                logger.warning(f"批量任务结束，成功 {job.created_count} 个，失败 {job.failed_count} 个")
            else:
                job.status = "failed"
                logger.error("批量任务失败，未成功创建任何别名")

        db.update_batch_job(job)

    def _select_account(
        self, strategy: str, target_account_id: str, cycle_index: int
    ) -> Tuple[Optional[AccountModel], str]:
        """Select an eligible account based on strategy and rate limit status."""
        if target_account_id:
            acc = db.get_account(target_account_id)
            if not acc:
                return None, f"指定的账号 ID 不存在：{target_account_id}"
            if acc.status != "active":
                return None, f"账号 [{acc.name}] 状态异常：{acc.status} ({acc.last_error})"
            if acc.is_cooling_down:
                remain = acc.cooldown_until - int(time.time())
                return None, f"账号 [{acc.name}] 正在限流冷却中，还需等待 {remain} 秒"
            return acc, ""

        available = db.get_available_accounts()
        if not available:
            # Check if all accounts are cooling down
            all_accounts = db.get_all_accounts()
            if not all_accounts:
                return None, "系统暂无任何 iCloud 账号，请先添加或导入账号"
            cooldowns = [a.cooldown_until for a in all_accounts if a.is_cooling_down]
            if cooldowns:
                earliest = min(cooldowns)
                wait_sec = max(0, earliest - int(time.time()))
                return None, f"所有账号均处于限流冷却中，最早冷却解除还需等待 {wait_sec} 秒"
            return None, "没有可用的正常账号（请检查账号状态或重新导入登录态）"

        if strategy == "fill_first":
            # Always pick the first available
            return available[0], ""
        else:
            # Round-robin: balance evenly across all available accounts
            chosen = available[cycle_index % len(available)]
            return chosen, ""

    def _create_item_with_account(
        self, item_id: str, account: AccountModel, label: str, note: str
    ) -> Tuple[Optional[str], str]:
        """Creates alias for a specific item using given account, handling rate limits and reconcile."""
        client = get_client(account)
        candidate = ""

        try:
            # 1. Intent: Mark generating
            db.update_batch_item(item_id, status="generating")
            candidate = client.generate_alias()
            db.update_batch_item(item_id, status="reserving", candidate=candidate)

            time.sleep(0.3)

            # 2. Reserve
            res = client.reserve_alias(candidate, label=label, note=note)
            email = res.get("email") or candidate
            anon_id = res.get("anonymous_id", "")

            # 3. Save alias to DB
            alias = AliasModel(
                id=str(uuid.uuid4())[:12],
                account_id=account.id,
                email=email,
                anonymous_id=anon_id,
                label=label,
                note=note,
                forward_to=res.get("forward_to", account.selected_forward_to),
                is_active=True,
                channel=account.channel,
                origin="created",
            )
            db.save_alias(alias)
            db.increment_account_local_alias_count(account.id)
            db.update_batch_item(item_id, status="success", created_email=email)
            return email, ""

        except RateLimitError as e:
            # Exponential backoff on rate limits
            stage = (account.cooldown_stage or 0) + 1
            cooldown_sec = min(7200, max(e.retry_after, 1800 * (2 ** (stage - 1))))
            db.set_account_cooldown(account.id, cooldown_sec, stage, last_error=str(e))
            logger.warning(
                f"账号 [{account.name}] 触发创建上限！自动进入冷却 {cooldown_sec // 60} 分钟 (第 {stage} 阶)",
                account.id,
            )
            db.update_batch_item(item_id, status="failed", error_message=f"账号限流：{e}")
            return None, f"账号限流 (冷却 {cooldown_sec // 60}m)"

        except AuthExpiredError as e:
            db.update_account_status(account.id, status="need_login", last_error=str(e))
            logger.error(f"账号 [{account.name}] 登录凭证失效，请重新导入：{e}", account.id)
            db.update_batch_item(item_id, status="failed", error_message=f"凭证失效：{e}")
            return None, "登录态失效"

        except CandidateExpiredError as e:
            # If candidate expired, retry once with fresh generate
            logger.warning(f"候选别名失效，尝试重新生成：{e}", account.id)
            try:
                candidate2 = client.generate_alias()
                res = client.reserve_alias(candidate2, label=label, note=note)
                email = res.get("email") or candidate2
                alias = AliasModel(
                    id=str(uuid.uuid4())[:12],
                    account_id=account.id,
                    email=email,
                    anonymous_id=res.get("anonymous_id", ""),
                    label=label,
                    note=note,
                    forward_to=res.get("forward_to", account.selected_forward_to),
                    channel=account.channel,
                    origin="created",
                )
                db.save_alias(alias)
                db.increment_account_local_alias_count(account.id)
                db.update_batch_item(item_id, status="success", created_email=email)
                return email, ""
            except Exception as e2:
                db.update_batch_item(item_id, status="failed", error_message=str(e2))
                return None, str(e2)

        except NetworkError as e:
            # Network issue during reserve: Reconcile! Check if alias was actually created
            if candidate:
                logger.warning(f"网络超时，执行 Reconcile 核对远端列表 (候选: {candidate})...", account.id)
                try:
                    time.sleep(1)
                    remotes = client.list_aliases()
                    for r in remotes:
                        if r.get("email") == candidate:
                            email = candidate
                            alias = AliasModel(
                                id=str(uuid.uuid4())[:12],
                                account_id=account.id,
                                email=email,
                                anonymous_id=r.get("anonymous_id", ""),
                                label=label,
                                note=note,
                                forward_to=r.get("forward_to", account.selected_forward_to),
                                channel=account.channel,
                                origin="created",
                            )
                            db.save_alias(alias)
                            db.increment_account_local_alias_count(account.id)
                            db.update_batch_item(item_id, status="success", created_email=email)
                            logger.success(f"Reconcile 成功确认别名已在远端创建：{email}")
                            return email, ""
                except Exception:
                    pass

            db.update_batch_item(item_id, status="failed", error_message=f"网络错误：{e}")
            return None, f"网络错误：{e}"

        except Exception as e:
            db.update_batch_item(item_id, status="failed", error_message=str(e))
            return None, str(e)

    def create_single_alias(
        self, account_id: Optional[str] = None, label: str = "", note: str = ""
    ) -> Tuple[Optional[dict], str]:
        """Creates a single alias immediately on specified or auto-selected account."""
        account, err = self._select_account("round_robin", account_id or "", 0)
        if not account:
            return None, err

        client = get_client(account)
        if not label:
            label = f"API-{time.strftime('%m%d-%H%M%S')}"

        try:
            res = client.create_alias(label=label, note=note)
            email = res.get("email", "")
            anon_id = res.get("anonymous_id", "")

            alias = AliasModel(
                id=str(uuid.uuid4())[:12],
                account_id=account.id,
                email=email,
                anonymous_id=anon_id,
                label=label,
                note=note,
                forward_to=res.get("forward_to", account.selected_forward_to),
                is_active=True,
                channel=account.channel,
                origin="created",
            )
            db.save_alias(alias)
            db.increment_account_local_alias_count(account.id)
            logger.success(f"单次创建成功：{email} (账号: {account.name})")
            return alias.to_dict(), ""
        except RateLimitError as e:
            db.set_account_cooldown(account.id, e.retry_after, 1, last_error=str(e))
            return None, f"账号 [{account.name}] 达到创建上限，已进入冷却"
        except AuthExpiredError as e:
            db.update_account_status(account.id, status="need_login", last_error=str(e))
            return None, f"账号 [{account.name}] 登录凭据失效"
        except Exception as e:
            return None, f"创建失败：{e}"


# Singleton scheduler instance
scheduler = BatchScheduler()
