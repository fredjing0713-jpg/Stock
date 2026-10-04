"""Interactive Brokers adapter on top of ``ib_async`` (DESIGN.md §3.5).

The engine never sees ib_async objects: everything is translated into the
broker-agnostic types of ``copybot.models``. The translation code lives in small
pure module-level helpers so it can be unit-tested with real ib_async objects
and without a network connection.

ib_async facts this module relies on (checked against ib_async 2.1 source):
* ``IB.disconnect()`` and a lost socket both reset ALL session state
  (trades, positions, account values); ``connectAsync`` re-downloads it.
* A request that fails with an API error resolves its future with the empty
  container (``RaiseRequestErrors`` is False), e.g. ``whatIfOrderAsync`` -> ``[]``.
* ``reqTickersAsync`` re-uses a cached ``Ticker`` per contract; fields that a
  new snapshot does not deliver would keep old values, so the cache entry is
  dropped before every snapshot.
* ``execDetailsEvent`` is only emitted for live fills of known trades.
* An order error that is not a warning flips the trade to ``Cancelled`` and
  appends a ``TradeLogEntry`` carrying the error code.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Coroutine, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from ib_async import (
    IB,
    Contract,
    LimitOrder,
    MarketOrder,
    Order,
    Stock,
    StopOrder,
    Ticker,
    Trade,
)
from ib_async import Fill as IBFill
from ib_async import OrderStatus as IBOrderStatus

from ..config import IBKRConfig
from ..models import (
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
from .base import Broker, BrokerError

log = logging.getLogger(__name__)

RECONNECT_INTERVAL_SECONDS = 10.0
QUOTE_TIMEOUT_SECONDS = 8.0
WHATIF_TIMEOUT_SECONDS = 10.0
QUALIFY_TIMEOUT_SECONDS = 10.0
MAX_VALID_MARGIN_RATE = 1.5

# IB "farm connection OK / inactive" style notices: not worth more than debug.
INFO_ERROR_CODES = frozenset({2104, 2106, 2158, 2107, 2108, 2119})
LINK_LOST_CODE = 1100  # connectivity between IB servers and the gateway lost
LINK_RESTORED_DATA_LOST_CODE = 1101  # restored, subscriptions must be re-requested
LINK_RESTORED_CODE = 1102  # restored, data maintained

# Non-segmented account tags we read (the "-S"/"-C" segment variants are ignored
# because we match tag names exactly).
_TAG_NLV = "NetLiquidation"
_TAG_EL = "ExcessLiquidity"
_TAG_AF = "AvailableFunds"
_TAG_MAINT = "FullMaintMarginReq"
_TAG_INIT = "FullInitMarginReq"
_TAG_GPV = "GrossPositionValue"
_TAG_FX = "ExchangeRate"

# ib_async marks "not set" doubles with sys.float_info.max; anything this large is not data.
_UNSET_THRESHOLD = 1e300
_QTY_EPS = 1e-6

_IB_STATUS_MAP = {
    IBOrderStatus.Filled: OrderStatus.FILLED,
    IBOrderStatus.Cancelled: OrderStatus.CANCELLED,
    IBOrderStatus.ApiCancelled: OrderStatus.CANCELLED,
    IBOrderStatus.Inactive: OrderStatus.REJECTED,
}


# --------------------------------------------------------------------------- small parsers


def _to_float(x: Any) -> float | None:
    """Float from a number or an IB string ("1,234.5"); None when unset / not finite."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, str):
        x = x.replace(",", "").strip()
        if not x:
            return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or abs(f) >= _UNSET_THRESHOLD:
        return None
    return f


def _price(x: Any) -> float | None:
    """A usable price: finite and > 0 (IB uses -1 / NaN / max-float for "none")."""
    f = _to_float(x)
    return f if f is not None and f > 0 else None


