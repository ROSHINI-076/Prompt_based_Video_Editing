"""Additional tests to reach ≥97% coverage on engine modules.

Covers previously missed branches:
- ops.py: apply_speed when no previous speed to trigger overlap (line 73)
- ops.py: apply_music (lines 377-384)
- replay.py: MuteOp, BleepOp, MusicOp, StyleOp through replay (lines 130-148)
- session.py: get_layout() (line 99)
- timeparse.py: edge cases (bare number-word, minute with no number)
"""

from __future__ import annotations

from incubrix.engine.models import (
    AddCaptionOp,
    BleepOp,
    CutOp,
    Edit,
    EffectType,
    MusicOp,
    MuteOp,
    SourceInfo,
    StyleOp,
    Transcript,
    Word,
    make_word_id,
)
from incubrix.engine.ops import apply_music, apply_speed
from incubrix.engine.replay import replay
from incubrix.engine.session import SessionManager
from incubrix.engine.timeparse import TimePoint, _parse_duration, parse_time_expression

FPS = 30.0


def _src(duration_s: float = 10.0) -> SourceInfo:
    return SourceInfo(sha256="test", duration_frames=int(duration_s * FPS), fps=FPS)


def _transcript(n: int = 20) -> Transcript:
    words = [
        Word(id=make_word_id(i), text=f"w{i}", start_s=i * 0.5, end_s=i * 0.5 + 0.4)
        for i in range(n)
    ]
    return Transcript(words=words)


def _edit(edit_id: str, instruction: str, *ops) -> Edit:
    return Edit(id=edit_id, instruction=instruction, ops=list(ops))


# ── ops.py coverage gaps ──────────────────────────────────────────


class TestApplySpeedCoverage:
    def test_no_overlap_no_warning(self):
        """A first speed change on normal footage is not an overlap."""
        from incubrix.engine.models import Segment

        segs = [Segment(src_in_f=0, src_out_f=300, speed=1.0)]
        new_segs, warnings = apply_speed(segs, (0, 300), 2.0)
        assert warnings == []
        assert new_segs[0].speed == 2.0

    def test_overlapping_speed_replacement_warns_and_last_speed_wins(self):
        from incubrix.engine.models import Segment

        segments, warnings = apply_speed([Segment(src_in_f=0, src_out_f=300)], (30, 120), 2.0)
        segments, replacement_warnings = apply_speed(segments, (60, 150), 3.0)

        assert len(warnings) == 0
        assert len(replacement_warnings) == 1
        assert "overlap" in replacement_warnings[0].lower()
        assert [(seg.src_in_f, seg.src_out_f, seg.speed) for seg in segments] == [
            (0, 30, 1.0),
            (30, 60, 2.0),
            (60, 120, 3.0),
            (120, 150, 3.0),
            (150, 300, 1.0),
        ]

    def test_reapplying_same_speed_does_not_warn(self):
        from incubrix.engine.models import Segment

        segments, warnings = apply_speed([Segment(src_in_f=0, src_out_f=300)], (30, 120), 2.0)
        segments, repeated_warnings = apply_speed(segments, (30, 120), 2.0)

        assert warnings == []
        assert repeated_warnings == []
        assert [(seg.src_in_f, seg.src_out_f, seg.speed) for seg in segments] == [
            (0, 30, 1.0),
            (30, 120, 2.0),
            (120, 300, 1.0),
        ]

    def test_speed_change_on_different_range_does_not_warn(self):
        from incubrix.engine.models import Segment

        segments, _ = apply_speed([Segment(src_in_f=0, src_out_f=300)], (30, 90), 2.0)
        segments, warnings = apply_speed(segments, (150, 210), 3.0)

        assert warnings == []
        assert [(seg.src_in_f, seg.src_out_f, seg.speed) for seg in segments] == [
            (0, 30, 1.0),
            (30, 90, 2.0),
            (90, 150, 1.0),
            (150, 210, 3.0),
            (210, 300, 1.0),
        ]

    def test_apply_music_returns_settings(self):
        from incubrix.engine.models import MusicSettings

        music, warnings = apply_music(MusicSettings(), "track.mp3", -18.0, True)
        assert music.track == "track.mp3"
        assert music.duck_db == -18.0
        assert music.enabled is True
        assert warnings == []


# ── replay.py coverage gaps ───────────────────────────────────────


