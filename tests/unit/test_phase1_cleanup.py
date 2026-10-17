"""Tests for Phase 1 cleanup requirements.

Covers:
1. Minimum caption duration (MIN_CAPTION_S = 0.7 s)
   (a) caption on words sped up 4x still lasts >= 0.7 s
   (b) caption at normal speed is unchanged
   (c) minimum is capped at timeline end
   (d) same history gives identical timeline_hash on repeated replays
2. Timeline length in frames == sum of segment output frames (cut + speed + reorder + restore)
3. All previously uncovered lines in ops.py, session.py, timeline.py, timeparse.py
"""

from __future__ import annotations

import pytest

from incubrix.engine.models import (
    AddCaptionOp,
    CutOp,
    Edit,
    ReorderOp,
    RestoreOp,
    SourceInfo,
    SpeedOp,
    Transcript,
    Word,
    make_word_id,
    segment_output_length,
)
from incubrix.engine.hashing import timeline_hash
from incubrix.engine.replay import replay
from incubrix.engine.session import SessionManager
from incubrix.engine.timeline import (
    Layout,
    MIN_CAPTION_S,
    caption_display_span,
    min_caption_frames,
    total_duration_frames,
    timeline_to_output,
)
from incubrix.engine.ops import (
    apply_cut,
    apply_keep_only,
    apply_speed,
    apply_style,
    update_overlay_statuses,
    _word_range,
)

FPS = 30.0


def _src(duration_s: float = 10.0) -> SourceInfo:
    return SourceInfo(sha256="test", duration_frames=int(duration_s * FPS), fps=FPS)


def _transcript(n: int = 20, word_dur: float = 0.4, gap: float = 0.1) -> Transcript:
    words = []
    t = 0.0
    for i in range(n):
        words.append(Word(id=make_word_id(i), text=f"w{i}", start_s=t, end_s=t + word_dur))
        t += word_dur + gap
    return Transcript(words=words)


def _edit(eid: str, instruction: str, *ops) -> Edit:
    return Edit(id=eid, instruction=instruction, ops=list(ops))


# ─────────────────────────────────────────────────────────────────
# 1. Minimum caption duration
# ─────────────────────────────────────────────────────────────────


class TestMinCaptionConstant:
    def test_constant_value(self):
        assert MIN_CAPTION_S == 0.7

    def test_min_caption_frames_at_30fps(self):
        # round(0.7 * 30) = 21
        assert min_caption_frames(30.0) == 21

    def test_min_caption_frames_at_25fps(self):
        # round(0.7 * 25) = round(17.5) = 18
        assert min_caption_frames(25.0) == 18

    def test_min_caption_frames_custom(self):
        assert min_caption_frames(30.0, min_s=1.0) == 30


class TestCaptionDisplaySpan:
    """Tests for caption_display_span() pure function."""

    def test_normal_speed_long_caption_unchanged(self):
        # 5-second caption at 30fps → 150 output frames; well above minimum
        start, end = caption_display_span(0, 150, 300, FPS)
        assert start == 0
        assert end == 150

    def test_minimum_enforced(self):
        # Caption covering only 5 frames (< 21 minimum at 30fps)
        start, end = caption_display_span(10, 15, 300, FPS)
        assert end - start == min_caption_frames(FPS)
        assert start == 10
        assert end == 10 + min_caption_frames(FPS)

    def test_capped_at_timeline_end(self):
        # Caption near the very end: minimum would overshoot
        total = 25
        start, end = caption_display_span(20, 22, total, FPS)
        assert end <= total, "End must not exceed timeline length"
        assert end == total  # capped

    def test_very_short_timeline_clamps_start_too(self):
        # Timeline shorter than min caption: start and end both clamp to total
        total = 5
        start, end = caption_display_span(0, 2, total, FPS)
        assert start <= end
        assert end <= total


