"""Position sizing, margin math and stop-loss planning (DESIGN.md §2 and §3.3).

Everything here is pure: no I/O, no clock, no randomness. Every money decision of
the bot goes through these functions, so they are small, explicit and tested.

Conventions (see models.py): weights are fractions, money is USD floats, share
quantities are ints, symbols are the provider's upper-case spelling.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace

from .config import RiskConfig, TradingConfig
from .models import (
    AccountSnapshot,
    Change,
    DesiredStop,
    GrossTarget,
    Intent,
    MarginRate,
    OrderInfo,
    PlannedOrder,
    Position,
    Quote,
    Side,
    Signal,
    SkipNote,
    StopActions,
)

# Float-noise tolerance used when turning money / prices into whole shares or ticks.
_EPS = 1e-9
# Position quantities from the broker are floats; ignore noise far below the
# smallest real fractional-share increment (0.0001) when truncating to whole shares.
_QTY_EPS = 1e-6
# Two stop prices closer than this (half a cent) are considered equal.
STOP_PRICE_TOLERANCE = 0.005

# Skip reasons (user-facing, Simplified Chinese).
SKIP_SHORT = "空头持仓，不处理"
SKIP_NO_QUOTE = "没有报价"
SKIP_TOO_SMALL = "金额太小"
SKIP_NO_GROSS = "仓位基数为 0，无法计算股数"
SKIP_BAD_WEIGHT = "目标仓位无效"
SKIP_BAD_DIRECTION = "未知方向"
NOTE_SYNC = "同步"
NOTE_DELEVERAGE = "降杠杆"


# --------------------------------------------------------------------------- small helpers


def _positive(x: float | None) -> float | None:
    """Return x if it is a finite number > 0, else None."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or x <= 0:
        return None
    return x


def _floor(x: float) -> int:
    """floor() that is robust to float noise just below an integer (2.9999999999 → 3)."""
    return math.floor(x + _EPS)


def _ceil(x: float) -> int:
    """ceil() that is robust to float noise just above an integer (3.0000000001 → 3)."""
    return math.ceil(x - _EPS)


def _whole_shares(qty: float) -> int:
    """Truncate a broker position quantity toward zero to whole shares."""
    if qty >= 0:
        return math.floor(qty + _QTY_EPS)
    return math.ceil(qty - _QTY_EPS)


def _norm_ignore(ignore: Iterable[str] | None) -> set[str]:
    return {s.strip().upper() for s in (ignore or ())}


def _fmt_pct(weight: float) -> str:
    """0.5 → "50", 1.0 → "100", 0.3333 → "33.33"."""
    return f"{round(weight * 100, 2):g}"


def _fmt_num(x: float) -> str:
    """422.6 → "422.6", 100.0 → "100", 0.12345 → "0.1235"."""
    return f"{x:.4f}".rstrip("0").rstrip(".")


def _clamp01(x: float) -> float:
    if not math.isfinite(x):
        return 0.0
    return min(max(x, 0.0), 1.0)


# --------------------------------------------------------------------------- ticks


def tick_size(price: float) -> float:
    """US stock tick: $0.01 at or above $1, $0.0001 below."""
    return 0.01 if price >= 1 else 0.0001


def round_to_tick(price: float, side: Side | None = None) -> float:
    """Round a price to a valid tick.

    BUY rounds up, SELL rounds down (both make limit orders a little more marketable),
    None rounds to the nearest tick (half up).
    """
    if not math.isfinite(price):
        raise ValueError(f"price must be finite, got {price!r}")
    tick = tick_size(price)
    steps = price / tick
    # Division by a non-representable tick adds noise proportional to the magnitude
    # (≈1e-8 steps at $700k), so the tolerance scales with it.
    tol = _EPS + abs(steps) * 1e-12
    if side == Side.BUY:
        n = math.ceil(steps - tol)
    elif side == Side.SELL:
        n = math.floor(steps + tol)
    else:
        n = math.floor(steps + 0.5 + tol)
    return round(n * tick, 4)


# --------------------------------------------------------------------------- margin rates


def _rate_is_valid(rate: MarginRate | None) -> bool:
    return rate is not None and math.isfinite(rate.init) and math.isfinite(rate.maint)


