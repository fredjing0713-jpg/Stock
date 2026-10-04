"""Tests for copybot.broker.dryrun.DryRunBroker with a tiny stub inner broker."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from copybot.broker.base import Broker, BrokerError
from copybot.broker.dryrun import DryRunBroker
from copybot.models import (
    STOP_TAG,
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

T0 = datetime(2026, 10, 2, 13, 50, tzinfo=timezone.utc)


def real_stop(order_id: str = "55") -> OrderInfo:
    return OrderInfo(
        order_id=order_id, symbol="WDC", side=Side.SELL, qty=10, filled_qty=0, avg_fill_price=None,
        order_type=OrderType.STOP, status=OrderStatus.PENDING, stop_price=380.0, tif="GTC", tag=STOP_TAG,
    )


class StubBroker(Broker):
    """Inner broker: canned reads; any write is a test failure."""

    def __init__(self) -> None:
        super().__init__()
        self.real_orders = {"55": real_stop("55")}
        self.writes: list[str] = []
        self.connected = True
        self.connect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    async def account(self) -> AccountSnapshot:
        return AccountSnapshot(100000, 40000, 30000, 60000, 70000, 180000, T0)

    async def positions(self) -> dict[str, Position]:
        return {"WDC": Position("WDC", 10, 420.0)}

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {s: Quote(s, bid=1.0, ask=1.1) for s in symbols}

    async def margin_rate(self, symbol: str, qty: int, price: float) -> MarginRate | None:
        return MarginRate(0.5, 0.25, "whatif")

    async def open_orders(self) -> list[OrderInfo]:
        return [o for o in self.real_orders.values() if not o.is_done]

    async def get_order(self, order_id: str) -> OrderInfo | None:
        return self.real_orders.get(order_id)

    async def place_order(self, req: OrderRequest) -> OrderInfo:
        self.writes.append("place")
        raise AssertionError("dry-run must not place real orders")

    async def modify_order(self, order_id, *, qty=None, limit_price=None, stop_price=None) -> OrderInfo:
        self.writes.append("modify")
        raise AssertionError("dry-run must not modify real orders")

    async def cancel_order(self, order_id: str) -> None:
        self.writes.append("cancel")
        raise AssertionError("dry-run must not cancel real orders")


@pytest.fixture
def pair():
    inner = StubBroker()
    return DryRunBroker(inner), inner


async def test_reads_delegate(pair):
    dry, inner = pair
    assert (await dry.account()).net_liquidation == 100000
    assert (await dry.positions())["WDC"].qty == 10
    assert (await dry.quotes(["WDC"]))["WDC"].ask == 1.1
    assert (await dry.margin_rate("WDC", 1, 1.0)).source == "whatif"
    assert dry.is_connected()
    await dry.connect()
    assert inner.connect_calls == 1
    await dry.disconnect()
    assert not dry.is_connected()


async def test_market_order_filled_immediately(pair):
    dry, inner = pair
    info = await dry.place_order(OrderRequest("WDC", Side.BUY, 12, OrderType.MARKET, tag="copybot:open"))
    assert info.order_id.startswith("dry-")
    assert info.status is OrderStatus.FILLED and info.filled_qty == 12 and info.avg_fill_price is None
    assert info.is_done
    done = await dry.wait_for_done(info.order_id, timeout=0.1)
    assert done is not None and done.status is OrderStatus.FILLED
    assert [o.order_id for o in await dry.open_orders()] == ["55"]  # MKT never working
    assert inner.writes == []


async def test_limit_order_stays_working_and_can_be_modified_and_cancelled(pair):
    dry, inner = pair
    lmt = await dry.place_order(
        OrderRequest("WDC", Side.BUY, 6, OrderType.LIMIT, limit_price=424.73, outside_rth=True, tag="copybot:pre")
    )
    assert lmt.status is OrderStatus.PENDING and lmt.filled_qty == 0 and lmt.limit_price == 424.73
    ids = [o.order_id for o in await dry.open_orders()]
    assert ids == ["55", lmt.order_id]

    m = await dry.modify_order(lmt.order_id, qty=8, limit_price=425.0)
    assert (m.qty, m.limit_price, m.status) == (8, 425.0, OrderStatus.PENDING)
    assert (await dry.get_order(lmt.order_id)).qty == 8
    with pytest.raises(BrokerError):
        await dry.modify_order(lmt.order_id, stop_price=400.0)  # not a stop order

    await dry.cancel_order(lmt.order_id)
    got = await dry.get_order(lmt.order_id)
    assert got is not None and got.status is OrderStatus.CANCELLED
    assert [o.order_id for o in await dry.open_orders()] == ["55"]
    with pytest.raises(BrokerError):
        await dry.modify_order(lmt.order_id, qty=1)  # done
    await dry.cancel_order(lmt.order_id)  # cancelling twice is harmless
    assert inner.writes == []


async def test_virtual_stop_order(pair):
    dry, _ = pair
    stp = await dry.place_order(
        OrderRequest("WDC", Side.SELL, 10, OrderType.STOP, stop_price=380.0, tif="GTC", tag=STOP_TAG)
    )
    assert stp.is_stop and stp.stop_price == 380.0 and stp.limit_price is None and stp.tif == "GTC"
    m = await dry.modify_order(stp.order_id, stop_price=385.5, qty=9)
    assert (m.stop_price, m.qty) == (385.5, 9)


async def test_returned_infos_are_copies(pair):
    dry, _ = pair
    lmt = await dry.place_order(OrderRequest("WDC", Side.BUY, 6, OrderType.LIMIT, limit_price=10.0))
    lmt.qty = 999  # caller mutating its snapshot must not change our state
    assert (await dry.get_order(lmt.order_id)).qty == 6


async def test_writes_on_real_orders_are_ignored(pair):
    dry, inner = pair
    await dry.cancel_order("55")
    await dry.cancel_order("4242")  # unknown real id: still a no-op
    info = await dry.modify_order("55", qty=3, stop_price=300.0)
    assert info.qty == 10 and info.stop_price == 380.0  # unchanged real order
    with pytest.raises(BrokerError):
        await dry.modify_order("4242", qty=3)
    assert inner.writes == []
    assert (await dry.get_order("55")).status is OrderStatus.PENDING
    assert await dry.get_order("dry-999") is None
    await dry.cancel_order("dry-999")  # unknown virtual id: no-op


@pytest.mark.parametrize(
    "req",
    [
        OrderRequest("WDC", Side.BUY, 0, OrderType.MARKET),
        OrderRequest("WDC", Side.BUY, 1.5, OrderType.MARKET),  # type: ignore[arg-type]
        OrderRequest("WDC", Side.BUY, 1, OrderType.LIMIT),
        OrderRequest("WDC", Side.SELL, 1, OrderType.STOP, stop_price=-1.0),
        OrderRequest("WDC", Side.BUY, 1, OrderType.MARKET, tag="manual"),
    ],
)
async def test_invalid_requests_rejected(pair, req):
    dry, _ = pair
    with pytest.raises(BrokerError):
        await dry.place_order(req)


async def test_inner_fills_and_connection_events_forwarded(pair):
    dry, inner = pair
    fills, states = [], []

    async def on_fill(f):
        fills.append(f)

    dry.add_fill_handler(on_fill)
    dry.add_connection_handler(lambda ok, detail: states.append((ok, detail)))
    real_fill = Fill("55", "e1", "WDC", Side.SELL, 10, 379.9, STOP_TAG, T0)
    await inner._emit_fill(real_fill)
    await inner._emit_connection(False, "down")
    assert fills == [real_fill]
    assert states == [(False, "down")]


async def test_virtual_orders_never_emit_fills(pair):
    dry, _ = pair
    fills = []
    dry.add_fill_handler(fills.append)
    await dry.place_order(OrderRequest("WDC", Side.BUY, 5, OrderType.MARKET))
    await dry.place_order(OrderRequest("WDC", Side.BUY, 5, OrderType.LIMIT, limit_price=1.0))
    assert fills == []