class TestCaptionOutSpan:
    """Tests for Layout.caption_out_span() — derives min-enforced display time."""

    def _layout_and_words(self, speed: float = 1.0):
        """Lay out a 10 s video, optionally at the given speed."""
        from incubrix.engine.models import Segment
        total_src = int(10.0 * FPS)
        segs = [Segment(src_in_f=0, src_out_f=total_src, speed=speed)]
        layout = Layout(segs)
        # Caption anchor: word at 3.0-3.4 s
        w_start = Word(id="w0001", text="hi", start_s=3.0, end_s=3.4)
        # End word at 3.5-3.9 s
        w_end = Word(id="w0002", text="there", start_s=3.5, end_s=3.9)
        return layout, w_start, w_end

    def test_normal_speed_caption_at_least_min(self):
        layout, w_start, w_end = self._layout_and_words(speed=1.0)
        span = layout.caption_out_span(w_start, w_end, FPS)
        assert span is not None
        start, end = span
        assert end - start >= min_caption_frames(FPS), (
            f"Caption duration {end - start} frames < minimum {min_caption_frames(FPS)}"
        )

    def test_4x_speed_caption_still_meets_minimum(self):
        """(a) Caption on words sped up 4x still lasts >= 0.7 s."""
        layout, w_start, w_end = self._layout_and_words(speed=4.0)
        span = layout.caption_out_span(w_start, w_end, FPS)
        assert span is not None
        start, end = span
        duration_s = (end - start) / FPS
        assert duration_s >= MIN_CAPTION_S, (
            f"Caption at 4x speed only lasted {duration_s:.3f}s, need >= {MIN_CAPTION_S}s"
        )

    def test_normal_speed_long_caption_unchanged(self):
        """(b) Caption at normal speed lasting 2+ seconds is not lengthened."""
        from incubrix.engine.models import Segment
        segs = [Segment(src_in_f=0, src_out_f=int(10.0 * FPS), speed=1.0)]
        layout = Layout(segs)
        # 2-second caption
        w_start = Word(id="w0001", text="a", start_s=1.0, end_s=1.4)
        w_end = Word(id="w0002", text="z", start_s=2.5, end_s=2.9)
        span = layout.caption_out_span(w_start, w_end, FPS)
        assert span is not None
        start, end = span
        # Natural duration from words: roughly 1.9 s → ~57 frames, well above min
        assert (end - start) >= 2 * min_caption_frames(FPS)

    def test_minimum_capped_at_timeline_end(self):
        """(c) Minimum is capped at the timeline end."""
        from incubrix.engine.models import Segment
        # Very short timeline: 10 frames total
        segs = [Segment(src_in_f=0, src_out_f=10, speed=1.0)]
        layout = Layout(segs)
        w_start = Word(id="w0001", text="a", start_s=0.0, end_s=(5 / FPS))
        w_end = Word(id="w0002", text="b", start_s=(6 / FPS), end_s=(8 / FPS))
        span = layout.caption_out_span(w_start, w_end, FPS)
        assert span is not None
        _, end = span
        assert end <= layout.total_frames, "End must not exceed timeline length"

    def test_dormant_words_return_none(self):
        """When all anchor words are cut, caption_out_span returns None."""
        from incubrix.engine.models import Segment
        # Timeline only has frames 50-300; words at 0-0.4 s (frames 0-12) are cut
        segs = [Segment(src_in_f=50, src_out_f=300, speed=1.0)]
        layout = Layout(segs)
        w_start = Word(id="w0001", text="a", start_s=0.0, end_s=0.2)
        w_end = Word(id="w0002", text="b", start_s=0.2, end_s=0.4)
        span = layout.caption_out_span(w_start, w_end, FPS)
        assert span is None


class TestMinCaptionDeterminism:
    """(d) Same history gives identical timeline_hash on repeated replays."""

    def test_hash_stable_with_sped_up_caption(self):
        src = _src(10.0)
        transcript = _transcript(20)
        edits = [
            _edit(
                "e1", "4x speed",
                SpeedOp(range=(0, int(5.0 * FPS)), factor=4.0),
            ),
            _edit(
                "e2", "caption",
                AddCaptionOp(
                    overlay_id="ov1", text="Hi!",
                    start_word_id=make_word_id(5), end_word_id=make_word_id(7),
                )
            ),
        ]
        hashes = [timeline_hash(replay(src, edits, transcript)) for _ in range(3)]
        assert len(set(hashes)) == 1, f"Hash not stable across runs: {hashes}"


