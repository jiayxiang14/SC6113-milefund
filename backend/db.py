"""SQLite storage for the event index, off-chain metadata and tracked transactions."""

import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Every contract event, decoded. Amounts are stored as decimal strings (wei) because
-- they can exceed SQLite's 64-bit integer range.
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_hash      TEXT    NOT NULL,
    log_index    INTEGER NOT NULL,
    block_number INTEGER NOT NULL,
    block_time   INTEGER NOT NULL,
    event        TEXT    NOT NULL,
    campaign_id  INTEGER NOT NULL,
    actor        TEXT,
    amount       TEXT,
    data         TEXT    NOT NULL,
    UNIQUE (tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS idx_events_campaign ON events (campaign_id, block_number);
CREATE INDEX IF NOT EXISTS idx_events_actor ON events (actor, block_number);

-- Off-chain descriptions, addressed by keccak256(content). Only the hash goes on-chain.
CREATE TABLE IF NOT EXISTS metadata (
    hash       TEXT PRIMARY KEY,
    kind       TEXT    NOT NULL,
    author     TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    signature  TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);

-- Transactions reported by the frontend so their status (including failures, which emit
-- no events) can be shown in the user's history.
CREATE TABLE IF NOT EXISTS transactions (
    hash         TEXT PRIMARY KEY,
    sender       TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    campaign_id  INTEGER,
    status       TEXT    NOT NULL,
    block_number INTEGER,
    gas_used     INTEGER,
    error        TEXT,
    submitted_at INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_sender ON transactions (sender, submitted_at);
CREATE INDEX IF NOT EXISTS idx_tx_status ON transactions (status);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        # A short-lived connection per unit of work keeps the indexer thread and request
        # threads independent; WAL mode lets them read and write concurrently.
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- key/value ---------------------------------------------------------------

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str, conn=None) -> None:
        sql = "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        if conn is not None:
            conn.execute(sql, (key, value))
            return
        with self.connect() as c:
            c.execute(sql, (key, value))

    # -- events ------------------------------------------------------------------

    def insert_events(self, rows: list[dict], last_block: int) -> None:
        """Store a batch of events and advance the cursor atomically."""
        with self.connect() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO events
                   (tx_hash, log_index, block_number, block_time, event, campaign_id, actor, amount, data)
                   VALUES (:tx_hash, :log_index, :block_number, :block_time, :event, :campaign_id,
                           :actor, :amount, :data)""",
                rows,
            )
            self.set_kv("last_indexed_block", str(last_block), conn)

    def campaign_events(self, campaign_id: int, limit: int = 200) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE campaign_id = ? ORDER BY block_number DESC, log_index DESC LIMIT ?",
                (campaign_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def actor_events(self, actor: str, limit: int = 200) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE actor = ? ORDER BY block_number DESC, log_index DESC LIMIT ?",
                (actor.lower(), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- metadata ----------------------------------------------------------------

    def get_metadata(self, hash_: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM metadata WHERE hash = ?", (hash_.lower(),)).fetchone()
        return dict(row) if row else None

    def insert_metadata(self, hash_: str, kind: str, author: str, content: str, signature: str, now: int) -> bool:
        """Returns False if the hash already exists (content-addressed, so it is identical)."""
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO metadata (hash, kind, author, content, signature, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (hash_.lower(), kind, author.lower(), content, signature, now),
            )
            return cur.rowcount == 1

    # -- transactions ------------------------------------------------------------

    def insert_transaction(self, hash_: str, sender: str, action: str, campaign_id, now: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO transactions
                   (hash, sender, action, campaign_id, status, submitted_at, updated_at)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                (hash_.lower(), sender.lower(), action, campaign_id, now, now),
            )
            return cur.rowcount == 1

    def get_transaction(self, hash_: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM transactions WHERE hash = ?", (hash_.lower(),)).fetchone()
        return dict(row) if row else None

    def pending_transactions(self, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE status = 'pending' ORDER BY submitted_at LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_transaction(self, hash_: str, now: int, **fields) -> None:
        fields["updated_at"] = now
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE transactions SET {cols} WHERE hash = ?", (*fields.values(), hash_.lower()))

    def delete_transaction(self, hash_: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM transactions WHERE hash = ?", (hash_.lower(),))

    def sender_transactions(self, sender: str, limit: int = 200) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE sender = ? ORDER BY submitted_at DESC LIMIT ?",
                (sender.lower(), limit),
            ).fetchall()
        return [dict(r) for r in rows]
