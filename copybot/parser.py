"""Parse the signal provider's portfolio-update messages (DESIGN.md §1 and §3.1).

Pure module: no I/O and no clock. `parse_signal` turns the (already flattened)
Discord text into a `Signal`; `normalize_text` also defines the de-duplication
fingerprint.

Safety rules on top of the bare format (a present-but-empty「持仓明细」section means
"the provider is 100% cash", so false positives there are dangerous):

* The「持仓明细」header only counts when it stands on its own line, optionally
  decorated (``**持仓明细**``, ``【持仓明细】``, ``持仓明细：``) or followed by an
  explicit empty marker (``持仓明细：无`` / ``空仓``). Chat such as
  ``持仓明细稍后更新`` is not a header.
* ``持仓明细: WDC | 仓位: ...`` / ``仓位变动: 建仓 WDC | ...`` (an embed field
  flattened onto one line) is split into the header and the data line.
* Leading emoji / "$" / brackets before a data line are ignored ("📈建仓 WDC | ...").
* If any line looks like a change / holding line but cannot be parsed, the holdings
  are reported as unknown (``has_holdings_section=False``, ``holdings=()``) instead
  of silently dropping a position. Parsed change lines are still returned. Use
  `suspicious_lines` to warn the user about such lines.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import Change, Holding, Signal

__all__ = ["NY", "normalize_text", "parse_signal", "suspicious_lines"]

NY = ZoneInfo("America/New_York")

# --------------------------------------------------------------------------- normalization

# Invisible characters injected by Discord / copy-paste: zero-width space & joiners,
# LRM/RLM, bidi embeddings and isolates, word joiner, BOM, emoji variation selectors
# (e.g. "➡️" == "➡" + U+FE0F) and the soft hyphen.
_INVISIBLE = (
    [chr(c) for c in range(0x200B, 0x2010)]
    + [chr(c) for c in range(0x202A, 0x202F)]
    + [chr(c) for c in range(0x2060, 0x2065)]
    + [chr(c) for c in range(0x2066, 0x206A)]
    + ["\ufeff", "\ufe0e", "\ufe0f", "\u00ad"]
)
_DELETE_INVISIBLE = dict.fromkeys(map(ord, _INVISIBLE))

# Applied after NFKC (which already folds full-width ASCII forms such as "：" "（"
# "）" "｜" "％" "，" "１" "Ｗ"); kept explicit so the contract is visible here.
_SPACES = ["\t", "\u00a0", "\u1680", "\u202f", "\u205f", "\u3000"] + [chr(c) for c in range(0x2000, 0x200B)]
_CHAR_MAP = str.maketrans(
    {
        **{s: " " for s in _SPACES},
        "：": ":",
        "（": "(",
        "）": ")",
        "｜": "|",
        "％": "%",
        "，": ",",
        "\u2212": "-",  # MINUS SIGN, e.g. 盈亏：−3.2%
        "\u2502": "|",  # BOX DRAWINGS LIGHT VERTICAL used as a separator
        "\u2223": "|",  # DIVIDES used as a separator
    }
)

_ESCAPE_RE = re.compile(r"\\([^\w\s])")  # markdown escapes: "\*" -> "*"
_INLINE_MD_RE = re.compile(r"\*+|__+|~~+|`+")  # bold/italic(*), underline, strike, code
_ARROW_RE = re.compile(r"(?:-+|=+|[—–]+)>|[⟶⟹⇒➡➔➜⮕→]")
_LEADING_MARKERS_RE = re.compile(r"^(?:>+\s*|[-•·]\s+|-#\s+|#{1,3}\s+)+")  # quotes, bullets, headings
_MULTI_SPACE_RE = re.compile(r" {2,}")


def normalize_text(text: str) -> str:
    """Canonical form of a message: plain text, ASCII punctuation, one item per line.

    CRLF/CR -> LF, invisible chars removed, NBSP / full-width spaces -> " ", NFKC
    (full-width digits, letters and punctuation -> ASCII), markdown decoration
    removed, arrows unified to "→", spaces collapsed, lines stripped, empty lines
    dropped.
    """
    text = text.translate(_DELETE_INVISIBLE)
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_CHAR_MAP)
    text = _ESCAPE_RE.sub(r"\1", text)
    text = _INLINE_MD_RE.sub("", text)
    text = _ARROW_RE.sub("→", text)
    lines: list[str] = []
    for line in text.splitlines():  # handles \r\n, \r, \n, \u2028, \u2029 ...
        line = _LEADING_MARKERS_RE.sub("", line.strip())
        line = _MULTI_SPACE_RE.sub(" ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------------------- line patterns

# Numbers use [0-9] instead of \d so exotic Unicode digits are never accepted.
_NUM = r"[0-9]+(?:\.[0-9]+)?"
_SYM = r"[A-Za-z][A-Za-z0-9.\-]{0,9}"

# 建仓 WDC | 仓位:0.0% → 100.0% | 成交价:422.6
_CHANGE_RE = re.compile(
    rf"^(?P<action>[\u4e00-\u9fff]{{1,4}})\s*(?P<sym>{_SYM})\s*\|\s*仓位\s*:\s*"
    rf"(?P<from>[+-]?{_NUM})\s*%\s*→\s*(?P<to>[+-]?{_NUM})\s*%"
    rf"(?:\s*\|\s*成交价\s*:\s*(?P<price>[^|]*))?\s*$"
)

# WDC | 仓位:100.0% | 成本:422.6 | 盈亏:+0.1%
_HOLDING_RE = re.compile(
    rf"^(?P<sym>{_SYM})\s*\|\s*仓位\s*:\s*(?P<w>{_NUM})\s*%"
    rf"(?:\s*\|\s*成本\s*:\s*(?P<cost>[^|]*))?"
    rf"(?:\s*\|\s*盈亏\s*:\s*(?P<pnl>[+-]?{_NUM})\s*%)?\s*$"
)

# The header alone, with optional decoration and an optional explicit "empty" marker.
_HOLDINGS_HEADER_RE = re.compile(
    r"[\W_]*持仓明细[\W_]*(?:(?:无|暂无|空仓|空|none|empty)[\W_]*)?", re.IGNORECASE
)

# An embed field flattened onto one line: "持仓明细: WDC | ..." / "仓位变动: 建仓 WDC | ...".
_SECTION_PREFIX_RE = re.compile(r"^(?P<head>仓位变动|持仓明细)\s*:\s*(?P<rest>\S.*)$")

# Starts like a holding line (a symbol token followed by "|", ":", "仓位" or a number)
# but did not parse -> the holdings section cannot be trusted.
_HOLDING_LIKE_RE = re.compile(rf"^{_SYM}(?![A-Za-z0-9.\-])\s*(?:\||:|仓位|[+-]?[0-9])")

# (2026-10-02 09:50:59 美东); the zone suffix (美东/美东时间/ET/EST/EDT) is optional
# and the time is always interpreted as America/New_York.
_TIMESTAMP_RE = re.compile(
    r"(?<![0-9])([0-9]{4})[-/]([0-9]{1,2})[-/]([0-9]{1,2})\s+"
    r"([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?(?![0-9])"
)

_PRICE_DECORATION_RE = re.compile(r"^(?:US)?\$\s*|\s*(?:USD|美元|美金)$", re.IGNORECASE)
_THOUSANDS_RE = re.compile(r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?")
_PLAIN_NUMBER_RE = re.compile(_NUM)


# --------------------------------------------------------------------------- field parsers


def _parse_price(raw: str | None) -> float | None:
    """成交价 / 成本 field -> positive float, or None ("无", "-", "N/A", "暂无", garbage).

    Commas are only accepted as proper thousands separators ("1,234.50"); a decimal
    comma such as "422,6" is rejected rather than misread as 4226.
    """
    if raw is None:
        return None
    s = _PRICE_DECORATION_RE.sub("", raw.strip()).strip()
    if _THOUSANDS_RE.fullmatch(s):
        s = s.replace(",", "")
    if not _PLAIN_NUMBER_RE.fullmatch(s):
        return None
    value = float(s)
    return value if value > 0 else None


def _pct_to_fraction(raw: str) -> float:
    """'33.33' (percent) -> 0.3333; rounded to hide float noise, -0.0 -> 0.0."""
    return round(float(raw) / 100.0, 10) + 0.0


def _make_change(m: re.Match[str]) -> Change:
    return Change(
        action=m["action"],
        symbol=m["sym"].upper(),
        from_weight=_pct_to_fraction(m["from"]),
        to_weight=_pct_to_fraction(m["to"]),
        price=_parse_price(m["price"]),
    )


def _make_holding(m: re.Match[str]) -> Holding:
    return Holding(
        symbol=m["sym"].upper(),
        weight=_pct_to_fraction(m["w"]),
        cost=_parse_price(m["cost"]),
        pnl_pct=float(m["pnl"]) + 0.0 if m["pnl"] is not None else None,
    )


def _parse_timestamp(normalized: str) -> datetime | None:
    """Last 'YYYY-MM-DD HH:MM[:SS]' in the text as a tz-aware New York datetime."""
    matches = list(_TIMESTAMP_RE.finditer(normalized))
    if not matches:
        return None
    y, mo, d, h, mi, s = matches[-1].groups()
    try:
        # Ambiguous / non-existent DST times: zoneinfo's default fold=0 decides.
        return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=NY)
    except ValueError:
        return None


# --------------------------------------------------------------------------- line scanning

# Emoji, "$", "#", brackets ... in front of the data ("📈建仓 WDC | ...", "$WDC | ...").
_LEADING_SYMBOLS_RE = re.compile(r"^[\W_]+")


@dataclass
class _Scan:
    changes: list[Change] = field(default_factory=list)
    holdings: list[Holding] = field(default_factory=list)
    header_seen: bool = False
    suspicious: list[str] = field(default_factory=list)


def _expand_line(line: str) -> list[str]:
    """Split 'header: data line' into two lines when the data part parses."""
    m = _SECTION_PREFIX_RE.match(line)
    if m and (_CHANGE_RE.match(m["rest"]) or _HOLDING_RE.match(m["rest"])):
        return [m["head"], m["rest"]]
    return [line]


def _looks_like_position_line(line: str) -> bool:
    """Unparsed line that probably carries trading information."""
    if _HOLDING_LIKE_RE.match(line):
        return True
    return ("仓位" in line and "%" in line) or "持仓明细" in line or "成交价" in line


def _scan(normalized: str) -> _Scan:
    """Classify every line; section order is irrelevant."""
    scan = _Scan()
    for raw_line in normalized.split("\n"):
        for line in _expand_line(_LEADING_SYMBOLS_RE.sub("", raw_line)):
            if m := _CHANGE_RE.match(line):
                scan.changes.append(_make_change(m))
            elif m := _HOLDING_RE.match(line):
                scan.holdings.append(_make_holding(m))
            elif _HOLDINGS_HEADER_RE.fullmatch(line):
                scan.header_seen = True
            elif _looks_like_position_line(line):
                scan.suspicious.append(raw_line)
    return scan


# --------------------------------------------------------------------------- public API


def parse_signal(text: str) -> Signal | None:
    """Parse one message; None when it is not a signal.

    A signal has at least one change line or a「持仓明细」header. Weights are
    fractions (50.0% -> 0.5); symbols are upper-cased; duplicate change lines for a
    symbol are kept in order (the planner keeps the last one).
    """
    if not text:
        return None
    normalized = normalize_text(text)
    scan = _scan(normalized)
    if not scan.changes and not scan.header_seen:
        return None
    # Any unparsed position-like line may be a dropped holding: report the holdings
    # as unknown rather than risk "provider sold X" being inferred.
    holdings_trusted = not scan.suspicious
    return Signal(
        changes=tuple(scan.changes),
        holdings=tuple(scan.holdings) if holdings_trusted else (),
        has_holdings_section=scan.header_seen and holdings_trusted,
        timestamp=_parse_timestamp(normalized),
        raw_text=text,
        fingerprint=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )


def suspicious_lines(text: str) -> list[str]:
    """Normalized lines that look like change / holding lines but could not be parsed.

    Meant for a warning to the user (e.g. the provider changed the format).
    """
    if not text:
        return []
    return _scan(normalize_text(text)).suspicious