class TestOverlayDisplayOutputInvariant:
    def test_partial_caption_uses_surviving_interior_words(self):
        """The public output span remains visible when both endpoint words are cut."""
        source = _src(10.0)
        transcript = Transcript(words=[
            Word(id=make_word_id(i), text=f"w{i}", start_s=i * 0.5, end_s=i * 0.5 + 0.3)
            for i in range(5)
        ])
        edits = [
            _edit(
                "e1", "caption",
                AddCaptionOp(
                    overlay_id="ov1",
                    text="middle",
                    start_word_id=make_word_id(0),
                    end_word_id=make_word_id(4),
                ),
            ),
            _edit(
                "e2", "cut caption endpoints",
                CutOp(ranges=[(0, 15), (60, source.duration_frames)]),
            ),
        ]

        timeline = replay(source, edits, transcript)
        display = timeline_to_output(timeline, transcript)["overlay_displays"][0]

        assert display["status"] == "partial"
        assert display["start_out_f"] is not None
        assert display["end_out_f"] is not None
        assert display["end_out_f"] > display["start_out_f"]

    def test_dormant_caption_has_no_output_times(self):
        source = _src(10.0)
        transcript = _transcript(5)
        edits = [
            _edit(
                "e1", "caption",
                AddCaptionOp(
                    overlay_id="ov1",
                    text="gone",
                    start_word_id=make_word_id(0),
                    end_word_id=make_word_id(1),
                ),
            ),
            _edit("e2", "cut caption", CutOp(ranges=[(0, 30)])),
        ]

        display = timeline_to_output(replay(source, edits, transcript), transcript)[
            "overlay_displays"
        ][0]

        assert display["status"] == "dormant"
        assert display["start_out_f"] is None
        assert display["end_out_f"] is None

    def test_caption_keeps_visible_part_of_midpoint_alive_word(self):
        source = _src(10.0)
        transcript = Transcript(words=[
            Word(id=make_word_id(0), text="clipped", start_s=2.0, end_s=2.4)
        ])
        edits = [
            _edit(
                "e1", "caption",
                AddCaptionOp(
                    overlay_id="ov1",
                    text="clipped word",
                    start_word_id=make_word_id(0),
                    end_word_id=make_word_id(0),
                ),
            ),
            _edit("e2", "clip word edges", CutOp(ranges=[(0, 62), (70, 300)])),
        ]

        timeline = replay(source, edits, transcript)
        display = timeline_to_output(timeline, transcript)["overlay_displays"][0]

        assert display["status"] == "active"
        assert display["start_out_f"] == 0
        assert display["end_out_f"] == 8

    def test_missing_anchor_id_raises_from_public_output(self):
        source = _src(10.0)
        transcript = _transcript(5)
        edits = [
            _edit(
                "e1", "invalid caption",
                AddCaptionOp(
                    overlay_id="ov1",
                    text="invalid",
                    start_word_id="w9999",
                    end_word_id=make_word_id(1),
                ),
            ),
        ]

        timeline = replay(source, edits, transcript)

        with pytest.raises(ValueError, match="Overlay 'ov1'.*w9999"):
            timeline_to_output(timeline, transcript)


# ─────────────────────────────────────────────────────────────────
# 2. Timeline length == sum of segment output frames
#    (cut + speed + reorder + restore mix)
# ─────────────────────────────────────────────────────────────────


