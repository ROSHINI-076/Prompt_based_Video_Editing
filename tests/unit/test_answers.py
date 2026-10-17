"""Three-way instruction handling over the source-anchored engine."""

from __future__ import annotations

import pytest

from incubrix.engine.answers import (
    InstructionHandler,
    _clarification_question,
    _format_operation,
    answer,
    handle_instruction,
)
from incubrix.engine.models import (
    AddCaptionOp,
    CutOp,
    Edit,
    KeepOnlyOp,
    MuteOp,
    ReorderOp,
    RestoreOp,
    SourceInfo,
    SpeedOp,
    Transcript,
    Word,
    make_word_id,
)
from incubrix.engine.session import SessionManager

FPS = 30.0


def _transcript(entries: list[tuple[str, float, float, float]] | None = None) -> Transcript:
    if entries is None:
        entries = [
            ("hello", 0.0, 0.3, 0.9),
            ("welcome", 1.0, 1.3, 0.95),
            ("to", 1.4, 1.6, 0.95),
            ("the", 1.7, 1.9, 0.95),
            ("channel", 2.0, 2.4, 0.95),
            ("thanks", 3.0, 3.3, 0.9),
            ("for", 3.4, 3.6, 0.9),
            ("watching", 3.7, 4.0, 0.9),
            ("pricing", 5.0, 5.3, 0.4),
            ("is", 5.4, 5.6, 0.95),
            ("high.", 5.7, 6.0, 0.95),
        ]
    return Transcript(words=[
        Word(
            id=make_word_id(i),
            text=text,
            start_s=start,
            end_s=end,
            confidence=confidence,
        )
        for i, (text, start, end, confidence) in enumerate(entries)
    ])


def _session(duration_s: float = 10.0, transcript: Transcript | None = None) -> SessionManager:
    return SessionManager(
        SourceInfo(sha256="answer-test", duration_frames=round(duration_s * FPS), fps=FPS),
        transcript or _transcript(),
    )


def _many_thanks_session() -> SessionManager:
    transcript = _transcript([
        ("thanks", 1.0, 1.2, 0.9),
        ("a", 2.0, 2.2, 0.9),
        ("thanks", 3.0, 3.2, 0.9),
        ("b", 4.0, 4.2, 0.9),
        ("thanks", 5.0, 5.2, 0.9),
        ("c", 6.0, 6.2, 0.9),
        ("thanks", 7.0, 7.2, 0.9),
    ])
    return _session(10, transcript)


def _time(start: float, end: float, policy: str = "current") -> dict:
    return {"kind": "time", "start": start, "end": end, "policy": policy}


def _say(
    session: SessionManager,
    *ops: dict,
    instruction: str = "instruction",
) -> dict:
    return handle_instruction(session, list(ops), instruction=instruction)


def test_applied_cut_reports_lengths_policy_and_resolved_source_operation():
    session = _session()

    result = _say(
        session,
        {"op": "cut", "target": _time(0, 1)},
        instruction="cut first second",
    )

    assert result["status"] == "applied"
    assert result["error"] is None
    assert result["edit_id"] == "e1"
    assert result["duration_s"] == 9.0
    assert "Video length 0:10 -> 0:09 (-0:01)" in result["summary"]
    assert "Times read as current (edited) time" in result["summary"]
    assert result["operations"] == [
        {"op": "cut", "ranges": [{"start_s": 0.0, "end_s": 1.0}]}
    ]
    assert isinstance(session.state.stack[0].ops[0], CutOp)


def test_ambiguous_phrase_is_pending_and_answer_selects_third_occurrence():
    session = _many_thanks_session()

    question = _say(
        session,
        {"op": "cut", "target": {"kind": "phrase", "text": "thanks"}},
        instruction="remove thanks",
    )

    assert question["status"] == "needs_clarification"
    assert question["question"] == "'thanks' is mentioned 4 times at 0:01, 0:03, 0:05 and 0:07. Which one, or all?"
    assert len(question["options"]) == 5
    assert question["options"][-1]["choice"] == "all"
    assert session.state.stack == []

    applied = answer(session, "the third one")
    assert applied["status"] == "applied"
    assert applied["edit_id"] == "e1"
    assert len(session.state.stack) == 1
    assert session.state.stack[0].ops[0].ranges == [(round(4.6 * FPS), round(5.6 * FPS))]


