"""Operation application functions.

All functions are PURE — they take immutable inputs and return new lists.
No I/O, no side effects, no global state.

Rules implemented here:
- Cut: split segments at range edges, remove ranges, mark overlays dormant/partial.
- Keep only: invert to cut everything outside.
- Speed: split at range edges, set speed; last-instruction-wins on overlap.
- Reorder: move contiguous segments to a new position.
- Add caption / style / mute / bleep / music: overlay/effect management.
- snap_to_silence: snap cut boundaries to midpoint of silence gaps.
- update_overlay_statuses: called after any cut/keep/reorder.
"""

from __future__ import annotations

import re

from incubrix.engine.models import (
    Anchor,
    Effect,
    EffectType,
    MusicSettings,
    Overlay,
    OverlayStatus,
    Segment,
    Transcript,
)
from incubrix.engine.timeline import is_word_alive

ALWAYS_FILLER_WORDS = frozenset({"um", "uh", "erm", "er", "ah", "hmm"})
LIKE_PREVIOUS_WORD_EXCLUSIONS = frozenset(
    {
        "i",
        "you",
        "we",
        "they",
        "would",
        "looks",
        "look",
        "feels",
        "feel",
        "sounds",
        "sound",
        "don't",
        "didn't",
        "doesn't",
        "not",
        "just",
        "and",
        "or",
    }
)
LIKE_PAUSE_THRESHOLD_S = 0.25
MIN_SILENCE_PAD_S = 0.15
AUTO_CAPTION_MAX_WORDS = 8


def _normalized_word(text: str) -> str:
    """Return the first word token without surrounding punctuation."""
    match = re.search(r"[a-z0-9']+", text.casefold())
    return match.group(0) if match else ""


def is_filler_word(index: int, transcript: Transcript) -> bool:
    """Apply the explicit always-filler and contextual 'like' rules."""
    word = transcript.words[index]
    token = _normalized_word(word.text)
    if token in ALWAYS_FILLER_WORDS:
        return True
    if token != "like":
        return False

    has_comma = word.text.rstrip().endswith(",")
    gap_before = word.start_s - transcript.words[index - 1].end_s if index > 0 else 0.0
    gap_after = (
        transcript.words[index + 1].start_s - word.end_s
        if index + 1 < len(transcript.words)
        else 0.0
    )
    has_pause = max(gap_before, gap_after) >= LIKE_PAUSE_THRESHOLD_S
    previous = _normalized_word(transcript.words[index - 1].text) if index > 0 else ""
    return (has_comma or has_pause) and previous not in LIKE_PREVIOUS_WORD_EXCLUSIONS


# ── Internal segment-splitting helper ────────────────────────────


def _split_segments_at(segments: list[Segment], frame: int) -> list[Segment]:
    """Split any segment that straddles 'frame' into two pieces.

    The frame boundary is always between src_in_f and src_out_f.
    Returns a new list of segments.
    """
    result: list[Segment] = []
    for seg in segments:
        if seg.src_in_f < frame < seg.src_out_f:
            result.append(Segment(src_in_f=seg.src_in_f, src_out_f=frame, speed=seg.speed))
            result.append(Segment(src_in_f=frame, src_out_f=seg.src_out_f, speed=seg.speed))
        else:
            result.append(seg)
    return result


def _remove_range(segments: list[Segment], cut_in: int, cut_out: int) -> list[Segment]:
    """Remove source frames [cut_in, cut_out) from segments.

    Segments are first split at the range boundaries, then the overlap is dropped.
    """
    segs = _split_segments_at(segments, cut_in)
    segs = _split_segments_at(segs, cut_out)
    return [s for s in segs if not (s.src_in_f >= cut_in and s.src_out_f <= cut_out)]


# ── Overlay status helper ─────────────────────────────────────────


def _word_range(transcript: Transcript, start_id: str, end_id: str) -> list[int]:
    """Return indices of words between start_id and end_id (inclusive)."""
    ids = [w.id for w in transcript.words]
    try:
        si = ids.index(start_id)
        ei = ids.index(end_id)
    except ValueError:
        return []
    if si > ei:
        si, ei = ei, si
    return list(range(si, ei + 1))


