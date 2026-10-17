"""Pure three-way instruction handling for the video-editing engine.

Intent dictionaries are resolved against a scratch SessionManager first. A
clarification or failed instruction therefore cannot partially mutate history;
successful parts are committed together as one undoable Edit.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from incubrix.engine.hashing import timeline_hash
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
    SpeedOp,
    StyleOp,
    Transcript,
    Word,
)
from incubrix.engine.ops import AUTO_CAPTION_MAX_WORDS, MIN_SILENCE_PAD_S
from incubrix.engine.resolver import (
    CaptionTarget,
    PhraseTarget,
    ReorderDestination,
    Resolution,
    ResolutionOption,
    Resolver,
    RestoreTarget,
    TimeTarget,
    TopicTarget,
)
from incubrix.engine.session import SessionManager
from incubrix.engine.timeline import is_word_alive, timeline_to_output

MIN_SPEED = 0.25
MAX_SPEED = 4.0
LOW_CONFIDENCE_THRESHOLD = 0.6
ALLOWED_MUSIC_TRACK_IDS = frozenset({"bundled_default"})


class _InputError(Exception):
    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _format_time(seconds: float) -> str:
    total = max(0, round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _format_duration(seconds: float) -> str:
    total = max(0, round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _duration_s(timeline: Any) -> float:
    frames = sum(
        max(1, round((segment.src_out_f - segment.src_in_f) / segment.speed))
        for segment in timeline.segments
    )
    return frames / timeline.source.fps


def _length_summary(before: Any, after: Any) -> str:
    before_s = _duration_s(before)
    after_s = _duration_s(after)
    delta = after_s - before_s
    sign = "-" if delta < 0 else "+"
    return (
        f"Video length {_format_duration(before_s)} -> {_format_duration(after_s)} "
        f"({sign}{_format_duration(abs(delta))})"
    )


def _response(
    session: SessionManager,
    status: str,
    *,
    operations: list[dict] | None = None,
    summary: str = "",
    question: str | None = None,
    options: list[dict] | None = None,
    warnings: list[str] | None = None,
    time_policy: str = "current",
    edit_id: str | None = None,
    error: dict | None = None,
) -> dict:
    timeline = session.get_timeline()
    return {
        "status": status,
        "operations": operations or [],
        "summary": summary,
        "question": question,
        "options": options,
        "warnings": warnings or [],
        "timeline_hash": timeline_hash(timeline),
        "duration_s": round(
            sum(
                max(1, round((segment.src_out_f - segment.src_in_f) / segment.speed))
                for segment in timeline.segments
            )
            / timeline.source.fps,
            3,
        ),
        "time_policy": time_policy,
        "edit_id": edit_id,
        "error": error,
    }


def _error_response(
    session: SessionManager,
    code: str,
    message: str,
    *,
    details: Any = None,
    warnings: list[str] | None = None,
    summary: str | None = None,
    time_policy: str = "current",
) -> dict:
    return _response(
        session,
        "rejected",
        summary=summary or message,
        warnings=warnings,
        time_policy=time_policy,
        error={"code": code, "message": message, "details": details},
    )


def _target_dict(value: Any) -> dict:
    if isinstance(value, str):
        return {"kind": "phrase", "text": value}
    if not isinstance(value, Mapping):
        raise _InputError("INVALID_ARGUMENT", "An operation target must be a structured object.")
    result = dict(value)
    if "kind" not in result:
        if "start" in result or "end" in result or "from_start" in result or "from_end" in result:
            result["kind"] = "time"
        elif "text" in result:
            result["kind"] = "phrase"
    return result


def _target_resolution(target_value: Any, resolver: Resolver) -> Resolution:
    target = _target_dict(target_value)
    kind = target.get("kind")
    choice = target.get("choice")
    if kind == "time":
        policy = target.get("policy", "current")
        if policy not in ("current", "original"):
            raise _InputError("INVALID_ARGUMENT", "Time policy must be 'current' or 'original'.")
        has_start, has_end = target.get("start") is not None, target.get("end") is not None
        has_from_start = target.get("from_start") is not None
        has_from_end = target.get("from_end") is not None
        if (
            (has_start != has_end)
            or sum((has_start, has_end, has_from_start, has_from_end)) not in (0, 2, 1)
            or (has_start and (has_from_start or has_from_end))
            or (has_from_start and has_from_end)
        ):
            raise _InputError(
                "INVALID_ARGUMENT",
                "Time target must provide start and end, from_start, or from_end.",
            )
        if not (has_start or has_from_start or has_from_end):
            raise _InputError(
                "INVALID_ARGUMENT",
                "Time target is missing its range fields.",
            )
        bounds = {
            name: _as_float(target[name], name)
            for name in ("start", "end", "from_start", "from_end")
            if target.get(name) is not None
        }
        return resolver.resolve_time(
            TimeTarget(
                start=bounds.get("start"),
                end=bounds.get("end"),
                from_start=bounds.get("from_start"),
                from_end=bounds.get("from_end"),
                policy=policy,
            )
        )
    if kind in ("phrase", "word"):
        relation = target.get("relation", "at")
        if relation not in ("at", "before", "after"):
            raise _InputError("INVALID_ARGUMENT", f"Unsupported phrase relation: {relation!r}.")
        text = target.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _InputError("INVALID_ARGUMENT", "Phrase target requires non-empty text.")
        speaker = target.get("speaker")
        if speaker is not None and not isinstance(speaker, str):
            raise _InputError("INVALID_ARGUMENT", "Phrase speaker must be a string.")
        return resolver.resolve_phrase(
            PhraseTarget(
                text=text,
                relation=relation,
                speaker=speaker,
                choice=choice,
            )
        )
    if kind == "topic":
        query = target.get("query", target.get("text", ""))
        if not isinstance(query, str) or not query.strip():
            raise _InputError("INVALID_ARGUMENT", "Topic target requires a non-empty query.")
        speaker = target.get("speaker")
        if speaker is not None and not isinstance(speaker, str):
            raise _InputError("INVALID_ARGUMENT", "Topic speaker must be a string.")
        return resolver.resolve_topic(
            TopicTarget(
                query=query,
                speaker=speaker,
                choice=choice,
            )
        )
    if kind == "caption":
        text = target.get("text")
        if text is not None and not isinstance(text, str):
            raise _InputError("INVALID_ARGUMENT", "Caption selection text must be a string.")
        return resolver.resolve_caption(
            CaptionTarget(
                text=text,
                choice=choice,
            )
        )
    if kind in ("edit", "overlap", "any", "cut_ordinal"):
        overlap = None
        if kind == "overlap":
            start = target.get("start")
            end = target.get("end")
            if start is None or end is None:
                raise _InputError(
                    "INVALID_ARGUMENT", "Restore overlap requires start and end times."
                )
            overlap = (_as_float(start, "Overlap start"), _as_float(end, "Overlap end"))
        edit_id = target.get("edit_id") if kind == "edit" else None
        if kind == "edit" and (not isinstance(edit_id, str) or not edit_id):
            raise _InputError("INVALID_ARGUMENT", "Restore edit target requires an edit id.")
        cut_ordinal = target.get("from_end") if kind == "cut_ordinal" else None
        if kind == "cut_ordinal" and (
            isinstance(cut_ordinal, bool) or not isinstance(cut_ordinal, int)
        ):
            raise _InputError("INVALID_ARGUMENT", "Cut ordinal from_end must be an integer.")
        return resolver.resolve_restore(
            RestoreTarget(
                edit_id=edit_id,
                overlap=overlap,
                cut_ordinal=cut_ordinal,
                any=kind == "any",
                choice=choice,
            )
        )
    raise _InputError("INVALID_ARGUMENT", f"Unsupported target kind: {kind!r}.")


def _destination_resolution(value: Any, resolver: Resolver) -> Resolution:
    if isinstance(value, str):
        if value in ("start", "end"):
            return resolver.resolve_reorder_destination(ReorderDestination(kind=value))
        value = {"kind": "phrase", "phrase": value}
    if not isinstance(value, Mapping):
        raise _InputError("BAD_RANGE", "A reorder destination must be structured.")
    kind = value.get("kind")
    if kind == "phrase":
        phrase = value.get("phrase", value.get("text"))
        if not isinstance(phrase, str) or not phrase.strip():
            raise _InputError("BAD_RANGE", "A reorder phrase destination must contain text.")
        speaker = value.get("speaker")
        if speaker is not None and not isinstance(speaker, str):
            raise _InputError("BAD_RANGE", "A reorder destination speaker must be a string.")
        return resolver.resolve_reorder_destination(
            ReorderDestination(
                kind="phrase",
                phrase=phrase,
                speaker=value.get("speaker"),
                choice=value.get("choice"),
            )
        )
    if kind == "time":
        time_s = value.get("time_s", value.get("time"))
        if time_s is None:
            raise _InputError("BAD_RANGE", "A reorder time destination requires a time.")
        return resolver.resolve_reorder_destination(
            ReorderDestination(
                kind="time",
                time_s=_as_float(time_s, "Destination time", error_code="BAD_RANGE"),
            )
        )
    if kind in ("start", "end"):
        return resolver.resolve_reorder_destination(ReorderDestination(kind=kind))
    raise _InputError("BAD_RANGE", "Reorder destination must be start, end, time, or phrase.")


def _active_target_path(op: Mapping, resolver: Resolver) -> tuple[Resolution, str]:
    if op.get("op") == "reorder":
        source = _target_resolution(op.get("target"), resolver)
        if source.status != "RESOLVED":
            return source, "target"
        destination = _destination_resolution(op.get("before", op.get("destination")), resolver)
        return destination, "before"
    return _target_resolution(op.get("target"), resolver), "target"


def _words_for_ranges(
    ranges: Sequence[tuple[int, int]],
    transcript: Transcript,
    resolver: Resolver,
) -> list[str]:
    selected: list[str] = []
    for word in transcript.words:
        midpoint = round(((word.start_s + word.end_s) / 2) * resolver.fps)
        if any(start <= midpoint < end for start, end in ranges) and is_word_alive(
            word, resolver.timeline.segments, resolver.fps
        ):
            selected.append(word.id)
    return selected


def _word_groups(word_ids: Sequence[str], transcript: Transcript) -> list[list[Word]]:
    by_id = {word.id: (index, word) for index, word in enumerate(transcript.words)}
    indexed = sorted(
        (by_id[word_id] for word_id in dict.fromkeys(word_ids) if word_id in by_id),
        key=lambda item: item[0],
    )
    groups: list[list[Word]] = []
    previous_index = -2
    for index, word in indexed:
        if index != previous_index + 1:
            groups.append([])
        groups[-1].append(word)
        previous_index = index
    return groups


def _as_float(value: Any, label: str, *, error_code: str = "INVALID_ARGUMENT") -> float:
    if isinstance(value, bool):
        raise _InputError(error_code, f"{label} must be numeric.")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise _InputError(error_code, f"{label} must be numeric.") from exc
    if not math.isfinite(number):
        raise _InputError(error_code, f"{label} must be finite.")
    return number


def _active_intent(
    resolver: Resolver,
    name: str,
    matches: Any = None,
) -> tuple[Edit, Mapping] | None:
    """Find the latest active edit carrying a matching parsed intent."""
    active_ids = resolver._active_edit_ids()
    for edit in reversed(resolver.edits):
        if edit.id not in active_ids:
            continue
        for intent in reversed(edit.intent_ops):
            if intent.get("op") == name and (matches is None or matches(intent)):
                return edit, intent
    return None


def _active_music_edit(resolver: Resolver) -> Edit | None:
    """Find the active music edit that supplies the current track setting."""
    active_ids = resolver._active_edit_ids()
    current_track = resolver.timeline.music.track_id
    for edit in reversed(resolver.edits):
        if edit.id in active_ids and any(
            isinstance(op, MusicOp) and op.track_id == current_track for op in edit.ops
        ):
            return edit
    return None


def _resolve_part(
    op: Mapping,
    resolver: Resolver,
    edit_id: str,
    part_index: int,
) -> tuple[list[AnyOp], Resolution | None, str, str | None, list[str], str | None]:
    raw_name = op.get("op")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise _InputError("INVALID_ARGUMENT", "An operation requires a non-empty name.")
    name = raw_name
    supported = {
        "cut",
        "keep_only",
        "speed",
        "reorder",
        "add_caption",
        "style",
        "mute",
        "bleep",
        "restore",
        "remove_fillers",
        "remove_silence",
        "keep_pause",
        "auto_caption",
        "add_music",
    }
    if name not in supported:
        raise _InputError("UNSUPPORTED_OPERATION", f"Unsupported instruction operation: {name!r}.")

    if name == "remove_fillers":
        if _active_intent(resolver, name):
            raise _InputError("NO_CHANGE", "Filler words were already removed.")
        resolved = resolver.resolve_fillers()
        if resolved.status != "RESOLVED":
            return [], resolved, "", name, resolved.warnings, None
        removed_count = len(resolved.word_ids)
        plural = "" if removed_count == 1 else "s"
        return (
            [RemoveFillersOp(ranges=resolved.source_ranges, word_ids=resolved.word_ids)],
            None,
            "",
            name,
            resolved.warnings,
            f"Removed {removed_count} filler word{plural}.",
        )

    if name == "remove_silence":
        min_gap_s = _as_float(op.get("min_gap_s"), "Minimum silence gap")
        if min_gap_s < 0:
            raise _InputError("INVALID_ARGUMENT", "Minimum silence gap cannot be negative.")
        repeated = _active_intent(
            resolver,
            name,
            lambda prior: prior.get("min_gap_s") == min_gap_s,
        )
        if repeated:
            raise _InputError("NO_CHANGE", "Silence gaps were already removed at this threshold.")
        resolved = resolver.resolve_silence(min_gap_s, MIN_SILENCE_PAD_S)
        if resolved.status != "RESOLVED":
            return [], resolved, "", name, resolved.warnings, None
        gap_count = len(resolved.source_ranges)
        plural = "" if gap_count == 1 else "s"
        return (
            [
                RemoveSilenceOp(
                    ranges=resolved.source_ranges,
                    min_gap_s=min_gap_s,
                    protected_after_word_ids=list(resolver.timeline.protected_pause_after_word_ids),
                )
            ],
            None,
            "",
            name,
            resolved.warnings,
            f"Removed {gap_count} silence gap{plural} longer than {min_gap_s:g}s.",
        )

    if name == "keep_pause":
        resolved, ambiguous_path = _active_target_path(op, resolver)
        if resolved.status != "RESOLVED":
            return [], resolved, ambiguous_path, name, [], None
        groups = _word_groups(resolved.word_ids, resolver.transcript)
        if not groups:
            raise _InputError("NO_MATCH", "No alive phrase or word anchors a pause.")
        pause_ops = [KeepPauseOp(after_word_id=group[-1].id) for group in groups]
        words_by_id = {word.id: word for word in resolver.transcript.words}
        labels = [
            f"'{words_by_id[pause.after_word_id].text.strip('.,!?;:')}'" for pause in pause_ops
        ]
        summary = f"Protected pause after {', '.join(labels)}."
        return pause_ops, None, "", name, [], summary

    if name == "auto_caption":
        max_words = op.get("max_words", AUTO_CAPTION_MAX_WORDS)
        if isinstance(max_words, bool) or not isinstance(max_words, int) or max_words < 1:
            raise _InputError(
                "INVALID_ARGUMENT", "Auto-caption max_words must be a positive integer."
            )
        if _active_intent(
            resolver,
            name,
            lambda prior: prior.get("max_words", AUTO_CAPTION_MAX_WORDS) == max_words,
        ):
            raise _InputError("NO_CHANGE", "The transcript is already auto-captioned this way.")
        captions = []
        for sentence_index, (first, last) in enumerate(resolver._sentences()):
            alive = [
                resolver.transcript.words[index]
                for index in range(first, last + 1)
                if is_word_alive(
                    resolver.transcript.words[index],
                    resolver.timeline.segments,
                    resolver.fps,
                )
            ]
            for group_index, group in enumerate(
                _word_groups([word.id for word in alive], resolver.transcript)
            ):
                for chunk_index in range(0, len(group), max_words):
                    chunk = group[chunk_index : chunk_index + max_words]
                    captions.append(
                        AddCaptionOp(
                            overlay_id=(
                                f"{edit_id}_auto_{part_index + 1}_"
                                f"{sentence_index + 1}_{group_index + 1}_"
                                f"{chunk_index // max_words + 1}"
                            ),
                            text=" ".join(word.text for word in chunk),
                            start_word_id=chunk[0].id,
                            end_word_id=chunk[-1].id,
                        )
                    )
        if not captions:
            raise _InputError("NO_MATCH", "No alive transcript words are available to caption.")
        plural = "" if len(captions) == 1 else "s"
        return (
            captions,
            None,
            "",
            name,
            [],
            f"Added {len(captions)} auto-caption{plural}.",
        )

    if name == "add_music":
        track_id = op.get("track_id")
        if not isinstance(track_id, str) or track_id not in ALLOWED_MUSIC_TRACK_IDS:
            raise _InputError(
                "NO_TARGET",
                f"Music track {track_id!r} is not in the configured allow-list.",
                {"allowed_track_ids": sorted(ALLOWED_MUSIC_TRACK_IDS)},
            )
        duck_db = _as_float(op.get("duck_db", -12.0), "Music duck_db")
        duck_when = op.get("duck_when", "speech")
        if duck_when != "speech":
            raise _InputError("INVALID_ARGUMENT", "Music duck_when must be 'speech'.")
        replace = op.get("replace", False)
        if not isinstance(replace, bool):
            raise _InputError("INVALID_ARGUMENT", "Music replace must be a boolean.")
        if resolver.timeline.music.enabled and not replace:
            existing = _active_music_edit(resolver)
            existing_ref = (
                f"edit {existing.id} ('{existing.instruction}')"
                if existing is not None
                else f"track '{resolver.timeline.music.track_id}'"
            )
            raise _InputError(
                "EDIT_CONFLICT",
                f"Music is already set by {existing_ref}; request replace to change it.",
                {"conflicting_edit_id": existing.id if existing is not None else None},
            )
        return (
            [MusicOp(track_id=track_id, duck_db=duck_db, duck_when=duck_when)],
            None,
            "",
            name,
            [],
            f"Added music track '{track_id}' ducked under speech.",
        )

    speed_factor = 0.0
    if name == "speed":
        speed_factor = _as_float(op.get("factor"), "Speed factor", error_code="INVALID_SPEED")
        if not MIN_SPEED <= speed_factor <= MAX_SPEED:
            raise _InputError(
                "INVALID_SPEED",
                f"Speed {speed_factor:g}x is outside the allowed range "
                f"{MIN_SPEED:g}x-{MAX_SPEED:g}x.",
                {"minimum": MIN_SPEED, "maximum": MAX_SPEED, "requested": speed_factor},
            )
    if name == "add_caption":
        text = op.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _InputError("INVALID_ARGUMENT", "A caption requires non-empty text.")
        style = op.get("style", {})
        if not isinstance(style, Mapping):
            raise _InputError("INVALID_ARGUMENT", "Caption style must be an object.")
    if name == "style":
        style = op.get("style")
        if not isinstance(style, Mapping) or not style:
            raise _InputError(
                "INVALID_ARGUMENT", "A style operation requires a non-empty style object."
            )
    resolved, ambiguous_path = _active_target_path(op, resolver)
    if resolved.status != "RESOLVED":
        return [], resolved, ambiguous_path, name, [], None
    target = _target_dict(op.get("target"))
    if name == "cut" and target.get("kind") == "topic" and target.get("speaker"):
        lead_in = resolver.resolve_host_lead_in(
            resolved.word_ids,
            target["speaker"],
        )
        host_question_choice = target.get("host_question_choice")
        if lead_in is not None and host_question_choice not in {
            "keep_host_question",
            "remove_host_question",
        }:
            return (
                [],
                Resolution(
                    status="AMBIGUOUS",
                    detail=(
                        "Should the host's lead-in question also be removed, or should it be kept?"
                    ),
                    note=lead_in.note,
                    options=[
                        ResolutionOption(
                            choice="remove_host_question",
                            snippet="Also remove the host's question",
                        ),
                        ResolutionOption(
                            choice="keep_host_question",
                            snippet="Keep the host's question",
                        ),
                    ],
                ),
                "host_lead_in",
                name,
                [],
                None,
            )
        if lead_in is not None and host_question_choice == "remove_host_question":
            resolved.source_ranges = sorted(resolved.source_ranges + lead_in.source_ranges)

    if name == "cut":
        if not resolved.source_ranges:
            raise _InputError("NO_MATCH", "The cut target has no source range.")
        return [CutOp(ranges=resolved.source_ranges)], None, "", name, [], None
    if name == "keep_only":
        if len(resolved.source_ranges) != 1:
            raise _InputError(
                "INVALID_ARGUMENT", "Keep-only must resolve to exactly one continuous range."
            )
        return [KeepOnlyOp(range=resolved.source_ranges[0])], None, "", name, [], None
    if name == "speed":
        if not resolved.source_ranges:
            raise _InputError("NO_MATCH", "The speed target has no surviving source range.")
        return (
            [
                SpeedOp(range=source_range, factor=speed_factor)
                for source_range in resolved.source_ranges
            ],
            None,
            "",
            name,
            [],
            None,
        )
    if name == "reorder":
        source_result = _target_resolution(op.get("target"), resolver)
        assert source_result.status == "RESOLVED"
        if len(source_result.source_ranges) != 1:
            raise _InputError(
                "INVALID_ARGUMENT", "Reorder must resolve to one continuous source range."
            )
        destination = resolved.destination_frame
        assert destination is not None
        return (
            [ReorderOp(range=source_result.source_ranges[0], before=destination)],
            None,
            "",
            name,
            [],
            None,
        )
    if name == "add_caption":
        text = op.get("text")
        assert isinstance(text, str)
        word_ids = resolved.word_ids or _words_for_ranges(
            resolved.source_ranges, resolver.transcript, resolver
        )
        groups = _word_groups(word_ids, resolver.transcript)
        if not groups:
            raise _InputError("NO_MATCH", "No alive words anchor the requested caption.")
        caption_ops = []
        for group_index, group in enumerate(groups):
            overlay_id = f"{edit_id}_ov{part_index + 1}_{group_index + 1}"
            caption_ops.append(
                AddCaptionOp(
                    overlay_id=overlay_id,
                    text=text,
                    start_word_id=group[0].id,
                    end_word_id=group[-1].id,
                    style=dict(op.get("style", {})),
                )
            )
        return caption_ops, None, "", name, [], None
    if name == "style":
        style = op.get("style")
        assert resolved.overlay_ids
        return (
            [
                StyleOp(overlay_id=overlay_id, style=dict(style))
                for overlay_id in resolved.overlay_ids
            ],
            None,
            "",
            name,
            [],
            None,
        )
    if name in ("mute", "bleep"):
        word_ids = resolved.word_ids or _words_for_ranges(
            resolved.source_ranges, resolver.transcript, resolver
        )
        groups = _word_groups(word_ids, resolver.transcript)
        if not groups:
            raise _InputError("NO_MATCH", f"No alive words match the {name} target.")
        effects: list[AnyOp] = []
        for group_index, group in enumerate(groups):
            effect_id = f"{edit_id}_fx{part_index + 1}_{group_index + 1}"
            cls = MuteOp if name == "mute" else BleepOp
            effects.append(
                cls(
                    effect_id=effect_id,
                    start_word_id=group[0].id,
                    end_word_id=group[-1].id,
                )
            )
        return effects, None, "", name, [], None
    if name == "restore":
        assert resolved.edit_ids
        return (
            [RestoreOp(target_edit_id=target_edit_id) for target_edit_id in resolved.edit_ids],
            None,
            "",
            name,
            [],
            None,
        )


def _resolution_error(resolution: Resolution, operation: str) -> tuple[str, str, Any]:
    code = resolution.error_code or resolution.status
    if operation == "style" and code == "NO_MATCH":
        code = "NO_TARGET"
    message = resolution.detail or f"The {operation} target could not be resolved."
    return code, message, {"note": resolution.note}


def _conflicting_cut(intent: Mapping, resolver: Resolver) -> tuple[str, str] | None:
    """Name the earlier active cut that explains a target found only in removed content."""
    target = intent.get("target")
    if not isinstance(target, Mapping):
        return None
    source_ranges: list[tuple[int, int]] = []
    if target.get("kind") == "time" and target.get("policy", "current") == "original":
        fps = resolver.fps
        duration = resolver.timeline.source.duration_frames
        if target.get("from_start") is not None:
            source_ranges = [(0, min(duration, round(float(target["from_start"]) * fps)))]
        elif target.get("from_end") is not None:
            start = max(0, duration - round(float(target["from_end"]) * fps))
            source_ranges = [(start, duration)]
        elif target.get("start") is not None and target.get("end") is not None:
            source_ranges = [
                (
                    max(0, round(float(target["start"]) * fps)),
                    min(duration, round(float(target["end"]) * fps)),
                )
            ]
    elif target.get("kind") == "phrase":
        matches = resolver._phrase_matches_all_words(
            str(target.get("text", "")), target.get("speaker")
        )
        source_ranges = [resolver._snapped_match_range(match) for match in matches]
    if not source_ranges:
        return None

    active = resolver._active_edit_ids()
    for edit in resolver.edits:
        if edit.id not in active:
            continue
        for cut_start, cut_end in resolver._cut_ranges_for_edit(edit):
            if any(start < cut_end and end > cut_start for start, end in source_ranges):
                return edit.id, edit.instruction
    return None


def _option_dict(option: Any) -> dict:
    return {
        "choice": option.choice,
        "start_s": option.current_start_s,
        "end_s": option.current_end_s,
        "snippet": option.snippet,
        "edit_id": option.edit_id,
        "overlay_id": option.overlay_id,
    }


def _clarification_question(op: Mapping, resolution: Resolution) -> str:
    if resolution.detail and resolution.detail.startswith("Should the host's lead-in question"):
        return resolution.detail
    target = op.get("target")
    target_data = dict(target) if isinstance(target, Mapping) else {}
    query = target_data.get("text", target_data.get("query"))
    matches = [option for option in resolution.options if option.choice != "all"]
    positions = [
        _format_time(option.current_start_s)
        for option in matches
        if option.current_start_s is not None
    ]
    if query and positions:
        if len(positions) == 2:
            location = f"{positions[0]} and {positions[1]}"
        else:
            location = ", ".join(positions[:-1]) + f" and {positions[-1]}"
        return f"'{query}' is mentioned {len(matches)} times at {location}. Which one, or all?"
    if matches:
        labels = [
            f"{option.edit_id}: {option.snippet}" if option.edit_id else option.snippet
            for option in matches
        ]
        return f"Which match do you mean ({'; '.join(labels)}), or all?"
    assert resolution.detail is not None
    return resolution.detail


def _format_operation(op: AnyOp, fps: float) -> dict:
    def range_seconds(value: tuple[int, int]) -> dict:
        return {"start_s": round(value[0] / fps, 3), "end_s": round(value[1] / fps, 3)}

    if isinstance(op, CutOp):
        return {"op": "cut", "ranges": [range_seconds(item) for item in op.ranges]}
    if isinstance(op, KeepOnlyOp):
        return {"op": "keep_only", "range": range_seconds(op.range)}
    if isinstance(op, SpeedOp):
        return {"op": "speed", "range": range_seconds(op.range), "factor": op.factor}
    if isinstance(op, ReorderOp):
        return {
            "op": "reorder",
            "range": range_seconds(op.range),
            "before_s": round(op.before / fps, 3),
        }
    if isinstance(op, AddCaptionOp):
        return {
            "op": "add_caption",
            "text": op.text,
            "start_word_id": op.start_word_id,
            "end_word_id": op.end_word_id,
        }
    if isinstance(op, StyleOp):
        return {"op": "style", "overlay_id": op.overlay_id, "style": dict(op.style)}
    if isinstance(op, MuteOp | BleepOp):
        return {
            "op": op.op,
            "effect_id": op.effect_id,
            "start_word_id": op.start_word_id,
            "end_word_id": op.end_word_id,
        }
    if isinstance(op, RestoreOp):
        return {"op": "restore", "target_edit_id": op.target_edit_id}
    if isinstance(op, RemoveFillersOp):
        return {
            "op": "remove_fillers",
            "ranges": [range_seconds(item) for item in op.ranges],
            "word_ids": list(op.word_ids),
        }
    if isinstance(op, RemoveSilenceOp):
        return {
            "op": "remove_silence",
            "ranges": [range_seconds(item) for item in op.ranges],
            "min_gap_s": op.min_gap_s,
            "protected_after_word_ids": list(op.protected_after_word_ids),
        }
    if isinstance(op, KeepPauseOp):
        return {"op": "keep_pause", "after_word_id": op.after_word_id}
    if isinstance(op, MusicOp):
        return {
            "op": "music",
            "track_id": op.track_id,
            "duck_db": op.duck_db,
            "duck_when": op.duck_when,
        }
    raise TypeError(f"Unsupported resolved operation type: {type(op).__name__}")


def _output_spans(
    timeline: Any, transcript: Transcript
) -> dict[str, tuple[str, int | None, int | None]]:
    projection = timeline_to_output(timeline, transcript)
    return {
        display["overlay_id"]: (
            display["text"],
            display["start_out_f"],
            display["end_out_f"],
        )
        for display in projection["overlay_displays"]
    }


def _new_timeline_warnings(before: Sequence[str], after: Sequence[str]) -> list[str]:
    remaining = Counter(before)
    result = []
    for warning in after:
        if remaining[warning]:
            remaining[warning] -= 1
        else:
            result.append(warning)
    return result


def _response_timeline_warnings(
    before: Sequence[str], after: Sequence[str]
) -> tuple[list[str], list[str]]:
    display_warnings: list[str] = []
    summary_warnings: list[str] = []
    for warning in _new_timeline_warnings(before, after):
        if warning.startswith("Speed overlap:"):
            summary_warnings.append(warning)
            continue
        if "It will revive if the cut is restored." in warning:
            warning = warning.replace(
                "It will revive if the cut is restored.",
                "restore the cut to bring it back.",
            )
        display_warnings.append(warning)
    return display_warnings, summary_warnings


def _caption_summary(before: Any, after: Any, transcript: Transcript) -> list[str]:
    before_spans = _output_spans(before, transcript)
    after_spans = _output_spans(after, transcript)
    fps = after.source.fps
    lines = []
    for overlay_id, (text, start, end) in after_spans.items():
        old = before_spans.get(overlay_id)
        if start is None or end is None:
            continue
        if old is not None and old[1:] == (start, end):
            continue
        start_s = _format_time(start / fps)
        end_s = _format_time(end / fps)
        lines.append(f"Caption '{text}' now plays at {start_s}-{end_s}, still on the same words")
    return lines


def _preview_session(session: SessionManager) -> SessionManager:
    preview = SessionManager(session.state.source, session.state.transcript)
    preview.state.stack = copy.deepcopy(session.state.stack)
    preview.state.redo = copy.deepcopy(session.state.redo)
    preview.state.edit_counter = session.state.edit_counter
    preview.state.pending_question = copy.deepcopy(session.state.pending_question)
    return preview


def _time_policy(ops: Sequence[Mapping]) -> str:
    for op in ops:
        target = op.get("target")
        if isinstance(target, Mapping):
            if target.get("policy") == "original" or target.get("kind") == "overlap":
                return "original"
    return "current"


def _target_signature(intent: Mapping) -> tuple | None:
    target = intent.get("target")
    if not isinstance(target, Mapping):
        return None
    kind = target.get("kind")
    if kind is None and any(key in target for key in ("start", "end", "from_start", "from_end")):
        kind = "time"
    if kind == "time":
        policy = target.get("policy", "current")
        forms = (
            ("range", "start", "end"),
            ("from_start", "from_start"),
            ("from_end", "from_end"),
        )
        for form in forms:
            name = form[0]
            keys = form[1:]
            if all(key in target and target[key] is not None for key in keys):
                try:
                    values = tuple(_as_float(target[key], key) for key in keys)
                except _InputError:
                    return None
                return ("time", policy, name, *values)
        return None
    if kind in ("phrase", "topic"):
        selector = (
            target.get("text") if kind == "phrase" else target.get("query", target.get("text"))
        )
        if not isinstance(selector, str):
            return None
        return (
            kind,
            selector.casefold(),
            target.get("relation", "at"),
            target.get("speaker"),
        )
    return None


def _destructive_intent(intent: Mapping) -> bool:
    return intent.get("op") in ("cut", "keep_only", "speed")


def _last_active_edit(session: SessionManager) -> Edit | None:
    edits = session.state.stack
    active_ids = Resolver(
        session.get_timeline(), session.state.transcript, edits
    )._active_edit_ids()
    return next((edit for edit in reversed(edits) if edit.id in active_ids), None)


def _repeat_match(
    session: SessionManager,
    intent_ops: Sequence[Mapping],
    *,
    current_only: bool,
) -> tuple[int, Resolution] | None:
    previous = _last_active_edit(session)
    if previous is None or not previous.intent_ops:
        return None
    prior_signatures = {
        (intent.get("op"), _target_signature(intent))
        for intent in previous.intent_ops
        if _destructive_intent(intent)
    }
    resolver = Resolver(session.get_timeline(), session.state.transcript, session.state.stack)
    for index, intent in enumerate(intent_ops):
        if not _destructive_intent(intent):
            continue
        signature = _target_signature(intent)
        if signature is None or (intent.get("op"), signature) not in prior_signatures:
            continue
        if current_only and (signature[0] != "time" or signature[1] != "current"):
            continue
        try:
            resolution = _target_resolution(intent.get("target"), resolver)
        except _InputError:
            continue
        if resolution.status == "RESOLVED":
            return index, resolution
    return None


def _same_target_as_last_active_edit(session: SessionManager, intent: Mapping) -> bool:
    previous = _last_active_edit(session)
    if previous is None:
        return False
    signature = _target_signature(intent)
    return signature is not None and any(
        prior.get("op") == intent.get("op") and _target_signature(prior) == signature
        for prior in previous.intent_ops
    )


def _repeat_question(intent: Mapping, resolution: Resolution, session: SessionManager) -> str:
    target = _target_dict(intent.get("target"))
    timeline = session.get_timeline()
    duration_s = (
        sum(
            max(1, round((segment.src_out_f - segment.src_in_f) / segment.speed))
            for segment in timeline.segments
        )
        / timeline.source.fps
    )
    if target.get("from_start") is not None:
        start_s, end_s = 0.0, min(float(target["from_start"]), duration_s)
    elif target.get("from_end") is not None:
        start_s, end_s = max(0.0, duration_s - float(target["from_end"])), duration_s
    else:
        start_s = float(target["start"])
        end_s = min(float(target["end"]), duration_s)
    span = max(0.0, end_s - start_s)
    span_label = f"{span:g} second" + ("" if span == 1 else "s")
    note_prefix = "Times read as current (edited) time: "
    conversion = resolution.note.removeprefix(note_prefix).rstrip(".")
    return (
        "You already did this in the last step. Doing it again would affect "
        f"the next {span_label} (currently {conversion}). Do it again?"
    )


def _repeat_answer_choice(answer_text: str) -> bool | None:
    normalized = answer_text.strip().casefold()
    if normalized in {"yes", "again", "do it", "do it again"}:
        return True
    if normalized in {"no", "cancel", "leave it"}:
        return False
    return None


def _answer_choice(answer_text: str) -> int | str | None:
    normalized = answer_text.strip().casefold()
    if normalized in {
        "also remove the host's question",
        "also remove host question",
        "remove the host's question",
        "remove host question",
    }:
        return "remove_host_question"
    if normalized in {
        "keep the host's question",
        "keep the host question",
        "keep host question",
    }:
        return "keep_host_question"
    normalized = re.sub(r"^(?:the|number)\s+", "", normalized)
    if normalized in ("all", "both", "all of them", "every one"):
        return "all"
    normalized = re.sub(r"\s+one$", "", normalized)
    ordinals = {
        "first": 1,
        "1st": 1,
        "one": 1,
        "second": 2,
        "2nd": 2,
        "two": 2,
        "third": 3,
        "3rd": 3,
        "three": 3,
        "fourth": 4,
        "4th": 4,
        "four": 4,
        "fifth": 5,
        "5th": 5,
        "five": 5,
        "last": -1,
        "final": -1,
    }
    if normalized in ordinals:
        return "last" if ordinals[normalized] == -1 else ordinals[normalized]
    match = re.fullmatch(r"(\d+)(?:st|nd|rd|th)?(?:\s+one)?", normalized)
    return int(match.group(1)) if match else None


def _inject_choice(ops: list[dict], op_index: int, path: str, choice: int | str) -> None:
    selected_op = ops[op_index]
    if path == "host_lead_in":
        target = _target_dict(selected_op.get("target"))
        target["host_question_choice"] = choice
        selected_op["target"] = target
    elif path == "before":
        key = "before" if "before" in selected_op else "destination"
        target = dict(selected_op.get(key) or {})
        target["choice"] = choice
        selected_op[key] = target
    else:
        target = _target_dict(selected_op.get("target"))
        target["choice"] = choice
        selected_op["target"] = target


def _handle_history_operation(session: SessionManager, name: str) -> dict:
    before = session.get_timeline()
    before_warnings = list(before.warnings)
    if name == "undo":
        edit = session.undo()
        if edit is None:
            return _error_response(session, "NOTHING_TO_UNDO", "There is nothing to undo.")
    else:
        edit = session.redo_edit()
        if edit is None:
            return _error_response(session, "NOTHING_TO_REDO", "There is nothing to redo.")
    after = session.get_timeline()
    warnings, summary_warnings = _response_timeline_warnings(before_warnings, after.warnings)
    summary_parts = [
        _length_summary(before, after),
        f"{'Undid' if name == 'undo' else 'Redid'} edit {edit.id}: {edit.instruction}",
    ]
    summary_parts.extend(_caption_summary(before, after, session.state.transcript))
    summary_parts.extend(summary_warnings)
    return _response(
        session,
        "applied",
        operations=[{"op": name}],
        summary=". ".join(summary_parts) + ".",
        warnings=warnings,
        edit_id=edit.id,
    )


def handle_instruction(
    session: SessionManager,
    intent_ops: Sequence[Mapping[str, Any]],
    *,
    instruction: str = "",
    _confirmed_repeat: bool = False,
) -> dict:
    """Resolve and apply one structured instruction as exactly one Edit.

    Any ambiguity is saved in SessionState.pending_question and leaves the
    timeline unchanged. Invalid parts are reported while other valid parts
    commit together as a single undo step.
    """
    session.state.pending_question = None
    if not isinstance(intent_ops, Sequence) or isinstance(intent_ops, str | bytes):
        return _error_response(
            session, "INVALID_ARGUMENT", "Instruction operations must be a list."
        )
    if not intent_ops:
        return _error_response(
            session, "INVALID_ARGUMENT", "An instruction must contain at least one operation."
        )
    if any(not isinstance(op, Mapping) for op in intent_ops):
        return _error_response(
            session, "INVALID_ARGUMENT", "Every intent operation must be an object."
        )
    copied_ops = [dict(op) for op in intent_ops]
    if not isinstance(instruction, str):
        return _error_response(session, "INVALID_ARGUMENT", "Instruction text must be a string.")
    if len(copied_ops) == 1 and copied_ops[0].get("op") in ("undo", "redo"):
        return _handle_history_operation(session, copied_ops[0]["op"])
    if any(op.get("op") in ("undo", "redo") for op in copied_ops):
        return _error_response(
            session,
            "INVALID_ARGUMENT",
            "Undo and redo must be the only operation in an instruction.",
        )

    instruction_text = instruction.strip()
    if not instruction_text:
        instruction_text = json.dumps(copied_ops, sort_keys=True, separators=(",", ":"))
    if not _confirmed_repeat:
        repeat = _repeat_match(session, copied_ops, current_only=True)
        if repeat is not None:
            op_index, resolution = repeat
            session.state.pending_question = {
                "kind": "repeat",
                "intent_ops": copy.deepcopy(copied_ops),
                "instruction": instruction_text,
                "op_index": op_index,
                "question": _repeat_question(copied_ops[op_index], resolution, session),
                "options": ["Yes, do it again", "No, leave it"],
            }
            return _response(
                session,
                "needs_clarification",
                question=session.state.pending_question["question"],
                options=session.state.pending_question["options"],
                summary="No changes were applied; confirm whether to repeat the current-time edit.",
                time_policy="current",
            )
    if (
        not _confirmed_repeat
        and session.state.stack
        and session.state.stack[-1].instruction.casefold() == instruction_text.casefold()
    ):
        return _error_response(
            session, "NO_CHANGE", "The same instruction was already applied; no change was made."
        )

    baseline_timeline = session.get_timeline()
    baseline_warnings = list(baseline_timeline.warnings)
    preview = _preview_session(session)
    edit_id = f"e{session.edit_counter + 1}"
    resolved_ops: list[AnyOp] = []
    applied_intents: list[dict[str, Any]] = []
    resolution_notes: list[str] = []
    operation_summaries: list[str] = []
    warnings: list[str] = []
    first_error: tuple[str, str, Any] | None = None
    resolver: Resolver

    for part_index, intent in enumerate(copied_ops):
        resolver_edits = preview.state.stack
        if resolver_edits and resolver_edits[-1].id == edit_id:
            resolver_edits = resolver_edits[:-1]
        resolver = Resolver(
            preview.get_timeline(),
            preview.state.transcript,
            resolver_edits,
        )
        try:
            (
                part_ops,
                unresolved,
                ambiguous_path,
                operation_name,
                part_warnings,
                part_summary,
            ) = _resolve_part(intent, resolver, edit_id, part_index)
        except _InputError as exc:
            try:
                note = resolver_note(intent, resolver)
                if note:
                    resolution_notes.append(note)
            except _InputError:
                pass
            operation_name = intent.get("op", "operation")
            code = exc.code
            if operation_name == "style" and intent.get("target") is None:
                code = "NO_TARGET"
            message = f"{operation_name}: {exc}"
            warnings.append(f"{message} [{code}]")
            if first_error is None:
                first_error = (code, str(exc), exc.details)
            continue

        warnings.extend(part_warnings)
        if unresolved is not None:
            if unresolved.note:
                resolution_notes.append(unresolved.note)
            if unresolved.status == "AMBIGUOUS":
                question = _clarification_question(intent, unresolved)
                options = [_option_dict(option) for option in unresolved.options]
                session.state.pending_question = {
                    "intent_ops": copy.deepcopy(copied_ops),
                    "instruction": instruction_text,
                    "op_index": part_index,
                    "target_path": ambiguous_path,
                }
                return _response(
                    session,
                    "needs_clarification",
                    question=question,
                    options=options,
                    warnings=warnings,
                    summary=(
                        "No changes were applied; clarification is required. " + unresolved.note
                    ),
                    time_policy=_time_policy(copied_ops),
                )
            code, message, details = _resolution_error(unresolved, operation_name)
            conflict = _conflicting_cut(intent, resolver)
            if conflict is not None:
                if _destructive_intent(intent) and _same_target_as_last_active_edit(
                    session, intent
                ):
                    code = "NO_CHANGE"
                    message = "This target was already removed; no change was made."
                else:
                    message = (
                        f"{message} It conflicts with earlier edit {conflict[0]} "
                        f"('{conflict[1]}'), which removed this content."
                    )
                    code = "EDIT_CONFLICT"
                    details = {
                        **details,
                        "conflicting_edit_id": conflict[0],
                        "conflicting_instruction": conflict[1],
                    }
            part_message = f"{operation_name}: {message}"
            warnings.append(f"{part_message} [{code}]")
            if first_error is None:
                first_error = (code, message, details)
            continue
        note = resolver_note(intent, resolver)
        if note:
            resolution_notes.append(note)

        assert part_ops
        previous_hash = preview.get_timeline_hash()
        candidate_ops = resolved_ops + part_ops
        candidate_edit = Edit(
            id=edit_id,
            instruction=instruction_text,
            ops=candidate_ops,
            intent_ops=copy.deepcopy(applied_intents + [intent]),
        )
        preview.state.stack = copy.deepcopy(session.state.stack) + [candidate_edit]
        preview.state.edit_counter = session.edit_counter + 1
        preview.state.redo = []
        candidate_hash = preview.get_timeline_hash()
        if not preview.get_timeline().segments:
            warnings.append(
                f"{operation_name}: CUT_ALL — the video must retain at least one frame."
            )
            if first_error is None:
                first_error = (
                    "CUT_ALL",
                    "The operation would remove the entire video.",
                    {"operation": operation_name},
                )
            preview = _preview_session(session)
            if resolved_ops:
                preview.state.stack.append(
                    Edit(
                        id=edit_id,
                        instruction=instruction_text,
                        ops=copy.deepcopy(resolved_ops),
                        intent_ops=copy.deepcopy(applied_intents),
                    )
                )
                preview.state.edit_counter = session.edit_counter + 1
            continue
        if candidate_hash == previous_hash:
            if _confirmed_repeat:
                resolved_ops = candidate_ops
                applied_intents.append(copy.deepcopy(intent))
                continue
            conflict = _conflicting_cut(intent, resolver)
            repeated_target = _destructive_intent(intent) and (
                _same_target_as_last_active_edit(session, intent)
            )
            code = "NO_CHANGE" if repeated_target or conflict is None else "EDIT_CONFLICT"
            message = "This part leaves the timeline unchanged."
            details = None
            if conflict is not None and not repeated_target:
                message = (
                    f"This part conflicts with earlier edit {conflict[0]} "
                    f"('{conflict[1]}'), which already removed the target."
                )
                details = {
                    "conflicting_edit_id": conflict[0],
                    "conflicting_instruction": conflict[1],
                }
            warnings.append(f"{operation_name}: {code} — {message}")
            if first_error is None:
                first_error = (code, message, details)
            preview = _preview_session(session)
            if resolved_ops:
                preview.state.stack.append(
                    Edit(
                        id=edit_id,
                        instruction=instruction_text,
                        ops=copy.deepcopy(resolved_ops),
                        intent_ops=copy.deepcopy(applied_intents),
                    )
                )
                preview.state.edit_counter = session.edit_counter + 1
            continue
        resolved_ops = candidate_ops
        applied_intents.append(copy.deepcopy(intent))
        if part_summary:
            operation_summaries.append(part_summary)

    if not resolved_ops:
        assert first_error is not None
        code, message, details = first_error
        return _error_response(
            session,
            code,
            message,
            details=details,
            warnings=warnings,
            summary=". ".join([message, *dict.fromkeys(resolution_notes)]),
            time_policy=_time_policy(copied_ops),
        )

    committed = session.push_edit(instruction_text, resolved_ops)
    committed.intent_ops = copy.deepcopy(applied_intents)
    final_timeline = session.get_timeline()

    operation_names = ", ".join(dict.fromkeys(str(op.get("op", "operation")) for op in copied_ops))
    summary_parts = [
        _length_summary(baseline_timeline, final_timeline),
        f"Applied {operation_names}.",
    ]
    summary_parts.extend(
        _caption_summary(baseline_timeline, final_timeline, session.state.transcript)
    )
    summary_parts.extend(operation_summaries)
    new_timeline_warnings = _new_timeline_warnings(baseline_warnings, final_timeline.warnings)
    overlap_notes = [
        warning for warning in new_timeline_warnings if warning.startswith("Speed overlap:")
    ]
    summary_parts.extend(overlap_notes)
    summary_parts.extend(dict.fromkeys(resolution_notes))
    timeline_warnings, overlap_warning_summaries = _response_timeline_warnings(
        baseline_warnings, final_timeline.warnings
    )
    warnings.extend(timeline_warnings)
    summary_parts.extend(
        warning for warning in overlap_warning_summaries if warning not in overlap_notes
    )
    for op in resolved_ops:
        if isinstance(op, MuteOp | BleepOp):
            for word in session.state.transcript.words:
                if word.confidence < LOW_CONFIDENCE_THRESHOLD and _id_in_word_interval(
                    word.id, op.start_word_id, op.end_word_id, session.state.transcript
                ):
                    warnings.append(
                        f"Low confidence ({word.confidence:.2f}) on {op.op} word "
                        f"'{word.text}' ({word.id}); review before relying on this effect."
                    )
    return _response(
        session,
        "applied",
        operations=[_format_operation(op, final_timeline.source.fps) for op in resolved_ops],
        summary=". ".join(part.rstrip(".") for part in summary_parts) + ".",
        warnings=list(dict.fromkeys(warnings)),
        time_policy=_time_policy(copied_ops),
        edit_id=committed.id,
    )


def _id_in_word_interval(word_id: str, start_id: str, end_id: str, transcript: Transcript) -> bool:
    ids = [word.id for word in transcript.words]
    index = ids.index(word_id)
    start = ids.index(start_id)
    end = ids.index(end_id)
    return min(start, end) <= index <= max(start, end)


def resolver_note(intent: Mapping, resolver: Resolver) -> str:
    """Re-resolve a successful target to retain the resolver's policy note."""
    if intent.get("op") in {
        "remove_fillers",
        "remove_silence",
        "auto_caption",
        "add_music",
    }:
        return ""
    source_note = _target_resolution(intent.get("target"), resolver).note
    if intent.get("op") != "reorder":
        return source_note
    destination = _destination_resolution(intent.get("before", intent.get("destination")), resolver)
    return f"{source_note} {destination.note}"


