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
                selected_forward_to TEXT NOT NULL DEFAULT '',
                alias_count INTEGER NOT NULL DEFAULT 0,
                local_alias_count INTEGER NOT NULL DEFAULT 0,
                last_checked_at INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """)

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
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """)

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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_created ON system_logs(created_at DESC);")
            conn.commit()

    # ==========================
    # ACCOUNT OPERATIONS
    # ==========================

    def save_account(self, account: AccountModel) -> None:
        with self._lock, self._get_connection() as conn:
            conn.execute("""
            INSERT INTO accounts (
                id, name, apple_id, dsid, client_id, client_build_number,
                client_mastering_number, host, cookie, origin, referer,
                user_agent, lang_code, channel, scnt, api_key, status,
                cooldown_until, cooldown_stage, selected_forward_to, alias_count,
                local_alias_count, last_checked_at, last_error, created_at, updated_at
            ) VALUES (
                :id, :name, :apple_id, :dsid, :client_id, :client_build_number,
                :client_mastering_number, :host, :cookie, :origin, :referer,
                :user_agent, :lang_code, :channel, :scnt, :api_key, :status,
                :cooldown_until, :cooldown_stage, :selected_forward_to, :alias_count,
                :local_alias_count, :last_checked_at, :last_error, :created_at, :updated_at
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
                selected_forward_to=excluded.selected_forward_to,
                alias_count=excluded.alias_count,
                local_alias_count=excluded.local_alias_count,
                last_checked_at=excluded.last_checked_at,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at;
            """, account.__dict__)
            conn.commit()

    def get_account(self, account_id: str) -> Optional[AccountModel]:
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
            row = cur.fetchone()
            if not row:
                return None
            return AccountModel(**dict(row))

    def get_all_accounts(self) -> List[AccountModel]:
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM accounts ORDER BY created_at ASC")
            return [AccountModel(**dict(row)) for row in cur.fetchall()]

    def get_available_accounts(self) -> List[AccountModel]:
        now = int(time.time())
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("""
            SELECT * FROM accounts
            WHERE status = 'active' AND cooldown_until <= ?
            ORDER BY cooldown_until ASC, created_at ASC
            """, (now,))
            return [AccountModel(**dict(row)) for row in cur.fetchall()]

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

    def clear_account_cooldown(self, account_id: str) -> None:
        now = int(time.time())
        with self._lock, self._get_connection() as conn:
            conn.execute("""
            UPDATE accounts
            SET status = 'active', cooldown_until = 0, cooldown_stage = 0, last_error = '', updated_at = ?
            WHERE id = ?
            """, (now, account_id))
            conn.commit()

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

    # ==========================
    # ALIAS OPERATIONS
    # ==========================

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
            conn.execute("""
            INSERT INTO batch_jobs (
                id, total_requested, created_count, failed_count, strategy,
                target_account_id, base_label, note, status, current_account_name,
                last_error, created_at, updated_at
            ) VALUES (
                :id, :total_requested, :created_count, :failed_count, :strategy,
                :target_account_id, :base_label, :note, :status, :current_account_name,
                :last_error, :created_at, :updated_at
            )
            """, job.__dict__)
            conn.commit()

    def get_batch_job(self, job_id: str) -> Optional[BatchJobModel]:
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM batch_jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
            if not row:
                return None
            return BatchJobModel(**dict(row))

    def get_active_batch_job(self) -> Optional[BatchJobModel]:
        with self._lock, self._get_connection() as conn:
            cur = conn.execute("""
            SELECT * FROM batch_jobs
            WHERE status IN ('queued', 'running', 'paused')
            ORDER BY created_at DESC LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return None
            return BatchJobModel(**dict(row))

    def update_batch_job(self, job: BatchJobModel) -> None:
        job.updated_at = int(time.time())
        with self._lock, self._get_connection() as conn:
            conn.execute("""
            UPDATE batch_jobs
            SET created_count = :created_count,
                failed_count = :failed_count,
                status = :status,
                current_account_name = :current_account_name,
                last_error = :last_error,
                updated_at = :updated_at
            WHERE id = :id
            """, job.__dict__)
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
        self, item_id: str, status: str, candidate: str = "", created_email: str = "", error: str = ""
    ) -> None:
        now = int(time.time())
        with self._lock, self._get_connection() as conn:
            conn.execute("""
            UPDATE batch_items
            SET status = ?, candidate_email = ?, created_email = ?, error_message = ?, updated_at = ?
            WHERE id = ?
            """, (status, candidate, created_email, error, now, item_id))
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


# Singleton database instance
db = Database()