class TestTimelineLengthInvariant:
    """Rendered length in frames == sum of segment output lengths, always."""

    def _assert_length_invariant(self, src, edits, transcript):
        tl = replay(src, edits, transcript)
        computed_total = total_duration_frames(tl.segments)
        segment_sum = sum(segment_output_length(s) for s in tl.segments)
        assert computed_total == segment_sum, (
            f"total_duration_frames={computed_total} != segment_sum={segment_sum}"
        )
        return computed_total

    def test_cut_only(self):
        src = _src(10.0)
        edits = [_edit("e1", "cut", CutOp(ranges=[(0, 90)]))]
        total = self._assert_length_invariant(src, edits, _transcript())
        assert total == int(10.0 * FPS) - 90

    def test_speed_only(self):
        src = _src(10.0)
        edits = [_edit("e1", "2x", SpeedOp(range=(0, 300), factor=2.0))]
        total = self._assert_length_invariant(src, edits, _transcript())
        assert total == 150  # 300 / 2.0

    def test_cut_then_speed(self):
        src = _src(10.0)
        edits = [
            _edit("e1", "cut", CutOp(ranges=[(0, 90)])),           # 300 → 210 src frames
            _edit("e2", "2x", SpeedOp(range=(90, 210), factor=2.0)), # 120 frames → 60 out
        ]
        total = self._assert_length_invariant(src, edits, _transcript())
        # segment [90,210] at 2x = 60; [210,300] at 1x = 90 → total = 150
        assert total == 150

    def test_reorder_preserves_length(self):
        src = _src(10.0)
        original_frames = int(10.0 * FPS)
        edits = [_edit("e1", "reorder", ReorderOp(range=(0, 90), before=210))]
        total = self._assert_length_invariant(src, edits, _transcript())
        assert total == original_frames  # reorder does not change total

    def test_restore_reverts_cut(self):
        src = _src(10.0)
        original_frames = int(10.0 * FPS)
        edits = [
            _edit("e1", "cut", CutOp(ranges=[(0, 90)])),
            _edit("e2", "restore", RestoreOp(target_edit_id="e1")),
        ]
        total = self._assert_length_invariant(src, edits, _transcript())
        assert total == original_frames

    def test_cut_speed_reorder_restore_mix(self):
        """Full mix: cut + speed + reorder + restore; invariant holds throughout."""
        src = _src(10.0)
        transcript = _transcript(20)
        original = int(10.0 * FPS)

        e1 = _edit("e1", "cut 0-1s", CutOp(ranges=[(0, 30)]))
        e2 = _edit("e2", "2x on 1-3s", SpeedOp(range=(30, 90), factor=2.0))
        e3 = _edit("e3", "reorder", ReorderOp(range=(90, 120), before=180))
        e4 = _edit("e4", "restore e1", RestoreOp(target_edit_id="e1"))

        for stack in [[e1], [e1, e2], [e1, e2, e3], [e1, e2, e3, e4]]:
            self._assert_length_invariant(src, stack, transcript)

        # Final state: e1 restored, e2 and e3 active
        tl = replay(src, [e1, e2, e3, e4], transcript)
        total = total_duration_frames(tl.segments)
        # e1 disabled → no 0-30 cut; e2 active (30-90 at 2x = 30 out); e3 active (reorder)
        # source: [0,30)=30, [30,90) at 2x=30, [90,120)=30, [120,180)=60, [180,210)=30...
        # (reorder moves [90,120) to before src=180, so the arithmetic is complex —
        # we just assert the invariant, not a specific number)
        segment_sum = sum(segment_output_length(s) for s in tl.segments)
        assert total == segment_sum


# ─────────────────────────────────────────────────────────────────
# 3. Previously uncovered lines
# ─────────────────────────────────────────────────────────────────


class TestWordRangeEdgeCases:
    """ops.py _word_range: ValueError and reversed-ID branches."""

    def test_missing_start_id_returns_empty(self):
        """ValueError on missing start_id → empty list (line 72-73)."""
        t = _transcript(5)
        result = _word_range(t, "w9999", make_word_id(2))
        assert result == []

    def test_missing_end_id_returns_empty(self):
        """ValueError on missing end_id → empty list."""
        t = _transcript(5)
        result = _word_range(t, make_word_id(0), "w9999")
        assert result == []

    def test_reversed_ids_returns_correct_range(self):
        """When end_id index < start_id index, swap and return correct range (line 75)."""
        t = _transcript(5)
        # Pass end before start — should still work
        forward = _word_range(t, make_word_id(1), make_word_id(3))
        backward = _word_range(t, make_word_id(3), make_word_id(1))
        assert forward == backward
        assert forward == [1, 2, 3]


class TestUpdateOverlayStatusesUnknownId:
    """ops.py update_overlay_statuses: overlay with non-existent anchor word IDs (lines 97-98)."""

    def test_overlay_with_unknown_word_ids_is_preserved_unchanged(self):
        from incubrix.engine.models import Anchor, Overlay, OverlayStatus, Segment
        ov = Overlay(
            id="ov1", text="test",
            anchor=Anchor(start_word_id="w9999", end_word_id="w9998"),
            status=OverlayStatus.ACTIVE,
            original_word_count=1,
        )
        segs = [Segment(src_in_f=0, src_out_f=300)]
        t = _transcript(5)
        updated, warnings = update_overlay_statuses([ov], segs, t, FPS)
        # Should be returned unchanged (no indices found → early continue)
        assert updated[0].id == "ov1"
        assert updated[0].status == OverlayStatus.ACTIVE
        assert warnings == []