def test_unclear_answer_reasks_and_new_instruction_clears_pending_question():
    session = _many_thanks_session()
    initial = _say(
        session,
        {"op": "cut", "target": {"kind": "phrase", "text": "thanks"}},
        instruction="ambiguous cut",
    )
    assert initial["status"] == "needs_clarification"

    reasked = answer(session, "maybe that one")
    assert reasked["status"] == "needs_clarification"
    assert session.state.stack == []

    replacement = _say(
        session,
        {"op": "cut", "target": _time(0, 0.5)},
        instruction="new cut",
    )
    assert replacement["status"] == "applied"
    assert session.state.pending_question is None


@pytest.mark.parametrize("choice", ["2", "2nd", "second", "last", "first", "all", "both"])
def test_answer_parser_accepts_numbered_and_all_choices(choice):
    session = _many_thanks_session()
    _say(session, {"op": "cut", "target": {"kind": "phrase", "text": "thanks"}})
    result = answer(session, choice)
    assert result["status"] == "applied"


def test_mult_part_applies_valid_op_and_names_rejected_part():
    session = _session()

    result = _say(
        session,
        {"op": "cut", "target": _time(0, 1)},
        {"op": "speed", "target": _time(1, 2), "factor": 10},
        instruction="cut and speed",
    )

    assert result["status"] == "applied"
    assert len(session.state.stack) == 1
    assert len(session.state.stack[0].ops) == 1
    assert any("speed" in warning and "INVALID_SPEED" in warning for warning in result["warnings"])


def test_multipart_ambiguity_does_not_apply_prior_resolved_part():
    session = _many_thanks_session()
    result = _say(
        session,
        {"op": "cut", "target": _time(0, 1)},
        {"op": "cut", "target": {"kind": "phrase", "text": "thanks"}},
        instruction="cut and choose thanks",
    )
    assert result["status"] == "needs_clarification"
    assert session.state.stack == []
    assert "cut" not in result["operations"]


def test_repeating_identical_cut_is_no_change():
    session = _session()
    _say(session, {"op": "cut", "target": _time(0, 1)}, instruction="cut intro")

    result = _say(session, {"op": "cut", "target": _time(0, 1)}, instruction="cut intro")

    assert result["status"] == "rejected"
    assert result["error"]["code"] == "NO_CHANGE"
    assert len(session.state.stack) == 1


def test_setting_same_speed_twice_is_no_change_not_an_edit():
    session = _session()
    _say(
        session,
        {"op": "speed", "target": _time(1, 2, "original"), "factor": 2},
        instruction="speed this section",
    )

    result = _say(
        session,
        {"op": "speed", "target": _time(1, 2, "original"), "factor": 2},
        instruction="set the same speed again",
    )

    assert result["status"] == "rejected"
    assert result["error"]["code"] == "NO_CHANGE"
    assert len(session.state.stack) == 1


def test_rejection_codes_and_error_shape():
    cases = [
        (_session(2), {"op": "cut", "target": _time(0, 2)}, "CUT_ALL"),
        (_session(), {"op": "speed", "target": _time(0, 1), "factor": 10}, "INVALID_SPEED"),
        (_session(2), {"op": "cut", "target": _time(3, 4)}, "OUT_OF_RANGE"),
        (_session(), {"op": "cut", "target": _time(2, 1)}, "BAD_RANGE"),
        (_session(), {"op": "cut", "target": {"kind": "phrase", "text": "absent"}}, "NO_MATCH"),
        (_session(), {"op": "style", "target": {"kind": "caption"}, "style": {"size": 20}}, "NO_TARGET"),
        (_session(), {"op": "undo"}, "NOTHING_TO_UNDO"),
        (_session(), {"op": "redo"}, "NOTHING_TO_REDO"),
    ]
    for session, op, code in cases:
        result = _say(session, op, instruction=f"reject {code}")
        assert result["status"] == "rejected", result
        assert result["error"]["code"] == code, result
        assert result["error"]["message"]
        assert "details" in result["error"]
        assert result["timeline_hash"] == session.get_timeline_hash()
        if code == "OUT_OF_RANGE":
            assert "Times read as current" in result["summary"]


