"""Strict pydantic schema for parser-produced instruction operations."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Number = int | float


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TimeTarget(StrictModel):
    kind: Literal["time"]
    start: Number | None = None
    end: Number | None = None
    from_start: Number | None = None
    from_end: Number | None = None
    policy: Literal["current", "original"] = "current"

    @model_validator(mode="after")
    def has_bounds(self) -> TimeTarget:
        """Require one complete, unambiguous time selector."""
        explicit_range = self.start is not None and self.end is not None
        relative_range = (self.from_start is not None) ^ (self.from_end is not None)
        if not explicit_range and not relative_range:
            raise ValueError("time targets need start/end, from_start, or from_end")
        if (self.start is None) != (self.end is None):
            raise ValueError("time range requires both start and end")
        if explicit_range and (self.from_start is not None or self.from_end is not None):
            raise ValueError("time targets cannot mix explicit and relative bounds")
        return self


class PhraseTarget(StrictModel):
    kind: Literal["phrase"]
    text: str = Field(min_length=1)
    relation: Literal["at", "before", "after"] = "at"
    speaker: str | None = None
    choice: int | str | None = None


class TopicTarget(StrictModel):
    kind: Literal["topic"]
    query: str = Field(min_length=1)
    speaker: str | None = None
    choice: int | str | None = None


class CaptionTarget(StrictModel):
    kind: Literal["caption"]
    text: str | None = None
    choice: int | str | None = None


class EditTarget(StrictModel):
    kind: Literal["edit"]
    edit_id: str | None = None
    choice: int | str | None = None


class OverlapTarget(StrictModel):
    kind: Literal["overlap"]
    start: Number
    end: Number
    policy: Literal["current", "original"] = "original"
    choice: int | str | None = None


class AnyTarget(StrictModel):
    kind: Literal["any"]
    choice: int | str | None = None


class CutOrdinalTarget(StrictModel):
    kind: Literal["cut_ordinal"]
    from_end: int = Field(ge=1)
    choice: int | str | None = None


class StartTarget(StrictModel):
    kind: Literal["start"]


class EndTarget(StrictModel):
    kind: Literal["end"]


class DestinationTimeTarget(StrictModel):
    kind: Literal["time"]
    time_s: Number
    policy: Literal["current", "original"] = "current"
    choice: int | str | None = None


class DestinationPhraseTarget(StrictModel):
    kind: Literal["phrase"]
    phrase: str = Field(min_length=1)
    speaker: str | None = None
    choice: int | str | None = None


class WordTarget(StrictModel):
    kind: Literal["word"]
    text: str = Field(min_length=1)
    choice: int | str | None = None


ContentTarget = Annotated[
    TimeTarget | PhraseTarget | TopicTarget,
    Field(discriminator="kind"),
]
CaptionAnchorTarget = Annotated[
    TimeTarget | PhraseTarget,
    Field(discriminator="kind"),
]
RestoreTarget = Annotated[
    EditTarget | OverlapTarget | AnyTarget | CutOrdinalTarget,
    Field(discriminator="kind"),
]
PauseTarget = Annotated[
    PhraseTarget | WordTarget,
    Field(discriminator="kind"),
]
Destination = Annotated[
    StartTarget | EndTarget | DestinationTimeTarget | DestinationPhraseTarget,
    Field(discriminator="kind"),
]


class CutIntent(StrictModel):
    op: Literal["cut"]
    target: ContentTarget


class KeepOnlyIntent(StrictModel):
    op: Literal["keep_only"]
    target: ContentTarget


class SpeedIntent(StrictModel):
    op: Literal["speed"]
    target: ContentTarget
    factor: Number


class ReorderIntent(StrictModel):
    op: Literal["reorder"]
    target: ContentTarget
    before: Destination


class AddCaptionIntent(StrictModel):
    op: Literal["add_caption"]
    text: str = Field(min_length=1)
    target: CaptionAnchorTarget
    style: dict[str, Any] = Field(default_factory=dict)


class StyleIntent(StrictModel):
    op: Literal["style"]
    target: CaptionTarget
    style: dict[str, Any] = Field(min_length=1)


class MuteIntent(StrictModel):
    op: Literal["mute"]
    target: ContentTarget


class BleepIntent(StrictModel):
    op: Literal["bleep"]
    target: ContentTarget


class RestoreIntent(StrictModel):
    op: Literal["restore"]
    target: RestoreTarget


class RemoveFillersIntent(StrictModel):
    op: Literal["remove_fillers"]


class RemoveSilenceIntent(StrictModel):
    op: Literal["remove_silence"]
    min_gap_s: Number = Field(ge=0)


class KeepPauseIntent(StrictModel):
    op: Literal["keep_pause"]
    target: PauseTarget


class AutoCaptionIntent(StrictModel):
    op: Literal["auto_caption"]
    max_words: int = Field(default=8, ge=1)


class AddMusicIntent(StrictModel):
    op: Literal["add_music"]
    track_id: str = Field(min_length=1)
    duck_db: Number = -12.0
    duck_when: Literal["speech"] = "speech"
    replace: bool = False


class HistoryIntent(StrictModel):
    op: Literal["undo", "redo"]


class RenderIntent(StrictModel):
    op: Literal["render"]


IntentOp = Annotated[
    CutIntent
    | KeepOnlyIntent
    | SpeedIntent
    | ReorderIntent
    | AddCaptionIntent
    | StyleIntent
    | MuteIntent
    | BleepIntent
    | RestoreIntent
    | RemoveFillersIntent
    | RemoveSilenceIntent
    | KeepPauseIntent
    | AutoCaptionIntent
    | AddMusicIntent
    | HistoryIntent
    | RenderIntent,
    Field(discriminator="op"),
]


class IntentOps(StrictModel):
    """The complete LLM/mock response envelope."""

    ops: list[IntentOp] = Field(min_length=1)


def intent_json_schema() -> dict[str, Any]:
    """Return a version-stable schema, omitting the default true object allowance."""
    schema = IntentOps.model_json_schema()

    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            normalized = {key: normalize(item) for key, item in value.items()}
            if normalized.get("additionalProperties") is True:
                del normalized["additionalProperties"]
            return normalized
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    return normalize(schema)


__all__ = [
    "CaptionAnchorTarget",
    "ContentTarget",
    "IntentOp",
    "IntentOps",
    "PauseTarget",
    "RestoreTarget",
    "intent_json_schema",
]
