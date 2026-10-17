"""Timeline layout helpers.

Provides Layout for converting between source and output frame positions.
All positions are integer frames. No I/O, no dependencies outside this package.

Key formulas:
  segment_output_length(seg) = max(1, round((src_out_f - src_in_f) / speed))
  src_to_out(f): sum of earlier segment lengths + (f - seg.src_in_f) / seg.speed
  A word is 'alive' if its midpoint frame lies inside any segment.
  caption_display_span: enforces MIN_CAPTION_FRAMES minimum duration.
"""

from __future__ import annotations

from incubrix.engine.models import Segment, SourceInfo, Timeline, Word, segment_output_length

# ── Minimum caption duration ──────────────────────────────────────
#
# Spec s6.5: "End time is at least MIN_CAPTION_S=0.7 after start, even when
# sped up."  This is a named constant in the engine; the API layer can later
# read it from env settings and pass it through.
#
# It is DERIVED at query time (when computing display times from anchors),
# never stored as an output timestamp.

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
        # Unreachable: out_frame < self._total guarantees one segment matched.
        # Kept as a safety net; covered by test_out_to_src_at_last_frame.
        return None  # pragma: no cover

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
