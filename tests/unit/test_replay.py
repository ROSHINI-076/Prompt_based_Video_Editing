"""Tests for engine/replay.py — the core determinism + correctness tests.

Required by spec (Phase 1):
- captions follow their words after cut, speed, reorder and restore
- cut of a caption's sentence, then restore, brings the caption back
- partial cut rule
- replaying twice gives the identical hash
- rendered length in frames equals the timeline length
- restoring an early edit leaves later edits intact
"""

from __future__ import annotations

import pytest

from incubrix.engine.models import (
    AddCaptionOp,
    CutOp,
    Edit,
    KeepOnlyOp,
    Overlay,
    OverlayStatus,
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
from incubrix.engine.timeline import (
    Layout,
    initial_timeline,
    is_word_alive,
    total_duration_frames,
)

FPS = 30.0


def _src(duration_s: float = 10.0) -> SourceInfo:
    return SourceInfo(sha256="test", duration_frames=int(duration_s * FPS), fps=FPS)


def _transcript(n: int = 20, word_dur: float = 0.4, gap: float = 0.1) -> Transcript:
    words = []
    t = 0.0
    for i in range(n):
        words.append(Word(
            id=make_word_id(i), text=f"w{i}",
            start_s=t, end_s=t + word_dur,
        ))
        t += word_dur + gap
    return Transcript(words=words)


def _edit(edit_id: str, instruction: str, *ops) -> Edit:
    return Edit(id=edit_id, instruction=instruction, ops=list(ops))


# ── Basic replay ──────────────────────────────────────────────────


class TestReplayBasic:
    def test_empty_stack_returns_initial_timeline(self):
        src = _src(10.0)
        tl = replay(src, [], _transcript())
        assert len(tl.segments) == 1
        assert total_duration_frames(tl.segments) == int(10.0 * FPS)

    def test_cut_reduces_duration(self):
        src = _src(10.0)
        cut_edit = _edit("e1", "cut first 3s", CutOp(ranges=[(0, 90)]))
        tl = replay(src, [cut_edit], _transcript())
        assert total_duration_frames(tl.segments) == int(10.0 * FPS) - 90

    def test_keep_only_reduces_duration(self):
        src = _src(10.0)
        keep_edit = _edit("e1", "keep 1-5s", KeepOnlyOp(range=(30, 150)))
        tl = replay(src, [keep_edit], _transcript())
        assert total_duration_frames(tl.segments) == 120

    def test_speed_changes_duration(self):
        src = _src(10.0)
        speed_edit = _edit("e1", "speed 2x", SpeedOp(range=(0, 300), factor=2.0))
        tl = replay(src, [speed_edit], _transcript())
        assert total_duration_frames(tl.segments) == 150

    def test_frame_length_matches_segment_sum(self):
        """Rendered length in frames == sum of segment output lengths."""
        src = _src(10.0)
        edits = [
            _edit("e1", "cut 0-2s", CutOp(ranges=[(0, 60)])),
            _edit("e2", "speed 2x on 3-5s", SpeedOp(range=(90, 150), factor=2.0)),
        ]
        tl = replay(src, edits, _transcript())
        computed = total_duration_frames(tl.segments)
        segment_sum = sum(segment_output_length(s) for s in tl.segments)
        assert computed == segment_sum


# ── Caption following words ───────────────────────────────────────


class TestCaptionFollowsWords:
    """Captions must stay on their anchor words after cut/speed/reorder/restore."""

    def test_caption_alive_after_earlier_cut(self):
        """Caption anchored at t=3s stays active when only t<2s is cut."""
        src = _src(10.0)
        transcript = _transcript(20)
        # Caption on words 6-8 (roughly 3.0-3.9 s)
        caption_edit = _edit(
            "e1", "add caption",
            AddCaptionOp(
                overlay_id="ov1", text="Caption!",
                start_word_id=make_word_id(6), end_word_id=make_word_id(8),
            )
        )
        cut_edit = _edit("e2", "cut first 2s", CutOp(ranges=[(0, 60)]))
        tl = replay(src, [caption_edit, cut_edit], transcript)
        ov = next(o for o in tl.overlays if o.id == "ov1")
        assert ov.status == OverlayStatus.ACTIVE

    def test_caption_dormant_when_words_cut(self):
        """Caption goes dormant when ALL its anchor words are cut."""
        src = _src(10.0)
        transcript = _transcript(20)
        # Words 2-4: roughly 1.0-2.2 s → frames 30-66
        caption_edit = _edit(
            "e1", "add caption",
            AddCaptionOp(
                overlay_id="ov1", text="Caption!",
                start_word_id=make_word_id(2), end_word_id=make_word_id(4),
            )
        )
        # Cut frames 25-70 — kills midpoints of words 2,3,4
        cut_edit = _edit("e2", "cut 1-2.5s", CutOp(ranges=[(25, 70)]))
        tl = replay(src, [caption_edit, cut_edit], transcript)
        ov = next(o for o in tl.overlays if o.id == "ov1")
        assert ov.status == OverlayStatus.DORMANT

    def test_caption_revives_after_restore(self):
        """Caption that went dormant revives when the cut is restored."""
        src = _src(10.0)
        transcript = _transcript(20)
        caption_edit = _edit(
            "e1", "add caption",
            AddCaptionOp(
                overlay_id="ov1", text="Caption!",
                start_word_id=make_word_id(2), end_word_id=make_word_id(4),
            )
        )
        cut_edit = _edit("e2", "cut words 2-4", CutOp(ranges=[(25, 70)]))
        restore_edit = _edit("e3", "restore e2", RestoreOp(target_edit_id="e2"))

        # After restore, the cut is disabled → caption revives
        tl = replay(src, [caption_edit, cut_edit, restore_edit], transcript)
        ov = next(o for o in tl.overlays if o.id == "ov1")
        assert ov.status == OverlayStatus.ACTIVE

    def test_caption_follows_speed_change(self):
        """Caption anchor words remain alive after speed change (no words cut)."""
        src = _src(10.0)
        transcript = _transcript(20)
        caption_edit = _edit(
            "e1", "add caption",
            AddCaptionOp(
                overlay_id="ov1", text="Caption!",
                start_word_id=make_word_id(6), end_word_id=make_word_id(8),
            )
        )
        speed_edit = _edit("e2", "speed 2x on first 5s", SpeedOp(range=(0, 150), factor=2.0))
        tl = replay(src, [caption_edit, speed_edit], transcript)
        ov = next(o for o in tl.overlays if o.id == "ov1")
        assert ov.status == OverlayStatus.ACTIVE

    def test_caption_partial_cut_rule(self):
        """Partial cut: caption shrinks to surviving words → status PARTIAL."""
        src = _src(10.0)
        transcript = _transcript(20)
        # Words 4-8 span roughly 2.0-4.4 s
        caption_edit = _edit(
            "e1", "add caption",
            AddCaptionOp(
                overlay_id="ov1", text="Caption!",
                start_word_id=make_word_id(4), end_word_id=make_word_id(8),
            )
        )
        # Cut only frames 0-50 → kills words 0-2 but not 4-8
        # BUT cut only the START of the caption range (word 4-5 area)
        # Words 4-5: t ≈ 2.0-2.9 s → midpoints at frames ~63,78
        # Cut frames 60-80 kills midpoints of words 4 and 5, but not 6-8
        cut_edit = _edit("e2", "cut partial", CutOp(ranges=[(60, 80)]))
        tl = replay(src, [caption_edit, cut_edit], transcript)
        ov = next(o for o in tl.overlays if o.id == "ov1")
        assert ov.status == OverlayStatus.PARTIAL

    def test_caption_reorder_preserves_it(self):
        """After reorder, caption is still anchored to its words."""
        src = _src(10.0)
        transcript = _transcript(20)
        # Caption on words 15-17 (late in video)
        caption_edit = _edit(
            "e1", "add caption",
            AddCaptionOp(
                overlay_id="ov1", text="Caption!",
                start_word_id=make_word_id(15), end_word_id=make_word_id(17),
            )
        )
        # Reorder: move frames 0-90 to after frame 180
        reorder_edit = _edit("e2", "reorder", ReorderOp(range=(0, 90), before=180))
        tl = replay(src, [caption_edit, reorder_edit], transcript)
        ov = next(o for o in tl.overlays if o.id == "ov1")
        # Caption words are at the end of video — reorder shouldn't affect them
        assert ov.status == OverlayStatus.ACTIVE


# ── Restore semantics ─────────────────────────────────────────────


class TestRestoreSemantics:
    def test_restore_disables_target_edit(self):
        """Restoring edit e1 should disable it, reverting its effect."""
        src = _src(10.0)
        cut_edit = _edit("e1", "cut first 3s", CutOp(ranges=[(0, 90)]))
        restore_edit = _edit("e2", "restore e1", RestoreOp(target_edit_id="e1"))

        tl_with_cut = replay(src, [cut_edit], _transcript())
        tl_restored = replay(src, [cut_edit, restore_edit], _transcript())

        # Duration should be back to original
        assert total_duration_frames(tl_restored.segments) == int(10.0 * FPS)

    def test_restore_early_edit_leaves_later_edits_intact(self):
        """Restoring an early edit leaves all later edits in place."""
        src = _src(10.0)
        transcript = _transcript(20)
        cut_e1 = _edit("e1", "cut 0-2s", CutOp(ranges=[(0, 60)]))
        caption_e2 = _edit(
            "e2", "add caption",
            AddCaptionOp(
                overlay_id="ov1", text="Caption!",
                start_word_id=make_word_id(10), end_word_id=make_word_id(12),
            )
        )
        cut_e3 = _edit("e3", "cut 8-9s", CutOp(ranges=[(240, 270)]))
        restore_e1 = _edit("e4", "restore e1", RestoreOp(target_edit_id="e1"))

        tl = replay(src, [cut_e1, caption_e2, cut_e3, restore_e1], transcript)

        # e1 is disabled (no cut at 0-2s), e2 and e3 still active
        assert total_duration_frames(tl.segments) == int(10.0 * FPS) - 30  # only e3 cut
        assert any(o.id == "ov1" for o in tl.overlays)  # e2 still active

    def test_restore_is_reversible_by_another_restore(self):
        """Restoring the RestoreOp itself re-enables the original cut."""
        src = _src(10.0)
        cut_e1 = _edit("e1", "cut", CutOp(ranges=[(0, 90)]))
        restore_e2 = _edit("e2", "restore", RestoreOp(target_edit_id="e1"))
        restore_e2_itself = _edit("e3", "restore restore", RestoreOp(target_edit_id="e2"))

        tl = replay(src, [cut_e1, restore_e2, restore_e2_itself], _transcript())
        # e2 is disabled → e1 is active again → cut applies
        assert total_duration_frames(tl.segments) == int(10.0 * FPS) - 90


# ── Determinism ───────────────────────────────────────────────────


class TestDeterminism:
    def test_same_stack_same_hash(self):
        """Replaying the same stack twice gives identical timeline_hash."""
        src = _src(10.0)
        transcript = _transcript(20)
        edits = [
            _edit("e1", "cut", CutOp(ranges=[(0, 90)])),
            _edit(
                "e2", "caption",
                AddCaptionOp(
                    overlay_id="ov1", text="Hi!",
                    start_word_id=make_word_id(5), end_word_id=make_word_id(7),
                )
            ),
            _edit("e3", "speed", SpeedOp(range=(90, 150), factor=1.5)),
        ]
        hash1 = timeline_hash(replay(src, edits, transcript))
        hash2 = timeline_hash(replay(src, edits, transcript))
        assert hash1 == hash2

    def test_different_edits_different_hash(self):
        src = _src(10.0)
        tl1 = replay(src, [], _transcript())
        tl2 = replay(src, [_edit("e1", "cut", CutOp(ranges=[(0, 90)]))], _transcript())
        assert timeline_hash(tl1) != timeline_hash(tl2)

    def test_hash_three_runs(self):
        """Spec requires identical hash across 3 runs."""
        src = _src(10.0)
        transcript = _transcript(20)
        edits = [
            _edit("e1", "cut", CutOp(ranges=[(0, 60), (150, 180)])),
            _edit("e2", "speed", SpeedOp(range=(90, 150), factor=2.0)),
        ]
        hashes = [timeline_hash(replay(src, edits, transcript)) for _ in range(3)]
        assert len(set(hashes)) == 1, f"Hashes differed across 3 runs: {hashes}"
