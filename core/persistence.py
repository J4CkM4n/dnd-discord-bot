"""SQLite transactions for snapshots, append-only facts, and narration status."""

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("dnd-bot")


class StorageError(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class CampaignRepository:
    def __init__(self, path: str):
        self.path = path
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise StorageError(f"Unsupported database version: {version}")
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS channels (
                        channel_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                        snapshot TEXT NOT NULL, last_event_id INTEGER NOT NULL,
                        imported_from TEXT
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        channel_id TEXT NOT NULL REFERENCES channels(channel_id),
                        event_id INTEGER NOT NULL, payload TEXT NOT NULL,
                        PRIMARY KEY(channel_id, event_id)
                    );
                    CREATE TABLE IF NOT EXISTS director_decisions (
                        id INTEGER PRIMARY KEY,
                        channel_id TEXT NOT NULL REFERENCES channels(channel_id),
                        revision INTEGER NOT NULL, payload TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS narration (
                        channel_id TEXT PRIMARY KEY REFERENCES channels(channel_id),
                        status TEXT NOT NULL, payload TEXT
                    );
                    PRAGMA user_version=1;
                """)
        except (OSError, sqlite3.Error) as exc:
            raise StorageError(f"Cannot open campaign database: {exc}") from exc

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            with conn:
                yield conn
        finally:
            conn.close()

    def load(self, channel_id: str) -> dict | None:
        try:
            with self.connect() as conn:
                conn.execute("BEGIN")
                row = conn.execute(
                    "SELECT revision,snapshot,last_event_id FROM channels WHERE channel_id=?",
                    (channel_id,)).fetchone()
                if row is None:
                    return None
                events = [json.loads(r[0]) for r in conn.execute(
                    "SELECT payload FROM events WHERE channel_id=? ORDER BY event_id",
                    (channel_id,))]
                if len(events) != row[2]:
                    raise StorageError("Campaign event history is incomplete")
                narr = conn.execute("SELECT payload FROM narration WHERE channel_id=?",
                                    (channel_id,)).fetchone()
                return {"revision": row[0], "snapshot": json.loads(row[1]),
                        "entries": events,
                        "unfinished": json.loads(narr[0]) if narr and narr[0] else None}
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise StorageError(f"Cannot read campaign: {exc}") from exc

    def commit(self, channel_id: str, revision: int, snapshot: dict | None,
               events: list[dict], unfinished: dict | None,
               decisions: list[dict] = (), imported_from: str | None = None) -> int:
        """snapshot=None commits narration only. Never rewrite previous events."""
        started = time.monotonic()
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT revision,last_event_id FROM channels WHERE channel_id=?",
                    (channel_id,)).fetchone()
                if (row[0] if row else 0) != revision:
                    raise StorageError("Campaign changed in another service; reload required")
                last_id = row[1] if row else 0
                for event in events:
                    if event["event_id"] != last_id + 1:
                        raise StorageError("Event append is out of sequence")
                    last_id += 1
                if row is None:
                    if snapshot is None:
                        raise StorageError("A new campaign needs a snapshot")
                    conn.execute("INSERT INTO channels VALUES (?,?,?,?,?)",
                                 (channel_id, revision + 1, _json(snapshot), last_id, imported_from))
                elif snapshot is not None:
                    conn.execute("UPDATE channels SET revision=?,snapshot=?,last_event_id=? "
                                 "WHERE channel_id=?",
                                 (revision + 1, _json(snapshot), last_id, channel_id))
                else:
                    conn.execute("UPDATE channels SET revision=?,last_event_id=? WHERE channel_id=?",
                                 (revision + 1, last_id, channel_id))
                conn.executemany("INSERT INTO events VALUES (?,?,?)",
                                 [(channel_id, e["event_id"], _json(e)) for e in events])
                conn.executemany("INSERT INTO director_decisions(channel_id,revision,payload) "
                                 "VALUES (?,?,?)", [(channel_id, revision + 1, _json(d))
                                                    for d in decisions])
                conn.execute("INSERT INTO narration VALUES (?,?,?) ON CONFLICT(channel_id) "
                             "DO UPDATE SET status=excluded.status,payload=excluded.payload",
                             (channel_id, "pending" if unfinished else "complete",
                              _json(unfinished) if unfinished else None))
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            raise StorageError(f"Cannot commit campaign: {exc}") from exc
        log.info("campaign commit | channel=%s revision=%d events=%d snapshot=%s ms=%.1f",
                 channel_id, revision + 1, len(events), snapshot is not None,
                 (time.monotonic() - started) * 1000)
        return revision + 1
