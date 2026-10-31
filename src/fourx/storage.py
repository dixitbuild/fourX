"""One SQLite file holding each conversation and its posting attempts.

Every write commits before the runner moves on. Related writes share a transaction,
so a tool result can never be saved without the posting state it belongs to.
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import Message, Posting, Proposal

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, provider TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS postings (
    action_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    call_id TEXT NOT NULL, proposal TEXT NOT NULL, reference TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL, entry_id TEXT, updated_at TEXT NOT NULL,
    UNIQUE (conversation_id, call_id));
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The worker thread and the prompt take turns; they never use this at the same time.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def open_conversation(self, conversation_id: str, provider: str) -> tuple[str, bool]:
        """Return the conversation's saved provider, and whether it already existed."""
        row = self.conn.execute("SELECT provider FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if row:
            return row["provider"], True
        with self.conn:
            self.conn.execute("INSERT INTO conversations VALUES (?, ?, ?)", (conversation_id, provider, _now()))
        return provider, False

    def set_provider(self, conversation_id: str, provider: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE conversations SET provider = ? WHERE id = ?", (provider, conversation_id))

    def _insert_message(self, conversation_id: str, message: Message) -> None:
        self.conn.execute("INSERT INTO messages (conversation_id, body, created_at) VALUES (?, ?, ?)",
                          (conversation_id, json.dumps(message, ensure_ascii=False), _now()))

    def add_message(self, conversation_id: str, message: Message) -> None:
        with self.conn:
            self._insert_message(conversation_id, message)

    def messages(self, conversation_id: str) -> list[Message]:
        rows = self.conn.execute("SELECT body FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,))
        return [json.loads(row["body"]) for row in rows]

    def propose(self, conversation_id: str, call_id: str, proposal: Proposal) -> Posting:
        action_id = uuid.uuid4().hex[:12]
        reference = f"fourx:{conversation_id}:{action_id}"
        with self.conn:
            self.conn.execute("INSERT INTO postings VALUES (?, ?, ?, ?, ?, 'awaiting_approval', NULL, ?)",
                              (action_id, conversation_id, call_id, json.dumps(proposal, ensure_ascii=False), reference, _now()))
        return self.postings(conversation_id, "awaiting_approval")[0]

    def postings(self, conversation_id: str, state: str) -> list[Posting]:
        rows = self.conn.execute("SELECT * FROM postings WHERE conversation_id = ? AND state = ? ORDER BY updated_at",
                                 (conversation_id, state)).fetchall()
        return [{**dict(row), "proposal": json.loads(row["proposal"])} for row in rows]

    def set_state(self, action_id: str, state: str) -> None:
        """Save a state change that has no tool result yet."""
        with self.conn:
            self._update_posting(action_id, state, None)

    def _update_posting(self, action_id: str, state: str, entry_id: str | None) -> None:
        self.conn.execute("UPDATE postings SET state = ?, entry_id = ?, updated_at = ? WHERE action_id = ?",
                          (state, entry_id, _now(), action_id))

    def finish_posting(self, posting: Posting, state: str, result: Message, entry_id: str | None = None) -> None:
        """Save the outcome and the tool result together."""
        with self.conn:
            self._update_posting(posting["action_id"], state, entry_id)
            self._insert_message(posting["conversation_id"], result)
