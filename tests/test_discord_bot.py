"""Tests for copybot.discord_bot (DESIGN.md §3.9). No network: duck-typed fakes only."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

from copybot.config import AppConfig, DiscordConfig
from copybot.discord_bot import (
    BOOTSTRAP_HISTORY_LIMIT,
    HELP_TEXT,
    PAUSED_TEXT,
    RESUMED_TEXT,
    SYNC_USAGE_TEXT,
    CopyBotClient,
    extract_text,
    handle_command,
    is_allowed_signal_author,
    parse_command,
)
from copybot.models import MessageMeta
from copybot.notifier import LogNotifier

SIGNAL_CH = 100
LOG_CH = 200
CONTROL_CH = 300
SIGNAL_AUTHOR = 10
WEBHOOK = 15
OWNER = 20
BOT_ID = 999
T0 = datetime(2026, 10, 2, 13, 50, 59, tzinfo=timezone.utc)

SIGNAL = """组合收益
10月收益：3.4%
总收益：168.3%
仓位变动
建仓 WDC | 仓位：0.0% → 100.0% | 成交价：422.6
持仓明细
WDC | 仓位：100.0% | 成本：422.6 | 盈亏：+0.1%
(2026-10-02 09:50:59 美东)"""


# --------------------------------------------------------------------------- fakes


def embed(**kw) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def msg(
    content: str = "",
    *,
    mid: int = 1,
    author: int = SIGNAL_AUTHOR,
    channel: int = SIGNAL_CH,
    webhook_id: int | None = None,
    embeds: list | None = None,
    snapshots: list | None = None,
    channel_obj=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=mid,
        content=content,
        author=SimpleNamespace(id=author),
        channel=channel_obj or SimpleNamespace(id=channel),
        webhook_id=webhook_id,
        embeds=embeds or [],
        message_snapshots=snapshots or [],
        created_at=T0,
    )


class FakeEngine:
    def __init__(self) -> None:
        self.handled: list[tuple[str, MessageMeta]] = []
        self.bootstrapped: list[str] = []
        self.paused = False
        self.fail: str | None = None  # name of the method that should raise
        self.handle_delay = 0.0

    def _maybe_fail(self, name: str) -> None:
        if self.fail == name:
            raise RuntimeError(f"{name} exploded")

    async def handle_text(self, text: str, meta: MessageMeta) -> bool:
        if self.handle_delay:
            await asyncio.sleep(self.handle_delay)
        self._maybe_fail("handle_text")
        self.handled.append((text, meta))
        return "仓位" in text

    async def bootstrap_from_text(self, text: str) -> bool:
        self._maybe_fail("bootstrap_from_text")
        self.bootstrapped.append(text)
        return "仓位" in text

    async def status_text(self) -> str:
        self._maybe_fail("status_text")
        return "账户状态 OK"

    async def sync_preview(self) -> str:
        return "预览计划"

    async def sync_confirm(self) -> str:
        return "已执行"

    async def cancel_all(self) -> str:
        return "已撤销 2 单"

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False


class AsyncPauseEngine(FakeEngine):
    async def pause(self) -> None:  # type: ignore[override]
        self.paused = True

    async def resume(self) -> None:  # type: ignore[override]
        self.paused = False


class FakeChannel:
    def __init__(self, channel_id: int, history: list | None = None) -> None:
        self.id = channel_id
        self._history = history or []  # newest first, like discord
        self.history_limits: list[int] = []
        self.sent: list[str] = []
        self.history_error: Exception | None = None

    def history(self, *, limit: int = 100):
        self.history_limits.append(limit)
        if self.history_error:
            raise self.history_error

        async def gen():
            for m in self._history[:limit]:
                await asyncio.sleep(0)
                yield m

        return gen()

    async def send(self, content: str, **kwargs) -> None:
        self.sent.append(content)


class FlushingNotifier(LogNotifier):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    async def flush(self) -> None:
        self.flushes += 1


def make_cfg(allowed: list[int] | None = None, control: int | None = CONTROL_CH, dry_run: bool = True) -> AppConfig:
    return AppConfig.model_validate(
        {
            "discord": {
                "signal_channel_id": SIGNAL_CH,
                "log_channel_id": LOG_CH,
                "control_channel_id": control,
                "allowed_signal_author_ids": [SIGNAL_AUTHOR, WEBHOOK] if allowed is None else allowed,
                "owner_ids": [OWNER],
            },
            "trading": {"dry_run": dry_run},
        }
    )


def make_client(
    cfg: AppConfig | None = None, history: list | None = None, engine: FakeEngine | None = None
) -> tuple[CopyBotClient, FakeEngine, FlushingNotifier, FakeChannel]:
    engine = engine or FakeEngine()
    notifier = FlushingNotifier()
    client = CopyBotClient(cfg or make_cfg())
    client.engine = engine  # settable after construction
    client.notifier = notifier
    client._connection.user = SimpleNamespace(id=BOT_ID)  # what discord sets after login
    signal_channel = FakeChannel(SIGNAL_CH, history)
    client.get_channel = lambda cid: signal_channel if cid == SIGNAL_CH else None  # type: ignore[method-assign]
    return client, engine, notifier, signal_channel


async def ready_client(**kw) -> tuple[CopyBotClient, FakeEngine, FlushingNotifier, FakeChannel]:
    client, engine, notifier, channel = make_client(**kw)
    await client.on_ready()
    return client, engine, notifier, channel


# --------------------------------------------------------------------------- extract_text


def test_extract_plain_content():
    assert extract_text(msg(SIGNAL)) == SIGNAL


def test_extract_embed_parts_in_order():
    e = embed(
        title="组合收益",
        description="10月收益：3.4%\n总收益：168.3%",
        fields=[
            SimpleNamespace(name="仓位变动", value="建仓 WDC | 仓位：0.0% → 100.0% | 成交价：422.6"),
            SimpleNamespace(name="持仓明细", value="WDC | 仓位：100.0% | 成本：422.6 | 盈亏：+0.1%"),
        ],
        footer=SimpleNamespace(text="(2026-10-02 09:50:59 美东)"),
    )
    assert extract_text(msg("", embeds=[e])) == SIGNAL


def test_extract_native_forward_snapshot():
    snap = SimpleNamespace(content=SIGNAL, embeds=[])
    assert extract_text(msg("", snapshots=[snap])) == SIGNAL


def test_extract_forward_with_embed_and_own_comment():
    e = embed(title="组合收益", description="仓位变动", fields=[], footer=SimpleNamespace(text=None))
    snap = SimpleNamespace(content="", embeds=[e])
    assert extract_text(msg("转发：", snapshots=[snap])) == "转发：\n组合收益\n仓位变动"


def test_extract_tolerates_missing_and_none_attributes():
    bare = SimpleNamespace()
    assert extract_text(bare) == ""
    weird = SimpleNamespace(content=None, embeds=None, message_snapshots=None)
    assert extract_text(weird) == ""
    e = SimpleNamespace(description="只有描述", fields=[SimpleNamespace(name="仅名称")])  # no title / footer
    assert extract_text(SimpleNamespace(content="  ", embeds=[e])) == "只有描述\n仅名称"


def test_extract_real_discord_embed_objects():
    e = discord.Embed(title="组合收益", description="仓位变动")
    e.add_field(name="持仓明细", value="WDC | 仓位：100.0%")
    e.set_footer(text="(2026-10-02 09:50:59 美东)")
    no_footer = discord.Embed(description="x")  # footer.text is None on a real embed
    text = extract_text(msg("", embeds=[e, no_footer]))
    assert text == "组合收益\n仓位变动\n持仓明细\nWDC | 仓位：100.0%\n(2026-10-02 09:50:59 美东)\nx"


# --------------------------------------------------------------------------- parse_command


@pytest.mark.parametrize(
    "content, expected",
    [
        ("!status", ("status", [])),
        ("  !Sync   CONFIRM  ", ("sync", ["CONFIRM"])),
        ("!sync confirm", ("sync", ["confirm"])),
        ("！pause", ("pause", [])),  # full-width exclamation mark
        ("!help me please", ("help", ["me", "please"])),
        ("!", None),
        ("!   ", None),
        ("status", None),
        ("hello !status", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_command(content, expected):
    assert parse_command(content) == expected


# --------------------------------------------------------------------------- allow-list


def test_is_allowed_signal_author():
    assert is_allowed_signal_author(1, None, []) is True  # empty list → anyone
    assert is_allowed_signal_author(1, None, [1, 2]) is True
    assert is_allowed_signal_author(3, None, [1, 2]) is False
    assert is_allowed_signal_author(3, 2, [1, 2]) is True  # webhook id listed
    assert is_allowed_signal_author(3, 4, [1, 2]) is False
    assert is_allowed_signal_author(None, None, [1]) is False


# --------------------------------------------------------------------------- handle_command


async def test_handle_command_dispatch():
    eng = FakeEngine()
    assert await handle_command(eng, "status", []) == "账户状态 OK"
    assert await handle_command(eng, "pause", []) == PAUSED_TEXT
    assert eng.paused is True
    assert await handle_command(eng, "resume", []) == RESUMED_TEXT
    assert eng.paused is False
    assert await handle_command(eng, "sync", []) == "预览计划"
    assert await handle_command(eng, "sync", ["CONFIRM"]) == "已执行"
    assert await handle_command(eng, "sync", ["now"]) == SYNC_USAGE_TEXT
    assert await handle_command(eng, "cancel", []) == "已撤销 2 单"
    assert await handle_command(eng, "help", []) == HELP_TEXT


async def test_handle_command_awaits_async_pause_resume():
    eng = AsyncPauseEngine()
    assert await handle_command(eng, "pause", []) == PAUSED_TEXT
    assert eng.paused is True
    await handle_command(eng, "resume", [])
    assert eng.paused is False


async def test_handle_command_unknown_and_no_engine():
    reply = await handle_command(FakeEngine(), "buy", ["WDC"])
    assert "!help" in reply and "!buy" in reply
    assert "未就绪" in await handle_command(None, "status", [])
    assert await handle_command(None, "help", []) == HELP_TEXT


async def test_handle_command_engine_error_becomes_reply():
    eng = FakeEngine()
    eng.fail = "status_text"
    reply = await handle_command(eng, "status", [])
    assert reply.startswith("🚨") and "status_text exploded" in reply


async def test_handle_command_empty_engine_reply():
    class Quiet(FakeEngine):
        async def cancel_all(self):
            return None

    assert await handle_command(Quiet(), "cancel", []) == "（无内容）"


def test_help_text_lists_every_command():
    for cmd in ("!status", "!pause", "!resume", "!sync", "!sync confirm", "!cancel", "!help"):
        assert cmd in HELP_TEXT
    assert "止损与降杠杆保护仍运行" in HELP_TEXT and "180 秒" in HELP_TEXT


# --------------------------------------------------------------------------- client


async def test_client_intents_and_defaults():
    client = CopyBotClient(make_cfg())
    assert client.intents.message_content is True
    assert client.intents.guild_messages is True
    assert client.engine is None and client.notifier is None
    assert client.allowed_mentions is not None and client.allowed_mentions.to_dict() == {"parse": []}


async def test_setup_hook_warns_when_allow_list_empty(caplog):
    client, *_ = make_client(cfg=make_cfg(allowed=[]))
    with caplog.at_level(logging.WARNING, logger="copybot.discord_bot"):
        await client.setup_hook()
    assert any("allowed_signal_author_ids is empty" in r.getMessage() for r in caplog.records)


async def test_setup_hook_quiet_when_configured(caplog):
    client, *_ = make_client()
    with caplog.at_level(logging.WARNING, logger="copybot.discord_bot"):
        await client.setup_hook()
    # (discord.py itself may log a PyNaCl warning on the first Client(); not ours)
    assert [r for r in caplog.records if r.name == "copybot.discord_bot"] == []


async def test_on_ready_flushes_announces_and_bootstraps_oldest_first():
    history = [  # newest first
        msg("最新 仓位 C", mid=5),
        msg("闲聊", mid=4, author=777),  # not an allowed author
        msg("bot 自己的 仓位", mid=3, author=BOT_ID),
        msg("", mid=2, snapshots=[SimpleNamespace(content="转发 仓位 B", embeds=[])]),
        msg("最旧 仓位 A", mid=1, webhook_id=WEBHOOK, author=12345),
    ]
    client, engine, notifier, channel = await ready_client(history=history)
    assert notifier.flushes == 1
    assert notifier.messages[0] == ("🤖 跟单机器人已上线（模式: DRY-RUN）", "info", False)
    assert channel.history_limits == [BOOTSTRAP_HISTORY_LIMIT]
    assert engine.bootstrapped == ["最旧 仓位 A", "转发 仓位 B", "最新 仓位 C"]
    assert engine.handled == []  # bootstrap never goes through handle_text


async def test_on_ready_live_mode_text():
    _, _, notifier, _ = await ready_client(cfg=make_cfg(dry_run=False))
    assert notifier.messages[0][0] == "🤖 跟单机器人已上线（模式: 实盘）"


async def test_on_ready_second_time_does_not_bootstrap_again():
    client, engine, notifier, channel = await ready_client(history=[msg("仓位 A", mid=1)])
    await client.on_ready()  # reconnect with a new gateway session
    assert notifier.flushes == 2
    assert engine.bootstrapped == ["仓位 A"]
    assert channel.history_limits == [BOOTSTRAP_HISTORY_LIMIT]
    assert len([m for m in notifier.messages if "已上线" in m[0]]) == 1


async def test_bootstrap_uses_fetch_channel_when_not_cached():
    client, engine, _, channel = make_client(history=[msg("仓位 A", mid=1)])
    client.get_channel = lambda cid: None  # type: ignore[method-assign]
    fetched: list[int] = []

    async def fetch_channel(cid: int):
        fetched.append(cid)
        return channel

    client.fetch_channel = fetch_channel  # type: ignore[method-assign]
    await client.on_ready()
    assert fetched == [SIGNAL_CH]
    assert engine.bootstrapped == ["仓位 A"]


async def test_bootstrap_history_error_warns_and_does_not_crash():
    client, engine, notifier, channel = make_client()
    channel.history_error = RuntimeError("Missing Access")
    await client.on_ready()
    assert engine.bootstrapped == []
    assert any(level == "warn" and "读取信号频道历史失败" in text for text, level, _ in notifier.messages)
    # live signals are not blocked by the failed bootstrap
    await client.on_message(msg(SIGNAL, mid=50))
    assert len(engine.handled) == 1


async def test_bootstrap_engine_error_on_one_message_continues():
    class Picky(FakeEngine):
        async def bootstrap_from_text(self, text: str) -> bool:
            if "坏" in text:
                raise ValueError("bad")
            return await super().bootstrap_from_text(text)

    _, engine, _, _ = await ready_client(history=[msg("好 2", mid=2), msg("坏 1", mid=1)], engine=Picky())
    assert engine.bootstrapped == ["好 2"]


async def test_signal_message_goes_to_engine_with_meta():
    client, engine, notifier, _ = await ready_client()
    await client.on_message(msg(SIGNAL, mid=123456789))
    assert len(engine.handled) == 1
    text, meta = engine.handled[0]
    assert text == SIGNAL
    assert meta == MessageMeta("123456789", SIGNAL_AUTHOR, T0)
    assert meta.received_at.tzinfo is not None


async def test_signal_from_webhook_and_forward_snapshot():
    client, engine, _, _ = await ready_client()
    snap = SimpleNamespace(content=SIGNAL, embeds=[])
    await client.on_message(msg("", mid=7, author=4242, webhook_id=WEBHOOK, snapshots=[snap]))
    assert [t for t, _ in engine.handled] == [SIGNAL]
    assert engine.handled[0][1].author_id == 4242


async def test_signal_from_not_allowed_author_is_ignored():
    client, engine, _, _ = await ready_client()
    await client.on_message(msg(SIGNAL, author=777))
    assert engine.handled == []


async def test_signal_allowed_when_allow_list_empty():
    client, engine, _, _ = await ready_client(cfg=make_cfg(allowed=[]))
    await client.on_message(msg(SIGNAL, author=777))
    assert len(engine.handled) == 1


async def test_own_messages_and_other_channels_are_ignored():
    client, engine, _, _ = await ready_client()
    await client.on_message(msg(SIGNAL, author=BOT_ID))
    await client.on_message(msg(SIGNAL, channel=555))
    await client.on_message(msg("", mid=9))  # empty text
    assert engine.handled == []


async def test_engine_error_is_notified_with_mention():
    client, engine, notifier, _ = await ready_client()
    engine.fail = "handle_text"
    await client.on_message(msg(SIGNAL, mid=42))  # must not raise
    text, level, mention = notifier.messages[-1]
    assert level == "error" and mention is True
    assert "42" in text and "handle_text exploded" in text


async def test_signal_without_engine_is_notified():
    client, _, notifier, _ = await ready_client()
    client.engine = None
    await client.on_message(msg(SIGNAL, mid=43))
    text, level, mention = notifier.messages[-1]
    assert level == "error" and mention is True and "43" in text


async def test_signals_are_handled_in_arrival_order():
    client, engine, _, _ = await ready_client()
    engine.handle_delay = 0.01
    await asyncio.gather(*(client.on_message(msg(f"仓位 {i}", mid=100 + i)) for i in range(5)))
    assert [t for t, _ in engine.handled] == [f"仓位 {i}" for i in range(5)]


async def test_live_signal_before_on_ready_waits_for_bootstrap():
    """discord.py may dispatch on_message before on_ready: history must not override it."""
    live = msg("最新 仓位 LIVE", mid=10)
    client, engine, _, _ = make_client(history=[live, msg("旧 仓位 OLD", mid=5)])
    order: list[str] = []
    orig_handle, orig_boot = engine.handle_text, engine.bootstrap_from_text

    async def handle_text(text, meta):
        order.append("live:" + text)
        return await orig_handle(text, meta)

    async def bootstrap_from_text(text):
        order.append("boot:" + text)
        return await orig_boot(text)

    engine.handle_text = handle_text  # type: ignore[method-assign]
    engine.bootstrap_from_text = bootstrap_from_text  # type: ignore[method-assign]

    live_task = asyncio.create_task(client.on_message(live))
    await asyncio.sleep(0.01)
    assert order == []  # waiting for the bootstrap
    await client.on_ready()
    await live_task
    # the live message is not bootstrapped (it is handled live), and comes last
    assert order == ["boot:旧 仓位 OLD", "live:最新 仓位 LIVE"]


async def test_live_signal_handled_after_wait_timeout_blocks_older_bootstrap():
    live = msg("最新 仓位 LIVE", mid=10)
    client, engine, _, _ = make_client(history=[live, msg("旧 仓位 OLD", mid=5)])
    client.bootstrap_wait_seconds = 0.01
    await client.on_message(live)  # on_ready did not come in time
    assert [t for t, _ in engine.handled] == ["最新 仓位 LIVE"]
    await client.on_ready()
    assert engine.bootstrapped == []  # nothing newer than the handled live message


# --------------------------------------------------------------------------- commands via on_message


async def test_owner_command_replies_in_channel():
    client, engine, _, _ = await ready_client()
    ch = FakeChannel(CONTROL_CH)
    await client.on_message(msg("!status", author=OWNER, channel_obj=ch))
    await client.on_message(msg("!pause", author=OWNER, channel_obj=ch))
    await client.on_message(msg("!sync confirm", author=OWNER, channel_obj=ch))
    await client.on_message(msg("!foo", author=OWNER, channel_obj=ch))
    assert ch.sent[:3] == ["账户状态 OK", PAUSED_TEXT, "已执行"]
    assert "!help" in ch.sent[3]
    assert engine.paused is True


async def test_non_owner_and_non_command_are_ignored():
    client, engine, _, _ = await ready_client()
    ch = FakeChannel(CONTROL_CH)
    await client.on_message(msg("!pause", author=777, channel_obj=ch))
    await client.on_message(msg("hello", author=OWNER, channel_obj=ch))
    assert ch.sent == [] and engine.paused is False


async def test_commands_in_signal_channel_are_ignored():
    client, engine, _, _ = await ready_client()
    ch = FakeChannel(SIGNAL_CH)
    await client.on_message(msg("!pause", author=OWNER, channel_obj=ch))
    assert ch.sent == [] and engine.paused is False


async def test_command_channel_defaults_to_log_channel():
    client, engine, _, _ = await ready_client(cfg=make_cfg(control=None))
    ch = FakeChannel(LOG_CH)
    await client.on_message(msg("!help", author=OWNER, channel_obj=ch))
    assert ch.sent == [HELP_TEXT]


async def test_long_command_reply_is_split():
    class Verbose(FakeEngine):
        async def status_text(self) -> str:
            return "\n".join(f"持仓 {i:03d} " + "x" * 50 for i in range(100))

    client, _, _, _ = await ready_client(engine=Verbose())
    ch = FakeChannel(CONTROL_CH)
    await client.on_message(msg("!status", author=OWNER, channel_obj=ch))
    assert len(ch.sent) > 1 and all(len(s) <= 1900 for s in ch.sent)
