"""Shared data types. Pure data, no I/O.

Conventions used everywhere in the code base:
- Weights are fractions (0.5 == 50%), never percent numbers.
- Money is USD floats. Quantities are whole shares (int) unless noted.
- Symbols use the provider's spelling, upper-case (e.g. "WDC", "BRK.B").
  Only the broker adapter translates to broker-specific symbols.
- Datetimes are timezone-aware.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# Every order the bot places carries an orderRef starting with this prefix so the
# bot can tell its own orders apart from manual ones (also across restarts).
BOT_TAG_PREFIX = "copybot"
STOP_TAG = f"{BOT_TAG_PREFIX}:stop"


def _valid_price(x: float | None) -> float | None:
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x) or x <= 0:
        return None
    return x


class Side(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, enum.Enum):
    MARKET = "MKT"
    LIMIT = "LMT"
    STOP = "STP"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"  # submitted / working / held until open
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"  # may still carry filled_qty > 0 (partial fill then cancel)
    REJECTED = "REJECTED"


DONE_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED})


class Session(str, enum.Enum):
    PRE = "PRE"  # 04:00-09:30 ET
    REGULAR = "REGULAR"  # 09:30-16:00 ET (13:00 on early-close days)
    AFTER = "AFTER"  # close-20:00 ET (17:00 on early-close days)
    CLOSED = "CLOSED"  # overnight, weekends, holidays

    @property
    def is_extended(self) -> bool:
        return self in (Session.PRE, Session.AFTER)


# --------------------------------------------------------------------------- signals


@dataclass(frozen=True)
class Change:
    """One line of the provider's「仓位变动」section.

    e.g. ``建仓 WDC | 仓位：0.0% → 100.0% | 成交价：422.6``
    """

    action: str  # raw action word: 建仓 / 加仓 / 减仓 / 清仓 / ...
    symbol: str
    from_weight: float  # fraction, 0.0 .. (normally) 1.0
    to_weight: float
    price: float | None  # 成交价; None when「无」

    @property
    def is_increase(self) -> bool:
        return self.to_weight > self.from_weight + 1e-9

    @property
    def is_decrease(self) -> bool:
        return self.to_weight < self.from_weight - 1e-9


@dataclass(frozen=True)
class Holding:
    """One line of the provider's「持仓明细」section.

    e.g. ``WDC | 仓位：100.0% | 成本：422.6 | 盈亏：+0.1%``
    """

    symbol: str
    weight: float  # fraction
    cost: float | None
    pnl_pct: float | None  # percent number as written (+0.1 means +0.1%)


@dataclass(frozen=True)
class Signal:
    changes: tuple[Change, ...]
    holdings: tuple[Holding, ...]
    # True when the「持仓明细」header is present. A present-but-empty section means
    # the provider is 100% cash. False means "unknown" (do not infer anything).
    has_holdings_section: bool
    timestamp: datetime | None  # tz-aware America/New_York, from "(2026-10-02 09:50:59 美东)"
    raw_text: str
    fingerprint: str  # sha256 hex of the normalized text, used for de-duplication

    def holding_weights(self) -> dict[str, float]:
        return {h.symbol: h.weight for h in self.holdings}


# --------------------------------------------------------------------------- market / account


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    close: float | None = None  # previous close

    def buy_ref(self) -> float | None:
        """Price we expect to pay: ask → last → close."""
        return _valid_price(self.ask) or _valid_price(self.last) or _valid_price(self.close)

    def sell_ref(self) -> float | None:
        """Price we expect to receive: bid → last → close."""
        return _valid_price(self.bid) or _valid_price(self.last) or _valid_price(self.close)

    def mark(self) -> float | None:
        """Best guess of fair value: last → mid → close."""
        last = _valid_price(self.last)
        if last:
            return last
        bid, ask = _valid_price(self.bid), _valid_price(self.ask)
        if bid and ask:
            return (bid + ask) / 2
        return _valid_price(self.close) or bid or ask


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float  # shares; negative == short (the bot never intends to be short)
    avg_cost: float  # per share, USD


@dataclass(frozen=True)
class AccountSnapshot:
    """Account-level values converted to USD."""

    net_liquidation: float  # 净清算值 / 总资产
    excess_liquidity: float  # 剩余流动性 = equity with loan - maintenance margin
    available_funds: float  # 可用资金 = equity with loan - initial margin
    maint_margin: float  # 维持保证金 (FullMaintMarginReq)
    init_margin: float  # 初始保证金 (FullInitMarginReq)
    gross_position_value: float  # Σ |position market value|
    ts: datetime

    @property
    def el_ratio(self) -> float:
        """剩余流动性 / 净资产."""
        if self.net_liquidation <= 0:
            return 0.0
        return self.excess_liquidity / self.net_liquidation

    @property
    def leverage(self) -> float:
        """总持仓市值 / 净资产."""
        if self.net_liquidation <= 0:
            return 0.0
        return self.gross_position_value / self.net_liquidation


@dataclass(frozen=True)
class MarginRate:
    """Margin requirement as a fraction of position value (0.30 == 30%)."""

    init: float
    maint: float
    source: str = "default"  # "whatif" | "default" | "override"


# --------------------------------------------------------------------------- orders


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    limit_price: float | None = None
    stop_price: float | None = None
    outside_rth: bool = False
    tif: Literal["DAY", "GTC"] = "DAY"
    tag: str = BOT_TAG_PREFIX  # orderRef, must start with BOT_TAG_PREFIX


@dataclass
class OrderInfo:
    """Broker-agnostic snapshot of an order."""

    order_id: str
    symbol: str
    side: Side
    qty: int  # total quantity
    filled_qty: int
    avg_fill_price: float | None
    order_type: OrderType
    status: OrderStatus
    limit_price: float | None = None
    stop_price: float | None = None
    outside_rth: bool = False
    tif: str = "DAY"
    tag: str = ""
    message: str = ""  # reject / error reason if any

    @property
    def is_done(self) -> bool:
        return self.status in DONE_STATUSES

    @property
    def remaining(self) -> int:
        return max(0, self.qty - self.filled_qty)

    @property
    def is_stop(self) -> bool:
        return self.tag.startswith(STOP_TAG)


@dataclass(frozen=True)
class Fill:
    order_id: str
    exec_id: str
    symbol: str
    side: Side
    qty: int
    price: float
    tag: str
    time: datetime


# --------------------------------------------------------------------------- planning


@dataclass(frozen=True)
class Intent:
    """What we want for one symbol, independent of *when* it is executed.

    The target is always a weight of G (the "100% 仓位" dollar amount), so it is
    re-sized with fresh prices / NLV whenever it is executed and re-executing the
    same intent is idempotent.
    direction limits the trade: "buy" never sells, "sell" never buys, "both" either.
    target_weight == 0 means "close the position" and needs no price.
    kind:
      "signal"    — a change line of his signal (only kind traded in pre/after-hours)
      "confirm"   — the same change broadcast again (he re-announces it when it fills)
      "rebalance" — his unchanged holdings, re-sized to the new G after a 调仓
      "sync"      — !sync
    band: skip the trade when |delta| < band · target (avoids churn); 0 = always trade.
    """

    symbol: str
    target_weight: float
    direction: Literal["buy", "sell", "both"]
    signal_price: float | None = None  # provider 成交价, for the chase / deviation guard
    reason: str = ""  # human readable, e.g. "建仓 0%→100%"
    signal_key: str = ""  # fingerprint of the originating signal ("sync" for !sync)
    kind: Literal["signal", "confirm", "rebalance", "sync"] = "signal"
    band: float = 0.0


@dataclass(frozen=True)
class PlannedOrder:
    symbol: str
    side: Side
    qty: int
    ref_price: float  # price used for sizing / margin estimates
    current_qty: int
    target_qty: int
    note: str = ""


@dataclass(frozen=True)
class SkipNote:
    symbol: str
    reason: str


@dataclass(frozen=True)
class GrossTarget:
    """G — the market value that「100% 仓位」maps to."""

    gross: float
    g_maint: float  # limit from the 剩余流动性 reserve
    g_init: float  # limit from initial margin (what the broker lets us open)
    g_cap: float  # limit from max_gross_leverage
    blended_maint: float
    blended_init: float

    @property
    def binding(self) -> str:
        return min(
            (("reserve", self.g_maint), ("init_margin", self.g_init), ("leverage_cap", self.g_cap)),
            key=lambda kv: kv[1],
        )[0]


@dataclass(frozen=True)
class DesiredStop:
    symbol: str
    qty: int
    stop_price: float


@dataclass
class StopActions:
    create: list[DesiredStop] = field(default_factory=list)
    modify: list[tuple[str, DesiredStop]] = field(default_factory=list)  # (order_id, new state)
    cancel: list[str] = field(default_factory=list)  # order ids

    @property
    def empty(self) -> bool:
        return not (self.create or self.modify or self.cancel)


@dataclass(frozen=True)
class MessageMeta:
    """Where a Discord message came from."""

    message_id: str
    author_id: int
    received_at: datetime  # tz-aware
