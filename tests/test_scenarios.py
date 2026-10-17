"""Run deterministic, structured-intent engine scenarios from tests/scenarios."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from incubrix.engine.answers import answer, handle_instruction
from incubrix.engine.models import Sentence, SourceInfo, Transcript, Word, make_word_id
from incubrix.engine.session import SessionManager
from incubrix.engine.timeline import is_word_alive, timeline_to_output
from incubrix.engine.timeparse import parse_time_expression, resolve_time_range
from tests.unit.conftest import source as source_fixture
from tests.unit.conftest import transcript as transcript_fixture

SCENARIO_DIR = Path(__file__).parent / "scenarios"
FPS = 30.0


def _synthetic_5min() -> tuple[SourceInfo, Transcript]:
    return source_fixture.__wrapped__(), transcript_fixture.__wrapped__()


def _scenario_transcript(scenario: dict[str, Any], synthetic_5min: Transcript) -> Transcript:
    if "words" not in scenario:
        if scenario["transcript"] == "synthetic_two_speaker":
            words = [
                Word(id=make_word_id(0), text="Could", start_s=1.0, end_s=1.2, speaker="S1"),
                Word(id=make_word_id(1), text="you", start_s=1.25, end_s=1.45, speaker="S1"),
                Word(id=make_word_id(2), text="explain", start_s=1.5, end_s=1.8, speaker="S1"),
                Word(id=make_word_id(3), text="pricing?", start_s=1.85, end_s=2.2, speaker="S1"),
                Word(id=make_word_id(4), text="The", start_s=2.5, end_s=2.7, speaker="S2"),
                Word(id=make_word_id(5), text="pricing", start_s=2.75, end_s=3.0, speaker="S2"),
                Word(id=make_word_id(6), text="is", start_s=3.05, end_s=3.2, speaker="S2"),
                Word(id=make_word_id(7), text="high.", start_s=3.25, end_s=3.6, speaker="S2"),
                Word(id=make_word_id(8), text="Thanks.", start_s=4.0, end_s=4.3, speaker="S1"),
            ]
            return Transcript(
                words=words,
                sentences=[
                    Sentence(first_word_idx=0, last_word_idx=3),
                    Sentence(first_word_idx=4, last_word_idx=7),
                    Sentence(first_word_idx=8, last_word_idx=8),
                ],
                speakers=["S1", "S2"],
            )
        return synthetic_5min.model_copy(deep=True)
    words = [
        Word(
            id=make_word_id(index),
            text=item["text"],
            start_s=item["start_s"],
            end_s=item["end_s"],
            speaker=item.get("speaker", "S1"),
        )
        for index, item in enumerate(scenario["words"])
    ]
    sentences = [
        Sentence(first_word_idx=first, last_word_idx=last)
        for first, last in scenario.get("sentences", [])
    ]
    return Transcript(words=words, sentences=sentences)


def _resolve_time_expressions(ops: list[dict], duration_s: float) -> list[dict]:
    """Adapt scenario-only natural-language time fixtures into ordinary intents."""
    result = copy.deepcopy(ops)
    for op in result:
        target = op.get("target")
        if not isinstance(target, dict) or "time_expression" not in target:
            continue
        parsed = parse_time_expression(target.pop("time_expression"))
        if parsed is None:
            pytest.fail("Scenario contains an unparseable time expression")
        resolved = resolve_time_range(parsed, duration_s)
        if resolved is None:
            pytest.fail("Scenario time expression did not resolve to a range")
        target.update({"kind": "time", "start": resolved[0], "end": resolved[1]})
    return result


def _apply_step(session: SessionManager, step: dict[str, Any]) -> dict[str, Any]:
    ops = _resolve_time_expressions(step["ops"], session.get_duration_frames() / FPS)
    if ops == [{"op": "render"}]:
        timeline = session.get_timeline()
        return {
            "status": "applied",
            "operations": [],
            "summary": "Render marker; timeline output inspected.",
            "warnings": [],
            "duration_s": session.get_duration_frames() / FPS,
            "timeline_hash": session.get_timeline_hash(),
            "error": None,
            "render_output": timeline_to_output(timeline, session.state.transcript),
        }
    result = handle_instruction(session, ops, instruction=step.get("instruction", "scenario step"))
    if result["status"] == "needs_clarification" and "answer" in step:
        clarification = result
        result = answer(session, step["answer"])
        result["clarification"] = clarification
    return result


def _assert_step_expectations(
    result: dict[str, Any],
    session: SessionManager,
    expect: dict[str, Any],
    prior_results: list[dict[str, Any]],
) -> None:
    if "clarification_status" in expect:
        assert result.get("clarification", {}).get("status") == expect["clarification_status"]
    assert result["status"] == expect["status"]
    if "error_code" in expect:
        assert result["error"]["code"] == expect["error_code"]
    if "duration_s" in expect:
        desired = expect["duration_s"]
        value = desired["value"] if isinstance(desired, dict) else desired
        tolerance = desired.get("tolerance", 0.01) if isinstance(desired, dict) else 0.01
        assert result["duration_s"] == pytest.approx(value, abs=tolerance)
    for text in expect.get("warnings_contain", []):
        assert any(text in warning for warning in result["warnings"])
    for text in expect.get("summary_contains", []):
        assert text.casefold() in result["summary"].casefold()
    for text in expect.get("summary_not_contains", []):
        assert text.casefold() not in result["summary"].casefold()
    clarification = result.get("clarification", result)
    for text in expect.get("question_contains", []):
        assert text.casefold() in (clarification.get("question") or "").casefold()
    for text in expect.get("options_contain", []):
        assert any(
            text.casefold() in str(option).casefold() for option in clarification.get("options", [])
        )
    if "hash_equals_step" in expect:
        assert (
            result["timeline_hash"]
            == prior_results[expect["hash_equals_step"] - 1]["timeline_hash"]
        )
    if "alive_word_ids" in expect:
        alive = {
            word.id
            for word in session.state.transcript.words
            if is_word_alive(word, session.get_timeline().segments, session.state.source.fps)
        }
        assert alive == set(expect["alive_word_ids"])
    if "protected_pause_after_word_ids" in expect:
        assert (
            session.get_timeline().protected_pause_after_word_ids
            == expect["protected_pause_after_word_ids"]
        )
    if "music_enabled" in expect:
        assert session.get_timeline().music.enabled is expect["music_enabled"]
    if "edit_count" in expect:
        assert len(session.state.stack) == expect["edit_count"]
    if "redo_count" in expect:
        assert len(session.state.redo) == expect["redo_count"]
    if "operation_ranges" in expect:
        ranges = [
            source_range
            for operation in result["operations"]
            for source_range in operation.get("ranges", [])
        ]
        assert len(ranges) == len(expect["operation_ranges"])
        for actual, desired in zip(ranges, expect["operation_ranges"]):
            assert actual["start_s"] == pytest.approx(desired[0], abs=0.05)
            assert actual["end_s"] == pytest.approx(desired[1], abs=0.05)
    if "operation_ranges_within" in expect:
        ranges = [
            source_range
            for operation in result["operations"]
            for source_range in operation.get("ranges", [])
        ]
        assert len(ranges) == len(expect["operation_ranges_within"])
        for actual, bounds in zip(ranges, expect["operation_ranges_within"]):
            assert bounds[0] <= actual["start_s"] < actual["end_s"] <= bounds[1]
    if "timeline" in expect:
        output = result.get("render_output") or timeline_to_output(
            session.get_timeline(), session.state.transcript
        )
        timeline_expect = expect["timeline"]
        if "segment_count" in timeline_expect:
            assert len(output["segments"]) == timeline_expect["segment_count"]
        if "caption_texts" in timeline_expect:
            assert [item["text"] for item in output["overlays"]] == timeline_expect["caption_texts"]
        if "caption_displays" in timeline_expect:
            for desired in timeline_expect["caption_displays"]:
                display = next(
                    item for item in output["overlay_displays"] if item["text"] == desired["text"]
                )
                assert display["start_s"] == pytest.approx(desired["start_s"], abs=0.1)
                assert display["end_s"] == pytest.approx(desired["end_s"], abs=0.1)
        if "duration_s" in timeline_expect:
            assert result["duration_s"] == pytest.approx(timeline_expect["duration_s"], abs=0.01)
    if "overlay_style" in expect:
        output = timeline_to_output(session.get_timeline(), session.state.transcript)
        overlay = next(
            item for item in output["overlays"] if item["text"] == expect["overlay_style"]["text"]
        )
        assert overlay["style"] == expect["overlay_style"]["style"]


def _run_scenario(
    scenario: dict[str, Any], source: SourceInfo, synthetic_5min: Transcript
) -> list[dict[str, Any]]:
    transcript = _scenario_transcript(scenario, synthetic_5min)
    session_source = source.model_copy(deep=True)
    if "duration_s" in scenario:
        session_source.duration_frames = round(scenario["duration_s"] * session_source.fps)
    session = SessionManager(session_source, transcript)
    results = []
    for index, step in enumerate(scenario["steps"], start=1):
        step = {
            **step,
            "instruction": step.get("instruction", f"{scenario['name']} step {index}"),
        }
        result = _apply_step(session, step)
        expect = step["expect"]
        _assert_step_expectations(result, session, expect, results)
        if "caption" in expect:
            output = timeline_to_output(session.get_timeline(), session.state.transcript)
            caption_expect = expect["caption"]
            caption = next(
                overlay
                for overlay in output["overlay_displays"]
                if (
                    overlay["text"] == caption_expect.get("text")
                    and caption_expect.get("id") in (None, overlay["overlay_id"])
                )
            )
            assert caption["start_s"] == pytest.approx(caption_expect["start_s"], abs=0.1)
            assert caption["end_s"] == pytest.approx(caption_expect["end_s"], abs=0.1)
        results.append(result)
    return results


def _scenario_files() -> list[Path]:
    return sorted(SCENARIO_DIR.glob("*.yaml"))


@pytest.mark.parametrize("scenario_path", _scenario_files(), ids=lambda path: path.stem)
def test_scenario_is_correct_and_deterministic_three_times(
    scenario_path: Path,
) -> None:
    scenario = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    assert scenario["transcript"] in {"synthetic_5min", "synthetic_two_speaker"}
    source, transcript = _synthetic_5min()
    runs = [_run_scenario(scenario, source, transcript) for _ in range(3)]
    hashes = [[step["timeline_hash"] for step in run] for run in runs]
    assert hashes[0] == hashes[1] == hashes[2]
    print(
        f"SCENARIO PASS {scenario['name']}: {len(runs[0])} steps; "
        f"three identical per-step hash sequences"
    )