def test_keep_only_speed_reorder_and_undo_redo_are_one_step():
    session = _session()
    keep = _say(
        session, {"op": "keep_only", "target": _time(1, 9)},
        instruction="keep middle",
    )
    assert keep["status"] == "applied"
    assert isinstance(session.state.stack[-1].ops[0], KeepOnlyOp)

    sped = _say(
        session, {"op": "speed", "target": _time(1, 2), "factor": 2},
        instruction="speed start",
    )
    assert sped["status"] == "applied"
    assert isinstance(session.state.stack[-1].ops[0], SpeedOp)

    reordered = _say(
        session,
        {
            "op": "reorder",
            "target": _time(2, 3),
            "before": {"kind": "start"},
        },
        instruction="reorder",
    )
    assert reordered["status"] == "applied"
    assert isinstance(session.state.stack[-1].ops[0], ReorderOp)

    undone = _say(session, {"op": "undo"}, instruction="undo")
    assert undone["status"] == "applied"
    assert undone["edit_id"] == "e3"
    assert undone["operations"] == [{"op": "undo"}]
    assert "Video length" in undone["summary"]
    redone = _say(session, {"op": "redo"}, instruction="redo")
    assert redone["status"] == "applied"
    assert redone["edit_id"] == "e3"
    assert redone["operations"] == [{"op": "redo"}]


def test_caption_style_mute_bleep_restore_and_caption_times_use_public_projection():
    session = _session()
    added = _say(
        session,
        {
            "op": "add_caption",
            "text": "Welcome!",
            "target": {"kind": "phrase", "text": "welcome to the channel"},
        },
        instruction="caption welcome",
    )
    assert added["status"] == "applied"
    assert "Caption 'Welcome!' now plays at" in added["summary"]
    assert isinstance(session.state.stack[-1].ops[0], AddCaptionOp)

    styled = _say(
        session,
        {"op": "style", "target": {"kind": "caption"}, "style": {"color": "red"}},
        instruction="style caption",
    )
    assert styled["status"] == "applied"

    muted = _say(
        session,
        {"op": "mute", "target": {"kind": "phrase", "text": "thanks for watching"}},
        instruction="mute thanks",
    )
    assert muted["status"] == "applied"
    assert isinstance(session.state.stack[-1].ops[0], MuteOp)

    bleeps = _say(
        session,
        {"op": "bleep", "target": {"kind": "phrase", "text": "pricing is high."}},
        instruction="bleep pricing",
    )
    assert bleeps["status"] == "applied"
    assert any("low confidence" in warning.casefold() for warning in bleeps["warnings"])

    cut = _say(
        session,
        {"op": "cut", "target": {"kind": "phrase", "text": "welcome to the channel"}},
        instruction="cut caption words",
    )
    assert cut["status"] == "applied"
    assert any("restore the cut to bring it back" in warning for warning in cut["warnings"])

    restore = _say(
        session,
        {"op": "restore", "target": {"kind": "edit", "edit_id": cut["edit_id"]}},
        instruction="restore caption words",
    )
    assert restore["status"] == "applied"
    recut = _say(session, {"op": "undo"}, instruction="undo restore")
    assert recut["status"] == "applied"
    assert any("restore the cut to bring it back" in warning for warning in recut["warnings"])


def test_speed_overlap_warning_only_when_existing_speed_is_overwritten():
    session = _session()
    _say(session, {"op": "speed", "target": _time(0, 3), "factor": 2}, instruction="speed 2x")
    changed = _say(
        session,
        {"op": "speed", "target": _time(1, 2), "factor": 3},
        instruction="speed overlap 3x",
    )
    assert "Speed overlap" in changed["summary"]
    assert all("Speed overlap" not in warning for warning in changed["warnings"])
    speeds = {
        (segment.src_in_f, segment.src_out_f): segment.speed
        for segment in session.get_timeline().segments
    }
    assert 3.0 in speeds.values()


