"""Tests for copybot.market_hours (DESIGN.md §3.2)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from copybot.market_hours import (
    NY,
    is_early_close,
    is_trading_day,
    next_regular_open,
    next_trading_day,
    regular_hours,
    regular_open_for,
    session_at,
)
from copybot.models import Session

UTC = timezone.utc


def ny(y: int, m: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, s, tzinfo=NY)


# --------------------------------------------------------------------------- sessions on a normal day


@pytest.mark.parametrize(
    ("h", "mi", "s", "expected"),
    [
        (0, 0, 0, Session.CLOSED),
        (3, 59, 0, Session.CLOSED),
        (3, 59, 59, Session.CLOSED),
        (4, 0, 0, Session.PRE),
        (9, 29, 59, Session.PRE),
        (9, 30, 0, Session.REGULAR),
        (12, 0, 0, Session.REGULAR),
        (15, 59, 59, Session.REGULAR),
        (16, 0, 0, Session.AFTER),
        (19, 59, 0, Session.AFTER),
        (19, 59, 59, Session.AFTER),
        (20, 0, 0, Session.CLOSED),
        (23, 59, 59, Session.CLOSED),
    ],
)
def test_session_normal_friday(h: int, mi: int, s: int, expected: Session) -> None:
    assert session_at(ny(2026, 10, 2, h, mi, s)) is expected


def test_normal_day_hours() -> None:
    d = date(2026, 10, 2)
    assert is_trading_day(d)
    assert not is_early_close(d)
    assert regular_hours(d) == (ny(2026, 10, 2, 9, 30), ny(2026, 10, 2, 16, 0))
    assert regular_open_for(d) == ny(2026, 10, 2, 9, 30)


@pytest.mark.parametrize("h", [0, 4, 9, 10, 12, 16, 19, 23])
def test_weekend_closed_all_day(h: int) -> None:
    assert session_at(ny(2026, 10, 3, h, 30)) is Session.CLOSED  # Saturday
    assert session_at(ny(2026, 10, 4, h, 30)) is Session.CLOSED  # Sunday


def test_weekend_is_not_trading_day() -> None:
    assert not is_trading_day(date(2026, 10, 3))
    assert not is_trading_day(date(2026, 10, 4))
    assert regular_hours(date(2026, 10, 3)) is None
    assert regular_open_for(date(2026, 10, 3)) is None


# --------------------------------------------------------------------------- holidays


@pytest.mark.parametrize(
    "d",
    [
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 7, 3),  # Independence Day observed (Jul 4 is Saturday)
        date(2026, 12, 25),  # Christmas
        date(2027, 3, 26),  # Good Friday
        date(2027, 1, 1),  # New Year's Day (Friday)
        date(2026, 9, 7),  # Labor Day
        date(2026, 6, 19),  # Juneteenth
    ],
)
def test_holidays_closed(d: date) -> None:
    assert not is_trading_day(d)
    assert not is_early_close(d)
    assert regular_hours(d) is None
    for h in (4, 9, 10, 15, 16, 19):
        assert session_at(datetime(d.year, d.month, d.day, h, 30, tzinfo=NY)) is Session.CLOSED


def test_days_around_holidays_are_trading_days() -> None:
    assert is_trading_day(date(2026, 7, 2))
    assert is_trading_day(date(2026, 11, 25))
    assert is_trading_day(date(2026, 11, 27))
    assert is_trading_day(date(2026, 12, 31))
    assert is_trading_day(date(2027, 1, 4))


# --------------------------------------------------------------------------- early close


@pytest.mark.parametrize(
    ("h", "mi", "expected"),
    [
        (3, 59, Session.CLOSED),
        (4, 0, Session.PRE),
        (9, 29, Session.PRE),
        (9, 30, Session.REGULAR),
        (12, 59, Session.REGULAR),
        (13, 0, Session.AFTER),
        (16, 0, Session.AFTER),
        (16, 59, Session.AFTER),
        (17, 0, Session.CLOSED),
        (19, 0, Session.CLOSED),
    ],
)
def test_session_early_close_day_after_thanksgiving(h: int, mi: int, expected: Session) -> None:
    assert session_at(ny(2026, 11, 27, h, mi)) is expected


def test_early_close_days() -> None:
    assert is_early_close(date(2026, 11, 27))  # day after Thanksgiving
    assert is_early_close(date(2026, 12, 24))  # Christmas Eve (Thursday)
    assert regular_hours(date(2026, 11, 27)) == (ny(2026, 11, 27, 9, 30), ny(2026, 11, 27, 13, 0))
    assert regular_hours(date(2026, 12, 24)) == (ny(2026, 12, 24, 9, 30), ny(2026, 12, 24, 13, 0))
    assert session_at(ny(2026, 12, 24, 13, 0)) is Session.AFTER
    assert session_at(ny(2026, 12, 24, 17, 0)) is Session.CLOSED


def test_not_early_close() -> None:
    # Jul 3 2026 is the observed holiday, so Jul 2 is a normal full day.
    assert not is_early_close(date(2026, 7, 2))
    assert session_at(ny(2026, 7, 2, 15, 0)) is Session.REGULAR
    assert regular_hours(date(2026, 7, 2))[1] == ny(2026, 7, 2, 16, 0)  # type: ignore[index]
    assert not is_early_close(date(2026, 10, 2))
    assert not is_early_close(date(2026, 11, 25))  # day before Thanksgiving
    # Dec 24 2027 is the observed Christmas holiday: closed, not an early close.
    assert not is_trading_day(date(2027, 12, 24))
    assert not is_early_close(date(2027, 12, 24))


def test_early_close_jul3_when_trading_day() -> None:
    # Jul 3 2025 was a Thursday: early close.
    assert is_early_close(date(2025, 7, 3))
    assert session_at(ny(2025, 7, 3, 13, 0)) is Session.AFTER


@pytest.mark.parametrize(
    ("year", "expected"),
    [(2024, date(2024, 11, 29)), (2025, date(2025, 11, 28)), (2026, date(2026, 11, 27)),
     (2027, date(2027, 11, 26)), (2028, date(2028, 11, 24))],
)
def test_day_after_thanksgiving_each_year(year: int, expected: date) -> None:
    assert is_early_close(expected)
    assert not is_early_close(expected - timedelta(days=7))


# --------------------------------------------------------------------------- next_regular_open


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (ny(2026, 10, 2, 10, 0), ny(2026, 10, 5, 9, 30)),  # Fri during RTH → Mon
        (ny(2026, 10, 2, 8, 0), ny(2026, 10, 2, 9, 30)),  # Fri pre-market → same day
        (ny(2026, 10, 2, 0, 0), ny(2026, 10, 2, 9, 30)),  # Fri overnight → same day
        (ny(2026, 10, 2, 9, 29, 59), ny(2026, 10, 2, 9, 30)),
        (ny(2026, 10, 2, 9, 30), ny(2026, 10, 5, 9, 30)),  # exactly at open → next day
        (ny(2026, 10, 2, 21, 0), ny(2026, 10, 5, 9, 30)),
        (ny(2026, 10, 3, 12, 0), ny(2026, 10, 5, 9, 30)),  # Saturday
        (ny(2026, 11, 25, 17, 0), ny(2026, 11, 27, 9, 30)),  # skip Thanksgiving
        (ny(2026, 12, 31, 18, 0), ny(2027, 1, 4, 9, 30)),  # skip Jan 1 (Fri) + weekend
        (ny(2026, 7, 2, 16, 30), ny(2026, 7, 6, 9, 30)),  # skip Jul 3 observed + weekend
        (ny(2027, 3, 25, 12, 0), ny(2027, 3, 29, 9, 30)),  # skip Good Friday
    ],
)
def test_next_regular_open(now: datetime, expected: datetime) -> None:
    got = next_regular_open(now)
    assert got == expected
    assert got.tzinfo is NY
    assert (got.hour, got.minute) == (9, 30)


def test_next_regular_open_accepts_utc() -> None:
    # 2026-10-02 14:00Z == 10:00 EDT (during RTH) → Monday open.
    got = next_regular_open(datetime(2026, 10, 2, 14, 0, tzinfo=UTC))
    assert got == ny(2026, 10, 5, 9, 30)
    assert got.tzinfo is NY
    # 2026-10-03 03:00Z == Fri 23:00 EDT → still Friday in NY → Monday open.
    assert next_regular_open(datetime(2026, 10, 3, 3, 0, tzinfo=UTC)) == ny(2026, 10, 5, 9, 30)
    # 2026-10-05 05:00Z == Mon 01:00 EDT → Monday open the same day.
    assert next_regular_open(datetime(2026, 10, 5, 5, 0, tzinfo=UTC)) == ny(2026, 10, 5, 9, 30)


def test_next_regular_open_across_dst_change() -> None:
    # DST ends Sun 2026-11-01: Friday open is 13:30Z, Monday open is 14:30Z.
    got = next_regular_open(ny(2026, 10, 30, 12, 0))
    assert got == ny(2026, 11, 2, 9, 30)
    assert got.astimezone(UTC) == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)


def test_next_trading_day() -> None:
    assert next_trading_day(date(2026, 10, 2)) == date(2026, 10, 5)
    assert next_trading_day(date(2026, 11, 25)) == date(2026, 11, 27)
    assert next_trading_day(date(2026, 12, 31)) == date(2027, 1, 4)


# --------------------------------------------------------------------------- tz handling


def test_session_utc_summer() -> None:
    # 13:30Z == 09:30 EDT (UTC-4).
    assert session_at(datetime(2026, 10, 2, 13, 30, tzinfo=UTC)) is Session.REGULAR
    assert session_at(datetime(2026, 10, 2, 13, 29, tzinfo=UTC)) is Session.PRE
    assert session_at(datetime(2026, 10, 2, 20, 0, tzinfo=UTC)) is Session.AFTER  # 16:00 EDT
    assert session_at(datetime(2026, 10, 3, 0, 0, tzinfo=UTC)) is Session.CLOSED  # 20:00 EDT


def test_session_utc_winter() -> None:
    # 14:30Z == 09:30 EST (UTC-5).
    assert session_at(datetime(2026, 12, 1, 14, 30, tzinfo=UTC)) is Session.REGULAR
    assert session_at(datetime(2026, 12, 1, 14, 29, tzinfo=UTC)) is Session.PRE
    assert session_at(datetime(2026, 12, 1, 9, 0, tzinfo=UTC)) is Session.PRE  # 04:00 EST
    assert session_at(datetime(2026, 12, 1, 8, 59, tzinfo=UTC)) is Session.CLOSED


def test_session_utc_date_rollover() -> None:
    # Sat 2026-10-03 01:00Z is still Friday 21:00 EDT → CLOSED (after 20:00).
    assert session_at(datetime(2026, 10, 3, 1, 0, tzinfo=UTC)) is Session.CLOSED
    # Sat 2026-10-03 23:30Z is Saturday in NY too.
    assert session_at(datetime(2026, 10, 3, 23, 30, tzinfo=UTC)) is Session.CLOSED
    # Fri 2026-10-02 23:59Z == 19:59 EDT → AFTER.
    assert session_at(datetime(2026, 10, 2, 23, 59, tzinfo=UTC)) is Session.AFTER


def test_session_other_timezone() -> None:
    # Asia/Shanghai (UTC+8): 2026-10-02 21:30 CST == 09:30 EDT.
    from zoneinfo import ZoneInfo

    sh = ZoneInfo("Asia/Shanghai")
    assert session_at(datetime(2026, 10, 2, 21, 30, tzinfo=sh)) is Session.REGULAR
    assert session_at(datetime(2026, 10, 2, 21, 29, tzinfo=sh)) is Session.PRE


def test_naive_datetime_raises() -> None:
    with pytest.raises(ValueError):
        session_at(datetime(2026, 10, 2, 10, 0))
    with pytest.raises(ValueError):
        next_regular_open(datetime(2026, 10, 2, 10, 0))


def test_regular_hours_offsets_follow_dst() -> None:
    summer = regular_hours(date(2026, 10, 2))
    winter = regular_hours(date(2026, 12, 1))
    assert summer is not None and winter is not None
    assert summer[0].utcoffset() == timedelta(hours=-4)
    assert winter[0].utcoffset() == timedelta(hours=-5)


def test_trading_day_accepts_aware_datetime_in_ny() -> None:
    # Sat 2026-10-03 02:00Z is Friday evening in New York.
    assert is_trading_day(datetime(2026, 10, 3, 2, 0, tzinfo=UTC))
