"""Phase 2C instruction tests for the pure timeline engine."""

from __future__ import annotations

import pytest

import incubrix.engine.answers as answers_module
from incubrix.engine.answers import handle_instruction
from incubrix.engine.hashing import timeline_hash
from incubrix.engine.models import (
    MusicSettings,
    OverlayStatus,
    Sentence,
    SourceInfo,
    Transcript,
    Word,
    make_word_id,
)
from incubrix.engine.resolver import Resolver
from incubrix.engine.session import SessionManager
from incubrix.engine.timeline import initial_timeline, is_word_alive, timeline_to_output

FPS = 30.0


def _session(
    entries: list[tuple[str, float, float]],
    *,
    duration_s: float = 10.0,
    sentences: list[tuple[int, int]] | None = None,
) -> SessionManager:
    transcript = Transcript(
        words=[
            Word(id=make_word_id(i), text=text, start_s=start, end_s=end)
            for i, (text, start, end) in enumerate(entries)
        ],
        sentences=[
            Sentence(first_word_idx=first, last_word_idx=last) for first, last in (sentences or [])
        ],
    )
    source = SourceInfo(
        sha256="phase2c",
        duration_frames=round(duration_s * FPS),
        fps=FPS,
    )
    return SessionManager(source, transcript)


def _word_entries(words: list[str], *, interval: float = 0.5) -> list[tuple[str, float, float]]:
    return [(word, index * interval, index * interval + 0.3) for index, word in enumerate(words)]


def _say(session: SessionManager, op: dict) -> dict:
    return handle_instruction(session, [op], instruction=op["op"])


def _alive_word_ids(session: SessionManager) -> set[str]:
    timeline = session.get_timeline()
    return {
        word.id
        for word in session.state.transcript.words
        if is_word_alive(word, timeline.segments, FPS)
    }


@pytest.mark.parametrize(
    ("words", "expected_removed"),
    [
        (["so,", "like,", "it", "was", "fine."], {"w0002"}),
        (["I", "like", "pizza."], set()),
        (["it", "looks", "like", "rain."], set()),
        (["do", "you", "like", "it?"], set()),
        (["we", "talked", "and", "like", "left."], set()),
        (
            ["UM!", "uh?", "erm.", "er,", "ah!", "hmm."],
            {"w0001", "w0002", "w0003", "w0004", "w0005", "w0006"},
        ),
    ],
)
def test_remove_fillers_uses_the_documented_like_rule(words, expected_removed):
    session = _session(_word_entries(words))

    result = _say(session, {"op": "remove_fillers"})

    if expected_removed:
        assert result["status"] == "applied"
        assert expected_removed <= {
            word.id
            for word in session.state.transcript.words
            if word.id not in _alive_word_ids(session)
        }
    else:
        assert result["status"] == "rejected"
        assert result["error"]["code"] == "NO_MATCH"


def test_remove_fillers_removes_always_fillers_and_repeat_is_no_change():
    session = _session(_word_entries(["well", "um,", "we", "uh!", "continue."]))

    first = _say(session, {"op": "remove_fillers"})
    second = handle_instruction(
        session, [{"op": "remove_fillers"}], instruction="remove those fillers"
    )

    assert first["status"] == "applied"
    assert "2 filler words" in first["summary"]
    assert second["status"] == "rejected"
    assert second["error"]["code"] == "NO_CHANGE"


def test_consecutive_fillers_merge_to_one_snapped_cut_range():
    session = _session(_word_entries(["um", "uh", "hello."]))
    result = _say(session, {"op": "remove_fillers"})

    assert result["status"] == "applied"
    assert result["operations"][0]["word_ids"] == ["w0001", "w0002"]
    assert len(result["operations"][0]["ranges"]) == 1


def test_like_pause_rule_requires_a_non_excluded_previous_word():
    positive = _session([("really", 0.0, 0.2), ("like", 0.5, 0.7), ("that.", 0.8, 1.0)])
    removed = _say(positive, {"op": "remove_fillers"})
    assert removed["status"] == "applied"
    assert "w0002" not in _alive_word_ids(positive)

    negative = _session([("looks", 0.0, 0.2), ("like", 0.5, 0.7), ("rain.", 0.8, 1.0)])
    kept = _say(negative, {"op": "remove_fillers"})
    assert kept["status"] == "rejected"
    assert kept["error"]["code"] == "NO_MATCH"
    assert "w0002" in _alive_word_ids(negative)