def _clamp_rate(rate: MarginRate, cfg: TradingConfig) -> MarginRate:
    """Clamp init / maint into [min_margin_rate, 1] and make init >= maint."""
    lo = min(max(cfg.min_margin_rate, 0.0), 1.0)
    maint = min(max(rate.maint, lo), 1.0)
    init = min(max(rate.init, lo), 1.0)
    return MarginRate(init=max(init, maint), maint=maint, source=rate.source)


def effective_rate(symbol: str, rates: Mapping[str, MarginRate], cfg: TradingConfig) -> MarginRate:
    """Margin rate for a symbol: config override → cached whatIf → config default, clamped."""
    for candidate in (cfg.override_rate(symbol), rates.get(symbol)):
        if _rate_is_valid(candidate):
            return _clamp_rate(candidate, cfg)
    return _clamp_rate(cfg.default_rate(), cfg)


# --------------------------------------------------------------------------- G (100% 仓位)


def _limit(numerator: float, rate: float) -> float:
    """numerator / rate, or +inf when the rate is zero (no constraint)."""
    return numerator / rate if rate > 0 else math.inf


def compute_gross_target(
    nlv: float,
    weights: Mapping[str, float],
    rates: Mapping[str, MarginRate],
    cfg: TradingConfig,
) -> GrossTarget:
    """Dollar value that "100% 仓位" maps to (DESIGN.md §2.1)."""
    if not math.isfinite(nlv) or nlv <= 0:
        return GrossTarget(gross=0.0, g_maint=0.0, g_init=0.0, g_cap=0.0, blended_maint=0.0, blended_init=0.0)

    w = {sym: x for sym, x in weights.items() if math.isfinite(x) and x > 0}
    sum_w = sum(w.values())
    unallocated = max(0.0, 1.0 - sum_w)
    default = _clamp_rate(cfg.default_rate(), cfg)

    blended_maint = unallocated * default.maint
    blended_init = unallocated * default.init
    for sym, x in w.items():
        r = effective_rate(sym, rates, cfg)
        blended_maint += x * r.maint
        blended_init += x * r.init

    g_maint = _limit((1.0 - cfg.reserve_ratio) * nlv, blended_maint)
    g_init = _limit((1.0 - cfg.init_margin_buffer) * nlv, blended_init)
    g_cap = cfg.max_gross_leverage * nlv / max(1.0, sum_w)
    gross = max(0.0, min(g_maint, g_init, g_cap))
    return GrossTarget(
        gross=gross,
        g_maint=g_maint,
        g_init=g_init,
        g_cap=g_cap,
        blended_maint=blended_maint,
        blended_init=blended_init,
    )


# --------------------------------------------------------------------------- intents


def intents_from_signal(signal: Signal, ignore: Iterable[str]) -> list[Intent]:
    """One Intent per changed symbol; the last line wins, first-seen order is kept."""
    skip = _norm_ignore(ignore)
    last: dict[str, Change] = {}
    for ch in signal.changes:
        if ch.symbol.upper() in skip:
            continue
        last[ch.symbol] = ch  # re-assignment keeps the first-seen position

    intents: list[Intent] = []
    for ch in last.values():
        if ch.is_increase:
            direction = "buy"
        elif ch.is_decrease:
            direction = "sell"
        else:
            continue
        reason = f"{ch.action} {_fmt_pct(ch.from_weight)}%→{_fmt_pct(ch.to_weight)}%".strip()
        intents.append(
            Intent(
                symbol=ch.symbol,
                target_weight=ch.to_weight,
                direction=direction,
                signal_price=ch.price,
                reason=reason,
                signal_key=signal.fingerprint,
            )
        )
    return intents


def intents_for_sync(
    provider_weights: Mapping[str, float],
    positions: Mapping[str, Position],
    ignore: Iterable[str],
) -> list[Intent]:
    """!sync: rebalance every provider symbol and every long we hold (weight 0 if he has none)."""
    skip = _norm_ignore(ignore)
    symbols = {s for s, w in provider_weights.items() if w > 0}
    symbols |= {s for s, p in positions.items() if p.qty > 0}
    return [
        Intent(
            symbol=sym,
            target_weight=provider_weights.get(sym, 0.0),
            direction="both",
            reason=NOTE_SYNC,
            signal_key="sync",
        )
        for sym in sorted(s for s in symbols if s.upper() not in skip)
    ]


