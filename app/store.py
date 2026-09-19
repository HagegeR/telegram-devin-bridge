from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Conversation:
    conv_key: str
    chat_id: int
    thread_id: int | None
    session_id: str
    session_url: str
    title: str
    last_event_id: str | None
    created_at: float
    last_user_text: str | None
    last_pr_url: str | None
    updated_at: float


@dataclass(frozen=True)
class HistoryEntry:
    id: int
    conv_key: str
    session_id: str
    session_url: str
    title: str
    created_at: float


class Store:
    def __init__(self, database_path: str) -> None:
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._initialize()

    @staticmethod
    def conv_key(
        chat_id: int,
        thread_id: int | None = None,
        *,
        is_forum: bool = False,
    ) -> str:
        if is_forum and thread_id is not None:
            return f"{chat_id}:{thread_id}"
        return str(chat_id)

    def _initialize(self) -> None:
        with self.lock, self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conv_key TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    session_id TEXT NOT NULL,
                    session_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    last_event_id TEXT,
                    created_at REAL NOT NULL,
                    last_user_text TEXT,
                    last_pr_url TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conv_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    session_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS processed_updates (
                    update_id INTEGER PRIMARY KEY,
                    seen_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_choices (
                    choice_id TEXT PRIMARY KEY,
                    conv_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    option_text TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    message_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS long_texts (
                    token TEXT PRIMARY KEY,
                    conv_key TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row["name"])
                for row in self.connection.execute(
                    "PRAGMA table_info(conversations)"
                )
            }
            if "last_user_text" not in columns:
                self.connection.execute(
                    "ALTER TABLE conversations ADD COLUMN last_user_text TEXT"
                )
            if "last_pr_url" not in columns:
                self.connection.execute(
                    "ALTER TABLE conversations ADD COLUMN last_pr_url TEXT"
                )
            if "updated_at" not in columns:
                self.connection.execute(
                    "ALTER TABLE conversations ADD COLUMN updated_at REAL"
                )
                self.connection.execute(
                    "UPDATE conversations SET updated_at = created_at "
                    "WHERE updated_at IS NULL"
                )
            choice_columns = {
                str(row["name"])
                for row in self.connection.execute(
                    "PRAGMA table_info(pending_choices)"
                )
            }
            if "chat_id" not in choice_columns:
                self.connection.execute(
                    "ALTER TABLE pending_choices ADD COLUMN chat_id INTEGER"
                )
            if "message_id" not in choice_columns:
                self.connection.execute(
                    "ALTER TABLE pending_choices ADD COLUMN message_id INTEGER"
                )
            legacy_table = self.connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'chat_sessions'
                """
            ).fetchone()
            if legacy_table is not None:
                self._migrate_legacy_sessions()

    def _migrate_legacy_sessions(self) -> None:
        rows = self.connection.execute(
            """
            SELECT chat_id, devin_session_id, last_message_id
            FROM chat_sessions
            """
        ).fetchall()
        for row in rows:
            chat_id = int(row["chat_id"])
            session_id = str(row["devin_session_id"])
            conv_key = str(chat_id)
            session_url = (
                "https://app.devin.ai/sessions/"
                f"{session_id.removeprefix('devin-')}"
            )
            timestamp = time.time()
            exists = self.connection.execute(
                "SELECT 1 FROM conversations WHERE conv_key = ?",
                (conv_key,),
            ).fetchone()
            if exists is None:
                self.connection.execute(
                    """
                    INSERT INTO conversations(
                        conv_key, chat_id, thread_id, session_id, session_url,
                        title, last_event_id, created_at, last_user_text, updated_at
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        conv_key,
                        chat_id,
                        session_id,
                        session_url,
                        "Telegram conversation",
                        row["last_message_id"],
                        timestamp,
                        timestamp,
                    ),
                )
            self.connection.execute(
                """
                INSERT INTO session_history(
                    conv_key, session_id, session_url, title, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    conv_key,
                    session_id,
                    session_url,
                    "Telegram conversation",
                    timestamp,
                ),
            )
        self.connection.execute("DROP TABLE chat_sessions")

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def get_conversation(self, conv_key: str) -> Conversation | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM conversations WHERE conv_key = ?",
                (conv_key,),
            ).fetchone()
        return self._conversation(row)

    def save_conversation(
        self,
        *,
        conv_key: str,
        chat_id: int,
        thread_id: int | None,
        session_id: str,
        session_url: str,
        title: str,
        last_event_id: str | None = None,
        last_user_text: str | None = None,
        last_pr_url: str | None = None,
        created_at: float | None = None,
    ) -> None:
        timestamp = time.time() if created_at is None else created_at
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM pending_choices WHERE conv_key = ?",
                (conv_key,),
            )
            self.connection.execute(
                """
                INSERT INTO conversations(
                    conv_key, chat_id, thread_id, session_id, session_url,
                    title, last_event_id, created_at, last_user_text, last_pr_url,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conv_key) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    thread_id = excluded.thread_id,
                    session_id = excluded.session_id,
                    session_url = excluded.session_url,
                    title = excluded.title,
                    last_event_id = excluded.last_event_id,
                    last_user_text = excluded.last_user_text,
                    last_pr_url = excluded.last_pr_url,
                    updated_at = excluded.updated_at
                """,
                (
                    conv_key,
                    chat_id,
                    thread_id,
                    session_id,
                    session_url,
                    title,
                    last_event_id,
                    timestamp,
                    last_user_text,
                    last_pr_url,
                    timestamp,
                ),
            )

    def update_conversation(
        self,
        conv_key: str,
        session_id: str,
        *,
        last_event_id: str | None = None,
        last_user_text: str | None = None,
        last_pr_url: str | None = None,
    ) -> None:
        assignments: list[str] = []
        values: list[str | None] = []
        if last_event_id is not None:
            assignments.append("last_event_id = ?")
            values.append(last_event_id)
        if last_user_text is not None:
            assignments.append("last_user_text = ?")
            values.append(last_user_text)
        if last_pr_url is not None:
            assignments.append("last_pr_url = ?")
            values.append(last_pr_url)
        if not assignments:
            return
        values.append(time.time())
        assignments.append("updated_at = ?")
        values.extend((conv_key, session_id))
        with self.lock, self.connection:
            self.connection.execute(
                f"UPDATE conversations SET {', '.join(assignments)} "
                "WHERE conv_key = ? AND session_id = ?",
                values,
            )

    def clear_conversation(self, conv_key: str, session_id: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM conversations WHERE conv_key = ? AND session_id = ?",
                (conv_key, session_id),
            )

    def get_conversation_for_chat(self, chat_id: int) -> Conversation | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM conversations WHERE chat_id = ? "
                "ORDER BY updated_at DESC, created_at DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        return self._conversation(row)

    def add_history(
        self,
        *,
        conv_key: str,
        session_id: str,
        session_url: str,
        title: str,
        created_at: float | None = None,
    ) -> int:
        timestamp = time.time() if created_at is None else created_at
        with self.lock, self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO session_history(
                    conv_key, session_id, session_url, title, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (conv_key, session_id, session_url, title, timestamp),
            )
            return int(cursor.lastrowid)

    def list_history(self, conv_key: str, limit: int = 10) -> list[HistoryEntry]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT id, conv_key, session_id, session_url, title, created_at
                FROM session_history
                WHERE conv_key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (conv_key, limit),
            ).fetchall()
        return [
            HistoryEntry(
                id=int(row["id"]),
                conv_key=str(row["conv_key"]),
                session_id=str(row["session_id"]),
                session_url=str(row["session_url"]),
                title=str(row["title"]),
                created_at=float(row["created_at"]),
            )
            for row in rows
        ]

    def mark_update_seen(self, update_id: int) -> bool:
        cutoff = time.time() - 86400
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM processed_updates WHERE seen_at < ?",
                (cutoff,),
            )
            try:
                self.connection.execute(
                    "INSERT INTO processed_updates(update_id, seen_at) VALUES (?, ?)",
                    (update_id, time.time()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def add_choice(
        self,
        choice_id: str,
        conv_key: str,
        session_id: str,
        chat_id: int,
        option_text: str,
        message_id: int | None = None,
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO pending_choices(
                    choice_id, conv_key, session_id, chat_id, option_text,
                    created_at, message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    choice_id,
                    conv_key,
                    session_id,
                    chat_id,
                    option_text,
                    time.time(),
                    message_id,
                ),
            )

    def get_choice(self, choice_id: str) -> tuple[str, str, int, str] | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT conv_key, session_id, chat_id, option_text
                FROM pending_choices WHERE choice_id = ?
                """,
                (choice_id,),
            ).fetchone()
        if row is None:
            return None
        chat_id = row["chat_id"]
        if not isinstance(chat_id, int):
            return None
        return (
            str(row["conv_key"]),
            str(row["session_id"]),
            chat_id,
            str(row["option_text"]),
        )

    def list_choices(
        self,
        conv_key: str,
        message_id: int | None = None,
    ) -> list[tuple[str, str]]:
        where = "conv_key = ?"
        values: tuple[object, ...] = (conv_key,)
        if message_id is not None:
            where += " AND message_id = ?"
            values += (message_id,)
        with self.lock:
            rows = self.connection.execute(
                f"""
                SELECT choice_id, option_text
                FROM pending_choices
                WHERE {where}
                ORDER BY created_at, choice_id
                """,
                values,
            ).fetchall()
        return [(str(row["choice_id"]), str(row["option_text"])) for row in rows]

    def list_choice_messages(self, conv_key: str) -> list[int]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT DISTINCT message_id
                FROM pending_choices
                WHERE conv_key = ? AND message_id IS NOT NULL
                ORDER BY message_id
                """,
                (conv_key,),
            ).fetchall()
        return [int(row["message_id"]) for row in rows]

    def delete_choices(self, conv_key: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM pending_choices WHERE conv_key = ?",
                (conv_key,),
            )

    def add_long_text(
        self,
        token: str,
        conv_key: str,
        chat_id: int,
        text: str,
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO long_texts(
                    token, conv_key, chat_id, text, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (token, conv_key, chat_id, text, time.time()),
            )

    def get_long_text(self, token: str) -> tuple[str, int, str, str] | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT conv_key, chat_id, text, token
                FROM long_texts WHERE token = ?
                """,
                (token,),
            ).fetchone()
        if row is None:
            return None
        return str(row["conv_key"]), int(row["chat_id"]), str(row["text"]), str(row["token"])

    def delete_long_text(self, token: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM long_texts WHERE token = ?",
                (token,),
            )

    def cleanup_long_texts(self, max_age_seconds: float = 7 * 86400) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM long_texts WHERE created_at < ?",
                (time.time() - max_age_seconds,),
            )

    def get_setting(self, key: str) -> str | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
        return None if row is None else str(row["value"])

    def set_setting(self, key: str, value: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    @staticmethod
    def _conversation(row: sqlite3.Row | None) -> Conversation | None:
        if row is None:
            return None
        return Conversation(
            conv_key=str(row["conv_key"]),
            chat_id=int(row["chat_id"]),
            thread_id=None if row["thread_id"] is None else int(row["thread_id"]),
            session_id=str(row["session_id"]),
            session_url=str(row["session_url"]),
            title=str(row["title"]),
            last_event_id=(
                None if row["last_event_id"] is None else str(row["last_event_id"])
            ),
            created_at=float(row["created_at"]),
            last_user_text=(
                None if row["last_user_text"] is None else str(row["last_user_text"])
            ),
            last_pr_url=(
                None if row["last_pr_url"] is None else str(row["last_pr_url"])
            ),
            updated_at=float(row["updated_at"] or row["created_at"]),
        )
