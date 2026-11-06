from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_EVENTS = {
    "issue",
    "job",
    "merge_request",
    "note",
    "pipeline",
    "push",
    "release",
    "tag_push",
}


def hash_link_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


class Database:
    def __init__(self, path: str):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS repositories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repository_key TEXT NOT NULL UNIQUE,
                    project_path TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    secret_hash TEXT NOT NULL,
                    shared_chat_id INTEGER,
                    shared_thread_id INTEGER,
                    enabled_events TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_links (
                    gitlab_username TEXT PRIMARY KEY,
                    gitlab_user_id TEXT,
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    telegram_username TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    linked_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS link_codes (
                    code_hash TEXT PRIMARY KEY,
                    gitlab_username TEXT NOT NULL,
                    gitlab_user_id TEXT,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );

                CREATE TABLE IF NOT EXISTS deliveries (
                    event_id TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    PRIMARY KEY (event_id, target_key)
                );
                """
            )
            repository_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(repositories)")
            }
            if "shared_thread_id" not in repository_columns:
                connection.execute(
                    "ALTER TABLE repositories ADD COLUMN shared_thread_id INTEGER"
                )

    @staticmethod
    def normalize_username(value: str) -> str:
        return value.strip().lstrip("@").casefold()

    @staticmethod
    def normalize_project_path(value: str) -> str:
        return value.strip().strip("/").casefold()

    def add_repository(
        self,
        repository_key: str,
        project_path: str,
        display_name: str,
        secret_hash: str,
        shared_chat_id: int | None,
        shared_thread_id: int | None = None,
        enabled_events: set[str] | None = None,
    ) -> sqlite3.Row:
        now = datetime.now(UTC).isoformat()
        events = sorted(enabled_events or DEFAULT_EVENTS)
        normalized_path = self.normalize_project_path(project_path)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO repositories (
                    repository_key, project_path, display_name, secret_hash,
                    shared_chat_id, shared_thread_id, enabled_events, active,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    repository_key,
                    normalized_path,
                    display_name,
                    secret_hash,
                    shared_chat_id,
                    shared_thread_id,
                    json.dumps(events),
                    now,
                ),
            )
            return connection.execute(
                "SELECT * FROM repositories WHERE repository_key = ?",
                (repository_key,),
            ).fetchone()

    def get_repository(self, repository_key: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM repositories WHERE repository_key = ?",
                (repository_key,),
            ).fetchone()

    def list_repositories(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM repositories ORDER BY created_at"
            ).fetchall()

    def set_repository_active(self, repository_key: str, active: bool) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE repositories SET active = ? WHERE repository_key = ?",
                (int(active), repository_key),
            )
            return cursor.rowcount > 0

    def set_repository_topic(
        self,
        repository_key: str,
        shared_chat_id: int,
        shared_thread_id: int | None,
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE repositories
                SET shared_chat_id = ?, shared_thread_id = ?
                WHERE repository_key = ?
                """,
                (shared_chat_id, shared_thread_id, repository_key),
            )
            return cursor.rowcount > 0

    def set_repository_events(self, repository_key: str, events: set[str]) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE repositories SET enabled_events = ? WHERE repository_key = ?",
                (json.dumps(sorted(events)), repository_key),
            )
            return cursor.rowcount > 0

    def repository_events(self, repository: sqlite3.Row) -> set[str]:
        return set(json.loads(repository["enabled_events"]))

    def create_link_code(
        self,
        code: str,
        gitlab_username: str,
        ttl_minutes: int,
        gitlab_user_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO link_codes (
                    code_hash, gitlab_username, gitlab_user_id, expires_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    hash_link_code(code),
                    self.normalize_username(gitlab_username),
                    gitlab_user_id,
                    (now + timedelta(minutes=ttl_minutes)).isoformat(),
                ),
            )

    def redeem_link_code(
        self,
        code: str,
        telegram_chat_id: int,
        telegram_user_id: int,
        telegram_username: str | None,
    ) -> str | None:
        now = datetime.now(UTC)
        code_hash = hash_link_code(code)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM link_codes WHERE code_hash = ?", (code_hash,)
            ).fetchone()
            if (
                not row
                or row["used_at"]
                or datetime.fromisoformat(row["expires_at"]) <= now
            ):
                return None
            username = row["gitlab_username"]
            connection.execute(
                """
                INSERT INTO user_links (
                    gitlab_username, gitlab_user_id, telegram_chat_id,
                    telegram_user_id, telegram_username, active, linked_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(gitlab_username) DO UPDATE SET
                    gitlab_user_id = COALESCE(
                        excluded.gitlab_user_id, user_links.gitlab_user_id
                    ),
                    telegram_chat_id = excluded.telegram_chat_id,
                    telegram_user_id = excluded.telegram_user_id,
                    telegram_username = excluded.telegram_username,
                    active = 1,
                    linked_at = excluded.linked_at
                """,
                (
                    username,
                    row["gitlab_user_id"],
                    telegram_chat_id,
                    telegram_user_id,
                    telegram_username,
                    now.isoformat(),
                ),
            )
            connection.execute(
                "UPDATE link_codes SET used_at = ? WHERE code_hash = ?",
                (now.isoformat(), code_hash),
            )
            return username

    def get_active_link(
        self, username: str | None = None, user_id: str | None = None
    ) -> sqlite3.Row | None:
        with self.connect() as connection:
            if user_id:
                row = connection.execute(
                    """
                    SELECT * FROM user_links
                    WHERE gitlab_user_id = ? AND active = 1
                    """,
                    (str(user_id),),
                ).fetchone()
                if row:
                    return row
            if username:
                return connection.execute(
                    """
                    SELECT * FROM user_links
                    WHERE gitlab_username = ? AND active = 1
                    """,
                    (self.normalize_username(username),),
                ).fetchone()
        return None

    def get_link_by_chat(self, chat_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM user_links WHERE telegram_chat_id = ?", (chat_id,)
            ).fetchone()

    def list_active_links(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM user_links WHERE active = 1"
            ).fetchall()

    def set_link_active(self, chat_id: int, active: bool) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE user_links SET active = ? WHERE telegram_chat_id = ?",
                (int(active), chat_id),
            )
            return cursor.rowcount > 0

    def claim_delivery(self, event_id: str, target_key: str) -> bool:
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO deliveries (
                        event_id, target_key, status, created_at
                    ) VALUES (?, ?, 'pending', ?)
                    """,
                    (event_id, target_key, datetime.now(UTC).isoformat()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def mark_delivery_sent(self, event_id: str, target_key: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE deliveries SET status = 'sent', sent_at = ?
                WHERE event_id = ? AND target_key = ?
                """,
                (datetime.now(UTC).isoformat(), event_id, target_key),
            )

    def release_delivery(self, event_id: str, target_key: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                DELETE FROM deliveries
                WHERE event_id = ? AND target_key = ? AND status = 'pending'
                """,
                (event_id, target_key),
            )
