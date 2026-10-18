"""Unit tests for database mapping and model serialization."""

from __future__ import annotations

import pytest

from incubrix.db.mapping import (
    edit_record_to_edit,
    edit_to_edit_record_kwargs,
    edit_to_redo_record_kwargs,
    op_from_dict,
    op_to_dict,
    ops_from_list,
    ops_to_list,
    source_info_to_video_kwargs,
    transcript_record_to_transcript,
    transcript_to_record_kwargs,
    video_to_source_info,
)
from incubrix.db.models import EditRecord, RedoEditRecord, TranscriptRecord, VideoRecord
from incubrix.engine.models import (
    AddCaptionOp,
    BleepOp,
    CutOp,
    Edit,
    KeepOnlyOp,
    KeepPauseOp,
    MusicOp,
    MuteOp,
    RemoveFillersOp,
    RemoveSilenceOp,
    ReorderOp,
    RestoreOp,
    Sentence,
    SourceInfo,
    SpeedOp,
    StyleOp,
    Transcript,
    Word,
)


class TestMappingSourceInfo:
    def test_source_info_round_trip(self):
        source = SourceInfo(
            sha256="abcdef1234567890" * 4,
            duration_frames=3000,
            fps=30.0,
            has_audio=True,
        )
        kwargs = source_info_to_video_kwargs(
            source,
            video_id="vid_123",
            source_key="sources/abcdef.mp4",
            status="ready",
            warnings=["test warning"],
            is_vfr=False,
        )
        video = VideoRecord(**kwargs)

        assert video.id == "vid_123"
        assert video.sha256 == source.sha256
        assert video.fps == 30.0
        assert video.duration_s == 100.0

        reconstructed = video_to_source_info(video)
        assert reconstructed.sha256 == source.sha256
        assert reconstructed.duration_frames == source.duration_frames
        assert reconstructed.fps == source.fps
        assert reconstructed.has_audio == source.has_audio


class TestMappingTranscript:
    def test_transcript_round_trip(self):
        words = [
            Word(id="w0001", text="Hello", start_s=0.0, end_s=0.5, speaker="spk1", confidence=0.95),
            Word(id="w0002", text="world", start_s=0.6, end_s=1.0, speaker="spk1", confidence=0.98),
        ]
        sentences = [Sentence(first_word_idx=0, last_word_idx=1)]
        transcript = Transcript(
            words=words,
            sentences=sentences,
            speakers=["spk1"],
            warnings=["warn1"],
        )
        kwargs = transcript_to_record_kwargs(
            transcript,
            video_id="vid_123",
            stt_provider="mock",
            stt_model="base",
            stt_version="1.0",
        )
        record = TranscriptRecord(**kwargs)

        assert record.video_id == "vid_123"
        assert record.stt_provider == "mock"

        reconstructed = transcript_record_to_transcript(record)
        assert len(reconstructed.words) == 2
        assert reconstructed.words[0].id == "w0001"
        assert reconstructed.words[0].text == "Hello"
        assert reconstructed.words[1].id == "w0002"
        assert reconstructed.sentences[0].first_word_idx == 0
        assert reconstructed.speakers == ["spk1"]


class TestMappingOps:
    @pytest.mark.parametrize(
        "op",
        [
            CutOp(ranges=[(0, 30), (60, 90)]),
            KeepOnlyOp(range=(30, 120)),
            SpeedOp(range=(0, 60), factor=2.0),
            ReorderOp(range=(30, 60), before=120),
            AddCaptionOp(
                overlay_id="ov_1",
                text="Welcome!",
                start_word_id="w0001",
                end_word_id="w0004",
                style={"color": "yellow"},
            ),
            StyleOp(overlay_id="ov_1", style={"color": "red"}),
            MuteOp(effect_id="fx_mute_1", start_word_id="w0002", end_word_id="w0003"),
            BleepOp(effect_id="fx_bleep_1", start_word_id="w0002", end_word_id="w0003"),
            RestoreOp(target_edit_id="e1"),
            RemoveFillersOp(ranges=[(15, 25)], word_ids=["w0003"]),
            RemoveSilenceOp(ranges=[(40, 50)], min_gap_s=1.5, protected_after_word_ids=["w0001"]),
            KeepPauseOp(after_word_id="w0005"),
            MusicOp(track_id="bundled_default", duck_db=-15.0),
        ],
    )
    def test_each_op_round_trip(self, op):
        d = op_to_dict(op)
        reconstructed = op_from_dict(d)
        assert reconstructed == op
        assert type(reconstructed) is type(op)

    def test_ops_list_round_trip(self):
        ops = [
            CutOp(ranges=[(0, 30)]),
            AddCaptionOp(
                overlay_id="e1_ov1_1",
                text="Intro",
                start_word_id="w0001",
                end_word_id="w0002",
            ),
        ]
        data = ops_to_list(ops)
        reconstructed = ops_from_list(data)
        assert reconstructed == ops

    def test_unknown_op_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown operation type"):
            op_from_dict({"op": "nonexistent_op"})


class TestMappingEdits:
    def test_edit_record_round_trip(self):
        edit = Edit(
            id="e1",
            instruction="Cut first second and caption intro",
            ops=[
                CutOp(ranges=[(0, 30)]),
                AddCaptionOp(
                    overlay_id="e1_ov1_1",
                    text="Hello!",
                    start_word_id="w0001",
                    end_word_id="w0003",
                ),
            ],
            intent_ops=[{"op": "cut", "from_start": 30}],
        )
        kwargs = edit_to_edit_record_kwargs(
            edit, session_id="sess_1", position=0, active_in_stack=True
        )
        record = EditRecord(**kwargs)

        assert record.session_id == "sess_1"
        assert record.position == 0
        assert record.edit_id == "e1"

        reconstructed = edit_record_to_edit(record)
        assert reconstructed.id == "e1"
        assert reconstructed.instruction == edit.instruction
        assert len(reconstructed.ops) == 2
        assert reconstructed.ops[0] == edit.ops[0]
        assert reconstructed.ops[1] == edit.ops[1]
        assert reconstructed.intent_ops == edit.intent_ops

    def test_redo_edit_record_round_trip(self):
        edit = Edit(
            id="e2",
            instruction="Speed up intro",
            ops=[SpeedOp(range=(0, 60), factor=2.0)],
        )
        kwargs = edit_to_redo_record_kwargs(edit, session_id="sess_1", position=0)
        record = RedoEditRecord(**kwargs)

        reconstructed = edit_record_to_edit(record)
        assert reconstructed.id == "e2"
        assert reconstructed.ops[0] == edit.ops[0]
