"""Tests for copybot.sizing (DESIGN.md §2 and §3.3) — the money math."""

from __future__ import annotations

import math
import random
from datetime import datetime, timezone

import pytest

from copybot.config import RiskConfig, TradingConfig
from copybot.models import (
    AccountSnapshot,
    Change,
    DesiredStop,
    Intent,
    MarginRate,
    OrderInfo,
    OrderStatus,
    OrderType,
    PlannedOrder,
    Position,
    Quote,
    Side,
    Signal,
    SkipNote,
    STOP_TAG,
)
from copybot.sizing import (
    compute_gross_target,
    deleverage_orders,
    desired_stops,
    diff_stops,
    effective_rate,
    estimate_after_sells,
    extended_limit_price,
    intents_for_sync,
    intents_from_signal,
    resolve_intents,
    round_to_tick,
    scale_buys_for_liquidity,
    split_extended,
    stop_price_for,
)

TS = datetime(2026, 10, 2, 13, 50, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- builders


def cfg(**kw) -> TradingConfig:
    return TradingConfig(**kw)


def acct(nlv=100_000.0, el=40_000.0, af=30_000.0, maint=0.0, init=0.0, gpv=0.0) -> AccountSnapshot:
    return AccountSnapshot(
        net_liquidation=nlv,
        excess_liquidity=el,
        available_funds=af,
        maint_margin=maint,
        init_margin=init,
        gross_position_value=gpv,
        ts=TS,
    )


def pos(sym: str, qty: float, avg: float = 100.0) -> Position:
    return Position(sym, qty, avg)


def positions(*ps: Position) -> dict[str, Position]:
    return {p.symbol: p for p in ps}


def buy(sym: str, w: float, price: float | None = None, reason: str = "") -> Intent:
    return Intent(sym, target_weight=w, direction="buy", signal_price=price, reason=reason, signal_key="fp")


def sell(sym: str, w: float, price: float | None = None, reason: str = "") -> Intent:
    return Intent(sym, target_weight=w, direction="sell", signal_price=price, reason=reason, signal_key="fp")


def both(sym: str, w: float) -> Intent:
    return Intent(sym, target_weight=w, direction="both", reason="同步", signal_key="sync")


def order(
    sym: str,
    side: Side = Side.SELL,
    qty: int = 10,
    *,
    filled: int = 0,
    stop: float | None = None,
    tag: str = "copybot",
    status: OrderStatus = OrderStatus.PENDING,
    oid: str = "1",
    otype: OrderType | None = None,
) -> OrderInfo:
    if otype is None:
        otype = OrderType.STOP if stop is not None else OrderType.LIMIT
    return OrderInfo(
        order_id=oid,
        symbol=sym,
        side=side,
        qty=qty,
        filled_qty=filled,
        avg_fill_price=None,
        order_type=otype,
        status=status,
        stop_price=stop,
        tag=tag,
    )


def stop_order(sym: str, qty: int, stop: float, oid: str, **kw) -> OrderInfo:
    return order(sym, Side.SELL, qty, stop=stop, tag=STOP_TAG, oid=oid, **kw)


def planned(sym: str, side: Side, qty: int, ref: float, cur: int = 0, target: int | None = None) -> PlannedOrder:
    if target is None:
        target = cur + qty if side == Side.BUY else cur - qty
    return PlannedOrder(sym, side, qty, ref, current_qty=cur, target_qty=target, note="n")


def signal(*changes: Change, fp: str = "fp1") -> Signal:
    return Signal(
        changes=tuple(changes),
        holdings=(),
        has_holdings_section=False,
        timestamp=None,
        raw_text="",
        fingerprint=fp,
    )


# --------------------------------------------------------------------------- round_to_tick


@pytest.mark.parametrize(
    ("price", "side", "expected"),
    [
        # nearest
        (422.6 * 0.9, None, 380.34),  # 380.34000000000003
        (100.004, None, 100.0),
        (100.006, None, 100.01),
        (1.005, None, 1.01),  # decimal half rounds up despite 1.005 being 1.00499.. in binary
        (0.12344, None, 0.1234),
        (0.12346, None, 0.1235),
        (0.12345, None, 0.1235),
        # BUY rounds up
        (100.001, Side.BUY, 100.01),
        (100.0, Side.BUY, 100.0),
        (422.6 * 1.005, Side.BUY, 424.72),  # 424.713
        (1.1 * 3, Side.BUY, 3.3),  # 3.3000000000000003 must not become 3.31
        (0.1 + 0.2, Side.BUY, 0.3),  # 0.30000000000000004 must not become 0.3001
        (0.56781, Side.BUY, 0.5679),
        (0.99995, Side.BUY, 1.0),
        # SELL rounds down
        (100.019, Side.SELL, 100.01),
        (100.0, Side.SELL, 100.0),
        (0.7 * 3, Side.SELL, 2.1),  # 2.0999999999999996 must not become 2.09
        (0.56789, Side.SELL, 0.5678),
        (1.0, Side.SELL, 1.0),
        (0.0, None, 0.0),
    ],
)
def test_round_to_tick(price, side, expected):
    assert round_to_tick(price, side) == expected


def test_round_to_tick_result_has_at_most_four_decimals():
    for p in (380.34000000000003, 0.123456789, 12.3456789):
        for side in (None, Side.BUY, Side.SELL):
            r = round_to_tick(p, side)
            assert r == round(r, 4)


def test_round_to_tick_accepts_plain_string_side():
    # Side is a str enum, so "BUY"/"SELL" coming from storage behave the same.
    assert round_to_tick(100.001, "BUY") == 100.01
    assert round_to_tick(100.019, "SELL") == 100.01


@pytest.mark.parametrize(
    ("price", "side"),
    [(664962.71, Side.SELL), (603297.69, Side.SELL), (290715.78, Side.BUY), (712345.67, None)],
)
def test_round_to_tick_large_prices_on_tick_stay_put(price, side):
    # BRK.A-sized prices: division noise grows with magnitude and must not move a tick.
    assert round_to_tick(price, side) == price


def test_round_to_tick_fuzz_exact_and_off_tick():
    rng = random.Random(7)
    for _ in range(20_000):
        cents = rng.randint(100, 100_000_000)
        p = cents / 100
        for side in (Side.BUY, Side.SELL, None):
            assert round_to_tick(p, side) == p
        off = p + 0.003
        b, s = round_to_tick(off, Side.BUY), round_to_tick(off, Side.SELL)
        assert s <= off <= b and b - s == pytest.approx(0.01)
    for k in range(1, 10_000):
        p = k / 10_000
        for side in (Side.BUY, Side.SELL, None):
            assert round_to_tick(p, side) == p


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_round_to_tick_rejects_non_finite(bad):
    with pytest.raises(ValueError):
        round_to_tick(bad)


# --------------------------------------------------------------------------- effective_rate


def test_effective_rate_uses_whatif_rate():
    r = effective_rate("WDC", {"WDC": MarginRate(0.30, 0.25, "whatif")}, cfg())
    assert (r.init, r.maint, r.source) == (0.30, 0.25, "whatif")


def test_effective_rate_default_when_missing():
    r = effective_rate("XYZ", {}, cfg())
    assert (r.init, r.maint, r.source) == (0.5, 0.3, "default")


def test_effective_rate_override_wins():
    c = cfg(margin_overrides={"wdc": {"init": 0.6, "maint": 0.55}})  # keys are upper-cased
    r = effective_rate("WDC", {"WDC": MarginRate(0.30, 0.25, "whatif")}, c)
    assert (r.init, r.maint, r.source) == (0.6, 0.55, "override")


def test_effective_rate_clamps_below_floor():
    r = effective_rate("A", {"A": MarginRate(0.05, 0.02, "whatif")}, cfg())
    assert (r.init, r.maint, r.source) == (0.1, 0.1, "whatif")


def test_effective_rate_clamps_above_one():
    r = effective_rate("A", {"A": MarginRate(1.5, 1.2, "whatif")}, cfg())
    assert (r.init, r.maint) == (1.0, 1.0)


def test_effective_rate_init_never_below_maint():
    r = effective_rate("A", {"A": MarginRate(0.25, 0.30, "whatif")}, cfg())
    assert (r.init, r.maint) == (0.30, 0.30)


def test_effective_rate_invalid_whatif_falls_back_to_default():
    r = effective_rate("A", {"A": MarginRate(math.nan, 0.3, "whatif")}, cfg())
    assert (r.init, r.maint, r.source) == (0.5, 0.3, "default")


def test_effective_rate_custom_floor():
    r = effective_rate("A", {"A": MarginRate(0.2, 0.15, "whatif")}, cfg(min_margin_rate=0.25))
    assert (r.init, r.maint) == (0.25, 0.25)


# --------------------------------------------------------------------------- compute_gross_target


def test_gross_vector_a_single_name_leverage_cap_binds():
    g = compute_gross_target(100_000, {"WDC": 1.0}, {"WDC": MarginRate(0.30, 0.25, "whatif")}, cfg())
    assert g.g_maint == pytest.approx(300_000)
    assert g.g_init == pytest.approx(326_666.6667, rel=1e-9)
    assert g.g_cap == pytest.approx(190_000)
    assert g.gross == pytest.approx(190_000)
    assert g.binding == "leverage_cap"
    assert g.blended_maint == pytest.approx(0.25)
    assert g.blended_init == pytest.approx(0.30)


def test_gross_vector_b_reserve_binds():
    rates = {"A": MarginRate(0.5, 0.4, "whatif"), "B": MarginRate(0.6, 0.5, "whatif")}
    g = compute_gross_target(100_000, {"A": 0.5, "B": 0.5}, rates, cfg())
    assert g.blended_maint == pytest.approx(0.45)
    assert g.blended_init == pytest.approx(0.55)
    assert g.g_maint == pytest.approx(166_666.6667, rel=1e-9)
    assert g.g_init == pytest.approx(178_181.8182, rel=1e-9)
    assert g.gross == pytest.approx(166_666.6667, rel=1e-9)
    assert g.binding == "reserve"


def test_gross_vector_c_unallocated_part_uses_default_rate():
    g = compute_gross_target(100_000, {"A": 0.5}, {"A": MarginRate(0.3, 0.25, "whatif")}, cfg())
    assert g.blended_maint == pytest.approx(0.275)
    assert g.blended_init == pytest.approx(0.4)
    assert g.g_maint == pytest.approx(272_727.2727, rel=1e-9)
    assert g.g_init == pytest.approx(245_000)
    assert g.g_cap == pytest.approx(190_000)
    assert g.gross == pytest.approx(190_000)


def test_gross_vector_d_whatif_below_floor_is_clamped():
    g = compute_gross_target(100_000, {"A": 1.0}, {"A": MarginRate(0.05, 0.02, "whatif")}, cfg())
    assert g.blended_maint == pytest.approx(0.1)
    assert g.blended_init == pytest.approx(0.1)
    assert g.g_maint == pytest.approx(750_000)
    assert g.g_init == pytest.approx(980_000)
    assert g.gross == pytest.approx(190_000)


def test_gross_vector_e_override_wins_over_whatif():
    c = cfg(margin_overrides={"A": {"init": 1.0, "maint": 1.0}})
    g = compute_gross_target(100_000, {"A": 1.0}, {"A": MarginRate(0.3, 0.25, "whatif")}, c)
    assert g.blended_maint == pytest.approx(1.0)
    assert g.g_maint == pytest.approx(75_000)
    assert g.g_init == pytest.approx(98_000)
    assert g.gross == pytest.approx(75_000)
    assert g.binding == "reserve"


def test_gross_vector_f_weights_above_one_shrink_the_cap():
    rates = {"A": MarginRate(0.3, 0.25, "whatif"), "B": MarginRate(0.3, 0.25, "whatif")}
    g = compute_gross_target(100_000, {"A": 0.6, "B": 0.6}, rates, cfg())
    assert g.g_cap == pytest.approx(1.9 * 100_000 / 1.2)
    assert g.blended_maint == pytest.approx(0.3)  # no unallocated part
    assert g.g_maint == pytest.approx(250_000)
    assert g.gross == pytest.approx(158_333.3333, rel=1e-9)


def test_gross_init_margin_can_bind():
    g = compute_gross_target(
        100_000, {"A": 1.0}, {"A": MarginRate(0.6, 0.3, "whatif")}, cfg(reserve_ratio=0.1)
    )
    assert g.g_init == pytest.approx(163_333.3333, rel=1e-9)
    assert g.gross == pytest.approx(g.g_init)
    assert g.binding == "init_margin"


def test_gross_all_cash_uses_default_rates():
    g = compute_gross_target(100_000, {}, {}, cfg())
    assert g.blended_maint == pytest.approx(0.3)
    assert g.blended_init == pytest.approx(0.5)
    assert g.g_maint == pytest.approx(250_000)
    assert g.g_init == pytest.approx(196_000)
    assert g.gross == pytest.approx(190_000)


def test_gross_ignores_non_positive_weights():
    rates = {"A": MarginRate(0.3, 0.25, "whatif"), "Z": MarginRate(1.0, 1.0, "whatif")}
    g1 = compute_gross_target(100_000, {"A": 0.5, "Z": 0.0, "Y": -0.3}, rates, cfg())
    g2 = compute_gross_target(100_000, {"A": 0.5}, rates, cfg())
    assert g1 == g2


@pytest.mark.parametrize("nlv", [0.0, -5_000.0, math.nan])
def test_gross_zero_when_nlv_not_positive(nlv):
    g = compute_gross_target(nlv, {"A": 1.0}, {}, cfg())
    assert (g.gross, g.g_maint, g.g_init, g.g_cap) == (0.0, 0.0, 0.0, 0.0)


def test_gross_scales_linearly_with_nlv():
    rates = {"A": MarginRate(0.5, 0.4, "whatif")}
    g1 = compute_gross_target(100_000, {"A": 1.0}, rates, cfg())
    g2 = compute_gross_target(200_000, {"A": 1.0}, rates, cfg())
    assert g2.gross == pytest.approx(2 * g1.gross)


def test_gross_respects_reserve_when_fully_deployed():
    # At G the maintenance requirement leaves exactly reserve_ratio·NLV as 剩余流动性.
    rates = {"A": MarginRate(0.5, 0.4, "whatif"), "B": MarginRate(0.6, 0.5, "whatif")}
    g = compute_gross_target(100_000, {"A": 0.5, "B": 0.5}, rates, cfg())
    maint_req = 0.5 * g.gross * 0.4 + 0.5 * g.gross * 0.5
    assert 100_000 - maint_req == pytest.approx(25_000)


# --------------------------------------------------------------------------- intents_from_signal


def test_intents_from_example_signal():
    sig = signal(
        Change("建仓", "WDC", 0.0, 1.0, 422.6),
        Change("清仓", "BE", 0.5, 0.0, None),
        Change("清仓", "ARM", 0.5, 0.0, None),
        fp="abc",
    )
    out = intents_from_signal(sig, set())
    assert out == [
        Intent("WDC", 1.0, "buy", 422.6, "建仓 0%→100%", "abc"),
        Intent("BE", 0.0, "sell", None, "清仓 50%→0%", "abc"),
        Intent("ARM", 0.0, "sell", None, "清仓 50%→0%", "abc"),
    ]


def test_intents_reason_formatting():
    sig = signal(
        Change("加仓", "A", 0.3333, 0.5, 10.0),
        Change("减仓", "B", 0.5, 0.25, 20.0),
        Change("减仓", "C", 0.125, 0.07, None),
    )
    reasons = [i.reason for i in intents_from_signal(sig, set())]
    assert reasons == ["加仓 33.33%→50%", "减仓 50%→25%", "减仓 12.5%→7%"]


def test_intents_direction_decided_by_numbers_not_action_word():
    sig = signal(Change("加仓", "A", 0.5, 0.25, None), Change("减仓", "B", 0.1, 0.3, None))
    out = intents_from_signal(sig, set())
    assert [(i.symbol, i.direction) for i in out] == [("A", "sell"), ("B", "buy")]


def test_intents_skip_unchanged_weight():
    sig = signal(Change("调仓", "A", 0.5, 0.5, 10.0), Change("建仓", "B", 0.0, 0.5, 5.0))
    assert [i.symbol for i in intents_from_signal(sig, set())] == ["B"]


def test_intents_duplicate_symbol_last_line_wins_first_order_kept():
    sig = signal(
        Change("建仓", "A", 0.0, 0.5, 10.0),
        Change("建仓", "B", 0.0, 0.3, 20.0),
        Change("减仓", "A", 0.5, 0.25, 11.0),
    )
    out = intents_from_signal(sig, set())
    assert [i.symbol for i in out] == ["A", "B"]
    assert out[0] == Intent("A", 0.25, "sell", 11.0, "减仓 50%→25%", "fp1")


def test_intents_ignore_list_case_insensitive():
    sig = signal(Change("建仓", "WDC", 0.0, 0.5, 1.0), Change("建仓", "BE", 0.0, 0.5, 1.0))
    assert [i.symbol for i in intents_from_signal(sig, {"wdc"})] == ["BE"]


def test_intents_empty_signal():
    assert intents_from_signal(signal(), set()) == []


# --------------------------------------------------------------------------- intents_for_sync


def test_intents_for_sync():
    provider = {"C": 0.5, "A": 0.3, "B": 0.0}
    held = positions(pos("B", 10), pos("D", 5), pos("E", -3), pos("F", 7), pos("A", 2))
    out = intents_for_sync(provider, held, {"F"})
    assert out == [
        Intent("A", 0.3, "both", None, "同步", "sync"),
        Intent("B", 0.0, "both", None, "同步", "sync"),
        Intent("C", 0.5, "both", None, "同步", "sync"),
        Intent("D", 0.0, "both", None, "同步", "sync"),
    ]


def test_intents_for_sync_nothing():
    assert intents_for_sync({}, {}, set()) == []


# --------------------------------------------------------------------------- resolve_intents


def test_resolve_basic_buy():
    quotes = {"WDC": Quote("WDC", bid=422.0, ask=422.6, last=422.3)}
    orders, skips = resolve_intents(
        [buy("WDC", 1.0, 422.6, "建仓 0%→100%")], {}, quotes, 190_000, cfg()
    )
    assert skips == []
    # floor(190000 / 422.6) = floor(449.59) = 449, sized on the ask.
    assert orders == [PlannedOrder("WDC", Side.BUY, 449, 422.6, 0, 449, "建仓 0%→100%")]


def test_resolve_buy_tops_up_existing_position():
    quotes = {"A": Quote("A", ask=100.0)}
    orders, _ = resolve_intents([buy("A", 0.5)], positions(pos("A", 400)), quotes, 100_000, cfg())
    assert orders == [PlannedOrder("A", Side.BUY, 100, 100.0, 400, 500, "")]


def test_resolve_buy_intent_above_target_sells_nothing():
    quotes = {"A": Quote("A", ask=100.0, bid=100.0)}
    orders, skips = resolve_intents([buy("A", 0.5)], positions(pos("A", 800)), quotes, 100_000, cfg())
    assert orders == [] and skips == []


def test_resolve_sell_intent_below_target_buys_nothing():
    quotes = {"A": Quote("A", ask=100.0, bid=100.0)}
    orders, skips = resolve_intents([sell("A", 0.5)], positions(pos("A", 100)), quotes, 100_000, cfg())
    assert orders == [] and skips == []


def test_resolve_partial_sell_sized_on_bid():
    quotes = {"A": Quote("A", bid=420.0, ask=421.0, last=420.5)}
    orders, skips = resolve_intents(
        [sell("A", 0.5, None, "减仓 100%→50%")], positions(pos("A", 449)), quotes, 190_000, cfg()
    )
    assert skips == []
    # target = floor(0.5·190000/420) = floor(226.19) = 226 → sell 223.
    assert orders == [PlannedOrder("A", Side.SELL, 223, 420.0, 449, 226, "减仓 100%→50%")]


def test_resolve_close_without_any_price_sells_full_position():
    held = positions(pos("BE", 37, avg=55.0))
    orders, skips = resolve_intents([sell("BE", 0.0)], held, {}, 190_000, cfg())
    assert skips == []
    assert orders == [PlannedOrder("BE", Side.SELL, 37, 55.0, 37, 0, "")]


def test_resolve_close_with_zero_gross_still_closes():
    held = positions(pos("BE", 37, avg=55.0))
    orders, skips = resolve_intents([sell("BE", 0.0)], held, {}, 0.0, cfg())
    assert [o.qty for o in orders] == [37] and skips == []


def test_resolve_close_ref_price_chain():
    held = positions(pos("A", 10, avg=50.0))
    # bid → mark → signal price → avg cost → 0.0
    cases = [
        ({"A": Quote("A", bid=60.0, last=61.0)}, None, 60.0),
        ({"A": Quote("A", last=61.0)}, None, 61.0),
        ({}, 62.0, 62.0),
        ({}, None, 50.0),
    ]
    for quotes, sp, expected in cases:
        orders, _ = resolve_intents([sell("A", 0.0, sp)], held, quotes, 1_000, cfg())
        assert orders[0].ref_price == expected
    orders, _ = resolve_intents([sell("A", 0.0)], positions(pos("A", 10, avg=0.0)), {}, 1_000, cfg())
    assert orders[0].ref_price == 0.0


def test_resolve_closing_sell_exempt_from_min_value():
    held = positions(pos("A", 1, avg=5.0))
    quotes = {"A": Quote("A", bid=5.0)}
    orders, skips = resolve_intents([sell("A", 0.0)], held, quotes, 100_000, cfg(min_order_value_usd=100))
    assert [(o.side, o.qty) for o in orders] == [(Side.SELL, 1)] and skips == []


def test_resolve_non_closing_sell_below_min_value_skipped():
    held = positions(pos("A", 21, avg=10.0))
    quotes = {"A": Quote("A", bid=10.0)}
    # target = floor(0.2·1000/10) = 20 → sell 1 share = $10 < $100
    orders, skips = resolve_intents([sell("A", 0.2)], held, quotes, 1_000, cfg())
    assert orders == []
    assert skips == [SkipNote("A", "金额太小")]


def test_resolve_buy_below_min_value_skipped():
    quotes = {"A": Quote("A", ask=50.0)}
    orders, skips = resolve_intents([buy("A", 0.01)], {}, quotes, 10_000, cfg())  # 2 shares = $100
    assert [o.qty for o in orders] == [2]  # exactly the minimum is allowed
    orders, skips = resolve_intents([buy("A", 0.005)], {}, quotes, 10_000, cfg())  # 1 share = $50
    assert orders == [] and skips == [SkipNote("A", "金额太小")]
    orders, skips = resolve_intents([buy("A", 0.005)], {}, quotes, 10_000, cfg(min_order_value_usd=0))
    assert [o.qty for o in orders] == [1] and skips == []


def test_resolve_never_short():
    orders, skips = resolve_intents([sell("A", 0.0)], {}, {}, 100_000, cfg())
    assert orders == [] and skips == []
    orders, skips = resolve_intents([both("A", 0.0)], {}, {}, 100_000, cfg())
    assert orders == [] and skips == []


def test_resolve_short_position_is_left_alone():
    held = positions(pos("A", -10))
    quotes = {"A": Quote("A", ask=10.0, bid=10.0)}
    for intent in (buy("A", 0.5), sell("A", 0.0), both("A", 0.5)):
        orders, skips = resolve_intents([intent], held, quotes, 10_000, cfg())
        assert orders == []
        assert skips == [SkipNote("A", "空头持仓，不处理")]


def test_resolve_no_price_for_positive_weight():
    orders, skips = resolve_intents([buy("A", 0.5)], {}, {"A": Quote("A")}, 100_000, cfg())
    assert orders == [] and skips == [SkipNote("A", "没有报价")]
    held = positions(pos("A", 10))
    orders, skips = resolve_intents([sell("A", 0.25)], held, {}, 100_000, cfg())
    assert orders == [] and skips == [SkipNote("A", "没有报价")]


def test_resolve_sell_intent_for_symbol_we_do_not_hold_is_silent():
    orders, skips = resolve_intents([sell("A", 0.25)], {}, {}, 100_000, cfg())
    assert orders == [] and skips == []


def test_resolve_zero_gross_never_turns_reduce_into_close():
    held = positions(pos("A", 100))
    quotes = {"A": Quote("A", bid=100.0, ask=100.0)}
    orders, skips = resolve_intents([sell("A", 0.25)], held, quotes, 0.0, cfg())
    assert orders == []
    assert len(skips) == 1 and skips[0].symbol == "A"
    orders, skips = resolve_intents([both("A", 0.25)], held, quotes, math.nan, cfg())
    assert orders == [] and len(skips) == 1


def test_resolve_buy_sized_on_signal_price_without_quote():
    orders, skips = resolve_intents([buy("A", 0.5, 100.0)], {}, {}, 100_000, cfg())
    assert skips == []
    assert orders == [PlannedOrder("A", Side.BUY, 500, 100.0, 0, 500, "")]


def test_resolve_buy_price_falls_back_to_mark():
    # No ask/last/close → buy_ref is None, mark uses the bid.
    orders, _ = resolve_intents([buy("A", 0.5)], {}, {"A": Quote("A", bid=100.0)}, 100_000, cfg())
    assert [(o.qty, o.ref_price) for o in orders] == [(500, 100.0)]


def test_resolve_chase_guard_skips_when_price_ran_away():
    quotes = {"A": Quote("A", ask=110.5)}
    orders, skips = resolve_intents([buy("A", 0.5, 100.0)], {}, quotes, 100_000, cfg())
    assert orders == []
    assert skips == [SkipNote("A", "现价 110.5 比他的成交价 100 高 10.5%，不追")]


def test_resolve_chase_guard_allows_within_deviation():
    quotes = {"A": Quote("A", ask=109.5)}
    orders, skips = resolve_intents([buy("A", 0.5, 100.0)], {}, quotes, 100_000, cfg())
    assert skips == [] and orders[0].qty == math.floor(50_000 / 109.5)
    # exactly at the limit is still allowed
    quotes = {"A": Quote("A", ask=110.0)}
    orders, skips = resolve_intents([buy("A", 0.5, 100.0)], {}, quotes, 100_000, cfg())
    assert skips == [] and len(orders) == 1


def test_resolve_chase_guard_off():
    quotes = {"A": Quote("A", ask=150.0)}
    # no signal price → no guard
    orders, skips = resolve_intents([buy("A", 0.5)], {}, quotes, 100_000, cfg())
    assert skips == [] and orders[0].qty == 333
    # very loose deviation setting
    orders, skips = resolve_intents(
        [buy("A", 0.5, 100.0)], {}, quotes, 100_000, cfg(max_price_deviation_pct=100)
    )
    assert skips == [] and orders[0].qty == 333


def test_resolve_chase_guard_ignores_signal_price_fallback_and_sells():
    # No live quote: the guard cannot fire (sizing uses the signal price).
    orders, skips = resolve_intents([buy("A", 0.5, 100.0)], {}, {}, 100_000, cfg())
    assert skips == [] and orders[0].qty == 500
    # Sells are never blocked by the chase guard.
    held = positions(pos("A", 100))
    quotes = {"A": Quote("A", bid=200.0, ask=200.0)}
    orders, skips = resolve_intents([sell("A", 0.0, 100.0)], held, quotes, 100_000, cfg())
    assert [o.qty for o in orders] == [100] and skips == []


def test_resolve_both_direction_sizes_on_mark_and_buys():
    quotes = {"A": Quote("A", bid=99.0, ask=101.0, last=100.0)}
    orders, _ = resolve_intents([both("A", 0.5)], positions(pos("A", 40)), quotes, 20_000, cfg())
    # target = floor(0.5·20000/100) = 100 (mark = last), ref price for a buy = ask
    assert orders == [PlannedOrder("A", Side.BUY, 60, 101.0, 40, 100, "同步")]


def test_resolve_both_direction_sells_down_to_target():
    quotes = {"A": Quote("A", bid=99.0, ask=101.0, last=100.0)}
    orders, _ = resolve_intents([both("A", 0.5)], positions(pos("A", 160)), quotes, 20_000, cfg())
    assert orders == [PlannedOrder("A", Side.SELL, 60, 99.0, 160, 100, "同步")]


def test_resolve_sync_band():
    quotes = {"A": Quote("A", bid=100.0, ask=100.0, last=100.0)}
    run = lambda cur, w=0.5: resolve_intents(  # noqa: E731
        [both("A", w)], positions(pos("A", cur)) if cur else {}, quotes, 20_000, cfg(), sync_band=0.05
    )
    # target 100: |delta| < 5 shares → silently skipped
    assert run(97) == ([], [])
    assert run(103) == ([], [])
    # outside the band → trade
    assert [(o.side, o.qty) for o in run(90)[0]] == [(Side.BUY, 10)]
    assert [(o.side, o.qty) for o in run(120)[0]] == [(Side.SELL, 20)]
    assert [(o.side, o.qty) for o in run(95)[0]] == [(Side.BUY, 5)]  # exactly 5% trades
    # target 0 (provider has none) → band does not apply, close everything
    assert [(o.side, o.qty) for o in run(3, 0.0)[0]] == [(Side.SELL, 3)]


def test_resolve_sync_band_default_off():
    quotes = {"A": Quote("A", bid=100.0, ask=100.0, last=100.0)}
    orders, _ = resolve_intents(
        [both("A", 0.5)], positions(pos("A", 99)), quotes, 20_000, cfg(min_order_value_usd=0)
    )
    assert [(o.side, o.qty) for o in orders] == [(Side.BUY, 1)]


def test_resolve_sells_first_and_input_order_within_side():
    quotes = {s: Quote(s, bid=10.0, ask=10.0, last=10.0) for s in "ABCD"}
    held = positions(pos("B", 100), pos("D", 100))
    intents = [buy("A", 0.1), sell("B", 0.0), buy("C", 0.1), sell("D", 0.0)]
    orders, _ = resolve_intents(intents, held, quotes, 100_000, cfg())
    assert [(o.symbol, o.side) for o in orders] == [
        ("B", Side.SELL),
        ("D", Side.SELL),
        ("A", Side.BUY),
        ("C", Side.BUY),
    ]


def test_resolve_duplicate_intents_last_wins():
    quotes = {"A": Quote("A", bid=10.0, ask=10.0, last=10.0)}
    orders, _ = resolve_intents([buy("A", 0.5), buy("A", 0.1)], {}, quotes, 10_000, cfg())
    assert [(o.symbol, o.qty) for o in orders] == [("A", 100)]


def test_resolve_fractional_position_truncated_to_whole_shares():
    held = positions(pos("A", 10.7, avg=10.0))
    orders, _ = resolve_intents([sell("A", 0.0)], held, {}, 1_000, cfg())
    assert [(o.qty, o.current_qty) for o in orders] == [(10, 10)]
    # float noise just below an integer is not a missing share
    held = positions(pos("A", 9.9999999999, avg=10.0))
    orders, _ = resolve_intents([sell("A", 0.0)], held, {}, 1_000, cfg())
    assert orders[0].qty == 10


def test_resolve_share_count_robust_to_float_noise():
    # 0.29·100000 = 28999.999999999996 → /29 must give 1000 shares, not 999.
    orders, _ = resolve_intents([buy("A", 0.29)], {}, {"A": Quote("A", ask=29.0)}, 100_000, cfg())
    assert orders[0].qty == 1000


def test_resolve_invalid_weight_is_skipped():
    held = positions(pos("A", 10))
    orders, skips = resolve_intents([both("A", math.nan)], held, {}, 100_000, cfg())
    assert orders == [] and len(skips) == 1


def test_resolve_empty():
    assert resolve_intents([], {}, {}, 100_000, cfg()) == ([], [])


def _random_quote(rng: random.Random, sym: str) -> Quote:
    pick = lambda: rng.choice([None, round(rng.uniform(0.5, 800), 2)])  # noqa: E731
    return Quote(sym, bid=pick(), ask=pick(), last=pick(), close=pick())


def test_resolve_invariants_randomized():
    """Never short, never overshoot the target, direction clamps always hold."""
    rng = random.Random(42)
    c = cfg(min_order_value_usd=0, max_price_deviation_pct=1e9)
    for _ in range(3_000):
        syms = [f"S{i}" for i in range(rng.randint(1, 6))]
        held = {
            s: pos(s, rng.choice([0, 0, rng.randint(1, 2_000), -rng.randint(1, 50)]), rng.uniform(1, 500))
            for s in syms
        }
        held = {s: p for s, p in held.items() if p.qty != 0}
        quotes = {s: _random_quote(rng, s) for s in syms if rng.random() < 0.8}
        intents = [
            Intent(
                s,
                target_weight=rng.choice([0.0, round(rng.uniform(0, 1.2), 4)]),
                direction=rng.choice(["buy", "sell", "both"]),
                signal_price=rng.choice([None, round(rng.uniform(1, 500), 2)]),
            )
            for s in syms
        ]
        gross = rng.choice([0.0, rng.uniform(1_000, 500_000)])
        orders, skips = resolve_intents(intents, held, quotes, gross, c, sync_band=rng.choice([0, 0.05]))
        by_sym = {i.symbol: i for i in intents}
        seen_buy = False
        for o in orders:
            it = by_sym[o.symbol]
            cur = int(held[o.symbol].qty) if o.symbol in held else 0
            assert o.qty > 0 and o.current_qty == cur >= 0
            if o.side == Side.SELL:
                assert not seen_buy, "sells must come before buys"
                assert it.direction in ("sell", "both")
                assert o.qty <= cur and o.target_qty == cur - o.qty >= 0
            else:
                seen_buy = True
                assert it.direction in ("buy", "both")
                assert gross > 0 and it.target_weight > 0
                assert cur + o.qty == o.target_qty
                # target value at the sizing price never exceeds w·G
                q = quotes.get(o.symbol) or Quote(o.symbol)
                lead = q.buy_ref() if it.direction == "buy" else None
                sizing = lead or q.mark() or it.signal_price
                assert o.target_qty * sizing <= it.target_weight * gross * (1 + 1e-9)
        assert len({o.symbol for o in orders}) == len(orders)


# --------------------------------------------------------------------------- estimate_after_sells


def test_estimate_after_sells():
    a = acct(nlv=100_000, el=10_000, af=5_000, maint=80_000, init=100_000, gpv=180_000)
    rates = {"A": MarginRate(0.5, 0.3, "whatif")}
    out = estimate_after_sells(a, [("A", 100, 600.0)], rates, cfg())
    assert out.net_liquidation == 100_000
    assert out.excess_liquidity == pytest.approx(28_000)
    assert out.available_funds == pytest.approx(35_000)
    assert out.maint_margin == pytest.approx(62_000)
    assert out.init_margin == pytest.approx(70_000)
    assert out.gross_position_value == pytest.approx(120_000)
    assert out.ts == a.ts
    assert a.excess_liquidity == 10_000  # input untouched


def test_estimate_after_sells_uses_default_rate_and_floors_at_zero():
    a = acct(nlv=100_000, el=10_000, af=5_000, maint=1_000, init=1_000, gpv=10_000)
    out = estimate_after_sells(a, [("X", 100, 200.0)], {}, cfg())  # default .5 / .3, v = 20000
    assert out.excess_liquidity == pytest.approx(16_000)
    assert out.available_funds == pytest.approx(15_000)
    assert (out.maint_margin, out.init_margin, out.gross_position_value) == (0.0, 0.0, 0.0)


def test_estimate_after_sells_ignores_empty_lines():
    a = acct()
    assert estimate_after_sells(a, [], {}, cfg()) == a
    assert estimate_after_sells(a, [("A", 0, 10.0), ("B", 5, 0.0)], {}, cfg()) == a


# --------------------------------------------------------------------------- scale_buys_for_liquidity


X_RATES = {"X": MarginRate(0.5, 0.4, "whatif"), "Y": MarginRate(0.5, 0.4, "whatif")}


def test_scale_buys_vector_maint_binds():
    a = acct(nlv=100_000, el=40_000, af=30_000, gpv=120_000)
    out, s = scale_buys_for_liquidity([planned("X", Side.BUY, 100, 500.0)], a, X_RATES, cfg())
    assert s == pytest.approx(0.75)
    assert [(o.symbol, o.qty, o.ref_price) for o in out] == [("X", 75, 500.0)]
    assert out[0].target_qty == 100  # target keeps the original intent target


def test_scale_buys_no_room_buys_nothing():
    a = acct(nlv=100_000, el=20_000, af=30_000, gpv=120_000)  # EL below the 25% reserve
    out, s = scale_buys_for_liquidity([planned("X", Side.BUY, 100, 500.0)], a, X_RATES, cfg())
    assert s == 0.0 and out == []


def test_scale_buys_gross_cap_binds():
    a = acct(nlv=100_000, el=90_000, af=90_000, gpv=180_000)
    out, s = scale_buys_for_liquidity([planned("X", Side.BUY, 100, 500.0)], a, X_RATES, cfg())
    assert s == pytest.approx(0.2)
    assert [o.qty for o in out] == [20]


def test_scale_buys_init_binds():
    a = acct(nlv=100_000, el=90_000, af=14_500, gpv=0)
    out, s = scale_buys_for_liquidity([planned("X", Side.BUY, 100, 500.0)], a, X_RATES, cfg())
    # need_i = 25000, room_i = 14500 - 2000 = 12500 → 0.5
    assert s == pytest.approx(0.5) and [o.qty for o in out] == [50]


def test_scale_buys_enough_room_unchanged():
    a = acct(nlv=100_000, el=90_000, af=90_000, gpv=0)
    buys = [planned("X", Side.BUY, 100, 500.0), planned("Y", Side.BUY, 3, 40.0)]
    out, s = scale_buys_for_liquidity(buys, a, X_RATES, cfg())
    assert s == 1.0 and out == buys


def test_scale_buys_same_factor_for_all_and_drops_tiny():
    a = acct(nlv=100_000, el=40_000, af=30_000, gpv=120_000)
    buys = [planned("X", Side.BUY, 100, 500.0), planned("Y", Side.BUY, 3, 40.0)]
    out, s = scale_buys_for_liquidity(buys, a, X_RATES, cfg())
    # need_m = (50000 + 120)·0.4 = 20048, room 15000 → s ≈ 0.7482 → X 74, Y 2 ($80 < $100 dropped)
    assert s == pytest.approx(15_000 / 20_048)
    assert [(o.symbol, o.qty) for o in out] == [("X", 74)]


def test_scale_buys_not_positive_nlv_buys_nothing():
    a = acct(nlv=0.0, el=50_000, af=50_000)
    out, s = scale_buys_for_liquidity([planned("X", Side.BUY, 1, 500.0)], a, X_RATES, cfg())
    assert s == 0.0 and out == []


def test_scale_buys_nan_account_buys_nothing():
    a = acct(el=math.nan)
    out, s = scale_buys_for_liquidity([planned("X", Side.BUY, 1, 500.0)], a, X_RATES, cfg())
    assert s == 0.0 and out == []


def test_scale_buys_empty():
    assert scale_buys_for_liquidity([], acct(), {}, cfg()) == ([], 1.0)


def test_scale_buys_after_sells_pipeline():
    # Sells free margin first; the guard must see the post-sell account.
    a = acct(nlv=100_000, el=10_000, af=5_000, maint=90_000, init=100_000, gpv=190_000)
    rates = {"OLD": MarginRate(0.5, 0.5, "whatif"), "X": MarginRate(0.5, 0.4, "whatif")}
    after = estimate_after_sells(a, [("OLD", 380, 500.0)], rates, cfg())  # v = 190000
    assert after.excess_liquidity == pytest.approx(105_000)
    out, s = scale_buys_for_liquidity([planned("X", Side.BUY, 380, 500.0)], after, rates, cfg())
    # need_m = 76000, room_m = 80000 → ok; need_i = 95000, room_i = 98000 → ok; gross 190000 ok
    assert s == 1.0 and out[0].qty == 380


# --------------------------------------------------------------------------- split_extended


def test_split_extended():
    orders = [
        planned("A", Side.BUY, 101, 0.5),
        planned("B", Side.SELL, 30, 10.0, cur=30),
        planned("C", Side.BUY, 1, 100.0),
    ]
    out = split_extended(orders, 0.5, 1.0)
    assert [(o.symbol, o.side, o.qty) for o in out] == [("A", Side.BUY, 50), ("B", Side.SELL, 30)]
    assert out[1] is orders[1]  # fraction 1.0 → unchanged
    assert out[0].target_qty == orders[0].target_qty


def test_split_extended_sell_fraction():
    out = split_extended([planned("B", Side.SELL, 7, 10.0, cur=7)], 0.5, 0.5)
    assert [o.qty for o in out] == [3]


def test_split_extended_zero_fraction_drops_all():
    assert split_extended([planned("A", Side.BUY, 100, 1.0)], 0.0, 1.0) == []


def test_split_extended_fraction_clamped():
    out = split_extended([planned("B", Side.SELL, 7, 10.0, cur=7)], 0.5, 1.5)
    assert [o.qty for o in out] == [7]  # never more than planned


# --------------------------------------------------------------------------- extended_limit_price


def test_extended_limit_price_buy_from_ask():
    assert extended_limit_price(Side.BUY, Quote("A", bid=99.0, ask=100.0), None, 0.5) == 100.5
    assert extended_limit_price(Side.BUY, Quote("A", ask=422.6), 400.0, 0.5) == 424.72


def test_extended_limit_price_sell_from_bid():
    assert extended_limit_price(Side.SELL, Quote("A", bid=422.6, ask=423.0), None, 0.5) == 420.48


def test_extended_limit_price_quote_fallbacks():
    # buy_ref falls back to last, sell_ref to last as well
    assert extended_limit_price(Side.BUY, Quote("A", last=200.0), None, 1.0) == 202.0
    assert extended_limit_price(Side.SELL, Quote("A", last=200.0), None, 1.0) == 198.0


def test_extended_limit_price_signal_fallback():
    assert extended_limit_price(Side.BUY, None, 100.0, 0.5) == 100.5
    assert extended_limit_price(Side.BUY, Quote("A"), 100.0, 0.5) == 100.5
    assert extended_limit_price(Side.SELL, Quote("A"), 100.0, 0.5) == 99.5


def test_extended_limit_price_none_without_price():
    assert extended_limit_price(Side.BUY, None, None, 0.5) is None
    assert extended_limit_price(Side.SELL, Quote("A"), None, 0.5) is None
    assert extended_limit_price(Side.SELL, Quote("A"), 0.0, 0.5) is None


def test_extended_limit_price_sub_dollar():
    assert extended_limit_price(Side.BUY, Quote("A", ask=0.5), None, 0.5) == 0.5025
    assert extended_limit_price(Side.SELL, Quote("A", bid=0.5), None, 0.5) == 0.4975


# --------------------------------------------------------------------------- stops


@pytest.mark.parametrize(
    ("avg", "pct", "expected"),
    [(422.6, 10, 380.34), (100.0, 7, 93.0), (0.5, 7, 0.465), (33.333, 7, 31.0)],
)
def test_stop_price_for(avg, pct, expected):
    assert stop_price_for(avg, pct) == expected


def test_desired_stops():
    held = positions(
        pos("A", 100, avg=50.0),
        pos("B", 10, avg=0.0),  # unknown cost → no stop
        pos("C", -5, avg=20.0),  # short → no stop
        pos("D", 20, avg=20.0),  # ignored
        pos("E", 10, avg=10.0),
    )
    working = [
        order("A", Side.SELL, 40, filled=10, oid="1"),  # 30 remaining → reduces the stop
        stop_order("A", 100, 46.5, oid="2"),  # stops themselves do not count
        order("A", Side.BUY, 50, oid="3"),  # buys do not count
        order("A", Side.SELL, 50, status=OrderStatus.CANCELLED, oid="4"),  # done
        order("E", Side.SELL, 10, oid="5"),  # whole position already being sold
        order("Z", Side.SELL, 10, oid="6"),  # other symbol
    ]
    out = desired_stops(held, working, 7.0, {"d"})
    assert out == {"A": DesiredStop("A", 70, 46.5)}


def test_desired_stops_simple():
    out = desired_stops(positions(pos("WDC", 449, avg=422.6)), [], 10.0, set())
    assert out == {"WDC": DesiredStop("WDC", 449, 380.34)}


def test_diff_stops_creates_missing():
    want = {"A": DesiredStop("A", 70, 46.5), "B": DesiredStop("B", 5, 9.3)}
    acts = diff_stops(want, [])
    assert acts.create == [want["A"], want["B"]] and acts.modify == [] and acts.cancel == []


def test_diff_stops_exact_match_is_noop():
    want = {"A": DesiredStop("A", 70, 46.5)}
    acts = diff_stops(want, [stop_order("A", 70, 46.504, oid="s1")])
    assert acts.empty


def test_diff_stops_modify_on_qty_or_price_change():
    want = {"A": DesiredStop("A", 70, 46.5)}
    acts = diff_stops(want, [stop_order("A", 100, 46.5, oid="s1")])
    assert acts.modify == [("s1", want["A"])] and not acts.create and not acts.cancel
    acts = diff_stops(want, [stop_order("A", 70, 46.51, oid="s1")])
    assert acts.modify == [("s1", want["A"])]
    acts = diff_stops(want, [stop_order("A", 70, None, oid="s1")])
    assert acts.modify == [("s1", want["A"])]


def test_diff_stops_duplicates_keep_exact_match():
    want = {"A": DesiredStop("A", 70, 46.5)}
    existing = [stop_order("A", 100, 40.0, oid="s1"), stop_order("A", 70, 46.5, oid="s2")]
    acts = diff_stops(want, existing)
    assert acts.cancel == ["s1"] and not acts.modify and not acts.create


def test_diff_stops_duplicates_modify_first_cancel_rest():
    want = {"A": DesiredStop("A", 70, 46.5)}
    existing = [
        stop_order("A", 100, 40.0, oid="s1"),
        stop_order("A", 90, 41.0, oid="s2"),
        stop_order("A", 80, 42.0, oid="s3"),
    ]
    acts = diff_stops(want, existing)
    assert acts.modify == [("s1", want["A"])]
    assert acts.cancel == ["s2", "s3"]
    assert not acts.create


def test_diff_stops_cancels_stops_for_symbols_not_desired():
    acts = diff_stops({}, [stop_order("A", 10, 9.0, oid="s1"), stop_order("A", 10, 9.0, oid="s2")])
    assert acts.cancel == ["s1", "s2"] and not acts.create and not acts.modify


def test_diff_stops_ignores_done_and_non_stop_orders():
    want = {"A": DesiredStop("A", 70, 46.5)}
    existing = [
        stop_order("A", 70, 46.5, oid="s1", status=OrderStatus.CANCELLED),
        order("A", Side.SELL, 70, oid="l1"),  # a working LMT sell, not a stop
        order("B", Side.SELL, 5, stop=1.0, tag="copybot", oid="x1"),  # STP without stop tag
    ]
    acts = diff_stops(want, existing)
    assert acts.create == [want["A"]] and not acts.modify and not acts.cancel


def test_diff_stops_partly_filled_stop_is_replaced_not_modified():
    want = {"A": DesiredStop("A", 50, 46.5)}
    acts = diff_stops(want, [stop_order("A", 100, 46.5, oid="s1", filled=30)])
    assert acts.create == [want["A"]] and acts.cancel == ["s1"] and not acts.modify
    # remaining == desired → already right
    acts = diff_stops({"A": DesiredStop("A", 70, 46.5)}, [stop_order("A", 100, 46.5, oid="s1", filled=30)])
    assert acts.empty


def test_diff_stops_full_cycle():
    want = {"A": DesiredStop("A", 70, 46.5), "B": DesiredStop("B", 5, 9.3)}
    existing = [
        stop_order("A", 60, 46.5, oid="a1"),
        stop_order("C", 3, 1.0, oid="c1"),
    ]
    acts = diff_stops(want, existing)
    assert acts.modify == [("a1", want["A"])]
    assert acts.cancel == ["c1"]
    assert acts.create == [want["B"]]


# --------------------------------------------------------------------------- deleverage


DELEV_RATES = {"A": MarginRate(0.4, 0.3, "whatif"), "B": MarginRate(0.5, 0.5, "whatif")}


def test_deleverage_vector():
    a = acct(nlv=100_000, el=10_000, af=0, gpv=120_000)
    held = positions(pos("A", 100, avg=500.0), pos("B", 200, avg=250.0))
    quotes = {"A": Quote("A", last=600.0), "B": Quote("B", last=300.0)}
    out = deleverage_orders(a, held, quotes, DELEV_RATES, cfg(), RiskConfig(), set())
    # needed 15000 / (18000 + 30000) = 0.3125 → ceil(31.25) = 32, ceil(62.5) = 63
    assert out == [
        PlannedOrder("A", Side.SELL, 32, 600.0, 100, 68, "降杠杆"),
        PlannedOrder("B", Side.SELL, 63, 300.0, 200, 137, "降杠杆"),
    ]


def test_deleverage_price_falls_back_to_avg_cost():
    a = acct(nlv=100_000, el=10_000)
    held = positions(pos("A", 100, avg=600.0), pos("B", 200, avg=300.0))
    out = deleverage_orders(a, held, {}, DELEV_RATES, cfg(), RiskConfig(), set())
    assert [(o.symbol, o.qty, o.ref_price) for o in out] == [("A", 32, 600.0), ("B", 63, 300.0)]


def test_deleverage_not_triggered():
    held = positions(pos("A", 100, avg=600.0))
    # el_ratio 0.15 == trigger → nothing
    assert deleverage_orders(acct(el=15_000), held, {}, DELEV_RATES, cfg(), RiskConfig(), set()) == []
    # disabled
    risk = RiskConfig(deleverage_enabled=False)
    assert deleverage_orders(acct(el=1_000), held, {}, DELEV_RATES, cfg(), risk, set()) == []
    # NLV <= 0
    assert deleverage_orders(acct(nlv=0, el=-5_000), held, {}, DELEV_RATES, cfg(), RiskConfig(), set()) == []


def test_deleverage_caps_at_full_position():
    a = acct(nlv=100_000, el=-50_000)  # needed 75000 > denom 18000 → s = 1
    held = positions(pos("A", 100, avg=600.0))
    out = deleverage_orders(a, held, {}, DELEV_RATES, cfg(), RiskConfig(), set())
    assert [(o.qty, o.target_qty) for o in out] == [(100, 0)]


def test_deleverage_skips_ignored_and_short_positions():
    a = acct(nlv=100_000, el=10_000)
    held = positions(pos("A", 100, avg=600.0), pos("B", 200, avg=300.0), pos("S", -50, avg=10.0))
    out = deleverage_orders(a, held, {}, DELEV_RATES, cfg(), RiskConfig(), {"b"})
    # only A is managed: s = min(1, 15000 / 18000) = 0.8333 → ceil(83.33) = 84
    assert [(o.symbol, o.qty) for o in out] == [("A", 84)]


def test_deleverage_nothing_managed():
    a = acct(nlv=100_000, el=10_000)
    assert deleverage_orders(a, {}, {}, DELEV_RATES, cfg(), RiskConfig(), set()) == []
    held = positions(pos("A", 100, avg=0.0))  # no price at all → denom 0
    assert deleverage_orders(a, held, {}, DELEV_RATES, cfg(), RiskConfig(), set()) == []


def test_scale_buys_invariants_randomized():
    """After scaling, the buys fit inside every room (reserve, initial margin, gross cap)."""
    rng = random.Random(3)
    c = cfg(min_order_value_usd=0)
    rates = {s: MarginRate(rng.uniform(0.25, 1.0), 0.0, "whatif") for s in "ABCDE"}
    rates = {s: MarginRate(r.init, rng.uniform(0.1, r.init), "whatif") for s, r in rates.items()}
    for _ in range(3_000):
        nlv = rng.uniform(1_000, 1_000_000)
        a = acct(
            nlv=nlv,
            el=rng.uniform(-0.2, 1.0) * nlv,
            af=rng.uniform(-0.2, 1.0) * nlv,
            gpv=rng.uniform(0, 2.5) * nlv,
        )
        buys = [
            planned(s, Side.BUY, rng.randint(1, 5_000), round(rng.uniform(1, 900), 2))
            for s in rng.sample("ABCDE", rng.randint(1, 5))
        ]
        out, s = scale_buys_for_liquidity(buys, a, rates, c)
        assert 0.0 <= s <= 1.0
        if s >= 1.0:
            assert out == buys
            continue
        need_m = sum(o.qty * o.ref_price * effective_rate(o.symbol, rates, c).maint for o in out)
        need_i = sum(o.qty * o.ref_price * effective_rate(o.symbol, rates, c).init for o in out)
        need_g = sum(o.qty * o.ref_price for o in out)
        tol = 1e-6 * nlv
        assert need_m <= max(0.0, a.excess_liquidity - c.reserve_ratio * nlv) + tol
        assert need_i <= max(0.0, a.available_funds - c.init_margin_buffer * nlv) + tol
        assert need_g <= max(0.0, c.max_gross_leverage * nlv - a.gross_position_value) + tol
        originals = {o.symbol: o.qty for o in buys}
        assert all(0 < o.qty <= originals[o.symbol] for o in out)


def test_resolve_sync_band_is_silent_even_when_below_min_value():
    # target 100 @ $10; delta 3 shares = $30 is both inside the band and below the
    # $100 minimum: the band wins, so !sync previews are not flooded with「金额太小」.
    quotes = {"A": Quote("A", bid=10.0, ask=10.0, last=10.0)}
    out = resolve_intents([both("A", 0.5)], positions(pos("A", 97)), quotes, 2_000, cfg(), sync_band=0.05)
    assert out == ([], [])
    # outside the band but still tiny → reported
    out = resolve_intents([both("A", 0.5)], positions(pos("A", 92)), quotes, 2_000, cfg(), sync_band=0.05)
    assert out == ([], [SkipNote("A", "金额太小")])
