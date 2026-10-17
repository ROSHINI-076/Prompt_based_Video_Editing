"""Unit tests for engine/ops.py."""

from __future__ import annotations

from incubrix.engine.models import (
    Anchor,
    Effect,
    EffectType,
    Overlay,
    OverlayStatus,
    Segment,
    Transcript,
    Word,
    make_word_id,
)
from incubrix.engine.ops import (
    apply_add_caption,
    apply_bleep,
    apply_cut,
    apply_keep_only,
    apply_mute,
    apply_reorder,
    apply_speed,
    apply_style,
    snap_to_silence,
)
from incubrix.engine.timeline import total_duration_frames

FPS = 30.0


def _make_simple_transcript(n_words: int = 10, gap_s: float = 0.1) -> Transcript:
    """Tiny transcript: n words, each 0.4 s long with gap_s silence between them."""
    words = []
    t = 0.0
    for i in range(n_words):
        words.append(
            Word(
                id=make_word_id(i),
                text=f"word{i}",
                start_s=t,
                end_s=t + 0.4,
                speaker="Alice",
            )
        )
        t += 0.4 + gap_s
    return Transcript(words=words)


def _overlay(ov_id: str, start_idx: int, end_idx: int, n_words: int) -> Overlay:
    return Overlay(
        id=ov_id,
        text="test caption",
        anchor=Anchor(
            start_word_id=make_word_id(start_idx),
            end_word_id=make_word_id(end_idx),
        ),
        status=OverlayStatus.ACTIVE,
        original_word_count=n_words,
    )


# ── apply_cut ─────────────────────────────────────────────────────


class TestApplyCut:
    def test_cut_removes_range(self):
        segs = [Segment(src_in_f=0, src_out_f=900)]
        new_segs, _, _, _ = apply_cut(segs, [], [], [(0, 300)], Transcript(words=[]), FPS)
        assert len(new_segs) == 1
        assert new_segs[0].src_in_f == 300
        assert new_segs[0].src_out_f == 900

    def test_cut_from_end(self):
        segs = [Segment(src_in_f=0, src_out_f=900)]
        new_segs, _, _, _ = apply_cut(segs, [], [], [(600, 900)], Transcript(words=[]), FPS)
        assert len(new_segs) == 1
        assert new_segs[0].src_out_f == 600

    def test_cut_middle_splits(self):
        segs = [Segment(src_in_f=0, src_out_f=900)]
        new_segs, _, _, _ = apply_cut(segs, [], [], [(300, 600)], Transcript(words=[]), FPS)
        assert len(new_segs) == 2
        assert new_segs[0] == Segment(src_in_f=0, src_out_f=300)
        assert new_segs[1] == Segment(src_in_f=600, src_out_f=900)

    def test_cut_total_duration_reduced(self):
        segs = [Segment(src_in_f=0, src_out_f=900)]
        new_segs, _, _, _ = apply_cut(segs, [], [], [(0, 300)], Transcript(words=[]), FPS)
        assert total_duration_frames(new_segs) == 600

    def test_cut_multiple_ranges(self):
        segs = [Segment(src_in_f=0, src_out_f=900)]
        new_segs, _, _, _ = apply_cut(
            segs, [], [], [(0, 100), (200, 300)], Transcript(words=[]), FPS
        )
        assert total_duration_frames(new_segs) == 700

    def test_caption_dormant_when_all_words_cut(self):
        transcript = _make_simple_transcript(10)
        # Caption on words 2-4 (indices 2,3,4)
        ov = _overlay("ov1", 2, 4, 3)
        segs = [Segment(src_in_f=0, src_out_f=900)]
        # Word 2 starts at ~1.0 s = frame 30; word 4 ends at ~2.2 s = frame 66
        # Cut all of frames 0-900 would kill them — let's cut 0-900
        _, overlays, _, warnings = apply_cut(segs, [ov], [], [(0, 900)], transcript, FPS)
        assert overlays[0].status == OverlayStatus.DORMANT
        assert any("dormant" in w.lower() for w in warnings)

    def test_caption_partial_when_some_words_cut(self):
        transcript = _make_simple_transcript(10)
        # Caption on words 0-4 (5 words)
        ov = _overlay("ov1", 0, 4, 5)
        segs = [Segment(src_in_f=0, src_out_f=900)]
        # Word 0: start=0.0, end=0.4 → midpoint frame 6
        # Word 1: start=0.5, end=0.9 → midpoint frame 21
        # Cut frames 0-15 will kill word 0 but leave words 1-4
        _, overlays, _, warnings = apply_cut(segs, [ov], [], [(0, 15)], transcript, FPS)
        assert overlays[0].status == OverlayStatus.PARTIAL
        assert any("partial" in w.lower() for w in warnings)

    def test_caption_stays_active_when_no_words_cut(self):
        transcript = _make_simple_transcript(10)
        # Caption on words 5-9
        ov = _overlay("ov1", 5, 9, 5)
        segs = [Segment(src_in_f=0, src_out_f=900)]
        # Cut only the first few frames (words 0-2)
        _, overlays, _, warnings = apply_cut(segs, [ov], [], [(0, 20)], transcript, FPS)
        assert overlays[0].status == OverlayStatus.ACTIVE

    def test_effects_unchanged_by_cut(self):
        segs = [Segment(src_in_f=0, src_out_f=900)]
        fx = Effect(
            id="fx1",
            type=EffectType.MUTE,
            anchor=Anchor(start_word_id="w0001", end_word_id="w0002"),
        )
        _, _, effects, _ = apply_cut(segs, [], [fx], [(0, 100)], Transcript(words=[]), FPS)
        assert len(effects) == 1
        assert effects[0].id == "fx1"


