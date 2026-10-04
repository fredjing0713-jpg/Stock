"""End-to-end tests of copybot.engine against the in-memory FakeBroker."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeBroker

from copybot.broker.base import Broker
from copybot.broker.dryrun import DryRunBroker
from copybot.config import AppConfig, DiscordConfig, RiskConfig, StorageConfig, TradingConfig
from copybot.engine import Engine
from copybot.models import (
    STOP_TAG,
    Fill,
    Intent,
    MarginRate,
    MessageMeta,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)
from copybot.notifier import LogNotifier
from copybot.parser import parse_signal
from copybot.sizing import round_to_tick
from copybot.store import Store

NY = ZoneInfo("America/New_York")
MKT, LMT, STP = OrderType.MARKET, OrderType.LIMIT, OrderType.STOP
BUY, SELL = Side.BUY, Side.SELL

EXAMPLE = """组合收益
10月收益：3.4%
总收益：168.3%
仓位变动
建仓 WDC | 仓位：0.0% → 100.0% | 成交价：422.6
清仓 BE | 仓位：50.0% → 0.0% | 成交价：无
清仓 ARM | 仓位：50.0% → 0.0% | 成交价：无
持仓明细
WDC | 仓位：100.0% | 成本：422.6 | 盈亏：+0.1%
(2026-10-02 09:50:59 美东)"""


# --------------------------------------------------------------------------- helpers


def ny(*args: int) -> datetime:
    return datetime(*args, tzinfo=NY)


REGULAR_T = ny(2026, 10, 2, 9, 51, 2)  # Friday, 3 s after the example signal
PRE_T = ny(2026, 10, 2, 8, 0, 3)
OPEN_DUE = ny(2026, 10, 2, 9, 30, 5)


class Clock:
    """Mutable clock shared by the engine and the fake broker."""

    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def set(self, t: datetime) -> None:
        self.t = t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


def make_cfg(dry_run: bool = False, risk: RiskConfig | None = None, **trading: object) -> AppConfig:
    # Most scenarios were written for a 1.9x cap without Reg T floor / 二次调仓; pin those.
    opts: dict[str, object] = {
        "dry_run": dry_run,
        "min_order_value_usd": 1.0,
        "fill_timeout_seconds": 1,
        "regt_overnight_check": False,
        "max_gross_leverage": 1.9,
        "rebalance_on_signal": False,
    }
    opts.update(trading)
    return AppConfig(
        discord=DiscordConfig(signal_channel_id=1, log_channel_id=2, owner_ids=[3]),
        trading=TradingConfig(**opts),
        risk=risk or RiskConfig(),
    )


def signal(changes: list[str], holdings: list[str] | None, ts: str) -> str:
    lines = ["组合收益", "仓位变动", *changes]
    if holdings is not None:
        lines += ["持仓明细", *holdings]
    lines.append(f"({ts} 美东)")
    return "\n".join(lines)


@dataclass
class Env:
    engine: Engine
    broker: FakeBroker
    store: Store
    notifier: LogNotifier
    clock: Clock
    _n: int = 0

    def meta(self) -> MessageMeta:
        self._n += 1
        return MessageMeta(str(self._n), 42, self.clock())

    async def send(self, text: str) -> bool:
        return await self.engine.handle_text(text, self.meta())

    def texts(self) -> list[str]:
        return [m[0] for m in self.notifier.messages]

    def joined(self) -> str:
        return "\n".join(self.texts())

    def status_of(self, text: str) -> str | None:
        sig = parse_signal(text)
        assert sig is not None
        row = self.store._fetchone("SELECT status FROM signals WHERE fingerprint = ?", (sig.fingerprint,))
        return row[0] if row else None


@pytest.fixture
def make() -> Iterator[Callable[..., Env]]:
    stores: list[Store] = []

    def factory(
        at: datetime = REGULAR_T,
        cash: float = 100_000.0,
        cfg: AppConfig | None = None,
        wrap: Callable[[FakeBroker], Broker] | None = None,
    ) -> Env:
        clock = Clock(at)
        fake = FakeBroker(cash=cash, clock=clock)
        broker = wrap(fake) if wrap else fake
        store = Store(":memory:")
        stores.append(store)
        notifier = LogNotifier()
        engine = Engine(cfg or make_cfg(), broker, store, notifier, clock=clock)
        engine.cancel_wait_seconds = 0.05
        # What start() registers, without the background loops (keeps tests deterministic).
        broker.add_fill_handler(engine._on_fill)
        broker.add_connection_handler(engine._on_connection)
        return Env(engine, fake, store, notifier, clock)

    yield factory
    for s in stores:
        s.close()


def example_account(b: FakeBroker) -> None:
    """BE 250 + ARM 400, no cash: NLV = 100,000."""
    b.set_position("BE", 250, 200.0)
    b.set_quote("BE", 199.9, 200.1, 200.0)
    b.set_position("ARM", 400, 125.0)
    b.set_quote("ARM", 124.9, 125.1, 125.0)
    b.set_quote("WDC", 422.4, 422.8, 422.6)
    b.rates["WDC"] = MarginRate(0.30, 0.25, "whatif")


def orders(b: FakeBroker) -> list[tuple[Side, str, int, OrderType]]:
    return [(r.side, r.symbol, r.qty, r.order_type) for r in b.placed]


# --------------------------------------------------------------------------- 1-4 regular flow and guards


async def test_regular_example_sells_first_then_buys_and_sets_stop(make):
    env = make(cash=0.0)
    b = env.broker
    example_account(b)

    assert await env.send(EXAMPLE) is True

    fp8 = parse_signal(EXAMPLE).fingerprint[:8]
    # G = min(0.75·NLV/0.25, 0.98·NLV/0.30, 1.9·NLV) = 190,000 → floor(G / ask)
    wdc_qty = math.floor(1.9 * 100_000 / 422.8)
    assert wdc_qty == 449
    assert orders(b) == [(SELL, "BE", 250, MKT), (SELL, "ARM", 400, MKT), (BUY, "WDC", wdc_qty, MKT)]
    assert all(r.tag == f"copybot:mkt:{fp8}" and r.tif == "DAY" and not r.outside_rth for r in b.placed)
    assert b.pos == {"WDC": [449.0, 422.8]}

    pf = env.store.get("provider_portfolio")
    assert pf["weights"] == {"WDC": 1.0} and pf["from_holdings"] is True
    assert env.store.get("last_signal_ts").startswith("2026-10-02T09:50:59")
    assert env.status_of(EXAMPLE) == "done"
    assert env.store.get("last_mismatch") == ""  # consistent after execution

    report = env.texts()[0]
    for part in ("信号 10-02 09:50:59", "建仓 WDC 0%→100% @422.6", "净资产 $100,000", "100%仓位 ≈ $190,000",
                 "卖出 BE 250股 成交 250 @ 199.90", "买入 WDC 449股 成交 449 @ 422.80"):
        assert part in report, part

    await env.engine.sync_stops()
    stops = b.working("WDC", STP)
    assert len(stops) == 1
    stop = stops[0]
    assert (stop.side, stop.qty, stop.tif, stop.tag, stop.outside_rth) == (SELL, 449, "GTC", STOP_TAG, False)
    assert stop.stop_price == round(422.8 * 0.9, 2) == 380.52
    assert "🛡️ 止损 WDC 449股 @380.52" in env.texts()[-1]

    await env.engine.sync_stops()  # idempotent
    assert len(b.placed) == 4


async def test_duplicate_message_is_ignored_silently(make):
    env = make(cash=0.0)
    example_account(env.broker)
    await env.send(EXAMPLE)
    n_orders, n_msgs = len(env.broker.placed), len(env.notifier.messages)

    assert await env.send(EXAMPLE) is True
    assert len(env.broker.placed) == n_orders
    assert len(env.notifier.messages) == n_msgs


async def test_reforward_with_extra_text_is_a_duplicate(make):
    env = make(cash=0.0)
    example_account(env.broker)
    await env.send(EXAMPLE)
    n_orders, n_msgs = len(env.broker.placed), len(env.notifier.messages)

    assert await env.send("转发：\n" + EXAMPLE + "\n（我的备注）") is True  # same timestamp + changes + holdings
    assert len(env.broker.placed) == n_orders
    assert len(env.notifier.messages) == n_msgs


async def test_out_of_order_signal_is_ignored_with_warning(make):
    env = make(cash=0.0)
    example_account(env.broker)
    await env.send(EXAMPLE)
    n_orders = len(env.broker.placed)

    older = signal(["加仓 BE | 仓位：10.0% → 50.0% | 成交价：200"], None, "2026-10-02 09:40:00")
    assert await env.send(older)

    assert len(env.broker.placed) == n_orders
    text, level, _ = env.notifier.messages[-1]
    assert level == "warn" and "更旧" in text
    assert env.status_of(older) == "out_of_order"
    assert env.store.get("provider_portfolio")["weights"] == {"WDC": 1.0}  # untouched


async def test_stale_signal_updates_portfolio_but_does_not_trade(make):
    env = make(at=ny(2026, 10, 2, 10, 11, 0), cash=0.0)
    example_account(env.broker)

    await env.send(EXAMPLE)

    assert env.broker.placed == []
    text, level, mention = env.notifier.messages[-1]
    assert "信号已过期" in text and "!sync" in text and level == "warn" and mention
    assert env.store.get("provider_portfolio")["weights"] == {"WDC": 1.0}
    assert env.status_of(EXAMPLE) == "stale"


async def test_unfilled_market_sells_give_no_buying_power_credit(make):
    env = make(cash=0.0)
    b = env.broker
    example_account(b)
    b.hold_market = True  # e.g. halted: market orders do not fill within fill_timeout

    await env.send(EXAMPLE)

    assert [(r.side, r.symbol) for r in b.placed[:2]] == [(SELL, "BE"), (SELL, "ARM")]
    value = 449 * 422.8
    # account unchanged (EL 70k, AF 50k, GPV 100k): gross room 1.9·100k − 100k = 90k binds
    s = min((70_000 - 25_000) / (value * 0.25), (50_000 - 2_000) / (value * 0.30), 90_000 / value)
    assert [(r.side, r.symbol, r.qty) for r in b.placed[2:]] == [(BUY, "WDC", math.floor(449 * s))]
    assert "尚未成交，仍在挂单" in env.texts()[0]
    await env.engine.sync_stops()  # working sells cover BE / ARM fully: no stops for them
    assert b.working("BE", STP) == [] and b.working("ARM", STP) == []


async def test_ignored_symbols_are_never_touched(make):
    env = make(cash=0.0, cfg=make_cfg(ignore_symbols=["be"]))
    example_account(env.broker)
    await env.send(EXAMPLE)
    assert "BE" not in {r.symbol for r in env.broker.placed}
    assert env.broker.qty("BE") == 250
    await env.engine.sync_stops()
    assert env.broker.working("BE", STP) == []
    assert env.store.get("last_mismatch") == ""


async def test_non_signal_text_returns_false(make):
    env = make()
    assert await env.send("大家早上好") is False
    assert env.notifier.messages == []


# --------------------------------------------------------------------------- 5-6 pre-market


X_BUY = signal(
    ["建仓 X | 仓位：0.0% → 50.0% | 成交价：100.0"],
    ["X | 仓位：50.0% | 成本：100.0 | 盈亏：+0.0%"],
    "2026-10-02 08:00:00",
)


def pre_buy_env(make) -> Env:
    env = make(at=PRE_T)
    env.broker.set_quote("X", 99.9, 100.1, 100.0)
    return env


async def test_pre_market_buy_places_half_as_limit_then_rest_at_open(make):
    env = pre_buy_env(make)
    b = env.broker

    await env.send(X_BUY)

    target = math.floor(0.5 * 190_000 / 100.1)  # G = 1.9·NLV (cap binding)
    assert target == 949
    assert orders(b) == [(BUY, "X", target // 2, LMT)]
    lmt = b.placed[0]
    assert lmt.outside_rth and lmt.tif == "DAY" and lmt.tag.startswith("copybot:ext:")
    assert lmt.limit_price == round_to_tick(100.1 * 1.005, BUY) == 100.61
    pending = env.store.list_pending()
    assert [(i.symbol, i.target_weight, i.direction) for i, _ in pending] == [("X", 0.5, "buy")]
    assert pending[0][1] == OPEN_DUE
    assert "09:30:05" in env.texts()[-1] and "开盘市价执行" in env.texts()[-1]
    assert env.status_of(X_BUY) == "extended"

    env.clock.set(ny(2026, 10, 2, 9, 30, 3))  # open, but not due yet
    await env.engine.run_due_pending()
    assert len(b.placed) == 1

    env.clock.set(ny(2026, 10, 2, 9, 30, 6))
    await env.engine.run_due_pending()

    assert b.working("X", LMT) == [] and b.orders["1"].status == OrderStatus.CANCELLED
    assert orders(b)[1:] == [(BUY, "X", target, MKT)]
    assert b.placed[1].tag == "copybot:mkt:open"
    assert [e for e in b.events if e[0] == "cancel"] == [("cancel", "1 X")]
    assert env.store.list_pending() == []
    assert b.qty("X") == target
    assert "🔔 开盘执行" in env.texts()[-1]


async def test_pre_market_partial_fill_then_open_buys_only_the_remainder(make):
    env = pre_buy_env(make)
    b = env.broker
    await env.send(X_BUY)
    await b.fill_order("1", qty=200, price=100.5)  # partly filled before the open
    assert "盘前/盘后成交 买入 X 200股 @ 100.50" in env.texts()[-1]

    env.clock.set(ny(2026, 10, 2, 9, 30, 6))
    nlv = (await b.account()).net_liquidation
    await env.engine.run_due_pending()

    assert b.orders["1"].status == OrderStatus.CANCELLED and b.orders["1"].filled_qty == 200
    target = math.floor(0.5 * 1.9 * nlv / 100.1)
    assert orders(b)[1:] == [(BUY, "X", target - 200, MKT)]
    assert b.qty("X") == target


async def test_pre_market_sell_cancels_stop_first_then_market_sells_at_open(make):
    env = make(at=PRE_T, cash=95_000.0)
    b = env.broker
    b.set_position("Y", 100, 50.0)
    b.set_quote("Y", 49.9, 50.1, 50.0)
    await env.engine.sync_stops()
    stop = b.working("Y", STP)[0]
    assert stop.qty == 100 and stop.stop_price == 45.0

    text = signal(["清仓 Y | 仓位：30.0% → 0.0% | 成交价：无"], [], "2026-10-02 08:00:00")
    await env.send(text)

    assert b.orders[stop.order_id].status == OrderStatus.CANCELLED
    lmt = b.placed[-1]
    assert (lmt.side, lmt.symbol, lmt.qty, lmt.order_type, lmt.outside_rth) == (SELL, "Y", 100, LMT, True)
    assert lmt.limit_price == round_to_tick(49.9 * 0.995, SELL) == 49.65
    lmt_id = b.working("Y", LMT)[0].order_id
    # the stop is cancelled (and confirmed) before the sell goes out
    assert b.events[-2:] == [("cancel", f"{stop.order_id} Y"), ("place", f"{lmt_id} Y SELL LMT 100")]

    await env.engine.sync_stops()  # the working sell covers the whole position: no stop
    assert b.working("Y", STP) == []

    env.clock.set(ny(2026, 10, 2, 9, 30, 6))
    await env.engine.run_due_pending()
    assert b.orders[lmt_id].status == OrderStatus.CANCELLED  # the leftover limit
    assert orders(b)[-1] == (SELL, "Y", 100, MKT)
    assert b.qty("Y") == 0
    await env.engine.sync_stops()
    assert b.working("Y", STP) == []


async def test_after_hours_signal_uses_limits_and_queues_for_next_trading_day(make):
    env = make(at=ny(2026, 10, 2, 16, 30, 2))  # Friday after the close
    env.broker.set_quote("X", 99.9, 100.1, 100.0)
    await env.send(signal(["建仓 X | 仓位：0.0% → 50.0% | 成交价：100.0"], None, "2026-10-02 16:30:00"))

    assert orders(env.broker) == [(BUY, "X", 474, LMT)]
    assert env.broker.placed[0].outside_rth
    assert env.store.list_pending()[0][1] == ny(2026, 10, 5, 9, 30, 5)  # Monday


# --------------------------------------------------------------------------- 7-8 reductions and stops


async def test_reduce_is_weight_based_and_never_buys(make):
    env = make(cash=5_000.0)
    b = env.broker
    b.set_position("Z", 950, 100.0)
    b.set_quote("Z", 99.9, 100.1, 100.0)
    text = signal(
        ["减仓 Z | 仓位：50.0% → 25.0% | 成交价：100.0"],
        ["Z | 仓位：25.0% | 成本：90.0 | 盈亏：+11.0%"],
        "2026-10-02 09:50:00",
    )
    await env.send(text)

    target = math.floor(0.25 * 190_000 / 99.9)
    assert orders(b) == [(SELL, "Z", 950 - target, MKT)]
    assert b.qty("Z") == target


async def test_reduce_below_our_position_does_not_buy(make):
    env = make(cash=90_000.0)
    env.broker.set_position("Z", 100, 100.0)
    env.broker.set_quote("Z", 99.9, 100.1, 100.0)
    await env.send(signal(["减仓 Z | 仓位：50.0% → 25.0% | 成交价：100"], None, "2026-10-02 09:50:00"))
    assert env.broker.placed == []


async def test_stop_out_marks_symbol_and_later_increase_buys_back_full_target(make):
    env = make(at=ny(2026, 10, 2, 10, 0, 0), cash=95_000.0)
    b = env.broker
    b.set_position("W", 100, 50.0)
    b.set_quote("W", 49.9, 50.1, 50.0)
    await env.engine.sync_stops()
    env.store.upsert_pending(Intent("W", 0.6, "buy", reason="加仓"), ny(2026, 10, 5, 9, 30, 5))

    await b.trigger_stop("W")

    assert b.qty("W") == 0
    mark = env.store.stopped()["W"]
    assert (mark["qty"], mark["price"]) == (100, 45.0)
    assert env.store.list_pending() == []
    text, level, mention = env.notifier.messages[-1]
    assert "止损触发 W 卖出 100股 @ 45.00" in text and level == "warn" and mention

    env.clock.set(ny(2026, 10, 2, 10, 5, 3))
    await env.send(signal(["加仓 W | 仓位：30.0% → 50.0% | 成交价：50.0"], None, "2026-10-02 10:05:00"))

    nlv = 95_000.0 + 100 * 45.0
    target = math.floor(0.5 * 1.9 * nlv / 50.1)
    assert orders(b)[-1] == (BUY, "W", target, MKT)
    assert env.store.stopped() == {}
    assert "W 止损后重新跟入" in env.texts()[-1]


async def test_decrease_signal_clears_stop_mark(make):
    env = make(at=ny(2026, 10, 2, 10, 0, 0))
    env.store.mark_stopped("W", ny(2026, 10, 2, 9, 45), 100, 45.0)
    await env.send(signal(["清仓 W | 仓位：30.0% → 0.0% | 成交价：无"], None, "2026-10-02 09:59:59"))
    assert env.store.stopped() == {}
    assert env.broker.placed == []


async def test_stop_that_fills_while_being_cancelled_blocks_the_sell(make):
    env = make(cash=95_000.0)
    b = env.broker
    b.set_position("Y", 100, 50.0)
    b.set_quote("Y", 49.9, 50.1, 44.0)
    await env.engine.sync_stops()
    b.fill_stop_on_cancel.add("Y")

    await env.send(signal(["清仓 Y | 仓位：30.0% → 0.0% | 成交价：无"], None, "2026-10-02 09:50:00"))

    assert [r.order_type for r in b.placed] == [STP]  # no market sell after the stop filled
    assert b.qty("Y") == 0  # flat, never short
    assert "止损单刚刚成交" in env.joined()


# --------------------------------------------------------------------------- 9-10 liquidity


async def test_liquidity_guard_scales_buys_on_a_levered_account(make):
    env = make(cash=-80_000.0)
    b = env.broker
    b.set_position("P", 1800, 100.0)
    b.set_quote("P", 99.9, 100.1, 100.0)
    b.set_quote("Q", 49.95, 50.05, 50.0)
    text = signal(
        ["建仓 Q | 仓位：0.0% → 20.0% | 成交价：50.0"],
        ["P | 仓位：80.0% | 成本：90 | 盈亏：+1%", "Q | 仓位：20.0% | 成本：50 | 盈亏：+0%"],
        "2026-10-02 09:50:00",
    )
    await env.send(text)

    unconstrained = math.floor(0.2 * 190_000 / 50.05)
    value = unconstrained * 50.05
    # account: NLV 100k, EL 46k, AF 10k, GPV 180k → gross room 10k is binding
    s = min((46_000 - 25_000) / (value * 0.3), (10_000 - 2_000) / (value * 0.5), (190_000 - 180_000) / value)
    assert orders(b) == [(BUY, "Q", math.floor(unconstrained * s), MKT)]
    assert 0 < b.placed[0].qty < unconstrained
    assert "缩放" in env.texts()[0]


async def test_deleverage_sells_proportionally_and_respects_cooldown(make):
    env = make(at=ny(2026, 10, 2, 11, 0, 0), cash=-170_000.0)
    b = env.broker
    b.set_position("D", 2200, 100.0)
    b.set_quote("D", 99.9, 100.1, 100.0)
    b.rates["D"] = MarginRate(0.5, 0.45, "whatif")
    b.set_position("E", 1000, 50.0)
    b.set_quote("E", 49.9, 50.1, 50.0)
    acct = await b.account()
    assert acct.el_ratio < 0.15

    await env.engine.check_liquidity()

    s = (0.25 * acct.net_liquidation - acct.excess_liquidity) / (220_000 * 0.45 + 50_000 * 0.3)
    expected = sorted([(SELL, "D", math.ceil(2200 * s), MKT), (SELL, "E", math.ceil(1000 * s), MKT)])
    assert sorted(orders(b)) == expected
    assert all(r.tag == "copybot:dlv" for r in b.placed)
    assert env.store.get("last_deleverage_ts")
    text, level, mention = env.notifier.messages[-1]
    assert level == "error" and mention and "降杠杆前" in text and "降杠杆后" in text

    env.clock.advance(minutes=1)
    b.set_position("D", 2200, 100.0)  # still levered
    await env.engine.check_liquidity()
    assert len(b.placed) == 2  # cooldown


async def test_low_liquidity_warns_at_most_hourly_and_only_in_regular_hours(make):
    env = make(at=ny(2026, 10, 2, 11, 0, 0), cash=-100_000.0)
    b = env.broker
    b.set_position("D", 2000, 100.0)
    b.set_quote("D", 99.9, 100.1, 100.0)
    b.rates["D"] = MarginRate(0.5, 0.42, "whatif")  # NLV 100k, EL 16k → 16%: warn zone, above the 15% trigger
    await env.engine.check_liquidity()
    assert b.placed == []
    assert "剩余流动性偏低" in env.texts()[-1]
    n = len(env.notifier.messages)
    env.clock.advance(minutes=30)
    await env.engine.check_liquidity()
    assert len(env.notifier.messages) == n
    env.clock.set(ny(2026, 10, 2, 17, 0, 0))  # after hours: no checks
    await env.engine.check_liquidity()
    assert len(env.notifier.messages) == n


# --------------------------------------------------------------------------- 11-14 pause / closed / offline / chase


async def test_paused_updates_portfolio_without_trading_and_resume_works(make):
    env = make(cash=0.0)
    example_account(env.broker)
    env.engine.pause()
    assert env.engine.is_paused

    await env.send(EXAMPLE)
    assert env.broker.placed == []
    assert env.store.get("provider_portfolio")["weights"] == {"WDC": 1.0}
    assert "已暂停" in env.texts()[-1]
    assert env.status_of(EXAMPLE) == "paused"

    env.engine.resume()
    env.clock.set(ny(2026, 10, 2, 9, 55, 2))
    await env.send(signal(["清仓 BE | 仓位：50.0% → 0.0% | 成交价：无"], None, "2026-10-02 09:55:00"))
    assert orders(env.broker) == [(SELL, "BE", 250, MKT)]


async def test_closed_session_queues_for_next_monday_open(make):
    env = make(at=ny(2026, 10, 3, 10, 0, 30))  # Saturday
    env.broker.set_quote("X", 99.9, 100.1, 100.0)
    text = signal(["建仓 X | 仓位：0.0% → 50.0% | 成交价：100.0"], None, "2026-10-03 10:00:00")
    await env.send(text)

    assert env.broker.placed == []
    pending = env.store.list_pending()
    assert len(pending) == 1 and pending[0][1] == ny(2026, 10, 5, 9, 30, 5)
    assert "休市中" in env.texts()[-1]
    assert env.status_of(text) == "queued"


async def test_disconnected_broker_queues_then_executes_after_reconnect(make):
    env = make(at=ny(2026, 10, 2, 10, 0, 3))
    b = env.broker
    b.set_quote("X", 99.9, 100.1, 100.0)
    b.connected = False
    await env.send(signal(["建仓 X | 仓位：0.0% → 50.0% | 成交价：100.0"], None, "2026-10-02 10:00:00"))

    assert b.placed == []
    pending = env.store.list_pending()
    assert len(pending) == 1 and pending[0][1] == env.clock()
    text, level, _ = env.notifier.messages[-1]
    assert "IBKR 未连接" in text and level == "warn"

    await env.engine.run_due_pending()  # still disconnected: nothing
    assert env.store.list_pending()

    await b.set_connected(True, "test")
    assert "IBKR 已连接" in env.texts()[-1]
    env.clock.advance(seconds=5)
    await env.engine.run_due_pending()
    assert orders(b) == [(BUY, "X", math.floor(0.5 * 190_000 / 100.1), MKT)]
    assert env.store.list_pending() == []


async def test_expired_pending_is_dropped_with_warning(make):
    env = make(at=ny(2026, 10, 2, 9, 35, 0))
    env.broker.set_quote("X", 99.9, 100.1, 100.0)
    env.store.upsert_pending(Intent("X", 0.5, "buy", reason="建仓 0%→50%"), ny(2026, 10, 1, 10, 0, 0))
    await env.engine.run_due_pending()
    assert env.broker.placed == []
    assert env.store.list_pending() == []
    text, level, _ = env.notifier.messages[-1]
    assert "过期未执行" in text and "X" in text and level == "warn"


async def test_chase_guard_skips_buy_far_above_signal_price(make):
    env = make()
    env.broker.set_quote("X", 111.8, 112.0, 111.9)
    await env.send(signal(["建仓 X | 仓位：0.0% → 50.0% | 成交价：100.0"], None, "2026-10-02 09:50:00"))
    assert env.broker.placed == []
    text, level, _ = env.notifier.messages[-1]
    assert "不追" in text and "X" in text and level == "warn"


async def test_consistency_mismatch_is_reported_once(make):
    env = make()
    b = env.broker
    b.set_quote("A", 49.9, 50.1, 50.0)
    b.set_quote("B", 111.8, 112.0, 111.9)  # B will be skipped by the chase guard
    holdings = ["A | 仓位：50.0% | 成本：50 | 盈亏：+0%", "B | 仓位：50.0% | 成本：100 | 盈亏：+0%"]
    await env.send(signal(["建仓 A | 仓位：0.0% → 50.0% | 成交价：50", "建仓 B | 仓位：0.0% → 50.0% | 成交价：100"],
                          holdings, "2026-10-02 09:50:00"))
    assert [r.symbol for r in b.placed] == ["A"]
    assert "持仓和他不一致：缺少 B" in env.texts()[-1]
    assert env.store.get("last_mismatch") == "缺少 B"

    n = len(env.notifier.messages)
    await env.send(signal([], holdings, "2026-10-02 09:51:00"))  # holdings only, same mismatch
    assert len(env.notifier.messages) == n + 1  # the report, but no second mismatch warning


# --------------------------------------------------------------------------- 15-16 !sync and bootstrap


SYNC_PORTFOLIO = signal(["建仓 B | 仓位：0.0% → 100.0% | 成交价：50.0"],
                        ["B | 仓位：100.0% | 成本：50 | 盈亏：+0%"], "2026-10-02 09:00:00")


async def test_sync_preview_then_confirm_rebalances(make):
    env = make(at=ny(2026, 10, 2, 10, 0, 0), cash=70_000.0)
    b = env.broker
    b.set_position("A", 300, 100.0)
    b.set_quote("A", 99.9, 100.1, 100.0)
    b.set_quote("B", 49.9, 50.1, 50.0)
    assert await env.engine.bootstrap_from_text(SYNC_PORTFOLIO)

    assert "请先发送 !sync 预览" in await env.engine.sync_confirm()

    preview = await env.engine.sync_preview()
    assert "卖出 A 300股" in preview and "买入 B" in preview and "180 秒内" in preview
    assert b.placed == []

    report = await env.engine.sync_confirm()
    assert orders(b)[0] == (SELL, "A", 300, MKT)
    assert orders(b)[1][:2] == (BUY, "B") and orders(b)[1][2] > 3700
    assert all(r.tag == "copybot:mkt:sync" for r in b.placed)
    assert "同步执行" in report and "买入 B" in report

    assert "请先发送 !sync 预览" in await env.engine.sync_confirm()  # one preview, one confirm


async def test_sync_preview_expires_and_band_skips_small_deltas(make):
    env = make(at=ny(2026, 10, 2, 10, 0, 0), cash=100_000.0)
    b = env.broker
    b.set_quote("B", 49.9, 50.1, 50.0)
    await env.engine.bootstrap_from_text(SYNC_PORTFOLIO)
    target = math.floor(190_000 / 50.0)  # "both" sizes with mark()
    b.set_position("B", target - 50, 50.0)  # within 5% of the target
    b.cash = 100_000.0 - (target - 50) * 50.0

    preview = await env.engine.sync_preview()
    assert "无需调整" in preview
    env.clock.advance(seconds=181)
    assert "请先发送 !sync 预览" in await env.engine.sync_confirm()
    assert b.placed == []


async def test_sync_never_buys_back_a_stopped_symbol(make):
    env = make(at=ny(2026, 10, 2, 10, 0, 0))
    env.broker.set_quote("B", 49.9, 50.1, 50.0)
    await env.engine.bootstrap_from_text(SYNC_PORTFOLIO)
    env.store.mark_stopped("B", ny(2026, 10, 2, 9, 45), 100, 45.0)
    preview = await env.engine.sync_preview()
    assert "已止损" in preview and "买入 B" not in preview


async def test_bootstrap_updates_portfolio_marks_seen_and_never_trades(make):
    env = make(cash=0.0)
    example_account(env.broker)
    assert await env.engine.bootstrap_from_text("随便聊聊") is False
    assert await env.engine.bootstrap_from_text(EXAMPLE) is True

    assert env.store.get("provider_portfolio")["weights"] == {"WDC": 1.0}
    assert env.status_of(EXAMPLE) == "bootstrap"
    assert env.broker.placed == []

    older = signal(["建仓 Q | 仓位：0.0% → 10.0% | 成交价：5"], ["Q | 仓位：10.0% | 成本：5 | 盈亏：+0%"],
                   "2026-10-01 10:00:00")
    assert await env.engine.bootstrap_from_text(older) is True
    assert env.store.get("provider_portfolio")["weights"] == {"WDC": 1.0}  # older does not override
    assert env.status_of(older) == "bootstrap"

    await env.send(EXAMPLE)  # the same message arriving live later is a duplicate
    assert env.broker.placed == []
    assert not any("错过信号" in t for t in env.texts())  # first run: nothing counts as missed


async def test_bootstrap_warns_about_old_signals_missed_while_offline(make):
    env = make(cash=0.0)
    example_account(env.broker)
    env.store.set("last_signal_ts", ny(2026, 10, 2, 9, 40, 0))  # newest signal handled before the restart
    env.clock.set(ny(2026, 10, 2, 10, 30, 0))  # 39 min after the missed signal: too old to trade

    assert await env.engine.bootstrap_from_text(EXAMPLE)

    text, level, mention = env.notifier.messages[-1]
    assert "离线期间错过信号 10-02 09:50:59" in text and "!sync" in text and level == "warn" and mention
    assert env.broker.placed == []
    assert env.store.get("last_signal_ts").startswith("2026-10-02T09:50:59")


async def test_bootstrap_trades_fresh_signal_missed_during_restart(make):
    env = make(cash=0.0)
    example_account(env.broker)
    env.store.set("last_signal_ts", ny(2026, 10, 2, 9, 40, 0))  # newest signal handled before the restart

    assert await env.engine.bootstrap_from_text(EXAMPLE)  # clock: a few seconds after the signal

    sides = [(r.symbol, r.side) for r in env.broker.placed]
    assert sides[:2] == [("BE", SELL), ("ARM", SELL)] and ("WDC", BUY) in sides
    # Handled once: the same text later (live re-delivery) is a duplicate.
    n = len(env.broker.placed)
    await env.engine.handle_text(EXAMPLE, env.meta())
    assert len(env.broker.placed) == n


# --------------------------------------------------------------------------- 17 stop sizing


async def test_stop_qty_tracks_position_minus_working_sells(make):
    env = make(cash=95_000.0)
    b = env.broker
    b.set_position("S", 100, 50.0)
    b.set_quote("S", 49.9, 50.1, 50.0)
    await env.engine.sync_stops()
    stop = b.working("S", STP)[0]
    assert (stop.qty, stop.stop_price) == (100, 45.0)

    await b.place_order(OrderRequest("S", SELL, 40, LMT, limit_price=60.0, outside_rth=True, tag="copybot:ext:t"))
    await env.engine.sync_stops()
    assert b.working("S", STP)[0].qty == 60
    assert "止损调整 S 60股 @45.00" in env.texts()[-1]

    b.set_quote("S", 59.9, 60.0, 60.0)
    await b.place_order(OrderRequest("S", BUY, 50, MKT, tag="copybot:mkt:t"))
    await env.engine.sync_stops()
    stop = b.working("S", STP)[0]
    avg = (100 * 50.0 + 50 * 60.0) / 150
    assert (stop.qty, stop.stop_price) == (110, round_to_tick(avg * 0.9))
    assert len(b.working("S", STP)) == 1


async def test_stop_for_closed_position_is_cancelled(make):
    env = make(cash=95_000.0)
    b = env.broker
    b.set_position("S", 100, 50.0)
    b.set_quote("S", 49.9, 50.1, 50.0)
    await env.engine.sync_stops()
    b.set_position("S", 0, 0.0)  # sold manually
    await env.engine.sync_stops()
    assert b.working("S", STP) == []
    assert "撤销止损 S" in env.texts()[-1]


# --------------------------------------------------------------------------- 18 dry run


async def test_dry_run_reports_but_never_touches_the_real_broker(make):
    env = make(cash=0.0, cfg=make_cfg(dry_run=True), wrap=DryRunBroker)
    inner = env.broker
    example_account(inner)

    await env.send(EXAMPLE)
    await env.engine.sync_stops()

    assert inner.placed == [] and inner.events == []
    report = env.texts()[0]
    assert "DRY-RUN" in report and "买入 WDC 449股" in report and "卖出 BE 250股" in report
    assert inner.pos["BE"][0] == 250  # real account untouched


# --------------------------------------------------------------------------- robustness


async def test_uncancellable_old_order_skips_symbol_at_open(make):
    env = pre_buy_env(make)
    b = env.broker
    await env.send(X_BUY)
    b.stuck_cancel.add("1")
    env.clock.set(ny(2026, 10, 2, 9, 30, 6))
    await env.engine.run_due_pending()
    assert len(b.placed) == 1  # no market order on top of the stuck limit
    text, level, mention = env.notifier.messages[-1]
    assert "旧挂单未能撤销" in text and level == "error" and mention


async def test_fill_dedupe_and_ext_fill_notice(make):
    env = make()
    fill = Fill("9", "x1", "X", BUY, 10, 100.0, "copybot:ext:abc", env.clock())
    await env.engine._on_fill(fill)
    await env.engine._on_fill(fill)
    assert env.store.fill_seen("x1")
    assert sum("盘前/盘后成交" in t for t in env.texts()) == 1


async def test_broker_error_during_signal_is_reported_and_recorded(make):
    env = make()

    async def boom() -> dict:
        raise RuntimeError("socket gone")

    env.broker.positions = boom  # type: ignore[method-assign]
    text = signal(["建仓 X | 仓位：0.0% → 50.0% | 成交价：100.0"], None, "2026-10-02 09:50:00")
    await env.send(text)
    msg, level, mention = env.notifier.messages[-1]
    assert "处理信号出错" in msg and "socket gone" in msg and level == "error" and mention
    assert env.status_of(text) == "error"


async def test_future_timestamp_does_not_block_later_signals(make):
    env = make()
    env.broker.set_quote("X", 99.9, 100.1, 100.0)
    await env.send(signal(["建仓 X | 仓位：0.0% → 10.0% | 成交价：100.0"], None, "2026-10-03 09:50:00"))
    assert "比当前时间晚" in env.texts()[-1]
    env.clock.advance(seconds=30)
    later = signal(["加仓 X | 仓位：10.0% → 20.0% | 成交价：100.0"], None, "2026-10-02 09:51:20")
    await env.send(later)
    assert env.status_of(later) == "done"


async def test_cancel_all_cancels_working_orders_and_clears_pending(make):
    env = pre_buy_env(make)
    await env.send(X_BUY)
    await env.engine.sync_stops()
    text = await env.engine.cancel_all()
    assert "已撤销 1 个挂单" in text and "清空 1 个待开盘任务" in text
    assert env.broker.working("X") == []
    assert env.store.list_pending() == []


async def test_status_text_connected_and_disconnected(make):
    env = make(cash=0.0)
    example_account(env.broker)
    await env.send(EXAMPLE)
    await env.engine.sync_stops()
    env.store.upsert_pending(Intent("Q", 0.1, "buy", reason="建仓 0%→10%"), ny(2026, 10, 5, 9, 30, 5))

    status = await env.engine.status_text()
    for part in ("实盘", "运行中", "已连接", "盘中", "净资产", "100%仓位", "WDC 449股", "止损 449股@380.52",
                 "待开盘执行", "Q 建仓 0%→10%", "他的持仓", "WDC 100%"):
        assert part in status, part

    env.broker.connected = False
    status = await env.engine.status_text()
    assert "未连接" in status and "他的持仓" in status


async def test_start_and_stop_run_the_loops(make):
    env = make(cash=95_000.0)
    env.broker.set_position("S", 100, 50.0)
    env.broker.set_quote("S", 49.9, 50.1, 50.0)
    env.engine.stop_sync_settle_seconds = 0.0
    await env.engine.start()
    for _ in range(50):
        if env.broker.working("S", STP):
            break
        await asyncio.sleep(0.01)
    await env.engine.stop()
    assert env.broker.working("S", STP)  # the stop loop ran once at start
    assert env.engine._tasks == []


# --------------------------------------------------------------------------- app wiring


async def test_app_run_wires_components_and_shuts_down(monkeypatch, tmp_path):
    from copybot import app

    fake = FakeBroker()
    seen: dict[str, object] = {}

    async def fake_run_client(client, token):  # Discord "ends" right away
        seen["client"], seen["token"] = client, token

    monkeypatch.setattr(app, "IBKRBroker", lambda *a, **k: fake)
    monkeypatch.setattr(app, "discord_token", lambda: "tok")
    monkeypatch.setattr(app, "run_client", fake_run_client)
    cfg = AppConfig(
        discord=DiscordConfig(signal_channel_id=1, log_channel_id=2, owner_ids=[3]),
        trading=TradingConfig(dry_run=True),
        storage=StorageConfig(db_path=str(tmp_path / "db" / "copybot.db"), log_dir=str(tmp_path / "logs")),
    )
    with pytest.raises(RuntimeError, match="discord ended unexpectedly"):  # → exit code 1
        await asyncio.wait_for(app.run(cfg), 10)

    client = seen["client"]
    assert seen["token"] == "tok"
    assert isinstance(client.engine, Engine) and isinstance(client.engine.broker, DryRunBroker)
    assert client.notifier is not None and client.engine.notifier is client.notifier
    assert client.engine._tasks == []
    assert fake.connected is False  # broker.disconnect() on shutdown
    assert (tmp_path / "db" / "copybot.db").exists()


# --------------------------------------------------------------------------- review regressions


async def test_premarket_rotation_buys_half_after_sells_fill(make):
    """At full leverage the pre-market buy leg must be funded by the pre-market sells."""
    env = make(at=ny(2026, 10, 2, 8, 0, 3), cash=-90_000.0)
    b = env.broker
    b.set_position("BE", 475, 200.0)
    b.set_quote("BE", 199.9, 200.1, 200.0)
    b.set_position("ARM", 760, 125.0)
    b.set_quote("ARM", 124.9, 125.1, 125.0)
    b.set_quote("WDC", 422.4, 422.8, 422.6)
    b.rates["WDC"] = MarginRate(0.30, 0.25, "whatif")
    b.auto_fill_limits = True
    text = EXAMPLE.replace("09:50:59", "08:00:00")

    await env.send(text)

    assert b.qty("BE") == 0 and b.qty("ARM") == 0
    wdc = [r for r in b.placed if r.symbol == "WDC"]
    assert len(wdc) == 1 and wdc[0].order_type == LMT and wdc[0].outside_rth
    assert 200 <= wdc[0].qty <= 225  # about half of the ~448-share target


async def test_stale_signal_cancels_leftover_premarket_order(make):
    env = make(at=ny(2026, 10, 2, 8, 0, 3))
    b = env.broker
    b.set_quote("X", 99.9, 100.1, 100.0)
    await env.send(signal(["建仓 X | 仓位：0.0% → 50.0% | 成交价：100"], ["X | 仓位：50.0% | 成本：100 | 盈亏：0.0%"], "2026-10-02 08:00:00"))
    assert len(b.working("X", LMT)) == 1

    env.clock.set(ny(2026, 10, 2, 8, 45, 0))  # the exit arrives 15 min late → stale, not traded
    await env.send(signal(["清仓 X | 仓位：50.0% → 0.0% | 成交价：无"], [], "2026-10-02 08:30:00"))

    assert b.working("X", LMT) == []
    assert env.store.list_pending() == []
    assert "已撤销旧挂单" in env.texts()[-1]


async def test_stop_fill_cancels_leftover_premarket_buy(make):
    env = make(at=ny(2026, 10, 2, 8, 0, 3))
    b = env.broker
    b.set_position("W", 100, 50.0)
    b.set_quote("W", 49.9, 50.1, 50.0)
    await env.engine.sync_stops()
    await env.send(signal(["加仓 W | 仓位：30.0% → 50.0% | 成交价：50"], None, "2026-10-02 08:00:00"))
    assert len(b.working("W", LMT)) == 1

    await b.trigger_stop("W")
    await asyncio.sleep(0)  # let the spawned cancel task run
    for _ in range(20):
        if not env.engine._bg:
            break
        await asyncio.sleep(0.01)

    assert b.working("W", LMT) == []
    assert "W" in env.store.stopped()
    assert env.store.list_pending() == []


async def test_stale_add_after_stop_out_clears_mark_so_sync_can_reenter(make):
    env = make()
    b = env.broker
    b.set_quote("W", 49.9, 50.1, 50.0)
    env.store.mark_stopped("W", ny(2026, 10, 2, 9, 35, 0), 100, 45.0)
    env.clock.set(ny(2026, 10, 2, 10, 16, 0))

    await env.send(signal(["加仓 W | 仓位：30.0% → 50.0% | 成交价：46"], ["W | 仓位：50.0% | 成本：48 | 盈亏：-4.0%"], "2026-10-02 10:05:00"))
    assert b.placed == [] and "W" not in env.store.stopped()

    preview = await env.engine.sync_preview()
    assert "买入 W" in preview


# --------------------------------------------------------------------------- review regressions (round 2)

PRE_BUY_X = signal(["建仓 X | 仓位：0.0% → 50.0% | 成交价：100"], ["X | 仓位：50.0% | 成本：100 | 盈亏：0.0%"], "2026-10-02 08:00:00")
EXIT_X = signal(["清仓 X | 仓位：50.0% → 0.0% | 成交价：无"], [], "2026-10-02 08:12:00")


async def test_first_run_never_trades_history_even_with_two_signals(make):
    env = make(cash=0.0)
    example_account(env.broker)
    older = signal(["建仓 BE | 仓位：0.0% → 50.0% | 成交价：200"], None, "2026-09-30 10:00:00")
    assert await env.engine.bootstrap_from_text(older)
    assert await env.engine.bootstrap_from_text(EXAMPLE)  # fresh, but this is a first run
    assert env.broker.placed == []
    assert not any("离线期间错过信号" in t for t in env.texts())


async def test_bootstrap_missed_exit_drops_pending_and_leftover_order(make):
    env = make(at=ny(2026, 10, 2, 8, 0, 3))
    b = env.broker
    b.set_quote("X", 99.9, 100.1, 100.0)
    await env.send(PRE_BUY_X)
    assert len(b.working("X", LMT)) == 1 and env.store.list_pending()

    # Bot restarts; the exit was posted 18 minutes ago while it was offline.
    env.engine._boot_snapshot_taken = False
    env.clock.set(ny(2026, 10, 2, 8, 30, 0))
    assert await env.engine.bootstrap_from_text(EXIT_X)
    assert b.working("X", LMT) == [] and env.store.list_pending() == []
    assert "离线期间错过信号" in env.texts()[-1]

    env.clock.set(ny(2026, 10, 2, 9, 30, 6))
    await env.engine.run_due_pending()
    assert [r for r in b.placed if r.symbol == "X" and r.order_type == MKT] == []


async def test_bootstrap_after_stop_does_not_trade(make):
    env = make(cash=0.0)
    example_account(env.broker)
    env.store.set("last_signal_ts", ny(2026, 10, 2, 9, 40, 0))
    await env.engine.stop()
    assert await env.engine.bootstrap_from_text(EXAMPLE) is False
    assert env.broker.placed == []


async def test_leftover_cancel_while_disconnected_is_retried_after_reconnect(make):
    env = make(at=ny(2026, 10, 2, 8, 0, 3))
    b = env.broker
    b.set_quote("X", 99.9, 100.1, 100.0)
    await env.send(PRE_BUY_X)
    await b.set_connected(False, "1100")
    env.clock.set(ny(2026, 10, 2, 8, 45, 0))  # exit arrives stale while IBKR is down
    await env.send(EXIT_X)
    assert "IBKR 未连接" in env.joined()
    assert env.store.get("cancel_leftovers") == ["X"]

    await b.set_connected(True, "back")
    await env.engine.sync_stops()  # the stop-sync loop drains the backlog
    assert b.working("X", LMT) == []
    assert env.store.get("cancel_leftovers") == []


async def test_stale_add_older_than_stop_out_keeps_mark(make):
    env = make()
    env.broker.set_quote("W", 49.9, 50.1, 50.0)
    env.store.mark_stopped("W", ny(2026, 10, 2, 10, 10, 0), 100, 45.0)
    env.clock.set(ny(2026, 10, 2, 10, 25, 0))
    await env.send(signal(["加仓 W | 仓位：30.0% → 50.0% | 成交价：46"], None, "2026-10-02 10:00:00"))
    assert "W" in env.store.stopped()


async def test_leftover_filled_during_cancel_is_reported_as_filled(make):
    env = make(at=ny(2026, 10, 2, 8, 0, 3))
    b = env.broker
    b.set_quote("X", 99.9, 100.1, 100.0)
    await env.send(PRE_BUY_X)
    order = b.working("X", LMT)[0]
    real_cancel = b.cancel_order

    async def cancel_loses_race(order_id: str) -> None:
        if order_id == order.order_id:
            await b.fill_order(order_id)  # the fill beats the cancel
            return
        await real_cancel(order_id)

    b.cancel_order = cancel_loses_race  # type: ignore[method-assign]
    env.clock.set(ny(2026, 10, 2, 8, 45, 0))
    await env.send(EXIT_X)
    assert "撤单前已成交：X 买入" in env.joined()
    assert "已撤销旧挂单" not in env.joined()


async def test_dry_run_premarket_rotation_does_not_wait_and_buys_half(make):
    env = make(at=ny(2026, 10, 2, 8, 0, 3), cash=-90_000.0, cfg=make_cfg(dry_run=True, fill_timeout_seconds=30), wrap=DryRunBroker)
    b = env.broker
    b.set_position("BE", 475, 200.0)
    b.set_quote("BE", 199.9, 200.1, 200.0)
    b.set_position("ARM", 760, 125.0)
    b.set_quote("ARM", 124.9, 125.1, 125.0)
    b.set_quote("WDC", 422.4, 422.8, 422.6)
    b.rates["WDC"] = MarginRate(0.30, 0.25, "whatif")

    loop = asyncio.get_running_loop()
    started = loop.time()
    await env.send(EXAMPLE.replace("09:50:59", "08:00:00"))
    assert loop.time() - started < 5  # no 30 s wait for simulated limits
    assert b.placed == []  # dry-run: nothing real
    assert "假设盘前/盘后卖单已全部成交" in env.joined()
    assert "限价买入 WDC" in env.joined()


async def test_paused_signal_keeps_protective_deleverage_sell(make):
    env = make()
    b = env.broker
    b.set_position("D", 100, 50.0)
    b.set_quote("D", 49.9, 50.1, 50.0)
    b.hold_market = True
    await b.place_order(OrderRequest("D", SELL, 30, MKT, tag="copybot:dlv"))
    env.engine.pause()
    env.clock.set(ny(2026, 10, 2, 9, 51, 30))
    await env.send(signal(["加仓 D | 仓位：10.0% → 20.0% | 成交价：50"], None, "2026-10-02 09:51:20"))
    assert [o.tag for o in b.working("D", MKT)] == ["copybot:dlv"]


# --------------------------------------------------------------------------- liquidity-first, 二次调仓, confirmations


def live_cfg(**trading: object) -> AppConfig:
    """The shipped defaults (liquidity first, Reg T check, 二次调仓 on)."""
    opts: dict[str, object] = {"dry_run": False, "min_order_value_usd": 1.0, "fill_timeout_seconds": 1}
    opts.update(trading)
    return AppConfig(
        discord=DiscordConfig(signal_channel_id=1, log_channel_id=2, owner_ids=[3]),
        trading=TradingConfig(**opts),
    )


THREE = ["RKLB | 仓位：33.33% | 成本：50 | 盈亏：0.0%", "MU | 仓位：33.33% | 成本：100 | 盈亏：0.0%"]
ROTATE = signal(
    ["清仓 BE | 仓位：33.33% → 0.0% | 成交价：无", "建仓 NVDA | 仓位：0.0% → 33.33% | 成交价：200"],
    [*THREE, "NVDA | 仓位：33.33% | 成本：200 | 盈亏：0.0%"],
    "2026-10-02 09:50:59",
)


def three_stock_account(b: FakeBroker) -> float:
    """RKLB / MU / BE equal thirds at the G the bot would have used. Returns that G."""
    b.rates.update({
        "RKLB": MarginRate(0.45, 0.45), "MU": MarginRate(0.40, 0.40),
        "BE": MarginRate(0.70, 0.70), "NVDA": MarginRate(0.30, 0.30),
    })
    for sym, px in (("RKLB", 50.0), ("MU", 100.0), ("BE", 150.0), ("NVDA", 200.0)):
        b.set_quote(sym, px - 0.05, px, px)
    g_before = 75_000 / ((0.45 + 0.40 + 0.70) / 3)  # 25% reserve binds (high-margin names)
    third = g_before / 3
    b.set_position("RKLB", int(third / 50), 50.0)
    b.set_position("MU", int(third / 100), 100.0)
    b.set_position("BE", int(third / 150), 150.0)
    b.cash = 100_000 - (int(third / 50) * 50 + int(third / 100) * 100 + int(third / 150) * 150)
    return g_before


async def test_example_with_defaults_is_limited_by_regt_not_a_fixed_cap(make):
    env = make(cash=0.0, cfg=live_cfg())
    example_account(env.broker)
    await env.send(EXAMPLE)
    wdc = [r for r in env.broker.placed if r.symbol == "WDC"][0]
    assert wdc.qty == math.floor(98_000 / 0.5 / 422.8)  # Reg T overnight 50% → 1.96x
    assert "Reg T 隔夜保证金" in env.joined()


async def test_rotation_into_lower_margin_stock_tops_up_the_others(make):
    env = make(cash=0.0, cfg=live_cfg())
    b = env.broker
    three_stock_account(b)
    rklb0, mu0 = b.qty("RKLB"), b.qty("MU")

    await env.send(ROTATE)

    sides = [(r.symbol, r.side) for r in b.placed]
    assert sides[0] == ("BE", SELL)  # sells first
    assert {("NVDA", BUY), ("RKLB", BUY), ("MU", BUY)} <= set(sides)
    assert b.qty("BE") == 0
    # Equal thirds again at the new, larger G (each within a share of the others).
    values = [b.qty("RKLB") * 50, b.qty("MU") * 100, b.qty("NVDA") * 200]
    assert max(values) - min(values) <= 200
    assert b.qty("RKLB") > rklb0 and b.qty("MU") > mu0
    assert "二次调仓" in env.joined()
    for r in b.placed:
        assert isinstance(r.qty, int)


async def test_rotation_into_higher_margin_stock_trims_the_others(make):
    env = make(cash=0.0, cfg=live_cfg())
    b = env.broker
    b.rates.update({"RKLB": MarginRate(0.45, 0.45), "MU": MarginRate(0.40, 0.40),
                    "NVDA": MarginRate(0.30, 0.30), "BE": MarginRate(0.70, 0.70)})
    for sym, px in (("RKLB", 50.0), ("MU", 100.0), ("BE", 150.0), ("NVDA", 200.0)):
        b.set_quote(sym, px - 0.05, px, px)
    third = 75_000 / ((0.45 + 0.40 + 0.30) / 3) / 3
    b.set_position("RKLB", int(third / 50), 50.0)
    b.set_position("MU", int(third / 100), 100.0)
    b.set_position("NVDA", int(third / 200), 200.0)
    b.cash = 100_000 - (int(third / 50) * 50 + int(third / 100) * 100 + int(third / 200) * 200)
    rklb0, mu0 = b.qty("RKLB"), b.qty("MU")

    await env.send(signal(
        ["清仓 NVDA | 仓位：33.33% → 0.0% | 成交价：无", "建仓 BE | 仓位：0.0% → 33.33% | 成交价：150"],
        [*THREE, "BE | 仓位：33.33% | 成本：150 | 盈亏：0.0%"],
        "2026-10-02 09:50:59",
    ))

    assert b.qty("RKLB") < rklb0 and b.qty("MU") < mu0  # trimmed to make room
    sells = [r.symbol for r in b.placed if r.side == SELL]
    buys = [r.symbol for r in b.placed if r.side == BUY]
    assert set(sells) == {"NVDA", "RKLB", "MU"} and buys == ["BE"]
    account = await b.account()
    assert account.el_ratio >= 0.25 - 0.01  # the liquidity reserve holds


async def test_repeated_broadcast_is_a_confirmation_not_a_second_buy(make):
    env = make(cash=0.0, cfg=live_cfg())
    example_account(env.broker)
    await env.send(EXAMPLE)
    n = len(env.broker.placed)

    env.clock.advance(minutes=3)  # he announces the same change again when it fills
    await env.send(EXAMPLE.replace("成交价：422.6", "成交价：422.9").replace("09:50:59", "09:53:30"))

    assert len(env.broker.placed) == n
    assert "重复播报" in env.texts()[-1]


async def test_confirmation_after_stop_out_does_not_buy_back(make):
    env = make(cash=0.0, cfg=live_cfg())
    b = env.broker
    example_account(b)
    await env.send(EXAMPLE)
    await env.engine.sync_stops()
    await b.trigger_stop("WDC")
    assert "WDC" in env.store.stopped()

    env.clock.advance(minutes=3)
    await env.send(EXAMPLE.replace("09:50:59", "09:53:30"))  # his fill confirmation, after our stop

    assert "WDC" in env.store.stopped()
    assert b.qty("WDC") == 0


async def test_premarket_rotation_trades_only_his_changes_and_rebalances_at_open(make):
    env = make(at=ny(2026, 10, 2, 8, 0, 3), cash=0.0, cfg=live_cfg())
    b = env.broker
    three_stock_account(b)
    b.auto_fill_limits = True

    await env.send(ROTATE.replace("09:50:59", "08:00:00"))

    pre = [(r.symbol, r.order_type) for r in b.placed]
    assert ("BE", LMT) in pre and ("NVDA", LMT) in pre
    assert not any(s in ("RKLB", "MU") for s, _ in pre)  # 二次调仓 waits for the open
    pending = {it.symbol: it.kind for it, _ in env.store.list_pending()}
    assert pending["RKLB"] == "rebalance" and pending["MU"] == "rebalance"

    env.clock.set(ny(2026, 10, 2, 9, 30, 6))
    await env.engine.run_due_pending()
    at_open = [(r.symbol, r.side, r.order_type) for r in b.placed[len(pre):]]
    assert ("RKLB", BUY, MKT) in at_open and ("MU", BUY, MKT) in at_open


# --------------------------------------------------------------------------- review regressions (round 3)

ROTATE_TO_BE = ["清仓 NVDA | 仓位：33.33% → 0.0% | 成交价：无", "建仓 BE | 仓位：0.0% → 33.33% | 成交价：150"]
HOLD_BE = [*THREE, "BE | 仓位：33.33% | 成本：150 | 盈亏：0.0%"]


def low_margin_three(b: FakeBroker) -> None:
    """RKLB / MU / NVDA equal thirds at G = 75k / 38.33% (NVDA is the low-margin name)."""
    b.rates.update({
        "RKLB": MarginRate(0.45, 0.45), "MU": MarginRate(0.40, 0.40),
        "BE": MarginRate(0.70, 0.70), "NVDA": MarginRate(0.30, 0.30),
    })
    for sym, px in (("RKLB", 50.0), ("MU", 100.0), ("BE", 150.0), ("NVDA", 200.0)):
        b.set_quote(sym, px - 0.05, px, px)
    third = 75_000 / ((0.45 + 0.40 + 0.30) / 3) / 3
    b.set_position("RKLB", int(third / 50), 50.0)
    b.set_position("MU", int(third / 100), 100.0)
    b.set_position("NVDA", int(third / 200), 200.0)
    b.cash = 100_000 - (int(third / 50) * 50 + int(third / 100) * 100 + int(third / 200) * 200)


async def test_confirmation_right_after_the_open_joins_the_open_batch(make):
    env = make(at=ny(2026, 10, 2, 3, 0, 0), cash=0.0, cfg=live_cfg())  # overnight: queued for the open
    b = env.broker
    low_margin_three(b)
    await env.send(signal(ROTATE_TO_BE, HOLD_BE, "2026-10-02 02:59:00"))
    assert b.placed == []

    env.clock.set(ny(2026, 10, 2, 9, 30, 1))  # his fill broadcast lands before our 09:30:05 batch
    await env.send(signal(ROTATE_TO_BE, HOLD_BE, "2026-10-02 09:30:00"))
    assert b.placed == [] and "并入" in env.texts()[-1]

    env.clock.set(ny(2026, 10, 2, 9, 30, 6))
    await env.engine.run_due_pending()
    values = [b.qty("RKLB") * 50, b.qty("MU") * 100, b.qty("BE") * 150]
    assert b.qty("NVDA") == 0 and max(values) - min(values) <= 200  # equal thirds, BE not under-bought


async def test_no_rebalance_without_holdings_section(make):
    env = make(cash=0.0, cfg=live_cfg())
    b = env.broker
    three_stock_account(b)
    await env.send(signal(["加仓 MU | 仓位：33.33% → 40.0% | 成交价：100"], None, "2026-10-02 09:50:59"))
    assert {r.symbol for r in b.placed} == {"MU"}


async def test_pure_exit_does_not_top_up_the_others(make):
    env = make(cash=0.0, cfg=live_cfg())
    b = env.broker
    three_stock_account(b)
    await env.send(signal(["清仓 BE | 仓位：33.33% → 0.0% | 成交价：无"], THREE, "2026-10-02 09:50:59"))
    assert [(r.symbol, r.side) for r in b.placed] == [("BE", SELL)]


async def test_rebalance_never_reopens_a_stopped_out_symbol(make):
    env = make(cash=0.0, cfg=live_cfg())
    b = env.broker
    three_stock_account(b)
    await env.engine.sync_stops()
    await b.trigger_stop("RKLB")
    env.clock.advance(minutes=1)
    await env.send(signal(["减仓 RKLB | 仓位：33.33% → 20.0% | 成交价：无"],
                          ["RKLB | 仓位：20.0% | 成本：50 | 盈亏：0.0%", *THREE[1:], "BE | 仓位：33.33% | 成本：150 | 盈亏：0.0%"],
                          "2026-10-02 09:52:00"))
    env.clock.advance(minutes=1)
    await env.send(signal(
        ["清仓 BE | 仓位：33.33% → 0.0% | 成交价：无", "建仓 NVDA | 仓位：0.0% → 33.33% | 成交价：200"],
        ["RKLB | 仓位：20.0% | 成本：50 | 盈亏：0.0%", THREE[1], "NVDA | 仓位：33.33% | 成本：200 | 盈亏：0.0%"],
        "2026-10-02 09:53:00",
    ))
    assert b.qty("RKLB") == 0
    assert not any(r.symbol == "RKLB" and r.side == BUY for r in b.placed)


async def test_rebalance_does_not_buy_a_chase_skipped_symbol(make):
    env = make(cash=0.0, cfg=live_cfg())
    b = env.broker
    three_stock_account(b)
    b.set_quote("NVDA", 224.9, 225.0, 225.0)  # 12.5% above his 200 → 不追
    await env.send(ROTATE)
    assert b.qty("NVDA") == 0
    env.clock.advance(minutes=5)
    await env.send(signal(["加仓 MU | 仓位：33.33% → 34.0% | 成交价：100"],
                          [THREE[0], "MU | 仓位：34.0% | 成本：100 | 盈亏：0.0%", "NVDA | 仓位：33.33% | 成本：200 | 盈亏：0.0%"],
                          "2026-10-02 09:56:00"))
    assert b.qty("NVDA") == 0


async def test_rebalance_keeps_a_pending_signal_and_its_premarket_order(make):
    env = make(at=ny(2026, 10, 2, 8, 0, 3), cash=0.0, cfg=live_cfg())
    b = env.broker
    three_stock_account(b)
    await env.send(ROTATE.replace("09:50:59", "08:00:00"))
    nvda = b.working("NVDA", LMT)
    assert len(nvda) == 1
    env.clock.set(ny(2026, 10, 2, 8, 30, 0))
    await env.send(signal(["加仓 MU | 仓位：33.33% → 34.0% | 成交价：100"],
                          [THREE[0], "MU | 仓位：34.0% | 成本：100 | 盈亏：0.0%", "NVDA | 仓位：33.33% | 成本：200 | 盈亏：0.0%"],
                          "2026-10-02 08:30:00"))
    assert [o.order_id for o in b.working("NVDA", LMT)] == [nvda[0].order_id]
    pending = {it.symbol: it for it, _ in env.store.list_pending()}
    assert pending["NVDA"].kind == "signal" and pending["NVDA"].signal_price == 200.0


async def test_premarket_confirmation_keeps_working_orders(make):
    env = make(at=ny(2026, 10, 2, 8, 0, 3), cash=0.0, cfg=live_cfg())
    b = env.broker
    three_stock_account(b)
    await env.send(ROTATE.replace("09:50:59", "08:00:00"))
    working = sorted(o.order_id for o in b.working(order_type=LMT))
    n = len(b.placed)
    env.clock.set(ny(2026, 10, 2, 8, 5, 0))
    await env.send(ROTATE.replace("09:50:59", "08:05:00"))  # re-broadcast
    assert sorted(o.order_id for o in b.working(order_type=LMT)) == working
    assert len(b.placed) == n


async def test_rebroadcast_of_a_paused_signal_is_traded_in_full(make):
    env = make(cash=0.0, cfg=live_cfg())
    b = env.broker
    low_margin_three(b)
    env.engine.pause()
    await env.send(signal(ROTATE_TO_BE, HOLD_BE, "2026-10-02 09:50:59"))
    env.engine.resume()
    env.clock.advance(minutes=2)
    await env.send(signal(ROTATE_TO_BE, HOLD_BE, "2026-10-02 09:52:30"))  # his fill broadcast
    assert "重复播报" not in env.texts()[-1]
    values = [b.qty("RKLB") * 50, b.qty("MU") * 100, b.qty("BE") * 150]
    assert b.qty("NVDA") == 0 and max(values) - min(values) <= 200


async def test_sync_preview_outside_regular_hours_says_it_runs_at_the_open(make):
    env = make(at=ny(2026, 10, 2, 8, 0, 3), cash=0.0, cfg=live_cfg())
    three_stock_account(env.broker)
    await env.send(ROTATE.replace("09:50:59", "08:00:00"))
    preview = await env.engine.sync_preview()
    assert "不在盘前/盘后下单" in preview and "先挂限价单" not in preview


async def test_missed_exit_is_forgotten_so_a_later_reentry_is_a_signal(make):
    env = make(cash=0.0, cfg=live_cfg())
    b = env.broker
    three_stock_account(b)
    await env.send(ROTATE)  # remembers NVDA 0→33.33
    env.engine._boot_snapshot_taken = False
    env.clock.set(ny(2026, 10, 2, 11, 0, 0))
    exit_nvda = signal(["清仓 NVDA | 仓位：33.33% → 0.0% | 成交价：无"], THREE, "2026-10-02 10:00:00")
    await env.engine.bootstrap_from_text(exit_nvda)  # missed while offline
    assert "NVDA" not in (env.store.get("last_changes") or {})
