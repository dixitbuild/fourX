"""One SQLite file holding every conversation and its messages.

A message is saved the moment it exists, so a restart can pick up exactly where the last
run stopped: the saved history is all the agent needs.
"""
import json
from contextlib import closing
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import Message

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "conversations.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open, or completed after /finish
    created_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._set_aside_old_database(Path(path))
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    @staticmethod
    def _set_aside_old_database(path: Path) -> None:
        """An earlier version saved conversations differently. Keep that file as .old and start fresh."""
        if not path.exists():
            return
        with closing(sqlite3.connect(path)) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(conversations)")]
        if columns and "last_active_at" not in columns:
            path.rename(path.with_name(path.name + ".old"))

    def latest_open(self) -> str | None:
        """The most recently active conversation that was not finished, if any."""
        row = self.conn.execute(
            "SELECT id FROM conversations WHERE status = 'open' ORDER BY last_active_at DESC, rowid DESC").fetchone()
        return row["id"] if row else None

    def create(self, provider: str) -> str:
        now = _now()
        conversation_id = "chat-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        with self.conn:
            self.conn.execute("INSERT INTO conversations (id, provider, created_at, last_active_at) VALUES (?, ?, ?, ?)",
                              (conversation_id, provider, now, now))
        return conversation_id

    def provider(self, conversation_id: str) -> str:
        # Read fresh each time: the switch_model tool changes it from the MCP server process.
        return self.conn.execute("SELECT provider FROM conversations WHERE id = ?", (conversation_id,)).fetchone()["provider"]

    def set_provider(self, conversation_id: str, provider: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE conversations SET provider = ? WHERE id = ?", (provider, conversation_id))

    def finish(self, conversation_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE conversations SET status = 'completed' WHERE id = ?", (conversation_id,))

    def add_message(self, conversation_id: str, message: Message) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO messages (conversation_id, body, created_at) VALUES (?, ?, ?)",
                              (conversation_id, json.dumps(message, ensure_ascii=False), _now()))
            self.conn.execute("UPDATE conversations SET last_active_at = ? WHERE id = ?", (_now(), conversation_id))

    def messages(self, conversation_id: str) -> list[Message]:
        rows = self.conn.execute("SELECT body FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,))
        return [json.loads(row["body"]) for row in rows]