@pytest.mark.parametrize(("effect", "reason"), [("bleep", "bleeped"), ("mute", "muted")])
def test_remove_fillers_keeps_and_reports_an_effected_filler(effect, reason):
    session = _session(_word_entries(["hello", "um,", "there."]))
    applied_effect = _say(
        session,
        {"op": effect, "target": {"kind": "word", "text": "um"}},
    )
    assert applied_effect["status"] == "applied"

    result = handle_instruction(session, [{"op": "remove_fillers"}], instruction="remove fillers")

    assert result["status"] == "rejected"
    assert result["error"]["code"] == "NO_CHANGE"
    assert any(f"kept 'um' at 0:00 because it is {reason}" in item for item in result["warnings"])
    assert "w0002" in _alive_word_ids(session)


def test_remove_silence_cuts_long_gaps_and_reports_no_match_when_none_remain():
    entries = [("hello", 0.0, 0.3), ("there", 1.2, 1.5), ("friend.", 2.4, 2.7)]
    session = _session(entries)

    first = _say(session, {"op": "remove_silence", "min_gap_s": 0.5})
    second = handle_instruction(
        session,
        [{"op": "remove_silence", "min_gap_s": 0.5}],
        instruction="remove silence again",
    )

    assert first["status"] == "applied"
    assert first["duration_s"] < 10.0
    assert "2 silence gaps" in first["summary"]
    assert second["status"] == "rejected"
    assert second["error"]["code"] == "NO_CHANGE"

    no_gaps = _session(_word_entries(["one", "two", "three"]))
    missing = _say(no_gaps, {"op": "remove_silence", "min_gap_s": 0.5})
    assert missing["status"] == "rejected"
    assert missing["error"]["code"] == "NO_MATCH"


def test_remove_silence_rejects_negative_threshold():
    session = _session(_word_entries(["one", "two."]))
    result = _say(session, {"op": "remove_silence", "min_gap_s": -0.1})
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "INVALID_ARGUMENT"


def test_silence_ranges_merge_across_a_speed_split_inside_the_gap():
    session = _session([("hello", 0.0, 0.3), ("there.", 2.4, 2.7)])
    speed = handle_instruction(
        session,
        [{"op": "speed", "target": {"kind": "time", "start": 1.0, "end": 1.2}, "factor": 2.0}],
        instruction="speed up part of the pause",
    )
    assert speed["status"] == "applied"

    trimmed = _say(session, {"op": "remove_silence", "min_gap_s": 0.5})

    assert trimmed["status"] == "applied"
    assert len(trimmed["operations"][0]["ranges"]) == 1


def test_protected_pause_survives_silence_removal_and_later_passes():
    session = _session([("hello", 0.0, 0.3), ("there", 1.2, 1.5), ("friend.", 2.4, 2.7)])
    protected = _say(
        session,
        {"op": "keep_pause", "target": {"kind": "word", "text": "hello"}},
    )
    assert protected["status"] == "applied"

    first_remove = _say(session, {"op": "remove_silence", "min_gap_s": 0.5})
    second_remove = handle_instruction(
        session,
        [{"op": "remove_silence", "min_gap_s": 0.1}],
        instruction="remove shorter pauses",
    )

    assert first_remove["status"] == "applied"
    assert any("Protected pause after 'hello'" in item for item in first_remove["warnings"])
    assert second_remove["status"] == "rejected"
    assert any("Protected pause after 'hello'" in item for item in second_remove["warnings"])
    assert "w0001" in session.get_timeline().protected_pause_after_word_ids
    assert session.get_timeline().segments


