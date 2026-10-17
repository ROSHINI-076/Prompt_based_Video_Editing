"""Timeline replay: derive the current Timeline from source + edit stack.

Replay algorithm (section 6.4):
  1. Collect disabled = {target_edit_id for each RestoreOp in ACTIVE edits}
     Uses fixpoint iteration: only RestoreOps in active (non-disabled) edits count.
  2. Start with initial_timeline (one segment covering the full source)
  3. Iterate stack in order; skip any edit whose id is in disabled
  4. Apply each op in the edit
  5. Return the resulting Timeline

This makes restore reversible: undoing a RestoreOp re-enables the original edit.
All timeline logic is deterministic — no randomness, no I/O.
"""

from __future__ import annotations

from incubrix.engine.models import (
    AddCaptionOp,
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
    Timeline,
    Transcript,
)
from incubrix.engine.ops import (
    apply_add_caption,
    apply_bleep,
    apply_cut,
    apply_keep_only,
    apply_keep_pause,
    apply_music,
    apply_mute,
    apply_reorder,
    apply_speed,
    apply_style,
    update_overlay_statuses,
)
from incubrix.engine.timeline import initial_timeline


def _compute_disabled(stack: list[Edit]) -> set[str]:
    """Compute the set of disabled edit IDs using fixpoint iteration.

    Only RestoreOps in ACTIVE (non-disabled) edits disable their targets.
    This means: restoring a RestoreOp re-enables the original edit.

    Example:
      e1=cut, e2=restore(e1), e3=restore(e2)
      Pass 1: e3 is active → disables e2. disabled={e2}
      Pass 2: e3 active, e2 disabled → only e3's RestoreOp counts → disabled={e2}
      Stable. e1 is NOT disabled → cut is active.
    """
    disabled: set[str] = set()
    for _ in range(len(stack) + 1):
        new_disabled: set[str] = set()
        for edit in stack:
            if edit.id in disabled:
                continue  # this edit is itself disabled; ignore its RestoreOps
            for op in edit.ops:
                if isinstance(op, RestoreOp):
                    new_disabled.add(op.target_edit_id)
        if new_disabled == disabled:
            break
        disabled = new_disabled
    return disabled


def replay(
    source: SourceInfo,
    stack: list[Edit],
    transcript: Transcript,
) -> Timeline:
    """Derive the current Timeline by replaying all active edits."""
    fps = source.fps

    # Step 1: compute disabled set
    disabled = _compute_disabled(stack)

    # Step 2: start with the initial timeline
    tl = initial_timeline(source)

    # Step 3: apply each non-disabled edit in order
    for edit in stack:
        if edit.id in disabled:
            continue

        for op in edit.ops:
            if isinstance(op, CutOp):
                tl.segments, tl.overlays, tl.effects, warnings = apply_cut(
                    tl.segments,
                    tl.overlays,
                    tl.effects,
                    op.ranges,
                    transcript,
                    fps,
                )
                tl.warnings.extend(warnings)

            elif isinstance(op, RemoveFillersOp | RemoveSilenceOp):
                tl.segments, tl.overlays, tl.effects, warnings = apply_cut(
                    tl.segments,
                    tl.overlays,
                    tl.effects,
                    op.ranges,
                    transcript,
                    fps,
                )
                tl.warnings.extend(warnings)

            elif isinstance(op, KeepOnlyOp):
                tl.segments, tl.overlays, tl.effects, warnings = apply_keep_only(
                    tl.segments,
                    tl.overlays,
                    tl.effects,
                    op.range,
                    transcript,
                    fps,
                )
                tl.warnings.extend(warnings)

            elif isinstance(op, SpeedOp):
                tl.segments, warnings = apply_speed(tl.segments, op.range, op.factor)
                tl.warnings.extend(warnings)
                tl.overlays, ov_warnings = update_overlay_statuses(
                    tl.overlays, tl.segments, transcript, fps
                )
                tl.warnings.extend(ov_warnings)

            elif isinstance(op, ReorderOp):
                tl.segments, warnings = apply_reorder(tl.segments, op.range, op.before)
                tl.warnings.extend(warnings)

            elif isinstance(op, AddCaptionOp):
                tl.overlays, warnings = apply_add_caption(
                    tl.overlays,
                    op.overlay_id,
                    op.text,
                    op.start_word_id,
                    op.end_word_id,
                    op.style,
                    transcript,
                )
                tl.warnings.extend(warnings)

            elif isinstance(op, StyleOp):
                tl.overlays, warnings = apply_style(tl.overlays, op.overlay_id, op.style)
                tl.warnings.extend(warnings)

            elif isinstance(op, MuteOp):
                tl.effects, warnings = apply_mute(
                    tl.effects,
                    op.effect_id,
                    op.start_word_id,
                    op.end_word_id,
                )
                tl.warnings.extend(warnings)

            elif isinstance(op, BleepOp):
                tl.effects, warnings = apply_bleep(
                    tl.effects,
                    op.effect_id,
                    op.start_word_id,
                    op.end_word_id,
                )
                tl.warnings.extend(warnings)

            elif isinstance(op, MusicOp):
                tl.music, _ = apply_music(
                    tl.music,
                    op.track_id,
                    op.duck_db,
                    op.enabled,
                    op.duck_when,
                )

            elif isinstance(op, KeepPauseOp):
                tl.protected_pause_after_word_ids = apply_keep_pause(
                    tl.protected_pause_after_word_ids,
                    op.after_word_id,
                )

            # RestoreOp: handled by the disabled set above; no action here

    return tl