# --------------------------------------------------------------------------- intents → orders


def _sizing_price(direction: str, quote: Quote, signal_price: float | None) -> float | None:
    """Price used to turn a weight into shares; quote first, then the provider's price."""
    if direction == "buy":
        price = quote.buy_ref() or quote.mark()
    elif direction == "sell":
        price = quote.sell_ref() or quote.mark()
    else:
        price = quote.mark()
    return price or signal_price


def _last_per_symbol(intents: Iterable[Intent]) -> list[Intent]:
    """Two intents for one symbol would trade twice from the same position: last one wins."""
    by_symbol: dict[str, Intent] = {}
    for it in intents:
        by_symbol[it.symbol] = it
    return list(by_symbol.values())


def _in_sync_band(delta: int, target: int, sync_band: float) -> bool:
    """|delta value| < sync_band · target value (the price cancels out)."""
    return sync_band > 0 and target > 0 and abs(delta) < sync_band * target


def _plan_sell(
    intent: Intent,
    pos: Position | None,
    quote: Quote,
    signal_price: float | None,
    cur: int,
    target: int,
    delta: int,
    cfg: TradingConfig,
    sync_band: float,
) -> PlannedOrder | SkipNote | None:
    qty = min(-delta, cur)  # never sell more than we hold → never short
    if qty <= 0:
        return None
    ref = (
        quote.sell_ref()
        or quote.mark()
        or signal_price
        or _positive(pos.avg_cost if pos is not None else None)
        or 0.0
    )
    closing = target == 0
    if _in_sync_band(delta, target, sync_band):
        return None
    if not closing and qty * ref < cfg.min_order_value_usd:
        return SkipNote(intent.symbol, SKIP_TOO_SMALL)
    return PlannedOrder(
        symbol=intent.symbol,
        side=Side.SELL,
        qty=qty,
        ref_price=ref,
        current_qty=cur,
        target_qty=cur - qty,
        note=intent.reason,
    )


def _plan_buy(
    intent: Intent,
    quote: Quote,
    signal_price: float | None,
    cur: int,
    target: int,
    delta: int,
    cfg: TradingConfig,
    sync_band: float,
) -> PlannedOrder | SkipNote | None:
    sym = intent.symbol
    # Chase guard: only a live quote counts (never the signal price itself).
    live = quote.buy_ref()
    if signal_price and live and live > signal_price * (1 + cfg.max_price_deviation_pct / 100):
        pct = (live / signal_price - 1) * 100
        return SkipNote(
            sym, f"现价 {_fmt_num(live)} 比他的成交价 {_fmt_num(signal_price)} 高 {pct:.1f}%，不追"
        )
    ref = live or quote.mark() or signal_price
    if ref is None:  # unreachable: target > 0 implies a sizing price existed
        return SkipNote(sym, SKIP_NO_QUOTE)
    if _in_sync_band(delta, target, sync_band):
        return None
    if delta * ref < cfg.min_order_value_usd:
        return SkipNote(sym, SKIP_TOO_SMALL)
    return PlannedOrder(
        symbol=sym,
        side=Side.BUY,
        qty=delta,
        ref_price=ref,
        current_qty=cur,
        target_qty=target,
        note=intent.reason,
    )