# ── apply_keep_only ───────────────────────────────────────────────


class TestApplyKeepOnly:
    def test_keeps_middle(self):
        segs = [Segment(src_in_f=0, src_out_f=900)]
        new_segs, _, _, _ = apply_keep_only(segs, [], [], (300, 600), Transcript(words=[]), FPS)
        assert len(new_segs) == 1
        assert new_segs[0].src_in_f == 300
        assert new_segs[0].src_out_f == 600

    def test_duration_correct(self):
        segs = [Segment(src_in_f=0, src_out_f=900)]
        new_segs, _, _, _ = apply_keep_only(segs, [], [], (100, 700), Transcript(words=[]), FPS)
        assert total_duration_frames(new_segs) == 600


# ── apply_speed ───────────────────────────────────────────────────


class TestApplySpeed:
    def test_sets_speed(self):
        segs = [Segment(src_in_f=0, src_out_f=900)]
        new_segs, _ = apply_speed(segs, (0, 900), 2.0)
        assert len(new_segs) == 1
        assert new_segs[0].speed == 2.0

    def test_duration_halved_at_2x(self):
        segs = [Segment(src_in_f=0, src_out_f=900)]
        new_segs, _ = apply_speed(segs, (0, 900), 2.0)
        assert total_duration_frames(new_segs) == 450

    def test_speed_partial_range(self):
        """Speed change on part of a segment should split it."""
        segs = [Segment(src_in_f=0, src_out_f=900)]
        new_segs, _ = apply_speed(segs, (300, 600), 2.0)
        assert len(new_segs) == 3
        assert new_segs[0].speed == 1.0
        assert new_segs[1].speed == 2.0
        assert new_segs[2].speed == 1.0

    def test_speed_overlap_last_wins(self):
        """Second speed instruction replaces first in the overlapping region."""
        segs = [Segment(src_in_f=0, src_out_f=900)]
        segs, _ = apply_speed(segs, (0, 900), 2.0)
        segs, warnings = apply_speed(segs, (0, 900), 3.0)
        assert segs[0].speed == 3.0
        # Overlap warning should be emitted
        assert any("overlap" in w.lower() or "replaced" in w.lower() for w in warnings)

    def test_speed_outside_partial(self):
        """Speed for part of a multi-segment timeline."""
        segs = [
            Segment(src_in_f=0, src_out_f=300),
            Segment(src_in_f=300, src_out_f=600),
            Segment(src_in_f=600, src_out_f=900),
        ]
        new_segs, _ = apply_speed(segs, (300, 600), 0.5)
        assert new_segs[1].speed == 0.5
        assert new_segs[0].speed == 1.0
        assert new_segs[2].speed == 1.0


# ── apply_reorder ─────────────────────────────────────────────────