def _whole(qty: Any) -> int:
    """Whole shares from an IB float / Decimal quantity (never rounds up)."""
    f = _to_float(qty)
    if f is None:
        return 0
    return int(math.floor(f + _QTY_EPS)) if f >= 0 else -int(math.floor(-f + _QTY_EPS))


def _is_int_qty(qty: Any) -> bool:
    return isinstance(qty, int) and not isinstance(qty, bool) and qty > 0


# --------------------------------------------------------------------------- symbols


def to_ib_symbol(sym: str, aliases: Mapping[str, str] | None = None) -> str:
    """Provider symbol -> IBKR symbol: alias if configured, else ``BRK.B`` -> ``BRK B``."""
    key = sym.strip().upper()
    alias = (aliases or {}).get(key)
    if alias:
        return alias.strip().upper()
    return key.replace(".", " ")


def from_ib_symbol(ib_sym: str, aliases: Mapping[str, str] | None = None) -> str:
    """IBKR symbol -> provider symbol (reverse alias lookup, else ``BRK B`` -> ``BRK.B``)."""
    key = ib_sym.strip().upper()
    for provider_sym, ib_alias in (aliases or {}).items():
        if ib_alias.strip().upper() == key:
            return provider_sym.strip().upper()
    return key.replace(" ", ".")


# --------------------------------------------------------------------------- account


def _account_tags(values: Iterable[Any], account: str) -> dict[str, dict[str, float]]:
    """{tag: {currency: value}} for the account, skipping BASE / empty currency and model codes."""
    out: dict[str, dict[str, float]] = {}
    for v in values:
        if v.account != account or getattr(v, "modelCode", ""):
            continue
        currency = (v.currency or "").strip().upper()
        if not currency or currency == "BASE":
            continue
        f = _to_float(v.value)
        if f is None:
            continue
        out.setdefault(v.tag, {})[currency] = f
    return out


def _base_and_fx(tags: dict[str, dict[str, float]]) -> tuple[str, float]:
    """(base currency, base units per 1 USD) derived from NetLiquidation / ExchangeRate."""
    nlv = tags.get(_TAG_NLV)
    if not nlv:
        raise BrokerError("IBKR 账户数据缺少 NetLiquidation (净清算值), 无法计算仓位")
    if "USD" in nlv:
        base = "USD"
    elif len(nlv) == 1:
        base = next(iter(nlv))
    else:
        raise BrokerError(f"NetLiquidation 出现多种货币 {sorted(nlv)}, 无法确定基础货币")
    if base == "USD":
        return base, 1.0
    rate = tags.get(_TAG_FX, {}).get("USD")
    if rate is None or rate <= 0:
        raise BrokerError(f"账户基础货币为 {base}, 但缺少 USD 汇率 (ExchangeRate), 无法换算成美元")
    return base, rate


def base_per_usd_from_values(values: Iterable[Any], account: str) -> float:
    """How many base-currency units one USD is worth (1.0 for a USD account)."""
    return _base_and_fx(_account_tags(values, account))[1]


def account_from_values(values: Iterable[Any], account: str, now: datetime) -> AccountSnapshot:
    """Build a USD ``AccountSnapshot`` from ``ib.accountValues(account)``.

    Missing NetLiquidation raises BrokerError; other missing tags count as 0.0.
    """
    tags = _account_tags(values, account)
    base, fx = _base_and_fx(tags)

    def usd(tag: str) -> float:
        return tags.get(tag, {}).get(base, 0.0) / fx

    return AccountSnapshot(
        net_liquidation=usd(_TAG_NLV),
        excess_liquidity=usd(_TAG_EL),
        available_funds=usd(_TAG_AF),
        maint_margin=usd(_TAG_MAINT),
        init_margin=usd(_TAG_INIT),
        gross_position_value=usd(_TAG_GPV),
        ts=now,
    )


# --------------------------------------------------------------------------- quotes / margin


