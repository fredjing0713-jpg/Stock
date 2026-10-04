"""Trading engine: provider signals → orders on the margin account (DESIGN.md §2, §3.8).

Concurrency model:
* One ``asyncio.Lock`` serialises every trading action (signals, the pending
  scheduler, !sync, !cancel, deleverage, stop sync). Public entry points take it;
  internal helpers (prefixed ``_``) never do, so there is no re-entrancy deadlock.
* The broker fill handler never takes the lock: it records the fill, notifies and
  pokes the stop-sync loop.
* Every public entry point catches exceptions, logs them and notifies the owner
  (level ``error`` with @mention); background loops never die.

Safety rules worth knowing when reading the code:
* Before any SELL the symbol's stop orders are cancelled and confirmed done; if
  that fails, or a stop filled meanwhile, the sell is skipped (never oversell).
* SELL quantities are clamped to a fresh position read right before placing.
* Pending intents are deleted from the store *before* they are executed
  (at-most-once: a crash never re-trades them).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from .broker.base import Broker
from .config import AppConfig, TradingConfig
from .market_hours import NY, next_regular_open, regular_hours, session_at, to_ny
from .models import (
    BOT_TAG_PREFIX,
    STOP_TAG,
    AccountSnapshot,
    Change,
    Fill,
    GrossTarget,
    Intent,
    MarginRate,
    MessageMeta,
    OrderInfo,
    OrderRequest,
    OrderStatus,
    OrderType,
    PlannedOrder,
    Position,
    Quote,
    Session,
    Side,
    Signal,
    SkipNote,
)
from .notifier import Notifier
from .parser import parse_signal, suspicious_lines
from .sizing import (
    compute_gross_target,
    conservative_account,
    deleverage_orders,
    desired_stops,
    diff_stops,
    estimate_after_sells,
    extended_limit_price,
    intents_for_sync,
    intents_from_signal,
    resolve_intents,
    scale_buys_for_liquidity,
    split_extended,
)
from .store import Store, ensure_aware, from_iso, to_iso

__all__ = ["Engine", "default_clock", "MKT_TAG", "EXT_TAG", "DLV_TAG", "SYNC_KEY", "OPEN_KEY"]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------- constants

MKT_TAG = f"{BOT_TAG_PREFIX}:mkt"  # + ":<key8>" regular-hours market orders
EXT_TAG = f"{BOT_TAG_PREFIX}:ext"  # + ":<key8>" pre / after-hours limit orders
DLV_TAG = f"{BOT_TAG_PREFIX}:dlv"  # deleverage market sells
SYNC_KEY = "sync"  # signal_key / key8 of !sync intents
OPEN_KEY = "open"  # key8 of orders placed by the pending scheduler

SYNC_BAND = 0.05  # !sync ignores |delta value| < 5% of the target value
SYNC_PREVIEW_TTL_SECONDS = 180
CANCEL_WAIT_SECONDS = 10.0  # how long to wait for a cancel to be confirmed
SCHEDULER_INTERVAL_SECONDS = 2.0
STOP_SYNC_SETTLE_SECONDS = 1.0  # after a poke, let position updates arrive first
STOP_FILL_GUARD_SECONDS = 5.0  # a stop fill this close before a sell blocks the sell
PENDING_GRACE_SECONDS = 60  # pending due earlier than today's open - 60 s is expired
LIQUIDITY_WARN_MARGIN = 0.05  # warn below reserve_ratio - 5%
LIQUIDITY_WARN_INTERVAL_SECONDS = 3600
STOP_NOTE_REPEAT_SECONDS = 3600  # identical stop-sync notices at most once per hour
STOP_MARK_MERGE_SECONDS = 3600  # partial stop fills within 1 h are one stop-out
RATE_PROBE_MIN_USD = 1000.0  # whatIf probe size: max($1000, 25% NLV)
RATE_PROBE_NLV_FRACTION = 0.25
SHUTDOWN_WAIT_SECONDS = 30.0  # stop() waits this long for an in-flight trade
EXT_SELL_WAIT_SECONDS = 15.0  # pre-market rotation: how long to wait for the sell limits

KV_PAUSED = "paused"
KV_LAST_SIGNAL_TS = "last_signal_ts"
KV_PORTFOLIO = "provider_portfolio"
KV_LAST_MISMATCH = "last_mismatch"
KV_LAST_DELEVERAGE_TS = "last_deleverage_ts"
KV_LAST_LIQUIDITY_WARN_TS = "last_liquidity_warn_ts"
KV_CANCEL_LEFTOVERS = "cancel_leftovers"  # symbols whose extended-hours orders still must be cancelled

SESSION_CN = {Session.PRE: "盘前", Session.REGULAR: "盘中", Session.AFTER: "盘后", Session.CLOSED: "休市"}
BINDING_CN = {"reserve": "剩余流动性保留", "init_margin": "初始保证金", "leverage_cap": "杠杆上限"}
_WEEKDAYS = "一二三四五六日"
_LEVEL_RANK = {"info": 0, "warn": 1, "error": 2}


def default_clock() -> datetime:
    """Current time in New York."""
    return datetime.now(NY)


# ---------------------------------------------------------------------------- formatting


def _whole(qty: float) -> int:
    """Broker position quantity → whole shares, truncated toward zero."""
    return math.floor(qty + 1e-6) if qty >= 0 else math.ceil(qty - 1e-6)


def _fmt_ts(dt: datetime, weekday: bool = False) -> str:
    """'10-02 09:50:59' in New York time (optionally with the weekday)."""
    ny = to_ny(ensure_aware(dt))
    if weekday:
        return f"{ny:%m-%d}（周{_WEEKDAYS[ny.weekday()]}）{ny:%H:%M:%S}"
    return f"{ny:%m-%d %H:%M:%S}"


def _usd(x: float) -> str:
    return f"${x:,.0f}" if math.isfinite(x) else "$?"


def _pct(weight: float) -> str:
    """0.5 → '50%', 0.3333 → '33.33%'."""
    return f"{round(weight * 100, 2):g}%"


def _num(x: float) -> str:
    """422.6 → '422.6', 100.0 → '100'."""
    return f"{x:.4f}".rstrip("0").rstrip(".")


def _px(p: float | None) -> str:
    if p is None or not math.isfinite(p):
        return "?"
    return f"{p:.2f}" if p >= 1 else f"{p:.4f}"


def _qty(q: float) -> str:
    return f"{q:g}"


def _parse_time(raw: Any, what: str = "") -> datetime | None:
    """Stored ISO time → NY datetime; None when missing or unreadable."""
    if not raw:
        return None
    try:
        return to_ny(from_iso(raw))
    except Exception:
        log.warning("unreadable stored time %s: %r", what, raw)
        return None


def _side_cn(side: Side) -> str:
    return "买入" if Side(side) == Side.BUY else "卖出"


def _change_text(ch: Change) -> str:
    price = f" @{_num(ch.price)}" if ch.price else ""
    return f"{ch.action} {ch.symbol} {_pct(ch.from_weight)}→{_pct(ch.to_weight)}{price}"


def _semantic_key(sig: Signal) -> str | None:
    """Second de-duplication key: same timestamp + changes + holdings = same signal.

    Catches a re-forward whose text differs slightly (e.g. an added comment line).
    Only for signals with a timestamp (identical changes at unknown times may be new).
    """
    if sig.timestamp is None:
        return None
    holdings = sig.holdings if sig.has_holdings_section else None
    payload = repr((ensure_aware(sig.timestamp).isoformat(), sig.changes, holdings))
    return "sem:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _intent_text(it: Intent) -> str:
    """'建仓 0%→50%' or '同步 → 目标 30%'."""
    if it.reason and "→" in it.reason:
        return it.reason
    return f"{it.reason or it.direction} → 目标 {_pct(it.target_weight)}"


def _signal_summary(sig: Signal) -> str:
    return "；".join(_change_text(c) for c in sig.changes) or "持仓更新（无仓位变动）"


def _result_line(o: PlannedOrder, info: OrderInfo | None, error: str = "", label: str = "") -> tuple[str, str]:
    """(report line, level) for one order. ``label`` marks extended-hours limit orders."""
    if label and info is not None and info.limit_price:
        head = f"{label}限价{_side_cn(o.side)} {o.symbol} {o.qty}股 @{_px(info.limit_price)}"
    else:
        head = f"{_side_cn(o.side)} {o.symbol} {o.qty}股"
    if info is None:
        return f"❌ {head} 下单失败：{error}", "error"
    filled = info.filled_qty
    price = f" @ {_px(info.avg_fill_price)}" if info.avg_fill_price else f"（参考价 {_px(o.ref_price)}）"
    msg = f"：{info.message}" if info.message and info.message != "dry-run" else ""
    if info.status == OrderStatus.FILLED:
        return f"✅ {head} 成交 {filled}{price}", "info"
    if info.status == OrderStatus.REJECTED:
        return f"❌ {head} 被拒绝{msg}", "error"
    if info.status == OrderStatus.CANCELLED:
        if filled:
            return f"⚠️ {head} 部分成交 {filled}{price} 后被取消{msg}", "warn"
        return f"❌ {head} 被取消{msg}", "error"
    if label:  # a working extended-hours order is the expected outcome
        done = f"，已成交 {filled}{price}" if filled else ""
        return f"📝 {head} 已挂单{done}", "info"
    if filled:
        return f"⏳ {head} 已成交 {filled}{price}，剩余仍在挂单", "warn"
    return f"⏳ {head} 尚未成交，仍在挂单", "warn"


# ---------------------------------------------------------------------------- internal types


@dataclass
class _Context:
    """Broker snapshot used for one planning step."""

    positions: dict[str, Position]
    quotes: dict[str, Quote]
    account: AccountSnapshot  # conservative: reported values vs. live positions × margin rates
    rates: dict[str, MarginRate]
    gross: GrossTarget
    started: float  # time.monotonic() when the snapshot was started
    raw_account: AccountSnapshot  # exactly as reported by the broker


@dataclass
class _Report:
    """Lines for one Discord message plus its severity."""

    lines: list[str] = field(default_factory=list)
    level: str = "info"
    mention: bool = False
    # Estimated whole-share positions after an execution (None = not executed).
    qty_after: dict[str, int] | None = None

    def add(self, line: str, level: str = "info", mention: bool = False) -> None:
        self.lines.append(line)
        self.escalate(level, mention)

    def escalate(self, level: str, mention: bool = False) -> None:
        if _LEVEL_RANK.get(level, 0) > _LEVEL_RANK.get(self.level, 0):
            self.level = level
        self.mention = self.mention or mention

    def merge(self, other: _Report) -> None:
        self.lines.extend(other.lines)
        self.escalate(other.level, other.mention)
        if other.qty_after is not None:
            self.qty_after = other.qty_after

    def text(self) -> str:
        return "\n".join(self.lines)


# ---------------------------------------------------------------------------- engine


class Engine:
    """Turns signals into orders and keeps stops / liquidity in check."""

    def __init__(
        self,
        cfg: AppConfig,
        broker: Broker,
        store: Store,
        notifier: Notifier,
        clock: Callable[[], datetime] = default_clock,
    ) -> None:
        self.cfg = cfg
        self.broker = broker
        self.store = store
        self.notifier = notifier
        self.clock = clock
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._handlers_added = False
        self._closed = False
        self._last_positions: dict[str, Position] = {}
        self._sync_preview_at: datetime | None = None
        self._stop_fill_mono: dict[str, float] = {}  # symbol → monotonic time of last stop fill
        self._bg: set[asyncio.Task] = set()  # fire-and-forget tasks (kept referenced)
        # last_signal_ts as it was before the first history message was read (taken
        # lazily): None means a first run (empty DB) — history is learned, never traded.
        self._boot_snapshot_taken = False
        self._boot_last_signal: datetime | None = None
        self._last_stop_note: tuple[str, float] = ("", 0.0)
        # Tunables (tests shorten them).
        self.cancel_wait_seconds = CANCEL_WAIT_SECONDS
        self.scheduler_interval_seconds = SCHEDULER_INTERVAL_SECONDS
        self.stop_sync_settle_seconds = STOP_SYNC_SETTLE_SECONDS

    # ------------------------------------------------------------------ small accessors

    @property
    def _t(self) -> TradingConfig:
        return self.cfg.trading

    @property
    def _ignore(self) -> set[str]:
        return set(self.cfg.trading.ignore_symbols)

    def _now(self) -> datetime:
        return to_ny(self.clock())

    def _open_due(self, now: datetime) -> datetime:
        """Next regular open + open_delay_seconds."""
        return next_regular_open(now) + timedelta(seconds=self._t.open_delay_seconds)

    def _kv_time(self, key: str) -> datetime | None:
        """A KV datetime (stored as ISO string) in NY time, None if missing / unreadable."""
        return _parse_time(self.store.get(key), key)

    def _portfolio(self) -> dict[str, Any] | None:
        pf = self.store.get(KV_PORTFOLIO)
        return pf if isinstance(pf, dict) else None

    def _provider_weights(self) -> dict[str, float]:
        weights = (self._portfolio() or {}).get("weights") or {}
        return {str(s): float(w) for s, w in weights.items()}

    def _poke_stops(self) -> None:
        self._stop_event.set()

    @property
    def is_paused(self) -> bool:
        return bool(self.store.get(KV_PAUSED, False))

    def pause(self) -> None:
        """Stop following signals (stops and deleverage keep running)."""
        self.store.set(KV_PAUSED, True)
        log.info("copy trading paused")

    def resume(self) -> None:
        self.store.set(KV_PAUSED, False)
        log.info("copy trading resumed")

    # ------------------------------------------------------------------ notifications

    async def _notify(self, text: str, *, level: str = "info", mention: bool = False) -> None:
        if not text.strip():
            return
        try:
            await self.notifier.send(text, level=level, mention=mention)
        except Exception:
            log.exception("notifier failed: %s", text)

    async def _send_report(self, rep: _Report) -> None:
        if self._t.dry_run and rep.lines:
            rep.lines.insert(0, "🧪 DRY-RUN 模拟（未真实下单）")
        await self._notify(rep.text(), level=rep.level, mention=rep.mention)

    async def _fail(self, where: str, exc: BaseException, hint: str = "") -> None:
        log.error("%s failed", where, exc_info=exc)
        text = f"{where}出错：{type(exc).__name__}: {exc}"
        if hint:
            text += f"\n{hint}"
        await self._notify(text, level="error", mention=True)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Register broker handlers and start the background loops."""
        if not self._handlers_added:
            self.broker.add_fill_handler(self._on_fill)
            self.broker.add_connection_handler(self._on_connection)
            self._handlers_added = True
        if self._tasks:
            return
        self._closed = False
        self._tasks = [
            asyncio.create_task(self._scheduler_loop(), name="copybot-scheduler"),
            asyncio.create_task(self._stop_loop(), name="copybot-stops"),
            asyncio.create_task(self._liquidity_loop(), name="copybot-liquidity"),
        ]
        self._poke_stops()

    async def stop(self) -> None:
        """Stop the loops and wait (bounded) for an in-flight trading action."""
        self._closed = True
        tasks, self._tasks = self._tasks + list(self._bg), []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(self._lock.acquire(), SHUTDOWN_WAIT_SECONDS)
            self._lock.release()
        except TimeoutError:
            log.warning("engine stop: a trading action is still running after %.0f s", SHUTDOWN_WAIT_SECONDS)

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await self.run_due_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler loop iteration failed")
            await asyncio.sleep(self.scheduler_interval_seconds)

    async def _stop_loop(self) -> None:
        while True:
            try:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), self._t.stop_sync_interval_seconds)
                    await asyncio.sleep(self.stop_sync_settle_seconds)
                except TimeoutError:
                    pass
                self._stop_event.clear()
                await self.sync_stops()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("stop loop iteration failed")
                await asyncio.sleep(1.0)

    async def _liquidity_loop(self) -> None:
        while True:
            try:
                await self.check_liquidity()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("liquidity loop iteration failed")
            await asyncio.sleep(self.cfg.risk.liquidity_check_interval_seconds)

    # ------------------------------------------------------------------ broker events

    async def _on_fill(self, fill: Fill) -> None:
        """Broker fill callback. Never takes the lock."""
        try:
            if self.store.fill_seen(fill.exec_id):
                return
            self.store.record_fill(fill)
            sym = fill.symbol
            if fill.tag.startswith(STOP_TAG):
                self._stop_fill_mono[sym] = time.monotonic()
                self._mark_stopped(fill)
                self.store.delete_pending(sym)
                self._spawn(self._cancel_after_stop(sym))
                await self._notify(
                    f"🛑 止损触发 {sym} 卖出 {fill.qty}股 @ {_px(fill.price)}；"
                    f"他之后对 {sym} 建仓/加仓时会重新跟入",
                    level="warn",
                    mention=True,
                )
            elif ":ext:" in fill.tag:
                await self._notify(f"🌙 盘前/盘后成交 {_side_cn(fill.side)} {sym} {fill.qty}股 @ {_px(fill.price)}")
        except Exception as exc:
            await self._fail("处理成交回报", exc)
        finally:
            self._poke_stops()

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _cancel_after_stop(self, symbol: str) -> None:
        """After a stop-out, leftover pre-market / after-hours orders must not re-buy."""
        if self._closed:
            return
        async with self._lock:
            try:
                note, filled = await self._cancel_leftovers({symbol})
                if note:
                    await self._notify(f"🧹 {symbol} 止损后：{note}", level="warn", mention=filled)
            except Exception as exc:
                await self._fail(f"止损后撤销 {symbol} 挂单", exc)

    def _leftover_backlog(self) -> set[str]:
        raw = self.store.get(KV_CANCEL_LEFTOVERS) or []
        return {str(x) for x in raw} if isinstance(raw, list) else set()

    def _set_leftover_backlog(self, symbols: set[str]) -> None:
        self.store.set(KV_CANCEL_LEFTOVERS, sorted(symbols))

    async def _cancel_leftovers(self, symbols: set[str]) -> tuple[str, bool]:
        """Cancel our extended-hours limit orders for `symbols` (their pending intent is gone).

        Only ``copybot:ext`` orders: protective deleverage sells and stops stay. Symbols
        whose orders cannot be cancelled now (IBKR down, cancel not confirmed) are kept in
        a persistent backlog that the stop-sync loop retries. Returns (note, any_filled).
        """
        if not symbols:
            return "", False
        backlog = self._leftover_backlog() | symbols
        if not self.broker.is_connected():
            self._set_leftover_backlog(backlog)
            return "⚠️ IBKR 未连接，旧的盘前/盘后挂单暂未撤销，连上后自动撤销", False
        working = [
            o
            for o in await self.broker.open_orders()
            if o.symbol in symbols and o.tag.startswith(EXT_TAG) and not o.is_done
        ]
        for o in working:
            try:
                await self.broker.cancel_order(o.order_id)
            except Exception as exc:
                log.warning("cancel leftover %s (%s) failed: %r", o.order_id, o.symbol, exc)
        finals = await asyncio.gather(*(self._wait_done(o, self.cancel_wait_seconds) for o in working))
        cancelled, filled, stuck = [], [], []
        for before, after in zip(working, finals, strict=True):
            if after.filled_qty > before.filled_qty:
                filled.append(f"{after.symbol} {_side_cn(after.side)}{after.filled_qty - before.filled_qty}股")
            if not after.is_done:
                stuck.append(f"{after.symbol}({after.order_id})")
            elif after.status != OrderStatus.FILLED:
                cancelled.append(f"{after.symbol} {_side_cn(after.side)}{after.remaining}股")
        stuck_symbols = {o.symbol for o, f in zip(working, finals, strict=True) if not f.is_done}
        self._set_leftover_backlog((backlog - symbols) | stuck_symbols)
        parts = []
        if cancelled:
            parts.append("已撤销旧挂单：" + "、".join(cancelled))
        if filled:
            parts.append("⚠️ 撤单前已成交：" + "、".join(filled))
        if stuck:
            parts.append("❌ 未能撤销（稍后自动重试）：" + "、".join(stuck))
        return "；".join(parts), bool(filled)

    async def _retry_leftover_backlog(self) -> None:
        """Retry cancelling leftovers that could not be cancelled earlier (lock held)."""
        backlog = self._leftover_backlog()
        if not backlog or not self.broker.is_connected():
            return
        note, filled = await self._cancel_leftovers(backlog)
        if note:
            await self._notify(f"🧹 补撤旧挂单（{'、'.join(sorted(backlog))}）：{note}", level="warn", mention=filled)

    def _mark_stopped(self, fill: Fill) -> None:
        """Remember the stop-out; partial fills close together add up."""
        qty, price = fill.qty, fill.price
        at = ensure_aware(fill.time)
        prev = self.store.stopped().get(fill.symbol)
        if prev and abs((at - prev["stopped_at"]).total_seconds()) < STOP_MARK_MERGE_SECONDS:
            total = prev["qty"] + fill.qty
            if total > 0:
                price = (prev["qty"] * prev["price"] + fill.qty * fill.price) / total
            qty = total
        self.store.mark_stopped(fill.symbol, at, qty, price)

    async def _on_connection(self, connected: bool, detail: str) -> None:
        try:
            if connected:
                await self._notify(f"🔌 IBKR 已连接：{detail}")
            else:
                await self._notify(f"🔌 IBKR 断开：{detail}", level="warn")
        except Exception:
            log.exception("connection handler failed")
        finally:
            self._poke_stops()

    # ------------------------------------------------------------------ signals

    async def handle_text(self, text: str, meta: MessageMeta) -> bool:
        """Parse a flattened Discord message; True when it was a signal."""
        try:
            sig = parse_signal(text)
        except Exception as exc:
            await self._fail("解析消息", exc)
            return False
        if sig is None:
            return False
        await self.handle_signal(sig, meta)
        return True

    async def handle_signal(self, sig: Signal, meta: MessageMeta) -> None:
        if self._closed:
            log.warning("engine stopped: signal %s ignored", sig.fingerprint[:8])
            return
        async with self._lock:
            try:
                await self._handle_signal(sig, meta)
            except Exception as exc:
                try:
                    self._record(sig, meta.message_id, sig.timestamp, "error", _signal_summary(sig))
                except Exception:
                    log.exception("could not record failed signal")
                hint = f"信号：{_signal_summary(sig)}\n可能只执行了一部分，请 !status 检查，可用 !sync 对齐"
                await self._fail("处理信号", exc, hint=hint)

    async def _handle_signal(self, sig: Signal, meta: MessageMeta) -> None:
        fp = sig.fingerprint
        if self._seen(sig):
            log.info("duplicate signal %s ignored", fp[:8])
            return
        now = self._now()
        sig_ts = to_ny(ensure_aware(sig.timestamp or meta.received_at))
        summary = _signal_summary(sig)
        age = (now - sig_ts).total_seconds()  # small negative values = clock skew, fine

        # 2. ordering guard
        last = self._kv_time(KV_LAST_SIGNAL_TS)
        if last is not None and sig_ts < last:
            self._record(sig, meta.message_id, sig_ts, "out_of_order", summary)
            await self._notify(
                f"收到一条比已处理信号更旧的信号（{_fmt_ts(sig_ts)} 早于 {_fmt_ts(last)}），已忽略：{summary}",
                level="warn",
            )
            return
        rep = _Report(lines=[f"📩 信号 {_fmt_ts(sig_ts)}（延迟 {max(0.0, age):.0f} 秒）"])
        rep.lines += [f"• {_change_text(c)}" for c in sig.changes] or ["• 持仓更新（无仓位变动）"]
        stored_ts = sig_ts
        if -age > self._t.max_signal_age_seconds:
            # A bogus future timestamp must not block every later signal.
            stored_ts = now
            rep.add(f"⚠️ 信号时间比当前时间晚 {-age:.0f} 秒，请检查时间戳", "warn")
        self.store.set(KV_LAST_SIGNAL_TS, stored_ts)

        # 3. provider portfolio (always, also when paused / stale)
        self._update_portfolio(sig, stored_ts)
        odd = suspicious_lines(sig.raw_text)
        if odd:
            rep.add(f"⚠️ 有 {len(odd)} 行无法识别（持仓明细按未知处理）：{' / '.join(odd[:3])}", "warn")

        # A new signal for a stopped-out symbol ends its stop-out mark — also when it is
        # not traded (paused / stale), so that the advised !sync can follow him back in.
        reentered = self._clear_stopped(sig, sig_ts)

        # 4. paused
        if self.is_paused:
            self._record(sig, meta.message_id, sig_ts, "paused", summary)
            await self._notify(f"⏸️ 已暂停，未执行: {summary}{await self._drop_superseded(sig)}")
            return

        # 5. stale
        if age > self._t.max_signal_age_seconds:
            self._record(sig, meta.message_id, sig_ts, "stale", summary)
            await self._notify(
                f"信号已过期 {age:.0f} 秒，未交易（可用 !sync 对齐）：{summary}{await self._drop_superseded(sig)}",
                level="warn",
                mention=True,
            )
            return

        # 6. intents (a stopped-out symbol with an add / open signal is bought back)
        intents = intents_from_signal(sig, self._ignore)
        for sym in reentered:
            rep.add(f"🔁 {sym} 止损后重新跟入")

        # 7. execute by session
        status = "no_action"
        if intents:
            res, status = await self._dispatch(intents, fp[:8], now)
            rep.merge(res)
        else:
            rep.add("无需交易")

        # 8. record + report (+ consistency check after a regular-hours execution)
        self._record(sig, meta.message_id, sig_ts, status, summary)
        await self._send_report(rep)
        if (
            sig.has_holdings_section
            and status in ("done", "no_action")
            and session_at(now) == Session.REGULAR
            and self.broker.is_connected()
        ):
            await self._check_consistency(rep.qty_after)

    def _seen(self, sig: Signal) -> bool:
        sem = _semantic_key(sig)
        return self.store.seen(sig.fingerprint) or (sem is not None and self.store.seen(sem))

    def _record(self, sig: Signal, message_id: str, ts: datetime | None, status: str, summary: str) -> None:
        """Record the signal under its text fingerprint and its semantic key."""
        self.store.record_signal(sig.fingerprint, message_id, ts, status, summary)
        sem = _semantic_key(sig)
        if sem is not None:
            self.store.record_signal(sem, message_id, ts, status, summary)

    def _clear_stopped(self, sig: Signal, sig_ts: datetime | None) -> list[str]:
        """Clear stop-out marks of the signal's symbols; returns those with an increase.

        Only a signal issued after the stop-out counts (an older one, delivered late,
        does not mean he re-entered).
        """
        stopped = self.store.stopped()
        reentered: list[str] = []
        for ch in sig.changes:
            mark = stopped.get(ch.symbol)
            if mark is None or ch.symbol in self._ignore:
                continue
            if sig_ts is not None and sig_ts <= mark["stopped_at"]:
                continue
            self.store.clear_stopped(ch.symbol)
            stopped.pop(ch.symbol)
            if ch.is_increase:
                reentered.append(ch.symbol)
        return reentered

    async def _drop_superseded(self, sig: Signal) -> str:
        """A newer (untraded) signal makes older pending intents of its symbols obsolete.

        Their leftover pre-market / after-hours limit orders are cancelled too, otherwise
        they could still fill later that day.
        """
        changed = {c.symbol for c in sig.changes} - self._ignore
        dropped = sorted(it.symbol for it, _ in self.store.list_pending() if it.symbol in changed)
        for sym in dropped:
            self.store.delete_pending(sym)
        notes: list[str] = []
        if dropped:
            notes.append(f"已取消被新信号取代的待开盘任务：{'、'.join(dropped)}")
        if changed:
            try:
                note, _ = await self._cancel_leftovers(changed)
            except Exception as exc:
                log.exception("cancel superseded orders failed")
                self._set_leftover_backlog(self._leftover_backlog() | changed)
                note = f"⚠️ 撤销旧挂单失败（稍后自动重试）：{type(exc).__name__}: {exc}"
            if note:
                notes.append(note)
        return "".join(f"\n{n}" for n in notes)

    def _update_portfolio(self, sig: Signal, ts: datetime | None) -> None:
        """Provider weights after this signal: 持仓明细 if present, else last known + changes."""
        if sig.has_holdings_section:
            weights = {h.symbol: h.weight for h in sig.holdings if h.weight > 0}
        else:
            weights = self._provider_weights()
            for ch in sig.changes:
                if ch.to_weight <= 0:
                    weights.pop(ch.symbol, None)
                else:
                    weights[ch.symbol] = ch.to_weight
        self.store.set(
            KV_PORTFOLIO,
            {"weights": weights, "ts": to_iso(ts) if ts else None, "from_holdings": sig.has_holdings_section},
        )

    async def _dispatch(self, intents: Sequence[Intent], key8: str, now: datetime) -> tuple[_Report, str]:
        """Execute intents according to the session; returns (report, signal status)."""
        for it in intents:  # a newer instruction replaces the symbol's pending one
            self.store.delete_pending(it.symbol)
        session = session_at(now)
        if not self.broker.is_connected():
            due = now if session == Session.REGULAR else self._open_due(now)
            self._queue(intents, due)
            rep = _Report()
            rep.add(f"⏳ IBKR 未连接，已排队，最早 {_fmt_ts(due, True)} 连上后执行", "warn")
            return rep, "queued"
        if session == Session.REGULAR:
            return await self._execute_regular(intents, key8), "done"
        if session.is_extended:
            return await self._execute_extended(intents, key8, session), "extended"
        due = self._open_due(now)
        self._queue(intents, due)
        rep = _Report()
        rep.add(f"🌙 休市中，已安排在 {_fmt_ts(due, True)} 开盘执行")
        return rep, "queued"

    def _queue(self, intents: Iterable[Intent], due: datetime) -> None:
        for it in intents:
            self.store.upsert_pending(it, due)

    # ------------------------------------------------------------------ broker snapshot

    async def _gather_context(self, symbols: Iterable[str], intents: Iterable[Intent] = ()) -> _Context:
        """Positions, quotes, account, margin rates and G for the given symbols."""
        started = time.monotonic()
        positions = dict(await self.broker.positions())
        self._last_positions = dict(positions)
        weights = self._provider_weights()
        if not weights:
            weights = {i.symbol: i.target_weight for i in intents if i.target_weight > 0}
        wanted = sorted(set(symbols) | set(weights) | set(positions))
        quotes = dict(await self.broker.quotes(wanted)) if wanted else {}
        raw = await self.broker.account()
        rates = await self._rates(wanted, quotes, raw.net_liquidation)
        # Broker account values can lag minutes behind fills; positions do not.
        account = conservative_account(raw, positions, quotes, rates, self._t)
        gross = compute_gross_target(raw.net_liquidation, weights, rates, self._t)
        return _Context(positions, quotes, account, rates, gross, started, raw)

    async def _rates(
        self, symbols: Iterable[str], quotes: Mapping[str, Quote], nlv: float
    ) -> dict[str, MarginRate]:
        """IBKR whatIf margin rates (concurrently); unknown ones fall back to config defaults."""
        probe_value = RATE_PROBE_MIN_USD
        if math.isfinite(nlv):
            probe_value = max(RATE_PROBE_MIN_USD, RATE_PROBE_NLV_FRACTION * nlv)
        jobs: dict[str, Awaitable[MarginRate | None]] = {}
        for sym in symbols:
            if sym in jobs or self._t.override_rate(sym) is not None:
                continue
            q = quotes.get(sym)
            price = (q.mark() or q.buy_ref()) if q is not None else None
            if not price:
                continue
            qty = max(1, int(probe_value / price))
            jobs[sym] = self.broker.margin_rate(sym, qty, price)
        if not jobs:
            return {}
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        out: dict[str, MarginRate] = {}
        for sym, r in zip(jobs, results, strict=True):
            if isinstance(r, MarginRate):
                out[sym] = r
            elif isinstance(r, BaseException):
                log.warning("margin rate for %s failed: %r", sym, r)
        return out

    def _context_lines(self, ctx: _Context) -> list[str]:
        a, g = ctx.account, ctx.gross
        return [
            f"💰 净资产 {_usd(a.net_liquidation)} | 剩余流动性 {_usd(a.excess_liquidity)}（{a.el_ratio:.1%}）"
            f" | 杠杆 {a.leverage:.2f}x",
            f"📐 100%仓位 ≈ {_usd(g.gross)}（受限于{BINDING_CN.get(g.binding, g.binding)}）",
        ]

    def _resolve(self, intents: Sequence[Intent], ctx: _Context) -> tuple[list[PlannedOrder], list[SkipNote]]:
        """resolve_intents with the !sync band applied only to !sync intents. SELLs first."""
        normal = [i for i in intents if i.signal_key != SYNC_KEY]
        synced = [i for i in intents if i.signal_key == SYNC_KEY]
        orders: list[PlannedOrder] = []
        skips: list[SkipNote] = []
        for group, band in ((normal, 0.0), (synced, SYNC_BAND)):
            if group:
                o, s = resolve_intents(group, ctx.positions, ctx.quotes, ctx.gross.gross, self._t, sync_band=band)
                orders += o
                skips += s
        sells = [o for o in orders if o.side == Side.SELL]
        buys = [o for o in orders if o.side == Side.BUY]
        return sells + buys, skips

    # ------------------------------------------------------------------ order helpers

    async def _wait_done(self, info: OrderInfo, timeout: float) -> OrderInfo:
        """Latest snapshot after waiting up to `timeout` s; the old one if the broker fails."""
        if info.is_done:
            return info
        try:
            latest = await self.broker.wait_for_done(info.order_id, timeout)
        except Exception as exc:
            log.warning("wait for order %s failed: %r", info.order_id, exc)
            return info
        return latest if latest is not None else info

    async def _cancel_and_wait(self, orders: Sequence[OrderInfo]) -> list[OrderInfo]:
        """Cancel the orders and wait; returns the ones still working afterwards."""
        for o in orders:
            try:
                await self.broker.cancel_order(o.order_id)
            except Exception as exc:
                log.warning("cancel %s (%s) failed: %r", o.order_id, o.symbol, exc)
        finals = await asyncio.gather(*(self._wait_done(o, self.cancel_wait_seconds) for o in orders))
        return [f for f in finals if not f.is_done]

    async def _cancel_working(self, symbols: Iterable[str]) -> set[str]:
        """Cancel our non-stop working orders for these symbols; returns symbols NOT cleaned."""
        wanted = set(symbols)
        if not wanted:
            return set()
        working = [o for o in await self.broker.open_orders() if not o.is_stop and not o.is_done and o.symbol in wanted]
        if not working:
            return set()
        log.info("cancelling working orders %s", [(o.order_id, o.symbol) for o in working])
        return {o.symbol for o in await self._cancel_and_wait(working)}

    async def _cancel_stops(self, symbols: Iterable[str], since: float) -> dict[str, str]:
        """Cancel the symbols' stop orders before selling.

        Returns {symbol: reason} for symbols that must NOT be sold now: a stop could
        not be cancelled, or a stop filled since `since` (monotonic) — selling then
        could go short.
        """
        wanted = set(symbols)
        problems: dict[str, str] = {}
        if not wanted:
            return problems
        stops = [o for o in await self.broker.open_orders() if o.is_stop and not o.is_done and o.symbol in wanted]
        finals: list[OrderInfo] = []
        if stops:
            for o in stops:
                try:
                    await self.broker.cancel_order(o.order_id)
                except Exception as exc:
                    log.warning("cancel stop %s (%s) failed: %r", o.order_id, o.symbol, exc)
            finals = list(await asyncio.gather(*(self._wait_done(o, self.cancel_wait_seconds) for o in stops)))
        for before, after in zip(stops, finals, strict=True):
            if not after.is_done:
                problems[before.symbol] = "止损单撤销失败，为避免超卖本次不卖出"
            elif after.filled_qty > before.filled_qty:
                problems.setdefault(before.symbol, "止损单刚刚成交，本次不再卖出")
        for sym in wanted:
            if self._stop_fill_mono.get(sym, -math.inf) >= since - STOP_FILL_GUARD_SECONDS:
                problems.setdefault(sym, "止损单刚刚成交，本次不再卖出")
        return problems

    async def _place_one(self, o: PlannedOrder, req: OrderRequest, rep: _Report) -> OrderInfo | None:
        try:
            return await self.broker.place_order(req)
        except Exception as exc:
            log.exception("place_order failed: %s", req)
            line, level = _result_line(o, None, f"{type(exc).__name__}: {exc}")
            rep.add(line, level, mention=True)
            return None

    async def _place_sells(
        self, pairs: Sequence[tuple[PlannedOrder, OrderRequest]], since: float, rep: _Report
    ) -> list[tuple[PlannedOrder, OrderInfo]]:
        """Cancel stops, clamp to the fresh position, then place the SELLs."""
        if not pairs:
            return []
        problems = await self._cancel_stops({o.symbol for o, _ in pairs}, since)
        fresh = await self.broker.positions()
        placed: list[tuple[PlannedOrder, OrderInfo]] = []
        for o, req in pairs:
            if o.symbol in problems:
                rep.add(f"❌ 卖出 {o.symbol} 跳过：{problems[o.symbol]}", "error", mention=True)
                continue
            pos = fresh.get(o.symbol)
            held = _whole(pos.qty) if pos is not None else 0
            qty = min(o.qty, held)
            if qty <= 0:
                rep.add(f"ℹ️ 卖出 {o.symbol} 跳过：当前已无持仓")
                continue
            if qty < o.qty:
                rep.add(f"⚠️ 卖出 {o.symbol} 按最新持仓调整数量 {o.qty}→{qty}", "warn")
                o, req = replace(o, qty=qty), replace(req, qty=qty)
            info = await self._place_one(o, req, rep)
            if info is not None:
                placed.append((o, info))
        return placed

    async def _place_buys(
        self, pairs: Sequence[tuple[PlannedOrder, OrderRequest]], rep: _Report
    ) -> list[tuple[PlannedOrder, OrderInfo]]:
        placed: list[tuple[PlannedOrder, OrderInfo]] = []
        for o, req in pairs:
            info = await self._place_one(o, req, rep)
            if info is not None:
                placed.append((o, info))
        return placed

    async def _run_market(
        self, orders: Sequence[PlannedOrder], tag: str, since: float, rep: _Report
    ) -> list[tuple[PlannedOrder, OrderInfo]]:
        """Place MKT DAY orders (all one side), wait for them and report the results."""
        if not orders:
            return []
        pairs = [(o, OrderRequest(o.symbol, o.side, o.qty, OrderType.MARKET, tif="DAY", tag=tag)) for o in orders]
        if orders[0].side == Side.SELL:
            placed = await self._place_sells(pairs, since, rep)
        else:
            placed = await self._place_buys(pairs, rep)
        timeout = float(self._t.fill_timeout_seconds)
        finals = await asyncio.gather(*(self._wait_done(info, timeout) for _, info in placed))
        results = [(o, info) for (o, _), info in zip(placed, finals, strict=True)]
        for o, info in results:
            line, level = _result_line(o, info)
            rep.add(line, level, mention=level == "error")
        return results

    @staticmethod
    def _qty_after(
        positions: Mapping[str, Position], results: Iterable[tuple[PlannedOrder, OrderInfo]]
    ) -> dict[str, int]:
        qty = {s: _whole(p.qty) for s, p in positions.items()}
        for o, info in results:
            signed = info.filled_qty if o.side == Side.BUY else -info.filled_qty
            qty[o.symbol] = qty.get(o.symbol, 0) + signed
        return qty

    @staticmethod
    def _add_skips(rep: _Report, skips: Iterable[SkipNote]) -> None:
        for sk in skips:
            rep.add(f"⏭️ {sk.symbol}：{sk.reason}", "warn")

    # ------------------------------------------------------------------ executors

    async def _execute_regular(self, intents: Sequence[Intent], key8: str) -> _Report:
        """Regular hours: cancel old orders → MKT sells → wait → liquidity guard → MKT buys."""
        rep = _Report()
        symbols = {i.symbol for i in intents}
        bad = await self._cancel_working(symbols)
        self._set_leftover_backlog(self._leftover_backlog() - (symbols - bad))
        for sym in sorted(bad):
            rep.add(f"❌ {sym}：旧挂单未能撤销，本次跳过", "error", mention=True)
        active = [i for i in intents if i.symbol not in bad]
        ctx = await self._gather_context(symbols, intents)
        rep.lines.extend(self._context_lines(ctx))
        orders, skips = self._resolve(active, ctx)
        tag = f"{MKT_TAG}:{key8}"

        sells = [o for o in orders if o.side == Side.SELL]
        buys = [o for o in orders if o.side == Side.BUY]
        sell_results = await self._run_market(sells, tag, ctx.started, rep)
        sold = [
            (o.symbol, info.filled_qty, info.avg_fill_price or o.ref_price)
            for o, info in sell_results
            if info.filled_qty > 0
        ]

        buy_results: list[tuple[PlannedOrder, OrderInfo]] = []
        if buys:
            after_sells = estimate_after_sells(ctx.account, sold, ctx.rates, self._t)
            scaled, s = scale_buys_for_liquidity(buys, after_sells, ctx.rates, self._t)
            self._report_scaling(rep, buys, scaled, s)
            buy_results = await self._run_market(scaled, tag, ctx.started, rep)

        self._add_skips(rep, skips)
        rep.qty_after = self._qty_after(ctx.positions, sell_results + buy_results)
        self._poke_stops()
        return rep

    @staticmethod
    def _report_scaling(rep: _Report, buys: Sequence[PlannedOrder], scaled: Sequence[PlannedOrder], s: float) -> None:
        if s >= 1.0:
            return
        kept = {o.symbol: o.qty for o in scaled}
        changes = "、".join(f"{o.symbol} {o.qty}→{kept[o.symbol]}" for o in buys if o.symbol in kept)
        rep.add(f"⚖️ 受剩余流动性/保证金限制，买单按 {math.floor(s * 10000) / 100:g}% 缩放（{changes}）", "warn")
        for o in buys:
            if o.symbol not in kept:
                rep.add(f"⏭️ 买入 {o.symbol} 缩放后数量太小，跳过", "warn")

    async def _execute_extended(self, intents: Sequence[Intent], key8: str, session: Session) -> _Report:
        """PRE / AFTER: marketable limit orders for part of the trade, the rest at the open."""
        rep = _Report()
        now = self._now()
        due = self._open_due(now)
        label = SESSION_CN.get(session, "")
        symbols = {i.symbol for i in intents}
        bad = await self._cancel_working(symbols)
        self._set_leftover_backlog(self._leftover_backlog() - (symbols - bad))
        for sym in sorted(bad):
            rep.add(f"❌ {sym}：旧挂单未能撤销，{label}不下单（开盘再试）", "error", mention=True)
        active = [i for i in intents if i.symbol not in bad]
        ctx = await self._gather_context(symbols, intents)
        rep.lines.extend(self._context_lines(ctx))
        orders, skips = self._resolve(active, ctx)
        ext = split_extended(orders, self._t.extended_hours_buy_fraction, self._t.extended_hours_sell_fraction)
        by_symbol = {i.symbol: i for i in active}
        tag = f"{EXT_TAG}:{key8}"
        buffer = self._t.ext_limit_buffer_pct

        sell_pairs: list[tuple[PlannedOrder, OrderRequest]] = []
        for o in (x for x in ext if x.side == Side.SELL):
            price = extended_limit_price(Side.SELL, ctx.quotes.get(o.symbol), by_symbol[o.symbol].signal_price, buffer)
            if price is None:
                rep.add(f"⏭️ {o.symbol} 无报价，开盘再卖")
                continue
            sell_pairs.append((o, self._ext_request(o, price, tag)))
        placed = await self._place_sells(sell_pairs, ctx.started, rep)

        ext_buys = [x for x in ext if x.side == Side.BUY]
        account = ctx.account
        if ext_buys and placed:
            # Rotation (sell A, buy B): marketable sell limits usually fill within seconds;
            # wait briefly for them and credit what filled, like the regular-hours path.
            if self._t.dry_run:  # simulated limits never fill: assume they would have
                sold = [(o.symbol, o.qty, info.limit_price or o.ref_price) for o, info in placed]
                rep.add("🧪 DRY-RUN：假设盘前/盘后卖单已全部成交，按此计算买入")
            else:
                timeout = min(float(self._t.fill_timeout_seconds), EXT_SELL_WAIT_SECONDS)
                finals = await asyncio.gather(*(self._wait_done(info, timeout) for _, info in placed))
                placed = [(o, info) for (o, _), info in zip(placed, finals, strict=True)]
                sold = [
                    (o.symbol, info.filled_qty, info.avg_fill_price or o.ref_price)
                    for o, info in placed
                    if info.filled_qty > 0
                ]
            account = estimate_after_sells(account, sold, ctx.rates, self._t)
        scaled, s = scale_buys_for_liquidity(ext_buys, account, ctx.rates, self._t)
        self._report_scaling(rep, ext_buys, scaled, s)
        buy_pairs: list[tuple[PlannedOrder, OrderRequest]] = []
        for o in scaled:
            price = extended_limit_price(Side.BUY, ctx.quotes.get(o.symbol), by_symbol[o.symbol].signal_price, buffer)
            if price is None:
                rep.add(f"⏭️ {o.symbol} 无报价，开盘再买")
                continue
            buy_pairs.append((o, self._ext_request(o, price, tag)))
        placed += await self._place_buys(buy_pairs, rep)

        for o, info in placed:
            line, level = _result_line(o, info, label=label)
            rep.add(line, level, mention=level == "error")
        self._add_skips(rep, skips)
        self._queue(intents, due)
        rep.add(f"⏰ 剩余部分将在 {_fmt_ts(due, True)} 开盘市价执行")
        self._poke_stops()
        return rep

    @staticmethod
    def _ext_request(o: PlannedOrder, price: float, tag: str) -> OrderRequest:
        return OrderRequest(
            o.symbol, o.side, o.qty, OrderType.LIMIT, limit_price=price, outside_rth=True, tif="DAY", tag=tag
        )

    # ------------------------------------------------------------------ consistency

    async def _check_consistency(self, qty_after: Mapping[str, int] | None) -> None:
        """Warn (once per distinct mismatch) when our holdings differ from the provider's."""
        try:
            if qty_after is None:
                qty_after = {s: _whole(p.qty) for s, p in (await self.broker.positions()).items()}
            ignore = self._ignore
            provider = {s for s, w in self._provider_weights().items() if w > 0} - ignore
            ours = {s for s, q in qty_after.items() if q > 0} - ignore
            stopped = set(self.store.stopped())
            pending = {i.symbol for i, _ in self.store.list_pending()}
            missing = sorted(provider - ours - stopped - pending)
            extra = sorted(ours - provider - pending)
            parts = []
            if missing:
                parts.append("缺少 " + "、".join(missing))
            if extra:
                parts.append("多出 " + "、".join(extra))
            text = "；".join(parts)
            if text and text != self.store.get(KV_LAST_MISMATCH, ""):
                await self._notify(f"持仓和他不一致：{text}。可用 !sync 对齐", level="warn")
            self.store.set(KV_LAST_MISMATCH, text)
        except Exception:
            log.exception("consistency check failed")

    # ------------------------------------------------------------------ pending scheduler

    async def run_due_pending(self) -> None:
        """Scheduler tick: execute pending intents that are due (regular hours only)."""
        if self._closed:
            return
        async with self._lock:
            try:
                await self._run_due_pending()
            except Exception as exc:
                await self._fail("开盘执行待办", exc)

    async def _run_due_pending(self) -> None:
        if self.is_paused or not self.broker.is_connected():
            return
        now = self._now()
        if session_at(now) != Session.REGULAR:
            return
        pending = self.store.list_pending()
        due = [(it, at) for it, at in pending if at <= now]
        if not due:
            return
        hours = regular_hours(now.date())
        if hours is None:  # unreachable in REGULAR
            return
        cutoff = hours[0] - timedelta(seconds=PENDING_GRACE_SECONDS)
        expired = [it for it, at in due if at < cutoff]
        ready = [it for it, at in due if at >= cutoff]
        for it in expired:
            self.store.delete_pending(it.symbol)
        if expired:
            names = "、".join(f"{it.symbol}（{it.reason or it.direction}）" for it in expired)
            await self._notify(f"过期未执行: {names}（可用 !sync 对齐）", level="warn")
        if not ready:
            return
        for it in ready:  # at-most-once: forget them before trading
            self.store.delete_pending(it.symbol)
        rep = _Report(lines=["🔔 开盘执行"])
        rep.lines += [f"• {it.symbol} {_intent_text(it)}" for it in ready]
        try:
            rep.merge(await self._execute_regular(ready, OPEN_KEY))
        except Exception as exc:
            names = "、".join(it.symbol for it in ready)
            await self._fail("开盘执行", exc, hint=f"以下任务未完成：{names}，请 !status 检查，可用 !sync 对齐")
            return
        await self._send_report(rep)

    # ------------------------------------------------------------------ stop loss

    async def sync_stops(self) -> None:
        """Make broker-side GTC stops match our long positions."""
        if self._closed:
            return
        async with self._lock:
            try:
                await self._sync_stops()
            except Exception as exc:
                await self._fail("同步止损单", exc)

    async def _sync_stops(self) -> None:
        if not self.broker.is_connected():
            return
        await self._retry_leftover_backlog()
        positions = await self.broker.positions()
        open_orders = await self.broker.open_orders()
        working = [o for o in open_orders if not o.is_stop]
        stops = [o for o in open_orders if o.is_stop]
        desired = desired_stops(positions, working, self._t.stop_loss_pct, self._ignore)
        actions = diff_stops(desired, stops)
        if actions.empty:
            return
        by_id = {o.order_id: o for o in stops}
        rep = _Report()
        for oid in actions.cancel:
            old = by_id.get(oid)
            name = f"{old.symbol} {old.remaining}股" if old else oid
            try:
                await self.broker.cancel_order(oid)
                rep.add(f"🛡️ 撤销止损 {name}")
            except Exception as exc:
                log.exception("cancel stop %s failed", oid)
                rep.add(f"❌ 撤销止损 {name} 失败：{exc}", "error", mention=True)
        for oid, want in actions.modify:
            try:
                await self.broker.modify_order(oid, qty=want.qty, stop_price=want.stop_price)
                rep.add(f"🛡️ 止损调整 {want.symbol} {want.qty}股 @{_px(want.stop_price)}")
            except Exception as exc:
                log.exception("modify stop %s failed", oid)
                rep.add(f"❌ 止损调整 {want.symbol} 失败：{exc}", "error", mention=True)
        for want in actions.create:
            req = OrderRequest(
                want.symbol,
                Side.SELL,
                want.qty,
                OrderType.STOP,
                stop_price=want.stop_price,
                outside_rth=False,
                tif="GTC",
                tag=STOP_TAG,
            )
            try:
                info = await self.broker.place_order(req)
            except Exception as exc:
                log.exception("place stop %s failed", want.symbol)
                rep.add(f"❌ 止损 {want.symbol} {want.qty}股 下单失败：{exc}", "error", mention=True)
                continue
            if info.status == OrderStatus.REJECTED:
                rep.add(f"❌ 止损 {want.symbol} 被拒绝：{info.message}", "error", mention=True)
            else:
                rep.add(f"🛡️ 止损 {want.symbol} {want.qty}股 @{_px(want.stop_price)}")
        await self._notify_stop_changes(rep)

    async def _notify_stop_changes(self, rep: _Report) -> None:
        """Identical stop notices (e.g. a stop the broker keeps rejecting) at most hourly."""
        text = rep.text()
        last_text, last_at = self._last_stop_note
        now = time.monotonic()
        if text == last_text and now - last_at < STOP_NOTE_REPEAT_SECONDS:
            log.info("stop sync (repeat, not re-notified): %s", text)
            return
        self._last_stop_note = (text, now)
        await self._send_report(rep)

    # ------------------------------------------------------------------ liquidity / deleverage

    async def check_liquidity(self) -> None:
        """Warn when 剩余流动性 is low; deleverage below the trigger (also when paused)."""
        if self._closed:
            return
        async with self._lock:
            try:
                await self._check_liquidity()
            except Exception as exc:
                await self._fail("检查剩余流动性", exc)

    async def _check_liquidity(self) -> None:
        if not self.broker.is_connected():
            return
        now = self._now()
        if session_at(now) != Session.REGULAR:
            return
        risk = self.cfg.risk
        account = await self.broker.account()
        ratio = account.el_ratio
        if ratio < risk.deleverage_trigger_ratio and risk.deleverage_enabled:
            last = self._kv_time(KV_LAST_DELEVERAGE_TS)
            cooled = last is None or (now - last).total_seconds() >= risk.deleverage_cooldown_seconds
            if cooled and await self._deleverage(now):
                return
        if ratio < self._t.reserve_ratio - LIQUIDITY_WARN_MARGIN:
            last_warn = self._kv_time(KV_LAST_LIQUIDITY_WARN_TS)
            if last_warn is None or (now - last_warn).total_seconds() >= LIQUIDITY_WARN_INTERVAL_SECONDS:
                self.store.set(KV_LAST_LIQUIDITY_WARN_TS, now)
                auto = (
                    f"低于 {risk.deleverage_trigger_ratio:.0%} 会自动降杠杆"
                    if risk.deleverage_enabled
                    else "自动降杠杆已关闭"
                )
                await self._notify(
                    f"⚠️ 剩余流动性偏低：{_usd(account.excess_liquidity)}（{ratio:.1%}，目标 "
                    f"{self._t.reserve_ratio:.0%}，{auto}）| 杠杆 {account.leverage:.2f}x",
                    level="warn",
                )

    async def _deleverage(self, now: datetime) -> bool:
        """Sell the same fraction of every managed long; True if orders were attempted."""
        ctx = await self._gather_context(())
        # Use the broker's own numbers here: the conservative model could oversell.
        orders = deleverage_orders(
            ctx.raw_account, ctx.positions, ctx.quotes, ctx.rates, self._t, self.cfg.risk, self._ignore
        )
        if not orders:
            return False
        self.store.set(KV_LAST_DELEVERAGE_TS, now)
        before = ctx.raw_account
        rep = _Report(level="error", mention=True)
        rep.add(
            f"🚨 剩余流动性 {before.el_ratio:.1%} 低于 {self.cfg.risk.deleverage_trigger_ratio:.0%}，自动按比例降杠杆"
        )
        rep.add(
            f"降杠杆前：净资产 {_usd(before.net_liquidation)} | 剩余流动性 {_usd(before.excess_liquidity)}"
            f"（{before.el_ratio:.1%}）| 杠杆 {before.leverage:.2f}x"
        )
        results = await self._run_market(orders, DLV_TAG, ctx.started, rep)
        sold = [
            (o.symbol, info.filled_qty, info.avg_fill_price or o.ref_price)
            for o, info in results
            if info.filled_qty > 0
        ]
        after = estimate_after_sells(before, sold, ctx.rates, self._t)
        rep.add(
            f"降杠杆后（估算）：剩余流动性 {_usd(after.excess_liquidity)}（{after.el_ratio:.1%}）"
            f"| 杠杆 {after.leverage:.2f}x"
        )
        self._poke_stops()
        await self._send_report(rep)
        return True

    # ------------------------------------------------------------------ !sync

    def _sync_intents(
        self, weights: Mapping[str, float], positions: Mapping[str, Position]
    ) -> tuple[list[Intent], list[SkipNote]]:
        """intents_for_sync, except that a stopped-out symbol is never bought back."""
        stopped = self.store.stopped()
        out: list[Intent] = []
        notes: list[SkipNote] = []
        for it in intents_for_sync(weights, positions, self._ignore):
            if it.symbol not in stopped:
                out.append(it)
                continue
            pos = positions.get(it.symbol)
            if pos is not None and _whole(pos.qty) > 0:
                out.append(replace(it, direction="sell"))
            if weights.get(it.symbol, 0.0) > 0:
                notes.append(SkipNote(it.symbol, "已止损，不重新买入（等他下次建仓/加仓）"))
        return out, notes

    async def sync_preview(self) -> str:
        """!sync: show the rebalancing plan (confirm within 180 s)."""
        async with self._lock:
            try:
                return await self._sync_preview()
            except Exception as exc:
                await self._fail("!sync 预览", exc)
                return f"🚨 !sync 预览失败：{type(exc).__name__}: {exc}"

    async def _sync_preview(self) -> str:
        pf = self._portfolio()
        if pf is None:
            return "还没有他的持仓数据，等下一条信号"
        if not self.broker.is_connected():
            return "IBKR 未连接，无法预览"
        weights = self._provider_weights()
        now = self._now()
        ctx = await self._gather_context(weights)
        intents, notes = self._sync_intents(weights, ctx.positions)
        orders, skips = self._resolve(intents, ctx)
        self._sync_preview_at = now

        ts = self._pf_time(pf)
        src = "持仓明细" if pf.get("from_holdings") else "仓位变动推算"
        lines = [f"🔄 同步预览（按他 {_fmt_ts(ts) if ts else '?'} 的持仓，来源：{src}）"]
        if not pf.get("from_holdings"):
            lines.append("⚠️ 他的持仓是从仓位变动推算的，可能不完整，请仔细核对")
        lines.extend(self._context_lines(ctx))
        if orders:
            for o in orders:
                lines.append(
                    f"{_side_cn(o.side)} {o.symbol} {o.qty}股 ≈{_usd(o.qty * o.ref_price)}"
                    f"（{o.current_qty}→{o.target_qty}）"
                )
        else:
            lines.append(f"✅ 已与他的持仓一致（差额小于 {SYNC_BAND:.0%} 的忽略），无需调整")
        for sk in notes + skips:
            lines.append(f"⏭️ {sk.symbol}：{sk.reason}")
        session = session_at(now)
        if session.is_extended:
            lines.append(f"当前{SESSION_CN[session]}：确认后先挂限价单（买入先下一部分），其余开盘市价执行")
        elif session == Session.CLOSED:
            lines.append("当前休市：确认后在下一个开盘执行")
        if any(o.side == Side.BUY for o in orders):
            lines.append("（执行时按最新价格和剩余流动性重新核算买入股数）")
        if orders:
            lines.append(f"{SYNC_PREVIEW_TTL_SECONDS} 秒内发送 !sync confirm 执行")
        return "\n".join(lines)

    async def sync_confirm(self) -> str:
        """!sync confirm: execute the rebalancing (needs a fresh preview)."""
        if self._closed:
            return "交易引擎已停止"
        async with self._lock:
            try:
                return await self._sync_confirm()
            except Exception as exc:
                await self._fail("!sync 执行", exc, hint="可能只执行了一部分，请 !status 检查")
                return f"🚨 !sync 执行失败：{type(exc).__name__}: {exc}"

    async def _sync_confirm(self) -> str:
        now = self._now()
        at = self._sync_preview_at
        if at is None or not 0 <= (now - at).total_seconds() <= SYNC_PREVIEW_TTL_SECONDS:
            return "请先发送 !sync 预览"
        self._sync_preview_at = None
        if self.is_paused:
            return "⏸️ 跟单已暂停，先 !resume 再同步"
        if not self.broker.is_connected():
            return "IBKR 未连接，无法同步"
        weights = self._provider_weights()
        positions = await self.broker.positions()
        intents, notes = self._sync_intents(weights, positions)
        rep = _Report(lines=["🔄 同步执行"])
        if intents:
            res, _ = await self._dispatch(intents, SYNC_KEY, now)
            rep.merge(res)
        else:
            rep.add("无需调整")
        self._add_skips(rep, notes)
        if self._t.dry_run:
            rep.lines.insert(0, "🧪 DRY-RUN 模拟（未真实下单）")
        text = rep.text()
        if self.cfg.discord.command_channel_id != self.cfg.discord.log_channel_id:
            await self._notify(text, level=rep.level, mention=rep.mention)
        return text

    # ------------------------------------------------------------------ !cancel

    async def cancel_all(self) -> str:
        """Cancel every working non-stop bot order and clear the pending intents."""
        async with self._lock:
            try:
                return await self._cancel_all()
            except Exception as exc:
                await self._fail("!cancel", exc)
                return f"🚨 !cancel 失败：{type(exc).__name__}: {exc}"

    async def _cancel_all(self) -> str:
        n_pending = len(self.store.list_pending())
        self.store.clear_pending()
        if not self.broker.is_connected():
            return f"已清空 {n_pending} 个待开盘任务；IBKR 未连接，挂单未撤销"
        working = [o for o in await self.broker.open_orders() if not o.is_stop and not o.is_done]
        still = await self._cancel_and_wait(working)
        self._poke_stops()
        text = f"🧹 已撤销 {len(working) - len(still)} 个挂单（止损单保留），清空 {n_pending} 个待开盘任务"
        if still:
            text += "\n❌ 未能撤销：" + "、".join(f"{o.symbol}({o.order_id})" for o in still)
        return text

    # ------------------------------------------------------------------ !status

    async def status_text(self) -> str:
        """Human readable status; broker errors are shown instead of raised."""
        try:
            return await self._status_text()
        except Exception as exc:
            log.exception("status_text failed")
            return f"🚨 读取状态失败：{type(exc).__name__}: {exc}"

    async def _status_text(self) -> str:
        now = self._now()
        connected = self.broker.is_connected()
        mode = "DRY-RUN（模拟）" if self._t.dry_run else "实盘"
        follow = "已暂停" if self.is_paused else "运行中"
        conn = "已连接" if connected else "未连接"
        session = SESSION_CN[session_at(now)]
        lines = [
            "📊 跟单状态",
            f"模式：{mode} | 跟单：{follow} | IBKR：{conn} | 时段：{session}（{_fmt_ts(now)} 美东）",
        ]
        if connected:
            try:
                lines.extend(await self._status_account_lines())
            except Exception as exc:
                log.exception("status: broker read failed")
                lines.append(f"⚠️ 读取账户/持仓失败：{type(exc).__name__}: {exc}")
        pending = self.store.list_pending()
        if pending:
            lines.append("⏰ 待开盘执行：")
            lines += [f"  {it.symbol} {_intent_text(it)}，{_fmt_ts(due, True)}" for it, due in pending]
        stopped = self.store.stopped()
        if stopped:
            lines.append("🛑 已止损（等他再次建仓/加仓才跟入）：")
            lines += [
                f"  {sym} {_fmt_ts(v['stopped_at'])} {v['qty']}股 @{_px(v['price'])}" for sym, v in stopped.items()
            ]
        lines.extend(self._status_provider_lines())
        return "\n".join(lines)

    async def _status_account_lines(self) -> list[str]:
        ctx = await self._gather_context(())
        open_orders = await self.broker.open_orders()
        lines = self._context_lines(ctx)
        gross = ctx.gross.gross
        stops: dict[str, OrderInfo] = {}
        for o in open_orders:
            if o.is_stop:
                stops.setdefault(o.symbol, o)
        if ctx.positions:
            lines.append("📦 持仓：")
            for sym, p in sorted(ctx.positions.items()):
                q = ctx.quotes.get(sym)
                mark = (q.mark() if q is not None else None) or p.avg_cost
                value = p.qty * mark
                weight = f"{value / gross:.1%}" if gross > 0 else "-"
                stop = stops.get(sym)
                if sym in self._ignore:
                    stop_txt = "（忽略，不管理）"
                elif stop is not None:
                    stop_txt = f" 止损 {stop.remaining}股@{_px(stop.stop_price)}"
                else:
                    stop_txt = " 无止损"
                lines.append(
                    f"  {sym} {_qty(p.qty)}股 均价 {_px(p.avg_cost)} 市值 {_usd(value)}（{weight}）{stop_txt}"
                )
        else:
            lines.append("📦 持仓：无")
        working = [o for o in open_orders if not o.is_stop]
        if working:
            lines.append("📝 挂单：")
            for o in working:
                price = f" @{_px(o.limit_price)}" if o.limit_price else ""
                ext = " 盘前盘后" if o.outside_rth else ""
                lines.append(
                    f"  {_side_cn(o.side)} {o.symbol} {o.filled_qty}/{o.qty} {o.order_type.value}{price}{ext} [{o.tag}]"
                )
        return lines

    def _status_provider_lines(self) -> list[str]:
        pf = self._portfolio()
        if pf is None:
            return ["👤 他的持仓：未知（等下一条信号）"]
        weights = sorted(self._provider_weights().items(), key=lambda kv: -kv[1])
        text = "、".join(f"{s} {_pct(w)}" for s, w in weights) or "空仓"
        ts = self._pf_time(pf)
        src = "" if pf.get("from_holdings") else "，由仓位变动推算"
        lines = [f"👤 他的持仓（{_fmt_ts(ts) if ts else '?'}{src}）：{text}"]
        last = self._kv_time(KV_LAST_SIGNAL_TS)
        if last is not None:
            lines.append(f"最近信号：{_fmt_ts(last)}")
        return lines

    # ------------------------------------------------------------------ bootstrap

    async def bootstrap_from_text(self, text: str) -> bool:
        """Learn the provider portfolio from a history message.

        Trades only a fresh signal that was posted while the bot was restarting.
        """
        if self._closed:
            return False
        async with self._lock:
            if self._closed:
                return False
            try:
                return await self._bootstrap(text)
            except Exception as exc:
                await self._fail("读取历史信号", exc)
                return False

    async def _bootstrap(self, text: str) -> bool:
        sig = parse_signal(text)
        if sig is None:
            return False
        if self._seen(sig):
            return True
        ts = to_ny(ensure_aware(sig.timestamp)) if sig.timestamp else None
        pf = self._portfolio()
        stored_ts = self._pf_time(pf)
        last = self._kv_time(KV_LAST_SIGNAL_TS)
        if not self._boot_snapshot_taken:
            self._boot_snapshot_taken = True
            self._boot_last_signal = last
        # Newer than everything handled before this process started (so never on a
        # first run with an empty DB, even after earlier history moved last_signal_ts)
        # and newer than everything handled so far: posted while the bot was offline.
        boot = self._boot_last_signal
        missed = (
            ts is not None
            and boot is not None
            and ts > boot
            and (last is None or ts > last)
            and bool(sig.changes)
        )
        if missed and ts is not None:
            now = self._now()
            if (now - ts).total_seconds() <= self._t.max_signal_age_seconds:
                # Posted while the bot was restarting and still fresh: trade it like a
                # live signal (re-trading is idempotent because targets are weights).
                log.info("bootstrap: fresh unprocessed signal %s, handling it live", sig.fingerprint[:8])
                await self._handle_signal(sig, MessageMeta("history", 0, now))
                return True
        newer = pf is None if ts is None else (stored_ts is None or ts > stored_ts)
        note = ""
        if missed:
            # Same clean-up as a live stale signal: his newer instruction supersedes
            # pending ones (and their leftover orders); a later add re-enables !sync.
            self._clear_stopped(sig, ts)
            note = await self._drop_superseded(sig)
        if newer:
            self._update_portfolio(sig, ts)
            if ts is not None and (last is None or ts > last):
                self.store.set(KV_LAST_SIGNAL_TS, ts)
            log.info("bootstrap: provider portfolio from signal %s (%s)", sig.fingerprint[:8], ts)
        # Mark as seen so the same message can never be traded later.
        self._record(sig, "", ts, "bootstrap", "")
        if missed and ts is not None:
            await self._notify(
                f"机器人离线期间错过信号 {_fmt_ts(ts)}：{_signal_summary(sig)}（未自动执行，可用 !sync 对齐）{note}",
                level="warn",
                mention=True,
            )
        return True

    @staticmethod
    def _pf_time(pf: Mapping[str, Any] | None) -> datetime | None:
        return _parse_time(pf.get("ts"), "provider_portfolio.ts") if pf else None
