"""Deterministic in-memory test doubles for the engine tests.

``FakeBroker`` simulates a USD margin account: cash (negative = margin loan),
positions, quotes, margin rates and orders. Market orders fill immediately at the
ask (BUY) / bid (SELL); limit orders only when marketable and ``auto_fill_limits``
is on; stop orders stay working until ``trigger_stop``. Nothing sleeps except the
(capped) base ``wait_for_done`` polling.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from copybot.broker.base import Broker, BrokerError
from copybot.models import (
    BOT_TAG_PREFIX,
    AccountSnapshot,
    Fill,
    MarginRate,
    OrderInfo,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
)

DEFAULT_TIME = datetime(2026, 10, 2, 14, 0, tzinfo=UTC)
STP = OrderType.STOP


class FakeBroker(Broker):
    """In-memory broker. Inspect ``placed``, ``events``, ``orders`` and ``pos`` in tests."""

    def __init__(self, cash: float = 100_000.0, clock: Callable[[], datetime] | None = None) -> None:
        super().__init__()
        self.cash = cash
        self.pos: dict[str, list[float]] = {}  # symbol -> [qty, avg_cost]
        self.quote_map: dict[str, Quote] = {}
        self.rates: dict[str, MarginRate] = {}  # what margin_rate() reports (whatIf)
        self.account_rate = MarginRate(0.5, 0.3, "fake")  # account math for symbols without a rate
        self.connected = True
        self.orders: dict[str, OrderInfo] = {}
        self.placed: list[OrderRequest] = []
        self.events: list[tuple[str, str]] = []  # ("place"|"cancel"|"modify", "<id> <symbol> ...")
        self.fills: list[Fill] = []
        self.hold_market = False  # True: MKT orders stay PENDING
        self.auto_fill_limits = False  # True: marketable LMT orders fill at once
        self.stuck_cancel: set[str] = set()  # order ids whose cancel is ignored
        self.fill_stop_on_cancel: set[str] = set()  # symbols whose stop fills instead of cancelling
        self.margin_calls: list[tuple[str, int, float]] = []
        self.max_wait = 0.05  # cap for wait_for_done (keeps tests fast)
        self._clock = clock or (lambda: DEFAULT_TIME)
        self._next_id = 1
        self._next_exec = 1

    # ---------------------------------------------------------------- test helpers
    def set_position(self, symbol: str, qty: float, avg_cost: float) -> None:
        if qty == 0:
            self.pos.pop(symbol, None)
        else:
            self.pos[symbol] = [float(qty), float(avg_cost)]

    def set_quote(
        self, symbol: str, bid: float | None = None, ask: float | None = None, last: float | None = None
    ) -> None:
        self.quote_map[symbol] = Quote(symbol, bid=bid, ask=ask, last=last)

    def qty(self, symbol: str) -> float:
        return self.pos.get(symbol, [0.0, 0.0])[0]

    def working(self, symbol: str | None = None, order_type: OrderType | None = None) -> list[OrderInfo]:
        return [
            replace(o)
            for o in self.orders.values()
            if not o.is_done
            and (symbol is None or o.symbol == symbol)
            and (order_type is None or o.order_type == order_type)
        ]

    async def set_connected(self, connected: bool, detail: str = "") -> None:
        self.connected = connected
        await self._emit_connection(connected, detail)

    async def trigger_stop(self, symbol: str) -> None:
        """Fill the working stop of `symbol` at its stop price."""
        stops = [o for o in self.orders.values() if not o.is_done and o.symbol == symbol and o.order_type == STP]
        assert stops, f"no working stop for {symbol}"
        for o in stops:
            await self._fill(o, o.remaining, float(o.stop_price or 0.0))

    async def fill_order(self, order_id: str, qty: int | None = None, price: float | None = None) -> None:
        """Manually (partially) fill a working order."""
        o = self.orders[order_id]
        assert not o.is_done, f"order {order_id} is done"
        px = price if price is not None else (o.limit_price or o.stop_price or self._market_price(o.symbol, o.side))
        await self._fill(o, o.remaining if qty is None else qty, float(px))

    # ---------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    def _require(self) -> None:
        if not self.connected:
            raise BrokerError("IBKR 未连接 (fake)")

    # ---------------------------------------------------------------- reads
    def _mark(self, symbol: str, avg_cost: float) -> float:
        q = self.quote_map.get(symbol)
        return (q.mark() if q is not None else None) or avg_cost

    def _account_rate(self, symbol: str) -> MarginRate:
        return self.rates.get(symbol) or self.account_rate

    async def account(self) -> AccountSnapshot:
        self._require()
        nlv, maint, init, gpv = self.cash, 0.0, 0.0, 0.0
        for sym, (qty, avg) in self.pos.items():
            value = qty * self._mark(sym, avg)
            rate = self._account_rate(sym)
            nlv += value
            maint += abs(value) * rate.maint
            init += abs(value) * rate.init
            gpv += abs(value)
        return AccountSnapshot(
            net_liquidation=nlv,
            excess_liquidity=nlv - maint,
            available_funds=nlv - init,
            maint_margin=maint,
            init_margin=init,
            gross_position_value=gpv,
            ts=self._clock(),
        )

    async def positions(self) -> dict[str, Position]:
        self._require()
        return {s: Position(s, q, a) for s, (q, a) in self.pos.items() if q != 0}

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        self._require()
        return {s: self.quote_map.get(s, Quote(s)) for s in symbols}

    async def margin_rate(self, symbol: str, qty: int, price: float) -> MarginRate | None:
        self.margin_calls.append((symbol, qty, price))
        if not self.connected:
            return None
        return self.rates.get(symbol)

    async def open_orders(self) -> list[OrderInfo]:
        self._require()
        return [replace(o) for o in self.orders.values() if not o.is_done and o.tag.startswith(BOT_TAG_PREFIX)]

    async def get_order(self, order_id: str) -> OrderInfo | None:
        self._require()
        o = self.orders.get(order_id)
        return replace(o) if o is not None else None

    async def wait_for_done(self, order_id: str, timeout: float, poll: float = 0.25) -> OrderInfo | None:
        return await super().wait_for_done(order_id, min(timeout, self.max_wait), poll=0.01)

    # ---------------------------------------------------------------- writes
    async def place_order(self, req: OrderRequest) -> OrderInfo:
        self._require()
        if not isinstance(req.qty, int) or req.qty <= 0:
            raise BrokerError(f"bad qty {req.qty!r}")
        if not req.tag.startswith(BOT_TAG_PREFIX):
            raise BrokerError(f"bad tag {req.tag!r}")
        self.placed.append(req)
        oid = str(self._next_id)
        self._next_id += 1
        info = OrderInfo(
            order_id=oid,
            symbol=req.symbol,
            side=req.side,
            qty=req.qty,
            filled_qty=0,
            avg_fill_price=None,
            order_type=req.order_type,
            status=OrderStatus.PENDING,
            limit_price=req.limit_price,
            stop_price=req.stop_price,
            outside_rth=req.outside_rth,
            tif=req.tif,
            tag=req.tag,
        )
        self.orders[oid] = info
        self.events.append(("place", f"{oid} {req.symbol} {req.side.value} {req.order_type.value} {req.qty}"))
        if req.order_type == OrderType.MARKET and not self.hold_market:
            await self._fill(info, req.qty, self._market_price(req.symbol, req.side))
        elif req.order_type == OrderType.LIMIT and self.auto_fill_limits:
            px = self._market_price(req.symbol, req.side)
            limit = req.limit_price or 0.0
            if (req.side == Side.BUY and limit >= px) or (req.side == Side.SELL and limit <= px):
                await self._fill(info, req.qty, px)
        return replace(info)

    async def modify_order(
        self,
        order_id: str,
        *,
        qty: int | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> OrderInfo:
        self._require()
        o = self.orders.get(order_id)
        if o is None or o.is_done:
            raise BrokerError(f"cannot modify {order_id}")
        if qty is not None:
            o.qty = qty
        if limit_price is not None:
            o.limit_price = limit_price
        if stop_price is not None:
            o.stop_price = stop_price
        self.events.append(("modify", f"{order_id} {o.symbol} qty={qty} lmt={limit_price} stp={stop_price}"))
        return replace(o)

    async def cancel_order(self, order_id: str) -> None:
        self._require()
        o = self.orders.get(order_id)
        self.events.append(("cancel", f"{order_id} {o.symbol if o else '?'}"))
        if o is None or o.is_done or order_id in self.stuck_cancel:
            return
        if o.order_type == OrderType.STOP and o.symbol in self.fill_stop_on_cancel:
            await self._fill(o, o.remaining, float(o.stop_price or 0.0))  # the stop "won the race"
            return
        o.status = OrderStatus.CANCELLED

    # ---------------------------------------------------------------- internals
    def _market_price(self, symbol: str, side: Side) -> float:
        q = self.quote_map.get(symbol, Quote(symbol))
        px = (q.ask if side == Side.BUY else q.bid) or q.mark()
        if px is None:
            px = self.pos.get(symbol, [0.0, 0.0])[1]
        assert px, f"no price for {symbol}"
        return float(px)

    async def _fill(self, o: OrderInfo, qty: int, price: float) -> None:
        qty = min(qty, o.remaining)
        assert qty > 0
        signed = qty if o.side == Side.BUY else -qty
        self.cash -= signed * price
        old_qty, old_avg = self.pos.get(o.symbol, [0.0, 0.0])
        new_qty = old_qty + signed
        if new_qty == 0:
            self.pos.pop(o.symbol, None)
        elif signed > 0 and old_qty >= 0:
            self.pos[o.symbol] = [new_qty, (old_qty * old_avg + qty * price) / new_qty]
        else:
            self.pos[o.symbol] = [new_qty, old_avg if old_qty * new_qty > 0 else price]
        prev = o.filled_qty
        o.filled_qty += qty
        o.avg_fill_price = ((o.avg_fill_price or 0.0) * prev + price * qty) / o.filled_qty
        if o.filled_qty >= o.qty:
            o.status = OrderStatus.FILLED
        fill = Fill(
            order_id=o.order_id,
            exec_id=f"e{self._next_exec}",
            symbol=o.symbol,
            side=o.side,
            qty=qty,
            price=price,
            tag=o.tag,
            time=self._clock(),
        )
        self._next_exec += 1
        self.fills.append(fill)
        await self._emit_fill(fill)
