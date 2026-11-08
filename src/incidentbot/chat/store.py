"""Chat storage behind one interface, with an in-memory and a SQLite implementation."""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Protocol

from incidentbot.chat.models import Channel, Message


class ChatStore(Protocol):
    def create_channel(self, name: str, ts: float, topic: str = "",
                       incident_id: str | None = None) -> Channel: ...
    def get_channel(self, channel_id: str) -> Channel | None: ...
    def channel_by_name(self, name: str) -> Channel | None: ...
    def list_channels(self) -> list[Channel]: ...
    def post(self, channel_id: str, user: str, text: str, ts: float,
             thread_ts: str | None = None, blocks: list[dict[str, Any]] | None = None,
             bot: bool = False) -> Message: ...
    def get_message(self, message_id: str) -> Message | None: ...
    def messages(self, channel_id: str, limit: int = 200) -> list[Message]: ...
    def add_reaction(self, message_id: str, reaction: str, user: str) -> bool: ...
    def update_blocks(self, message_id: str, blocks: list[dict[str, Any]]) -> None: ...


class InMemoryChatStore:
    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}
        self._messages: dict[str, Message] = {}
        self._by_channel: dict[str, list[str]] = {}
        self._seq = 0

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq:06d}"

    def create_channel(self, name, ts, topic="", incident_id=None):
        if self.channel_by_name(name):
            raise ValueError(f"channel #{name} already exists")
        ch = Channel(self._next("C"), name, topic, ts, incident_id)
        self._channels[ch.id] = ch
        self._by_channel[ch.id] = []
        return ch

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def channel_by_name(self, name):
        return next((c for c in self._channels.values() if c.name == name), None)

    def list_channels(self):
        return list(self._channels.values())

    def post(self, channel_id, user, text, ts, thread_ts=None, blocks=None, bot=False):
        if channel_id not in self._channels:
            raise KeyError(f"no channel {channel_id}")
        m = Message(self._next("M"), channel_id, user, text, ts, thread_ts,
                    list(blocks or []), {}, bot)
        self._messages[m.id] = m
        self._by_channel[channel_id].append(m.id)
        return m

    def get_message(self, message_id):
        return self._messages.get(message_id)

    def messages(self, channel_id, limit=200):
        ids = self._by_channel.get(channel_id, [])[-limit:]
        return [self._messages[i] for i in ids]

    def add_reaction(self, message_id, reaction, user):
        m = self._messages[message_id]
        users = m.reactions.setdefault(reaction, [])
        if user in users:
            return False
        users.append(user)
        return True

    def update_blocks(self, message_id, blocks):
        self._messages[message_id].blocks = list(blocks)


SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
  id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, topic TEXT NOT NULL,
  created_at REAL NOT NULL, incident_id TEXT);
CREATE TABLE IF NOT EXISTS messages (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
  channel_id TEXT NOT NULL REFERENCES channels(id), user TEXT NOT NULL, text TEXT NOT NULL,
  ts REAL NOT NULL, thread_ts TEXT, blocks TEXT NOT NULL, bot INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reactions (
  message_id TEXT NOT NULL REFERENCES messages(id), reaction TEXT NOT NULL,
  user TEXT NOT NULL, PRIMARY KEY (message_id, reaction, user));
CREATE INDEX IF NOT EXISTS messages_channel ON messages(channel_id, seq);
"""


class SqliteChatStore:
    """Same behavior as InMemoryChatStore, persisted. One connection guarded by a lock."""

    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(SCHEMA)

    def _next(self, prefix: str, table: str) -> str:
        n = self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return f"{prefix}{n + 1:06d}"

    def _channel(self, row: sqlite3.Row | None) -> Channel | None:
        if row is None:
            return None
        return Channel(row["id"], row["name"], row["topic"], row["created_at"],
                       row["incident_id"])

    def _message(self, row: sqlite3.Row) -> Message:
        reactions: dict[str, list[str]] = {}
        for r in self._db.execute(
                "SELECT reaction, user FROM reactions WHERE message_id = ? ORDER BY rowid",
                (row["id"],)):
            reactions.setdefault(r["reaction"], []).append(r["user"])
        return Message(row["id"], row["channel_id"], row["user"], row["text"], row["ts"],
                       row["thread_ts"], json.loads(row["blocks"]), reactions, bool(row["bot"]))

    def create_channel(self, name, ts, topic="", incident_id=None):
        with self._lock, self._db:
            if self._db.execute("SELECT 1 FROM channels WHERE name = ?", (name,)).fetchone():
                raise ValueError(f"channel #{name} already exists")
            cid = self._next("C", "channels")
            self._db.execute("INSERT INTO channels VALUES (?, ?, ?, ?, ?)",
                             (cid, name, topic, ts, incident_id))
        return Channel(cid, name, topic, ts, incident_id)

    def get_channel(self, channel_id):
        with self._lock:
            return self._channel(self._db.execute(
                "SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone())

    def channel_by_name(self, name):
        with self._lock:
            return self._channel(self._db.execute(
                "SELECT * FROM channels WHERE name = ?", (name,)).fetchone())

    def list_channels(self):
        with self._lock:
            rows = self._db.execute("SELECT * FROM channels ORDER BY rowid").fetchall()
            return [self._channel(r) for r in rows]

    def post(self, channel_id, user, text, ts, thread_ts=None, blocks=None, bot=False):
        with self._lock, self._db:
            if not self._db.execute("SELECT 1 FROM channels WHERE id = ?",
                                    (channel_id,)).fetchone():
                raise KeyError(f"no channel {channel_id}")
            mid = self._next("M", "messages")
            self._db.execute(
                "INSERT INTO messages (id, channel_id, user, text, ts, thread_ts, blocks, bot)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, channel_id, user, text, ts, thread_ts, json.dumps(blocks or []), int(bot)))
        return Message(mid, channel_id, user, text, ts, thread_ts, list(blocks or []), {}, bot)

    def get_message(self, message_id):
        with self._lock:
            row = self._db.execute("SELECT * FROM messages WHERE id = ?",
                                   (message_id,)).fetchone()
            return self._message(row) if row else None

    def messages(self, channel_id, limit=200):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE channel_id = ? ORDER BY seq DESC"
                " LIMIT ?) ORDER BY seq", (channel_id, limit)).fetchall()
            return [self._message(r) for r in rows]

    def add_reaction(self, message_id, reaction, user):
        with self._lock, self._db:
            if not self._db.execute("SELECT 1 FROM messages WHERE id = ?",
                                    (message_id,)).fetchone():
                raise KeyError(message_id)
            cur = self._db.execute("INSERT OR IGNORE INTO reactions VALUES (?, ?, ?)",
                                   (message_id, reaction, user))
            return cur.rowcount == 1

    def update_blocks(self, message_id, blocks):
        with self._lock, self._db:
            self._db.execute("UPDATE messages SET blocks = ? WHERE id = ?",
                             (json.dumps(blocks), message_id))