def update_overlay_statuses(
    overlays: list[Overlay],
    segments: list[Segment],
    transcript: Transcript,
    fps: float,
) -> tuple[list[Overlay], list[str]]:
    """Update ACTIVE/DORMANT/PARTIAL status of all overlays.

    Rules (section 6.5):
    - 0 alive anchor words → DORMANT (warn)
    - 1..n-1 alive anchor words → PARTIAL (warn "now covers k of n words")
    - all alive → ACTIVE
    """
    updated: list[Overlay] = []
    warnings: list[str] = []
    for ov in overlays:
        indices = _word_range(transcript, ov.anchor.start_word_id, ov.anchor.end_word_id)
        if not indices:
            updated.append(ov)
            continue
        alive_count = sum(1 for i in indices if is_word_alive(transcript.words[i], segments, fps))
        total = len(indices)
        if alive_count == 0:
            if ov.status != OverlayStatus.DORMANT:
                warnings.append(
                    f"Caption '{ov.text}' (id={ov.id}) is now dormant — "
                    f"all {total} anchor words were cut. It will revive if the cut is restored."
                )
            new_status = OverlayStatus.DORMANT
        elif alive_count < total:
            if ov.status == OverlayStatus.ACTIVE or (
                ov.status == OverlayStatus.PARTIAL
                and alive_count != ov.original_word_count - (total - alive_count)
            ):
                warnings.append(
                    f"Caption '{ov.text}' (id={ov.id}) now covers {alive_count} of "
                    f"{ov.original_word_count} words (partial)."
                )
            new_status = OverlayStatus.PARTIAL
        else:
            new_status = OverlayStatus.ACTIVE

        updated.append(ov.model_copy(update={"status": new_status}))
    return updated, warnings


# ── snap_to_silence ───────────────────────────────────────────────


def snap_to_silence(
    first_word_idx: int,
    last_word_idx: int,
    transcript: Transcript,
    fps: float,
) -> tuple[int, int]:
    """Snap cut boundaries to the midpoint of surrounding silence gaps.

    Rule (section 6.5): 'Cuts defined by words are snapped to the midpoint
    of the silence gap before the first word and after the last word so no
    word is clipped.'

    Returns (snap_in_frame, snap_out_frame) as integer frames.
    """
    words = transcript.words
    first_word = words[first_word_idx]
    last_word = words[last_word_idx]

    # Snap in: midpoint between previous word's end and first word's start
    if first_word_idx > 0:
        prev_end_s = words[first_word_idx - 1].end_s
        snap_in_s = (prev_end_s + first_word.start_s) / 2.0
    else:
        snap_in_s = first_word.start_s

    # Snap out: midpoint between last word's end and next word's start
    if last_word_idx < len(words) - 1:
        next_start_s = words[last_word_idx + 1].start_s
        snap_out_s = (last_word.end_s + next_start_s) / 2.0
    else:
        snap_out_s = last_word.end_s

    return round(snap_in_s * fps), round(snap_out_s * fps)


# ── apply_cut ─────────────────────────────────────────────────────


def apply_cut(
    segments: list[Segment],
    overlays: list[Overlay],
    effects: list[Effect],
    cut_ranges: list[tuple[int, int]],
    transcript: Transcript,
    fps: float,
) -> tuple[list[Segment], list[Overlay], list[Effect], list[str]]:
    """Remove source-frame ranges from segments.

    Overlays whose all anchor words are cut → DORMANT.
    Overlays whose some anchor words are cut → PARTIAL.
    Returns (new_segments, updated_overlays, effects_unchanged, warnings).
    """
    new_segs = list(segments)
    for cut_in, cut_out in cut_ranges:
        new_segs = _remove_range(new_segs, cut_in, cut_out)
    new_overlays, warnings = update_overlay_statuses(overlays, new_segs, transcript, fps)
    return new_segs, new_overlays, list(effects), warnings


# ── apply_keep_only ───────────────────────────────────────────────


def apply_keep_only(
    segments: list[Segment],
    overlays: list[Overlay],
    effects: list[Effect],
    keep_range: tuple[int, int],
    transcript: Transcript,
    fps: float,
) -> tuple[list[Segment], list[Overlay], list[Effect], list[str]]:
    """Keep only the given source-frame range; cut everything outside.

    Equivalent to cutting [0, keep_in) and [keep_out, total_source).
    """
    keep_in, keep_out = keep_range
    # Find the full source span
    if not segments:
        return segments, overlays, effects, []
    total_src = max(s.src_out_f for s in segments)
    cut_ranges: list[tuple[int, int]] = []
    if keep_in > 0:
        cut_ranges.append((0, keep_in))
    if keep_out < total_src:
        cut_ranges.append((keep_out, total_src))
    if not cut_ranges:
        return list(segments), list(overlays), list(effects), []
    return apply_cut(segments, overlays, effects, cut_ranges, transcript, fps)


# ── apply_speed ───────────────────────────────────────────────────


def apply_speed(
    segments: list[Segment],
    speed_range: tuple[int, int],
    factor: float,
) -> tuple[list[Segment], list[str]]:
    """Set playback speed for a source-frame range.

    Rule (section 6.5): 'last instruction wins (replace) on the overlapped region'.
    Splits segments at range boundaries, then sets speed on overlapping parts.
    """
    sp_in, sp_out = speed_range
    segs = _split_segments_at(segments, sp_in)
    segs = _split_segments_at(segs, sp_out)
    result: list[Segment] = []
    overlap_found = False
    for seg in segs:
        if seg.src_in_f >= sp_in and seg.src_out_f <= sp_out:
            if seg.speed not in (1.0, factor):
                overlap_found = True
            result.append(Segment(src_in_f=seg.src_in_f, src_out_f=seg.src_out_f, speed=factor))
        else:
            result.append(seg)
    warnings: list[str] = []
    if overlap_found:
        warnings.append(
            f"Speed overlap: previous speed in {sp_in}-{sp_out} replaced by {factor}x "
            f"(last instruction wins)."
        )
    return result, warnings


