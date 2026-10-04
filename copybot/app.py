"""Process entry point (DESIGN.md §3.10): wire config, storage, broker, Discord and the engine.

``main()`` loads the config, sets up logging (stdout + rotating file) and runs ``run(cfg)``
until SIGINT / SIGTERM or until the Discord client stops, then shuts down cleanly:
engine loops first (no new trades), then Discord, then the broker, then the database.
Exit code 0 after a stop signal, 1 when a component failed (so a supervisor restarts it).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .broker.base import Broker
from .broker.dryrun import DryRunBroker
from .broker.ibkr import IBKRBroker
from .config import AppConfig, discord_token, load_config
from .discord_bot import CopyBotClient, run_client
from .engine import Engine
from .notifier import DiscordNotifier
from .store import Store

log = logging.getLogger("copybot")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_FILE = "copybot.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5
SHUTDOWN_STEP_SECONDS = 40.0  # per step; engine.stop() itself waits up to 30 s for a trade
_HANDLER_MARK = "_copybot_handler"


def setup_logging(log_dir: str, level: int = logging.INFO) -> None:
    """Log to stdout and to <log_dir>/copybot.log (5 MB × 5). Safe to call twice."""
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for h in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(h)
        h.close()
    fmt = logging.Formatter(LOG_FORMAT)
    stream = logging.StreamHandler(sys.stdout)
    file = RotatingFileHandler(
        Path(log_dir) / LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    for h in (stream, file):
        h.setFormatter(fmt)
        setattr(h, _HANDLER_MARK, True)
        root.addHandler(h)


def build_broker(cfg: AppConfig) -> Broker:
    """IBKR adapter, wrapped in the dry-run simulator when trading.dry_run is on."""
    real = IBKRBroker(cfg.ibkr, cfg.trading.symbol_aliases, cfg.trading.margin_cache_seconds)
    return DryRunBroker(real) if cfg.trading.dry_run else real


def _install_signal_handlers(stop: asyncio.Event) -> list[signal.Signals]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):  # e.g. Windows / not main thread
            log.debug("cannot install handler for %s", sig)
    return installed


def _remove_signal_handlers(installed: list[signal.Signals]) -> None:
    loop = asyncio.get_running_loop()
    for sig in installed:
        with contextlib.suppress(Exception):
            loop.remove_signal_handler(sig)


async def _step(name: str, coro: object) -> None:
    """Run one shutdown step with a timeout; log (never raise) failures."""
    try:
        await asyncio.wait_for(coro, SHUTDOWN_STEP_SECONDS)  # type: ignore[arg-type]
    except Exception:
        log.exception("shutdown step %s failed", name)


async def run(cfg: AppConfig) -> None:
    """Run the bot until SIGINT / SIGTERM or until the Discord client stops."""
    token = discord_token()  # fail fast, before touching the broker
    store = Store(cfg.storage.db_path)
    broker = build_broker(cfg)
    client = CopyBotClient(cfg)
    notifier = DiscordNotifier(client, cfg.discord.log_channel_id, cfg.discord.owner_ids)
    engine = Engine(cfg, broker, store, notifier)
    client.engine = engine
    client.notifier = notifier

    stop = asyncio.Event()
    installed = _install_signal_handlers(stop)
    tasks: list[asyncio.Task] = []
    failure: str | None = None  # set when a component ended without a stop signal
    log.info(
        "copybot starting: mode=%s ibkr=%s:%s db=%s",
        "DRY-RUN" if cfg.trading.dry_run else "LIVE",
        cfg.ibkr.host,
        cfg.ibkr.port,
        cfg.storage.db_path,
    )
    try:
        await engine.start()
        tasks = [
            asyncio.create_task(broker.run_forever(), name="ibkr"),
            asyncio.create_task(run_client(client, token), name="discord"),
        ]
        stopper = asyncio.create_task(stop.wait(), name="stop-signal")
        done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
        stopper.cancel()
        for task in done:
            if task is stopper:
                log.info("stop signal received")
                continue
            if task.cancelled():
                failure = f"{task.get_name()} was cancelled"
            elif task.exception() is not None:
                failure = f"{task.get_name()} failed: {task.exception()!r}"
                log.error("task %s failed", task.get_name(), exc_info=task.exception())
            else:
                failure = f"{task.get_name()} ended unexpectedly"
            log.error("%s", failure)
    finally:
        log.info("shutting down")
        await _step("engine", engine.stop())
        await _step("discord", client.close())
        await _step("broker", broker.disconnect())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _remove_signal_handlers(installed)
        store.close()
        log.info("stopped")
    if failure is not None:  # non-zero exit so a supervisor restarts the bot
        raise RuntimeError(failure)


def main() -> None:
    """Console entry point: ``python -m copybot``."""
    cfg = load_config()
    setup_logging(cfg.storage.log_dir)
    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run(cfg))
    except Exception as exc:
        log.critical("copybot stopped: %s", exc, exc_info=True)
        raise SystemExit(1) from exc
