"""Discord layer (DESIGN.md §3.9).

- Signal channel: messages from allowed authors are flattened (`extract_text`) and passed
  to ``engine.handle_text``. Signals are handled one at a time in arrival order.
- Command channel: owners can run ``!status !pause !resume !sync [confirm] !cancel !help``.
- ``on_ready`` (first time only): flush queued notifications, announce, then bootstrap the
  provider portfolio from the last 20 messages of the signal channel (never trades).

The engine is injected (``client.engine = ...``); it is only referenced for typing here.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING, Any, Iterable

import discord

from .config import AppConfig
from .models import MessageMeta
from .notifier import Notifier, split_message

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)

BOOTSTRAP_HISTORY_LIMIT = 20
# discord.py may dispatch on_message before on_ready; live signals wait (at most this
# long) for the bootstrap so an older history message can never overwrite a newer signal.
BOOTSTRAP_WAIT_SECONDS = 30.0
COMMAND_PREFIXES = ("!", "！")  # also accept the full-width "！" typed by Chinese IMEs

HELP_TEXT = """📖 跟单机器人命令
!status — 查看账户 / 持仓 / 止损 / 挂单 / 待开盘执行的任务
!pause — 暂停跟单（止损与降杠杆保护仍运行）
!resume — 恢复跟单
!sync — 预览：按他最新持仓再平衡的下单计划
!sync confirm — 确认执行上面的计划（需在 !sync 后 180 秒内）
!cancel — 撤销机器人所有未成交单（不含止损）并清空待开盘任务
!help — 显示本帮助"""

PAUSED_TEXT = "⏸️ 已暂停跟单（止损和降杠杆保护仍在运行）"
RESUMED_TEXT = "▶️ 已恢复跟单"
SYNC_USAGE_TEXT = "用法：!sync 预览再平衡计划；!sync confirm 确认执行"
ENGINE_NOT_READY_TEXT = "⚠️ 交易引擎尚未就绪，请稍后再试"


# --------------------------------------------------------------------------- pure helpers


def _str_attr(obj: Any, name: str) -> str:
    """``obj.name`` if it is a non-blank string, else ``""`` (missing attrs are fine)."""
    value = getattr(obj, name, None)
    return value if isinstance(value, str) and value.strip() else ""


def _embed_parts(embed: Any) -> list[str]:
    """title, description, each field as "name\\nvalue", footer.text."""
    parts = [_str_attr(embed, "title"), _str_attr(embed, "description")]
    for field in getattr(embed, "fields", None) or []:
        parts.append("\n".join(p for p in (_str_attr(field, "name"), _str_attr(field, "value")) if p))
    parts.append(_str_attr(getattr(embed, "footer", None), "text"))
    return parts


def _content_and_embeds(obj: Any) -> list[str]:
    parts = [_str_attr(obj, "content")]
    for embed in getattr(obj, "embeds", None) or []:
        parts.extend(_embed_parts(embed))
    return parts


def extract_text(message: Any) -> str:
    """Flatten a message (content + embeds + native-forward snapshots) into one text."""
    parts = _content_and_embeds(message)
    for snapshot in getattr(message, "message_snapshots", None) or []:
        parts.extend(_content_and_embeds(snapshot))
    return "\n".join(p for p in parts if p)


def parse_command(content: str | None) -> tuple[str, list[str]] | None:
    """``"!sync confirm"`` → ``("sync", ["confirm"])``; None if not a command."""
    if not content:
        return None
    text = content.strip()
    if not text.startswith(COMMAND_PREFIXES):
        return None
    words = text[1:].split()
    if not words:
        return None
    return words[0].lower(), words[1:]


def is_allowed_signal_author(
    author_id: int | None, webhook_id: int | None, allowed_ids: Iterable[int]
) -> bool:
    """Empty allow-list → everyone; else the author or the webhook must be listed."""
    allowed = set(allowed_ids)
    if not allowed:
        return True
    return any(i is not None and i in allowed for i in (author_id, webhook_id))


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _as_reply(value: Any) -> str:
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    return text if text.strip() else "（无内容）"


async def handle_command(engine: "Engine | None", cmd: str, args: list[str]) -> str:
    """Run one owner command and return the reply text. Never raises."""
    if cmd == "help":
        return HELP_TEXT
    if cmd not in ("status", "pause", "resume", "sync", "cancel"):
        return f"❓ 未知命令 !{cmd}，发送 !help 查看可用命令"
    if engine is None:
        return ENGINE_NOT_READY_TEXT
    try:
        if cmd == "status":
            return _as_reply(await engine.status_text())
        if cmd == "pause":
            await _maybe_await(engine.pause())
            return PAUSED_TEXT
        if cmd == "resume":
            await _maybe_await(engine.resume())
            return RESUMED_TEXT
        if cmd == "sync":
            if not args:
                return _as_reply(await engine.sync_preview())
            if args[0].lower() == "confirm":
                return _as_reply(await engine.sync_confirm())
            return SYNC_USAGE_TEXT
        return _as_reply(await engine.cancel_all())  # cmd == "cancel"
    except Exception as exc:
        log.exception("command !%s %s failed", cmd, " ".join(args))
        return f"🚨 命令 !{cmd} 执行失败：{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- client


class CopyBotClient(discord.Client):
    """Reads signals and owner commands; reports through the notifier."""

    def __init__(
        self, cfg: AppConfig, engine: "Engine | None" = None, notifier: Notifier | None = None
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: enable it in the developer portal too
        # Default for every message the client sends: ping nobody. DiscordNotifier
        # passes its own allowed_mentions (owners only) per message.
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.cfg = cfg
        self.engine = engine
        self.notifier = notifier
        self.bootstrap_wait_seconds = BOOTSTRAP_WAIT_SECONDS
        self._ready_once = False
        self._bootstrap_done = asyncio.Event()
        # Live signal messages seen before the bootstrap finished: bootstrap skips them
        # (they are handled live) so no message is applied twice.
        self._live_ids: set[int] = set()
        # Newest live message already handled before the bootstrap ran (only if the
        # bootstrap wait timed out): bootstrap must not apply anything older.
        self._handled_before_bootstrap: int | None = None
        self._signal_lock = asyncio.Lock()  # one signal at a time, in arrival order

    # ---------------------------------------------------------------- lifecycle

    async def setup_hook(self) -> None:
        self.log_config_warnings()

    def log_config_warnings(self) -> None:
        d = self.cfg.discord
        if not d.allowed_signal_author_ids:
            log.warning(
                "discord.allowed_signal_author_ids is empty: ANY message in signal channel %s "
                "is treated as a potential signal. Set it to your own user id / webhook id.",
                d.signal_channel_id,
            )
        if not d.owner_ids:
            log.warning("discord.owner_ids is empty: nobody can run commands and alerts mention nobody.")

    async def on_ready(self) -> None:
        log.info("Discord connected as %s", self.user)
        await self._flush_notifier()
        if self._ready_once:  # reconnect with a new session: nothing else to do
            return
        self._ready_once = True
        mode = "DRY-RUN" if self.cfg.trading.dry_run else "实盘"
        await self._notify(f"🤖 跟单机器人已上线（模式: {mode}）")
        await self.bootstrap()

    async def bootstrap(self) -> None:
        """Feed the last messages of the signal channel to the engine, oldest first."""
        try:
            async with self._signal_lock:
                await self._bootstrap_locked()
        finally:
            self._bootstrap_done.set()
            self._live_ids.clear()

    async def _bootstrap_locked(self) -> None:
        if self.engine is None:
            log.warning("bootstrap skipped: engine not attached")
            return
        channel_id = self.cfg.discord.signal_channel_id
        try:
            channel = await self._resolve_channel(channel_id)
            newest_first = [m async for m in channel.history(limit=BOOTSTRAP_HISTORY_LIMIT)]
        except Exception as exc:
            log.exception("bootstrap: cannot read history of signal channel %s", channel_id)
            await self._notify(
                f"读取信号频道历史失败（{type(exc).__name__}: {exc}），他的最新持仓暂未知，"
                "!sync 需等下一条信号后才准确。",
                level="warn",
            )
            return
        applied = 0
        for message in reversed(newest_first):  # oldest first → the newest wins
            if not self._is_bootstrap_candidate(message):
                continue
            text = extract_text(message)
            if not text:
                continue
            try:
                if await self.engine.bootstrap_from_text(text):
                    applied += 1
            except Exception:
                log.exception("bootstrap: engine failed on message %s", getattr(message, "id", "?"))
        log.info("bootstrap: read %d messages, %d signal(s) applied", len(newest_first), applied)

    def _is_bootstrap_candidate(self, message: Any) -> bool:
        message_id = getattr(message, "id", None)
        if self._is_own(message) or message_id in self._live_ids:
            return False
        newest_live = self._handled_before_bootstrap
        if newest_live is not None and (not isinstance(message_id, int) or message_id <= newest_live):
            return False  # a newer signal was already handled live
        author = getattr(message, "author", None)
        return is_allowed_signal_author(
            getattr(author, "id", None),
            getattr(message, "webhook_id", None),
            self.cfg.discord.allowed_signal_author_ids,
        )

    # ---------------------------------------------------------------- messages

    async def on_message(self, message: discord.Message) -> None:
        if self._is_own(message):
            return
        channel_id = getattr(message.channel, "id", None)
        d = self.cfg.discord
        if channel_id == d.signal_channel_id:
            await self._on_signal_message(message)
        if channel_id == d.command_channel_id:
            await self._on_command_message(message)

    async def _on_signal_message(self, message: discord.Message) -> None:
        author_id = message.author.id
        allowed = self.cfg.discord.allowed_signal_author_ids
        if not is_allowed_signal_author(author_id, message.webhook_id, allowed):
            log.info("ignored message %s from non-allowed author %s in signal channel", message.id, author_id)
            return
        text = extract_text(message)
        if not text:
            return
        meta = MessageMeta(str(message.id), author_id, message.created_at)
        if not self._bootstrap_done.is_set():
            self._live_ids.add(message.id)
            await self._wait_for_bootstrap(message.id)
        async with self._signal_lock:
            if not self._bootstrap_done.is_set():
                prev = self._handled_before_bootstrap
                self._handled_before_bootstrap = message.id if prev is None else max(prev, message.id)
            await self._handle_signal_text(text, meta)

    async def _wait_for_bootstrap(self, message_id: int) -> None:
        try:
            await asyncio.wait_for(self._bootstrap_done.wait(), timeout=self.bootstrap_wait_seconds)
        except asyncio.TimeoutError:
            log.warning(
                "bootstrap not finished after %.0fs; handling message %s now",
                self.bootstrap_wait_seconds,
                message_id,
            )

    async def _handle_signal_text(self, text: str, meta: MessageMeta) -> None:
        if self.engine is None:
            log.error("signal message %s received but engine not attached", meta.message_id)
            await self._notify(
                f"收到信号频道消息（{meta.message_id}）但交易引擎未就绪，未处理。",
                level="error",
                mention=True,
            )
            return
        try:
            is_signal = await self.engine.handle_text(text, meta)
            log.info("message %s handled (signal=%s)", meta.message_id, is_signal)
        except Exception as exc:
            log.exception("engine.handle_text failed for message %s", meta.message_id)
            await self._notify(
                f"处理信号消息出错（消息 {meta.message_id}）：{type(exc).__name__}: {exc}",
                level="error",
                mention=True,
            )

    async def _on_command_message(self, message: discord.Message) -> None:
        if message.author.id not in self.cfg.discord.owner_ids:
            return  # non-owners are ignored silently
        parsed = parse_command(message.content)
        if parsed is None:
            return
        cmd, args = parsed
        log.info("command from %s: !%s %s", message.author.id, cmd, " ".join(args))
        reply = await handle_command(self.engine, cmd, args)
        await self._reply(message.channel, reply)

    # ---------------------------------------------------------------- utils

    def _is_own(self, message: Any) -> bool:
        me = self.user
        author = getattr(message, "author", None)
        return me is not None and author is not None and getattr(author, "id", None) == me.id

    async def _reply(self, channel: Any, text: str) -> None:
        for chunk in split_message(text):
            try:
                await channel.send(chunk)
            except Exception:
                log.exception("failed to reply in channel %s", getattr(channel, "id", "?"))
                return

    async def _resolve_channel(self, channel_id: int) -> Any:
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        return channel

    async def _flush_notifier(self) -> None:
        flush = getattr(self.notifier, "flush", None)
        if flush is None:
            return
        try:
            await _maybe_await(flush())
        except Exception:
            log.exception("notifier flush failed")

    async def _notify(self, text: str, *, level: str = "info", mention: bool = False) -> None:
        if self.notifier is None:
            log.log(logging.ERROR if level == "error" else logging.INFO, "notify (no notifier): %s", text)
            return
        try:
            await self.notifier.send(text, level=level, mention=mention)
        except Exception:
            log.exception("notifier.send failed: %s", text)


async def run_client(client: discord.Client, token: str) -> None:
    """Run the client until it is closed (clean shutdown via ``async with``)."""
    async with client:
        await client.start(token)