# ── apply_reorder ─────────────────────────────────────────────────


def apply_reorder(
    segments: list[Segment],
    move_range: tuple[int, int],
    before: int,
) -> tuple[list[Segment], list[str]]:
    """Move segments covering move_range to before the segment at 'before'.

    Rejects if 'before' falls inside the moved range.
    Returns (new_segments, warnings).
    """
    mv_in, mv_out = move_range
    warnings: list[str] = []

    # Guard: destination must not be inside the section being moved
    if mv_in <= before < mv_out:
        warnings.append(
            f"Reorder rejected: destination frame {before} is inside the section "
            f"being moved ({mv_in}–{mv_out})."
        )
        return list(segments), warnings

    # Split at all boundaries
    segs = _split_segments_at(segments, mv_in)
    segs = _split_segments_at(segs, mv_out)
    segs = _split_segments_at(segs, before)

    moving = [s for s in segs if s.src_in_f >= mv_in and s.src_out_f <= mv_out]
    rest = [s for s in segs if not (s.src_in_f >= mv_in and s.src_out_f <= mv_out)]

    # Find insertion point in rest
    insert_idx = next(
        (i for i, s in enumerate(rest) if s.src_in_f >= before),
        len(rest),
    )
    result = rest[:insert_idx] + moving + rest[insert_idx:]
    return result, warnings


# ── apply_add_caption ─────────────────────────────────────────────


def apply_add_caption(
    overlays: list[Overlay],
    overlay_id: str,
    text: str,
    start_word_id: str,
    end_word_id: str,
    style: dict,
    transcript: Transcript,
) -> tuple[list[Overlay], list[str]]:
    """Add a new caption overlay anchored to words."""
    indices = _word_range(transcript, start_word_id, end_word_id)
    word_count = len(indices)
    overlay = Overlay(
        id=overlay_id,
        text=text,
        anchor=Anchor(start_word_id=start_word_id, end_word_id=end_word_id),
        style=dict(style),
        status=OverlayStatus.ACTIVE,
        original_word_count=word_count,
    )
    return list(overlays) + [overlay], []


# ── apply_style ───────────────────────────────────────────────────


def apply_style(
    overlays: list[Overlay],
    overlay_id: str,
    style: dict,
) -> tuple[list[Overlay], list[str]]:
    """Change the style of an existing overlay."""
    result: list[Overlay] = []
    found = False
    for ov in overlays:
        if ov.id == overlay_id:
            merged = {**ov.style, **style}
            result.append(ov.model_copy(update={"style": merged}))
            found = True
        else:
            result.append(ov)
    warnings = [] if found else [f"Style op: overlay '{overlay_id}' not found."]
    return result, warnings


# ── apply_mute / apply_bleep ──────────────────────────────────────


def apply_mute(
    effects: list[Effect],
    effect_id: str,
    start_word_id: str,
    end_word_id: str,
) -> tuple[list[Effect], list[str]]:
    """Add a mute effect anchored to words."""
    fx = Effect(
        id=effect_id,
        type=EffectType.MUTE,
        anchor=Anchor(start_word_id=start_word_id, end_word_id=end_word_id),
    )
    return list(effects) + [fx], []


def apply_bleep(
    effects: list[Effect],
    effect_id: str,
    start_word_id: str,
    end_word_id: str,
) -> tuple[list[Effect], list[str]]:
    """Add a bleep effect (silence + sine tone) anchored to words."""
    fx = Effect(
        id=effect_id,
        type=EffectType.BLEEP,
        anchor=Anchor(start_word_id=start_word_id, end_word_id=end_word_id),
    )
    return list(effects) + [fx], []


# ── apply_music ───────────────────────────────────────────────────


def apply_music(
    current: MusicSettings,
    track: str,
    duck_db: float,
    enabled: bool,
    duck_when: str = "speech",
) -> tuple[MusicSettings, list[str]]:
    """Update background music settings."""
    return (
        MusicSettings(track_id=track, duck_db=duck_db, enabled=enabled, duck_when=duck_when),
        [],
    )


def apply_keep_pause(protected_after_word_ids: list[str], after_word_id: str) -> list[str]:
    """Add an anchored pause protection without duplicating existing markers."""
    if after_word_id in protected_after_word_ids:
        return list(protected_after_word_ids)
    return [*protected_after_word_ids, after_word_id]
