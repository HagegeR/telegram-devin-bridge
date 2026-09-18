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
                    last_user_text TEXT
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
                    option_text TEXT NOT NULL,
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
                        title, last_event_id, created_at, last_user_text
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        conv_key,
                        chat_id,
                        session_id,
                        session_url,
                        "Telegram conversation",
                        row["last_message_id"],
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
        created_at: float | None = None,
    ) -> None:
        timestamp = time.time() if created_at is None else created_at
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO conversations(
                    conv_key, chat_id, thread_id, session_id, session_url,
                    title, last_event_id, created_at, last_user_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conv_key) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    thread_id = excluded.thread_id,
                    session_id = excluded.session_id,
                    session_url = excluded.session_url,
                    title = excluded.title,
                    last_event_id = excluded.last_event_id,
                    last_user_text = excluded.last_user_text
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
                ),
            )

    def update_conversation(
        self,
        conv_key: str,
        *,
        last_event_id: str | None = None,
        last_user_text: str | None = None,
    ) -> None:
        assignments: list[str] = []
        values: list[str | None] = []
        if last_event_id is not None:
            assignments.append("last_event_id = ?")
            values.append(last_event_id)
        if last_user_text is not None:
            assignments.append("last_user_text = ?")
            values.append(last_user_text)
        if not assignments:
            return
        values.append(conv_key)
        with self.lock, self.connection:
            self.connection.execute(
                f"UPDATE conversations SET {', '.join(assignments)} WHERE conv_key = ?",
                values,
            )

    def clear_conversation(self, conv_key: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM conversations WHERE conv_key = ?",
                (conv_key,),
            )

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
        option_text: str,
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO pending_choices(
                    choice_id, conv_key, session_id, option_text, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (choice_id, conv_key, session_id, option_text, time.time()),
            )

    def get_choice(self, choice_id: str) -> tuple[str, str, str] | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT conv_key, session_id, option_text
                FROM pending_choices WHERE choice_id = ?
                """,
                (choice_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["conv_key"]), str(row["session_id"]), str(row["option_text"])

    def delete_choices(self, conv_key: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM pending_choices WHERE conv_key = ?",
                (conv_key,),
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
        )