def test_duplicate_keep_pause_is_no_change():
    session = _session([("hello", 0.0, 0.3), ("there.", 1.2, 1.5)])
    pause = {"op": "keep_pause", "target": {"kind": "word", "text": "hello"}}
    assert _say(session, pause)["status"] == "applied"
    repeated = handle_instruction(session, [pause], instruction="keep that pause")
    assert repeated["status"] == "rejected"
    assert repeated["error"]["code"] == "NO_CHANGE"


@pytest.mark.parametrize(
    "target",
    [
        {"kind": "word", "text": "missing"},
        {"kind": "time", "start": 0, "end": 1},
    ],
)
def test_keep_pause_rejects_targets_without_a_resolved_word(target):
    session = _session(_word_entries(["hello", "there."]))
    result = _say(session, {"op": "keep_pause", "target": target})
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "NO_MATCH"


def test_auto_caption_splits_at_max_words_and_one_undo_removes_all():
    session = _session(
        _word_entries(["One", "two", "three", "four", "five."]),
        sentences=[(0, 4)],
    )

    result = _say(session, {"op": "auto_caption", "max_words": 2})
    timeline = session.get_timeline()
    projection = timeline_to_output(timeline, session.state.transcript)

    assert result["status"] == "applied"
    assert len(timeline.overlays) == 3
    assert all(
        overlay.anchor.start_word_id and overlay.anchor.end_word_id for overlay in timeline.overlays
    )
    assert len(projection["overlay_displays"]) == 3
    undo = handle_instruction(session, [{"op": "undo"}], instruction="undo")
    assert undo["status"] == "applied"
    assert session.get_timeline().overlays == []


def test_auto_caption_becomes_dormant_when_its_words_are_cut():
    session = _session(_word_entries(["One", "two."]), sentences=[(0, 1)])
    created = _say(session, {"op": "auto_caption"})
    assert created["status"] == "applied"

    cut = handle_instruction(
        session,
        [{"op": "cut", "target": {"kind": "time", "start": 0.0, "end": 1.0}}],
        instruction="cut captioned words",
    )

    assert cut["status"] == "applied"
    assert len(session.get_timeline().overlays) == 1
    assert session.get_timeline().overlays[0].status == OverlayStatus.DORMANT


def test_auto_caption_same_active_configuration_is_no_change():
    session = _session(_word_entries(["One", "two", "three."]), sentences=[(0, 2)])
    assert _say(session, {"op": "auto_caption"})["status"] == "applied"
    repeated = handle_instruction(
        session, [{"op": "auto_caption"}], instruction="caption the transcript"
    )
    assert repeated["status"] == "rejected"
    assert repeated["error"]["code"] == "NO_CHANGE"


def test_auto_caption_allows_reapplying_after_its_edit_is_restored():
    session = _session(_word_entries(["one", "two."]), sentences=[(0, 1)])
    created = _say(session, {"op": "auto_caption"})
    restored = handle_instruction(
        session,
        [{"op": "restore", "target": {"kind": "edit", "edit_id": created["edit_id"]}}],
        instruction="restore auto-caption edit",
    )

    assert restored["status"] == "applied"
    assert session.get_timeline().overlays == []
    reapplied = handle_instruction(session, [{"op": "auto_caption"}], instruction="caption again")
    assert reapplied["status"] == "applied"


@pytest.mark.parametrize(
    ("entries", "max_words"),
    [
        (_word_entries(["one."]), 0),
        ([("out of range.", 20.0, 20.5)], 8),
    ],
)
def test_auto_caption_rejects_invalid_or_unavailable_words(entries, max_words):
    session = _session(entries)
    result = _say(session, {"op": "auto_caption", "max_words": max_words})
    assert result["status"] == "rejected"
    assert result["error"]["code"] == ("INVALID_ARGUMENT" if max_words == 0 else "NO_MATCH")