class TestReplayEffectsAndMusic:
    def test_mute_op_through_replay(self):
        src = _src(10.0)
        transcript = _transcript(20)
        edits = [
            _edit(
                "e1",
                "mute words 5-7",
                MuteOp(effect_id="fx1", start_word_id=make_word_id(5), end_word_id=make_word_id(7)),
            )
        ]
        tl = replay(src, edits, transcript)
        assert len(tl.effects) == 1
        assert tl.effects[0].type == EffectType.MUTE
        assert tl.effects[0].id == "fx1"

    def test_bleep_op_through_replay(self):
        src = _src(10.0)
        transcript = _transcript(20)
        edits = [
            _edit(
                "e1",
                "bleep word 3",
                BleepOp(
                    effect_id="fx2", start_word_id=make_word_id(3), end_word_id=make_word_id(3)
                ),
            )
        ]
        tl = replay(src, edits, transcript)
        assert len(tl.effects) == 1
        assert tl.effects[0].type == EffectType.BLEEP

    def test_music_op_through_replay(self):
        src = _src(10.0)
        edits = [
            _edit("e1", "add music", MusicOp(track="background.mp3", duck_db=-12.0, enabled=True))
        ]
        tl = replay(src, edits, _transcript())
        assert tl.music.enabled is True
        assert tl.music.track == "background.mp3"

    def test_style_op_through_replay(self):
        src = _src(10.0)
        transcript = _transcript(20)
        edits = [
            _edit(
                "e1",
                "add caption",
                AddCaptionOp(
                    overlay_id="ov1",
                    text="Hi!",
                    start_word_id=make_word_id(5),
                    end_word_id=make_word_id(7),
                    style={"color": "white"},
                ),
            ),
            _edit("e2", "style red", StyleOp(overlay_id="ov1", style={"color": "red"})),
        ]
        tl = replay(src, edits, transcript)
        ov = next(o for o in tl.overlays if o.id == "ov1")
        assert ov.style["color"] == "red"

    def test_multiple_effects_through_replay(self):
        src = _src(10.0)
        transcript = _transcript(20)
        edits = [
            _edit(
                "e1",
                "mute",
                MuteOp(effect_id="fx1", start_word_id=make_word_id(1), end_word_id=make_word_id(2)),
            ),
            _edit(
                "e2",
                "bleep",
                BleepOp(
                    effect_id="fx2", start_word_id=make_word_id(5), end_word_id=make_word_id(6)
                ),
            ),
        ]
        tl = replay(src, edits, transcript)
        assert len(tl.effects) == 2
        types = {e.type for e in tl.effects}
        assert EffectType.MUTE in types
        assert EffectType.BLEEP in types


# ── session.py coverage gaps ─────────────────────────────────────


class TestSessionLayout:
    def test_get_layout(self):
        mgr = SessionManager(_src(10.0), _transcript())
        layout = mgr.get_layout()
        assert layout.total_frames == int(10.0 * FPS)

    def test_get_layout_after_cut(self):
        mgr = SessionManager(_src(10.0), _transcript())
        mgr.push_edit("cut 3s", [CutOp(ranges=[(0, 90)])])
        layout = mgr.get_layout()
        assert layout.total_frames == int(10.0 * FPS) - 90


# ── timeparse.py coverage gaps ────────────────────────────────────


class TestTimeparseCoverage:
    def test_numeric_hours(self):
        result = _parse_duration("2 hours")
        assert result == 7200.0

    def test_numeric_hr(self):
        result = _parse_duration("1 hr")
        assert result == 3600.0

    def test_bare_number_word_treated_as_seconds(self):
        # 'thirty' alone treated as 30 seconds
        result = parse_time_expression("thirty seconds")
        assert isinstance(result, TimePoint)
        assert result.seconds == 30.0

    def test_first_without_the(self):
        from incubrix.engine.timeparse import TimeRange

        result = parse_time_expression("first 15 seconds")
        assert isinstance(result, TimeRange)
        assert result.start == 0.0
        assert result.end == 15.0

    def test_last_without_the(self):
        from incubrix.engine.timeparse import TimeRange

        result = parse_time_expression("last 5 seconds")
        assert isinstance(result, TimeRange)
        assert result.from_end == 5.0

    def test_a_minute_with_word_number(self):
        result = _parse_duration("a minute and thirty seconds")
        assert result == 90.0

    def test_minute_forty_five(self):
        result = _parse_duration("a minute forty five")
        assert result == 105.0
