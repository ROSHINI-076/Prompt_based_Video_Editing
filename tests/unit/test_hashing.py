"""Tests for engine/hashing.py — deterministic timeline hashing."""

from __future__ import annotations

from incubrix.engine.hashing import canonical_json, timeline_hash, timeline_to_hashable
from incubrix.engine.models import (
    AddCaptionOp,
    CutOp,
    Edit,
    SourceInfo,
    SpeedOp,
    Transcript,
    Word,
    make_word_id,
)
from incubrix.engine.replay import replay
from incubrix.engine.timeline import initial_timeline

FPS = 30.0


def _src() -> SourceInfo:
    return SourceInfo(sha256="deadbeef", duration_frames=9000, fps=FPS)


def _transcript(n: int = 20) -> Transcript:
    words = [
        Word(id=make_word_id(i), text=f"w{i}", start_s=i * 0.5, end_s=i * 0.5 + 0.4)
        for i in range(n)
    ]
    return Transcript(words=words)


class TestCanonicalJson:
    def test_sorted_keys(self):
        obj = {"b": 2, "a": 1}
        result = canonical_json(obj)
        assert result.index('"a"') < result.index('"b"')

    def test_compact_separators(self):
        obj = {"a": [1, 2, 3]}
        result = canonical_json(obj)
        assert " " not in result

    def test_nested(self):
        obj = {"z": {"y": 1, "x": 2}}
        result = canonical_json(obj)
        assert '"x":2' in result
        assert '"y":1' in result


class TestTimelineToHashable:
    def test_excludes_warnings(self):
        tl = initial_timeline(_src())
        tl.warnings.append("Some non-deterministic text")
        hashable = timeline_to_hashable(tl)
        assert "warnings" not in hashable

    def test_includes_segments(self):
        tl = initial_timeline(_src())
        hashable = timeline_to_hashable(tl)
        assert "segments" in hashable

    def test_includes_source(self):
        tl = initial_timeline(_src())
        hashable = timeline_to_hashable(tl)
        assert "source" in hashable


class TestTimelineHash:
    def test_same_timeline_same_hash(self):
        tl = initial_timeline(_src())
        assert timeline_hash(tl) == timeline_hash(tl)

    def test_different_sources_different_hash(self):
        src1 = SourceInfo(sha256="aaa", duration_frames=9000, fps=30.0)
        src2 = SourceInfo(sha256="bbb", duration_frames=9000, fps=30.0)
        tl1 = initial_timeline(src1)
        tl2 = initial_timeline(src2)
        assert timeline_hash(tl1) != timeline_hash(tl2)

    def test_warnings_dont_affect_hash(self):
        tl1 = initial_timeline(_src())
        tl2 = initial_timeline(_src())
        tl2.warnings.append("some warning text")
        assert timeline_hash(tl1) == timeline_hash(tl2)

    def test_hash_is_hex_string(self):
        tl = initial_timeline(_src())
        h = timeline_hash(tl)
        assert isinstance(h, str)
        assert len(h) == 64
        int(h, 16)  # Should not raise

    def test_replay_deterministic_three_runs(self):
        """Spec requirement: same stack → same hash across 3 independent runs."""
        src = _src()
        transcript = _transcript()
        edits = [
            Edit(id="e1", instruction="cut", ops=[CutOp(ranges=[(0, 90), (150, 180)])]),
            Edit(id="e2", instruction="speed", ops=[SpeedOp(range=(90, 150), factor=2.0)]),
            Edit(
                id="e3",
                instruction="caption",
                ops=[
                    AddCaptionOp(
                        overlay_id="ov1",
                        text="Hi!",
                        start_word_id=make_word_id(5),
                        end_word_id=make_word_id(7),
                    )
                ],
            ),
        ]
        hashes = [timeline_hash(replay(src, edits, transcript)) for _ in range(3)]
        assert len(set(hashes)) == 1, f"Hashes differed: {hashes}"
