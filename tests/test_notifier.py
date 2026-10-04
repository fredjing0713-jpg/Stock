"""Tests for copybot.notifier (DESIGN.md §3.7). No network: fake client / channel."""

from __future__ import annotations

import logging

import pytest

from copybot.notifier import (
    LEVEL_ICONS,
    MAX_CHUNK,
    MAX_QUEUE,
    DiscordNotifier,
    LogNotifier,
    Notifier,
    format_message,
    split_message,
)

LOG_CHANNEL = 200
OWNERS = [11, 22]


# --------------------------------------------------------------------------- fakes


class FakeChannel:
    def __init__(self, channel_id: int = LOG_CHANNEL, fail: bool = False) -> None:
        self.id = channel_id
        self.fail = fail
        self.sent: list[tuple[str, dict]] = []

    async def send(self, content: str, **kwargs) -> None:
        if self.fail:
            raise RuntimeError("discord down")
        self.sent.append((content, kwargs))

    @property
    def texts(self) -> list[str]:
        return [c for c, _ in self.sent]


class FakeClient:
    def __init__(self, channel: FakeChannel, *, ready: bool = True, cached: bool = True) -> None:
        self.channel = channel
        self.ready = ready
        self.cached = cached
        self.fetch_calls = 0
        self.fetch_error: Exception | None = None

    def is_ready(self) -> bool:
        return self.ready

    def get_channel(self, channel_id: int):
        return self.channel if self.cached and channel_id == self.channel.id else None

    async def fetch_channel(self, channel_id: int):
        self.fetch_calls += 1
        if self.fetch_error:
            raise self.fetch_error
        assert channel_id == self.channel.id
        return self.channel


def make(ready: bool = True, **kw) -> tuple[DiscordNotifier, FakeClient, FakeChannel]:
    channel = FakeChannel(fail=kw.pop("fail", False))
    client = FakeClient(channel, ready=ready, **kw)
    return DiscordNotifier(client, LOG_CHANNEL, OWNERS), client, channel


# --------------------------------------------------------------------------- split_message


def test_split_short_text_is_one_chunk():
    assert split_message("hello\nworld") == ["hello\nworld"]


def test_split_empty_or_blank_gives_nothing():
    assert split_message("") == []
    assert split_message("  \n \n") == []


def test_split_exact_limit_is_one_chunk():
    text = "a" * MAX_CHUNK
    assert split_message(text) == [text]
    lines = "\n".join(["b" * 9] * 10)  # 10*9 + 9 newlines = 99
    assert split_message(lines, limit=99) == [lines]


def test_split_one_over_limit_breaks_on_line_boundary():
    text = "a" * 50 + "\n" + "b" * 50  # 101 chars
    assert split_message(text, limit=100) == ["a" * 50, "b" * 50]


def test_split_many_lines_respects_limit_and_keeps_lines_whole():
    lines = [f"第{i}行 WDC | 仓位：{i}.0%" for i in range(500)]
    text = "\n".join(lines)
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= MAX_CHUNK for c in chunks)
    assert "\n".join(chunks) == text  # only split at newlines, nothing lost
    for c in chunks:
        assert all(line in lines for line in c.split("\n"))


def test_split_single_huge_line_is_hard_split():
    text = "x" * (MAX_CHUNK * 2 + 7)
    chunks = split_message(text)
    assert [len(c) for c in chunks] == [MAX_CHUNK, MAX_CHUNK, 7]
    assert "".join(chunks) == text


def test_split_huge_line_between_normal_lines():
    text = "head\n" + "y" * 25 + "\ntail"
    chunks = split_message(text, limit=10)
    assert all(len(c) <= 10 for c in chunks)
    assert chunks == ["head", "y" * 10, "y" * 10, "y" * 5 + "\ntail"]


def test_split_drops_blank_chunks():
    text = "a" * 10 + "\n\n\n" + "b" * 10
    assert split_message(text, limit=10) == ["a" * 10, "b" * 10]


def test_split_rejects_bad_limit():
    with pytest.raises(ValueError):
        split_message("x", limit=0)


def test_format_message_icons_and_mentions():
    assert format_message("hi", "info", False, OWNERS) == f"{LEVEL_ICONS['info']} hi"
    assert format_message("hi", "warn", False, OWNERS) == "⚠️ hi"
    assert format_message("hi", "error", True, OWNERS) == "<@11> <@22> 🚨 hi"
    assert format_message("hi", "bogus", False, []) == "ℹ️ hi"  # unknown level → info


# --------------------------------------------------------------------------- LogNotifier


async def test_log_notifier_records_and_logs(caplog):
    n = LogNotifier()
    assert isinstance(n, Notifier)
    with caplog.at_level(logging.INFO, logger="copybot.notifier"):
        await n.send("建仓 WDC")
        await n.send("止损成交", level="error", mention=True)
    assert n.messages == [("建仓 WDC", "info", False), ("止损成交", "error", True)]
    levels = [r.levelno for r in caplog.records]
    assert levels == [logging.INFO, logging.ERROR]
    assert "止损成交" in caplog.records[1].getMessage()