def quote_from_ticker(symbol: str, ticker: Ticker | None) -> Quote:
    """Quote from an ib_async Ticker; NaN / -1 / <= 0 become None."""
    if ticker is None:
        return Quote(symbol)
    return Quote(
        symbol,
        bid=_price(ticker.bid),
        ask=_price(ticker.ask),
        last=_price(ticker.last),
        close=_price(ticker.close),
    )


def rate_from_whatif(state: Any, qty: float, price: float, base_per_usd: float = 1.0) -> MarginRate | None:
    """Margin rates from a whatIf ``OrderState``: margin change / order value.

    The OrderState margin fields are strings in the account's base currency, hence
    ``base_per_usd``. Unset ("1.7976931348623157E308"), empty, non-finite, <= 0 or
    implausible (> 1.5) values give None.
    """
    if not hasattr(state, "initMarginChange") or not hasattr(state, "maintMarginChange"):
        return None  # e.g. [] when the whatIf request failed
    q, p, fx = _to_float(qty), _to_float(price), _to_float(base_per_usd)
    if not q or not p or not fx or q <= 0 or p <= 0 or fx <= 0:
        return None
    init, maint = _to_float(state.initMarginChange), _to_float(state.maintMarginChange)
    if init is None or maint is None or init <= 0 or maint <= 0:
        return None
    value = q * p
    init_rate, maint_rate = init / fx / value, maint / fx / value
    if init_rate > MAX_VALID_MARGIN_RATE or maint_rate > MAX_VALID_MARGIN_RATE:
        return None
    return MarginRate(init=init_rate, maint=maint_rate, source="whatif")


# --------------------------------------------------------------------------- orders


def build_ib_order(req: OrderRequest, account: str) -> Order:
    """Translate an ``OrderRequest`` into an ib_async Order (validated)."""
    if not _is_int_qty(req.qty):
        raise BrokerError(f"下单数量无效 {req.symbol}: {req.qty!r} (必须是正整数)")
    if not str(req.tag).startswith(BOT_TAG_PREFIX):
        raise BrokerError(f"订单标签必须以 {BOT_TAG_PREFIX} 开头: {req.tag!r}")
    if req.tif not in ("DAY", "GTC"):
        raise BrokerError(f"不支持的 TIF: {req.tif!r}")
    action = Side(req.side).value
    otype = OrderType(req.order_type)
    common: dict[str, Any] = dict(
        tif=req.tif,
        outsideRth=bool(req.outside_rth),
        orderRef=req.tag,
        account=account,
        transmit=True,
    )
    if otype is OrderType.MARKET:
        return MarketOrder(action, req.qty, **common)
    if otype is OrderType.LIMIT:
        px = _price(req.limit_price)
        if px is None:
            raise BrokerError(f"限价单缺少有效限价 {req.symbol}: {req.limit_price!r}")
        return LimitOrder(action, req.qty, px, **common)
    px = _price(req.stop_price)
    if px is None:
        raise BrokerError(f"止损单缺少有效触发价 {req.symbol}: {req.stop_price!r}")
    return StopOrder(action, req.qty, px, **common)


def trade_order_id(trade: Trade) -> str:
    """Our order id: ``str(orderId)``; orders without an API id fall back to the permId."""
    oid = int(trade.order.orderId or 0)
    if oid > 0:
        return str(oid)
    return f"perm{trade.order.permId or trade.orderStatus.permId}"


def _order_type(ib_type: str) -> OrderType:
    t = (ib_type or "").upper()
    if "STP" in t or "TRAIL" in t:
        return OrderType.STOP
    if "LMT" in t:
        return OrderType.LIMIT
    return OrderType.MARKET


def _avg_fill_price(trade: Trade) -> float | None:
    px = _price(trade.orderStatus.avgFillPrice)
    if px is not None:
        return px
    shares = sum(_to_float(f.execution.shares) or 0.0 for f in trade.fills)
    if shares <= 0:
        return None
    notional = sum((_to_float(f.execution.shares) or 0.0) * (_to_float(f.execution.price) or 0.0) for f in trade.fills)
    return _price(notional / shares)


