"""Mapping between SQLAlchemy database models and pure engine models.

The engine itself knows nothing about the database. This module handles:
- Converting VideoRecord to/from SourceInfo
- Converting TranscriptRecord to/from Transcript
- Converting EditRecord and RedoEditRecord to/from Edit
- Deserializing any engine Op preserving exact types and identifiers
"""

from __future__ import annotations

from typing import Any

from incubrix.db.models import EditRecord, RedoEditRecord, TranscriptRecord, VideoRecord
from incubrix.engine.models import (
    AddCaptionOp,
    AnyOp,
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
    SourceInfo,
    SpeedOp,
    StyleOp,
    Transcript,
)

_OP_MAP: dict[str, type[AnyOp]] = {
    "cut": CutOp,
    "keep_only": KeepOnlyOp,
    "speed": SpeedOp,
    "reorder": ReorderOp,
    "add_caption": AddCaptionOp,
    "style": StyleOp,
    "mute": MuteOp,
    "bleep": BleepOp,
    "restore": RestoreOp,
    "remove_fillers": RemoveFillersOp,
    "remove_silence": RemoveSilenceOp,
    "keep_pause": KeepPauseOp,
    "music": MusicOp,
}


def op_to_dict(op: AnyOp) -> dict[str, Any]:
    """Serialize a single engine operation to a JSON-serializable dict."""
    return op.model_dump(mode="json")


def op_from_dict(data: dict[str, Any]) -> AnyOp:
    """Deserialize a dictionary back into its exact engine Op instance."""
    op_name = data.get("op")
    if not op_name or op_name not in _OP_MAP:
        raise ValueError(f"Unknown operation type: {op_name!r}")
    cls = _OP_MAP[op_name]
    return cls.model_validate(data)


def ops_to_list(ops: list[AnyOp]) -> list[dict[str, Any]]:
    """Serialize a list of operations to JSON-serializable dicts."""
    return [op_to_dict(op) for op in ops]


def ops_from_list(data: list[dict[str, Any]]) -> list[AnyOp]:
    """Deserialize a list of operation dictionaries into engine Op instances."""
    return [op_from_dict(item) for item in data]


def video_to_source_info(video: VideoRecord) -> SourceInfo:
    """Reconstruct an immutable engine SourceInfo from a VideoRecord row."""
    duration_frames = round(video.duration_s * video.fps)
    return SourceInfo(
        sha256=video.sha256,
        duration_frames=duration_frames,
        fps=video.fps,
        has_audio=video.has_audio,
    )


def source_info_to_video_kwargs(
    source: SourceInfo,
    video_id: str,
    source_key: str,
    status: str = "ready",
    warnings: list[str] | None = None,
    is_vfr: bool = False,
) -> dict[str, Any]:
    """Prepare keyword arguments to create a VideoRecord row from SourceInfo."""
    duration_s = round(source.duration_frames / source.fps, 3)
    return {
        "id": video_id,
        "source_key": source_key,
        "sha256": source.sha256,
        "duration_s": duration_s,
        "fps": source.fps,
        "has_audio": source.has_audio,
        "is_vfr": is_vfr,
        "status": status,
        "warnings": warnings or [],
    }


def transcript_record_to_transcript(record: TranscriptRecord) -> Transcript:
    """Reconstruct an engine Transcript from a TranscriptRecord row."""
    return Transcript.model_validate(record.data)


def transcript_to_record_kwargs(
    transcript: Transcript,
    video_id: str,
    stt_provider: str = "mock",
    stt_model: str = "base",
    stt_version: str | None = None,
) -> dict[str, Any]:
    """Prepare keyword arguments to create a TranscriptRecord row."""
    return {
        "video_id": video_id,
        "data": transcript.model_dump(mode="json"),
        "stt_provider": stt_provider,
        "stt_model": stt_model,
        "stt_version": stt_version,
    }


def edit_record_to_edit(record: EditRecord | RedoEditRecord) -> Edit:
    """Reconstruct an engine Edit from an EditRecord or RedoEditRecord row."""
    ops = ops_from_list(record.ops)
    intent_ops = getattr(record, "intent_ops", None) or []
    return Edit(
        id=record.edit_id,
        instruction=record.instruction,
        ops=ops,
        intent_ops=intent_ops,
    )


def edit_to_edit_record_kwargs(
    edit: Edit,
    session_id: str,
    position: int,
    active_in_stack: bool = True,
) -> dict[str, Any]:
    """Prepare keyword arguments to create an EditRecord row."""
    return {
        "session_id": session_id,
        "position": position,
        "edit_id": edit.id,
        "instruction": edit.instruction,
        "ops": ops_to_list(edit.ops),
        "active_in_stack": active_in_stack,
        "intent_ops": edit.intent_ops,
    }


def edit_to_redo_record_kwargs(
    edit: Edit,
    session_id: str,
    position: int,
) -> dict[str, Any]:
    """Prepare keyword arguments to create a RedoEditRecord row."""
    return {
        "session_id": session_id,
        "position": position,
        "edit_id": edit.id,
        "instruction": edit.instruction,
        "ops": ops_to_list(edit.ops),
        "intent_ops": edit.intent_ops,
    }
