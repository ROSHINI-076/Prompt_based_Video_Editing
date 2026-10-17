"""Timeline layout helpers.

Provides Layout for converting between source and output frame positions,
and compute_overlay_displays() — the CANONICAL function the API and renderer
use to obtain caption display times with the minimum-duration rule applied.

All positions are integer frames. No I/O, no dependencies outside this package.

Key formulas:
  segment_output_length(seg) = max(1, round((src_out_f - src_in_f) / speed))
  src_to_out(f): sum of earlier segment lengths + (f - seg.src_in_f) / seg.speed
  A word is 'alive' if its midpoint frame lies inside any segment.
  caption_display_span: enforces MIN_CAPTION_FRAMES minimum duration.

Design note (see docs/decisions.md D004):
  Display times are NEVER stored in the Timeline.  They are derived from
  word anchors + segment list on demand via compute_overlay_displays().
  timeline_hash therefore hashes the source (fps, duration), segments, and
  word-ID anchors, which transitively guarantees identical display times for
  an identical hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from incubrix.engine.models import (
    Overlay,
    OverlayStatus,
    Segment,
    SourceInfo,
    Timeline,
    Word,
    segment_output_length,
)

if TYPE_CHECKING:
    from incubrix.engine.models import Transcript


# ── Minimum caption duration ──────────────────────────────────────
#
# Spec s6.5: "End time is at least MIN_CAPTION_S=0.7 after start, even when
# sped up."  This is a named constant in the engine; the API layer can later
# read it from env settings and pass it through to compute_overlay_displays().
#
# IMPORTANT: The minimum is DERIVED at query time (when computing display times
# from anchors), never stored as an output timestamp in the Timeline or Overlay.

MIN_CAPTION_S: float = 0.7


# ── Pure helpers ──────────────────────────────────────────────────


def total_duration_frames(segments: list[Segment]) -> int:
    """Total output duration in frames = sum of all segment output lengths."""
    return sum(segment_output_length(s) for s in segments)


def initial_timeline(source: SourceInfo) -> Timeline:
    """Create the initial timeline: one segment covering the whole source."""
    from incubrix.engine.models import MusicSettings

    return Timeline(
        source=source,
        segments=[Segment(src_in_f=0, src_out_f=source.duration_frames)],
        music=MusicSettings(),
    )


def word_midpoint_frame(word: Word, fps: float) -> int:
    """The source midpoint frame of a word.

    mid = round(((start_s + end_s) / 2) * fps)
    """
    mid_s = (word.start_s + word.end_s) / 2.0
    return round(mid_s * fps)


def is_word_alive(word: Word, segments: list[Segment], fps: float) -> bool:
    """A word is alive if its midpoint frame lies inside any segment.

    Rule (section 6.3): 'A word is alive if its midpoint frame lies inside
    a segment.'
    """
    mid_f = word_midpoint_frame(word, fps)
    return any(seg.src_in_f <= mid_f < seg.src_out_f for seg in segments)


def frames_to_seconds(frames: int, fps: float) -> float:
    """Convert frames to seconds, rounded to 3 decimal places."""
    return round(frames / fps, 3)


def seconds_to_frames(seconds: float, fps: float) -> int:
    """Convert seconds to frames (round to nearest)."""
    return round(seconds * fps)


def min_caption_frames(fps: float, min_s: float = MIN_CAPTION_S) -> int:
    """Minimum caption duration in output frames.

    min_caption_frames(30.0) = round(0.7 * 30) = 21 frames.
    """
    return round(min_s * fps)


def caption_display_span(
    start_out_f: int,
    end_out_f: int,
    total_out_frames: int,
    fps: float,
    min_s: float = MIN_CAPTION_S,
) -> tuple[int, int]:
    """Enforce the minimum caption duration rule.

    Spec s6.5: "End time is at least MIN_CAPTION_S after start, even when
    sped up."  The minimum is derived at query time from word anchors; it is
    NEVER stored as an output timestamp.

    Also caps at the timeline end so the caption never overshoots.

    Returns (start_out_f, end_out_f) with the minimum enforced.
    """
    min_frames = min_caption_frames(fps, min_s)
    # Enforce minimum end = start + min_frames
    end_out_f = max(end_out_f, start_out_f + min_frames)
    # Cap at the timeline end
    end_out_f = min(end_out_f, total_out_frames)
    # If capping moved end before start (video shorter than min), clamp start too
    start_out_f = min(start_out_f, end_out_f)
    return start_out_f, end_out_f


# ── Layout class ──────────────────────────────────────────────────


class Layout:
    """Converts between source-frame and output-frame positions.

    Constructed from a list of Segments.  Precomputes cumulative output starts
    so each lookup is O(segments).

    Output time of source frame f:
        find the segment containing f, then
        out = out_start[i] + round((f - seg.src_in_f) / seg.speed)
    """

    def __init__(self, segments: list[Segment]) -> None:
        self._segments = list(segments)
        self._out_starts: list[int] = []
        acc = 0
        for seg in self._segments:
            self._out_starts.append(acc)
            acc += segment_output_length(seg)
        self._total = acc

    @property
    def total_frames(self) -> int:
        """Total output duration in frames."""
        return self._total

    def src_to_out(self, frame: int) -> int | None:
        """Convert a source frame to its output frame position.

        Returns None if the frame was cut (not in any segment).
        """
        for i, seg in enumerate(self._segments):
            if seg.src_in_f <= frame < seg.src_out_f:
                offset = (frame - seg.src_in_f) / seg.speed
                return self._out_starts[i] + round(offset)
        return None

    def out_to_src(self, out_frame: int) -> int | None:
        """Convert an output frame to its source frame position.

        Returns None if out_frame is out of range.

        Proof that the final 'return None' is unreachable for a normal Layout:
          Precondition: the bounds check at the top ensures
            0 <= out_frame < self._total
          where self._total = sum(segment_output_length(s) for s in segments).
          The loop walks segments whose cumulative lengths exactly tile [0, _total).
          For each segment i: out_frame < seg_end iff out_frame is in that tile.
          Because the tiles are exhaustive and non-overlapping, exactly one
          segment satisfies the condition and the function returns there.
          The final return None is therefore unreachable while this invariant
          holds; it remains a defensive fallback if cached state is inconsistent.
        """
        if out_frame < 0 or out_frame >= self._total:
            return None
        for i, seg in enumerate(self._segments):
            seg_len = segment_output_length(seg)
            seg_end = self._out_starts[i] + seg_len
            if out_frame < seg_end:
                offset = out_frame - self._out_starts[i]
                src_frame = seg.src_in_f + round(offset * seg.speed)
                return min(src_frame, seg.src_out_f - 1)
        return None

    def out_range_to_src_ranges(
        self, out_start: int, out_end: int
    ) -> list[tuple[int, int]]:
        """Convert an output-time range [out_start, out_end) to source ranges.

        A single output range may map to multiple source ranges when segments
        are non-contiguous in source space (e.g. after a reorder).
        Returns list of (src_in_f, src_out_f) tuples.
        """
        result: list[tuple[int, int]] = []
        for i, seg in enumerate(self._segments):
            seg_out_start = self._out_starts[i]
            seg_out_end = seg_out_start + segment_output_length(seg)
            overlap_start = max(out_start, seg_out_start)
            overlap_end = min(out_end, seg_out_end)
            if overlap_start < overlap_end:
                src_start = seg.src_in_f + round(
                    (overlap_start - seg_out_start) * seg.speed
                )
                src_end = seg.src_in_f + round(
                    (overlap_end - seg_out_start) * seg.speed
                )
                src_start = max(src_start, seg.src_in_f)
                src_end = min(src_end, seg.src_out_f)
                if src_start < src_end:
                    result.append((src_start, src_end))
        return result

    def word_out_span(self, word: Word, fps: float) -> tuple[int, int] | None:
        """Get the output frame span (start, end) of a word.

        Returns None if the word is not alive (was cut).
        """
        start_f = round(word.start_s * fps)
        end_f = round(word.end_s * fps)
        out_start = self.src_to_out(start_f)
        out_end = self.src_to_out(end_f)
        if out_start is None or out_end is None:
            return None
        return (out_start, out_end)

    def caption_out_span(
        self,
        start_word: Word,
        end_word: Word,
        fps: float,
        min_s: float = MIN_CAPTION_S,
    ) -> tuple[int, int] | None:
        """Get the output frame span of a caption, enforcing minimum duration.

        Computes the display span from the word anchors, then applies the
        minimum caption duration rule (spec s6.5) and caps at the timeline end.

        Returns None if neither anchor word is alive.
        """
        start_span = self.word_out_span(start_word, fps)
        end_span = self.word_out_span(end_word, fps)

        if start_span is None and end_span is None:
            return None  # all anchor words cut → dormant

        # Use whichever anchor is alive; fall back to the other
        start_out = start_span[0] if start_span is not None else (end_span[0] if end_span else 0)
        end_out = end_span[1] if end_span is not None else (start_span[1] if start_span else 0)

        return caption_display_span(start_out, end_out, self._total, fps, min_s)


# ── OverlayDisplay: canonical output type ─────────────────────────


@dataclass
class OverlayDisplay:
    """Computed display information for one overlay.

    This is what the API serialises and what the renderer consumes.
    start_out_f / end_out_f already have the MIN_CAPTION_S minimum applied.
    They are None when the overlay is dormant (all anchor words cut).

    Design (see docs/decisions.md D004):
      These times are NOT stored in Timeline — they are re-derived on every
      call to compute_overlay_displays(). timeline_hash does not cover them
      directly; its scope is the stored Timeline, not transcript timestamps.
    """

    overlay_id: str
    text: str
    style: dict
    status: OverlayStatus
    # Output-frame positions with MIN_CAPTION_S enforced. None = dormant.
    start_out_f: int | None
    end_out_f: int | None
    # Seconds derived from integer frames: round(frames / fps, 3)
    start_s: float | None
    end_s: float | None

    def to_dict(self) -> dict:
        """Serialize the computed overlay view using JSON-friendly values."""
        return {
            "overlay_id": self.overlay_id,
            "text": self.text,
            "style": dict(self.style),
            "status": self.status.value,
            "start_out_f": self.start_out_f,
            "end_out_f": self.end_out_f,
            "start_s": self.start_s,
            "end_s": self.end_s,
        }


def compute_overlay_displays(
    timeline: Timeline,
    transcript: "Transcript",
    min_s: float = MIN_CAPTION_S,
) -> list[OverlayDisplay]:
    """Compute display times for all overlays with the minimum duration applied.

    THIS IS THE CANONICAL PATH the API and renderer use to get caption times.

    Rules applied here (spec s6.5):
    - DORMANT overlays → start_out_f / end_out_f / start_s / end_s are None.
    - ACTIVE / PARTIAL overlays → times derived from alive anchor words via
      Layout.caption_out_span(), which enforces MIN_CAPTION_S and caps at the
      timeline end.

    Args:
        timeline:   The current Timeline (produced by replay()).
        transcript: The full transcript with all words.
        min_s:      Minimum caption display duration in seconds. Defaults to
                    MIN_CAPTION_S (0.7). The API layer passes the env-configured
                    value here; the engine default is used when called directly.

    Returns a list of OverlayDisplay — one per overlay in timeline.overlays,
    in the same order.
    """
    fps = timeline.source.fps
    layout = Layout(timeline.segments)

    # Build O(1) word lookup
    word_by_id: dict[str, Word] = {w.id: w for w in transcript.words}

    result: list[OverlayDisplay] = []
    for ov in timeline.overlays:
        # Dormant overlays never have display times
        if ov.status == OverlayStatus.DORMANT:
            result.append(OverlayDisplay(
                overlay_id=ov.id,
                text=ov.text,
                style=dict(ov.style),
                status=ov.status,
                start_out_f=None,
                end_out_f=None,
                start_s=None,
                end_s=None,
            ))
            continue

        start_word = word_by_id.get(ov.anchor.start_word_id)
        end_word = word_by_id.get(ov.anchor.end_word_id)

        if start_word is None or end_word is None:
            # Anchor words not found in transcript — treat as dormant
            result.append(OverlayDisplay(
                overlay_id=ov.id,
                text=ov.text,
                style=dict(ov.style),
                status=ov.status,
                start_out_f=None,
                end_out_f=None,
                start_s=None,
                end_s=None,
            ))
            continue

        span = layout.caption_out_span(start_word, end_word, fps, min_s)
        if span is None:
            # Both anchor words cut (status mismatch — guard defensively)
            result.append(OverlayDisplay(
                overlay_id=ov.id,
                text=ov.text,
                style=dict(ov.style),
                status=ov.status,
                start_out_f=None,
                end_out_f=None,
                start_s=None,
                end_s=None,
            ))
        else:
            start_out_f, end_out_f = span
            result.append(OverlayDisplay(
                overlay_id=ov.id,
                text=ov.text,
                style=dict(ov.style),
                status=ov.status,
                start_out_f=start_out_f,
                end_out_f=end_out_f,
                start_s=round(start_out_f / fps, 3),
                end_s=round(end_out_f / fps, 3),
            ))

    return result


def timeline_to_output(
    timeline: Timeline,
    transcript: "Transcript",
    min_s: float = MIN_CAPTION_S,
) -> dict:
    """Serialize a timeline with its current, minimum-enforced overlay display times.

    Stored overlays remain anchored to word IDs. The output projection is the
    public engine boundary for API and renderer consumers that need display times.
    """
    result = timeline.model_dump(mode="json")
    result["overlay_displays"] = [
        display.to_dict()
        for display in compute_overlay_displays(timeline, transcript, min_s)
    ]
    return result
