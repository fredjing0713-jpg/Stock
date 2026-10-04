"""NYSE trading calendar and session classification (pure, no I/O).

All public functions work in America/New_York. Datetimes passed in must be
timezone-aware (any zone); they are converted to New York time first.

Sessions on a trading day (DESIGN.md §3.2):
    [04:00, open)        PRE
    [open, close)        REGULAR   09:30-16:00 (close 13:00 on early-close days)
    [close, after_end)   AFTER     until 20:00 (17:00 on early-close days)
    otherwise            CLOSED    (also all day on weekends / NYSE holidays)
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import holidays

from .models import Session

NY = ZoneInfo("America/New_York")

PRE_START = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
AFTER_END = time(20, 0)
EARLY_AFTER_END = time(17, 0)

# How far next_regular_open() searches before giving up (longest real NYSE gap is
# a few days; 30 is a generous safety bound).
_MAX_SEARCH_DAYS = 30


# --------------------------------------------------------------------------- calendar


@lru_cache(maxsize=None)
def _holiday_dates(year: int) -> frozenset[date]:
    """NYSE full-day closures that fall inside `year`.

    Neighbouring years are loaded too so an "observed" holiday that crosses a
    year boundary is never missed, then the set is filtered to `year`.
    """
    cal = holidays.financial_holidays("NYSE", years=range(year - 1, year + 2))
    return frozenset(d for d in cal if d.year == year)


def is_trading_day(d: date) -> bool:
    """Mon-Fri and not an NYSE holiday."""
    d = _as_date(d)
    return d.weekday() < 5 and d not in _holiday_dates(d.year)


def _thanksgiving(year: int) -> date:
    """4th Thursday of November."""
    nov1 = date(year, 11, 1)
    first_thu = 1 + (3 - nov1.weekday()) % 7  # Thursday == weekday 3
    return date(year, 11, first_thu + 21)


def is_early_close(d: date) -> bool:
    """13:00 close: day after Thanksgiving, Jul 3, Dec 24 (only when a trading day)."""
    d = _as_date(d)
    if not is_trading_day(d):
        return False
    if d == _thanksgiving(d.year) + timedelta(days=1):
        return True
    return (d.month, d.day) in ((7, 3), (12, 24))


def _at(d: date, t: time) -> datetime:
    """Wall-clock time `t` on day `d` in New York (zoneinfo picks the right DST offset)."""
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=NY)


def regular_hours(d: date) -> tuple[datetime, datetime] | None:
    """(open, close) in NY tz, or None if `d` is not a trading day."""
    d = _as_date(d)
    if not is_trading_day(d):
        return None
    close = EARLY_CLOSE if is_early_close(d) else REGULAR_CLOSE
    return _at(d, REGULAR_OPEN), _at(d, close)


def regular_open_for(d: date) -> datetime | None:
    """Regular-session open of `d` in NY tz, or None if not a trading day."""
    hours = regular_hours(d)
    return hours[0] if hours else None


def next_trading_day(d: date) -> date:
    """First trading day strictly after `d`."""
    d = _as_date(d)
    for i in range(1, _MAX_SEARCH_DAYS + 1):
        cand = d + timedelta(days=i)
        if is_trading_day(cand):
            return cand
    raise RuntimeError(f"no NYSE trading day within {_MAX_SEARCH_DAYS} days after {d}")


# --------------------------------------------------------------------------- sessions


def to_ny(now: datetime) -> datetime:
    """Convert an aware datetime to New York time; ValueError on naive input."""
    if not isinstance(now, datetime):
        raise TypeError(f"expected datetime, got {type(now).__name__}")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("naive datetime: a timezone-aware datetime is required")
    return now.astimezone(NY)


def session_at(now: datetime) -> Session:
    """Market session at instant `now` (any aware tz)."""
    ny = to_ny(now)
    d = ny.date()
    hours = regular_hours(d)
    if hours is None:
        return Session.CLOSED
    open_, close = hours
    after_end = _at(d, EARLY_AFTER_END if is_early_close(d) else AFTER_END)
    # Both sides share the NY tzinfo, so these are plain wall-clock comparisons.
    if _at(d, PRE_START) <= ny < open_:
        return Session.PRE
    if open_ <= ny < close:
        return Session.REGULAR
    if close <= ny < after_end:
        return Session.AFTER
    return Session.CLOSED


def next_regular_open(now: datetime) -> datetime:
    """Today's open if `now` is before it on a trading day, else the next trading day's open (NY tz)."""
    ny = to_ny(now)
    today_open = regular_open_for(ny.date())
    if today_open is not None and ny < today_open:
        return today_open
    nxt = regular_open_for(next_trading_day(ny.date()))
    if nxt is None:  # unreachable: next_trading_day only returns trading days
        raise RuntimeError("next trading day has no regular session")
    return nxt


# --------------------------------------------------------------------------- helpers


def _as_date(d: date) -> date:
    """Accept a date; an aware datetime is first converted to NY, a naive one used as is."""
    if isinstance(d, datetime):
        if d.tzinfo is not None and d.utcoffset() is not None:
            return d.astimezone(NY).date()
        return d.date()
    if not isinstance(d, date):
        raise TypeError(f"expected date, got {type(d).__name__}")
    return d
