from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

from core.models import AccountModel, AliasModel, BatchJobModel

DB_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DB_DIR / "icloud_alias.db"


class Database:
    """Thread-safe SQLite database manager for multi-account alias platform."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        # Enable WAL mode for high performance concurrent reads and writes
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._get_connection() as conn:
            # Accounts table
            conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                apple_id TEXT NOT NULL DEFAULT '',
                dsid TEXT NOT NULL,
                client_id TEXT NOT NULL,
                client_build_number TEXT NOT NULL,
                client_mastering_number TEXT NOT NULL,
                host TEXT NOT NULL,
                cookie TEXT NOT NULL,
                origin TEXT NOT NULL DEFAULT 'https://www.icloud.com',
                referer TEXT NOT NULL DEFAULT 'https://www.icloud.com/',
                user_agent TEXT NOT NULL DEFAULT '',
                lang_code TEXT NOT NULL DEFAULT 'zh-cn',
                channel TEXT NOT NULL DEFAULT 'icloud_web',
                scnt TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                cooldown_until INTEGER NOT NULL DEFAULT 0,
                cooldown_stage INTEGER NOT NULL DEFAULT 0,
                apple_cooldown_until INTEGER NOT NULL DEFAULT 0,
                icloud_cooldown_until INTEGER NOT NULL DEFAULT 0,
                selected_forward_to TEXT NOT NULL DEFAULT '',
                alias_count INTEGER NOT NULL DEFAULT 0,
                local_alias_count INTEGER NOT NULL DEFAULT 0,
                last_checked_at INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """)

            # Auto-migration for existing databases
            for col, col_def in [
                ("apple_cooldown_until", "INTEGER NOT NULL DEFAULT 0"),
                ("icloud_cooldown_until", "INTEGER NOT NULL DEFAULT 0"),
                ("password", "TEXT NOT NULL DEFAULT ''"),
                ("code_url", "TEXT NOT NULL DEFAULT ''"),
                ("target_alias_count", "INTEGER NOT NULL DEFAULT 750"),
                ("proxy_url", "TEXT NOT NULL DEFAULT ''"),
                ("autopilot_status", "TEXT NOT NULL DEFAULT 'idle'"),
                ("autopilot_cooldown_until", "INTEGER NOT NULL DEFAULT 0"),
                ("autopilot_last_error", "TEXT NOT NULL DEFAULT ''"),
                ("apple_auth_expired", "INTEGER NOT NULL DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE accounts ADD COLUMN {col} {col_def};")
                except sqlite3.OperationalError:
                    pass

            # Aliases table
            conn.execute("""
            CREATE TABLE IF NOT EXISTS aliases (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                email TEXT NOT NULL UNIQUE,
                anonymous_id TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                forward_to TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                channel TEXT NOT NULL DEFAULT 'icloud_web',
                origin TEXT NOT NULL DEFAULT 'created',
                created_at INTEGER NOT NULL
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_aliases_acc ON aliases(account_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_aliases_created ON aliases(created_at DESC);")

            # Batch Jobs table
            conn.execute("""
            CREATE TABLE IF NOT EXISTS batch_jobs (
                id TEXT PRIMARY KEY,
                total_requested INTEGER NOT NULL,
                created_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                strategy TEXT NOT NULL DEFAULT 'round_robin',
                target_account_id TEXT NOT NULL DEFAULT '',
                base_label TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                current_account_name TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                auto_pilot INTEGER NOT NULL DEFAULT 1,
                cooldown_resume_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """)

            # Auto-migrate batch_jobs columns if upgrading from earlier version
            for col, col_def in [("auto_pilot", "INTEGER NOT NULL DEFAULT 1"), ("cooldown_resume_at", "INTEGER NOT NULL DEFAULT 0")]:
                try:
                    conn.execute(f"ALTER TABLE batch_jobs ADD COLUMN {col} {col_def};")
                except Exception:
                    pass

            # Batch Items table (for intent tracking and resilience)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS batch_items (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES batch_jobs(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                label TEXT NOT NULL,
                candidate_email TEXT NOT NULL DEFAULT '',
                created_email TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_batch_items_job ON batch_items(job_id, position);")

            # System Logs table
            conn.execute("""
            CREATE TABLE IF NOT EXISTS system_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                account_id TEXT,
                message TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """)

            # Proxy Subscriptions table
            conn.execute("""
            CREATE TABLE IF NOT EXISTS proxy_subscriptions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_created ON system_logs(created_at DESC);")
            conn.commit()

    # ==========================
    # ACCOUNT OPERATIONS
    # ==========================

    def get_account_by_apple_id(self, apple_id: str) -> Optional[AccountModel]:
        if not apple_id:
            return None
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM accounts WHERE LOWER(apple_id) = LOWER(?) LIMIT 1", (apple_id.strip(),))
            row = cur.fetchone()
            if not row:
                return None
            return AccountModel(**dict(row))

    def save_account(self, account: AccountModel) -> None:
        with self._lock, self._get_connection() as conn:
            # Intelligent merge if account with same apple_id exists
            if account.apple_id:
                cur = conn.execute("SELECT * FROM accounts WHERE LOWER(apple_id) = LOWER(?) LIMIT 1", (account.apple_id.strip(),))
                row = cur.fetchone()
                if row:
                    existing = AccountModel(**dict(row))
                    if existing.id != account.id:
                        account.id = existing.id

                    # Merge credentials
                    if not account.dsid or account.dsid == "apple_account":
                        account.dsid = existing.dsid
                    if not account.host or "maildomainws" not in account.host:
                        if existing.host and "maildomainws" in existing.host:
                            account.host = existing.host
                    if not account.scnt and existing.scnt:
                        account.scnt = existing.scnt
                    if not account.api_key and existing.api_key:
                        account.api_key = existing.api_key
                    if not account.selected_forward_to and existing.selected_forward_to:
                        account.selected_forward_to = existing.selected_forward_to
                    if existing.alias_count > 0 and account.alias_count == 0:
                        account.alias_count = existing.alias_count

                    if not account.password and existing.password:
                        account.password = existing.password
                    if not account.code_url and existing.code_url:
                        account.code_url = existing.code_url
                    if not account.proxy_url and existing.proxy_url:
                        account.proxy_url = existing.proxy_url
                    if existing.target_alias_count and not account.target_alias_count:
                        account.target_alias_count = existing.target_alias_count

                    # Merge cookies
                    cookies_dict = {}
                    for c_str in (existing.cookie, account.cookie):
                        for part in c_str.split(";"):
                            if "=" in part:
                                k, v = part.strip().split("=", 1)
                                cookies_dict[k.strip()] = v.strip()
                    account.cookie = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())

            # Mark dual if both channel credentials exist
            has_icloud = bool(account.host and "maildomainws" in account.host)
            has_apple = bool(account.scnt or account.api_key)
            if has_icloud and has_apple:
                account.channel = "dual"

            conn.execute("""
            INSERT INTO accounts (
                id, name, apple_id, dsid, client_id, client_build_number,
                client_mastering_number, host, cookie, origin, referer,
                user_agent, lang_code, channel, scnt, api_key, status,
                cooldown_until, cooldown_stage, apple_cooldown_until, icloud_cooldown_until,
                selected_forward_to, alias_count,
                local_alias_count, last_checked_at, last_error, password, code_url,
                target_alias_count, proxy_url, autopilot_status, autopilot_cooldown_until, autopilot_last_error,
                created_at, updated_at
            ) VALUES (
                :id, :name, :apple_id, :dsid, :client_id, :client_build_number,
                :client_mastering_number, :host, :cookie, :origin, :referer,
                :user_agent, :lang_code, :channel, :scnt, :api_key, :status,
                :cooldown_until, :cooldown_stage, :apple_cooldown_until, :icloud_cooldown_until,
                :selected_forward_to, :alias_count,
                :local_alias_count, :last_checked_at, :last_error, :password, :code_url,
                :target_alias_count, :proxy_url, :autopilot_status, :autopilot_cooldown_until, :autopilot_last_error,
                :created_at, :updated_at
            )
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                apple_id=excluded.apple_id,
                dsid=excluded.dsid,
                client_id=excluded.client_id,
                client_build_number=excluded.client_build_number,
                client_mastering_number=excluded.client_mastering_number,
                host=excluded.host,
                cookie=excluded.cookie,
                origin=excluded.origin,
                referer=excluded.referer,
                user_agent=excluded.user_agent,
                lang_code=excluded.lang_code,
                channel=excluded.channel,
                scnt=excluded.scnt,
                api_key=excluded.api_key,
                status=excluded.status,
                cooldown_until=excluded.cooldown_until,
                cooldown_stage=excluded.cooldown_stage,
                apple_cooldown_until=excluded.apple_cooldown_until,
                icloud_cooldown_until=excluded.icloud_cooldown_until,
                selected_forward_to=excluded.selected_forward_to,
                alias_count=excluded.alias_count,
                local_alias_count=excluded.local_alias_count,
                last_checked_at=excluded.last_checked_at,
                last_error=excluded.last_error,
                password=excluded.password,
                code_url=excluded.code_url,
                target_alias_count=excluded.target_alias_count,
                proxy_url=excluded.proxy_url,
                autopilot_status=excluded.autopilot_status,
                autopilot_cooldown_until=excluded.autopilot_cooldown_until,
                autopilot_last_error=excluded.autopilot_last_error,
                updated_at=excluded.updated_at;
            """, account.__dict__)
            conn.commit()

    def get_account(self, account_id: str) -> Optional[AccountModel]:
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
            row = cur.fetchone()
            if not row:
                return None
            acc = AccountModel(**dict(row))
            cnt_row = conn.execute("SELECT COUNT(*) FROM aliases WHERE account_id = ?", (account_id,)).fetchone()
            db_alias_count = cnt_row[0] if cnt_row else 0
            if db_alias_count > acc.alias_count:
                acc.alias_count = db_alias_count
            if db_alias_count > acc.local_alias_count:
                acc.local_alias_count = db_alias_count
            return acc

    def get_all_accounts(self) -> List[AccountModel]:
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM accounts ORDER BY created_at ASC")
            accounts = [AccountModel(**dict(row)) for row in cur.fetchall()]
            for acc in accounts:
                cnt_row = conn.execute("SELECT COUNT(*) FROM aliases WHERE account_id = ?", (acc.id,)).fetchone()
                db_alias_count = cnt_row[0] if cnt_row else 0
                if db_alias_count > acc.alias_count:
                    acc.alias_count = db_alias_count
                if db_alias_count > acc.local_alias_count:
                    acc.local_alias_count = db_alias_count
            return accounts

    def get_available_accounts(self) -> List[AccountModel]:
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM accounts WHERE status IN ('active', 'cooling_down') ORDER BY created_at ASC")
            accs = [AccountModel(**dict(row)) for row in cur.fetchall()]
            available = []
            for a in accs:
                if not a.is_cooling_down:
                    if a.status == "cooling_down":
                        conn.execute("UPDATE accounts SET status = 'active', cooldown_until = 0, last_error = '' WHERE id = ?", (a.id,))
                        a.status = "active"
                    available.append(a)
            conn.commit()
            return available

    def delete_account(self, account_id: str) -> bool:
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            conn.commit()
            return cur.rowcount > 0

    def update_account_status(self, account_id: str, status: str, last_error: str = "") -> None:
        now = int(time.time())
        with self._lock, self._get_connection() as conn:
            conn.execute("""
            UPDATE accounts
            SET status = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """, (status, last_error, now, account_id))
            conn.commit()

    def set_account_cooldown(self, account_id: str, cooldown_seconds: int, stage: int, last_error: str = "") -> None:
        now = int(time.time())
        until = now + cooldown_seconds
        with self._lock, self._get_connection() as conn:
            conn.execute("""
            UPDATE accounts
            SET status = 'cooling_down', cooldown_until = ?, cooldown_stage = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """, (until, stage, last_error, now, account_id))
            conn.commit()

    def set_channel_cooldown(self, account_id: str, channel: str, cooldown_seconds: int, last_error: str = "") -> None:
        now = int(time.time())
        until = now + cooldown_seconds
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
            row = cur.fetchone()
            if not row:
                return
            acc = AccountModel(**dict(row))

            apple_until = until if channel == "apple_account" else acc.apple_cooldown_until
            icloud_until = until if channel == "icloud_web" else acc.icloud_cooldown_until

            if acc.channel == "dual":
                both_cooling = (apple_until > now) and (icloud_until > now)
                account_cooldown = min(apple_until, icloud_until) if both_cooling else 0
                status = "cooling_down" if both_cooling else "active"
            elif channel == "apple_account":
                account_cooldown = apple_until
                status = "cooling_down"
            else:
                account_cooldown = icloud_until
                status = "cooling_down"

            conn.execute("""
            UPDATE accounts
            SET status = ?, cooldown_until = ?, apple_cooldown_until = ?, icloud_cooldown_until = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """, (status, account_cooldown, apple_until, icloud_until, last_error, now, account_id))
            conn.commit()

    def clear_account_cooldown(self, account_id: str) -> None:
        now = int(time.time())
        with self._lock, self._get_connection() as conn:
            conn.execute("""
            UPDATE accounts
            SET status = 'active', cooldown_until = 0, cooldown_stage = 0,
                apple_cooldown_until = 0, icloud_cooldown_until = 0,
                last_error = '', updated_at = ?
            WHERE id = ?
            """, (now, account_id))
            conn.commit()

    def set_apple_auth_expired(self, account_id: str, expired: bool = True, last_error: str = "") -> None:
        now = int(time.time())
        with self._lock, self._get_connection() as conn:
            conn.execute("""
            UPDATE accounts
            SET apple_auth_expired = ?, apple_cooldown_until = 0, last_error = ?, updated_at = ?
            WHERE id = ?
            """, (1 if expired else 0, last_error, now, account_id))
            conn.commit()

    def update_account_autopilot_status(
        self, account_id: str, status: str, cooldown_until: int = 0, last_error: str = ""
    ) -> None:
        now = int(time.time())
        with self._lock, self._get_connection() as conn:
            conn.execute("""
            UPDATE accounts
            SET autopilot_status = ?, autopilot_cooldown_until = ?, autopilot_last_error = ?, updated_at = ?
            WHERE id = ?
            """, (status, cooldown_until, last_error, now, account_id))
            conn.commit()

    def set_account_proxy(self, account_id: str, proxy_url: str) -> None:
        now = int(time.time())
        with self._lock, self._get_connection() as conn:
            conn.execute("""
            UPDATE accounts
            SET proxy_url = ?, updated_at = ?
            WHERE id = ?
            """, (proxy_url.strip(), now, account_id))
            conn.commit()

    def set_account_target(self, account_id: str, target_count: int) -> None:
        now = int(time.time())
        with self._lock, self._get_connection() as conn:
            conn.execute("""
            UPDATE accounts
            SET target_alias_count = ?, updated_at = ?
            WHERE id = ?
            """, (target_count, now, account_id))
            conn.commit()

    def update_account_profile(
        self,
        account_id: str,
        name: str,
        apple_id: str,
        password: str,
        code_url: str,
        proxy_url: str,
        target_alias_count: int,
        cookie: Optional[str] = None,
        host: Optional[str] = None,
        dsid: Optional[str] = None,
        scnt: Optional[str] = None,
        api_key: Optional[str] = None,
        channel: Optional[str] = None,
        clear_expired: bool = False,
    ) -> bool:
        now = int(time.time())
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
            row = cur.fetchone()
            if not row:
                return False
            acc_dict = dict(row)

            updates: Dict[str, Any] = {
                "name": name.strip() or acc_dict["name"],
                "apple_id": apple_id.strip() or acc_dict["apple_id"],
                "password": password.strip() if password.strip() else acc_dict.get("password", ""),
                "code_url": code_url.strip() if code_url.strip() else acc_dict.get("code_url", ""),
                "proxy_url": proxy_url.strip(),
                "target_alias_count": target_alias_count if target_alias_count > 0 else acc_dict.get("target_alias_count", 750),
                "updated_at": now,
            }
            if cookie is not None:
                updates["cookie"] = cookie
            if host is not None:
                updates["host"] = host
            if dsid is not None:
                updates["dsid"] = dsid
            if scnt is not None:
                updates["scnt"] = scnt
            if api_key is not None:
                updates["api_key"] = api_key
            if channel is not None:
                updates["channel"] = channel
            if clear_expired:
                updates["apple_auth_expired"] = 0
                updates["apple_cooldown_until"] = 0
                updates["status"] = "active"
                updates["last_error"] = ""

            set_clauses = [f"{k} = ?" for k in updates.keys()]
            values = list(updates.values()) + [account_id]
            conn.execute(f"UPDATE accounts SET {', '.join(set_clauses)} WHERE id = ?", values)
            conn.commit()
            return True

    def update_account_check_result(
        self, account_id: str, alias_count: int, selected_forward_to: str, status: str = "active", error: str = ""
    ) -> None:
        now = int(time.time())
        with self._lock, self._get_connection() as conn:
            conn.execute("""
            UPDATE accounts
            SET alias_count = ?, selected_forward_to = ?, status = ?, last_error = ?, last_checked_at = ?, updated_at = ?
            WHERE id = ?
            """, (alias_count, selected_forward_to, status, error, now, now, account_id))
            conn.commit()

    def increment_account_local_alias_count(self, account_id: str) -> None:
        now = int(time.time())
        with self._lock, self._get_connection() as conn:
            conn.execute("""
            UPDATE accounts
            SET local_alias_count = local_alias_count + 1,
                alias_count = alias_count + 1,
                updated_at = ?
            WHERE id = ?
            """, (now, account_id))
            conn.commit()

    def set_apple_auth_expired(self, account_id: str, expired: bool = True, last_error: str = "") -> None:
        now = int(time.time())
        with self._lock, self._get_connection() as conn:
            conn.execute("""
            UPDATE accounts
            SET apple_auth_expired = ?, apple_cooldown_until = 0, last_error = ?, updated_at = ?
            WHERE id = ?
            """, (1 if expired else 0, last_error, now, account_id))
            conn.commit()

    # ==========================
    # ALIAS OPERATIONS
    # ==========================

    def get_account_alias_count(self, account_id: str) -> int:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM aliases WHERE account_id = ?", (account_id,))
            row = cur.fetchone()
            return row[0] if row else 0

    def save_alias(self, alias: AliasModel) -> bool:
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("""
            INSERT INTO aliases (
                id, account_id, email, anonymous_id, label, note, forward_to, is_active, channel, origin, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                anonymous_id=excluded.anonymous_id,
                label=excluded.label,
                note=excluded.note,
                forward_to=excluded.forward_to,
                is_active=excluded.is_active;
            """, (
                alias.id, alias.account_id, alias.email, alias.anonymous_id,
                alias.label, alias.note, alias.forward_to,
                1 if alias.is_active else 0, alias.channel, alias.origin, alias.created_at
            ))
            conn.commit()
            return cur.rowcount > 0

    def get_aliases(
        self, account_id: Optional[str] = None, search: Optional[str] = None, limit: int = 50, offset: int = 0
    ) -> Tuple[List[dict], int]:
        with self._lock, self._get_connection() as conn:
            query = """
            SELECT a.*, acc.name as account_name, acc.apple_id
            FROM aliases a
            LEFT JOIN accounts acc ON a.account_id = acc.id
            WHERE 1=1
            """
            params: List[Any] = []

            if account_id:
                query += " AND a.account_id = ?"
                params.append(account_id)

            if search:
                query += " AND (a.email LIKE ? OR a.label LIKE ? OR a.note LIKE ?)"
                term = f"%{search}%"
                params.extend([term, term, term])

            # Count total
            count_query = f"SELECT COUNT(*) FROM ({query})"
            total = conn.execute(count_query, params).fetchone()[0]

            # Paginate
            query += " ORDER BY a.created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows], total

    def delete_alias(self, alias_id: str) -> Optional[dict]:
        with self._lock, self._get_connection() as conn:
            row = conn.execute("SELECT * FROM aliases WHERE id = ?", (alias_id,)).fetchone()
            if not row:
                return None
            conn.execute("DELETE FROM aliases WHERE id = ?", (alias_id,))
            conn.commit()
            return dict(row)

    def set_alias_status(self, alias_id: str, is_active: bool) -> None:
        with self._lock, self._get_connection() as conn:
            conn.execute("UPDATE aliases SET is_active = ? WHERE id = ?", (1 if is_active else 0, alias_id))
            conn.commit()

    def get_all_alias_emails(self, account_id: Optional[str] = None) -> List[str]:
        with self._lock, self._get_connection() as conn:
            if account_id:
                cur = conn.execute("SELECT email FROM aliases WHERE account_id = ? ORDER BY created_at DESC", (account_id,))
            else:
                cur = conn.execute("SELECT email FROM aliases ORDER BY created_at DESC")
            return [r[0] for r in cur.fetchall()]

    # ==========================
    # BATCH JOB OPERATIONS
    # ==========================

    def create_batch_job(self, job: BatchJobModel) -> None:
        with self._lock, self._get_connection() as conn:
            d = dict(job.__dict__)
            d["auto_pilot"] = 1 if d.get("auto_pilot", True) else 0
            d["cooldown_resume_at"] = int(d.get("cooldown_resume_at", 0))
            conn.execute("""
            INSERT INTO batch_jobs (
                id, total_requested, created_count, failed_count, strategy,
                target_account_id, base_label, note, status, current_account_name,
                last_error, auto_pilot, cooldown_resume_at, created_at, updated_at
            ) VALUES (
                :id, :total_requested, :created_count, :failed_count, :strategy,
                :target_account_id, :base_label, :note, :status, :current_account_name,
                :last_error, :auto_pilot, :cooldown_resume_at, :created_at, :updated_at
            )
            """, d)
            conn.commit()

    def _row_to_batch_job(self, row: Any) -> BatchJobModel:
        d = dict(row)
        d["auto_pilot"] = bool(d.get("auto_pilot", 1))
        d["cooldown_resume_at"] = int(d.get("cooldown_resume_at", 0))
        return BatchJobModel(**d)

    def get_batch_job(self, job_id: str) -> Optional[BatchJobModel]:
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM batch_jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_batch_job(row)

    def get_active_batch_job(self) -> Optional[BatchJobModel]:
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("""
            SELECT * FROM batch_jobs
            WHERE status IN ('queued', 'running', 'paused', 'waiting_cooldown')
            ORDER BY created_at DESC LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_batch_job(row)

    def update_batch_job(self, job: BatchJobModel) -> None:
        job.updated_at = int(time.time())
        with self._lock, self._get_connection() as conn:
            d = dict(job.__dict__)
            d["auto_pilot"] = 1 if d.get("auto_pilot", True) else 0
            d["cooldown_resume_at"] = int(d.get("cooldown_resume_at", 0))
            conn.execute("""
            UPDATE batch_jobs
            SET created_count = :created_count,
                failed_count = :failed_count,
                status = :status,
                current_account_name = :current_account_name,
                last_error = :last_error,
                auto_pilot = :auto_pilot,
                cooldown_resume_at = :cooldown_resume_at,
                updated_at = :updated_at
            WHERE id = :id
            """, d)
            conn.commit()

    def add_batch_item(
        self, item_id: str, job_id: str, position: int, account_id: str, label: str
    ) -> None:
        now = int(time.time())
        with self._lock, self._get_connection() as conn:
            conn.execute("""
            INSERT INTO batch_items (id, job_id, position, account_id, label, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (item_id, job_id, position, account_id, label, now, now))
            conn.commit()

    def update_batch_item(
        self,
        item_id: str,
        status: str,
        candidate: Optional[str] = None,
        created_email: Optional[str] = None,
        error: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        now = int(time.time())
        err = error if error is not None else error_message
        updates = ["status = ?", "updated_at = ?"]
        params: list = [status, now]
        if candidate is not None:
            updates.append("candidate_email = ?")
            params.append(candidate)
        if created_email is not None:
            updates.append("created_email = ?")
            params.append(created_email)
        if err is not None:
            updates.append("error_message = ?")
            params.append(err)
        params.append(item_id)
        sql = f"UPDATE batch_items SET {', '.join(updates)} WHERE id = ?"
        with self._lock, self._get_connection() as conn:
            conn.execute(sql, tuple(params))
            conn.commit()

    def get_job_items(self, job_id: str) -> List[dict]:
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("""
            SELECT bi.*, acc.name as account_name
            FROM batch_items bi
            LEFT JOIN accounts acc ON bi.account_id = acc.id
            WHERE bi.job_id = ?
            ORDER BY bi.position ASC
            """, (job_id,))
            return [dict(r) for r in cur.fetchall()]

    # ==========================
    # LOGS & STATS
    # ==========================

    def add_log(self, level: str, message: str, account_id: Optional[str] = None) -> None:
        now = int(time.time())
        with self._lock, self._get_connection() as conn:
            conn.execute("""
            INSERT INTO system_logs (level, account_id, message, created_at)
            VALUES (?, ?, ?, ?)
            """, (level, account_id, message, now))
            conn.commit()

    def get_logs(self, limit: int = 100) -> List[dict]:
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("""
            SELECT l.*, acc.name as account_name
            FROM system_logs l
            LEFT JOIN accounts acc ON l.account_id = acc.id
            ORDER BY l.id DESC
            LIMIT ?
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]

    def get_summary_stats(self) -> dict:
        now = int(time.time())
        with self._lock, self._get_connection() as conn:
            total_accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            active_accounts = conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE status = 'active' AND cooldown_until <= ?", (now,)
            ).fetchone()[0]
            cooling_down = conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE cooldown_until > ?", (now,)
            ).fetchone()[0]
            total_aliases = conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]
            today_aliases = conn.execute(
                "SELECT COUNT(*) FROM aliases WHERE created_at >= ?", (now - 86400,)
            ).fetchone()[0]
            return {
                "total_accounts": total_accounts,
                "active_accounts": active_accounts,
                "cooling_down_accounts": cooling_down,
                "total_aliases": total_aliases,
                "today_aliases": today_aliases,
            }

    # ==========================
    # PROXY SUBSCRIPTIONS
    # ==========================

    def save_subscription(self, sub_id: str, name: str, url: str) -> None:
        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO proxy_subscriptions (id, name, url, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name = excluded.name, url = excluded.url;
                """,
                (sub_id, name, url, int(time.time())),
            )

    def delete_subscription(self, sub_id: str) -> bool:
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("DELETE FROM proxy_subscriptions WHERE id = ?", (sub_id,))
            return cur.rowcount > 0

    def get_all_subscriptions(self) -> List[Dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM proxy_subscriptions ORDER BY created_at DESC")
            return [dict(r) for r in cur.fetchall()]


# Singleton database instance
db = Database()