def test_music_conflict_replacement_and_restore(monkeypatch):
    monkeypatch.setattr(
        answers_module, "ALLOWED_MUSIC_TRACK_IDS", frozenset({"bundled_default", "alt"})
    )
    session = _session(_word_entries(["one", "two."]))

    first = _say(
        session,
        {"op": "add_music", "track_id": "bundled_default", "duck_db": -16, "duck_when": "speech"},
    )
    assert first["status"] == "applied"
    assert session.get_timeline().music.track_id == "bundled_default"
    assert session.get_timeline().music.duck_db == -16
    assert session.get_timeline_output()["music"]["duck_when"] == "speech"

    conflict = handle_instruction(
        session,
        [{"op": "add_music", "track_id": "alt"}],
        instruction="add another music track",
    )
    assert conflict["status"] == "rejected"
    assert conflict["error"]["code"] == "EDIT_CONFLICT"
    assert "e1" in conflict["error"]["message"]
    repeated_conflict = handle_instruction(
        session,
        [{"op": "add_music", "track_id": "alt"}],
        instruction="try another music track",
    )
    assert repeated_conflict["status"] == "rejected"
    assert repeated_conflict["error"]["code"] == "EDIT_CONFLICT"

    replacement = handle_instruction(
        session,
        [{"op": "add_music", "track_id": "alt", "replace": True}],
        instruction="replace the music",
    )
    assert replacement["status"] == "applied"
    assert session.get_timeline().music.track_id == "alt"
    restored = handle_instruction(
        session,
        [{"op": "restore", "target": {"kind": "edit", "edit_id": replacement["edit_id"]}}],
        instruction="restore previous music",
    )
    assert restored["status"] == "applied"
    assert session.get_timeline().music.track_id == "bundled_default"
    assert (
        handle_instruction(session, [{"op": "undo"}], instruction="undo restore")["status"]
        == "applied"
    )
    assert session.get_timeline().music.track_id == "alt"
    assert (
        handle_instruction(session, [{"op": "redo"}], instruction="redo restore")["status"]
        == "applied"
    )
    assert session.get_timeline().music.track_id == "bundled_default"


def test_music_unknown_track_is_no_target_on_each_attempt():
    session = _session(_word_entries(["one", "two."]))
    for instruction in ("add unknown music", "try unknown music again"):
        result = handle_instruction(
            session,
            [{"op": "add_music", "track_id": "not_configured"}],
            instruction=instruction,
        )
        assert result["status"] == "rejected"
        assert result["error"]["code"] == "NO_TARGET"


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"duck_when": "always"}, "INVALID_ARGUMENT"),
        ({"replace": "yes"}, "INVALID_ARGUMENT"),
    ],
)
def test_music_validates_ducking_and_replace_intents(fields, code):
    session = _session(_word_entries(["one."]))
    result = _say(session, {"op": "add_music", "track_id": "bundled_default", **fields})
    assert result["status"] == "rejected"
    assert result["error"]["code"] == code


def test_music_conflict_without_a_history_edit_still_names_the_current_track():
    session = _session(_word_entries(["one."]))
    timeline = initial_timeline(session.state.source)
    timeline.music = MusicSettings(track_id="bundled_default", enabled=True)
    resolver = Resolver(timeline, session.state.transcript)

    assert answers_module._active_music_edit(resolver) is None


@pytest.mark.parametrize(
    ("words", "intent"),
    [
        (["one", "um,", "three."], {"op": "remove_fillers"}),
        (
            ["one", "two."],
            {"op": "remove_silence", "min_gap_s": 0.5},
        ),
        (["one", "two."], {"op": "keep_pause", "target": {"kind": "word", "text": "one"}}),
        (["one", "two."], {"op": "auto_caption"}),
        (["one", "two."], {"op": "add_music", "track_id": "bundled_default"}),
    ],
)
def test_phase2c_operations_are_deterministic_on_three_runs(words, intent):
    entries = [("one", 0.0, 0.3), ("um,", 1.0, 1.3), ("three.", 2.0, 2.3)]
    if intent["op"] != "remove_fillers":
        entries = _word_entries(words, interval=1.0)
    hashes = []
    for _ in range(3):
        session = _session(entries, sentences=[(0, len(entries) - 1)])
        result = _say(session, intent)
        assert result["status"] == "applied"
        hashes.append(timeline_hash(session.get_timeline()))
    assert len(set(hashes)) == 1


def test_session_history_reports_empty_stacks():
    session = _session(_word_entries(["one."]))
    assert session.get_history() == {"stack": [], "redo": [], "edit_counter": 0}
