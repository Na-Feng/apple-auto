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


def get_client(account: AccountModel, prefer_channel: str = ""):
    if prefer_channel == "apple_account":
        return AppleAccountClient(account)
    elif prefer_channel == "icloud_web":
        return ICloudWebClient(account)

    if account.channel == "apple_account":
        return AppleAccountClient(account)
    elif account.channel == "dual":
        # Dual-channel: prioritize Apple Account (20/h high quota) if not cooling down
        if account.has_apple_account and not account.is_apple_cooling:
            return AppleAccountClient(account)
        return ICloudWebClient(account)
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
        auto_pilot: bool = True,
    ) -> BatchJobModel:
        with self._lock:
            # Check if there is already an active job
            active = db.get_active_batch_job()
            if active and active.status in ("queued", "running", "waiting_cooldown"):
                raise RuntimeError(f"已有正在运行或托管中的批量创建任务 (ID: {active.id})，请先等待完成或取消")

            job_id = str(uuid.uuid4())[:8]
            job = BatchJobModel(
                id=job_id,
                total_requested=total_requested,
                strategy=strategy,
                target_account_id=target_account_id,
                base_label=base_label or "HME",
                note=note or "",
                status="queued",
                auto_pilot=auto_pilot,
            )
            db.create_batch_job(job)

            # Pre-create batch items for intent tracking
            for i in range(total_requested):
                item_id = f"{job_id}-{i+1:03d}"
                label = f"{base_label}{i+1:02d}" if total_requested > 1 else base_label
                db.add_batch_item(item_id, job_id, i + 1, target_account_id, label)

            mode_str = "【自动托管模式（遇频控自动休眠重试直至完成）】" if auto_pilot else "【普通模式】"
            logger.info(f"创建批量任务成功：共 {total_requested} 个别名，策略：{strategy}，{mode_str}")

            self._stop_event.clear()
            self._pause_event.clear()
            self._worker_thread = threading.Thread(target=self._run_worker, args=(job,), daemon=True)
            self._worker_thread.start()
            return job

    def pause_batch(self) -> bool:
        with self._lock:
            job = db.get_active_batch_job()
            if not job or job.status not in ("running", "waiting_cooldown"):
                return False
            self._pause_event.set()
            job.status = "paused"
            job.cooldown_resume_at = 0
            db.update_batch_job(job)
            logger.warning(f"批量任务已暂停 (ID: {job.id})")
            return True

    def resume_batch(self) -> bool:
        with self._lock:
            job = db.get_active_batch_job()
            if not job or job.status not in ("paused", "waiting_cooldown"):
                return False
            self._stop_event.clear()
            self._pause_event.clear()
            job.status = "running"
            job.cooldown_resume_at = 0
            db.update_batch_job(job)
            if self._worker_thread is None or not self._worker_thread.is_alive():
                self._worker_thread = threading.Thread(target=self._run_worker, args=(job,), daemon=True)
                self._worker_thread.start()
            logger.info(f"批量任务已恢复执行 (ID: {job.id})")
            return True

    def cancel_batch(self) -> bool:
        with self._lock:
            job = db.get_active_batch_job()
            if not job or job.status in ("completed", "cancelled", "failed"):
                return False
            self._stop_event.set()
            job.status = "cancelled"
            job.cooldown_resume_at = 0
            db.update_batch_job(job)
            logger.warning(f"批量任务已取消 (ID: {job.id})")
            return True

    def resume_active_job_if_any(self) -> None:
        """Called on server startup to resume any ongoing autopilot task."""
        with self._lock:
            job = db.get_active_batch_job()
            if job and job.status in ("running", "waiting_cooldown"):
                logger.info(f"检测到未完成的自动托管批量任务 (ID: {job.id}, 目标: {job.total_requested})，正在自动重启后台工作线程...")
                self._stop_event.clear()
                self._pause_event.clear()
                self._worker_thread = threading.Thread(target=self._run_worker, args=(job,), daemon=True)
                self._worker_thread.start()

    def _run_worker(self, job: BatchJobModel) -> None:
        try:
            job.status = "running"
            db.update_batch_job(job)

            items = db.get_job_items(job.id)
            account_cycle_idx = 0

            logger.info(f"开始执行批量创建任务 (ID: {job.id})，共 {len(items)} 项，托管模式: {job.auto_pilot}")

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

                # Process this item with auto-pilot retry loop
                item_succeeded = False
                while not item_succeeded and not self._stop_event.is_set() and not self._pause_event.is_set():
                    account, err = self._select_account(job.strategy, job.target_account_id, account_cycle_idx)

                    # If no account available (due to cooling or no accounts)
                    while not account:
                        if not job.auto_pilot:
                            logger.warning(f"无法选择可用账号（已自动暂停任务）：{err}")
                            job.last_error = err
                            job.status = "paused"
                            db.update_batch_job(job)
                            break

                        # Auto-pilot mode: compute cooldown sleep time
                        all_accounts = db.get_all_accounts()
                        now = int(time.time())
                        cooldown_times = []
                        for a in all_accounts:
                            if a.channel == "dual":
                                cands = [x for x in (a.apple_cooldown_until, a.icloud_cooldown_until) if x > now]
                                if cands:
                                    cooldown_times.append(min(cands))
                            else:
                                c = a.cooldown_until or a.apple_cooldown_until or a.icloud_cooldown_until
                                if c > now:
                                    cooldown_times.append(c)

                        earliest_until = min(cooldown_times) if cooldown_times else (now + 180)
                        wait_sec = max(5, earliest_until - now + 3)

                        job.status = "waiting_cooldown"
                        job.cooldown_resume_at = now + wait_sec
                        job.last_error = f"触发通道官方频控，自动托管休眠中：{wait_sec // 60} 分 {wait_sec % 60} 秒后自动唤醒继续创建..."
                        db.update_batch_job(job)
                        logger.info(f"【自动托管模式】所有通道进入频控期，自动休眠 {wait_sec} 秒 ({wait_sec // 60} 分钟)，期间保活守护进程持续维持会话有效性...")

                        # Responsive interruptible sleep
                        slept = 0
                        while slept < wait_sec and not self._stop_event.is_set() and not self._pause_event.is_set():
                            time.sleep(1)
                            slept += 1

                        if self._stop_event.is_set() or self._pause_event.is_set():
                            break

                        job.status = "running"
                        job.cooldown_resume_at = 0
                        job.last_error = ""
                        db.update_batch_job(job)
                        logger.success(f"【自动托管模式】冷却期已结束，系统自动唤醒并继续执行创建任务！")

                        account, err = self._select_account(job.strategy, job.target_account_id, account_cycle_idx)

                    if not account:
                        # User paused, cancelled, or non-autopilot stopped
                        break

                    account_cycle_idx += 1
                    job.current_account_name = account.name
                    db.update_batch_job(job)

                    # Attempt creation with intent & retry logic
                    try:
                        created_email, err_msg = self._create_item_with_account(item_id, account, label, job.note)
                    except Exception as e:
                        logger.error(f"处理任务项 [{item_id}] 发生异常：{e}")
                        created_email, err_msg = None, str(e)

                    if created_email:
                        item_succeeded = True
                        job.created_count += 1
                        logger.success(f"[{position}/{job.total_requested}] 账号 [{account.name}] 成功创建别名：{created_email} (标签: {label})")
                        db.update_batch_job(job)
                    else:
                        logger.warning(f"[{position}/{job.total_requested}] 账号 [{account.name}] 创建暂未成功：{err_msg}")
                        # If failed due to rate limit or cooldown, and auto_pilot is active:
                        # Preserve this item in pending status and let outer loop sleep and retry!
                        is_cooldown_err = any(w in err_msg for w in ["额度", "上限", "冷却", "rate_limit", "429"])
                        if job.auto_pilot and is_cooldown_err:
                            logger.info(f"任务项 [{position}] 遇官方频控限流，自动托管将保留该项并在冷却后重试")
                            db.update_batch_item(item_id, status="pending", error="等待频控冷却解除后自动重试")
                            continue
                        else:
                            # Permanent error or non-autopilot mode
                            db.update_batch_item(item_id, status="failed", error=err_msg)
                            job.failed_count += 1
                            db.update_batch_job(job)
                            break

                if self._stop_event.is_set() or self._pause_event.is_set() or (not item_succeeded and not job.auto_pilot):
                    if job.status not in ("cancelled", "paused"):
                        job.status = "paused"
                        db.update_batch_job(job)
                    break

                # Safety delay between creations
                if not self._stop_event.is_set() and position < job.total_requested:
                    time.sleep(self.inter_create_delay)

            # Mark finished if not paused or cancelled
            if not self._stop_event.is_set() and job.status not in ("paused", "waiting_cooldown"):
                if job.created_count >= job.total_requested:
                    job.status = "completed"
                    logger.success(f"批量创建任务已圆满完成！共成功创建 {job.created_count} 个别名")
                elif job.created_count > 0:
                    job.status = "completed"
                    logger.warning(f"批量任务结束，成功 {job.created_count} 个，失败 {job.failed_count} 个")
                else:
                    job.status = "failed"
                    logger.error("批量任务失败，未成功创建任何别名")

        except Exception as e:
            logger.error(f"批量任务工作线程异常退出: {e}")
            job.last_error = str(e)
            if job.status == "running":
                job.status = "paused"
        finally:
            db.update_batch_job(job)

    def _select_account(
        self, strategy: str, target_account_id: str, cycle_index: int
    ) -> Tuple[Optional[AccountModel], str]:
        """Select an eligible account based on strategy and rate limit status."""
        if target_account_id:
            acc = db.get_account(target_account_id)
            if not acc:
                return None, f"指定的账号 ID 不存在：{target_account_id}"
            if acc.status not in ("active", "cooling_down"):
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
            cooldowns = []
            now = int(time.time())
            for a in all_accounts:
                if a.is_cooling_down:
                    if a.channel == "dual":
                        cands = [x for x in (a.apple_cooldown_until, a.icloud_cooldown_until) if x > now]
                        if cands:
                            cooldowns.append(min(cands))
                    else:
                        c = a.cooldown_until or a.apple_cooldown_until or a.icloud_cooldown_until
                        if c > now:
                            cooldowns.append(c)
            if cooldowns:
                earliest = min(cooldowns)
                wait_sec = max(0, earliest - now)
                return None, f"所有账号通道均处于官方频控冷却中，最早通道解除冷却还需等待 {wait_sec} 秒 ({wait_sec // 60} 分钟)"
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
        """Creates alias for a specific item using smart dual-channel quota scheduling."""
        channels_to_try = []
        if account.channel == "dual":
            # 1. Prefer Apple Account (high quota 20/h) if available & not cooling
            if account.has_apple_account and not account.is_apple_cooling:
                channels_to_try.append("apple_account")
            # 2. Fallback to iCloud Web (5/h) if available & not cooling
            if account.has_icloud_web and not account.is_icloud_cooling:
                channels_to_try.append("icloud_web")
        elif account.channel == "apple_account":
            if not account.is_apple_cooling:
                channels_to_try.append("apple_account")
        else:
            if not account.is_icloud_cooling:
                channels_to_try.append("icloud_web")

        if not channels_to_try:
            return None, "该账号所有可用通道均处于限流冷却中"

        last_err = ""
        for channel_name in channels_to_try:
            client = get_client(account, prefer_channel=channel_name)
            logger.info(f"账号 [{account.name}] 调度通道 [{channel_name}] 执行别名创建...", account.id)

            try:
                db.update_batch_item(item_id, status="generating")
                candidate = client.generate_alias()
                db.update_batch_item(item_id, status="reserving", candidate=candidate)

                time.sleep(0.3)

                res = client.reserve_alias(candidate, label=label, note=note)
                email = res.get("email") or candidate
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
                    channel=channel_name,
                    origin="created",
                )
                db.save_alias(alias)
                db.increment_account_local_alias_count(account.id)
                db.update_batch_item(item_id, status="success", created_email=email)
                return email, ""

            except RateLimitError as e:
                cooldown_sec = max(e.retry_after, 1800)
                db.set_channel_cooldown(account.id, channel_name, cooldown_sec, last_error=str(e))
                logger.warning(
                    f"账号 [{account.name}] 的 [{channel_name}] 通道触发额度限流，自动冷却 {cooldown_sec // 60} 分钟",
                    account.id,
                )
                last_err = f"{channel_name} 额度达上限"
                if len(channels_to_try) > 1 and channel_name == "apple_account":
                    logger.info(f"账号 [{account.name}] 智能自动无缝降级回落至 [icloud_web] 通道继续创建！", account.id)
            except CandidateExpiredError as e:
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
                        channel=channel_name,
                        origin="created",
                    )
                    db.save_alias(alias)
                    db.increment_account_local_alias_count(account.id)
                    db.update_batch_item(item_id, status="success", created_email=email)
                    return email, ""
                except Exception as e2:
                    last_err = str(e2)
                    continue

            except NetworkError as e:
                if candidate and hasattr(client, "list_aliases"):
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
                                    channel=channel_name,
                                    origin="created",
                                )
                                db.save_alias(alias)
                                db.increment_account_local_alias_count(account.id)
                                db.update_batch_item(item_id, status="success", created_email=email)
                                logger.success(f"Reconcile 成功确认别名已在远端创建：{email}")
                                return email, ""
                    except Exception:
                        pass
                last_err = f"网络错误：{e}"
                continue

            except AuthExpiredError as e:
                if channel_name == "apple_account":
                    if account.can_reacquire_apple:
                        logger.info(f"账号 [{account.name}] Apple Account 凭据过期，尝试自动重新采集...", account.id)
                        if client._auto_reacquire():
                            account = db.get_account(account.id)
                            try:
                                candidate2 = client.generate_alias()
                                time.sleep(0.3)
                                res2 = client.reserve_alias(candidate2, label=label, note=note)
                                email2 = res2.get("email") or candidate2
                                alias2 = AliasModel(
                                    id=str(uuid.uuid4())[:12],
                                    account_id=account.id,
                                    email=email2,
                                    anonymous_id=res2.get("anonymous_id", ""),
                                    label=label,
                                    note=note,
                                    forward_to=res2.get("forward_to", account.selected_forward_to),
                                    channel=channel_name,
                                    origin="created",
                                )
                                db.save_alias(alias2)
                                db.increment_account_local_alias_count(account.id)
                                db.update_batch_item(item_id, status="success", created_email=email2)
                                return email2, ""
                            except Exception:
                                pass
                    db.set_apple_auth_expired(account.id, True, last_error=str(e))
                    logger.warning(f"账号 [{account.name}] [apple_account] 凭据失效：{e}，自动转由备用通道接管", account.id)
                else:
                    db.update_account_status(account.id, status="need_login", last_error=str(e))
                last_err = str(e)
                continue

            except HmeClientError as e:
                logger.warning(f"账号 [{account.name}] [{channel_name}] 客户端异常：{e}", account.id)
                last_err = str(e)
                continue

            except Exception as e:
                logger.warning(f"账号 [{account.name}] [{channel_name}] 创建异常：{e}", account.id)
                last_err = str(e)
                continue

        db.update_batch_item(item_id, status="failed", error=last_err)
        return None, last_err

    def create_single_alias(
        self, account_id: Optional[str] = None, label: str = "", note: str = ""
    ) -> Tuple[Optional[dict], str]:
        """Creates a single alias immediately on specified or auto-selected account."""
        account, err = self._select_account("round_robin", account_id or "", 0)
        if not account:
            return None, err

        if not label:
            label = f"API-{time.strftime('%m%d-%H%M%S')}"

        channels_to_try = []
        if account.channel == "dual":
            if account.has_apple_account and not account.is_apple_cooling:
                channels_to_try.append("apple_account")
            if account.has_icloud_web and not account.is_icloud_cooling:
                channels_to_try.append("icloud_web")
        elif account.channel == "apple_account":
            if not account.is_apple_cooling:
                channels_to_try.append("apple_account")
        else:
            if not account.is_icloud_cooling:
                channels_to_try.append("icloud_web")

        if not channels_to_try:
            return None, f"账号 [{account.name}] 当前所有可用通道均处于频控冷却中"

        last_err = ""
        for ch in channels_to_try:
            client = get_client(account, prefer_channel=ch)
            try:
                cand = client.generate_alias()
                time.sleep(0.3)
                res = client.reserve_alias(cand, label=label, note=note)
                email = res.get("email", "") or cand
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
                    channel=ch,
                    origin="created",
                )
                db.save_alias(alias)
                db.increment_account_local_alias_count(account.id)
                logger.success(f"单次创建成功：{email} (账号: {account.name}, 通道: {ch})")
                return alias.to_dict(), ""
            except RateLimitError as e:
                db.set_channel_cooldown(account.id, ch, max(e.retry_after, 1800), last_error=str(e))
                last_err = f"通道 [{ch}] 频控：{e}"
                continue
            except AuthExpiredError as e:
                if ch == "apple_account":
                    db.set_apple_auth_expired(account.id, True, last_error=str(e))
                    logger.warning(f"账号 [{account.name}] Apple Account 凭据已失效，已自动转由备用通道接管", account.id)
                else:
                    db.update_account_status(account.id, status="need_login", last_error=str(e))
                last_err = str(e)
                continue
            except Exception as e:
                last_err = str(e)
                continue

        return None, f"创建失败：{last_err}"