def test_dormant_partial_caption_and_low_confidence_warning():
    transcript = _transcript([
        ("sensitive", 1.0, 1.3, 0.2),
        ("words", 1.4, 1.7, 0.9),
    ])
    session = _session(4, transcript)
    _say(
        session,
        {"op": "add_caption", "text": "Caption", "target": {"kind": "phrase", "text": "sensitive words"}},
        instruction="add caption",
    )
    partial = _say(
        session,
        {"op": "cut", "target": _time(1.35, 1.8)},
        instruction="cut one caption word",
    )
    assert any("now covers 1 of 2 words" in warning for warning in partial["warnings"])

    muted = _say(
        session,
        {"op": "mute", "target": {"kind": "phrase", "text": "sensitive"}},
        instruction="mute low-confidence",
    )
    assert any("low confidence" in warning.casefold() for warning in muted["warnings"])


def test_deterministic_hash_for_repeated_runs():
    hashes = []
    for _ in range(3):
        session = _session()
        _say(session, {"op": "cut", "target": _time(0, 1)}, instruction="cut first")
        _say(
            session,
            {"op": "add_caption", "text": "Welcome!", "target": {"kind": "phrase", "text": "welcome to the channel"}},
            instruction="caption welcome",
        )
        hashes.append(session.get_timeline_hash())
    assert len(set(hashes)) == 1


def test_cut_all_and_conflict_protection_leave_session_unchanged():
    session = _session(10)
    before = session.get_timeline_hash()
    result = _say(session, {"op": "cut", "target": _time(0, 10, "original")}, instruction="cut all")
    assert result["error"]["code"] == "CUT_ALL"
    assert session.get_timeline_hash() == before

    conflict_session = _session()
    _say(
        conflict_session,
        {"op": "cut", "target": {"kind": "phrase", "text": "thanks"}},
        instruction="remove thanks once",
    )
    conflict = _say(
        conflict_session,
        {"op": "cut", "target": {"kind": "phrase", "text": "thanks"}},
        instruction="remove thanks again",
    )
    assert conflict["error"]["code"] == "EDIT_CONFLICT"
    assert "e1" in conflict["error"]["message"]
    assert "remove thanks once" in conflict["error"]["message"]


def test_resolved_time_policy_original_is_reported_and_caption_only_part_has_span():
    session = _session()
    result = _say(
        session,
        {"op": "cut", "target": _time(0, 1, "original")},
        instruction="original time cut",
    )
    assert result["time_policy"] == "original"
    assert "Times read as original time" in result["summary"]