# --------------------------------------------------------------------------- DiscordNotifier


async def test_discord_send_when_ready_with_icon_and_no_pings():
    n, client, channel = make()
    assert isinstance(n, Notifier)
    await n.send("下单成功", level="warn")
    assert channel.texts == ["⚠️ 下单成功"]
    allowed = channel.sent[0][1]["allowed_mentions"]
    assert allowed.everyone is False and allowed.roles is False
    assert allowed.to_dict() == {"parse": []}  # nobody pinged without mention=True


async def test_discord_mention_prefix_and_allowed_users_only():
    n, _, channel = make()
    await n.send("止损触发", level="error", mention=True)
    assert channel.texts == ["<@11> <@22> 🚨 止损触发"]
    allowed = channel.sent[0][1]["allowed_mentions"]
    assert allowed.to_dict() == {"parse": [], "users": OWNERS}  # no @everyone / roles


async def test_discord_long_message_is_chunked_icon_only_on_first():
    n, _, channel = make()
    text = "\n".join(f"line {i:04d} " + "z" * 40 for i in range(200))
    await n.send(text, level="info", mention=True)
    assert len(channel.texts) > 1
    assert all(len(t) <= MAX_CHUNK for t in channel.texts)
    assert channel.texts[0].startswith("<@11> <@22> ℹ️ line 0000")
    assert all("ℹ️" not in t and "<@" not in t for t in channel.texts[1:])
    assert "\n".join(channel.texts) == format_message(text, "info", True, OWNERS)


async def test_discord_queue_before_ready_then_flush_in_order():
    n, client, channel = make(ready=False)
    await n.send("one")
    await n.send("two", level="error", mention=True)
    assert channel.sent == [] and n.queued == 2
    await n.flush()  # still not ready → no-op
    assert channel.sent == []
    client.ready = True
    await n.flush()
    assert channel.texts == ["ℹ️ one", "<@11> <@22> 🚨 two"]
    assert n.queued == 0
    await n.flush()  # nothing left
    assert len(channel.texts) == 2


async def test_discord_send_after_ready_drains_queue_first():
    n, client, channel = make(ready=False)
    await n.send("old")
    client.ready = True
    await n.send("new")  # without explicit flush
    assert channel.texts == ["ℹ️ old", "ℹ️ new"]


async def test_discord_queue_is_bounded_and_reports_drops():
    n, client, channel = make(ready=False)
    for i in range(MAX_QUEUE + 5):
        await n.send(f"m{i}")
    assert n.queued == MAX_QUEUE
    client.ready = True
    await n.flush()
    assert channel.texts[0].startswith("⚠️") and "5 条" in channel.texts[0]
    assert channel.texts[1] == "ℹ️ m5"  # the 5 oldest were dropped
    assert channel.texts[-1] == f"ℹ️ m{MAX_QUEUE + 4}"
    assert len(channel.texts) == MAX_QUEUE + 1


async def test_discord_fetches_channel_when_not_cached_and_caches_it():
    n, client, channel = make(cached=False)
    await n.send("a")
    await n.send("b")
    assert client.fetch_calls == 1
    assert channel.texts == ["ℹ️ a", "ℹ️ b"]


async def test_discord_send_never_raises_on_channel_error(caplog):
    n, _, channel = make(fail=True)
    with caplog.at_level(logging.ERROR, logger="copybot.notifier"):
        await n.send("boom")  # must not raise
    assert any("failed to post notification" in r.getMessage() for r in caplog.records)


async def test_discord_send_never_raises_on_fetch_error():
    n, client, channel = make(cached=False)
    client.fetch_error = RuntimeError("forbidden")
    await n.send("x")  # must not raise
    client.fetch_error = None
    await n.send("y")  # channel not cached after the failure → retried
    assert channel.texts == ["ℹ️ y"]
    assert client.fetch_calls == 2


async def test_discord_failed_queued_message_does_not_block_others():
    n, client, channel = make(ready=False)
    await n.send("a")
    await n.send("b")
    client.ready = True
    channel.fail = True
    await n.flush()  # both fail, logged, not raised
    channel.fail = False
    await n.send("c")
    assert channel.texts == ["ℹ️ c"]
    assert n.queued == 0


async def test_discord_is_ready_error_means_queue():
    class Broken(FakeClient):
        def is_ready(self) -> bool:
            raise RuntimeError("weird")

    channel = FakeChannel()
    n = DiscordNotifier(Broken(channel), LOG_CHANNEL, OWNERS)
    await n.send("x")
    assert n.queued == 1 and channel.sent == []
