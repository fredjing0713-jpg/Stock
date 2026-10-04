"""Tests for copybot.store (DESIGN.md §3.4)."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from copybot.models import Fill, Intent, Side
from copybot.store import SCHEMA_VERSION, Store, from_iso, to_iso

NY = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 2, 9, 50, 59, tzinfo=NY)


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    yield s
    s.close()


def _intent(symbol: str = "WDC", **kw) -> Intent:
    base = dict(
        symbol=symbol,
        target_weight=1.0,
        direction="buy",
        signal_price=422.6,
        reason="建仓 0%→100%",
        signal_key="abc123",
    )
    base.update(kw)
    return Intent(**base)


def _fill(exec_id: str = "0001.01", **kw) -> Fill:
    base = dict(
        order_id="42",
        exec_id=exec_id,
        symbol="WDC",
        side=Side.BUY,
        qty=10,
        price=422.61,
        tag="copybot:entry",
        time=T0,
    )
    base.update(kw)
    return Fill(**base)


# --------------------------------------------------------------------------- helpers


class TestIsoHelpers:
    def test_round_trip_keeps_instant_and_offset(self):
        s = to_iso(T0)
        assert s == "2026-10-02T09:50:59-04:00"
        back = from_iso(s)
        assert back == T0
        assert back.utcoffset() == timedelta(hours=-4)

    def test_naive_treated_as_utc_both_ways(self):
        naive = datetime(2026, 1, 2, 3, 4, 5)
        assert to_iso(naive) == "2026-01-02T03:04:05+00:00"
        got = from_iso("2026-01-02T03:04:05")
        assert got.tzinfo is not None
        assert got == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    def test_z_suffix_and_microseconds(self):
        dt = datetime(2026, 3, 4, 5, 6, 7, 891011, tzinfo=timezone.utc)
        assert from_iso(to_iso(dt)) == dt
        assert from_iso("2026-03-04T05:06:07Z") == datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)

    def test_none_and_datetime_passthrough(self):
        assert from_iso(None) is None
        assert from_iso(T0) is T0
        assert from_iso(datetime(2026, 1, 1)).tzinfo is not None

    def test_malformed_raises(self):
        with pytest.raises(ValueError):
            from_iso("not a date")


# --------------------------------------------------------------------------- schema / lifecycle


class TestLifecycle:
    def test_memory_db_has_all_tables(self, store: Store):
        names = {r[0] for r in store._fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"meta", "kv", "signals", "pending", "stopped", "fills"} <= names
        assert store.schema_version == SCHEMA_VERSION

    def test_file_db_creates_parent_dir_and_uses_wal(self, tmp_path):
        path = tmp_path / "a" / "b" / "copybot.db"
        s = Store(str(path))
        try:
            assert path.exists()
            assert s._fetchone("PRAGMA journal_mode")[0].lower() == "wal"
        finally:
            s.close()

    def test_close_is_idempotent_and_blocks_further_use(self):
        s = Store(":memory:")
        s.close()
        s.close()
        with pytest.raises(sqlite3.ProgrammingError):
            s.get("x")

    def test_context_manager_closes(self, tmp_path):
        with Store(str(tmp_path / "c.db")) as s:
            s.set("k", 1)
        with pytest.raises(sqlite3.ProgrammingError):
            s.get("k")

    def test_newer_schema_version_is_refused(self, tmp_path):
        path = str(tmp_path / "v.db")
        Store(path).close()
        conn = sqlite3.connect(path)
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION + 1),))
        conn.commit()
        conn.close()
        with pytest.raises(RuntimeError, match="schema_version"):
            Store(path)

    def test_reopen_keeps_schema_version(self, tmp_path):
        path = str(tmp_path / "v.db")
        Store(path).close()
        with Store(path) as s:
            assert s.schema_version == SCHEMA_VERSION
            assert s._fetchall("SELECT COUNT(*) FROM meta")[0][0] == 1


# --------------------------------------------------------------------------- signals


class TestSignals:
    def test_unknown_fingerprint_not_seen(self, store: Store):
        assert store.seen("nope") is False

    def test_record_then_seen_and_row_round_trip(self, store: Store):
        store.record_signal("fp1", "123456789", T0, "executed", "建仓 WDC")
        assert store.seen("fp1") is True
        assert store.seen("fp2") is False
        row = store._fetchone(
            "SELECT message_id, signal_ts, status, summary, recorded_at FROM signals WHERE fingerprint = 'fp1'"
        )
        assert row[0] == "123456789"
        assert from_iso(row[1]) == T0
        assert row[2] == "executed"
        assert row[3] == "建仓 WDC"
        recorded = from_iso(row[4])
        assert abs(datetime.now(timezone.utc) - recorded) < timedelta(minutes=1)

    def test_null_timestamp_and_int_message_id(self, store: Store):
        store.record_signal("fp1", 987, None, "ignored", "")  # type: ignore[arg-type]
        assert store.seen("fp1")
        row = store._fetchone("SELECT message_id, signal_ts FROM signals WHERE fingerprint = 'fp1'")
        assert row == ("987", None)

    def test_record_replaces_same_fingerprint(self, store: Store):
        store.record_signal("fp1", "1", T0, "pending", "a")
        store.record_signal("fp1", "1", T0, "executed", "b")
        rows = store._fetchall("SELECT status, summary FROM signals")
        assert rows == [("executed", "b")]


# --------------------------------------------------------------------------- key / value


class TestKV:
    def test_unknown_key_default(self, store: Store):
        assert store.get("missing") is None
        assert store.get("missing", 5) == 5
        assert store.get("missing", default={"a": 1}) == {"a": 1}

    @pytest.mark.parametrize(
        "value",
        [
            True,
            False,
            0,
            3.14159,
            "中文字符串",
            [1, 2, "x"],
            {"weights": {"WDC": 1.0, "BRK.B": 0.25}, "ts": "2026-10-02T09:50:59-04:00", "from_holdings": True},
        ],
    )
    def test_round_trip(self, store: Store, value):
        store.set("k", value)
        got = store.get("k")
        assert got == value
        assert type(got) is type(value)

    def test_stored_none_is_returned_not_default(self, store: Store):
        store.set("k", None)
        assert store.get("k", "dflt") is None

    def test_overwrite(self, store: Store):
        store.set("paused", False)
        store.set("paused", True)
        assert store.get("paused") is True

    def test_datetime_stored_as_iso_string(self, store: Store):
        store.set("last_signal_ts", T0)
        got = store.get("last_signal_ts")
        assert got == "2026-10-02T09:50:59-04:00"
        assert from_iso(got) == T0

    def test_nested_datetime_and_date(self, store: Store):
        store.set("pp", {"weights": {"WDC": 1.0}, "ts": T0, "d": date(2026, 10, 2)})
        got = store.get("pp")
        assert got == {"weights": {"WDC": 1.0}, "ts": to_iso(T0), "d": "2026-10-02"}

    def test_naive_datetime_becomes_utc(self, store: Store):
        store.set("t", datetime(2026, 1, 1, 12, 0, 0))
        assert from_iso(store.get("t")) == datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    def test_str_enum_serialises_as_value(self, store: Store):
        store.set("side", Side.SELL)
        assert store.get("side") == "SELL"

    def test_unserialisable_raises_and_keeps_old_value(self, store: Store):
        store.set("k", 1)
        with pytest.raises(TypeError):
            store.set("k", {1, 2})
        with pytest.raises(TypeError):
            store.set("k", object())
        assert store.get("k") == 1


# --------------------------------------------------------------------------- pending intents


class TestPending:
    def test_empty(self, store: Store):
        assert store.list_pending() == []

    def test_round_trip(self, store: Store):
        intent = _intent()
        due = datetime(2026, 10, 5, 9, 30, 5, tzinfo=NY)
        store.upsert_pending(intent, due)
        [(got, got_due)] = store.list_pending()
        assert got == intent
        assert isinstance(got, Intent)
        assert got_due == due
        assert got_due.tzinfo is not None

    def test_round_trip_none_price_and_zero_weight(self, store: Store):
        intent = _intent("BE", target_weight=0.0, direction="sell", signal_price=None, reason="清仓 50%→0%")
        store.upsert_pending(intent, T0)
        [(got, _)] = store.list_pending()
        assert got == intent
        assert got.signal_price is None
        assert got.target_weight == 0.0

    def test_float_weight_exact(self, store: Store):
        intent = _intent(target_weight=1 / 3)
        store.upsert_pending(intent, T0)
        assert store.list_pending()[0][0].target_weight == 1 / 3

    def test_upsert_replaces_per_symbol(self, store: Store):
        store.upsert_pending(_intent("WDC", target_weight=1.0), T0)
        store.upsert_pending(_intent("ARM", target_weight=0.5), T0)
        newer = _intent("WDC", target_weight=0.5, direction="sell", signal_key="fp2")
        later = T0 + timedelta(days=1)
        store.upsert_pending(newer, later)
        items = store.list_pending()
        assert len(items) == 2
        by_sym = {i.symbol: (i, d) for i, d in items}
        assert by_sym["WDC"] == (newer, later)
        assert by_sym["ARM"][0].target_weight == 0.5

    def test_sorted_by_due_then_symbol(self, store: Store):
        d1 = datetime(2026, 10, 5, 9, 30, 5, tzinfo=NY)
        d2 = d1 + timedelta(minutes=1)
        store.upsert_pending(_intent("ZZZ"), d1)
        store.upsert_pending(_intent("MMM"), d2)
        store.upsert_pending(_intent("AAA"), d2)
        store.upsert_pending(_intent("BBB"), d1)
        assert [(i.symbol, d) for i, d in store.list_pending()] == [
            ("BBB", d1),
            ("ZZZ", d1),
            ("AAA", d2),
            ("MMM", d2),
        ]

    def test_sorting_is_by_instant_not_text(self, store: Store):
        # 13:00 UTC == 09:00 NY (EDT). As text "2026-10-05T09:00:00-04:00" < "2026-10-05T12:00:00+00:00",
        # but in time the UTC one (12:00Z) is earlier than the NY one (13:00Z).
        ny_due = datetime(2026, 10, 5, 9, 0, tzinfo=NY)
        utc_due = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
        store.upsert_pending(_intent("AAA"), ny_due)
        store.upsert_pending(_intent("ZZZ"), utc_due)
        assert [i.symbol for i, _ in store.list_pending()] == ["ZZZ", "AAA"]

    def test_delete_and_clear(self, store: Store):
        for sym in ("A", "B", "C"):
            store.upsert_pending(_intent(sym), T0)
        store.delete_pending("B")
        store.delete_pending("NOT_THERE")  # no error
        assert [i.symbol for i, _ in store.list_pending()] == ["A", "C"]
        store.clear_pending()
        assert store.list_pending() == []

    def test_unreadable_row_skipped_not_fatal(self, store: Store, caplog):
        store.upsert_pending(_intent("GOOD"), T0)
        store._execute(
            "INSERT INTO pending (symbol, intent_json, due_at) VALUES (?, ?, ?)", ("BAD", "{not json", to_iso(T0))
        )
        store._execute(
            "INSERT INTO pending (symbol, intent_json, due_at) VALUES (?, ?, ?)",
            ("MISSING", '{"symbol": "MISSING"}', to_iso(T0)),
        )
        assert [i.symbol for i, _ in store.list_pending()] == ["GOOD"]
        assert "BAD" in caplog.text and "MISSING" in caplog.text

    def test_extra_unknown_field_ignored(self, store: Store):
        store._execute(
            "INSERT INTO pending (symbol, intent_json, due_at) VALUES (?, ?, ?)",
            (
                "X",
                '{"symbol": "X", "target_weight": 0.2, "direction": "both", "future_field": 1}',
                to_iso(T0),
            ),
        )
        [(got, _)] = store.list_pending()
        assert got == Intent("X", 0.2, "both")


# --------------------------------------------------------------------------- stopped


class TestStopped:
    def test_empty(self, store: Store):
        assert store.stopped() == {}

    def test_round_trip(self, store: Store):
        store.mark_stopped("WDC", T0, 25, 380.12)
        got = store.stopped()
        assert got == {"WDC": {"stopped_at": T0, "qty": 25, "price": 380.12}}
        assert got["WDC"]["stopped_at"].tzinfo is not None
        assert isinstance(got["WDC"]["qty"], int)
        assert isinstance(got["WDC"]["price"], float)

    def test_mark_replaces_and_clear(self, store: Store):
        store.mark_stopped("WDC", T0, 25, 380.12)
        store.mark_stopped("ARM", T0, 3, 100)
        later = T0 + timedelta(hours=1)
        store.mark_stopped("WDC", later, 10, 370.0)
        got = store.stopped()
        assert got["WDC"] == {"stopped_at": later, "qty": 10, "price": 370.0}
        assert got["ARM"]["price"] == 100.0
        store.clear_stopped("WDC")
        store.clear_stopped("NOT_THERE")
        assert set(store.stopped()) == {"ARM"}


# --------------------------------------------------------------------------- fills


class TestFills:
    def test_unknown_not_seen(self, store: Store):
        assert store.fill_seen("x") is False

    def test_round_trip(self, store: Store):
        f = _fill()
        store.record_fill(f)
        assert store.fill_seen(f.exec_id) is True
        row = store._fetchone(
            "SELECT order_id, symbol, side, qty, price, tag, time FROM fills WHERE exec_id = ?", (f.exec_id,)
        )
        order_id, symbol, side, qty, price, tag, time = row
        rebuilt = Fill(order_id, f.exec_id, symbol, Side(side), qty, price, tag, from_iso(time))
        assert rebuilt == f
        assert rebuilt.time.tzinfo is not None

    def test_duplicate_exec_id_ignored(self, store: Store):
        store.record_fill(_fill("e1", qty=10))
        store.record_fill(_fill("e1", qty=99))  # duplicate delivery: first record wins, no error
        store.record_fill(_fill("e2", side=Side.SELL, qty=5))
        rows = store._fetchall("SELECT exec_id, side, qty FROM fills ORDER BY exec_id")
        assert rows == [("e1", "BUY", 10), ("e2", "SELL", 5)]


# --------------------------------------------------------------------------- persistence / threads


def test_persistence_across_instances(tmp_path):
    path = str(tmp_path / "data" / "copybot.db")
    due = datetime(2026, 10, 5, 9, 30, 5, tzinfo=NY)
    s1 = Store(path)
    s1.record_signal("fp1", "m1", T0, "executed", "建仓 WDC")
    s1.set("paused", True)
    s1.set("last_signal_ts", T0)
    s1.set("provider_portfolio", {"weights": {"WDC": 1.0}, "ts": T0, "from_holdings": True})
    s1.upsert_pending(_intent("WDC"), due)
    s1.mark_stopped("ARM", T0, 7, 99.5)
    s1.record_fill(_fill("e1"))
    s1.close()

    s2 = Store(path)
    try:
        assert s2.seen("fp1")
        assert s2.get("paused") is True
        assert from_iso(s2.get("last_signal_ts")) == T0
        assert s2.get("provider_portfolio") == {"weights": {"WDC": 1.0}, "ts": to_iso(T0), "from_holdings": True}
        assert s2.list_pending() == [(_intent("WDC"), due)]
        assert s2.stopped() == {"ARM": {"stopped_at": T0, "qty": 7, "price": 99.5}}
        assert s2.fill_seen("e1")
    finally:
        s2.close()


def test_writes_visible_to_second_open_connection(tmp_path):
    # Autocommit: a write is visible to another connection immediately, no commit needed.
    path = str(tmp_path / "x.db")
    a, b = Store(path), Store(path)
    try:
        a.set("k", "v")
        assert b.get("k") == "v"
    finally:
        a.close()
        b.close()


def test_concurrent_threads(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(50):
                s.set(f"k{n}", i)
                s.upsert_pending(_intent(f"S{n}", target_weight=i / 100), T0)
                s.list_pending()
        except BaseException as e:  # pragma: no cover - only on failure
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == []
        assert all(s.get(f"k{n}") == 49 for n in range(8))
        assert len(s.list_pending()) == 8
    finally:
        s.close()
