# copybot — design spec

Discord copy-trading bot: a signal provider posts portfolio updates in Discord; the user
forwards them (with their own account) into a channel of their own server; this bot reads
that channel, mirrors the provider's position weights on an IBKR margin account, and
reports everything back to Discord.

This document is the contract between modules. `copybot/models.py`, `copybot/config.py`
and `copybot/broker/base.py` already exist and are authoritative for types.

## 1. Signal format (input)

```
组合收益
10月收益：3.4%
总收益：168.3%
仓位变动
建仓 WDC | 仓位：0.0% → 100.0% | 成交价：422.6
清仓 BE | 仓位：50.0% → 0.0% | 成交价：无
清仓 ARM | 仓位：50.0% → 0.0% | 成交价：无
持仓明细
WDC | 仓位：100.0% | 成本：422.6 | 盈亏：+0.1%
(2026-10-02 09:50:59 美东)
```

* Change line: `<action> <SYMBOL> | 仓位：<from>% → <to>% | 成交价：<price|无>`.
  Actions seen/expected: 建仓, 加仓, 减仓, 清仓 (accept any CJK word). Direction is decided by
  the numbers (to > from = increase), never by the action word.
* Holding line: `<SYMBOL> | 仓位：<w>% | 成本：<cost> | 盈亏：<+/-x>%`.
* Timestamp line: `(YYYY-MM-DD HH:MM:SS 美东)`, America/New_York.
* Must tolerate: full/half-width colons (`：`/`:`) and parentheses, `→`/`->`/`=>`, extra
  spaces, markdown decoration (`**`, `__`, `` ` ``, `>`), zero-width chars / NBSP, CRLF,
  symbols with `.`/`-` (BRK.B), prices with thousands separators, missing sections.
* Messages may arrive as plain content, as embeds (title/description/fields/footer), or as
  Discord native forwards (`message.message_snapshots[*].content/embeds`). The Discord layer
  flattens all of these into one text before parsing.

## 2. Trading rules (what the user asked for)

1. **Gross target G ("100% 仓位" in dollars)** — computed fresh for every execution:
   ```
   W   = provider portfolio weights after the signal (from 持仓明细, else last known
         portfolio with this signal's changes applied)
   u   = max(0, 1 - ΣW)                    # unallocated part, assume default margin
   M   = Σ w_i·maint_i + u·default_maint    # blended maintenance rate at full deployment
   N   = Σ w_i·init_i  + u·default_init
   G_maint = (1 - reserve_ratio) · NLV / M          # leaves 25% NLV as 剩余流动性
   G_init  = (1 - init_margin_buffer) · NLV / N     # what the broker lets us open
   G_cap   = max_gross_leverage · NLV / max(1, ΣW)
   G = min(G_maint, G_init, G_cap)   (0 if NLV <= 0)
   ```
   Margin rates per symbol: config override → cached IBKR whatIf → config defaults; every
   rate clamped into [min_margin_rate, 1.0].
2. **Per change line** (Intent, always a weight of G):
   * increase (建仓/加仓): `Intent(sym, target_weight=to, direction="buy")`;
     target shares = floor(to·G / buy_ref_price).
   * decrease (减仓/清仓): `Intent(sym, target_weight=to, direction="sell")`;
     target shares = floor(to·G / price), or 0 when to == 0 (no price needed).
     Weight-based (not "sell the same ratio of our shares") so it is idempotent, needs no
     live positions at signal time, and does the right thing when a pre-market entry was
     only half filled.
   * Direction clamp: a buy intent never sells, a sell intent never buys, never go short.
3. **Live liquidity guard (always)**: IBKR pushes account values only every few minutes,
   so the account snapshot is first made conservative (`sizing.conservative_account`):
   margin usage is rebuilt from live positions × marks × margin rates and the stricter of
   reported vs. modelled EL / AF / GPV is used (known, accepted bias: NLV stays as reported,
   so after a rally since IBKR's last push buys are shrunk by roughly that move — fail-safe).
   Then, before buys, estimate post-sell account
   (EL' = EL + Σ sold_value·maint_i, AF' = AF + Σ sold_value·init_i, GPV' = GPV − Σ sold_value)
   and scale ALL buys of the batch by one factor
   `s = clamp(min(room_maint/need_maint, room_init/need_init, room_gross/need_gross), 0, 1)`
   where `room_maint = EL' − reserve·NLV`, `room_init = AF' − init_buffer·NLV`,
   `room_gross = max_gross_leverage·NLV − GPV'`.
4. **Sessions** (America/New_York, NYSE calendar):
   * REGULAR: cancel our working orders for the symbols → sells (MKT) first, wait for fills
     (fill_timeout) → liquidity guard → buys (MKT) → wait → stop sync.
   * PRE / AFTER: sells: `extended_hours_sell_fraction` (=1.0, all) as LMT outside RTH at
     `sell_ref·(1-buffer)`; buys: `extended_hours_buy_fraction` (=0.5) of the buy delta as LMT
     outside RTH at `buy_ref·(1+buffer)`. The *whole* intent is stored as pending, due at the
     next regular open + `open_delay_seconds`; at that time the scheduler cancels the leftover
     extended-hours orders and executes the intent as in REGULAR (re-sized with fresh prices).
     No price → skip the extended part, everything waits for the open.
   * CLOSED (overnight / weekend / holiday): store intents as pending for the next open.
   * Broker disconnected when a signal arrives: store intents as pending (due now in REGULAR,
     next open otherwise); the scheduler runs them once connected.
   * A newer signal for a symbol replaces that symbol's pending intent. When the newer
     signal is not traded (paused / stale / missed while offline) the pending intent is
     dropped and the symbol's leftover `copybot:ext` limit orders are cancelled (also after
     a stop fill). If IBKR is down or a cancel is not confirmed, the symbol goes into a
     persistent backlog (`cancel_leftovers`) that every stop-sync tick retries.
   * Rotation in PRE / AFTER: sells are placed first and waited for (≤ 15 s); their fills are
     credited before the buy leg is sized (dry-run assumes they fill).
5. **Guards**: paused → ignore (but still update provider portfolio); duplicate fingerprint
   → ignore silently; signal timestamp older than the newest processed one → ignore + warn;
   age > max_signal_age_seconds → do not trade, warn (portfolio still updated);
   buy whose ref price > signal price·(1+max_price_deviation_pct) → skip + warn;
   order value < min_order_value_usd → skip (except sells that close the position);
   symbols in ignore_symbols are never touched.
6. **Stop loss**: one broker-side GTC STP SELL per long position, tag `copybot:stop`,
   `qty = position − Σ remaining of our working non-stop SELL orders`,
   `stop_price = round_to_tick(avg_cost·(1 − stop_loss_pct/100))`. Synced every
   `stop_sync_interval_seconds` and right after fills. Before placing any SELL for a symbol,
   cancel its stop first (prevents selling more than we hold); the next sync re-creates it.
   Stop fill → mark symbol "stopped", drop its pending intent, alert with @owner.
   A later increase signal for that symbol re-enters (clears the mark, buys the full target).
   A decrease signal clears the mark.
7. **Deleverage (动态调整融资)**: every `liquidity_check_interval_seconds` during REGULAR:
   if EL/NLV < deleverage_trigger_ratio and cooldown passed → sell the same fraction `s` of
   every managed position, `s = min(1, (reserve·NLV − EL) / Σ_managed value_i·maint_i)`,
   qty_i = ceil(qty_i·s). Runs even when paused (it is protective). Alert with @owner.
8. **!sync**: rebalance every managed symbol to the provider's last known portfolio at the
   current G (`direction="both"`; provider weight 0 for symbols we hold but he
   doesn't). Skip symbols whose |delta value| < 5% of target value (avoid churn).
   Two-step: `!sync` shows the plan, `!sync confirm` within 180 s executes it via the normal
   session rules.

## 3. Module APIs

All modules import types from `copybot.models`. Pure modules (parser, market_hours,
sizing) do no I/O and get "now" as a parameter.

### 3.1 `copybot/parser.py`
```python
NY: ZoneInfo                                  # America/New_York
def normalize_text(text: str) -> str          # strip markdown/zero-width, unify punctuation
def parse_signal(text: str) -> Signal | None  # None if the text is not a signal
                                              # (a signal has ≥1 change line OR a 持仓明细 header)
```
`fingerprint` = sha256 of the normalized text. Weights are fractions (`50.0%` → 0.5).

### 3.2 `copybot/market_hours.py`
```python
NY: ZoneInfo
def is_trading_day(d: date) -> bool
def is_early_close(d: date) -> bool
def regular_hours(d: date) -> tuple[datetime, datetime] | None    # (open, close) tz NY
def session_at(now: datetime) -> Session
def next_regular_open(now: datetime) -> datetime   # today's open if now < today's open on a
                                                   # trading day, else the next trading day's
```
Holidays: `holidays.financial_holidays("NYSE")`. Early close (13:00, after-hours until 17:00):
day after Thanksgiving; Jul 3 and Dec 24 when they are trading days.
PRE 04:00–open, REGULAR open–close, AFTER close–20:00 (17:00 on early-close days).

### 3.3 `copybot/sizing.py` (pure)
```python
def round_to_tick(price: float, side: Side | None = None) -> float
    # ≥ $1: 0.01 tick, < $1: 0.0001. side=BUY rounds up, SELL rounds down, None nearest.
def effective_rate(symbol, rates: dict[str, MarginRate], cfg: TradingConfig) -> MarginRate
    # override → rates[symbol] → default; clamp to [min_margin_rate, 1]
def compute_gross_target(nlv, weights: dict[str, float], rates, cfg) -> GrossTarget
def intents_from_signal(signal: Signal, ignore: set[str]) -> list[Intent]   # last line wins per symbol
def intents_for_sync(provider_weights: dict[str, float], positions, ignore: set[str]) -> list[Intent]
def resolve_intents(intents, positions, quotes: dict[str, Quote], gross: float, cfg,
                    *, sync_band: float = 0.0) -> tuple[list[PlannedOrder], list[SkipNote]]
    # target = 0 if w <= 0 else floor(w·G/price); price: "buy" → buy_ref, "sell" → sell_ref,
    # "both" → mark(); each falls back to mark() then signal_price; no price and w > 0 →
    # SkipNote. delta clamped by direction, never short (sell qty <= current);
    # deviation guard for buys; min_order_value (closing sells exempt); sync_band skips
    # |delta·price| < sync_band·target_value (only when target_value > 0).
def estimate_after_sells(account: AccountSnapshot, sold: list[tuple[str, int, float]],
                         rates, cfg) -> AccountSnapshot        # (symbol, qty, price)
def scale_buys_for_liquidity(buys: list[PlannedOrder], account: AccountSnapshot, rates, cfg)
    -> tuple[list[PlannedOrder], float]                          # scaled orders, factor s
def split_extended(orders: list[PlannedOrder], buy_fraction, sell_fraction) -> list[PlannedOrder]
    # qty = floor(qty·fraction); drop zero-qty
def extended_limit_price(side: Side, quote: Quote | None, signal_price: float | None,
                         buffer_pct: float) -> float | None
    # BUY: buy_ref·(1+b) rounded up to tick; SELL: sell_ref·(1-b) rounded down; fallback
    # signal_price; None if no price at all.
def stop_price_for(avg_cost: float, stop_loss_pct: float) -> float
def desired_stops(positions, working_orders: list[OrderInfo], stop_loss_pct, ignore) -> dict[str, DesiredStop]
def diff_stops(desired: dict[str, DesiredStop], existing: list[OrderInfo]) -> StopActions
    # existing = our working stop orders. Per symbol keep ONE: modify if qty differs or
    # |stop_price diff| >= 0.005; cancel duplicates and stops for symbols not desired.
def deleverage_orders(account, positions, quotes, rates, cfg, risk: RiskConfig, ignore) -> list[PlannedOrder]
```

### 3.4 `copybot/store.py` (sqlite3, stdlib, sync calls are fine)
```python
class Store:
    def __init__(self, path: str)            # creates parent dir + tables; ":memory:" ok
    # signals
    def seen(self, fingerprint: str) -> bool
    def record_signal(self, fingerprint, message_id: str, signal_ts: datetime | None,
                      status: str, summary: str) -> None
    # key/value (JSON)
    def get(self, key: str, default=None); def set(self, key: str, value) -> None
    # pending intents (one per symbol)
    def upsert_pending(self, intent: Intent, due_at: datetime) -> None
    def list_pending(self) -> list[tuple[Intent, datetime]]
    def delete_pending(self, symbol: str) -> None
    def clear_pending(self) -> None
    # stopped-out symbols
    def mark_stopped(self, symbol: str, at: datetime, qty: int, price: float) -> None
    def clear_stopped(self, symbol: str) -> None
    def stopped(self) -> dict[str, dict]
    # processed fills (dedupe by exec_id)
    def fill_seen(self, exec_id: str) -> bool; def record_fill(self, fill: Fill) -> None
```
KV keys used by the engine: `paused` (bool), `last_signal_ts` (iso str),
`provider_portfolio` ({"weights": {...}, "ts": iso, "from_holdings": bool}),
`last_mismatch` (str), `last_deleverage_ts` (iso str).
Datetimes are stored as ISO strings and returned tz-aware.

### 3.5 `copybot/broker/ibkr.py` — `class IBKRBroker(Broker)` on `ib_async`
* `__init__(cfg: IBKRConfig, aliases: dict[str, str], margin_cache_seconds: int)`.
* Symbol mapping: alias → else `.`→space (`BRK.B` → `BRK B`); reverse for positions.
* Contracts `Stock(sym, "SMART", "USD")`, qualified once and cached.
* `account()`: from `ib.accountValues(account)` tags NetLiquidation, ExcessLiquidity,
  AvailableFunds, FullMaintMarginReq, FullInitMarginReq, GrossPositionValue (base currency,
  non-segmented tags); convert to USD with tag `ExchangeRate`/currency `USD` when base ≠ USD.
* `quotes()`: `reqTickersAsync` with timeout; NaN/≤0 → None.
* `margin_rate()`: `whatIfOrderAsync(contract, MarketOrder("BUY", qty))`, rate =
  change / (qty·price) for init & maint; invalid → None; cached `margin_cache_seconds`.
* Orders: `MarketOrder` / `LimitOrder` / `StopOrder` with `tif`, `outsideRth`, `orderRef=tag`,
  `account`. `order_id` = `str(order.orderId)`. Status map: Filled→FILLED,
  Cancelled/ApiCancelled→CANCELLED, Inactive→REJECTED (message from trade.log), else PENDING.
* `open_orders()`: `ib.openTrades()` filtered by tag prefix.
* Fills: `ib.execDetailsEvent` (live only) → `Fill` → `_emit_fill`, only for our tags.
* `run_forever()`: reconnect loop (every 10 s) + `_emit_connection` on changes.
* All ib_async exceptions / timeouts are wrapped into `BrokerError`.

### 3.6 `copybot/broker/dryrun.py` — `class DryRunBroker(Broker)`
Wraps a real broker: reads delegate; writes are simulated. MKT orders → immediately
FILLED (filled_qty = qty, avg_fill_price None); LMT/STP orders → kept as virtual working
orders (returned by `open_orders()` together with the real ones, modifiable/cancellable),
never filled. Order ids are prefixed `dry-`. Never emits fills.

### 3.7 `copybot/notifier.py`
```python
class Notifier(Protocol):
    async def send(self, text: str, *, level: str = "info", mention: bool = False) -> None
class LogNotifier       # logs only (tests, before Discord is ready)
class DiscordNotifier   # posts to log channel; splits > 1900 chars; mention → "<@owner>"
                        # prefix; level icons ℹ️/⚠️/🚨; never raises (logs failures)
```

### 3.8 `copybot/engine.py` — `class Engine`
```python
Engine(cfg: AppConfig, broker: Broker, store: Store, notifier: Notifier,
       clock: Callable[[], datetime] = lambda: datetime.now(NY))
async start()                          # register fill/connection handlers, start loops
async stop()
async handle_text(text: str, meta: MessageMeta) -> bool   # parse; returns True if signal
async handle_signal(sig: Signal, meta: MessageMeta) -> None
async bootstrap_from_text(text) -> bool   # update provider portfolio only, never trades
async run_due_pending() -> None        # scheduler tick (also called by tests)
async sync_stops() -> None
async check_liquidity() -> None
async sync_preview() -> str; async sync_confirm() -> str
async cancel_all() -> str              # cancel working non-stop bot orders + clear pending
def pause(); def resume(); async status_text() -> str
```
`MessageMeta` lives in `copybot.models` (message_id: str, author_id: int, received_at: datetime).
One `asyncio.Lock` serialises every trading action (signals, scheduler, sync, deleverage,
stop sync). The fill handler never takes the lock; it records, notifies and pokes the
stop-sync loop.

### 3.9 `copybot/discord_bot.py`
`extract_text(message) -> str` (content + embeds + message_snapshots),
`class CopyBotClient(discord.Client)`: intents default + message_content. Signal channel +
allowed authors → `engine.handle_text`. Command channel + owner_ids → commands
`!status !pause !resume !sync [confirm] !cancel !help`. `on_ready` → bootstrap provider
portfolio from the last 20 messages of the signal channel. A history signal that was never
processed, is newer than the last signal handled before this process started and is still
fresh (≤ max_signal_age_seconds, i.e. posted during a restart) is traded like a live signal;
older missed ones only warn (and supersede pending intents like a stale signal). On a first
run (empty DB) history is never traded. A stop-out mark is only cleared by a signal issued
after the stop-out.

### 3.10 `copybot/app.py` / `copybot/__main__.py`
Load config, logging (stdout + rotating file), Store, IBKRBroker (wrapped in DryRunBroker
when dry_run), DiscordNotifier, Engine, CopyBotClient; run broker.run_forever(),
engine.start() and the Discord client concurrently; clean shutdown on SIGTERM.
