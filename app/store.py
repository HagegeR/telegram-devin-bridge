from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

UNSET = object()
PLACEHOLDER_TITLE_PREFIX = "Telegram: "


@dataclass(frozen=True)
class Conversation:
    conv_key: str
    chat_id: int
    thread_id: int | None
    session_id: str
    session_url: str
    title: str
    title_pending: bool
    last_event_id: str | None
    created_at: float
    last_user_text: str | None
    last_user_message_id: int | None
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
    title_pending: bool = False


@dataclass(frozen=True)
class ConversationSettings:
    silent: bool = False
    drafts: bool | None = None
    status_timer: bool | None = None
    default_playbook: str | None = None


@dataclass(frozen=True)
class AccessRequest:
    user_id: int
    username: str | None
    first_name: str | None
    status: str
    requested_at: float
    decided_at: float | None
    decided_by: int | None


@dataclass(frozen=True)
class UserStats:
    messages: int = 0
    sessions: int = 0
    last_seen_at: float | None = None


class Store:
    def __init__(self, database_path: str) -> None:
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(database_path)
        self.connection = sqlite3.connect(database_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._last_processed_prune = 0.0
        self._initialize()

    def integrity_check(self) -> list[str]:
        with self.lock:
            return [
                row[0]
                for row in self.connection.execute("PRAGMA integrity_check")
            ]

    def backup_to(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(dest)
        try:
            with self.lock:
                self.connection.backup(target)
        finally:
            target.close()

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
                    title_pending INTEGER NOT NULL DEFAULT 0,
                    last_event_id TEXT,
                    created_at REAL NOT NULL,
                    last_user_text TEXT,
                    last_user_message_id INTEGER,
                    last_pr_url TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conv_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    session_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    title_pending INTEGER NOT NULL DEFAULT 0
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
                CREATE TABLE IF NOT EXISTS message_index (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    conv_key TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(chat_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_settings (
                    conv_key TEXT PRIMARY KEY,
                    silent INTEGER NOT NULL DEFAULT 0,
                    drafts INTEGER,
                    status_timer INTEGER,
                    default_playbook TEXT
                );
                CREATE TABLE IF NOT EXISTS access_requests (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    status TEXT NOT NULL,
                    requested_at REAL NOT NULL,
                    decided_at REAL,
                    decided_by INTEGER
                );
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id INTEGER PRIMARY KEY,
                    messages INTEGER NOT NULL DEFAULT 0,
                    sessions INTEGER NOT NULL DEFAULT 0,
                    last_seen_at REAL NOT NULL
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
            if "last_user_message_id" not in columns:
                self.connection.execute(
                    "ALTER TABLE conversations ADD COLUMN last_user_message_id INTEGER"
                )
            if "title_pending" not in columns:
                self.connection.execute(
                    "ALTER TABLE conversations ADD COLUMN title_pending INTEGER NOT NULL DEFAULT 0"
                )
            history_columns = {
                str(row["name"])
                for row in self.connection.execute(
                    "PRAGMA table_info(session_history)"
                )
            }
            if "title_pending" not in history_columns:
                self.connection.execute(
                    "ALTER TABLE session_history ADD COLUMN title_pending INTEGER NOT NULL DEFAULT 0"
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
            message_index_columns = {
                str(row["name"])
                for row in self.connection.execute(
                    "PRAGMA table_info(message_index)"
                )
            }
            if "created_at" not in message_index_columns:
                self.connection.execute(
                    "ALTER TABLE message_index ADD COLUMN created_at REAL"
                )
                self.connection.execute(
                    "UPDATE message_index SET created_at = ? "
                    "WHERE created_at IS NULL",
                    (time.time(),),
                )
            legacy_table = self.connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'chat_sessions'
                """
            ).fetchone()
            if legacy_table is not None:
                self._migrate_legacy_sessions()
            self.connection.execute(
                "DELETE FROM message_index WHERE created_at < ?",
                (time.time() - 30 * 86400,),
            )
            # Indexed after the column migrations above: some keys live on
            # columns added by ALTER TABLE on legacy databases.
            self.connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_conversations_chat_updated
                    ON conversations(chat_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_conversations_updated
                    ON conversations(updated_at);
                CREATE INDEX IF NOT EXISTS idx_history_conv_id
                    ON session_history(conv_key, id);
                CREATE INDEX IF NOT EXISTS idx_choices_conv_message
                    ON pending_choices(conv_key, message_id);
                CREATE INDEX IF NOT EXISTS idx_long_texts_created
                    ON long_texts(created_at);
                CREATE INDEX IF NOT EXISTS idx_processed_updates_seen
                    ON processed_updates(seen_at);
                CREATE INDEX IF NOT EXISTS idx_message_index_created
                    ON message_index(created_at);
                """
            )

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
                        title, last_event_id, created_at, last_user_text,
                        last_user_message_id, updated_at
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, ?)
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
        title_pending: bool = False,
        last_event_id: str | None = None,
        last_user_text: str | None = None,
        last_user_message_id: int | None = None,
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
                    title, title_pending, last_event_id, created_at, last_user_text,
                    last_user_message_id, last_pr_url, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conv_key) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    thread_id = excluded.thread_id,
                    session_id = excluded.session_id,
                    session_url = excluded.session_url,
                    title = excluded.title,
                    title_pending = excluded.title_pending,
                    last_event_id = excluded.last_event_id,
                    last_user_text = excluded.last_user_text,
                    last_user_message_id = excluded.last_user_message_id,
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
                    int(title_pending),
                    last_event_id,
                    timestamp,
                    last_user_text,
                    last_user_message_id,
                    last_pr_url,
                    timestamp,
                ),
            )

    def update_conversation(
        self,
        conv_key: str,
        session_id: str,
        *,
        title: str | None = None,
        title_pending: bool | None = None,
        last_event_id: str | None = None,
        last_user_text: str | None = None,
        last_user_message_id: object = UNSET,
        last_pr_url: str | None = None,
    ) -> None:
        assignments: list[str] = []
        values: list[object] = []
        if title is not None:
            assignments.append("title = ?")
            values.append(title)
        if title_pending is not None:
            assignments.append("title_pending = ?")
            values.append(int(title_pending))
        if last_event_id is not None:
            assignments.append("last_event_id = ?")
            values.append(last_event_id)
        if last_user_text is not None:
            assignments.append("last_user_text = ?")
            values.append(last_user_text)
        if last_user_message_id is not UNSET:
            assignments.append("last_user_message_id = ?")
            values.append(last_user_message_id)
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
            if title is not None:
                self.connection.execute(
                    "UPDATE session_history SET title = ? "
                    "WHERE conv_key = ? AND session_id = ?",
                    (title, conv_key, session_id),
                )
            if title_pending is not None:
                self.connection.execute(
                    "UPDATE session_history SET title_pending = ? "
                    "WHERE conv_key = ? AND session_id = ?",
                    (int(title_pending), conv_key, session_id),
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

    def list_recent_conversations(self, since: float) -> list[Conversation]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM conversations WHERE updated_at >= ? "
                "ORDER BY updated_at",
                (since,),
            ).fetchall()
        return [
            conversation
            for row in rows
            if (conversation := self._conversation(row)) is not None
        ]

    def count_conversations_for_chat(self, chat_id: int) -> int:
        with self.lock:
            row = self.connection.execute(
                "SELECT COUNT(*) AS count FROM conversations WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def get_settings(self, conv_key: str) -> ConversationSettings:
        with self.lock:
            row = self.connection.execute(
                "SELECT silent, drafts, status_timer, default_playbook "
                "FROM conversation_settings WHERE conv_key = ?",
                (conv_key,),
            ).fetchone()
        if row is None:
            return ConversationSettings()
        return ConversationSettings(
            silent=bool(row["silent"]),
            drafts=None if row["drafts"] is None else bool(row["drafts"]),
            status_timer=(
                None if row["status_timer"] is None else bool(row["status_timer"])
            ),
            default_playbook=(
                None
                if row["default_playbook"] is None
                else str(row["default_playbook"])
            ),
        )

    def update_settings(self, conv_key: str, **fields: object) -> None:
        allowed = {"silent", "drafts", "status_timer", "default_playbook"}
        if not fields or any(key not in allowed for key in fields):
            raise ValueError("Unknown conversation setting")
        current = self.get_settings(conv_key)
        values = {
            "silent": int(fields.get("silent", current.silent)),
            "drafts": fields.get("drafts", current.drafts),
            "status_timer": fields.get("status_timer", current.status_timer),
            "default_playbook": fields.get(
                "default_playbook",
                current.default_playbook,
            ),
        }
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO conversation_settings(
                    conv_key, silent, drafts, status_timer, default_playbook
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conv_key) DO UPDATE SET
                    silent = excluded.silent,
                    drafts = excluded.drafts,
                    status_timer = excluded.status_timer,
                    default_playbook = excluded.default_playbook
                """,
                (
                    conv_key,
                    values["silent"],
                    None if values["drafts"] is None else int(bool(values["drafts"])),
                    (
                        None
                        if values["status_timer"] is None
                        else int(bool(values["status_timer"]))
                    ),
                    values["default_playbook"],
                ),
            )

    def get_access_request(self, user_id: int) -> AccessRequest | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM access_requests WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return AccessRequest(
            user_id=int(row["user_id"]),
            username=None if row["username"] is None else str(row["username"]),
            first_name=None if row["first_name"] is None else str(row["first_name"]),
            status=str(row["status"]),
            requested_at=float(row["requested_at"]),
            decided_at=None if row["decided_at"] is None else float(row["decided_at"]),
            decided_by=None if row["decided_by"] is None else int(row["decided_by"]),
        )

    def save_access_request(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
    ) -> AccessRequest:
        timestamp = time.time()
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO access_requests(
                    user_id, username, first_name, status, requested_at,
                    decided_at, decided_by
                ) VALUES (?, ?, ?, 'requested', ?, NULL, NULL)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    status = 'requested',
                    requested_at = excluded.requested_at,
                    decided_at = NULL,
                    decided_by = NULL
                """,
                (user_id, username, first_name, timestamp),
            )
        return self.get_access_request(user_id) or AccessRequest(
            user_id, username, first_name, "requested", timestamp, None, None
        )

    def decide_access_request(
        self,
        user_id: int,
        status: str,
        decided_by: int,
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                UPDATE access_requests
                SET status = ?, decided_at = ?, decided_by = ?
                WHERE user_id = ?
                """,
                (status, time.time(), decided_by, user_id),
            )

    def bump_user_stats(self, user_id: int, *, messages: int = 0, sessions: int = 0) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO user_stats(user_id, messages, sessions, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    messages = messages + excluded.messages,
                    sessions = sessions + excluded.sessions,
                    last_seen_at = excluded.last_seen_at
                """,
                (user_id, messages, sessions, time.time()),
            )

    def get_user_stats(self, user_id: int) -> UserStats:
        with self.lock:
            row = self.connection.execute(
                "SELECT messages, sessions, last_seen_at FROM user_stats WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return UserStats()
        return UserStats(int(row["messages"]), int(row["sessions"]), float(row["last_seen_at"]))

    def list_access_requests(self, status: str = "approved") -> list[AccessRequest]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM access_requests WHERE status = ? ORDER BY user_id",
                (status,),
            ).fetchall()
        return [
            AccessRequest(
                user_id=int(row["user_id"]),
                username=None if row["username"] is None else str(row["username"]),
                first_name=None if row["first_name"] is None else str(row["first_name"]),
                status=str(row["status"]),
                requested_at=float(row["requested_at"]),
                decided_at=(
                    None if row["decided_at"] is None else float(row["decided_at"])
                ),
                decided_by=(
                    None if row["decided_by"] is None else int(row["decided_by"])
                ),
            )
            for row in rows
        ]

    def index_message(self, chat_id: int, message_id: int, conv_key: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO message_index(chat_id, message_id, conv_key, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    conv_key = excluded.conv_key,
                    created_at = excluded.created_at
                """,
                (chat_id, message_id, conv_key, time.time()),
            )

    def conv_key_for_message(self, chat_id: int, message_id: int) -> str | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT conv_key FROM message_index
                WHERE chat_id = ? AND message_id = ?
                """,
                (chat_id, message_id),
            ).fetchone()
        return None if row is None else str(row["conv_key"])

    def cleanup_message_index(self, max_age_seconds: float = 30 * 86400) -> None:
        cutoff = time.time() - max_age_seconds
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM message_index WHERE created_at < ?",
                (cutoff,),
            )

    def add_history(
        self,
        *,
        conv_key: str,
        session_id: str,
        session_url: str,
        title: str,
        title_pending: bool = False,
        created_at: float | None = None,
    ) -> int:
        timestamp = time.time() if created_at is None else created_at
        with self.lock, self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO session_history(
                    conv_key, session_id, session_url, title, created_at,
                    title_pending
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    conv_key,
                    session_id,
                    session_url,
                    title,
                    timestamp,
                    int(title_pending),
                ),
            )
            return int(cursor.lastrowid)

    def update_history_title(self, conv_key: str, session_id: str, title: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                UPDATE session_history
                SET title = ?, title_pending = 0
                WHERE conv_key = ? AND session_id = ?
                """,
                (title, conv_key, session_id),
            )

    def list_history(self, conv_key: str, limit: int = 10) -> list[HistoryEntry]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT id, conv_key, session_id, session_url, title, created_at,
                    title_pending
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
                title_pending=bool(row["title_pending"]),
            )
            for row in rows
        ]

    def mark_update_seen(self, update_id: int) -> bool:
        now = time.time()
        with self.lock, self.connection:
            if now - self._last_processed_prune >= 3600:
                self._last_processed_prune = now
                self.connection.execute(
                    "DELETE FROM processed_updates WHERE seen_at < ?",
                    (now - 86400,),
                )
            try:
                self.connection.execute(
                    "INSERT INTO processed_updates(update_id, seen_at) VALUES (?, ?)",
                    (update_id, now),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def unmark_update_seen(self, update_id: int) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM processed_updates WHERE update_id = ?",
                (update_id,),
            )

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

    def delete_setting(self, key: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM settings WHERE key = ?",
                (key,),
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
            title_pending=bool(row["title_pending"]),
            last_event_id=(
                None if row["last_event_id"] is None else str(row["last_event_id"])
            ),
            created_at=float(row["created_at"]),
            last_user_text=(
                None if row["last_user_text"] is None else str(row["last_user_text"])
            ),
            last_user_message_id=(
                None
                if row["last_user_message_id"] is None
                else int(row["last_user_message_id"])
            ),
            last_pr_url=(
                None if row["last_pr_url"] is None else str(row["last_pr_url"])
            ),
            updated_at=float(row["updated_at"] or row["created_at"]),
        )
