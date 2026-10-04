"""Persistent state on stdlib sqlite3 (DESIGN.md §3.4).

Everything the bot must remember across restarts lives here: processed signals
(de-duplication), small JSON key/value state, pending intents waiting for the next
regular open, stopped-out symbols and processed fills (de-duplication by exec_id).

Conventions:
- Datetimes are stored as ISO-8601 strings (``to_iso``) and returned tz-aware
  (``from_iso``). A naive datetime is interpreted as UTC, both when writing and
  when reading back a naive stored value.
- KV values are JSON. ``set()`` converts datetimes (also nested ones) to ISO strings,
  so ``get()`` returns them as strings; use ``from_iso`` to turn them back.
- Calls are synchronous and short; the connection is guarded by a threading.Lock.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, overload

from .models import Fill, Intent, Side

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MEMORY = ":memory:"

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS signals (
        fingerprint TEXT PRIMARY KEY,
        message_id  TEXT NOT NULL,
        signal_ts   TEXT,
        status      TEXT NOT NULL,
        summary     TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS pending (
        symbol      TEXT PRIMARY KEY,
        intent_json TEXT NOT NULL,
        due_at      TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS stopped (
        symbol     TEXT PRIMARY KEY,
        stopped_at TEXT NOT NULL,
        qty        INTEGER NOT NULL,
        price      REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS fills (
        exec_id  TEXT PRIMARY KEY,
        order_id TEXT NOT NULL,
        symbol   TEXT NOT NULL,
        side     TEXT NOT NULL,
        qty      INTEGER NOT NULL,
        price    REAL NOT NULL,
        tag      TEXT NOT NULL,
        time     TEXT NOT NULL
    )""",
)

_INTENT_FIELDS = frozenset(f.name for f in dataclasses.fields(Intent))


# --------------------------------------------------------------------------- datetime helpers


def ensure_aware(dt: datetime) -> datetime:
    """Return ``dt`` unchanged if tz-aware, otherwise the same wall time in UTC."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def to_iso(dt: datetime) -> str:
    """Serialise a datetime as ISO-8601 keeping its UTC offset (naive → UTC)."""
    return ensure_aware(dt).isoformat()


@overload
def from_iso(s: str | datetime) -> datetime: ...


@overload
def from_iso(s: None) -> None: ...


def from_iso(s: str | datetime | None) -> datetime | None:
    """Parse an ISO-8601 string into a tz-aware datetime (naive → UTC).

    ``None`` passes through (handy for optional KV values); a datetime is only made
    tz-aware. Raises ValueError on a malformed string.
    """
    if s is None:
        return None
    if isinstance(s, datetime):
        return ensure_aware(s)
    return ensure_aware(datetime.fromisoformat(s.strip()))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_default(obj: Any) -> Any:
    """json.dumps hook: datetimes / dates become ISO strings, anything else is an error."""
    if isinstance(obj, datetime):
        return to_iso(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"value of type {type(obj).__name__} is not JSON serialisable")


def _dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False)


# --------------------------------------------------------------------------- intent helpers


def _intent_to_json(intent: Intent) -> str:
    return _dumps(dataclasses.asdict(intent))


def _intent_from_json(text: str) -> Intent:
    """Rebuild an Intent; unknown keys (from a newer/older schema) are ignored."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("intent_json is not an object")
    return Intent(**{k: v for k, v in data.items() if k in _INTENT_FIELDS})


# --------------------------------------------------------------------------- store


