"""Tests for copybot.parser (DESIGN.md §1, §3.1)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pytest

from copybot.models import Change, Holding
from copybot.parser import NY, normalize_text, parse_signal, suspicious_lines

# Exact example from DESIGN.md §1.
EXAMPLE = """组合收益
10月收益：3.4%
总收益：168.3%
仓位变动
建仓 WDC | 仓位：0.0% → 100.0% | 成交价：422.6
清仓 BE | 仓位：50.0% → 0.0% | 成交价：无
清仓 ARM | 仓位：50.0% → 0.0% | 成交价：无
持仓明细
WDC | 仓位：100.0% | 成本：422.6 | 盈亏：+0.1%
(2026-10-02 09:50:59 美东)"""

EXPECTED_CHANGES = (
    Change("建仓", "WDC", 0.0, 1.0, 422.6),
    Change("清仓", "BE", 0.5, 0.0, None),
    Change("清仓", "ARM", 0.5, 0.0, None),
)
EXPECTED_HOLDINGS = (Holding("WDC", 1.0, 422.6, 0.1),)
EXPECTED_TS = datetime(2026, 10, 2, 9, 50, 59, tzinfo=NY)

# Same content with half-width colons, ASCII/other arrows, extra spaces, markdown,
# CRLF, zero-width chars, NBSP, full-width parentheses / pipe, emoji variation selector.
DECORATED = (
    "**组合收益**\r\n"
    "10月收益: 3.4%\r\n"
    "> 总收益:168.3%\r\n"
    "> **仓位变动**\r\n"
    "**建仓**  WDC​ |  仓位:0.0%  ->  100.0%  |  成交价: 422.6\r\n"
    "- 清仓 `BE` | 仓位 : 50.0 % => 0.0 % | 成交价 : 无\r\n"
    "清仓 ARM｜仓位：50.0%➡️0.0%｜成交价：无\r\n"
    "\r\n"
    "__持仓明细__\r\n"
    "﻿WDC | 仓位：100.0% | 成本：422.6 | 盈亏：+0.1%\r\n"
    "（2026-10-02 09:50:59 美东）\r\n"
)


def _sig(text: str):
    sig = parse_signal(text)
    assert sig is not None, f"expected a signal for: {text!r}"
    return sig


# --------------------------------------------------------------------------- 1. exact example


def test_exact_example():
    sig = _sig(EXAMPLE)
    assert sig.changes == EXPECTED_CHANGES
    assert sig.holdings == EXPECTED_HOLDINGS
    assert sig.has_holdings_section is True
    assert sig.timestamp == EXPECTED_TS
    assert sig.timestamp.tzinfo is not None
    assert sig.timestamp.utcoffset() == timedelta(hours=-4)  # EDT in October
    assert sig.raw_text == EXAMPLE
    assert sig.fingerprint == hashlib.sha256(normalize_text(EXAMPLE).encode("utf-8")).hexdigest()
    assert len(sig.fingerprint) == 64
    assert sig.holding_weights() == {"WDC": 1.0}


def test_example_directions():
    wdc, be, arm = _sig(EXAMPLE).changes
    assert wdc.is_increase and not wdc.is_decrease
    assert be.is_decrease and arm.is_decrease


# --------------------------------------------------------------------------- 2. decorated variant


def test_decorated_variant_parses_identically():
    sig = _sig(DECORATED)
    assert sig.changes == EXPECTED_CHANGES
    assert sig.holdings == EXPECTED_HOLDINGS
    assert sig.has_holdings_section is True
    assert sig.timestamp == EXPECTED_TS
    assert sig.raw_text == DECORATED


def test_fingerprint_stable_for_same_text():
    assert _sig(EXAMPLE).fingerprint == _sig(EXAMPLE).fingerprint
    assert _sig(DECORATED).fingerprint == _sig(DECORATED).fingerprint


def test_fingerprint_ignores_pure_transport_noise():
    # CRLF, zero-width chars, bold markers and trailing spaces vanish in normalization.
    noisy = "\r\n".join("**" + line + "**​  " for line in EXAMPLE.split("\n"))
    assert _sig(noisy).fingerprint == _sig(EXAMPLE).fingerprint


def test_fingerprint_differs_for_different_signals():
    other = EXAMPLE.replace("09:50:59", "09:51:00")
    assert _sig(other).fingerprint != _sig(EXAMPLE).fingerprint


def test_normalize_text_basic_rules():
    text = "> **建仓**  WDC​ ｜ 仓位：0.0% -> 100.0%\r\n\r\n  - `成交价`：1，234.50  \r\n"
    assert normalize_text(text) == "建仓 WDC | 仓位:0.0% → 100.0%\n成交价:1,234.50"


@pytest.mark.parametrize("arrow", ["->", "=>", "⟶", "➡", "➡️", "→", "-->", "－＞"])
def test_normalize_unifies_arrows(arrow):
    assert normalize_text(f"0.0% {arrow} 100.0%") == "0.0% → 100.0%"


def test_normalize_is_idempotent():
    for text in (EXAMPLE, DECORATED):
        once = normalize_text(text)
        assert normalize_text(once) == once


def test_normalize_full_width_ascii_and_spaces():
    text = "建仓　ＷＤＣ　｜　仓位：０.０％　→　１００％"
    assert normalize_text(text) == "建仓 WDC | 仓位:0.0% → 100%"


def test_normalize_strips_quote_bullets_headings_and_escapes():
    text = ">>> # 仓位变动\n> - 建仓 WDC \\| 仓位:0% → 10%\n* ~~x~~"
    assert normalize_text(text) == "仓位变动\n建仓 WDC | 仓位:0% → 10%\nx"


# --------------------------------------------------------------------------- 3. 加仓 / 减仓, numbers


def test_add_and_reduce_lines():
    text = (
        "仓位变动\n"
        "加仓 NVDA | 仓位：25.0% → 50.0% | 成交价：181.35\n"
        "减仓 TSLA | 仓位：40% → 20% | 成交价：无\n"
        "建仓 AMD | 仓位：0% → 33.33% | 成交价：1,234.50\n"
    )
    nvda, tsla, amd = _sig(text).changes
    assert nvda == Change("加仓", "NVDA", 0.25, 0.5, 181.35)
    assert nvda.is_increase
    assert tsla == Change("减仓", "TSLA", 0.4, 0.2, None)
    assert tsla.is_decrease
    assert amd.symbol == "AMD"
    assert amd.to_weight == pytest.approx(0.3333)
    assert amd.price == pytest.approx(1234.5)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("422.6", 422.6),
        ("1,234.50", 1234.5),
        ("12,345,678", 12345678.0),
        ("$1,234.50", 1234.5),
        ("181.35 USD", 181.35),
        ("181.35美元", 181.35),
        ("1，234.50", 1234.5),  # full-width comma inside the number
        ("０.５", 0.5),
        ("无", None),
        ("暂无", None),
        ("-", None),
        ("--", None),
        ("N/A", None),
        ("n/a", None),
        ("", None),
        ("0", None),
        ("nan", None),
        ("inf", None),
        ("422,6", None),  # decimal comma: refuse rather than read 4226
        ("1,23", None),
        ("abc", None),
    ],
)
def test_change_price_variants(raw, expected):
    sig = _sig(f"建仓 WDC | 仓位：0% → 10% | 成交价：{raw}")
    assert sig.changes[0].price == (pytest.approx(expected) if expected is not None else None)


def test_change_without_price_field():
    (change,) = _sig("建仓 WDC | 仓位：0% → 10%").changes
    assert change == Change("建仓", "WDC", 0.0, 0.1, None)


def test_duplicate_symbol_changes_kept_in_order():
    text = "加仓 NVDA | 仓位：10% → 20% | 成交价：100\n加仓 NVDA | 仓位：20% → 30% | 成交价：101"
    changes = _sig(text).changes
    assert [(c.symbol, c.from_weight, c.to_weight) for c in changes] == [("NVDA", 0.1, 0.2), ("NVDA", 0.2, 0.3)]


def test_any_cjk_action_word_is_accepted():
    (change,) = _sig("调仓 WDC | 仓位：30% → 10% | 成交价：无").changes
    assert change.action == "调仓" and change.is_decrease


def test_action_word_longer_than_four_chars_is_not_a_change():
    assert parse_signal("我觉得可以建仓 WDC | 仓位：0% → 100% | 成交价：422.6") is None


# --------------------------------------------------------------------------- 4. symbols


def test_symbol_with_dot_and_lowercase():
    text = (
        "建仓 brk.b | 仓位：0% → 10% | 成交价：480\n"
        "持仓明细\n"
        "wdc | 仓位：90% | 成本：422.6 | 盈亏：+1%\n"
        "BRK.B | 仓位：10% | 成本：480 | 盈亏：0%"
    )
    sig = _sig(text)
    assert sig.changes[0].symbol == "BRK.B"
    assert [h.symbol for h in sig.holdings] == ["WDC", "BRK.B"]


def test_symbol_with_dash_and_digits():
    (change,) = _sig("建仓 BF-B | 仓位：0% → 5% | 成交价：50").changes
    assert change.symbol == "BF-B"


# --------------------------------------------------------------------------- 5. holdings


def test_negative_pnl_and_missing_cost():
    text = "持仓明细\nNVDA | 仓位：60.0% | 成本：无 | 盈亏：-3.2%\nAMD | 仓位：40% | 成本：1,234.5 | 盈亏：−0.5%"
    sig = _sig(text)
    assert sig.holdings == (Holding("NVDA", 0.6, None, -3.2), Holding("AMD", 0.4, 1234.5, -0.5))


def test_holding_without_optional_fields():
    sig = _sig("持仓明细\nWDC | 仓位：100%")
    assert sig.holdings == (Holding("WDC", 1.0, None, None),)
    assert sig.has_holdings_section is True


# --------------------------------------------------------------------------- 6. empty holdings section


def test_holdings_header_without_lines_means_all_cash():
    text = "仓位变动\n清仓 WDC | 仓位：100.0% → 0.0% | 成交价：无\n持仓明细\n(2026-10-05 10:00:00 美东)"
    sig = _sig(text)
    assert sig.has_holdings_section is True
    assert sig.holdings == ()
    assert sig.changes == (Change("清仓", "WDC", 1.0, 0.0, None),)


@pytest.mark.parametrize(
    "header", ["持仓明细", "**持仓明细**", "【持仓明细】", "📊 持仓明细：", "持仓明细：无", "持仓明细: 空仓"]
)
def test_decorated_or_explicitly_empty_header(header):
    sig = _sig(header)
    assert sig.has_holdings_section is True
    assert sig.holdings == ()


# --------------------------------------------------------------------------- 7. signal or not


def test_only_holdings_section_is_a_signal():
    sig = _sig("组合收益\n持仓明细\nWDC | 仓位：100.0% | 成本：422.6 | 盈亏：+0.1%")
    assert sig.changes == ()
    assert sig.holdings == EXPECTED_HOLDINGS
    assert sig.has_holdings_section is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "今天大盘不错",
        "WDC 不错 | 观望",
        "组合收益\n10月收益：3.4%\n总收益：168.3%",
        "仓位变动\n今天没有操作\n(2026-10-02 09:50:59 美东)",
        "仓位变动\n建仓 WDC 仓位 0 → 100",
        "WDC | 仓位：100.0% | 成本：422.6 | 盈亏：+0.1%",  # holding line without the header
        "持仓明细稍后更新",  # chat, not a header (would otherwise mean "100% cash")
        "今晚会更新持仓明细",
    ],
)
def test_non_signals(text):
    assert parse_signal(text) is None


def test_holding_lines_without_header_are_parsed_but_section_unknown():
    text = "建仓 WDC | 仓位：0% → 100% | 成交价：422.6\nWDC | 仓位：100% | 成本：422.6 | 盈亏：0%"
    sig = _sig(text)
    assert sig.holdings == (Holding("WDC", 1.0, 422.6, 0.0),)
    assert sig.has_holdings_section is False


def test_section_order_does_not_matter():
    lines = EXAMPLE.split("\n")
    reordered = "\n".join(lines[7:9] + lines[3:7] + lines[9:] + lines[:3])
    sig = _sig(reordered)
    assert sig.changes == EXPECTED_CHANGES
    assert sig.holdings == EXPECTED_HOLDINGS
    assert sig.timestamp == EXPECTED_TS


# --------------------------------------------------------------------------- safety: unknown holdings


@pytest.mark.parametrize(
    "bad_line",
    [
        "WDC | 仓位：100.0% | 成本：422.6 | 盈亏：+0.1% | 市值：42260",  # extra field
        "WDC 100%",  # different layout
        "WDC | 仓位：100.0% | 盈亏：+0.1% | 成本：422.6",  # fields swapped
        "WDC | 仓位：.5%",
    ],
)
def test_unparseable_holding_line_makes_holdings_unknown(bad_line):
    text = f"仓位变动\n建仓 WDC | 仓位：0.0% → 100.0% | 成交价：422.6\n持仓明细\n{bad_line}\n(2026-10-02 09:50:59 美东)"
    sig = _sig(text)
    # Never "header present + nothing parsed" (= provider all cash) when a line was dropped.
    assert sig.has_holdings_section is False
    assert sig.holdings == ()
    assert sig.changes == (Change("建仓", "WDC", 0.0, 1.0, 422.6),)
    assert suspicious_lines(text) == [normalize_text(bad_line)]


def test_unparseable_change_line_is_reported_and_holdings_distrusted():
    text = EXAMPLE.replace("清仓 BE | 仓位：50.0% → 0.0% | 成交价：无", "清仓 BE | 仓位：50.0% 0.0% | 成交价：无")
    sig = _sig(text)
    assert [c.symbol for c in sig.changes] == ["WDC", "ARM"]
    assert sig.has_holdings_section is False
    assert sig.holdings == ()
    assert suspicious_lines(text) == ["清仓 BE | 仓位:50.0% 0.0% | 成交价:无"]


def test_clean_example_has_no_suspicious_lines():
    assert suspicious_lines(EXAMPLE) == []
    assert suspicious_lines(DECORATED) == []
    assert suspicious_lines("") == []


def test_header_with_extra_text_is_not_a_header():
    sig = _sig("建仓 WDC | 仓位：0% → 100% | 成交价：422.6\n持仓明细(共0只)")
    assert sig.has_holdings_section is False


# --------------------------------------------------------------------------- tolerated layouts


def test_embed_field_flattened_on_one_line():
    text = (
        "仓位变动: 建仓 WDC | 仓位：0.0% → 100.0% | 成交价：422.6\n"
        "持仓明细: WDC | 仓位：100.0% | 成本：422.6 | 盈亏：+0.1%"
    )
    sig = _sig(text)
    assert sig.changes == (Change("建仓", "WDC", 0.0, 1.0, 422.6),)
    assert sig.holdings == EXPECTED_HOLDINGS
    assert sig.has_holdings_section is True


def test_leading_emoji_or_cashtag_before_data_lines():
    text = "📈 建仓 WDC | 仓位：0% → 100% | 成交价：422.6\n持仓明细\n🟢 $WDC | 仓位：100% | 成本：422.6 | 盈亏：+0.1%"
    sig = _sig(text)
    assert sig.changes == (Change("建仓", "WDC", 0.0, 1.0, 422.6),)
    assert sig.holdings == EXPECTED_HOLDINGS
    assert sig.has_holdings_section is True


def test_weights_signs_and_large_values():
    (change,) = _sig("建仓 WDC | 仓位：-0.0% → +150% | 成交价：1").changes
    assert change.from_weight == 0.0
    assert change.to_weight == 1.5


# --------------------------------------------------------------------------- 8. timestamps


def test_no_timestamp():
    sig = _sig("建仓 WDC | 仓位：0% → 10% | 成交价：1")
    assert sig.timestamp is None


def test_timestamp_with_slashes_and_no_seconds():
    sig = _sig("建仓 WDC | 仓位：0% → 10% | 成交价：1\n(2026/1/5 9:31 美东)")
    assert sig.timestamp == datetime(2026, 1, 5, 9, 31, 0, tzinfo=NY)


@pytest.mark.parametrize("suffix", ["", " 美东", " 美东时间", " ET", " EST", " EDT"])
def test_timestamp_suffixes(suffix):
    sig = _sig(f"持仓明细\n(2026-10-02 09:50:59{suffix})")
    assert sig.timestamp == EXPECTED_TS


def test_last_timestamp_wins():
    sig = _sig("持仓明细\n(2026-10-01 15:00:00 美东)\n(2026-10-02 09:50:59 美东)")
    assert sig.timestamp == EXPECTED_TS


@pytest.mark.parametrize("bad", ["2026-02-30 10:00:00", "2026-10-02 25:00:00", "2026-13-01 10:00", "2026-10-02 10:61"])
def test_invalid_timestamp_is_none(bad):
    assert _sig(f"持仓明细\n({bad} 美东)").timestamp is None


def test_timestamp_full_width_digits():
    sig = _sig("持仓明细\n（２０２６-１０-０２ ０９:５０:５９ 美东）")
    assert sig.timestamp == EXPECTED_TS


# --------------------------------------------------------------------------- 9. DST


def test_winter_and_summer_offsets():
    winter = _sig("持仓明细\n(2026-12-01 10:00:00 美东)").timestamp
    summer = _sig("持仓明细\n(2026-07-01 10:00:00 美东)").timestamp
    assert winter.utcoffset() == timedelta(hours=-5)
    assert summer.utcoffset() == timedelta(hours=-4)
    assert winter.astimezone(NY).hour == 10


def test_dst_transition_days():
    # 2026-03-08: clocks jump 02:00 -> 03:00; 2026-11-01: 01:00-02:00 happens twice.
    before = _sig("持仓明细\n(2026-03-08 01:59:00 美东)").timestamp
    after = _sig("持仓明细\n(2026-03-08 03:00:00 美东)").timestamp
    assert before.utcoffset() == timedelta(hours=-5)
    assert after.utcoffset() == timedelta(hours=-4)
    gap = _sig("持仓明细\n(2026-03-08 02:30:00 美东)").timestamp  # non-existent: no crash
    ambiguous = _sig("持仓明细\n(2026-11-01 01:30:00 美东)").timestamp  # fold=0 -> EDT
    assert gap is not None and gap.tzinfo is NY
    assert ambiguous.utcoffset() == timedelta(hours=-4)
    assert _sig("持仓明细\n(2026-11-01 02:30:00 美东)").timestamp.utcoffset() == timedelta(hours=-5)