# Singleton scheduler instance
scheduler = BatchScheduler()


class AccountAutopilotManager:
    """
    Manages per-account concurrent, non-blocking, isolated Auto-Pilot workers.
    Each account has its own target (e.g. 750 aliases), its own worker thread,
    its own rate-limit cooldown sleep, and its own assigned proxy.
    """
    def __init__(self):
        self._lock = threading.RLock()
        self._workers: Dict[str, threading.Thread] = {}
        self._stop_events: Dict[str, threading.Event] = {}
        self._pause_events: Dict[str, threading.Event] = {}

    def is_running(self, account_id: str) -> bool:
        with self._lock:
            t = self._workers.get(account_id)
            return t is not None and t.is_alive()

    def start_account(self, account_id: str, target_count: int = 750, base_label: str = "HME") -> bool:
        with self._lock:
            account = db.get_account(account_id)
            if not account:
                return False

            if self.is_running(account_id):
                return True

            account.target_alias_count = target_count
            account.autopilot_status = "running"
            account.autopilot_last_error = ""
            db.save_account(account)

            stop_ev = threading.Event()
            pause_ev = threading.Event()
            self._stop_events[account_id] = stop_ev
            self._pause_events[account_id] = pause_ev

            t = threading.Thread(
                target=self._run_account_worker,
                args=(account_id, base_label, stop_ev, pause_ev),
                daemon=True,
                name=f"autopilot-{account.name}"
            )
            self._workers[account_id] = t
            t.start()
            logger.info(
                f"【账号独立托管】已启动账号 [{account.name}] 专属 750 托管线程 (当前: {max(account.alias_count, account.local_alias_count)}/750, 代理: {account.proxy_url or '直连'})",
                account_id,
            )
            return True

    def stop_account(self, account_id: str) -> bool:
        with self._lock:
            stop_ev = self._stop_events.get(account_id)
            if stop_ev:
                stop_ev.set()
            db.update_account_autopilot_status(account_id, status="idle")
            return True

    def pause_account(self, account_id: str) -> bool:
        with self._lock:
            pause_ev = self._pause_events.get(account_id)
            if pause_ev:
                pause_ev.set()
            db.update_account_autopilot_status(account_id, status="paused")
            return True

    def resume_account(self, account_id: str) -> bool:
        with self._lock:
            pause_ev = self._pause_events.get(account_id)
            if pause_ev:
                pause_ev.clear()
            account = db.get_account(account_id)
            if account:
                db.update_account_autopilot_status(account_id, status="running")
                if not self.is_running(account_id):
                    return self.start_account(account_id, target_count=account.target_alias_count or 750)
            return True

    def start_all(self, target_count: int = 750) -> Dict[str, Any]:
        accounts = db.get_all_accounts()
        started = 0
        for acc in accounts:
            if acc.status in ("active", "cooling_down"):
                if self.start_account(acc.id, target_count=target_count):
                    started += 1
        return {"total_accounts": len(accounts), "started": started}

    def stop_all(self) -> None:
        with self._lock:
            for acc_id in list(self._workers.keys()):
                self.stop_account(acc_id)

    def resume_all_active(self) -> None:
        """Called on server startup: auto-resumes all accounts that were running or waiting."""
        accounts = db.get_all_accounts()
        for acc in accounts:
            if acc.autopilot_status in ("running", "waiting_cooldown"):
                logger.info(f"检测到账号 [{acc.name}] 处于托管状态，自动重启专属工作线程...")
                self.start_account(acc.id, target_count=acc.target_alias_count or 750)

    def _run_account_worker(
        self,
        account_id: str,
        base_label: str,
        stop_event: threading.Event,
        pause_event: threading.Event,
    ) -> None:
        try:
            while not stop_event.is_set():
                # Handle pause
                while pause_event.is_set() and not stop_event.is_set():
                    time.sleep(1)
                if stop_event.is_set():
                    break

                account = db.get_account(account_id)
                if not account:
                    break

                current_total = max(account.alias_count, account.local_alias_count)
                if current_total >= account.target_alias_count:
                    db.update_account_autopilot_status(account_id, status="completed", last_error="已达到 750 目标满额")
                    logger.success(f"🎉 账号 [{account.name}] 已圆满完成目标 ({current_total}/{account.target_alias_count})！", account_id)
                    break

                # Determine channel for this account
                now = int(time.time())
                channels = []
                if account.channel == "dual":
                    if account.has_apple_account and not account.is_apple_cooling:
                        channels.append("apple_account")
                    if account.has_icloud_web and not account.is_icloud_cooling:
                        channels.append("icloud_web")
                elif account.channel == "apple_account":
                    if not account.is_apple_cooling:
                        channels.append("apple_account")
                else:
                    if not account.is_icloud_cooling:
                        channels.append("icloud_web")

                # If all channels for this specific account are cooling
                if not channels:
                    cands = []
                    if account.channel == "dual":
                        for t in (account.apple_cooldown_until, account.icloud_cooldown_until):
                            if t > now:
                                cands.append(t)
                    else:
                        c = account.cooldown_until or account.apple_cooldown_until or account.icloud_cooldown_until
                        if c > now:
                            cands.append(c)
                    if not cands:
                        err_detail = "账号所有可用通道均处于失效或不可用状态（凭据过期且无自动接码配置，或通道未激活）"
                        db.update_account_autopilot_status(account_id, status="error", last_error=err_detail)
                        logger.error(f"【账号独立托管】账号 [{account.name}] 无法继续：{err_detail}", account_id)
                        break

                    earliest = min(cands)
                    wait_sec = max(5, earliest - now + 3)

                    db.update_account_autopilot_status(
                        account_id,
                        status="waiting_cooldown",
                        cooldown_until=now + wait_sec,
                        last_error=f"官方频控中，专属休眠 {wait_sec // 60} 分 {wait_sec % 60} 秒后自动继续...",
                    )
                    logger.info(
                        f"【账号独立托管】账号 [{account.name}] 触发频控，专属休眠 {wait_sec} 秒 ({wait_sec // 60} 分钟)，其它账号并发正常运行不受影响！",
                        account_id,
                    )

                    slept = 0
                    while slept < wait_sec and not stop_event.is_set() and not pause_event.is_set():
                        time.sleep(1)
                        slept += 1

                    if stop_event.is_set() or pause_event.is_set():
                        continue

                    db.update_account_autopilot_status(account_id, status="running")
                    logger.success(f"【账号独立托管】账号 [{account.name}] 冷却期结束，唤醒继续创建！", account_id)
                    continue

                # Attempt creation
                label = f"{base_label}-{current_total + 1:03d}"
                created = False
                for ch in channels:
                    client = get_client(account, prefer_channel=ch)
                    try:
                        cand = client.generate_alias()
                        time.sleep(0.3)
                        res = client.reserve_alias(cand, label=label)
                        email = res.get("email") or cand
                        alias = AliasModel(
                            id=str(uuid.uuid4())[:12],
                            account_id=account.id,
                            email=email,
                            anonymous_id=res.get("anonymous_id", ""),
                            label=label,
                            forward_to=res.get("forward_to", account.selected_forward_to),
                            channel=ch,
                            origin="created",
                        )
                        db.save_alias(alias)
                        db.increment_account_local_alias_count(account.id)
                        current_total += 1
                        created = True
                        logger.success(
                            f"【账号独立托管】账号 [{account.name}] 成功创建第 {current_total} 个别名：{email} (通道: {ch}, 进度: {current_total}/{account.target_alias_count})",
                            account.id,
                        )
                        break
                    except RateLimitError as e:
                        cooldown_sec = max(e.retry_after, 1800)
                        db.set_channel_cooldown(account.id, ch, cooldown_sec, last_error=str(e))
                        logger.warning(f"账号 [{account.name}] 的 [{ch}] 触发频控，自动冷却 {cooldown_sec // 60} 分钟", account.id)
                        continue
                    except AuthExpiredError as e:
                        if ch == "apple_account":
                            if account.can_reacquire_apple:
                                logger.info(f"账号 [{account.name}] Apple Account 凭据过期，正在尝试全自动重新采集...", account.id)
                                if client._auto_reacquire():
                                    account = db.get_account(account_id)
                                    try:
                                        cand2 = client.generate_alias()
                                        time.sleep(0.3)
                                        res2 = client.reserve_alias(cand2, label=label)
                                        email2 = res2.get("email") or cand2
                                        alias2 = AliasModel(
                                            id=str(uuid.uuid4())[:12],
                                            account_id=account.id,
                                            email=email2,
                                            anonymous_id=res2.get("anonymous_id", ""),
                                            label=label,
                                            forward_to=res2.get("forward_to", account.selected_forward_to),
                                            channel=ch,
                                            origin="created",
                                        )
                                        db.save_alias(alias2)
                                        db.increment_account_local_alias_count(account.id)
                                        current_total += 1
                                        created = True
                                        logger.success(
                                            f"【账号独立托管】账号 [{account.name}] 重新认证后成功创建第 {current_total} 个别名：{email2} (通道: {ch})",
                                            account.id,
                                        )
                                        break
                                    except Exception as retry_err:
                                        logger.warning(f"重新采集凭据后创建依然未完成: {retry_err}", account.id)

                            db.set_apple_auth_expired(account.id, True, last_error=str(e))
                            logger.warning(f"账号 [{account.name}] Apple Account 凭据已失效，自动转入备用通道继续托管", account.id)
                        else:
                            db.update_account_status(account.id, status="need_login", last_error=str(e))
                        continue
                    except Exception as e:
                        logger.warning(f"账号 [{account.name}] 通道 [{ch}] 创建异常: {e}", account.id)
                        continue

                # Safety delay between creations for this account (e.g. 4 seconds)
                if not stop_event.is_set():
                    time.sleep(4)

        except Exception as e:
            logger.error(f"账号 [{account_id}] 托管线程异常退出: {e}")
            db.update_account_autopilot_status(account_id, status="error", last_error=str(e))
        finally:
            with self._lock:
                self._workers.pop(account_id, None)


# Singleton account autopilot instance
account_autopilot = AccountAutopilotManager()
