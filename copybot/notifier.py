"""User-facing notifications (DESIGN.md §3.7).

`Notifier` is the tiny interface the engine talks to. `LogNotifier` only logs (tests,
before Discord is wired up); `DiscordNotifier` posts to the log channel.

Delivery rules of `DiscordNotifier`:
- level icon (ℹ️ / ⚠️ / 🚨) and, with ``mention=True``, one ``<@owner>`` per owner are
  prefixed to the first chunk; messages are split into chunks of <= 1900 characters on
  line boundaries (Discord's hard limit is 2000);
- only the owners can be pinged: never @everyone / @here / roles;
- while the client is not ready, messages are queued (max 100, oldest dropped) and
  delivered, in order, by ``flush()`` (called from ``on_ready``) or by the next ``send``;
- it never raises: every failure is logged (and every message is logged as well, so the
  log file is a complete audit trail even if Discord is down).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Protocol, runtime_checkable

import discord

log = logging.getLogger(__name__)

MAX_CHUNK = 1900  # Discord allows 2000; keep a margin
MAX_QUEUE = 100  # messages kept while the client is not ready
LOG_KEEP = 1000  # LogNotifier keeps at most this many messages in memory

LEVEL_ICONS: dict[str, str] = {"info": "ℹ️", "warn": "⚠️", "error": "🚨"}
_LOG_LEVELS: dict[str, int] = {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}


@runtime_checkable
class Notifier(Protocol):
    async def send(self, text: str, *, level: str = "info", mention: bool = False) -> None:
        """Deliver `text` to the user; must never raise."""
        ...


def _log_level(level: str) -> int:
    return _LOG_LEVELS.get(level, logging.INFO)


# --------------------------------------------------------------------------- splitting


def _hard_split(line: str, limit: int) -> list[str]:
    """Cut one line into pieces of at most `limit` characters ("" stays [""])."""
    if len(line) <= limit:
        return [line]
    return [line[i : i + limit] for i in range(0, len(line), limit)]


def split_message(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split `text` into chunks of <= `limit` chars, preferring line boundaries.

    Lines longer than `limit` are hard-split. The newline at a chunk boundary is
    dropped. Whitespace-only chunks are dropped (Discord rejects empty messages), so an
    empty / blank text gives ``[]``.
    """
    if limit <= 0:
        raise ValueError("limit must be > 0")
    if len(text) <= limit:
        return [text] if text.strip() else []

    chunks: list[str] = []
    buf: list[str] = []
    size = 0  # == len("\n".join(buf))
    for line in text.split("\n"):
        for piece in _hard_split(line, limit):
            added = len(piece) + (1 if buf else 0)
            if buf and size + added > limit:
                chunks.append("\n".join(buf))
                buf, size, added = [], 0, len(piece)
            buf.append(piece)
            size += added
    if buf:
        chunks.append("\n".join(buf))
    return [c for c in chunks if c.strip()]


def format_message(text: str, level: str, mention: bool, owner_ids: list[int]) -> str:
    """Prefix mentions and the level icon: ``"<@1> <@2> ⚠️ text"``."""
    icon = LEVEL_ICONS.get(level, LEVEL_ICONS["info"])
    mentions = "".join(f"<@{oid}> " for oid in owner_ids) if mention else ""
    return f"{mentions}{icon} {text}"


# --------------------------------------------------------------------------- notifiers


class LogNotifier:
    """Logs every message; keeps them in ``self.messages`` (text, level, mention) for tests."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, bool]] = []

    async def send(self, text: str, *, level: str = "info", mention: bool = False) -> None:
        self.messages.append((text, level, mention))
        if len(self.messages) > LOG_KEEP:
            del self.messages[: len(self.messages) - LOG_KEEP]
        log.log(_log_level(level), "notify%s: %s", " @owner" if mention else "", text)


class DiscordNotifier:
    """Posts notifications to one Discord text channel (the log channel)."""

    def __init__(self, client: discord.Client, channel_id: int, owner_ids: list[int]) -> None:
        self._client = client
        self.channel_id = channel_id
        self.owner_ids = list(owner_ids)
        self._queue: deque[tuple[str, str, bool]] = deque()
        self._dropped = 0  # queued messages discarded because the queue was full
        self._channel: Any = None  # resolved lazily, then cached
        self._lock = asyncio.Lock()  # keeps delivery order (queue first, then new messages)

    @property
    def queued(self) -> int:
        return len(self._queue)

    async def send(self, text: str, *, level: str = "info", mention: bool = False) -> None:
        try:
            log.log(_log_level(level), "notify%s: %s", " @owner" if mention else "", text)
            if not self._is_ready():
                self._enqueue(text, level, mention)
                return
            async with self._lock:
                await self._drain()
                await self._deliver(text, level, mention)
        except Exception:  # never raise into the engine
            log.exception("DiscordNotifier.send failed (message lost): %s", text)

    async def flush(self) -> None:
        """Deliver queued messages; call once the client is ready (``on_ready``)."""
        try:
            if not self._is_ready():
                return
            async with self._lock:
                await self._drain()
        except Exception:
            log.exception("DiscordNotifier.flush failed")

    # ---------------------------------------------------------------- internals

    def _is_ready(self) -> bool:
        try:
            return bool(self._client.is_ready())
        except Exception:
            return False

    def _enqueue(self, text: str, level: str, mention: bool) -> None:
        if len(self._queue) >= MAX_QUEUE:
            self._queue.popleft()
            self._dropped += 1
        self._queue.append((text, level, mention))

    async def _drain(self) -> None:
        """Deliver everything queued, oldest first. Caller holds ``self._lock``."""
        if self._dropped:
            dropped, self._dropped = self._dropped, 0
            await self._deliver(
                f"Discord 未连接期间有 {dropped} 条较早的通知因队列已满被丢弃，详见日志。", "warn", False
            )
        while self._queue:
            text, level, mention = self._queue.popleft()
            await self._deliver(text, level, mention)

    async def _deliver(self, text: str, level: str, mention: bool) -> None:
        """Send one message (possibly several chunks). Logs and swallows failures."""
        try:
            channel = await self._get_channel()
            allowed = self._allowed_mentions(mention)
            for chunk in split_message(format_message(text, level, mention, self.owner_ids)):
                await channel.send(chunk, allowed_mentions=allowed)
        except Exception:
            log.exception("failed to post notification to channel %s: %s", self.channel_id, text)

    async def _get_channel(self) -> Any:
        if self._channel is None:
            channel = self._client.get_channel(self.channel_id)
            if channel is None:
                channel = await self._client.fetch_channel(self.channel_id)
            self._channel = channel
        return self._channel

    def _allowed_mentions(self, mention: bool) -> discord.AllowedMentions:
        users: list[discord.Object] | bool = (
            [discord.Object(id=oid) for oid in self.owner_ids] if mention else False
        )
        return discord.AllowedMentions(everyone=False, roles=False, replied_user=False, users=users)
