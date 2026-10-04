"""Dry-run wrapper (DESIGN.md §3.6): real reads, simulated writes.

* Reads (account, positions, quotes, margin, open orders) come from the wrapped
  broker, so sizing uses the real account.
* MKT orders are reported FILLED immediately (``avg_fill_price`` None); LMT / STP
  orders become virtual working orders that can be modified / cancelled and are
  never filled. Virtual ids start with ``dry-``.
* Real orders are never touched: writes on non-``dry-`` ids are logged and ignored.
* Virtual orders never emit fills; real fills of the wrapped broker (e.g. a stop
  placed before switching to dry-run) are still forwarded to our handlers.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from ..models import (
    BOT_TAG_PREFIX,
    AccountSnapshot,
    MarginRate,
    OrderInfo,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
)
from .base import Broker, BrokerError

log = logging.getLogger(__name__)

DRY_PREFIX = "dry-"


def _positive(x: float | None) -> bool:
    return x is not None and x == x and 0 < x < float("inf")


def _check_request(req: OrderRequest) -> None:
    """Same basic validation the real adapter does, so dry-run catches the same bugs."""
    if not isinstance(req.qty, int) or isinstance(req.qty, bool) or req.qty <= 0:
        raise BrokerError(f"下单数量无效 {req.symbol}: {req.qty!r} (必须是正整数)")
    if not str(req.tag).startswith(BOT_TAG_PREFIX):
        raise BrokerError(f"订单标签必须以 {BOT_TAG_PREFIX} 开头: {req.tag!r}")
    otype = OrderType(req.order_type)
    if otype is OrderType.LIMIT and not _positive(req.limit_price):
        raise BrokerError(f"限价单缺少有效限价 {req.symbol}: {req.limit_price!r}")
    if otype is OrderType.STOP and not _positive(req.stop_price):
        raise BrokerError(f"止损单缺少有效触发价 {req.symbol}: {req.stop_price!r}")


class DryRunBroker(Broker):
    """Wraps a real broker; never sends an order to it."""

    def __init__(self, inner: Broker) -> None:
        super().__init__()
        self.inner = inner
        self._seq = 0
        self._orders: dict[str, OrderInfo] = {}  # every virtual order, done or not
        # Forward the inner broker's events to our own handlers.
        inner.add_fill_handler(self._emit_fill)
        inner.add_connection_handler(self._emit_connection)

    # ---------------------------------------------------------------- lifecycle (delegated)
    async def connect(self) -> None:
        await self.inner.connect()

    async def disconnect(self) -> None:
        await self.inner.disconnect()

    def is_connected(self) -> bool:
        return self.inner.is_connected()

    async def run_forever(self) -> None:
        await self.inner.run_forever()

    # ---------------------------------------------------------------- reads (delegated)
    async def account(self) -> AccountSnapshot:
        return await self.inner.account()

    async def positions(self) -> dict[str, Position]:
        return await self.inner.positions()

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return await self.inner.quotes(symbols)

    async def margin_rate(self, symbol: str, qty: int, price: float) -> MarginRate | None:
        return await self.inner.margin_rate(symbol, qty, price)

    async def open_orders(self) -> list[OrderInfo]:
        real = await self.inner.open_orders()
        return list(real) + [replace(o) for o in self._orders.values() if not o.is_done]

    async def get_order(self, order_id: str) -> OrderInfo | None:
        virtual = self._orders.get(order_id)
        if virtual is not None:
            return replace(virtual)
        if order_id.startswith(DRY_PREFIX):
            return None
        return await self.inner.get_order(order_id)

    # ---------------------------------------------------------------- writes (simulated)
    async def place_order(self, req: OrderRequest) -> OrderInfo:
        _check_request(req)
        self._seq += 1
        otype = OrderType(req.order_type)
        is_market = otype is OrderType.MARKET
        info = OrderInfo(
            order_id=f"{DRY_PREFIX}{self._seq}",
            symbol=req.symbol,
            side=Side(req.side),
            qty=req.qty,
            filled_qty=req.qty if is_market else 0,
            avg_fill_price=None,
            order_type=otype,
            status=OrderStatus.FILLED if is_market else OrderStatus.PENDING,
            limit_price=req.limit_price if otype is OrderType.LIMIT else None,
            stop_price=req.stop_price if otype is OrderType.STOP else None,
            outside_rth=req.outside_rth,
            tif=req.tif,
            tag=req.tag,
            message="dry-run",
        )
        self._orders[info.order_id] = info
        log.info(
            "DRY-RUN place %s %s %s x%d lmt=%s stp=%s outsideRth=%s tif=%s tag=%s -> %s %s",
            otype.value, info.side.value, req.symbol, req.qty, req.limit_price, req.stop_price,
            req.outside_rth, req.tif, req.tag, info.order_id, info.status.value,
        )
        return replace(info)

    async def modify_order(
        self,
        order_id: str,
        *,
        qty: int | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> OrderInfo:
        info = self._orders.get(order_id)
        if info is None:
            if order_id.startswith(DRY_PREFIX):
                raise BrokerError(f"找不到可修改的订单 {order_id}")
            return await self._ignore_real_modify(order_id)
        if info.is_done:
            raise BrokerError(f"订单 {order_id} 已结束 ({info.status.value}), 不能修改")
        if qty is not None:
            if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
                raise BrokerError(f"改单数量无效: {qty!r}")
            info.qty = qty
        if limit_price is not None:
            if info.order_type is not OrderType.LIMIT or not _positive(limit_price):
                raise BrokerError(f"无法对 {info.order_type.value} 订单设置限价 {limit_price!r}")
            info.limit_price = limit_price
        if stop_price is not None:
            if info.order_type is not OrderType.STOP or not _positive(stop_price):
                raise BrokerError(f"无法对 {info.order_type.value} 订单设置止损价 {stop_price!r}")
            info.stop_price = stop_price
        log.info("DRY-RUN modify %s qty=%s lmt=%s stp=%s", order_id, qty, limit_price, stop_price)
        return replace(info)

    async def cancel_order(self, order_id: str) -> None:
        info = self._orders.get(order_id)
        if info is None:
            if order_id.startswith(DRY_PREFIX):
                log.info("DRY-RUN cancel: order %s not found, ignored", order_id)
            else:
                log.info("DRY-RUN ignore cancel of real order %s", order_id)
            return
        if not info.is_done:
            info.status = OrderStatus.CANCELLED
            log.info("DRY-RUN cancel %s", order_id)

    async def _ignore_real_modify(self, order_id: str) -> OrderInfo:
        """Real orders are never modified in dry-run; return the real order unchanged."""
        log.info("DRY-RUN ignore modify of real order %s", order_id)
        current = await self.inner.get_order(order_id)
        if current is None:
            raise BrokerError(f"找不到订单 {order_id}")
        return current
