"""Unit tests for engine/models.py."""

from __future__ import annotations

from incubrix.engine.models import (
    Anchor,
    CutOp,
    Edit,
    Overlay,
    OverlayStatus,
    Segment,
    Word,
    make_word_id,
    segment_output_length,
)


class TestMakeWordId:
    def test_zero_based(self):
        assert make_word_id(0) == "w0001"
        assert make_word_id(1) == "w0002"
        assert make_word_id(99) == "w0100"
        assert make_word_id(9999) == "w10000"

    def test_deterministic(self):
        assert make_word_id(5) == make_word_id(5)


class TestSegmentOutputLength:
    def test_speed_one(self):
        seg = Segment(src_in_f=0, src_out_f=300, speed=1.0)
        assert segment_output_length(seg) == 300

    def test_speed_two(self):
        # 300 / 2.0 = 150
        seg = Segment(src_in_f=0, src_out_f=300, speed=2.0)
        assert segment_output_length(seg) == 150

    def test_speed_half(self):
        # 300 / 0.5 = 600
        seg = Segment(src_in_f=0, src_out_f=300, speed=0.5)
        assert segment_output_length(seg) == 600

    def test_minimum_one_frame(self):
        # Very short segment should still be at least 1 frame
        seg = Segment(src_in_f=0, src_out_f=1, speed=4.0)
        assert segment_output_length(seg) == 1

    def test_rounding(self):
        # 10 / 3.0 = 3.333 → rounds to 3
        seg = Segment(src_in_f=0, src_out_f=10, speed=3.0)
        assert segment_output_length(seg) == 3


class TestWord:
    def test_defaults(self):
        w = Word(id="w0001", text="hello", start_s=0.0, end_s=0.4)
        assert w.speaker == "default"
        assert w.confidence == 1.0
        assert w.flags == []

    def test_custom_fields(self):
        w = Word(
            id="w0002",
            text="world",
            start_s=0.5,
            end_s=0.9,
            speaker="Alice",
            confidence=0.8,
            flags=["low_confidence"],
        )
        assert w.speaker == "Alice"
        assert "low_confidence" in w.flags


class TestOverlay:
    def test_active_by_default(self):
        ov = Overlay(
            id="ov1",
            text="Welcome!",
            anchor=Anchor(start_word_id="w0001", end_word_id="w0004"),
        )
        assert ov.status == OverlayStatus.ACTIVE

    def test_dormant_status(self):
        ov = Overlay(
            id="ov1",
            text="Welcome!",
            anchor=Anchor(start_word_id="w0001", end_word_id="w0004"),
            status=OverlayStatus.DORMANT,
        )
        assert ov.status == OverlayStatus.DORMANT


class TestEdit:
    def test_edit_with_cut_op(self):
        op = CutOp(ranges=[(0, 900)])
        edit = Edit(id="e1", instruction="cut the first 30 seconds", ops=[op])
        assert edit.id == "e1"
        assert len(edit.ops) == 1
        assert isinstance(edit.ops[0], CutOp)
