"""Parse natural-language time expressions into seconds or ranges.

Supports all formats required by section 6.6 of the master prompt:
  - MM:SS          e.g.  1:20   → 80.0
  - H:MM:SS        e.g.  1:02:03 → 3723.0
  - N seconds      e.g.  80 seconds → 80.0
  - N minutes      e.g.  two minutes → 120.0
  - compound       e.g.  a minute twenty → 80.0
  - compound       e.g.  a minute and twenty seconds → 80.0
  - written num    e.g.  twenty five seconds → 25.0
  - first N        e.g.  the first 30 seconds → TimeRange(start=0, end=30)
  - last N         e.g.  the last 10 seconds  → TimeRange(from_end=10)
  - at T           e.g.  at 5:00 → TimePoint(300.0)
  - from X to Y    e.g.  from 1:00 to 1:30 → TimeRange(start=60, end=90)
  - between X and Y e.g. between 0:30 and 0:40 → TimeRange(start=30, end=40)

All functions are pure — no I/O, no side effects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ── Number-word tables ────────────────────────────────────────────

_ONES: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_TENS: dict[str, int] = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
}
_MULTIPLIERS: dict[str, int] = {
    "hundred": 100, "thousand": 1000,
}
_UNIT: dict[str, float] = {
    "second": 1.0, "seconds": 1.0, "sec": 1.0, "secs": 1.0,
    "minute": 60.0, "minutes": 60.0, "min": 60.0, "mins": 60.0,
    "hour": 3600.0, "hours": 3600.0, "hr": 3600.0, "hrs": 3600.0,
}


# ── Result types ──────────────────────────────────────────────────


@dataclass
class TimePoint:
    """A single instant in time, in seconds."""
    seconds: float


@dataclass
class TimeRange:
    """A range [start, end) in seconds.

    from_start: set for 'the first N seconds' (start=0, end=N)
    from_end:   set for 'the last N seconds' (needs duration to resolve)
    """
    start: Optional[float] = None
    end: Optional[float] = None
    from_start: Optional[float] = None
    from_end: Optional[float] = None


# ── Number-word parser ────────────────────────────────────────────


def _parse_number_words(text: str) -> float | None:
    """Parse written-out numbers: 'twenty five', 'a hundred', 'two'.

    Handles: single words, compound tens+ones ('twenty five'), 'a'/'an' as 1.
    Returns the numeric value or None.
    """
    text = text.strip().lower()
    if not text:
        return None
    if text in ("a", "an"):
        return 1.0

    tokens = text.split()
    total = 0.0
    current = 0.0
    for tok in tokens:
        if tok in ("and",):
            continue
        if tok in ("a", "an"):
            current += 1
        elif tok in _ONES:
            current += _ONES[tok]
        elif tok in _TENS:
            current += _TENS[tok]
        elif tok in _MULTIPLIERS:
            if current == 0:
                current = 1
            current *= _MULTIPLIERS[tok]
            total += current
            current = 0.0
        else:
            try:
                current += float(tok)
            except ValueError:
                return None
    total += current
    return total if (total > 0 or text in ("zero", "0")) else None


# ── Timestamp parser ──────────────────────────────────────────────


def _parse_timestamp(text: str) -> float | None:
    """Parse H:MM:SS or MM:SS timestamps.  Returns seconds or None."""
    text = text.strip()
    m = re.fullmatch(r"(\d+):(\d{1,2}):(\d{1,2})", text)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    m = re.fullmatch(r"(\d+):(\d{1,2})", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


# ── Duration-phrase parser ────────────────────────────────────────


def _parse_duration(text: str) -> float | None:
    """Parse a duration phrase and return seconds.

    Handles:
    - MM:SS / H:MM:SS
    - N unit  (e.g. '80 seconds', '2 minutes')
    - word-number unit  (e.g. 'twenty five seconds', 'two minutes')
    - 'a minute [and] N [seconds]'  (e.g. 'a minute twenty', 'a minute and 20 seconds')
    - bare number (treated as seconds)
    """
    text = text.strip().lower()

    # Timestamp format
    ts = _parse_timestamp(text)
    if ts is not None:
        return ts

    # 'a minute [and] N [seconds]' — handles 'a minute twenty', 'a minute and twenty seconds'
    m = re.fullmatch(
        r"(?:a\s+)?minute(?:\s+and)?\s+(.+?)(?:\s+seconds?)?",
        text,
    )
    if m:
        secs_part = _parse_duration(m.group(1))
        if secs_part is not None:
            return 60.0 + secs_part

    # Numeric prefix + unit:  '80 seconds', '1.5 minutes'
    m = re.fullmatch(
        r"([\d.]+)\s+(" + "|".join(re.escape(k) for k in _UNIT) + r")",
        text,
    )
    if m:
        return float(m.group(1)) * _UNIT[m.group(2)]

    # Word-number prefix + unit: 'twenty five seconds', 'two minutes'
    unit_pattern = "|".join(re.escape(k) for k in sorted(_UNIT, key=len, reverse=True))
    m = re.fullmatch(rf"(.+?)\s+({unit_pattern})", text)
    if m:
        num = _parse_number_words(m.group(1))
        if num is not None:
            return num * _UNIT[m.group(2)]

    # Bare numeric (seconds)
    try:
        return float(text)
    except ValueError:
        pass

    # Bare number word (treated as seconds for plain references)
    num = _parse_number_words(text)
    if num is not None:
        return num

    return None


# ── Main entry point ──────────────────────────────────────────────


def parse_time_expression(text: str) -> TimePoint | TimeRange | None:
    """Parse a complete time expression into a TimePoint or TimeRange.

    Supports all formats from master prompt section 6.6.
    Returns None if the expression cannot be parsed.
    """
    t = text.strip().lower()
    # Remove leading articles that don't carry meaning
    t = re.sub(r"^the\s+", "", t)

    # 'first N seconds'
    m = re.fullmatch(r"first\s+(.+)", t)
    if m:
        n = _parse_duration(m.group(1))
        if n is None:
            # try treating as bare number (seconds)
            n = _parse_number_words(m.group(1))
        if n is not None:
            return TimeRange(start=0.0, end=n, from_start=n)

    # 'last N seconds'
    m = re.fullmatch(r"last\s+(.+)", t)
    if m:
        n = _parse_duration(m.group(1))
        if n is None:
            n = _parse_number_words(m.group(1))
        if n is not None:
            return TimeRange(from_end=n)

    # 'from X to Y'
    m = re.fullmatch(r"from\s+(.+?)\s+to\s+(.+)", t)
    if m:
        start = _parse_duration(m.group(1))
        end = _parse_duration(m.group(2))
        if start is not None and end is not None:
            return TimeRange(start=start, end=end)

    # 'between X and Y'
    m = re.fullmatch(r"between\s+(.+?)\s+and\s+(.+)", t)
    if m:
        start = _parse_duration(m.group(1))
        end = _parse_duration(m.group(2))
        if start is not None and end is not None:
            return TimeRange(start=start, end=end)

    # 'at X'
    m = re.fullmatch(r"at\s+(.+)", t)
    if m:
        pt = _parse_duration(m.group(1))
        if pt is not None:
            return TimePoint(seconds=pt)

    # Plain duration / timestamp
    pt = _parse_duration(t)
    if pt is not None:
        return TimePoint(seconds=pt)

    return None


def resolve_time_range(
    tr: TimeRange,
    total_duration_s: float,
) -> tuple[float, float] | None:
    """Resolve a TimeRange to absolute (start_s, end_s).

    For 'the last N seconds', uses total_duration_s to anchor the start.
    Returns None if the range is underspecified.
    """
    if tr.from_end is not None:
        start = max(0.0, total_duration_s - tr.from_end)
        return (start, total_duration_s)
    if tr.from_start is not None:
        return (0.0, tr.from_start)
    if tr.start is not None and tr.end is not None:
        return (tr.start, tr.end)
    return None