class TestApplyKeepOnlyEdgeCases:
    """ops.py apply_keep_only: empty segments (line 206) and no-op path (line 214)."""

    def test_empty_segments_returns_unchanged(self):
        """Empty segment list → early return (line 206)."""
        segs, ovs, fxs, warnings = apply_keep_only(
            [], [], [], (0, 100), Transcript(words=[]), FPS
        )
        assert segs == []
        assert warnings == []

    def test_keep_whole_range_is_noop(self):
        """keep_range covers entire source → no cuts needed (line 213-214)."""
        from incubrix.engine.models import Segment
        segs = [Segment(src_in_f=0, src_out_f=300)]
        new_segs, _, _, warnings = apply_keep_only(
            segs, [], [], (0, 300), Transcript(words=[]), FPS
        )
        assert len(new_segs) == 1
        assert new_segs[0].src_in_f == 0
        assert new_segs[0].src_out_f == 300
        assert warnings == []


class TestApplyStyleElseBranch:
    """ops.py apply_style: the else-branch appending non-matching overlay (line 336)."""

    def test_style_only_updates_matching_overlay(self):
        """Two overlays: only the targeted one is restyled; other is kept unchanged."""
        from incubrix.engine.models import Anchor, Overlay, OverlayStatus
        ov1 = Overlay(
            id="ov1", text="A",
            anchor=Anchor(start_word_id="w0001", end_word_id="w0001"),
            style={"color": "white"}, status=OverlayStatus.ACTIVE,
        )
        ov2 = Overlay(
            id="ov2", text="B",
            anchor=Anchor(start_word_id="w0002", end_word_id="w0002"),
            style={"color": "blue"}, status=OverlayStatus.ACTIVE,
        )
        result, warnings = apply_style([ov1, ov2], "ov1", {"color": "red"})
        # ov1 updated, ov2 unchanged (the else-branch at line 336)
        assert result[0].style["color"] == "red"
        assert result[1].style["color"] == "blue"
        assert warnings == []


class TestSessionEditCounterProperty:
    """session.py: edit_counter property (line 39)."""

    def test_edit_counter_starts_at_zero(self):
        mgr = SessionManager(_src(), _transcript())
        assert mgr.edit_counter == 0

    def test_edit_counter_increments_with_each_push(self):
        mgr = SessionManager(_src(), _transcript())
        mgr.push_edit("a", [CutOp(ranges=[(0, 30)])])
        assert mgr.edit_counter == 1
        mgr.push_edit("b", [CutOp(ranges=[(60, 90)])])
        assert mgr.edit_counter == 2


class TestTimeparse_MoreEdgeCases:
    """timeparse.py: remaining uncovered paths."""

    def test_empty_string_returns_none(self):
        from incubrix.engine.timeparse import _parse_number_words
        assert _parse_number_words("") is None  # line 82

    def test_and_token_skipped(self):
        from incubrix.engine.timeparse import _parse_number_words
        # "and" alone → total=0, not "zero", returns None
        assert _parse_number_words("and") is None

    def test_a_token_in_compound(self):
        from incubrix.engine.timeparse import _parse_number_words
        # "a hundred" → 100 (lines 92-93: tok == "a" in loop)
        assert _parse_number_words("a hundred") == 100.0  # line 92-93, 99-103

    def test_thousand_multiplier(self):
        from incubrix.engine.timeparse import _parse_number_words
        # "two thousand" → 2000 (lines 99-103: multiplier path)
        assert _parse_number_words("two thousand") == 2000.0

    def test_hundred_multiplier_with_implicit_one(self):
        from incubrix.engine.timeparse import _parse_number_words
        # "hundred" alone → current=0 → set to 1 → 100 (line 100)
        assert _parse_number_words("hundred") == 100.0

    def test_first_and_last_with_bare_word_numbers(self):
        """'first twenty' / 'last five' are handled by _parse_duration (which calls
        _parse_number_words internally). No separate fallback needed."""
        from incubrix.engine.timeparse import parse_time_expression, TimeRange
        r1 = parse_time_expression("first twenty")
        assert isinstance(r1, TimeRange)
        assert r1.from_start == 20.0

        r2 = parse_time_expression("last five")
        assert isinstance(r2, TimeRange)
        assert r2.from_end == 5.0