def _resolve_one(
    intent: Intent,
    positions: Mapping[str, Position],
    quotes: Mapping[str, Quote],
    gross: float,
    cfg: TradingConfig,
    sync_band: float,
) -> PlannedOrder | SkipNote | None:
    sym = intent.symbol
    pos = positions.get(sym)
    cur = _whole_shares(pos.qty) if pos is not None else 0
    if cur < 0:
        return SkipNote(sym, SKIP_SHORT)
    if intent.direction not in ("buy", "sell", "both"):
        return SkipNote(sym, SKIP_BAD_DIRECTION)
    if intent.direction == "sell" and cur == 0:
        return None  # nothing to sell, and a sell intent never buys
    weight = intent.target_weight
    if not math.isfinite(weight):
        return SkipNote(sym, SKIP_BAD_WEIGHT)

    quote = quotes.get(sym) or Quote(sym)
    signal_price = _positive(intent.signal_price)

    if weight <= 0:
        target = 0  # close: no price needed
    else:
        if not math.isfinite(gross) or gross <= 0:
            # A broken / zero G must never turn "reduce" into "sell everything".
            return SkipNote(sym, SKIP_NO_GROSS)
        price = _sizing_price(intent.direction, quote, signal_price)
        if price is None:
            return SkipNote(sym, SKIP_NO_QUOTE)
        target = _floor(weight * gross / price)

    delta = target - cur
    if intent.direction == "buy":
        delta = max(0, delta)
    elif intent.direction == "sell":
        delta = min(0, delta)
    if delta == 0:
        return None
    if delta < 0:
        return _plan_sell(intent, pos, quote, signal_price, cur, target, delta, cfg, sync_band)
    return _plan_buy(intent, quote, signal_price, cur, target, delta, cfg, sync_band)


def resolve_intents(
    intents: Iterable[Intent],
    positions: Mapping[str, Position],
    quotes: Mapping[str, Quote],
    gross: float,
    cfg: TradingConfig,
    *,
    sync_band: float = 0.0,
) -> tuple[list[PlannedOrder], list[SkipNote]]:
    """Turn weight intents into share orders at G = `gross`. SELLs come first."""
    sells: list[PlannedOrder] = []
    buys: list[PlannedOrder] = []
    skips: list[SkipNote] = []
    for intent in _last_per_symbol(intents):
        result = _resolve_one(intent, positions, quotes, gross, cfg, sync_band)
        if isinstance(result, SkipNote):
            skips.append(result)
        elif isinstance(result, PlannedOrder):
            (sells if result.side == Side.SELL else buys).append(result)
    return sells + buys, skips


# --------------------------------------------------------------------------- liquidity guard


def estimate_after_sells(
    account: AccountSnapshot,
    sold: Sequence[tuple[str, int, float]],
    rates: Mapping[str, MarginRate],
    cfg: TradingConfig,
) -> AccountSnapshot:
    """Approximate the account after the given (symbol, qty, price) sells. NLV unchanged."""
    el = account.excess_liquidity
    af = account.available_funds
    maint = account.maint_margin
    init = account.init_margin
    gpv = account.gross_position_value
    for symbol, qty, price in sold:
        if qty <= 0 or _positive(price) is None:
            continue
        value = qty * price
        r = effective_rate(symbol, rates, cfg)
        el += value * r.maint
        af += value * r.init
        maint -= value * r.maint
        init -= value * r.init
        gpv -= value
    return replace(
        account,
        excess_liquidity=el,
        available_funds=af,
        maint_margin=max(0.0, maint),
        init_margin=max(0.0, init),
        gross_position_value=max(0.0, gpv),
    )


def conservative_account(
    account: AccountSnapshot,
    positions: Mapping[str, Position],
    quotes: Mapping[str, Quote],
    rates: Mapping[str, MarginRate],
    cfg: TradingConfig,
) -> AccountSnapshot:
    """Guard against stale broker account values.

    IBKR pushes account values only every few minutes, but positions in real time.
    Rebuild margin usage from live positions × marks × margin rates and keep the
    more conservative of the reported and the modelled value (lower 剩余流动性 /
    可用资金, higher gross exposure). NLV is taken as reported.
    """
    nlv = account.net_liquidation
    if not math.isfinite(nlv) or nlv <= 0:
        return account
    gpv = maint = init = 0.0
    for symbol, pos in positions.items():
        quote = quotes.get(symbol)
        price = (quote.mark() if quote is not None else None) or _positive(pos.avg_cost)
        if price is None or not math.isfinite(pos.qty):
            continue
        value = abs(pos.qty) * price
        r = effective_rate(symbol, rates, cfg)
        gpv += value
        maint += value * r.maint
        init += value * r.init
    return replace(
        account,
        excess_liquidity=min(account.excess_liquidity, nlv - maint),
        available_funds=min(account.available_funds, nlv - init),
        maint_margin=max(account.maint_margin, maint),
        init_margin=max(account.init_margin, init),
        gross_position_value=max(account.gross_position_value, gpv),
    )