class Store:
    """sqlite3-backed persistent state. Safe to call from several threads."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)
        if self.path != MEMORY:
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser())
        self._lock = threading.Lock()
        # isolation_level=None → autocommit: every statement is durable on return.
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._closed = False
        try:
            with self._lock:
                if self.path != MEMORY:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                self._init_schema()
        except BaseException:
            self.close()
            raise

    def _init_schema(self) -> None:
        """Create tables if missing and check the schema version (caller holds the lock)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _SCHEMA:
                self._conn.execute(stmt)
            row = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
                )
            elif int(row[0]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database {self.path} has schema_version {row[0]}, "
                    f"this code only understands <= {SCHEMA_VERSION}"
                )
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return int(row[0])

    def close(self) -> None:
        """Close the connection (idempotent). Further calls raise sqlite3.ProgrammingError."""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ low level

    def _execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)

    def _fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------ signals

    def seen(self, fingerprint: str) -> bool:
        """True if a signal with this fingerprint was already recorded (any status)."""
        return self._fetchone("SELECT 1 FROM signals WHERE fingerprint = ?", (fingerprint,)) is not None

    def record_signal(
        self,
        fingerprint: str,
        message_id: str,
        signal_ts: datetime | None,
        status: str,
        summary: str,
    ) -> None:
        """Insert or replace the record for this fingerprint."""
        self._execute(
            "INSERT OR REPLACE INTO signals "
            "(fingerprint, message_id, signal_ts, status, summary, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                fingerprint,
                str(message_id),
                to_iso(signal_ts) if signal_ts is not None else None,
                status,
                summary,
                to_iso(_utcnow()),
            ),
        )

    # ------------------------------------------------------------------ key / value

    def get(self, key: str, default: Any = None) -> Any:
        """JSON-decoded value for ``key``; ``default`` if the key was never set."""
        row = self._fetchone("SELECT value FROM kv WHERE key = ?", (key,))
        if row is None:
            return default
        return json.loads(row[0])

    def set(self, key: str, value: Any) -> None:
        """Store a JSON-serialisable value (datetimes, also nested, become ISO strings).

        Raises TypeError for values JSON cannot represent (nothing is written then).
        """
        text = _dumps(value)  # serialise first so a bad value never touches the DB
        self._execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, text))

    # ------------------------------------------------------------------ pending intents

    def upsert_pending(self, intent: Intent, due_at: datetime) -> None:
        """Store the intent for its symbol, replacing any earlier pending intent."""
        self._execute(
            "INSERT OR REPLACE INTO pending (symbol, intent_json, due_at) VALUES (?, ?, ?)",
            (intent.symbol, _intent_to_json(intent), to_iso(due_at)),
        )

    def list_pending(self) -> list[tuple[Intent, datetime]]:
        """All pending intents sorted by (due_at, symbol). Unreadable rows are logged and skipped."""
        out: list[tuple[Intent, datetime]] = []
        for symbol, intent_json, due_at in self._fetchall("SELECT symbol, intent_json, due_at FROM pending"):
            try:
                out.append((_intent_from_json(intent_json), from_iso(due_at)))
            except (ValueError, TypeError) as e:
                log.error("pending intent for %s is unreadable, skipped: %s (%r)", symbol, e, intent_json)
        # Sort in Python: ISO strings with different UTC offsets do not sort as text.
        out.sort(key=lambda item: (item[1], item[0].symbol))
        return out

    def delete_pending(self, symbol: str) -> None:
        self._execute("DELETE FROM pending WHERE symbol = ?", (symbol,))

    def clear_pending(self) -> None:
        self._execute("DELETE FROM pending")

    # ------------------------------------------------------------------ stopped-out symbols

    def mark_stopped(self, symbol: str, at: datetime, qty: int, price: float) -> None:
        """Remember that ``symbol`` was stopped out (replaces an older mark)."""
        self._execute(
            "INSERT OR REPLACE INTO stopped (symbol, stopped_at, qty, price) VALUES (?, ?, ?, ?)",
            (symbol, to_iso(at), int(qty), float(price)),
        )

    def clear_stopped(self, symbol: str) -> None:
        self._execute("DELETE FROM stopped WHERE symbol = ?", (symbol,))

    def stopped(self) -> dict[str, dict]:
        """{symbol: {"stopped_at": datetime, "qty": int, "price": float}}."""
        out: dict[str, dict] = {}
        for symbol, stopped_at, qty, price in self._fetchall(
            "SELECT symbol, stopped_at, qty, price FROM stopped ORDER BY symbol"
        ):
            try:
                out[symbol] = {"stopped_at": from_iso(stopped_at), "qty": int(qty), "price": float(price)}
            except (ValueError, TypeError) as e:
                log.error("stopped row for %s is unreadable, skipped: %s", symbol, e)
        return out

    # ------------------------------------------------------------------ fills

    def fill_seen(self, exec_id: str) -> bool:
        return self._fetchone("SELECT 1 FROM fills WHERE exec_id = ?", (exec_id,)) is not None

    def record_fill(self, fill: Fill) -> None:
        """Record a processed fill; a fill whose exec_id is already stored is ignored."""
        self._execute(
            "INSERT OR IGNORE INTO fills (exec_id, order_id, symbol, side, qty, price, tag, time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fill.exec_id,
                str(fill.order_id),
                fill.symbol,
                Side(fill.side).value,
                int(fill.qty),
                float(fill.price),
                fill.tag,
                to_iso(fill.time),
            ),
        )