def test_target_forms_topic_and_invalid_structured_intents():
    string_target = _say(
        _session(),
        {"op": "cut", "target": "pricing is high."},
        instruction="remove the pricing sentence",
    )
    inferred_time = _say(
        _session(),
        {"op": "cut", "target": {"start": 0, "end": 1}},
        instruction="remove one second",
    )
    topic = _say(
        _session(),
        {"op": "cut", "target": {"kind": "topic", "query": "pricing"}},
        instruction="remove pricing topic",
    )
    inferred_phrase = _say(
        _session(),
        {"op": "cut", "target": {"text": "pricing is high."}},
        instruction="infer phrase target",
    )
    assert all(
        result["status"] == "applied"
        for result in (string_target, inferred_time, topic, inferred_phrase)
    )

    invalid = [
        ({"op": "cut", "target": None}, "BAD_RANGE"),
        ({"op": "cut", "target": {"kind": "mystery"}}, "BAD_RANGE"),
        ({"op": "cut", "target": {"kind": "phrase", "text": "thanks", "relation": "around"}}, "BAD_RANGE"),
        ({"op": "restore", "target": {"kind": "overlap", "start": 0}}, "BAD_RANGE"),
        ({"op": "speed", "target": _time(0, 1), "factor": True}, "BAD_RANGE"),
        ({"op": "speed", "target": _time(0, 1), "factor": "fast"}, "BAD_RANGE"),
        ({"op": "speed", "target": _time(0, 1), "factor": float("inf")}, "BAD_RANGE"),
        ({"op": "unsupported", "target": _time(0, 1)}, "BAD_RANGE"),
        ({"op": "cut", "target": {"kind": "phrase", "text": "hello", "relation": "before"}}, "NO_MATCH"),
        ({"op": "speed", "target": {"kind": "phrase", "text": "hello", "relation": "before"}, "factor": 2}, "NO_MATCH"),
        ({"op": "add_caption", "text": "", "target": {"kind": "phrase", "text": "welcome"}}, "BAD_RANGE"),
        ({"op": "style", "style": {"size": 18}}, "NO_TARGET"),
        ({"op": "reorder", "target": _time(0, 1), "before": 10}, "BAD_RANGE"),
        ({"op": "reorder", "target": _time(0, 1), "before": {"kind": "nowhere"}}, "BAD_RANGE"),
        ({"op": "reorder", "target": _time(0, 1), "before": {"kind": "phrase"}}, "BAD_RANGE"),
        ({"op": "reorder", "target": {"kind": "phrase", "text": "absent"}, "before": "end"}, "NO_MATCH"),
        ({"op": "style", "target": {"kind": "caption", "text": 7}, "style": {"color": "red"}}, "BAD_RANGE"),
    ]
    for op, code in invalid:
        response = _say(_session(), op, instruction=f"invalid {code} {op['op']}")
        assert response["status"] == "rejected"
        assert response["error"]["code"] == code

    assert _say(_session(), instruction="empty")["error"]["code"] == "BAD_RANGE"
    assert _say(_session(), 5, instruction="not a mapping")["error"]["code"] == "BAD_RANGE"
    assert _say(
        _session(),
        {"op": "undo"},
        {"op": "cut", "target": _time(0, 1)},
        instruction="mixed undo",
    )["error"]["code"] == "BAD_RANGE"
    caption_session = _session()
    _say(
        caption_session,
        {"op": "add_caption", "text": "Welcome", "target": {"kind": "phrase", "text": "welcome"}},
        instruction="add caption for style validation",
    )
    empty_style = _say(
        caption_session,
        {"op": "style", "target": {"kind": "caption"}, "style": {}},
        instruction="apply empty style",
    )
    assert empty_style["error"]["code"] == "BAD_RANGE"


def test_invalid_reorder_answer_reasks_at_destination_and_source_ambiguity():
    session = _many_thanks_session()
    question = _say(
        session,
        {
            "op": "reorder",
            "target": _time(8, 9),
            "before": {"kind": "phrase", "phrase": "thanks"},
        },
        instruction="move to some thanks",
    )
    assert question["status"] == "needs_clarification"
    assert answer(session, "not a choice")["status"] == "needs_clarification"
    assert session.state.stack == []

    source_question = _say(
        _many_thanks_session(),
        {
            "op": "reorder",
            "target": {"kind": "phrase", "text": "thanks"},
            "before": "end",
        },
        instruction="move one thanks",
    )
    assert source_question["status"] == "needs_clarification"


def test_reorder_string_phrase_and_unstructured_destination_errors():
    phrase_destination = _say(
        _session(),
        {"op": "reorder", "target": _time(6, 7), "before": "pricing"},
        instruction="move before pricing",
    )
    assert phrase_destination["status"] == "applied"

    for destination in (7, {"kind": "unknown"}):
        response = _say(
            _session(),
            {"op": "reorder", "target": _time(0, 1), "before": destination},
            instruction=f"bad destination {destination}",
        )
        assert response["error"]["code"] == "BAD_RANGE"


def test_direct_clarification_question_fallbacks_and_string_conflict_target():
    from incubrix.engine.resolver import Resolution, ResolutionOption

    no_options = Resolution(status="AMBIGUOUS", note="", detail="Select an option.", options=[])
    assert _clarification_question({"op": "cut"}, no_options) == "Select an option."
    edit_option = ResolutionOption(choice=1, edit_id="e1", snippet="remove intro")
    with_edit = Resolution(
        status="AMBIGUOUS",
        note="",
        detail="Select one.",
        options=[edit_option],
    )
    assert "e1: remove intro" in _clarification_question(
        {"op": "restore", "target": {"kind": "any"}}, with_edit
    )

    result = _say(
        _session(),
        {"op": "cut", "target": "not in this transcript"},
        instruction="cut unmatched string",
    )
    assert result["error"]["code"] == "NO_MATCH"