def _liquidity_factor(
    buys: Sequence[PlannedOrder],
    account: AccountSnapshot,
    rates: Mapping[str, MarginRate],
    cfg: TradingConfig,
) -> float:
    """Largest s in [0, 1] such that all buys scaled by s keep the reserve / margin / cap."""
    nlv = account.net_liquidation
    values = (nlv, account.excess_liquidity, account.available_funds, account.gross_position_value)
    if not all(math.isfinite(v) for v in values) or nlv <= 0:
        return 0.0  # unknown or broken account: buy nothing

    need_m = need_i = need_g = 0.0
    for o in buys:
        value = o.qty * o.ref_price
        if not math.isfinite(value):
            value = math.inf  # unknown price → unbounded need → s = 0
        r = effective_rate(o.symbol, rates, cfg)
        need_m += value * r.maint
        need_i += value * r.init
        need_g += value

    room_m = account.excess_liquidity - cfg.reserve_ratio * nlv
    room_i = account.available_funds - cfg.init_margin_buffer * nlv
    room_g = cfg.max_gross_leverage * nlv - account.gross_position_value

    s = 1.0
    for room, need in ((room_m, need_m), (room_i, need_i), (room_g, need_g)):
        if need > 0:
            s = min(s, room / need)
    return max(0.0, s)


def scale_buys_for_liquidity(
    buys: Sequence[PlannedOrder],
    account: AccountSnapshot,
    rates: Mapping[str, MarginRate],
    cfg: TradingConfig,
) -> tuple[list[PlannedOrder], float]:
    """Scale ALL buys by one factor so 剩余流动性 / 可用资金 / 杠杆上限 stay respected."""
    buys = list(buys)
    if not buys:
        return [], 1.0
    s = _liquidity_factor(buys, account, rates, cfg)
    if s >= 1.0:
        return buys, 1.0
    scaled: list[PlannedOrder] = []
    for o in buys:
        qty = _floor(o.qty * s)
        if qty <= 0 or qty * o.ref_price < cfg.min_order_value_usd:
            continue
        scaled.append(replace(o, qty=qty))
    return scaled, s


# --------------------------------------------------------------------------- extended hours


def split_extended(
    orders: Iterable[PlannedOrder], buy_fraction: float, sell_fraction: float
) -> list[PlannedOrder]:
    """Part of each order that is sent in PRE / AFTER: qty = floor(qty · fraction)."""
    out: list[PlannedOrder] = []
    for o in orders:
        fraction = _clamp01(buy_fraction if o.side == Side.BUY else sell_fraction)
        qty = o.qty if fraction >= 1.0 else _floor(o.qty * fraction)
        if qty <= 0:
            continue
        out.append(o if qty == o.qty else replace(o, qty=qty))
    return out


def extended_limit_price(
    side: Side, quote: Quote | None, signal_price: float | None, buffer_pct: float
) -> float | None:
    """Marketable limit for PRE / AFTER: BUY ref·(1+b%) up to tick, SELL ref·(1−b%) down."""
    fallback = _positive(signal_price)
    if side == Side.BUY:
        ref = (quote.buy_ref() if quote is not None else None) or fallback
        factor = 1 + buffer_pct / 100
    else:
        ref = (quote.sell_ref() if quote is not None else None) or fallback
        factor = 1 - buffer_pct / 100
    if ref is None:
        return None
    raw = ref * factor
    if not math.isfinite(raw) or raw <= 0:
        return None
    price = round_to_tick(raw, side)
    return price if price > 0 else None


# --------------------------------------------------------------------------- stop loss


def stop_price_for(avg_cost: float, stop_loss_pct: float) -> float:
    """Stop trigger price, nearest tick: 422.6 at 10% → 380.34."""
    return round_to_tick(avg_cost * (1 - stop_loss_pct / 100))