def _last_error(trade: Trade) -> str:
    for entry in reversed(trade.log):
        if getattr(entry, "errorCode", 0):
            return entry.message or f"error {entry.errorCode}"
    return ""


def order_info_from_trade(trade: Trade, from_symbol: Callable[[str], str]) -> OrderInfo:
    """Broker-agnostic snapshot of an ib_async Trade."""
    order, st = trade.order, trade.orderStatus
    otype = _order_type(order.orderType)
    ib_type = (order.orderType or "").upper()
    # orderStatus.filled is cumulative and can be ahead of the execDetails fills
    # (or include fills from before a restart), so take the larger of the two.
    filled = max(_whole(trade.filled()), _whole(st.filled))
    return OrderInfo(
        order_id=trade_order_id(trade),
        symbol=from_symbol(trade.contract.symbol),
        side=Side.BUY if (order.action or "").upper() == "BUY" else Side.SELL,
        qty=_whole(order.totalQuantity),
        filled_qty=filled,
        avg_fill_price=_avg_fill_price(trade) if filled > 0 else None,
        order_type=otype,
        status=_IB_STATUS_MAP.get(st.status, OrderStatus.PENDING),
        limit_price=_price(order.lmtPrice) if "LMT" in ib_type else None,
        stop_price=_price(order.auxPrice) if otype is OrderType.STOP else None,
        outside_rth=bool(order.outsideRth),
        tif=order.tif or "DAY",
        tag=order.orderRef or "",
        message=_last_error(trade),
    )


def is_bot_trade(trade: Trade) -> bool:
    """A stock order placed by this bot (orderRef carries the bot prefix)."""
    return (trade.order.orderRef or "").startswith(BOT_TAG_PREFIX) and trade.contract.secType == "STK"


def fill_from_ib(trade: Trade, fill: IBFill, from_symbol: Callable[[str], str], now: datetime) -> Fill | None:
    """``Fill`` for one execution of one of our stock orders; None for anything else."""
    if not is_bot_trade(trade):
        return None
    ex = fill.execution
    when = fill.time if isinstance(fill.time, datetime) else now
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return Fill(
        order_id=trade_order_id(trade),
        exec_id=ex.execId,
        symbol=from_symbol(trade.contract.symbol),
        side=Side.BUY if ex.side == "BOT" else Side.SELL,
        qty=_whole(ex.shares),
        price=float(ex.price),
        tag=trade.order.orderRef,
        time=when,
    )


# --------------------------------------------------------------------------- broker


