import sqlite3
from pathlib import Path


class Store:
    def __init__(self, database_path: str) -> None:
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path, check_same_thread=False)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                chat_id INTEGER PRIMARY KEY,
                devin_session_id TEXT NOT NULL,
                last_message_id TEXT
            )
            """
        )
        self.connection.commit()

    def get(self, chat_id: int) -> tuple[str, str | None] | None:
        row = self.connection.execute(
            "SELECT devin_session_id, last_message_id FROM chat_sessions WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return row if row else None

    def put(self, chat_id: int, devin_session_id: str) -> None:
        self.connection.execute(
            """
            INSERT INTO chat_sessions(chat_id, devin_session_id)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET devin_session_id = excluded.devin_session_id
            """,
            (chat_id, devin_session_id),
        )
        self.connection.commit()

    def mark_message(self, chat_id: int, message_id: str) -> None:
        self.connection.execute(
            "UPDATE chat_sessions SET last_message_id = ? WHERE chat_id = ?",
            (message_id, chat_id),
        )
        self.connection.commit()
