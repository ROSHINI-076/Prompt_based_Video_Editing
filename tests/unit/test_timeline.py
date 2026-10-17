"""Unit tests for engine/timeline.py (Layout, helpers)."""

from __future__ import annotations

import pytest

from incubrix.engine.models import Segment, SourceInfo, Word, make_word_id, segment_output_length
from incubrix.engine.timeline import (
    Layout,
    frames_to_seconds,
    initial_timeline,
    is_word_alive,
    seconds_to_frames,
    total_duration_frames,
    word_midpoint_frame,
)


FPS = 30.0


# ── Helpers ───────────────────────────────────────────────────────


def _word(idx: int, start_s: float, end_s: float) -> Word:
    return Word(id=make_word_id(idx), text="x", start_s=start_s, end_s=end_s)


class TestHelpers:
    def test_frames_to_seconds(self):
        assert frames_to_seconds(30, 30.0) == 1.0
        assert frames_to_seconds(45, 30.0) == 1.5
        assert frames_to_seconds(1, 30.0) == round(1 / 30.0, 3)

    def test_seconds_to_frames(self):
        assert seconds_to_frames(1.0, 30.0) == 30
        assert seconds_to_frames(1.5, 30.0) == 45
        assert seconds_to_frames(0.0, 30.0) == 0

    def test_total_duration_frames_single(self):
        segs = [Segment(src_in_f=0, src_out_f=300, speed=1.0)]
        assert total_duration_frames(segs) == 300

    def test_total_duration_frames_multiple(self):
        segs = [
            Segment(src_in_f=0, src_out_f=100),
            Segment(src_in_f=200, src_out_f=300),
        ]
        assert total_duration_frames(segs) == 200

    def test_word_midpoint_frame(self):
        w = _word(0, start_s=1.0, end_s=1.4)
        assert word_midpoint_frame(w, FPS) == round(1.2 * FPS)

    def test_is_word_alive_yes(self):
        w = _word(0, start_s=1.0, end_s=1.4)  # midpoint ~ frame 36
        segs = [Segment(src_in_f=0, src_out_f=300)]
        assert is_word_alive(w, segs, FPS) is True

    def test_is_word_alive_no(self):
        w = _word(0, start_s=1.0, end_s=1.4)  # midpoint ~ frame 36
        segs = [Segment(src_in_f=0, src_out_f=30)]  # only first second
        assert is_word_alive(w, segs, FPS) is False

    def test_is_word_alive_at_boundary(self):
        # Word from 0.0–0.4 s; midpoint = 0.2 s = frame 6
        w = _word(0, start_s=0.0, end_s=0.4)
        segs = [Segment(src_in_f=6, src_out_f=300)]  # starts at midpoint frame
        assert is_word_alive(w, segs, FPS) is True
        segs2 = [Segment(src_in_f=7, src_out_f=300)]  # starts after midpoint
        assert is_word_alive(w, segs2, FPS) is False


class TestInitialTimeline:
    def test_single_segment(self):
        source = SourceInfo(sha256="x", duration_frames=9000, fps=FPS)
        tl = initial_timeline(source)
        assert len(tl.segments) == 1
        assert tl.segments[0].src_in_f == 0
        assert tl.segments[0].src_out_f == 9000
        assert tl.segments[0].speed == 1.0

    def test_total_duration_matches_source(self):
        source = SourceInfo(sha256="x", duration_frames=9000, fps=FPS)
        tl = initial_timeline(source)
        assert total_duration_frames(tl.segments) == 9000


class TestLayout:
    def test_src_to_out_identity(self):
        """Single segment at speed 1.0 — src and out frames are identical."""
        segs = [Segment(src_in_f=0, src_out_f=300)]
        layout = Layout(segs)
        assert layout.src_to_out(0) == 0
        assert layout.src_to_out(150) == 150
        assert layout.src_to_out(299) == 299

    def test_src_to_out_cut_gap(self):
        """After a cut, the first segment maps 0–99 → 0–99, second maps 200–299 → 100–199."""
        segs = [
            Segment(src_in_f=0, src_out_f=100),
            Segment(src_in_f=200, src_out_f=300),
        ]
        layout = Layout(segs)
        assert layout.src_to_out(0) == 0
        assert layout.src_to_out(99) == 99
        # Frame 150 was cut — should be None
        assert layout.src_to_out(150) is None
        # Frame 200 should map to output frame 100
        assert layout.src_to_out(200) == 100

    def test_out_to_src_identity(self):
        segs = [Segment(src_in_f=0, src_out_f=300)]
        layout = Layout(segs)
        assert layout.out_to_src(0) == 0
        assert layout.out_to_src(150) == 150

    def test_out_to_src_after_cut(self):
        segs = [
            Segment(src_in_f=0, src_out_f=100),
            Segment(src_in_f=200, src_out_f=300),
        ]
        layout = Layout(segs)
        assert layout.out_to_src(99) == 99
        assert layout.out_to_src(100) == 200  # second segment starts here
        assert layout.out_to_src(199) == 299

    def test_out_to_src_out_of_range(self):
        segs = [Segment(src_in_f=0, src_out_f=100)]
        layout = Layout(segs)
        assert layout.out_to_src(-1) is None
        assert layout.out_to_src(100) is None

    def test_out_to_src_fallback_when_cached_total_is_inconsistent(self):
        """The defensive fallback is reached only if the layout invariant is broken."""
        layout = Layout([Segment(src_in_f=0, src_out_f=100)])
        layout._total += 1
        assert layout.out_to_src(100) is None

    def test_speed_2x(self):
        """Speed 2x: 300 source frames → 150 output frames."""
        segs = [Segment(src_in_f=0, src_out_f=300, speed=2.0)]
        layout = Layout(segs)
        assert layout.total_frames == 150
        assert layout.src_to_out(0) == 0
        assert layout.src_to_out(100) == 50
        assert layout.src_to_out(200) == 100

    def test_out_range_to_src_ranges_single(self):
        segs = [Segment(src_in_f=0, src_out_f=300)]
        layout = Layout(segs)
        ranges = layout.out_range_to_src_ranges(50, 150)
        assert ranges == [(50, 150)]

    def test_out_range_to_src_ranges_spanning_gap(self):
        """Output range spanning two source segments returns two source ranges."""
        segs = [
            Segment(src_in_f=0, src_out_f=100),
            Segment(src_in_f=200, src_out_f=300),
        ]
        layout = Layout(segs)
        ranges = layout.out_range_to_src_ranges(0, 200)
        assert len(ranges) == 2
        assert ranges[0] == (0, 100)
        assert ranges[1] == (200, 300)

    def test_total_frames(self):
        segs = [
            Segment(src_in_f=0, src_out_f=100),
            Segment(src_in_f=200, src_out_f=350),
        ]
        layout = Layout(segs)
        assert layout.total_frames == 250

    def test_word_out_span_alive(self):
        segs = [Segment(src_in_f=0, src_out_f=300)]
        layout = Layout(segs)
        w = Word(id="w0001", text="hi", start_s=1.0, end_s=1.4)
        span = layout.word_out_span(w, FPS)
        assert span is not None
        start, end = span
        assert start == round(1.0 * FPS)
        assert end == round(1.4 * FPS)

    def test_word_out_span_cut(self):
        """Word in a cut region returns None."""
        segs = [Segment(src_in_f=0, src_out_f=30)]  # only first second
        layout = Layout(segs)
        w = Word(id="w0001", text="hi", start_s=2.0, end_s=2.4)
        assert layout.word_out_span(w, FPS) is None
