"""Configuration: YAML file + environment variables for secrets.

The YAML path comes from $COPYBOT_CONFIG (default ./config.yaml).
Secrets never live in the YAML: the Discord token is read from $DISCORD_TOKEN.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import MarginRate


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscordConfig(_Strict):
    signal_channel_id: int  # 你转发信号进来的频道
    # Only messages from these user / webhook ids are treated as signals.
    # Empty list = anyone who can post in the channel (a warning is logged at startup).
    allowed_signal_author_ids: list[int] = Field(default_factory=list)
    log_channel_id: int  # 机器人播报下单 / 成交 / 报警的频道
    control_channel_id: int | None = None  # 接收 ! 命令的频道, 默认 = log_channel_id
    owner_ids: list[int] = Field(default_factory=list)  # 可执行命令的人; 报警时 @ 他们

    @property
    def command_channel_id(self) -> int:
        return self.control_channel_id or self.log_channel_id


class IBKRConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = 4004  # gnzsnz/ib-gateway: 4004 = paper, 4003 = live
    client_id: int = 17
    account: str = ""  # empty = first managed account
    market_data_type: int = 1  # 1 = live, 3 = delayed (only if you have no subscription)
    connect_timeout_seconds: float = 15.0


class MarginOverride(_Strict):
    init: float
    maint: float


class TradingConfig(_Strict):
    dry_run: bool = True  # True = 只算不下单 (先用 paper 账户 / dry-run 验证!)

    # ---- 融资 / 仓位基数 (G) ----
    reserve_ratio: float = 0.25  # 目标剩余流动性 = 净资产 × 25%
    max_gross_leverage: float = 1.9  # 总持仓市值 / 净资产 上限 (Reg T 隔夜上限约 2.0)
    init_margin_buffer: float = 0.02  # 开仓时可用资金至少保留 净资产 × 2%
    default_init_margin: float = 0.50  # whatIf 拿不到时的默认初始保证金比例
    default_maint_margin: float = 0.30  # whatIf 拿不到时的默认维持保证金比例
    min_margin_rate: float = 0.10  # whatIf 结果低于此值视为异常, 夹到此值
    margin_overrides: dict[str, MarginOverride] = Field(default_factory=dict)
    margin_cache_seconds: int = 1800

    # ---- 盘前 / 盘后 ----
    extended_hours_buy_fraction: float = 0.5  # 盘前买入先下 50%, 其余开盘市价
    extended_hours_sell_fraction: float = 1.0  # 盘前卖出全部下 (你的选择)
    ext_limit_buffer_pct: float = 0.5  # 盘前限价 = 卖一价 × (1 + 0.5%) / 买一价 × (1 - 0.5%)
    open_delay_seconds: int = 5  # 开盘后几秒执行剩余部分

    # ---- 下单保护 ----
    max_price_deviation_pct: float = 10.0  # 买入参考价比他的成交价高出超过此值 → 不追
    max_signal_age_seconds: int = 600  # 信号时间戳比现在早超过此值 → 不交易, 只报警
    min_order_value_usd: float = 100.0  # 小于此金额的买单 / 非清仓卖单忽略
    fill_timeout_seconds: int = 60  # 市价单等待成交的最长时间

    # ---- 止损 ----
    stop_loss_pct: float = 10.0  # 按持仓均价下方 10% 挂 GTC 止损单 (券商端)
    stop_sync_interval_seconds: int = 30

    # ---- 其他 ----
    ignore_symbols: list[str] = Field(default_factory=list)  # 机器人永远不碰的股票
    symbol_aliases: dict[str, str] = Field(default_factory=dict)  # 他的代码 → IBKR 代码

    @field_validator("ignore_symbols")
    @classmethod
    def _upper_list(cls, v: list[str]) -> list[str]:
        return [s.strip().upper() for s in v]

    @field_validator("symbol_aliases", "margin_overrides")
    @classmethod
    def _upper_keys(cls, v: dict) -> dict:
        return {k.strip().upper(): val for k, val in v.items()}

    @model_validator(mode="after")
    def _check_ranges(self) -> "TradingConfig":
        if not 0 <= self.reserve_ratio < 1:
            raise ValueError("reserve_ratio must be in [0, 1)")
        if self.max_gross_leverage <= 0:
            raise ValueError("max_gross_leverage must be > 0")
        for name in ("extended_hours_buy_fraction", "extended_hours_sell_fraction"):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must be in [0, 1]")
        if not 0 < self.stop_loss_pct < 100:
            raise ValueError("stop_loss_pct must be in (0, 100)")
        if not 0 < self.default_init_margin <= 1 or not 0 < self.default_maint_margin <= 1:
            raise ValueError("default margin rates must be in (0, 1]")
        return self

    def default_rate(self) -> MarginRate:
        return MarginRate(self.default_init_margin, self.default_maint_margin, "default")

    def override_rate(self, symbol: str) -> MarginRate | None:
        o = self.margin_overrides.get(symbol.upper())
        return MarginRate(o.init, o.maint, "override") if o else None


class RiskConfig(_Strict):
    # 剩余流动性 / 净资产 跌破 trigger 时, 按比例减仓直到恢复到 reserve_ratio
    deleverage_enabled: bool = True
    deleverage_trigger_ratio: float = 0.15
    deleverage_cooldown_seconds: int = 900
    liquidity_check_interval_seconds: int = 60


class StorageConfig(_Strict):
    db_path: str = "data/copybot.db"
    log_dir: str = "logs"


class AppConfig(_Strict):
    discord: DiscordConfig
    ibkr: IBKRConfig = Field(default_factory=IBKRConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    @model_validator(mode="after")
    def _check_cross(self) -> "AppConfig":
        if self.risk.deleverage_trigger_ratio >= self.trading.reserve_ratio:
            raise ValueError("risk.deleverage_trigger_ratio must be below trading.reserve_ratio")
        return self


def load_config(path: str | os.PathLike | None = None) -> AppConfig:
    path = Path(path or os.environ.get("COPYBOT_CONFIG", "config.yaml"))
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    # Optional env overrides for deployment convenience.
    ib = raw.setdefault("ibkr", {}) or {}
    raw["ibkr"] = ib
    if os.environ.get("IBKR_HOST"):
        ib["host"] = os.environ["IBKR_HOST"]
    if os.environ.get("IBKR_PORT"):
        ib["port"] = int(os.environ["IBKR_PORT"])
    if os.environ.get("COPYBOT_DRY_RUN"):
        raw.setdefault("trading", {})
        raw["trading"] = raw["trading"] or {}
        raw["trading"]["dry_run"] = os.environ["COPYBOT_DRY_RUN"].strip().lower() in ("1", "true", "yes")
    return AppConfig.model_validate(raw)


def discord_token() -> str:
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set")
    return token