class TestApplyReorder:
    def test_reorder_basic(self):
        """Move frames 300-600 to before frame 0."""
        segs = [Segment(src_in_f=0, src_out_f=900)]
        new_segs, warnings = apply_reorder(segs, (300, 600), before=0)
        assert len(warnings) == 0
        # The moved section should appear first
        assert any(s.src_in_f == 300 and s.src_out_f == 600 for s in new_segs[:2])

    def test_reorder_rejects_destination_inside_range(self):
        segs = [Segment(src_in_f=0, src_out_f=900)]
        new_segs, warnings = apply_reorder(segs, (300, 600), before=450)
        assert any("rejected" in w.lower() or "inside" in w.lower() for w in warnings)
        # Segments unchanged
        assert new_segs == segs

    def test_reorder_preserves_total_duration(self):
        segs = [Segment(src_in_f=0, src_out_f=900)]
        new_segs, _ = apply_reorder(segs, (300, 600), before=0)
        assert total_duration_frames(new_segs) == total_duration_frames(segs)


# ── apply_add_caption / apply_style ──────────────────────────────


class TestCaptionStyle:
    def test_add_caption(self):
        transcript = _make_simple_transcript(5)
        overlays, warnings = apply_add_caption(
            [], "ov1", "Hello!", make_word_id(0), make_word_id(2), {}, transcript
        )
        assert len(overlays) == 1
        assert overlays[0].id == "ov1"
        assert overlays[0].text == "Hello!"
        assert overlays[0].status == OverlayStatus.ACTIVE
        assert overlays[0].original_word_count == 3

    def test_style_merges(self):
        transcript = _make_simple_transcript(5)
        overlays, _ = apply_add_caption(
            [], "ov1", "Hello!", make_word_id(0), make_word_id(2), {"color": "white"}, transcript
        )
        overlays, _ = apply_style(overlays, "ov1", {"size": 24})
        assert overlays[0].style == {"color": "white", "size": 24}

    def test_style_overrides(self):
        transcript = _make_simple_transcript(5)
        overlays, _ = apply_add_caption(
            [], "ov1", "Hello!", make_word_id(0), make_word_id(2), {"color": "white"}, transcript
        )
        overlays, _ = apply_style(overlays, "ov1", {"color": "red"})
        assert overlays[0].style["color"] == "red"

    def test_style_missing_id(self):
        _, warnings = apply_style([], "nonexistent", {"color": "red"})
        assert any("not found" in w.lower() for w in warnings)


# ── apply_mute / apply_bleep ──────────────────────────────────────


class TestMuteBleep:
    def test_apply_mute(self):
        effects, warnings = apply_mute([], "fx1", "w0001", "w0003")
        assert len(effects) == 1
        assert effects[0].type == EffectType.MUTE
        assert effects[0].anchor.start_word_id == "w0001"

    def test_apply_bleep(self):
        effects, warnings = apply_bleep([], "fx1", "w0001", "w0003")
        assert len(effects) == 1
        assert effects[0].type == EffectType.BLEEP


# ── snap_to_silence ───────────────────────────────────────────────


class TestSnapToSilence:
    def test_snap_uses_midpoint(self):
        """
        Words:  [0.0-0.4]  [0.5-0.9]  [1.0-1.4]
        Gap before word1: 0.4 to 0.5 → midpoint 0.45
        Gap after word1:  0.9 to 1.0 → midpoint 0.95
        """
        transcript = _make_simple_transcript(3, gap_s=0.1)
        snap_in, snap_out = snap_to_silence(1, 1, transcript, FPS)
        expected_in = round(0.45 * FPS)
        expected_out = round(0.95 * FPS)
        assert snap_in == expected_in
        assert snap_out == expected_out

    def test_snap_first_word_uses_word_start(self):
        transcript = _make_simple_transcript(3)
        snap_in, _ = snap_to_silence(0, 0, transcript, FPS)
        # No previous word, so snap_in = word 0 start frame
        assert snap_in == round(transcript.words[0].start_s * FPS)

    def test_snap_last_word_uses_word_end(self):
        transcript = _make_simple_transcript(3)
        n = len(transcript.words) - 1
        _, snap_out = snap_to_silence(n, n, transcript, FPS)
        assert snap_out == round(transcript.words[n].end_s * FPS)
