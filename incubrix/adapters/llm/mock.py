"""Deterministic, offline natural-language instruction parser."""

from __future__ import annotations

import re
import time
from typing import Any

from incubrix.adapters.base import InstructionNotUnderstoodError, Usage
from incubrix.adapters.llm.base import ParseResult
from incubrix.engine.timeparse import parse_time_expression
from incubrix.pipeline.intents import IntentOps

_QUOTED = re.compile(r"""['"]([^'"]+)['"]""")
_NUMBER_WORDS = (
    r"(?:\d+(?:\.\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty(?:\s+(?:one|two|three|four|five|six|seven|eight|nine))?|"
    r"thirty(?:\s+(?:one|two|three|four|five|six|seven|eight|nine))?|"
    r"forty(?:\s+(?:one|two|three|four|five|six|seven|eight|nine))?|"
    r"fifty|sixty|a|an)"
)
_DURATION = (
    rf"(?:a\s+minute(?:\s+(?:and\s+)?{_NUMBER_WORDS}(?:\s+seconds?)?)?"
    rf"|{_NUMBER_WORDS}\s+(?:seconds?|secs?|minutes?|mins?|hours?|hrs?))"
)


class MockLLMAdapter:
    """Parse the project instruction grammar without network or model keys."""

    provider = "mock"

    def __init__(self, model: str = "mock-rules") -> None:
        self.model = model

    def close(self) -> None:
        """The in-process mock owns no external resources."""

    def parse(self, instruction: str, context: dict[str, Any]) -> ParseResult:
        """Return schema-validated operations or a clear parse rejection."""
        started = time.perf_counter()
        ops = self._parse_ops(instruction, context)
        try:
            ops = [
                item
                for item in IntentOps.model_validate({"ops": ops}).model_dump(
                    mode="json", exclude_none=True
                )["ops"]
            ]
        except (ValueError, TypeError) as exc:
            raise InstructionNotUnderstoodError("Instruction is not understood.") from exc
        elapsed = (time.perf_counter() - started) * 1000
        return ParseResult(
            ops=ops,
            usage=Usage(provider=self.provider, model=self.model, latency_ms=elapsed),
        )

    def _parse_ops(self, text: str, context: dict[str, Any]) -> list[dict]:
        clean = text.strip()
        normalized = re.sub(r"\s+", " ", clean.casefold())
        parts = re.split(
            r"\s+and\s+(?=(?:cut|remove|keep|speed up|mute|bleep|add|put|make|turn|"
            r"restore|undo|redo|render)\b)",
            clean,
            flags=re.IGNORECASE,
        )
        if len(parts) > 1:
            return [operation for part in parts for operation in self._parse_ops(part, context)]
        if normalized in {"undo", "undo that"}:
            return [{"op": "undo"}]
        if normalized in {"redo", "redo that"}:
            return [{"op": "redo"}]
        if normalized in {"render", "render it", "render the video"}:
            return [{"op": "render"}]
        if re.search(r"\bundo the second[- ]to[- ]last cut\b", normalized):
            return [{"op": "restore", "target": {"kind": "cut_ordinal", "from_end": 2}}]

        if re.search(r"\b(?:put|bring|restore).{0,30}\bintro\b.{0,20}\bback\b", normalized):
            return [
                {
                    "op": "restore",
                    "target": {"kind": "overlap", "start": 0.0, "end": 30.0, "policy": "original"},
                }
            ]

        caption_match = re.search(
            r"\b(?:add|put|show)\s+(?:a\s+)?caption\s+['\"]([^'\"]+)['\"]\s+"
            r"(?:when|while|as)\s+(?:i\s+)?(?:say|says)\s+(.+)$",
            clean,
            re.IGNORECASE,
        )
        if caption_match:
            return [
                {
                    "op": "add_caption",
                    "text": caption_match.group(1),
                    "target": {"kind": "phrase", "text": caption_match.group(2).strip(" .!?")},
                }
            ]

        if re.search(r"\b(?:make|turn|change)\b.*\b(?:caption|that)\b.*\bred\b", normalized):
            return [{"op": "style", "target": {"kind": "caption"}, "style": {"color": "red"}}]
        if re.search(r"\bmake\b.*\b(?:caption|that|it)\b.*\bbigger\b", normalized):
            return [{"op": "style", "target": {"kind": "caption"}, "style": {"font_size": 48}}]

        if re.search(r"\b(?:add|put)\b.*\bbackground music\b|\badd music\b", normalized):
            return [{"op": "add_music", "track_id": "bundled_default"}]
        if re.search(
            r"\b(?:add|create)\s+captions?\s+(?:to|for)\s+(?:the\s+)?whole video\b",
            normalized,
        ):
            return [{"op": "auto_caption"}]
        if re.search(r"\bremove\s+(?:the\s+)?(?:ums?|uhs?|erms?|ers?|ahs?|fillers?)\b", normalized):
            return [{"op": "remove_fillers"}]
        silence = re.search(r"\bremove\s+(?:the\s+)?silences?\s+longer than\s+(.+)$", clean, re.I)
        if silence:
            duration = _duration_seconds(silence.group(1))
            if duration is not None:
                return [{"op": "remove_silence", "min_gap_s": duration}]

        effect_match = re.search(r"\b(mute|bleep)\s+(?:the\s+)?word\s+(.+)$", clean, re.I)
        if effect_match:
            return [
                {
                    "op": effect_match.group(1).casefold(),
                    "target": {"kind": "phrase", "text": effect_match.group(2).strip(" .!?")},
                }
            ]
        bleep_match = re.search(r"\bbleep\s+(.+)$", clean, re.I)
        if bleep_match:
            return [{"op": "bleep", "target": {"kind": "phrase", "text": bleep_match.group(1)}}]

        if re.search(r"\bkeep\s+(?:that|the)\s+pause\b", normalized):
            return [{"op": "keep_pause", "target": {"kind": "word", "text": "pause"}}]

        after_match = re.search(
            r"\bremove\s+everything\s+after\s+(?:i\s+)?(?:say|said)\s+(.+)$",
            clean,
            re.I,
        )
        if after_match:
            return [
                {
                    "op": "cut",
                    "target": {
                        "kind": "phrase",
                        "text": after_match.group(1).strip(" .!?"),
                        "relation": "after",
                    },
                }
            ]

        speed_match = re.search(
            r"\bspeed\s+up\s+from\s+(.+?)\s+to\s+(.+?)\s+by\s+([\d.]+)\s*x\b",
            clean,
            re.I,
        )
        if speed_match:
            start = _time_seconds(speed_match.group(1))
            end = _time_seconds(speed_match.group(2))
            if start is not None and end is not None:
                return [
                    {
                        "op": "speed",
                        "target": {"kind": "time", "start": start, "end": end, "policy": "current"},
                        "factor": float(speed_match.group(3)),
                    }
                ]

        whole_speed = re.search(
            r"\bspeed\s+up\s+(?:(?:the\s+)?whole video\s+by\s+|by\s+)?([\d.]+)\s*x\b",
            clean,
            re.I,
        )
        if whole_speed:
            return [
                {
                    "op": "speed",
                    "target": {
                        "kind": "time",
                        "start": 0.0,
                        "end": float(context.get("video_duration_s", 0.0)),
                        "policy": "current",
                    },
                    "factor": float(whole_speed.group(1)),
                }
            ]

        range_match = re.search(
            rf"\b(?:(?:from|between)\s+)?({_DURATION}|\d+(?::\d{{1,2}}){{1,2}})"
            rf"\s*(?:-|to|through|and)\s*({_DURATION}|\d+(?::\d{{1,2}}){{1,2}})\b",
            clean,
            re.I,
        )
        if range_match and re.search(r"\b(?:cut|remove|speed|keep)\b", normalized):
            start = _time_seconds(range_match.group(1))
            end = _time_seconds(range_match.group(2))
            if start is not None and end is not None:
                op = "keep_only" if re.search(r"\bkeep\b", normalized) else "cut"
                policy = "original" if "original time" in normalized else "current"
                return [
                    {
                        "op": op,
                        "target": {
                            "kind": "time",
                            "start": start,
                            "end": end,
                            "policy": policy,
                        },
                    }
                ]

        first_last = re.search(
            rf"\b(?:cut|remove)\s+(?:the\s+)?(first|last)\s+({_DURATION})", clean, re.I
        )
        if first_last:
            duration = _duration_seconds(first_last.group(2))
            if duration is not None:
                target = (
                    {"kind": "time", "from_start": duration}
                    if first_last.group(1).casefold() == "first"
                    else {"kind": "time", "from_end": duration}
                )
                if "original time" in normalized:
                    target["policy"] = "original"
                return [{"op": "cut", "target": target}]

        if (
            re.search(r"\b(?:cut|remove)\s+(?:everything|the whole video|all of it)\b", normalized)
            and "about" not in normalized
        ):
            duration = float(context.get("video_duration_s", 0.0))
            return [{"op": "cut", "target": {"kind": "time", "start": 0.0, "end": duration}}]

        topic = re.search(
            r"\b(?:remove|cut)\s+(?:the\s+)?(?:part|section|bit)\s+about\s+(.+)$", clean, re.I
        )
        if topic:
            return [
                {"op": "cut", "target": {"kind": "topic", "query": topic.group(1).strip(" .!?")}}
            ]
        topic = re.search(r"\b(?:remove|cut)\s+(?:everything\s+)?about\s+(.+)$", clean, re.I)
        if topic:
            return [
                {"op": "cut", "target": {"kind": "topic", "query": topic.group(1).strip(" .!?")}}
            ]

        phrase_cut = re.search(
            r"\b(?:cut|remove)\s+(?:the\s+)?(?:part|section|phrase)\s+['\"]([^'\"]+)['\"]",
            clean,
            re.I,
        )
        if phrase_cut:
            return [{"op": "cut", "target": {"kind": "phrase", "text": phrase_cut.group(1)}}]
        if re.search(r"\b(?:cut|remove)\s+['\"]", clean, re.I):
            quoted = _QUOTED.search(clean)
            if quoted:
                return [{"op": "cut", "target": {"kind": "phrase", "text": quoted.group(1)}}]
        raise InstructionNotUnderstoodError("Instruction is not understood.")


def _duration_seconds(text: str) -> float | None:
    parsed = parse_time_expression(text.strip().rstrip("."))
    if parsed is not None and hasattr(parsed, "seconds"):
        return float(parsed.seconds)
    return None


def _time_seconds(text: str) -> float | None:
    parsed = parse_time_expression(text.strip().rstrip(" .,"))
    if parsed is not None and hasattr(parsed, "seconds"):
        return float(parsed.seconds)
    return None
