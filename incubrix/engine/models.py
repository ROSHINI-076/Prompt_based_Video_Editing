"""Core engine data models.

All positions are integer frames internally. Seconds are derived as
frames / fps, rounded to 3 decimals for output only.

Word IDs are deterministic and index-based: 'w0001', 'w0002', etc.
Edit IDs are monotonic: 'e1', 'e2', ... and never reused.
Overlays / effects anchor to word IDs via Anchor — never to output timestamps.
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, Field

# ── Transcript ────────────────────────────────────────────────────


class Word(BaseModel):
    """A single word from the transcript.

    id is deterministic and index-based ('w0001', 'w0002', ...).
    start_s / end_s are original source times from STT.
    """

    id: str
    text: str
    start_s: float
    end_s: float
    speaker: str = "default"
    confidence: float = 1.0
    flags: list[str] = Field(default_factory=list)


class Sentence(BaseModel):
    """Sentence boundary defined by inclusive word indices."""

    first_word_idx: int
    last_word_idx: int


class Transcript(BaseModel):
    """Full transcript with words, sentence boundaries, and speaker info."""

    words: list[Word]
    sentences: list[Sentence] = Field(default_factory=list)
    speakers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ── Source + segment ──────────────────────────────────────────────


class SourceInfo(BaseModel):
    """Immutable metadata about the source video. Never mutated after intake."""

    sha256: str
    duration_frames: int
    fps: float
    has_audio: bool = True


class Segment(BaseModel):
    """A contiguous range of source frames [src_in_f, src_out_f) at a speed.

    Output length = max(1, round((src_out_f - src_in_f) / speed)) frames.
    """

    src_in_f: int  # inclusive
    src_out_f: int  # exclusive
    speed: float = 1.0


# ── Overlay / effect ──────────────────────────────────────────────


class Anchor(BaseModel):
    """Anchors an overlay or effect to content via word IDs (never timestamps)."""

    start_word_id: str
    end_word_id: str


class OverlayStatus(str, enum.Enum):
    """Lifecycle status of an overlay."""

    ACTIVE = "active"
    DORMANT = "dormant"  # all anchor words cut; revives when the cut is restored
    PARTIAL = "partial"  # some anchor words cut; display shrinks


class Overlay(BaseModel):
    """Caption / text overlay anchored to words.

    Display time is COMPUTED from alive anchor words on demand.
    It is never stored as an output timestamp.
    """

    id: str
    type: str = "caption"
    text: str
    style: dict[str, Any] = Field(default_factory=dict)
    anchor: Anchor
    status: OverlayStatus = OverlayStatus.ACTIVE
    # Number of words in range at creation time (for partial-cut tracking).
    original_word_count: int = 0


class EffectType(str, enum.Enum):
    MUTE = "mute"
    BLEEP = "bleep"


class Effect(BaseModel):
    """Audio effect (mute / bleep) anchored to words."""

    id: str
    type: EffectType
    anchor: Anchor


class MusicSettings(BaseModel):
    """Background music track settings."""

    track: str = ""
    duck_db: float = -12.0
    enabled: bool = False


# ── Timeline ──────────────────────────────────────────────────────


class Timeline(BaseModel):
    """Derived state of the video after all active edits.

    Always produced by replay(source, stack). Never stored or mutated directly.
    """

    source: SourceInfo
    segments: list[Segment]
    overlays: list[Overlay] = Field(default_factory=list)
    effects: list[Effect] = Field(default_factory=list)
    music: MusicSettings = Field(default_factory=MusicSettings)
    warnings: list[str] = Field(default_factory=list)


# ── Resolved, source-anchored operation models ────────────────────


class CutOp(BaseModel):
    """Remove source-frame ranges from the timeline."""

    op: str = "cut"
    ranges: list[tuple[int, int]]  # list of (src_in_f, src_out_f)


class KeepOnlyOp(BaseModel):
    """Retain only a given source-frame range (cut everything outside)."""

    op: str = "keep_only"
    range: tuple[int, int]  # (src_in_f, src_out_f)


class SpeedOp(BaseModel):
    """Set playback speed for a source-frame range.

    Overlap rule: last instruction wins on the overlapped region.
    """

    op: str = "speed"
    range: tuple[int, int]  # (src_in_f, src_out_f)
    factor: float


class ReorderOp(BaseModel):
    """Move a source-frame range to appear before a destination frame."""

    op: str = "reorder"
    range: tuple[int, int]  # (src_in_f, src_out_f) to move
    before: int  # destination source frame (insert before this)


class AddCaptionOp(BaseModel):
    """Add a caption overlay anchored to words."""

    op: str = "add_caption"
    overlay_id: str
    text: str
    start_word_id: str
    end_word_id: str
    style: dict[str, Any] = Field(default_factory=dict)


class StyleOp(BaseModel):
    """Change the style of an existing overlay."""

    op: str = "style"
    overlay_id: str
    style: dict[str, Any]


class MuteOp(BaseModel):
    """Mute audio over a word range."""

    op: str = "mute"
    effect_id: str
    start_word_id: str
    end_word_id: str


class BleepOp(BaseModel):
    """Bleep audio over a word range (silence + sine tone)."""

    op: str = "bleep"
    effect_id: str
    start_word_id: str
    end_word_id: str


class RestoreOp(BaseModel):
    """Restore: mark a previous edit as disabled during replay.

    In replay, any edit whose id appears as target_edit_id of a RestoreOp
    anywhere in the stack is skipped entirely.  This makes restore reversible:
    undoing a RestoreOp re-applies the original cut.
    """

    op: str = "restore"
    target_edit_id: str


class MusicOp(BaseModel):
    """Configure background music."""

    op: str = "music"
    track: str
    duck_db: float = -12.0
    enabled: bool = True


# Union type — used for type hints only
AnyOp = (
    CutOp
    | KeepOnlyOp
    | SpeedOp
    | ReorderOp
    | AddCaptionOp
    | StyleOp
    | MuteOp
    | BleepOp
    | RestoreOp
    | MusicOp
)

_ALL_OPS = (
    CutOp,
    KeepOnlyOp,
    SpeedOp,
    ReorderOp,
    AddCaptionOp,
    StyleOp,
    MuteOp,
    BleepOp,
    RestoreOp,
    MusicOp,
)


class Edit(BaseModel):
    """One instruction = one Edit containing resolved, source-anchored ops.

    Edit IDs are monotonic ('e1', 'e2', ...) and never reused. `intent_ops`
    retains input selectors for safe repeat confirmation; replay ignores them.
    """

    id: str
    instruction: str
    ops: list[AnyOp]
    # This metadata supports confirmation without changing source-anchored ops.
    intent_ops: list[dict[str, Any]] = Field(default_factory=list)


class SessionState(BaseModel):
    """Edit history and redo stack for a session.

    timeline = replay(source, stack) — always derived, never stored.
    """

    source: SourceInfo
    transcript: Transcript
    stack: list[Edit] = Field(default_factory=list)
    redo: list[Edit] = Field(default_factory=list)
    edit_counter: int = 0
    pending_question: dict[str, Any] | None = None


# ── Helper functions ──────────────────────────────────────────────


def make_word_id(index: int) -> str:
    """Create deterministic word ID from zero-based index.

    >>> make_word_id(0)
    'w0001'
    >>> make_word_id(99)
    'w0100'
    """
    return f"w{index + 1:04d}"


def segment_output_length(seg: Segment) -> int:
    """Output length of a segment in frames.

    Rule: max(1, round((src_out_f - src_in_f) / speed))
    """
    return max(1, round((seg.src_out_f - seg.src_in_f) / seg.speed))