class IBKRBroker(Broker):
    """``Broker`` implementation for an IBKR (margin) account via TWS / IB Gateway."""

    def __init__(
        self,
        cfg: IBKRConfig,
        aliases: dict[str, str] | None = None,
        margin_cache_seconds: int = 1800,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.aliases = {k.strip().upper(): v.strip().upper() for k, v in (aliases or {}).items()}
        self.margin_cache_seconds = margin_cache_seconds
        self.ib = IB()
        self._account = cfg.account
        self._contracts: dict[str, Contract] = {}
        self._margin_cache: dict[str, tuple[MarginRate, float]] = {}
        self._connect_lock = asyncio.Lock()
        self._events_bound = False
        self._stopped = False  # set by disconnect(): run_forever stops reconnecting
        self._link_down = False  # IB error 1100 until 1101/1102
        self._conn_state: bool | None = None  # last state reported to handlers
        self._tasks: set[asyncio.Task] = set()

    @property
    def account_id(self) -> str:
        return self._account

    # ---------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        """Connect + sync; idempotent. Raises BrokerError on any failure."""
        c = self.cfg
        async with self._connect_lock:
            self._stopped = False
            if self.ib.isConnected():
                return
            self._bind_events()
            try:
                await self.ib.connectAsync(
                    c.host, c.port, clientId=c.client_id, timeout=c.connect_timeout_seconds, account=c.account
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                raise BrokerError(f"IBKR 连接失败 ({c.host}:{c.port} clientId={c.client_id}): {e!r}") from e
            try:
                self._account = self._pick_account()
                self.ib.reqMarketDataType(c.market_data_type)
            except Exception as e:
                self.ib.disconnect()
                if isinstance(e, BrokerError):
                    raise
                raise BrokerError(f"IBKR 连接后初始化失败: {e!r}") from e
            self._link_down = False
            log.info("IBKR connected %s:%s clientId=%s account=%s", c.host, c.port, c.client_id, self._account)
        await self._set_conn_state(True, f"已连接 IBKR {c.host}:{c.port} 账户 {self._account}")

    async def disconnect(self) -> None:
        self._stopped = True
        try:
            self.ib.disconnect()
        except Exception as e:  # pragma: no cover - defensive
            log.warning("IBKR disconnect error: %r", e)
        self._link_down = False
        await self._set_conn_state(False, "已主动断开 IBKR 连接")

    def is_connected(self) -> bool:
        return self.ib.isConnected() and not self._link_down

    async def run_forever(self) -> None:
        """Reconnect loop: every 10 s reconnect when the socket is down. Never raises."""
        while True:
            try:
                if not self._stopped and not self.ib.isConnected():
                    try:
                        await self.connect()
                    except BrokerError as e:
                        if self._conn_state is not False:
                            log.warning("IBKR 未连接, %.0f 秒后重试: %s", RECONNECT_INTERVAL_SECONDS, e)
                        else:
                            log.debug("IBKR reconnect failed: %s", e)
                        await self._set_conn_state(False, f"IBKR 未连接: {e}")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("IBKR reconnect loop error")
            await asyncio.sleep(RECONNECT_INTERVAL_SECONDS)

    def _pick_account(self) -> str:
        accounts = list(self.ib.managedAccounts())
        if not accounts:
            raise BrokerError("IBKR 没有返回任何账户")
        if self.cfg.account:
            if self.cfg.account not in accounts:
                raise BrokerError(f"配置的账户 {self.cfg.account} 不在 IBKR 账户列表 {accounts} 中")
            return self.cfg.account
        if len(accounts) > 1:
            log.warning("IBKR has several accounts %s and none is configured; using %s", accounts, accounts[0])
        return accounts[0]

    def _bind_events(self) -> None:
        """Subscribe the ib_async events once (they survive reconnects)."""
        if self._events_bound:
            return
        self.ib.execDetailsEvent += self._on_exec_details
        self.ib.disconnectedEvent += self._on_disconnected
        self.ib.errorEvent += self._on_error
        self._events_bound = True

    async def _set_conn_state(self, connected: bool, detail: str) -> None:
        """Tell the handlers about a state change (only on changes)."""
        if self._conn_state is connected:
            return
        self._conn_state = connected
        try:
            await self._emit_connection(connected, detail)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("connection handler failed")

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Run a coroutine from a sync ib_async callback, keeping a reference to it."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        task = loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("IBKR background task failed", exc_info=task.exception())

    # ---------------------------------------------------------------- ib_async callbacks
    def _on_disconnected(self) -> None:
        log.warning("IBKR socket disconnected")
        self._link_down = False
        self._spawn(self._report_if_disconnected())

    async def _report_if_disconnected(self) -> None:
        if not self.ib.isConnected():
            await self._set_conn_state(False, f"IBKR 连接断开, 每 {RECONNECT_INTERVAL_SECONDS:.0f} 秒自动重连")

    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract: Any = None) -> None:
        sym = getattr(contract, "symbol", "") if contract is not None else ""
        if errorCode in INFO_ERROR_CODES:
            log.debug("IB info %s reqId=%s: %s", errorCode, reqId, errorString)
        elif errorCode == LINK_LOST_CODE:
            log.warning("IB %s: %s", errorCode, errorString)
            self._link_down = True
            self._spawn(self._set_conn_state(False, f"IBKR 与服务器连接中断 ({errorCode})"))
        elif errorCode == LINK_RESTORED_CODE:
            log.info("IB %s: %s", errorCode, errorString)
            if self._link_down:
                self._link_down = False
                self._spawn(self._set_conn_state(True, f"IBKR 与服务器连接已恢复 ({errorCode})"))
        elif errorCode == LINK_RESTORED_DATA_LOST_CODE:
            # Account / market data subscriptions are gone: resync with a full reconnect.
            log.warning("IB %s: %s -> reconnecting to resubscribe", errorCode, errorString)
            self._spawn(self._resync())
        else:
            log.warning("IB error %s reqId=%s %s: %s", errorCode, reqId, sym, errorString)

    async def _resync(self) -> None:
        await asyncio.sleep(0)  # leave the ib_async message handler first
        if self._stopped:
            return
        self._link_down = False
        self.ib.disconnect()
        try:
            await self.connect()
        except BrokerError as e:
            log.warning("IBKR resync reconnect failed (run_forever will retry): %s", e)
            await self._set_conn_state(False, f"IBKR 未连接: {e}")

    def _on_exec_details(self, trade: Trade, fill: IBFill) -> None:
        try:
            f = fill_from_ib(trade, fill, self._from_ib, datetime.now(timezone.utc))
        except Exception:
            log.exception("cannot convert execution %r", fill)
            return
        if f is None:
            return
        log.info("fill %s %s %d @ %.4f order=%s exec=%s", f.side.value, f.symbol, f.qty, f.price, f.order_id, f.exec_id)
        self._spawn(self._emit_fill(f))

    # ---------------------------------------------------------------- helpers
    def _require_connected(self) -> None:
        if not self.ib.isConnected():
            raise BrokerError("IBKR 未连接")
        if self._link_down:
            raise BrokerError("IBKR 与服务器连接中断 (1100), 等待恢复")

    def _from_ib(self, ib_sym: str) -> str:
        return from_ib_symbol(ib_sym, self.aliases)

    def _info(self, trade: Trade) -> OrderInfo:
        return order_info_from_trade(trade, self._from_ib)

    def _is_ours(self, trade: Trade) -> bool:
        return is_bot_trade(trade) and (trade.order.account or self._account) == self._account

    def _find_trade(self, order_id: str) -> Trade | None:
        """The trade with this id; a live one wins over done ones, newest last."""
        matches = [t for t in self.ib.trades() if trade_order_id(t) == str(order_id)]
        if not matches:
            return None
        live = [t for t in matches if not t.isDone()]
        return (live or matches)[-1]

    async def _qualify(self, keys: list[str]) -> dict[str, Contract | None]:
        """Qualified ``Stock(sym, "SMART", "USD")`` per upper-case provider symbol (cached)."""
        missing = [k for k in dict.fromkeys(keys) if k not in self._contracts]
        if missing:
            raw = [Stock(to_ib_symbol(k, self.aliases), "SMART", "USD") for k in missing]
            try:
                res = await asyncio.wait_for(self.ib.qualifyContractsAsync(*raw), QUALIFY_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                log.warning("contract lookup timed out: %s", missing)
                res = [None] * len(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                raise BrokerError(f"合约查询失败 {missing}: {e!r}") from e
            for k, c in zip(missing, res, strict=False):  # mismatch → unknown, never crash
                if isinstance(c, Contract) and c.conId:
                    self._contracts[k] = c
                else:
                    log.warning("cannot qualify contract %s (IB symbol %r)", k, to_ib_symbol(k, self.aliases))
        return {k: self._contracts.get(k) for k in keys}

    async def _contract(self, symbol: str) -> Contract:
        key = symbol.strip().upper()
        c = (await self._qualify([key]))[key]
        if c is None:
            raise BrokerError(f"无法识别合约 {symbol} (IBKR 代码 {to_ib_symbol(key, self.aliases)!r})")
        return c

    def _drop_cached_tickers(self, contracts: list[Contract]) -> None:
        """Forget ib_async's cached Ticker objects so a snapshot never shows old fields."""
        tickers = getattr(getattr(self.ib, "wrapper", None), "tickers", None)
        if not isinstance(tickers, dict):
            return
        for c in contracts:
            tickers.pop(hash(c), None)

    # ---------------------------------------------------------------- reads
    async def account(self) -> AccountSnapshot:
        self._require_connected()
        values = self.ib.accountValues(self._account)
        return account_from_values(values, self._account, datetime.now(timezone.utc))

    async def positions(self) -> dict[str, Position]:
        """Non-zero USD stock positions of the account, keyed by provider symbol."""
        self._require_connected()
        out: dict[str, Position] = {}
        for p in self.ib.positions(self._account):
            c = p.contract
            if c.secType != "STK" or (c.currency or "USD") != "USD":
                continue
            qty = float(p.position)
            if qty == 0:
                continue
            sym = self._from_ib(c.symbol)
            pos = Position(symbol=sym, qty=qty, avg_cost=float(p.avgCost))
            prev = out.get(sym)
            if prev is not None:  # same symbol under two conIds: merge
                log.warning("two positions map to %s; merging", sym)
                total = prev.qty + qty
                cost = (prev.qty * prev.avg_cost + qty * pos.avg_cost) / total if total else 0.0
                pos = Position(symbol=sym, qty=total, avg_cost=cost)
            out[sym] = pos
        return out

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Snapshot quotes; unknown symbols / timeouts give Quote(sym) with None fields."""
        result = {s: Quote(s) for s in dict.fromkeys(symbols)}
        if not result:
            return result
        self._require_connected()
        keyed = {s: s.strip().upper() for s in result}
        contracts = await self._qualify(list(keyed.values()))
        pairs = [(s, contracts[k]) for s, k in keyed.items() if contracts.get(k) is not None]
        if not pairs:
            return result
        cs = [c for _, c in pairs]
        self._drop_cached_tickers(cs)
        try:
            tickers: list[Ticker | None] = list(
                await asyncio.wait_for(self.ib.reqTickersAsync(*cs), QUOTE_TIMEOUT_SECONDS)
            )
        except asyncio.TimeoutError:
            # The snapshot end can take IB up to ~11 s; keep whatever arrived (fresh tickers).
            log.warning("quote snapshot timed out after %.0f s: %s", QUOTE_TIMEOUT_SECONDS, [s for s, _ in pairs])
            tickers = [self.ib.ticker(c) for c in cs]
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise BrokerError(f"获取行情失败: {e!r}") from e
        for (s, _), t in zip(pairs, tickers, strict=False):
            result[s] = quote_from_ticker(s, t)
        return result

    async def margin_rate(self, symbol: str, qty: int, price: float) -> MarginRate | None:
        """Margin rate from an IBKR whatIf order (cached); None when unknown. Never raises."""
        key = symbol.strip().upper()
        cached = self._margin_cache.get(key)
        if cached and time.monotonic() - cached[1] < self.margin_cache_seconds:
            return cached[0]
        if not self.is_connected() or _to_float(qty) is None or qty <= 0 or _price(price) is None:
            return None
        try:
            contract = await self._contract(key)
            order = MarketOrder("BUY", int(qty), account=self._account)
            state = await asyncio.wait_for(self.ib.whatIfOrderAsync(contract, order), WHATIF_TIMEOUT_SECONDS)
            fx = base_per_usd_from_values(self.ib.accountValues(self._account), self._account)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("whatIf margin for %s failed: %r", key, e)
            return None
        rate = rate_from_whatif(state, qty, price, fx)
        if rate is None:
            log.warning("whatIf margin for %s unusable: %r", key, state)
            return None
        self._margin_cache[key] = (rate, time.monotonic())
        return rate

    async def open_orders(self) -> list[OrderInfo]:
        self._require_connected()
        return [self._info(t) for t in self.ib.openTrades() if self._is_ours(t)]

    async def get_order(self, order_id: str) -> OrderInfo | None:
        self._require_connected()
        trade = self._find_trade(order_id)
        return self._info(trade) if trade is not None else None

    # ---------------------------------------------------------------- writes
    async def place_order(self, req: OrderRequest) -> OrderInfo:
        self._require_connected()
        order = build_ib_order(req, self._account)
        contract = await self._contract(req.symbol)
        try:
            trade = self.ib.placeOrder(contract, order)
        except Exception as e:
            raise BrokerError(f"下单失败 {req.symbol}: {e!r}") from e
        # The order is live from here on: nothing below may raise.
        info = self._info(trade)
        log.info(
            "placed %s %s %s x%d lmt=%s stp=%s outsideRth=%s tif=%s tag=%s id=%s",
            order.orderType, order.action, req.symbol, req.qty, info.limit_price, info.stop_price,
            order.outsideRth, order.tif, order.orderRef, info.order_id,
        )
        return info

    async def modify_order(
        self,
        order_id: str,
        *,
        qty: int | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> OrderInfo:
        self._require_connected()
        trade = self._find_trade(order_id)
        if trade is None or trade.isDone():
            raise BrokerError(f"找不到可修改的订单 {order_id}")
        if not self._is_ours(trade):
            raise BrokerError(f"订单 {order_id} 不是机器人下的单, 不修改")
        order = trade.order
        changes = self._modify_changes(order, qty, limit_price, stop_price)
        if not changes:
            return self._info(trade)
        old = {k: getattr(order, k) for k in changes}
        for k, v in changes.items():
            setattr(order, k, v)
        try:
            trade = self.ib.placeOrder(trade.contract, order)
        except Exception as e:
            for k, v in old.items():
                setattr(order, k, v)
            raise BrokerError(f"改单失败 {order_id}: {e!r}") from e
        log.info("modified order %s: %s", order_id, changes)
        return self._info(trade)

    @staticmethod
    def _modify_changes(
        order: Order, qty: int | None, limit_price: float | None, stop_price: float | None
    ) -> dict[str, Any]:
        """Validated field changes for a modification (order attribute -> value)."""
        ib_type = (order.orderType or "").upper()
        changes: dict[str, Any] = {}
        if qty is not None:
            if not _is_int_qty(qty):
                raise BrokerError(f"改单数量无效: {qty!r}")
            changes["totalQuantity"] = qty
        if limit_price is not None:
            px = _price(limit_price)
            if px is None or "LMT" not in ib_type:
                raise BrokerError(f"无法对 {ib_type} 订单设置限价 {limit_price!r}")
            changes["lmtPrice"] = px
        if stop_price is not None:
            px = _price(stop_price)
            if px is None or "STP" not in ib_type:
                raise BrokerError(f"无法对 {ib_type} 订单设置止损价 {stop_price!r}")
            changes["auxPrice"] = px
        return changes

    async def cancel_order(self, order_id: str) -> None:
        self._require_connected()
        trade = self._find_trade(order_id)
        if trade is None:
            log.info("cancel: order %s not found, ignored", order_id)
            return
        if trade.isDone():
            log.debug("cancel: order %s already done (%s)", order_id, trade.orderStatus.status)
            return
        if not self._is_ours(trade):
            log.warning("cancel: order %s is not a bot order, not touching it", order_id)
            return
        try:
            self.ib.cancelOrder(trade.order)
        except Exception as e:
            raise BrokerError(f"撤单失败 {order_id}: {e!r}") from e
        log.info("cancel requested for order %s", order_id)
