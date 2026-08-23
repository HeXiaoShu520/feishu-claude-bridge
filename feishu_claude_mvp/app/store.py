from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Conversation:
    key: str
    session_id: str | None
    mode: str
    cwd: str
    chat_id: str = ""
    user_open_id: str = ""


class ConversationStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS conversations (
                    key TEXT PRIMARY KEY,
                    session_id TEXT,
                    mode TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    chat_id TEXT NOT NULL DEFAULT '',
                    user_open_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS session_permissions (
                    conversation_key TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    PRIMARY KEY (conversation_key, tool_name)
                );
                """
            )
            self._ensure_columns(conn)

    def get(self, key: str) -> Conversation | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT key, session_id, mode, cwd, chat_id, user_open_id FROM conversations WHERE key = ?",
                (key,),
            ).fetchone()
        return Conversation(*row) if row else None

    def save(self, conversation: Conversation) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations (key, session_id, mode, cwd, chat_id, user_open_id)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    session_id = excluded.session_id,
                    mode = excluded.mode,
                    cwd = excluded.cwd,
                    chat_id = excluded.chat_id,
                    user_open_id = excluded.user_open_id
                """,
                (
                    conversation.key,
                    conversation.session_id,
                    conversation.mode,
                    conversation.cwd,
                    conversation.chat_id,
                    conversation.user_open_id,
                ),
            )

    def find_session_by_suffix(self, suffix: str, chat_id: str, user_open_id: str) -> str | None:
        """按当前用户会话的最后八位查找完整 Claude session ID。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT session_id FROM conversations WHERE chat_id = ? AND user_open_id = ? AND session_id LIKE ?",
                (chat_id, user_open_id, f"%{suffix}"),
            ).fetchone()
        return row[0] if row else None

    def clear_session(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE conversations SET session_id = NULL WHERE key = ?", (key,))
            conn.execute("DELETE FROM session_permissions WHERE conversation_key = ?", (key,))

    def grant_permission(self, key: str, tool_name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO session_permissions (conversation_key, tool_name) VALUES (?, ?)",
                (key, tool_name),
            )

    def has_permission(self, key: str, tool_name: str) -> bool:
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM session_permissions WHERE conversation_key = ? AND tool_name = ?",
                (key, tool_name),
            ).fetchone() is not None

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
        for name in ("chat_id", "user_open_id"):
            if name not in columns:
                conn.execute(f"ALTER TABLE conversations ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
