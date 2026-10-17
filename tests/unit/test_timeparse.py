"""Tests for engine/timeparse.py — all formats from spec section 6.6.

Required by spec (Phase 1):
- "80 seconds" → 80.0
- "a minute twenty" → 80.0
- "1:02:03" → 3723.0
- "the last 10 seconds" → TimeRange(from_end=10)
And all other listed formats.
"""

from __future__ import annotations

import pytest

from incubrix.engine.timeparse import (
    TimePoint,
    TimeRange,
    parse_time_expression,
    resolve_time_range,
    _parse_duration,
    _parse_number_words,
    _parse_timestamp,
)


class TestParseTimestamp:
    def test_mm_ss(self):
        assert _parse_timestamp("1:20") == 80.0
        assert _parse_timestamp("0:30") == 30.0
        assert _parse_timestamp("10:00") == 600.0

    def test_h_mm_ss(self):
        assert _parse_timestamp("1:02:03") == 3723.0
        assert _parse_timestamp("0:01:30") == 90.0
        assert _parse_timestamp("2:00:00") == 7200.0

    def test_invalid(self):
        assert _parse_timestamp("hello") is None
        assert _parse_timestamp("") is None


class TestParseNumberWords:
    def test_simple_ones(self):
        assert _parse_number_words("five") == 5.0
        assert _parse_number_words("ten") == 10.0
        assert _parse_number_words("nineteen") == 19.0

    def test_tens(self):
        assert _parse_number_words("twenty") == 20.0
        assert _parse_number_words("thirty") == 30.0

    def test_compound(self):
        assert _parse_number_words("twenty five") == 25.0
        assert _parse_number_words("thirty one") == 31.0

    def test_a_and_an(self):
        assert _parse_number_words("a") == 1.0
        assert _parse_number_words("an") == 1.0

    def test_invalid(self):
        assert _parse_number_words("xyz abc") is None


class TestParseDuration:
    def test_numeric_seconds(self):
        assert _parse_duration("80 seconds") == 80.0
        assert _parse_duration("30 seconds") == 30.0
        assert _parse_duration("1 second") == 1.0

    def test_numeric_minutes(self):
        assert _parse_duration("2 minutes") == 120.0
        assert _parse_duration("1 minute") == 60.0
        assert _parse_duration("1.5 minutes") == 90.0

    def test_word_number_seconds(self):
        assert _parse_duration("twenty five seconds") == 25.0
        assert _parse_duration("ten seconds") == 10.0
        assert _parse_duration("two seconds") == 2.0

    def test_word_number_minutes(self):
        assert _parse_duration("two minutes") == 120.0
        assert _parse_duration("five minutes") == 300.0

    def test_a_minute_twenty(self):
        """'a minute twenty' → 60 + 20 = 80 seconds."""
        assert _parse_duration("a minute twenty") == 80.0

    def test_a_minute_and_twenty_seconds(self):
        assert _parse_duration("a minute and twenty seconds") == 80.0

    def test_timestamp_as_duration(self):
        assert _parse_duration("1:20") == 80.0
        assert _parse_duration("1:02:03") == 3723.0

    def test_bare_number(self):
        assert _parse_duration("30") == 30.0


class TestParseTimeExpression:
    """Full spec section 6.6 table tests."""

    # Simple timestamps
    def test_1_20(self):
        result = parse_time_expression("1:20")
        assert isinstance(result, TimePoint)
        assert result.seconds == 80.0

    def test_1_02_03(self):
        result = parse_time_expression("1:02:03")
        assert isinstance(result, TimePoint)
        assert result.seconds == 3723.0

    # Numeric duration
    def test_80_seconds(self):
        result = parse_time_expression("80 seconds")
        assert isinstance(result, TimePoint)
        assert result.seconds == 80.0

    def test_two_minutes(self):
        result = parse_time_expression("two minutes")
        assert isinstance(result, TimePoint)
        assert result.seconds == 120.0

    # Compound written numbers
    def test_twenty_five_seconds(self):
        result = parse_time_expression("twenty five seconds")
        assert isinstance(result, TimePoint)
        assert result.seconds == 25.0

    def test_a_minute_twenty(self):
        result = parse_time_expression("a minute twenty")
        assert isinstance(result, TimePoint)
        assert result.seconds == 80.0

    def test_a_minute_and_twenty_seconds(self):
        result = parse_time_expression("a minute and twenty seconds")
        assert isinstance(result, TimePoint)
        assert result.seconds == 80.0

    # Relative expressions
    def test_the_first_30_seconds(self):
        result = parse_time_expression("the first 30 seconds")
        assert isinstance(result, TimeRange)
        assert result.start == 0.0
        assert result.end == 30.0
        assert result.from_start == 30.0

    def test_the_last_10_seconds(self):
        result = parse_time_expression("the last 10 seconds")
        assert isinstance(result, TimeRange)
        assert result.from_end == 10.0

    # Point reference
    def test_at_5_00(self):
        result = parse_time_expression("at 5:00")
        assert isinstance(result, TimePoint)
        assert result.seconds == 300.0

    # Explicit ranges
    def test_from_1_00_to_1_30(self):
        result = parse_time_expression("from 1:00 to 1:30")
        assert isinstance(result, TimeRange)
        assert result.start == 60.0
        assert result.end == 90.0

    def test_between_0_30_and_0_40(self):
        result = parse_time_expression("between 0:30 and 0:40")
        assert isinstance(result, TimeRange)
        assert result.start == 30.0
        assert result.end == 40.0

    # Invalid
    def test_invalid_returns_none(self):
        assert parse_time_expression("blah blah blah") is None


class TestResolveTimeRange:
    def test_from_end(self):
        tr = TimeRange(from_end=10.0)
        start, end = resolve_time_range(tr, 300.0)
        assert start == 290.0
        assert end == 300.0

    def test_from_end_clamps_to_zero(self):
        tr = TimeRange(from_end=400.0)
        start, end = resolve_time_range(tr, 300.0)
        assert start == 0.0
        assert end == 300.0

    def test_from_start(self):
        tr = TimeRange(from_start=30.0)
        start, end = resolve_time_range(tr, 300.0)
        assert start == 0.0
        assert end == 30.0

    def test_explicit_range(self):
        tr = TimeRange(start=60.0, end=90.0)
        result = resolve_time_range(tr, 300.0)
        assert result == (60.0, 90.0)

    def test_underspecified_returns_none(self):
        tr = TimeRange()
        assert resolve_time_range(tr, 300.0) is None