def test_valid_part_survives_no_change_part_in_same_instruction():
    session = _session()
    result = _say(
        session,
        {"op": "speed", "target": _time(1, 2, "original"), "factor": 2},
        {"op": "speed", "target": _time(1, 2, "original"), "factor": 2},
        instruction="apply speed once",
    )
    assert result["status"] == "applied"
    assert len(session.state.stack) == 1
    assert len(session.state.stack[0].ops) == 1
    assert any("NO_CHANGE" in warning for warning in result["warnings"])


def test_restore_does_not_select_a_cut_part_in_the_same_instruction():
    session = _session()
    result = _say(
        session,
        {"op": "cut", "target": _time(1, 2, "original")},
        {"op": "restore", "target": {"kind": "any"}},
        instruction="cut and restore any existing cut",
    )
    assert result["status"] == "applied"
    assert len(session.state.stack[-1].ops) == 1
    assert isinstance(session.state.stack[-1].ops[0], CutOp)
    assert any("restore" in warning and "NO_MATCH" in warning for warning in result["warnings"])


def test_time_targets_can_anchor_caption_and_effect_words():
    session = _session()
    caption = _say(
        session,
        {
            "op": "add_caption",
            "text": "Intro",
            "target": _time(0.9, 2.5),
        },
        instruction="caption by time",
    )
    assert caption["status"] == "applied"
    overlay = session.get_timeline().overlays[0]
    assert overlay.anchor.start_word_id == make_word_id(1)
    assert overlay.anchor.end_word_id == make_word_id(4)

    effect = _say(
        session,
        {"op": "mute", "target": _time(3, 4.5)},
        instruction="mute by time",
    )
    assert effect["status"] == "applied"
    assert isinstance(session.state.stack[-1].ops[0], MuteOp)

    no_words = _say(
        session,
        {"op": "bleep", "target": _time(8, 9)},
        instruction="bleep silence",
    )
    assert no_words["error"]["code"] == "NO_MATCH"

    no_caption_words = _say(
        _session(),
        {"op": "add_caption", "text": "Empty", "target": _time(8, 9)},
        instruction="caption silence",
    )
    assert no_caption_words["error"]["code"] == "NO_MATCH"


def test_multi_range_keep_and_reorder_reject_but_valid_parts_can_still_apply():
    session = _many_thanks_session()
    keep = _say(
        session,
        {"op": "keep_only", "target": {"kind": "phrase", "text": "thanks", "choice": "all"}},
        instruction="keep all thanks",
    )
    assert keep["error"]["code"] == "BAD_RANGE"

    reorder = _say(
        session,
        {
            "op": "reorder",
            "target": {"kind": "phrase", "text": "thanks", "choice": "all"},
            "before": "end",
        },
        instruction="reorder all thanks",
    )
    assert reorder["error"]["code"] == "BAD_RANGE"

    partial = _say(
        _session(2),
        {"op": "cut", "target": _time(0, 0.5)},
        {"op": "cut", "target": _time(0, 2)},
        instruction="cut a little, then everything",
    )
    assert partial["status"] == "applied"
    assert len(partial["operations"]) == 1
    assert any("CUT_ALL" in warning for warning in partial["warnings"])


@pytest.mark.parametrize(
    "destination",
    [
        "end",
        {"kind": "time", "time": 4},
        {"kind": "phrase", "phrase": "pricing"},
    ],
)
def test_reorder_accepts_end_time_and_phrase_destinations(destination):
    session = _session()
    result = _say(
        session,
        {"op": "reorder", "target": _time(6, 7), "before": destination},
        instruction=f"reorder to {destination}",
    )
    assert result["status"] == "applied"
    assert isinstance(session.state.stack[-1].ops[0], ReorderOp)


def test_ambiguous_reorder_destination_is_pending_and_resumed():
    session = _many_thanks_session()
    question = _say(
        session,
        {
            "op": "reorder",
            "target": _time(8, 9),
            "before": {"kind": "phrase", "phrase": "thanks"},
        },
        instruction="move to a thanks",
    )
    assert question["status"] == "needs_clarification"
    assert session.state.stack == []
    result = answer(session, "2nd")
    assert result["status"] == "applied"
    assert isinstance(session.state.stack[-1].ops[0], ReorderOp)