def answer(session: SessionManager, answer_text: str) -> dict:
    """Apply the stored pending instruction after parsing an explicit choice."""
    pending = session.state.pending_question
    if pending is None:
        return _error_response(session, "NO_MATCH", "There is no pending question to answer.")
    if not isinstance(answer_text, str):
        return _error_response(
            session, "INVALID_ARGUMENT", "A clarification answer must be a string."
        )
    if pending.get("kind") == "repeat":
        choice = _repeat_answer_choice(answer_text)
        if choice is None:
            return _response(
                session,
                "needs_clarification",
                question=pending["question"],
                options=copy.deepcopy(pending["options"]),
                summary=(
                    "Please answer yes to repeat the edit or no to leave the timeline unchanged."
                ),
                time_policy="current",
            )
        session.state.pending_question = None
        if not choice:
            return _error_response(
                session,
                "NO_CHANGE",
                "The repeated edit was cancelled; no change was made.",
                summary="No changes were applied.",
                time_policy="current",
            )
        return handle_instruction(
            session,
            copy.deepcopy(pending["intent_ops"]),
            instruction=pending["instruction"],
            _confirmed_repeat=True,
        )
    choice = _answer_choice(answer_text)
    if choice is None:
        saved_ops = pending["intent_ops"]
        index = pending["op_index"]
        intent = saved_ops[index]
        target_value = (
            intent.get("before", intent.get("destination"))
            if pending["target_path"] == "before"
            else intent.get("target")
        )
        resolver = Resolver(session.get_timeline(), session.state.transcript, session.state.stack)
        if intent.get("op") == "reorder" and pending["target_path"] == "before":
            resolution = _destination_resolution(target_value, resolver)
        else:
            resolution = _target_resolution(target_value, resolver)
        return _response(
            session,
            "needs_clarification",
            question=_clarification_question(intent, resolution),
            options=[_option_dict(option) for option in resolution.options],
            summary=(
                "That answer did not identify an option. Please choose one of the listed options."
            ),
            time_policy=_time_policy(saved_ops),
        )
    saved_ops = copy.deepcopy(pending["intent_ops"])
    _inject_choice(saved_ops, pending["op_index"], pending["target_path"], choice)
    session.state.pending_question = None
    return handle_instruction(
        session,
        saved_ops,
        instruction=pending["instruction"],
    )


class InstructionHandler:
    """Convenience wrapper that binds structured instruction handling to a session."""

    def __init__(self, session: SessionManager) -> None:
        self.session = session

    def handle(self, intent_ops: Sequence[Mapping[str, Any]], instruction: str = "") -> dict:
        return handle_instruction(self.session, intent_ops, instruction=instruction)

    def answer(self, answer_text: str) -> dict:
        return answer(self.session, answer_text)
