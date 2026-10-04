"""Tests for copybot.broker.ibkr — pure helpers with real ib_async objects, and the
broker class against a small in-memory fake of ``ib_async.IB`` (no network)."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone

import pytest
from ib_async import (
    AccountValue,
    CommissionReport,
    Contract,
    Execution,
    LimitOrder,
    MarketOrder,
    OrderState,
    Stock,
    StopOrder,
    Ticker,
    Trade,
    TradeLogEntry,
)
from ib_async import Fill as IBFill
from ib_async import OrderStatus as IBOrderStatus
from ib_async import Position as IBPosition

from copybot.broker import ibkr
from copybot.broker.base import BrokerError
from copybot.broker.ibkr import (
    IBKRBroker,
    account_from_values,
    build_ib_order,
    fill_from_ib,
    from_ib_symbol,
    order_info_from_trade,
    quote_from_ticker,
    rate_from_whatif,
    to_ib_symbol,
)
from copybot.config import IBKRConfig
from copybot.models import (
    STOP_TAG,
    MarginRate,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)

T0 = datetime(2026, 10, 2, 13, 50, tzinfo=timezone.utc)
ACC = "U1234567"
UNSET = "1.7976931348623157E308"


def ident(sym: str) -> str:
    return from_ib_symbol(sym, {})


def stock(sym: str = "WDC", con_id: int = 1001) -> Contract:
    return Stock(sym, "SMART", "USD", conId=con_id)


def make_trade(
    order,
    status: str = IBOrderStatus.Submitted,
    *,
    sym: str = "WDC",
    order_id: int = 7,
    filled: float = 0.0,
    avg: float = 0.0,
    fills: list | None = None,
    log: list | None = None,
    account: str = ACC,
) -> Trade:
    order.orderId = order_id
    order.account = account
    st = IBOrderStatus(orderId=order_id, status=status, filled=filled, avgFillPrice=avg)
    return Trade(
        contract=stock(sym),
        order=order,
        orderStatus=st,
        fills=fills or [],
        log=log or [TradeLogEntry(T0, status)],
    )


def ib_fill(shares: float, price: float, side: str = "BOT", exec_id: str = "e1", sym: str = "WDC") -> IBFill:
    ex = Execution(execId=exec_id, side=side, shares=shares, price=price, time=T0)
    return IBFill(stock(sym), ex, CommissionReport(), T0)


# --------------------------------------------------------------------------- symbols


def test_symbol_dot_to_space_round_trip():
    assert to_ib_symbol("BRK.B") == "BRK B"
    assert from_ib_symbol("BRK B") == "BRK.B"
    assert from_ib_symbol(to_ib_symbol("brk.b")) == "BRK.B"
    assert to_ib_symbol("WDC", None) == "WDC"


def test_symbol_alias_round_trip():
    aliases = {"BF.B": "BF B", "XYZ.U": "XYZU"}
    assert to_ib_symbol("XYZ.U", aliases) == "XYZU"
    assert from_ib_symbol("XYZU", aliases) == "XYZ.U"
    assert from_ib_symbol(to_ib_symbol("BF.B", aliases), aliases) == "BF.B"
    # Not aliased: plain mapping still applies.
    assert to_ib_symbol("BRK.B", aliases) == "BRK B"
    assert from_ib_symbol("AAPL", aliases) == "AAPL"


# --------------------------------------------------------------------------- account


def _values(base: str, extra: list[AccountValue] | None = None) -> list[AccountValue]:
    v = [
        AccountValue(ACC, "NetLiquidation", "100000.00", base, ""),
        AccountValue(ACC, "NetLiquidation-S", "99999.00", base, ""),  # segmented: ignored
        AccountValue(ACC, "ExcessLiquidity", "40000", base, ""),
        AccountValue(ACC, "AvailableFunds", "30,000.5", base, ""),
        AccountValue(ACC, "FullMaintMarginReq", "60000", base, ""),
        AccountValue(ACC, "FullInitMarginReq", "70000", base, ""),
        AccountValue(ACC, "GrossPositionValue", "180000", base, ""),
        AccountValue(ACC, "TotalCashBalance", "5", "BASE", ""),
        AccountValue(ACC, "ExchangeRate", "1.00", "BASE", ""),
        AccountValue(ACC, "AccountType", "INDIVIDUAL", "", ""),
        AccountValue("U999", "NetLiquidation", "1", base, ""),  # other account
        AccountValue(ACC, "NetLiquidation", "5", base, "MODEL1"),  # model portfolio
    ]
    return v + (extra or [])


def test_account_usd_base():
    snap = account_from_values(_values("USD"), ACC, T0)
    assert snap.net_liquidation == pytest.approx(100000.0)
    assert snap.excess_liquidity == pytest.approx(40000.0)
    assert snap.available_funds == pytest.approx(30000.5)
    assert snap.maint_margin == pytest.approx(60000.0)
    assert snap.init_margin == pytest.approx(70000.0)
    assert snap.gross_position_value == pytest.approx(180000.0)
    assert snap.ts == T0
    assert snap.el_ratio == pytest.approx(0.4)


def test_account_hkd_base_converted_with_exchange_rate():
    fx = [
        AccountValue(ACC, "ExchangeRate", "7.8", "USD", ""),
        AccountValue(ACC, "ExchangeRate", "1.0", "HKD", ""),
    ]
    snap = account_from_values(_values("HKD", fx), ACC, T0)
    assert snap.net_liquidation == pytest.approx(100000.0 / 7.8)
    assert snap.excess_liquidity == pytest.approx(40000.0 / 7.8)
    assert snap.gross_position_value == pytest.approx(180000.0 / 7.8)
    assert snap.leverage == pytest.approx(1.8)


def test_account_non_usd_without_rate_raises():
    with pytest.raises(BrokerError):
        account_from_values(_values("HKD"), ACC, T0)


def test_account_missing_nlv_raises():
    values = [v for v in _values("USD") if v.tag != "NetLiquidation" or v.account != ACC or v.modelCode]
    with pytest.raises(BrokerError):
        account_from_values(values, ACC, T0)
    with pytest.raises(BrokerError):
        account_from_values([], ACC, T0)


def test_account_missing_other_tags_are_zero():
    values = [AccountValue(ACC, "NetLiquidation", "50000", "USD", "")]
    snap = account_from_values(values, ACC, T0)
    assert snap.net_liquidation == 50000.0
    assert snap.excess_liquidity == 0.0
    assert snap.available_funds == 0.0
    assert snap.gross_position_value == 0.0


# --------------------------------------------------------------------------- quotes / margin


def test_quote_from_ticker_cleans_values():
    t = Ticker(contract=stock())
    t.bid, t.ask, t.last, t.close = 99.5, -1.0, float("nan"), 0.0
    q = quote_from_ticker("WDC", t)
    assert (q.bid, q.ask, q.last, q.close) == (99.5, None, None, None)
    assert quote_from_ticker("X", None).bid is None


def test_rate_from_whatif_normal():
    st = OrderState(initMarginChange="5,000.00", maintMarginChange="2500")
    r = rate_from_whatif(st, 100, 100.0)
    assert r == MarginRate(init=0.5, maint=0.25, source="whatif")


def test_rate_from_whatif_base_currency_conversion():
    st = OrderState(initMarginChange="39000", maintMarginChange="19500")  # HKD
    r = rate_from_whatif(st, 100, 100.0, base_per_usd=7.8)
    assert r is not None
    assert r.init == pytest.approx(0.5) and r.maint == pytest.approx(0.25)


@pytest.mark.parametrize(
    "init,maint",
    [
        (UNSET, "2500"),
        ("5000", UNSET),
        ("", "2500"),
        ("-5000", "2500"),
        ("0", "0"),
        ("nan", "2500"),
        ("inf", "2500"),
        ("16000", "2500"),  # 1.6 > 1.5 → implausible
        ("abc", "2500"),
    ],
)
def test_rate_from_whatif_invalid(init, maint):
    assert rate_from_whatif(OrderState(initMarginChange=init, maintMarginChange=maint), 100, 100.0) is None


def test_rate_from_whatif_bad_inputs():
    good = OrderState(initMarginChange="5000", maintMarginChange="2500")
    assert rate_from_whatif([], 100, 100.0) is None  # failed request resolves to []
    assert rate_from_whatif(None, 100, 100.0) is None
    assert rate_from_whatif(good, 0, 100.0) is None
    assert rate_from_whatif(good, 100, 0.0) is None
    assert rate_from_whatif(good, 100, 100.0, base_per_usd=0) is None


# --------------------------------------------------------------------------- orders


def test_build_market_order():
    req = OrderRequest("WDC", Side.BUY, 10, OrderType.MARKET, tag="copybot:open")
    o = build_ib_order(req, ACC)
    assert isinstance(o, MarketOrder)
    assert (o.action, o.totalQuantity, o.orderType) == ("BUY", 10, "MKT")
    assert (o.tif, o.outsideRth, o.orderRef, o.account, o.transmit) == ("DAY", False, "copybot:open", ACC, True)


def test_build_limit_order_outside_rth():
    req = OrderRequest("WDC", Side.BUY, 5, OrderType.LIMIT, limit_price=424.73, outside_rth=True, tag="copybot:pre")
    o = build_ib_order(req, ACC)
    assert isinstance(o, LimitOrder)
    assert (o.orderType, o.lmtPrice, o.outsideRth, o.tif) == ("LMT", 424.73, True, "DAY")


def test_build_stop_order_gtc():
    req = OrderRequest("WDC", Side.SELL, 7, OrderType.STOP, stop_price=380.34, tif="GTC", tag=STOP_TAG)
    o = build_ib_order(req, ACC)
    assert isinstance(o, StopOrder)
    assert (o.action, o.orderType, o.auxPrice, o.tif, o.orderRef) == ("SELL", "STP", 380.34, "GTC", STOP_TAG)


@pytest.mark.parametrize(
    "req",
    [
        OrderRequest("WDC", Side.BUY, 0, OrderType.MARKET),
        OrderRequest("WDC", Side.BUY, -3, OrderType.MARKET),
        OrderRequest("WDC", Side.BUY, 2.5, OrderType.MARKET),  # type: ignore[arg-type]
        OrderRequest("WDC", Side.BUY, True, OrderType.MARKET),  # type: ignore[arg-type]
        OrderRequest("WDC", Side.BUY, 1, OrderType.LIMIT),
        OrderRequest("WDC", Side.BUY, 1, OrderType.LIMIT, limit_price=float("nan")),
        OrderRequest("WDC", Side.SELL, 1, OrderType.STOP, stop_price=0.0),
        OrderRequest("WDC", Side.BUY, 1, OrderType.MARKET, tag="manual"),
    ],
)
def test_build_order_rejects_invalid(req):
    with pytest.raises(BrokerError):
        build_ib_order(req, ACC)


def test_order_info_partial_fill_working_limit():
    order = LimitOrder("BUY", 10, 424.73, orderRef="copybot:pre", outsideRth=True)
    trade = make_trade(order, IBOrderStatus.Submitted, fills=[ib_fill(3, 424.0), ib_fill(1, 425.0, exec_id="e2")])
    info = order_info_from_trade(trade, ident)
    assert info.order_id == "7"
    assert (info.symbol, info.side, info.qty, info.filled_qty) == ("WDC", Side.BUY, 10, 4)
    assert info.status is OrderStatus.PENDING
    assert info.order_type is OrderType.LIMIT
    assert info.limit_price == 424.73 and info.stop_price is None
    assert info.avg_fill_price == pytest.approx((3 * 424.0 + 425.0) / 4)  # VWAP fallback
    assert info.outside_rth is True and info.tag == "copybot:pre" and info.tif == "DAY"
    assert info.remaining == 6 and not info.is_done


def test_order_info_filled_uses_order_status():
    order = MarketOrder("SELL", 10, orderRef="copybot")
    # orderStatus reports the full fill before all execDetails arrived.
    trade = make_trade(order, IBOrderStatus.Filled, filled=10, avg=421.5, fills=[ib_fill(4, 421.0, "SLD")])
    info = order_info_from_trade(trade, ident)
    assert info.status is OrderStatus.FILLED
    assert info.filled_qty == 10
    assert info.avg_fill_price == 421.5
    assert info.side is Side.SELL and info.order_type is OrderType.MARKET
    assert info.limit_price is None and info.stop_price is None


def test_order_info_cancelled_with_error_message():
    order = MarketOrder("BUY", 10, orderRef="copybot")
    log = [
        TradeLogEntry(T0, "PendingSubmit"),
        TradeLogEntry(T0, "Cancelled", "Error 201, reqId 7: Order rejected - insufficient margin", 201),
        TradeLogEntry(T0, "Cancelled", "", 0),
    ]
    info = order_info_from_trade(make_trade(order, IBOrderStatus.Cancelled, log=log), ident)
    assert info.status is OrderStatus.CANCELLED
    assert "insufficient margin" in info.message
    assert info.filled_qty == 0 and info.avg_fill_price is None


@pytest.mark.parametrize(
    "ib_status,expected",
    [
        ("ApiCancelled", OrderStatus.CANCELLED),
        ("Inactive", OrderStatus.REJECTED),
        ("PreSubmitted", OrderStatus.PENDING),
        ("PendingSubmit", OrderStatus.PENDING),
        ("PendingCancel", OrderStatus.PENDING),
        ("ValidationError", OrderStatus.PENDING),
    ],
)
def test_order_info_status_map(ib_status, expected):
    info = order_info_from_trade(make_trade(MarketOrder("BUY", 1, orderRef="copybot"), ib_status), ident)
    assert info.status is expected
    assert info.message == ""


def test_order_info_stop_and_symbol_mapping():
    order = StopOrder("SELL", 7, 380.34, orderRef=STOP_TAG, tif="GTC")
    info = order_info_from_trade(make_trade(order, sym="BRK B"), ident)
    assert info.symbol == "BRK.B"
    assert info.order_type is OrderType.STOP and info.stop_price == 380.34 and info.limit_price is None
    assert info.is_stop and info.tif == "GTC"


def test_fill_from_ib_ours_and_not_ours():
    trade = make_trade(LimitOrder("BUY", 10, 424.0, orderRef="copybot:pre"))
    f = fill_from_ib(trade, ib_fill(3, 423.9), ident, T0)
    assert f is not None
    assert (f.order_id, f.exec_id, f.symbol, f.side, f.qty, f.price, f.tag) == (
        "7", "e1", "WDC", Side.BUY, 3, 423.9, "copybot:pre",
    )
    assert f.time == T0
    sell = fill_from_ib(make_trade(StopOrder("SELL", 5, 380.0, orderRef=STOP_TAG)), ib_fill(5, 379.5, "SLD"), ident, T0)
    assert sell is not None and sell.side is Side.SELL and sell.tag == STOP_TAG
    manual = make_trade(MarketOrder("BUY", 1, orderRef=""))
    assert fill_from_ib(manual, ib_fill(1, 1.0), ident, T0) is None
    opt = make_trade(MarketOrder("BUY", 1, orderRef="copybot"))
    opt.contract = Contract(secType="OPT", symbol="WDC", conId=5)
    assert fill_from_ib(opt, ib_fill(1, 1.0), ident, T0) is None


# --------------------------------------------------------------------------- broker with a fake IB


class FakeWrapper:
    def __init__(self) -> None:
        self.tickers: dict[int, Ticker] = {}


class FakeIB:
    """Just enough of ib_async.IB for IBKRBroker, recording calls."""

    def __init__(self) -> None:
        self.connected = True
        self.wrapper = FakeWrapper()
        self.values: list[AccountValue] = _values("USD")
        self.pos: list[IBPosition] = []
        self.known: dict[str, int] = {"WDC": 1001, "ARM": 1002, "BRK B": 1003}
        self.ticker_data: dict[str, tuple[float, float, float, float]] = {}
        self.ticker_delay = 0.0
        self.whatif_result: object = OrderState(initMarginChange="5000", maintMarginChange="2500")
        self.whatif_calls = 0
        self.qualify_calls = 0
        self.placed: list[tuple[Contract, object]] = []
        self.cancelled: list[object] = []
        self._trades: list[Trade] = []
        self._next_id = 100

    def isConnected(self) -> bool:
        return self.connected

    def managedAccounts(self) -> list[str]:
        return [ACC]

    def accountValues(self, account: str = "") -> list[AccountValue]:
        return [v for v in self.values if not account or v.account == account]

    def positions(self, account: str = "") -> list[IBPosition]:
        return [p for p in self.pos if not account or p.account == account]

    async def qualifyContractsAsync(self, *contracts):
        self.qualify_calls += 1
        out = []
        for c in contracts:
            if c.symbol in self.known:
                c.conId = self.known[c.symbol]
                out.append(c)
            else:
                out.append(None)
        return out

    def _ticker(self, c: Contract) -> Ticker:
        t = self.wrapper.tickers.get(hash(c))
        if t is None:
            t = Ticker(contract=c)
            self.wrapper.tickers[hash(c)] = t
        return t

    async def reqTickersAsync(self, *contracts):
        tickers = [self._ticker(c) for c in contracts]
        for t in tickers:
            data = self.ticker_data.get(t.contract.symbol)
            if data:
                t.bid, t.ask, t.last, t.close = data
        if self.ticker_delay:
            await asyncio.sleep(self.ticker_delay)
        return tickers

    def ticker(self, contract: Contract):
        return self.wrapper.tickers.get(hash(contract))

    async def whatIfOrderAsync(self, contract, order):
        self.whatif_calls += 1
        if isinstance(self.whatif_result, Exception):
            raise self.whatif_result
        return self.whatif_result

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        for t in self._trades:
            if t.order is order:
                return t
        self._next_id += 1
        order.orderId = self._next_id
        trade = Trade(contract, order, IBOrderStatus(orderId=self._next_id, status="PendingSubmit"), [], [])
        self._trades.append(trade)
        return trade

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def trades(self) -> list[Trade]:
        return list(self._trades)

    def openTrades(self) -> list[Trade]:
        return [t for t in self._trades if not t.isDone()]

    def disconnect(self) -> None:
        self.connected = False


@pytest.fixture
def broker_and_ib():
    b = IBKRBroker(IBKRConfig(account=ACC), aliases={"XYZ.U": "XYZU"}, margin_cache_seconds=1800)
    fake = FakeIB()
    b.ib = fake  # type: ignore[assignment]
    return b, fake


async def test_methods_require_connection(broker_and_ib):
    b, fake = broker_and_ib
    fake.connected = False
    assert not b.is_connected()
    with pytest.raises(BrokerError):
        await b.account()
    with pytest.raises(BrokerError):
        await b.positions()
    with pytest.raises(BrokerError):
        await b.open_orders()
    with pytest.raises(BrokerError):
        await b.get_order("1")
    with pytest.raises(BrokerError):
        await b.place_order(OrderRequest("WDC", Side.BUY, 1, OrderType.MARKET))
    with pytest.raises(BrokerError):
        await b.quotes(["WDC"])
    assert await b.margin_rate("WDC", 10, 100.0) is None  # never raises


async def test_account_and_positions(broker_and_ib):
    b, fake = broker_and_ib
    snap = await b.account()
    assert snap.net_liquidation == 100000.0
    fake.pos = [
        IBPosition(ACC, Stock("WDC", "NASDAQ", "USD", conId=1001), 120.0, 400.25),
        IBPosition(ACC, Stock("BRK B", "NYSE", "USD", conId=1003), 3.0, 450.0),
        IBPosition(ACC, Stock("XYZU", "NYSE", "USD", conId=1009), 1.0, 10.0),
        IBPosition(ACC, Stock("ARM", "NASDAQ", "USD", conId=1002), 0.0, 0.0),  # flat
        IBPosition(ACC, Stock("700", "SEHK", "HKD", conId=2000), 100.0, 300.0),  # not USD
        IBPosition(ACC, Contract(secType="OPT", symbol="WDC", conId=3000, currency="USD"), 1.0, 500.0),
    ]
    pos = await b.positions()
    assert set(pos) == {"WDC", "BRK.B", "XYZ.U"}
    assert pos["WDC"].qty == 120.0 and pos["WDC"].avg_cost == 400.25
    assert pos["BRK.B"].qty == 3.0


async def test_quotes_mapping_and_unknown_symbols(broker_and_ib):
    b, fake = broker_and_ib
    fake.ticker_data = {"WDC": (420.0, 420.5, 420.2, 410.0), "BRK B": (float("nan"), -1.0, 450.0, 449.0)}
    q = await b.quotes(["WDC", "BRK.B", "NOPE"])
    assert (q["WDC"].bid, q["WDC"].ask, q["WDC"].last, q["WDC"].close) == (420.0, 420.5, 420.2, 410.0)
    assert (q["BRK.B"].bid, q["BRK.B"].ask, q["BRK.B"].last) == (None, None, 450.0)
    assert q["NOPE"].buy_ref() is None and q["NOPE"].symbol == "NOPE"
    # Contracts are qualified once (NOPE is retried, but WDC / BRK.B are cached).
    calls = fake.qualify_calls
    await b.quotes(["WDC", "BRK.B"])
    assert fake.qualify_calls == calls
    assert await b.quotes([]) == {}


async def test_quotes_drop_stale_ticker_fields(broker_and_ib):
    b, fake = broker_and_ib
    fake.ticker_data = {"WDC": (420.0, 420.5, 420.2, 410.0)}
    await b.quotes(["WDC"])
    fake.ticker_data = {}  # the next snapshot delivers nothing
    q = await b.quotes(["WDC"])
    assert q["WDC"].bid is None and q["WDC"].last is None


async def test_quotes_timeout_keeps_partial_data(broker_and_ib, monkeypatch):
    b, fake = broker_and_ib
    monkeypatch.setattr(ibkr, "QUOTE_TIMEOUT_SECONDS", 0.05)
    fake.ticker_delay = 1.0
    fake.ticker_data = {"WDC": (420.0, 420.5, float("nan"), 410.0)}
    q = await b.quotes(["WDC", "ARM"])
    assert q["WDC"].ask == 420.5 and q["WDC"].last is None
    assert q["ARM"].bid is None


async def test_margin_rate_cached_and_errors_return_none(broker_and_ib, monkeypatch):
    b, fake = broker_and_ib
    r = await b.margin_rate("WDC", 100, 100.0)
    assert r == MarginRate(0.5, 0.25, "whatif")
    assert await b.margin_rate("wdc", 5, 300.0) == r
    assert fake.whatif_calls == 1
    # Expired cache → asks again.
    b.margin_cache_seconds = 0
    fake.whatif_result = OrderState(initMarginChange=UNSET, maintMarginChange=UNSET)
    assert await b.margin_rate("ARM", 10, 100.0) is None
    fake.whatif_result = RuntimeError("boom")
    assert await b.margin_rate("ARM", 10, 100.0) is None
    assert await b.margin_rate("NOPE", 10, 100.0) is None
    assert await b.margin_rate("ARM", 0, 100.0) is None


async def test_margin_rate_timeout_returns_none(broker_and_ib, monkeypatch):
    b, fake = broker_and_ib
    monkeypatch.setattr(ibkr, "WHATIF_TIMEOUT_SECONDS", 0.05)

    async def never(contract, order):
        await asyncio.sleep(10)

    fake.whatIfOrderAsync = never  # type: ignore[method-assign]
    assert await b.margin_rate("WDC", 10, 100.0) is None


async def test_place_modify_cancel_flow(broker_and_ib):
    b, fake = broker_and_ib
    info = await b.place_order(
        OrderRequest("BRK.B", Side.BUY, 4, OrderType.LIMIT, limit_price=451.2, outside_rth=True, tag="copybot:pre")
    )
    assert info.status is OrderStatus.PENDING and info.symbol == "BRK.B" and info.qty == 4
    contract, order = fake.placed[-1]
    assert contract.symbol == "BRK B" and contract.conId == 1003
    assert order.account == ACC and order.lmtPrice == 451.2 and order.outsideRth

    # Modify: same order object re-sent with new fields.
    m = await b.modify_order(info.order_id, qty=6, limit_price=452.0)
    assert fake.placed[-1][1] is order
    assert order.totalQuantity == 6 and order.lmtPrice == 452.0
    assert m.qty == 6 and m.limit_price == 452.0
    with pytest.raises(BrokerError):
        await b.modify_order(info.order_id, stop_price=400.0)  # not a stop order
    with pytest.raises(BrokerError):
        await b.modify_order(info.order_id, qty=0)
    with pytest.raises(BrokerError):
        await b.modify_order("999", qty=1)

    opens = await b.open_orders()
    assert [o.order_id for o in opens] == [info.order_id]

    await b.cancel_order(info.order_id)
    assert fake.cancelled == [order]
    await b.cancel_order("999")  # unknown → no-op
    assert len(fake.cancelled) == 1

    # Done orders: cannot be modified, cancel is a no-op, not listed as open.
    trade = fake.trades()[-1]
    trade.orderStatus.status = IBOrderStatus.Cancelled
    with pytest.raises(BrokerError):
        await b.modify_order(info.order_id, qty=2)
    await b.cancel_order(info.order_id)
    assert len(fake.cancelled) == 1
    assert await b.open_orders() == []
    got = await b.get_order(info.order_id)
    assert got is not None and got.status is OrderStatus.CANCELLED
    assert await b.get_order("424242") is None


async def test_place_order_unknown_symbol_and_invalid(broker_and_ib):
    b, fake = broker_and_ib
    with pytest.raises(BrokerError):
        await b.place_order(OrderRequest("NOPE", Side.BUY, 1, OrderType.MARKET))
    with pytest.raises(BrokerError):
        await b.place_order(OrderRequest("WDC", Side.BUY, 0, OrderType.MARKET))
    assert fake.placed == []


async def test_modify_restores_fields_when_send_fails(broker_and_ib):
    b, fake = broker_and_ib
    req = OrderRequest("WDC", Side.SELL, 5, OrderType.STOP, stop_price=380.0, tif="GTC", tag=STOP_TAG)
    info = await b.place_order(req)
    order = fake.placed[-1][1]

    def boom(contract, o):
        raise ConnectionError("Not connected")

    fake.placeOrder = boom  # type: ignore[method-assign]
    with pytest.raises(BrokerError):
        await b.modify_order(info.order_id, qty=9, stop_price=370.0)
    assert order.totalQuantity == 5 and order.auxPrice == 380.0


async def test_open_orders_only_bot_orders_and_manual_untouched(broker_and_ib):
    b, fake = broker_and_ib
    manual = make_trade(LimitOrder("BUY", 1, 10.0, orderRef=""), order_id=50)
    other_acc = make_trade(LimitOrder("BUY", 1, 10.0, orderRef="copybot"), order_id=51, account="U999")
    ours = make_trade(StopOrder("SELL", 3, 90.0, orderRef=STOP_TAG), order_id=52)
    fake._trades += [manual, other_acc, ours]
    assert [o.order_id for o in await b.open_orders()] == ["52"]
    await b.cancel_order("50")
    with pytest.raises(BrokerError):
        await b.modify_order("50", qty=2)
    assert fake.cancelled == []


async def test_exec_details_emits_fill_for_bot_orders(broker_and_ib):
    b, _ = broker_and_ib
    got = []

    async def handler(f):
        got.append(f)

    b.add_fill_handler(handler)
    ours = make_trade(LimitOrder("BUY", 10, 424.0, orderRef="copybot:pre"))
    b._on_exec_details(ours, ib_fill(3, 423.9))
    b._on_exec_details(make_trade(MarketOrder("BUY", 1, orderRef="")), ib_fill(1, 1.0, exec_id="e9"))
    for _ in range(3):
        await asyncio.sleep(0)
    assert len(got) == 1
    assert (got[0].symbol, got[0].qty, got[0].price, got[0].exec_id) == ("WDC", 3, 423.9, "e1")


async def test_link_lost_and_restored_reports_state_once(broker_and_ib):
    b, _ = broker_and_ib
    states = []
    b.add_connection_handler(lambda ok, detail: states.append(ok))
    b._conn_state = True
    b._on_error(-1, 2104, "Market data farm connection is OK", None)
    b._on_error(-1, 1100, "Connectivity between IB and TWS has been lost", None)
    b._on_error(-1, 1100, "Connectivity between IB and TWS has been lost", None)
    await asyncio.sleep(0)
    assert not b.is_connected()
    with pytest.raises(BrokerError):
        await b.open_orders()
    b._on_error(-1, 1102, "Connectivity restored - data maintained", None)
    await asyncio.sleep(0)
    assert b.is_connected()
    assert states == [False, True]


async def test_run_forever_reports_outage_once_and_reconnects(broker_and_ib, monkeypatch):
    b, fake = broker_and_ib
    monkeypatch.setattr(ibkr, "RECONNECT_INTERVAL_SECONDS", 0.01)
    fake.connected = False
    attempts = {"n": 0}

    async def fake_connect():
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise BrokerError("refused")
        fake.connected = True
        await b._set_conn_state(True, "ok")

    monkeypatch.setattr(b, "connect", fake_connect)
    states = []
    b.add_connection_handler(lambda ok, detail: states.append((ok, detail)))
    task = asyncio.create_task(b.run_forever())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if fake.connected:
            break
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts["n"] == 4
    assert [s[0] for s in states] == [False, True]
    assert "refused" in states[0][1]


async def test_run_forever_survives_unexpected_errors(broker_and_ib, monkeypatch):
    b, fake = broker_and_ib
    monkeypatch.setattr(ibkr, "RECONNECT_INTERVAL_SECONDS", 0.01)
    fake.connected = False
    calls = {"n": 0}

    async def bad_connect():
        calls["n"] += 1
        raise ValueError("unexpected")

    monkeypatch.setattr(b, "connect", bad_connect)
    task = asyncio.create_task(b.run_forever())
    for _ in range(200):  # wait (up to 2 s) for at least two attempts; robust under load
        if calls["n"] >= 2:
            break
        await asyncio.sleep(0.01)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls["n"] >= 2


async def test_disconnect_stops_reconnecting(broker_and_ib):
    b, fake = broker_and_ib
    states = []
    b.add_connection_handler(lambda ok, detail: states.append(ok))
    b._conn_state = True
    await b.disconnect()
    assert not fake.connected and b._stopped
    assert states == [False]


async def test_connect_failure_is_broker_error(monkeypatch):
    b = IBKRBroker(IBKRConfig(account=ACC))

    async def refuse(*args, **kwargs):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(b.ib, "connectAsync", refuse)
    with pytest.raises(BrokerError):
        await b.connect()
    assert not b.is_connected()


async def test_connect_rejects_unknown_account(monkeypatch):
    b = IBKRBroker(IBKRConfig(account="U000"))
    fake = FakeIB()
    connect_calls = []

    async def connect_async(*args, **kwargs):
        connect_calls.append(kwargs)

    fake.connected = False
    fake.connectAsync = connect_async  # type: ignore[attr-defined]
    fake.reqMarketDataType = lambda n: None  # type: ignore[attr-defined]
    b.ib = fake  # type: ignore[assignment]
    b._events_bound = True  # FakeIB has no events
    with pytest.raises(BrokerError):
        await b.connect()
    assert connect_calls and connect_calls[0]["account"] == "U000"


async def test_connect_success_reports_once(monkeypatch):
    b = IBKRBroker(IBKRConfig(account="", market_data_type=3))
    fake = FakeIB()
    fake.connected = False
    mdt = []

    async def connect_async(*args, **kwargs):
        fake.connected = True

    fake.connectAsync = connect_async  # type: ignore[attr-defined]
    fake.reqMarketDataType = mdt.append  # type: ignore[attr-defined]
    b.ib = fake  # type: ignore[assignment]
    b._events_bound = True
    states = []
    b.add_connection_handler(lambda ok, detail: states.append(ok))
    await b.connect()
    await b.connect()  # already connected: no-op
    assert b.account_id == ACC and mdt == [3] and states == [True]


def test_nan_guard_helpers():
    assert ibkr._price(float("nan")) is None
    assert ibkr._price(-1) is None
    assert ibkr._price(1.7976931348623157e308) is None
    assert ibkr._whole(9.9999999) == 10 and ibkr._whole(9.5) == 9
    assert math.isclose(ibkr._to_float("1,234.5"), 1234.5)


async def test_real_ib_events_bound_once():
    b = IBKRBroker(IBKRConfig(account=ACC))
    b._bind_events()
    b._bind_events()  # e.g. second connect after a reconnect
    got = []
    states = []
    b.add_fill_handler(got.append)
    b.add_connection_handler(lambda ok, detail: states.append(ok))
    b._conn_state = True
    trade = make_trade(LimitOrder("BUY", 10, 424.0, orderRef="copybot:pre"))
    b.ib.execDetailsEvent.emit(trade, ib_fill(2, 424.0))
    b.ib.errorEvent.emit(-1, 1100, "Connectivity between IB and TWS has been lost", None)
    b.ib.disconnectedEvent.emit()
    for _ in range(3):
        await asyncio.sleep(0)
    assert len(got) == 1 and got[0].qty == 2
    assert states == [False]