def desired_stops(
    positions: Mapping[str, Position],
    working_orders: Sequence[OrderInfo],
    stop_loss_pct: float,
    ignore: Iterable[str],
) -> dict[str, DesiredStop]:
    """One stop per long position, sized to what is not already being sold by our orders."""
    skip = _norm_ignore(ignore)
    out: dict[str, DesiredStop] = {}
    for sym, pos in positions.items():
        if sym.upper() in skip:
            continue
        held = _whole_shares(pos.qty)
        if held <= 0:
            continue
        working_sell = sum(
            o.remaining
            for o in working_orders
            if o.symbol == sym and o.side == Side.SELL and not o.is_stop and not o.is_done
        )
        qty = held - working_sell
        if qty <= 0 or _positive(pos.avg_cost) is None:
            continue
        stop = stop_price_for(pos.avg_cost, stop_loss_pct)
        if stop <= 0:
            continue
        out[sym] = DesiredStop(symbol=sym, qty=qty, stop_price=stop)
    return out


def _stop_matches(order: OrderInfo, want: DesiredStop) -> bool:
    return (
        order.remaining == want.qty
        and order.stop_price is not None
        and abs(order.stop_price - want.stop_price) < STOP_PRICE_TOLERANCE
    )


def diff_stops(desired: Mapping[str, DesiredStop], existing: Sequence[OrderInfo]) -> StopActions:
    """Plan create / modify / cancel so each desired symbol ends up with exactly one stop."""
    actions = StopActions()
    wanted = {s: d for s, d in desired.items() if d.qty > 0 and d.stop_price > 0}

    by_symbol: dict[str, list[OrderInfo]] = {}
    for o in existing:
        if not o.is_stop or o.is_done:
            continue
        if o.side != Side.SELL:
            actions.cancel.append(o.order_id)  # a BUY stop is never ours to keep
            continue
        by_symbol.setdefault(o.symbol, []).append(o)

    for sym, orders in by_symbol.items():
        want = wanted.get(sym)
        if want is None:
            actions.cancel.extend(o.order_id for o in orders)
            continue
        keep = next((o for o in orders if _stop_matches(o, want)), None)
        if keep is None:
            # Modify one that has not started filling; a partly filled stop is replaced.
            keep = next((o for o in orders if o.filled_qty == 0), None)
            if keep is not None:
                actions.modify.append((keep.order_id, want))
            else:
                actions.create.append(want)
        actions.cancel.extend(o.order_id for o in orders if o is not keep)

    for sym, want in wanted.items():
        if sym not in by_symbol:
            actions.create.append(want)
    return actions


# --------------------------------------------------------------------------- deleverage


def deleverage_orders(
    account: AccountSnapshot,
    positions: Mapping[str, Position],
    quotes: Mapping[str, Quote],
    rates: Mapping[str, MarginRate],
    cfg: TradingConfig,
    risk: RiskConfig,
    ignore: Iterable[str],
) -> list[PlannedOrder]:
    """Sell the same fraction of every managed long to bring 剩余流动性 back to the reserve."""
    nlv = account.net_liquidation
    if not risk.deleverage_enabled or not math.isfinite(nlv) or nlv <= 0:
        return []
    if account.el_ratio >= risk.deleverage_trigger_ratio:
        return []
    needed = cfg.reserve_ratio * nlv - account.excess_liquidity
    if not math.isfinite(needed) or needed <= 0:
        return []

    skip = _norm_ignore(ignore)
    managed: list[tuple[str, int, float]] = []
    denom = 0.0
    for sym, pos in positions.items():
        qty = _whole_shares(pos.qty)
        if qty <= 0 or sym.upper() in skip:
            continue
        quote = quotes.get(sym)
        price = (quote.mark() if quote is not None else None) or _positive(pos.avg_cost) or 0.0
        managed.append((sym, qty, price))
        denom += qty * price * effective_rate(sym, rates, cfg).maint
    if denom <= 0:
        return []

    s = min(1.0, needed / denom)
    orders: list[PlannedOrder] = []
    for sym, qty, price in managed:
        q = min(qty, _ceil(qty * s))
        if q <= 0:
            continue
        orders.append(
            PlannedOrder(
                symbol=sym,
                side=Side.SELL,
                qty=q,
                ref_price=price,
                current_qty=qty,
                target_qty=qty - q,
                note=NOTE_DELEVERAGE,
            )
        )
    return orders
