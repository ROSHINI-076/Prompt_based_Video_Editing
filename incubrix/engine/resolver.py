"""Pure resolution of edit targets to source-anchored engine results."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Literal

from incubrix.engine.models import (
    CutOp,
    Edit,
    KeepOnlyOp,
    RestoreOp,
    Segment,
    Timeline,
    Transcript,
    Word,
)
from incubrix.engine.ops import snap_to_silence
from incubrix.engine.timeline import (
    Layout,
    is_word_alive,
    timeline_to_output,
    word_midpoint_frame,
)

ResolutionStatus = Literal["RESOLVED", "AMBIGUOUS", "NO_MATCH", "OUT_OF_RANGE", "BAD_RANGE"]
TimePolicy = Literal["current", "original"]


@dataclass(frozen=True)
class TimeTarget:
    """A time range, in seconds, in current or original time."""

    start: float | None = None
    end: float | None = None
    from_start: float | None = None
    from_end: float | None = None
    policy: TimePolicy = "current"


@dataclass(frozen=True)
class PhraseTarget:
    """A transcript phrase and its relation to the selected occurrence."""

    text: str
    relation: Literal["at", "before", "after"] = "at"
    speaker: str | None = None
    choice: int | str | None = None


@dataclass(frozen=True)
class TopicTarget:
    """A semantic/keyword topic query, optionally constrained to one speaker."""

    query: str
    speaker: str | None = None
    choice: int | str | None = None


@dataclass(frozen=True)
class CaptionTarget:
    """Select a caption overlay by optional exact/partial display text."""

    text: str | None = None
    choice: int | str | None = None


@dataclass(frozen=True)
class RestoreTarget:
    """Select an active cut/keep edit by ID, original-time overlap, or any."""

    edit_id: str | None = None
    overlap: tuple[float, float] | None = None
    any: bool = False
    choice: int | str | None = None


@dataclass(frozen=True)
class ReorderDestination:
    """Resolve a reorder insertion point from start/end/current time/a phrase."""

    kind: Literal["start", "end", "time", "phrase"]
    time_s: float | None = None
    phrase: str | None = None
    speaker: str | None = None
    choice: int | str | None = None


@dataclass(frozen=True)
class ResolutionOption:
    """One explicit choice for an ambiguous target."""

    choice: int | str
    current_start_s: float | None = None
    current_end_s: float | None = None
    snippet: str = ""
    source_ranges: tuple[tuple[int, int], ...] = ()
    word_ids: tuple[str, ...] = ()
    edit_id: str | None = None
    overlay_id: str | None = None


@dataclass
class Resolution:
    """Source-anchored result for the next engine/response-layer step."""

    status: ResolutionStatus
    note: str
    source_ranges: list[tuple[int, int]] = field(default_factory=list)
    word_ids: list[str] = field(default_factory=list)
    edit_ids: list[str] = field(default_factory=list)
    overlay_ids: list[str] = field(default_factory=list)
    destination_frame: int | None = None
    options: list[ResolutionOption] = field(default_factory=list)
    error_code: str | None = None
    detail: str | None = None
    clamped: bool = False


@dataclass(frozen=True)
class _Match:
    indices: tuple[int, ...]
    current_start_f: int
    current_end_f: int
    snippet: str


_SYNONYMS: dict[str, set[str]] = {
    "pricing": {"pricing", "price", "prices", "cost", "costs", "fee", "fees"},
    "price": {"pricing", "price", "prices", "cost", "costs", "fee", "fees"},
    "cost": {"pricing", "price", "prices", "cost", "costs", "fee", "fees"},
    "fee": {"pricing", "price", "prices", "cost", "costs", "fee", "fees"},
}
_TOKEN_RE = re.compile(r"[a-z0-9']+")
_PAUSE_SENTENCE_GAP_S = 1.0


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.casefold())


def _format_time(seconds: float) -> str:
    """Format seconds as a stable minute/second label."""
    total = max(0, round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _format_ranges(ranges: list[tuple[int, int]], fps: float) -> str:
    if not ranges:
        return "no surviving source interval"
    return ", ".join(
        f"{_format_time(start / fps)}-{_format_time(end / fps)}"
        for start, end in ranges
    )


def _choice_index(choice: int | str | None, count: int) -> int | None:
    if isinstance(choice, int):
        return choice - 1 if 1 <= choice <= count else None
    if choice is None:
        return None
    normalized = choice.strip().casefold()
    if normalized in {"first", "1", "one"}:
        return 0
    if normalized in {"second", "2", "two"}:
        return 1 if count > 1 else None
    if normalized in {"third", "3", "three"}:
        return 2 if count > 2 else None
    if normalized in {"last", "final"}:
        return count - 1 if count else None
    if normalized.isdigit():
        return _choice_index(int(normalized), count)
    return None


class Resolver:
    """Resolve interpreted targets using only the current source, transcript and timeline.

    All public positions returned by this class are source frames or stable word/edit/
    overlay identifiers. Current-time values appear only in notes and clarification options.
    """

    def __init__(
        self,
        timeline: Timeline,
        transcript: Transcript,
        edits: list[Edit] | None = None,
        embedder: Callable[[str, str], float] | None = None,
        embedder_threshold: float = 0.5,
    ) -> None:
        self.timeline = timeline
        self.transcript = transcript
        self.edits = list(edits or [])
        self.layout = Layout(timeline.segments)
        self.fps = timeline.source.fps
        self.embedder = embedder
        self.embedder_threshold = embedder_threshold

    @property
    def _source_duration_f(self) -> int:
        return self.timeline.source.duration_frames

    @property
    def _current_duration_f(self) -> int:
        return self.layout.total_frames

    def _note(
        self,
        policy: TimePolicy,
        label: str,
        source_ranges: list[tuple[int, int]] | None = None,
    ) -> str:
        if source_ranges is None:
            return f"Times read as {policy} time: {label}."
        original = _format_ranges(source_ranges, self.fps)
        if policy == "current":
            return f"Times read as current (edited) time: {label} = {original} in the original."
        return f"Times read as original time: {label} = {original} in the original."

    def _failure(
        self, code: Literal["NO_MATCH", "OUT_OF_RANGE", "BAD_RANGE"], detail: str, policy: TimePolicy
    ) -> Resolution:
        return Resolution(
            status=code,
            error_code=code,
            detail=detail,
            note=self._note(policy, detail),
        )

    def _source_ranges_for_current(
        self, start_f: int, end_f: int
    ) -> list[tuple[int, int]]:
        return self.layout.out_range_to_src_ranges(start_f, end_f)

    def _resolve_time_bounds(self, target: TimeTarget) -> tuple[int, int, bool] | Resolution:
        policy = target.policy
        if policy not in ("current", "original"):
            return self._failure("BAD_RANGE", f"Unknown time policy: {policy}", "current")
        duration_f = (
            self._current_duration_f if policy == "current" else self._source_duration_f
        )
        duration_s = duration_f / self.fps
        values = (target.start, target.end, target.from_start, target.from_end)
        if sum(value is not None for value in values) not in (1, 2):
            return self._failure(
                "BAD_RANGE", "Specify one range form: start/end, from_start, or from_end.", policy
            )
        if any(value is not None and value < 0 for value in values):
            return self._failure("BAD_RANGE", "Time ranges cannot be negative.", policy)
        if target.from_start is not None and any(
            value is not None for value in (target.start, target.end, target.from_end)
        ):
            return self._failure("BAD_RANGE", "from_start cannot be combined with other bounds.", policy)
        if target.from_end is not None and any(
            value is not None for value in (target.start, target.end, target.from_start)
        ):
            return self._failure("BAD_RANGE", "from_end cannot be combined with other bounds.", policy)

        if target.from_start is not None:
            start_s, end_s = 0.0, target.from_start
        elif target.from_end is not None:
            start_s, end_s = duration_s - target.from_end, duration_s
        else:
            start_s = target.start
            end_s = target.end
            if start_s is None or end_s is None:
                return self._failure("BAD_RANGE", "Both start and end are required.", policy)

        assert start_s is not None and end_s is not None
        if start_s < 0 or end_s < 0:
            return self._failure("BAD_RANGE", "Time ranges cannot be negative.", policy)
        if start_s > duration_s:
            return self._failure("OUT_OF_RANGE", "Range start is beyond the video.", policy)
        if end_s <= start_s:
            return self._failure("BAD_RANGE", "Range end must be after its start.", policy)

        clamped = end_s > duration_s
        end_s = min(end_s, duration_s)
        if end_s <= start_s:
            return self._failure("BAD_RANGE", "Range is empty after clamping.", policy)
        return round(start_s * self.fps), round(end_s * self.fps), clamped

    def resolve_time(self, target: TimeTarget) -> Resolution:
        """Resolve a time target to one or more source-frame intervals."""
        bounds = self._resolve_time_bounds(target)
        if isinstance(bounds, Resolution):
            return bounds
        start_f, end_f, clamped = bounds
        if target.policy == "current":
            ranges = self._source_ranges_for_current(start_f, end_f)
        else:
            ranges = [(start_f, end_f)]
        if not ranges:
            return self._failure("NO_MATCH", "The requested time range has no surviving video.", target.policy)

        start_s = start_f / self.fps
        end_s = end_f / self.fps
        label = f"{_format_time(start_s)}-{_format_time(end_s)}"
        note = self._note(target.policy, label, ranges)
        if clamped:
            note += " End time exceeded the video and was clamped to its end."
        return Resolution(
            status="RESOLVED",
            source_ranges=ranges,
            note=note,
            detail="End time clamped to video duration." if clamped else None,
            clamped=clamped,
        )

    def _is_alive(self, index: int) -> bool:
        return is_word_alive(self.transcript.words[index], self.timeline.segments, self.fps)

    def _phrase_matches(self, phrase: str, speaker: str | None) -> list[_Match]:
        return self._find_phrase_matches(
            phrase, speaker, self.timeline.segments, self.layout, alive_only=True
        )

    def _find_phrase_matches(
        self,
        phrase: str,
        speaker: str | None,
        segments: list[Segment],
        layout: Layout,
        alive_only: bool,
    ) -> list[_Match]:
        query = _tokens(phrase)
        words = self.transcript.words
        if not query:
            return []
        transcript_tokens = [_tokens(word.text) for word in words]
        matches: list[_Match] = []
        for start in range(0, len(words) - len(query) + 1):
            indices = tuple(range(start, start + len(query)))
            if speaker is not None and any(words[i].speaker != speaker for i in indices):
                continue
            if alive_only and any(
                not is_word_alive(words[i], segments, self.fps) for i in indices
            ):
                continue
            flat_tokens = [token for i in indices for token in transcript_tokens[i]]
            if flat_tokens != query:
                continue
            midpoint_out = [
                layout.src_to_out(word_midpoint_frame(words[i], self.fps))
                for i in indices
            ]
            mapped = [frame for frame in midpoint_out if frame is not None]
            if not mapped:
                continue
            matches.append(
                _Match(
                    indices=indices,
                    current_start_f=min(mapped),
                    current_end_f=max(mapped) + 1,
                    snippet=" ".join(words[i].text for i in indices),
                )
            )
        return matches

    def _snapped_match_range(self, match: _Match) -> tuple[int, int]:
        return snap_to_silence(
            match.indices[0], match.indices[-1], self.transcript, self.fps
        )

    def _match_option(self, choice: int, match: _Match) -> ResolutionOption:
        start_s = match.current_start_f / self.fps
        end_s = match.current_end_f / self.fps
        src_ranges = [self._snapped_match_range(match)]
        return ResolutionOption(
            choice=choice,
            current_start_s=round(start_s, 3),
            current_end_s=round(end_s, 3),
            snippet=match.snippet,
            source_ranges=tuple(src_ranges),
            word_ids=tuple(self.transcript.words[i].id for i in match.indices),
        )

    def _ambiguous_note(
        self, label: str, options: list[ResolutionOption], policy: TimePolicy = "current"
    ) -> str:
        conversions: list[str] = []
        for option in options:
            if option.current_start_s is None or option.current_end_s is None:
                continue
            current = (
                f"{_format_time(option.current_start_s)}-"
                f"{_format_time(option.current_end_s)}"
            )
            original = _format_ranges(list(option.source_ranges), self.fps)
            conversions.append(f"{current} = {original} original")
        if not conversions:
            if policy == "original":
                originals = [
                    f"{option.edit_id}: {_format_ranges(list(option.source_ranges), self.fps)}"
                    for option in options
                    if option.edit_id is not None
                ]
                if originals:
                    return (
                        f"Times read as original time: {label}; "
                        + "; ".join(originals)
                        + "."
                    )
            return self._note(policy, label)
        if policy == "current":
            return (
                f"Times read as current (edited) time: {label}; "
                + "; ".join(conversions)
                + "."
            )
        return f"Times read as original time: {label}; " + "; ".join(conversions) + "."

    def _choose_match(
        self,
        matches: list[_Match],
        choice: int | str | None,
    ) -> tuple[_Match | None, list[ResolutionOption], bool]:
        options = [self._match_option(i + 1, match) for i, match in enumerate(matches)]
        if len(matches) <= 1:
            return (matches[0] if matches else None), options, False
        if choice is None:
            return None, options, False
        if isinstance(choice, str) and choice.strip().casefold() == "all":
            return None, options, True
        index = _choice_index(choice, len(matches))
        return (matches[index] if index is not None else None), options, False

    def _relation_ranges(self, match: _Match, relation: str) -> list[tuple[int, int]]:
        snap_start, snap_end = self._snapped_match_range(match)
        if relation == "at":
            return [(snap_start, snap_end)]
        if relation == "after":
            boundary_out = self.layout.src_to_out(snap_end)
            if boundary_out is None:
                boundary_out = match.current_end_f
            return self.layout.out_range_to_src_ranges(boundary_out, self._current_duration_f)
        boundary_out = self.layout.src_to_out(snap_start)
        if boundary_out is None:
            boundary_out = match.current_start_f
        return self.layout.out_range_to_src_ranges(0, boundary_out)

    def resolve_phrase(self, target: PhraseTarget) -> Resolution:
        """Resolve a phrase to snapped source ranges, requiring an explicit repeated-match choice."""
        matches = self._phrase_matches(target.text, target.speaker)
        if not matches:
            dead = self._phrase_matches_all_words(target.text, target.speaker, alive_only=False)
            detail = (
                f"'{target.text}' occurs only in cut regions."
                if dead else f"No alive transcript match for '{target.text}'."
            )
            return self._failure("NO_MATCH", detail, "current")

        selected, options, all_matches = self._choose_match(
            matches, target.choice
        )
        if selected is None and not all_matches:
            return Resolution(
                status="AMBIGUOUS",
                options=options + [ResolutionOption(choice="all", snippet="All matches")],
                note=self._ambiguous_note(
                    f"'{target.text}' has {len(matches)} alive matches", options
                ),
                detail="Choose a numbered match or all.",
            )
        selected_matches = matches if all_matches else [selected]
        ranges: list[tuple[int, int]] = []
        word_ids: list[str] = []
        for match in selected_matches:
            ranges.extend(self._relation_ranges(match, target.relation))
            word_ids.extend(self.transcript.words[i].id for i in match.indices)
        label = f"'{target.text}' ({target.relation})"
        return Resolution(
            status="RESOLVED",
            source_ranges=ranges,
            word_ids=word_ids,
            note=self._note("current", label, ranges),
        )

    def _phrase_matches_all_words(
        self, phrase: str, speaker: str | None, alive_only: bool
    ) -> list[_Match]:
        if alive_only:
            return self._phrase_matches(phrase, speaker)
        full_segments = [Segment(src_in_f=0, src_out_f=self._source_duration_f)]
        return self._find_phrase_matches(
            phrase,
            speaker,
            full_segments,
            Layout(full_segments),
            alive_only=False,
        )

    def _sentences(self) -> list[tuple[int, int]]:
        words = self.transcript.words
        if self.transcript.sentences:
            return [
                (max(0, sent.first_word_idx), min(len(words) - 1, sent.last_word_idx))
                for sent in self.transcript.sentences
                if words and sent.first_word_idx < len(words) and sent.last_word_idx >= 0
            ]
        if not words:
            return []
        result: list[tuple[int, int]] = []
        start = 0
        for index, word in enumerate(words):
            punctuation_end = bool(re.search(r"[.!?][\"')\]]?$", word.text))
            pause_end = (
                index + 1 < len(words)
                and words[index + 1].start_s - word.end_s >= _PAUSE_SENTENCE_GAP_S
            )
            if punctuation_end or pause_end:
                result.append((start, index))
                start = index + 1
        if start < len(words):
            result.append((start, len(words) - 1))
        return result

    def _topic_terms(self, query: str) -> set[str]:
        terms: set[str] = set()
        for token in _tokens(query):
            terms.update(_SYNONYMS.get(token, {token}))
        return terms

    def resolve_topic(self, target: TopicTarget) -> Resolution:
        """Find keyword/synonym or embedder-matched transcript sentences."""
        terms = self._topic_terms(target.query)
        matches: list[_Match] = []
        dead_match = False
        for first, last in self._sentences():
            all_indices = tuple(range(first, last + 1))
            if target.speaker is not None:
                all_indices = tuple(
                    i for i in all_indices if self.transcript.words[i].speaker == target.speaker
                )
            if not all_indices:
                continue
            sentence_text = " ".join(self.transcript.words[i].text for i in all_indices)
            keyword_match = bool(terms.intersection(_tokens(sentence_text)))
            if not keyword_match and self.embedder is None:
                continue
            alive_indices = tuple(i for i in all_indices if self._is_alive(i))
            if not alive_indices:
                dead_match = dead_match or keyword_match or (
                    self.embedder is not None
                    and self.embedder(target.query, sentence_text) >= self.embedder_threshold
                )
                continue
            alive_text = " ".join(self.transcript.words[i].text for i in alive_indices)
            alive_keyword_match = bool(terms.intersection(_tokens(alive_text)))
            alive_semantic_match = (
                self.embedder is not None
                and self.embedder(target.query, alive_text) >= self.embedder_threshold
            )
            if not alive_keyword_match and not alive_semantic_match:
                dead_match = dead_match or keyword_match
                continue
            output_frames = [
                self.layout.src_to_out(
                    word_midpoint_frame(self.transcript.words[i], self.fps)
                )
                for i in alive_indices
            ]
            output_frames = [frame for frame in output_frames if frame is not None]
            if not output_frames:
                dead_match = True
                continue
            matches.append(
                _Match(
                    indices=alive_indices,
                    current_start_f=min(output_frames),
                    current_end_f=max(output_frames) + 1,
                    snippet=" ".join(self.transcript.words[i].text for i in alive_indices)[:160],
                )
            )
        if not matches:
            detail = (
                f"Matches for '{target.query}' exist only in cut regions."
                if dead_match else f"No sentence matches '{target.query}'."
            )
            return self._failure("NO_MATCH", detail, "current")

        selected, options, all_matches = self._choose_match(matches, target.choice)
        if selected is None and not all_matches:
            return Resolution(
                status="AMBIGUOUS",
                options=options + [ResolutionOption(choice="all", snippet="All matches")],
                note=self._ambiguous_note(
                    f"'{target.query}' matches {len(matches)} sentences", options
                ),
                detail="Choose a numbered sentence or all.",
            )
        chosen = matches if all_matches else [selected]
        ranges: list[tuple[int, int]] = []
        word_ids: list[str] = []
        for match in chosen:
            ranges.append(self._snapped_match_range(match))
            word_ids.extend(self.transcript.words[i].id for i in match.indices)
        return Resolution(
            status="RESOLVED",
            source_ranges=ranges,
            word_ids=word_ids,
            options=options,
            note=self._note("current", f"topic '{target.query}'", ranges),
        )

    def resolve_caption(self, target: CaptionTarget) -> Resolution:
        """Select existing captions, using only canonical computed display times."""
        output = timeline_to_output(self.timeline, self.transcript)
        displays = output["overlay_displays"]
        overlays = {overlay["id"]: overlay for overlay in output["overlays"]}
        if target.text is not None:
            needle = target.text.casefold()
            displays = [
                display
                for display in displays
                if needle in overlays.get(display["overlay_id"], {}).get("text", "").casefold()
            ]
        if not displays:
            return self._failure("NO_MATCH", "No matching caption overlay.", "current")
        display_ranges_by_overlay: dict[str, list[tuple[int, int]]] = {}
        options: list[ResolutionOption] = []
        for i, display in enumerate(displays):
            ranges = (
                self.layout.out_range_to_src_ranges(
                    display["start_out_f"], display["end_out_f"]
                )
                if display["start_out_f"] is not None
                and display["end_out_f"] is not None
                else []
            )
            display_ranges_by_overlay[display["overlay_id"]] = ranges
            options.append(
                ResolutionOption(
                    choice=i + 1,
                    current_start_s=display["start_s"],
                    current_end_s=display["end_s"],
                    snippet=display["text"],
                    source_ranges=tuple(ranges),
                    overlay_id=display["overlay_id"],
                )
            )
        display_ranges = list(display_ranges_by_overlay.values())
        original_ranges = [rng for group in display_ranges for rng in group]
        note = self._note("current", "caption display times", original_ranges)
        if len(options) > 1 and target.choice is None:
            caption_note = self._ambiguous_note(
                "caption display times", options
            )
            return Resolution(
                status="AMBIGUOUS",
                options=options + [ResolutionOption(choice="all", snippet="All captions")],
                note=caption_note,
                detail="Choose a numbered caption or all.",
            )
        if len(options) > 1 and isinstance(target.choice, str) and (
            target.choice.strip().casefold() == "all"
        ):
            chosen = options
        elif len(options) > 1:
            index = _choice_index(target.choice, len(options))
            if index is None:
                return Resolution(
                    status="AMBIGUOUS",
                    options=options + [ResolutionOption(choice="all", snippet="All captions")],
                    note=self._ambiguous_note("caption display times", options),
                    detail="Choose a valid numbered caption or all.",
                )
            chosen = [options[index]]
        else:
            chosen = options
        selected_ranges = [
            source_range
            for option in chosen
            for source_range in option.source_ranges
        ]
        return Resolution(
            status="RESOLVED",
            overlay_ids=[option.overlay_id for option in chosen if option.overlay_id],
            options=chosen,
            note=self._note("current", "caption display times", selected_ranges),
        )

    def _active_edit_ids(self) -> set[str]:
        disabled: set[str] = set()
        for _ in range(len(self.edits) + 1):
            next_disabled = {
                op.target_edit_id
                for edit in self.edits
                if edit.id not in disabled
                for op in edit.ops
                if isinstance(op, RestoreOp)
            }
            if next_disabled == disabled:
                break
            disabled = next_disabled
        return {edit.id for edit in self.edits if edit.id not in disabled}

    def _cut_ranges_for_edit(self, edit: Edit) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        for op in edit.ops:
            if isinstance(op, CutOp):
                ranges.extend(
                    (max(0, start), min(self._source_duration_f, end))
                    for start, end in op.ranges
                    if max(0, start) < min(self._source_duration_f, end)
                )
            elif isinstance(op, KeepOnlyOp):
                keep_start, keep_end = op.range
                cut_before_end = min(self._source_duration_f, keep_start)
                if cut_before_end > 0:
                    ranges.append((0, cut_before_end))
                if keep_end < self._source_duration_f:
                    ranges.append((max(0, keep_end), self._source_duration_f))
        return ranges

    def resolve_restore(self, target: RestoreTarget) -> Resolution:
        """Find active cut/keep edits eligible for restoration."""
        active = self._active_edit_ids()
        candidates = [
            edit for edit in self.edits
            if edit.id in active and self._cut_ranges_for_edit(edit)
        ]
        if target.edit_id is not None:
            candidates = [edit for edit in candidates if edit.id == target.edit_id]
            if not candidates:
                return self._failure(
                    "NO_MATCH", f"No active cut/keep edit with id '{target.edit_id}'.", "original"
                )
        elif target.overlap is not None:
            start_s, end_s = target.overlap
            if start_s < 0 or end_s <= start_s:
                return self._failure("BAD_RANGE", "Original overlap range is invalid.", "original")
            start_f = round(start_s * self.fps)
            end_f = min(round(end_s * self.fps), self._source_duration_f)
            if start_f >= self._source_duration_f:
                return self._failure("OUT_OF_RANGE", "Overlap start is beyond the video.", "original")
            candidates = [
                edit for edit in candidates
                if any(a < end_f and b > start_f for a, b in self._cut_ranges_for_edit(edit))
            ]
        elif not target.any:
            return self._failure(
                "BAD_RANGE", "Specify an edit id, original-time overlap, or any=True.", "original"
            )

        options = [
            ResolutionOption(
                choice=i + 1,
                snippet=edit.instruction,
                edit_id=edit.id,
                source_ranges=tuple(self._cut_ranges_for_edit(edit)),
            )
            for i, edit in enumerate(candidates)
        ]
        if not options:
            return self._failure("NO_MATCH", "No active cut/keep edits can be restored.", "original")
        if len(options) > 1 and target.choice is None:
            note = self._ambiguous_note("restore candidate selection", options, "original")
            return Resolution(
                status="AMBIGUOUS",
                options=options + [ResolutionOption(choice="all", snippet="All candidate edits")],
                note=note,
                detail="Choose a numbered edit or all.",
            )
        if len(options) > 1 and isinstance(target.choice, str) and (
            target.choice.strip().casefold() == "all"
        ):
            chosen = options
        elif len(options) > 1:
            index = _choice_index(target.choice, len(options))
            if index is None:
                return Resolution(
                    status="AMBIGUOUS",
                    options=options + [ResolutionOption(choice="all", snippet="All candidate edits")],
                    note=self._ambiguous_note("restore candidate selection", options, "original"),
                    detail="Choose a valid numbered edit or all.",
                )
            chosen = [options[index]]
        else:
            chosen = options
        return Resolution(
            status="RESOLVED",
            edit_ids=[option.edit_id for option in chosen if option.edit_id],
            options=chosen,
            note=self._ambiguous_note("restore candidate selection", chosen, "original"),
        )

    def resolve_reorder_destination(self, target: ReorderDestination) -> Resolution:
        """Resolve reorder insertion point to one original source-frame boundary."""
        if target.kind == "start":
            if not self.timeline.segments:
                return self._failure("NO_MATCH", "The current timeline is empty.", "current")
            frame = self.timeline.segments[0].src_in_f
            ranges = [(frame, frame)]
            return Resolution(
                status="RESOLVED", destination_frame=frame,
                note=self._note("current", "start of current video", ranges),
            )
        if target.kind == "end":
            if not self.timeline.segments:
                return self._failure("NO_MATCH", "The current timeline is empty.", "current")
            frame = self.timeline.segments[-1].src_out_f
            ranges = [(frame, frame)]
            return Resolution(
                status="RESOLVED", destination_frame=frame,
                note=self._note("current", "end of current video", ranges),
            )
        if target.kind == "time":
            if target.time_s is None:
                return self._failure("BAD_RANGE", "A current time is required.", "current")
            time_f = round(target.time_s * self.fps)
            if time_f < 0:
                return self._failure("BAD_RANGE", "Destination time cannot be negative.", "current")
            if time_f >= self._current_duration_f:
                if time_f == self._current_duration_f and self.timeline.segments:
                    frame = self.timeline.segments[-1].src_out_f
                    return Resolution(
                        status="RESOLVED", destination_frame=frame,
                        note=self._note("current", "end of current video", [(frame, frame)]),
                    )
                return self._failure("OUT_OF_RANGE", "Destination time is beyond the video.", "current")
            frame = self.layout.out_to_src(time_f)
            if frame is None:
                return self._failure("NO_MATCH", "No source frame at destination time.", "current")
            return Resolution(
                status="RESOLVED",
                destination_frame=frame,
                note=self._note(
                    "current", f"destination {_format_time(target.time_s or 0)}",
                    [(frame, frame)],
                ),
            )
        if target.kind != "phrase" or not target.phrase:
            return self._failure("BAD_RANGE", "A reorder phrase is required.", "current")
        matches = self._phrase_matches(target.phrase, target.speaker)
        if not matches:
            return self._failure("NO_MATCH", f"No alive phrase '{target.phrase}'.", "current")
        selected, options, all_matches = self._choose_match(matches, target.choice)
        if selected is None:
            if all_matches or target.choice is not None:
                return self._failure(
                    "BAD_RANGE", "A reorder destination must resolve to exactly one phrase.", "current"
                )
            return Resolution(
                status="AMBIGUOUS",
                options=options,
                note=self._ambiguous_note(
                    f"reorder destination '{target.phrase}'", options
                ),
                detail="Choose one phrase occurrence.",
            )
        destination = self._snapped_match_range(selected)[0]
        return Resolution(
            status="RESOLVED",
            destination_frame=destination,
            word_ids=[self.transcript.words[i].id for i in selected.indices],
            note=self._note("current", f"reorder before '{target.phrase}'", [(destination, destination)]),
        )