def test_restore_by_overlap_any_and_all_candidate_edits():
    session = _session()
    _say(session, {"op": "cut", "target": _time(0, 1, "original")}, instruction="cut first")
    restored = _say(
        session,
        {"op": "restore", "target": {"kind": "overlap", "start": 0, "end": 1}},
        instruction="restore overlap",
    )
    assert restored["status"] == "applied"
    assert isinstance(session.state.stack[-1].ops[0], RestoreOp)

    multiple = _session()
    _say(multiple, {"op": "cut", "target": _time(0, 1, "original")}, instruction="cut first")
    _say(multiple, {"op": "cut", "target": _time(2, 3, "original")}, instruction="cut second")
    question = _say(
        multiple,
        {"op": "restore", "target": {"kind": "any"}},
        instruction="restore a cut",
    )
    assert question["status"] == "needs_clarification"
    all_restored = answer(multiple, "all")
    assert all_restored["status"] == "applied"
    assert len(multiple.state.stack[-1].ops) == 2


def test_caption_time_summary_updates_after_preceding_cut():
    session = _session()
    _say(
        session,
        {"op": "add_caption", "text": "Welcome!", "target": {"kind": "phrase", "text": "welcome to the channel"}},
        instruction="add welcome caption",
    )
    result = _say(
        session,
        {"op": "cut", "target": _time(0, 1)},
        instruction="cut before caption",
    )
    assert "Caption 'Welcome!' now plays at 0:00-0:01, still on the same words" in result["summary"]


def test_handler_wrapper_pending_absent_and_operation_serializer_guard():
    session = _session()
    handler = InstructionHandler(session)
    assert handler.answer("first")["error"]["code"] == "NO_MATCH"
    assert handler.handle([{"op": "cut", "target": _time(0, 1)}])["status"] == "applied"
    with pytest.raises(TypeError, match="Unsupported resolved operation"):
        _format_operation(object(), FPS)


def test_two_occurrence_ambiguity_uses_and_separator_and_unknown_choice_reasks():
    transcript = _transcript([
        ("thanks", 1, 1.2, 0.9),
        ("pause", 2, 2.2, 0.9),
        ("thanks", 3, 3.2, 0.9),
    ])
    session = _session(5, transcript)
    question = _say(
        session,
        {"op": "cut", "target": {"kind": "phrase", "text": "thanks"}},
        instruction="cut thanks",
    )
    assert "0:01 and 0:03" in question["question"]
    assert answer(session, "invalid")["status"] == "needs_clarification"


def test_source_time_conflict_includes_edit_details_and_from_duration_forms():
    for target in (_time(1, 2, "original"), {"kind": "time", "from_start": 2, "policy": "original"},
                   {"kind": "time", "from_end": 2, "policy": "original"}):
        session = _session()
        _say(session, {"op": "cut", "target": target}, instruction="prior cut")
        conflict = _say(session, {"op": "cut", "target": target}, instruction="repeat source range")
        assert conflict["error"]["code"] == "EDIT_CONFLICT"
        assert conflict["error"]["details"]["conflicting_edit_id"] == "e1"


def test_conflict_search_skips_edits_disabled_by_restore():
    session = _session()
    _say(
        session,
        {"op": "cut", "target": {"kind": "phrase", "text": "thanks"}},
        instruction="initial thanks cut",
    )
    _say(
        session,
        {"op": "restore", "target": {"kind": "edit", "edit_id": "e1"}},
        instruction="restore initial thanks cut",
    )
    _say(
        session,
        {"op": "cut", "target": {"kind": "phrase", "text": "thanks"}},
        instruction="cut thanks again",
    )

    conflict = _say(
        session,
        {"op": "cut", "target": {"kind": "phrase", "text": "thanks"}},
        instruction="cut thanks another time",
    )
    assert conflict["error"]["code"] == "EDIT_CONFLICT"
    assert conflict["error"]["details"]["conflicting_edit_id"] == "e3"
