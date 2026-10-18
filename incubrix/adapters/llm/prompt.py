"""Prompt text for OpenAI-compatible instruction parsing."""

from __future__ import annotations

import json

from incubrix.pipeline.intents import intent_json_schema

PROMPT_VERSION = "intent-ops-v1"


def system_prompt() -> str:
    """Build the parser contract from the source-of-truth pydantic schema."""
    schema = json.dumps(intent_json_schema(), sort_keys=True, separators=(",", ":"))
    return f"""You translate one user instruction into the editing intent schema below.
Never invent timestamps from the transcript; only copy times the user stated.
Resolve words and topics by meaning only as selectors; never output transcript-derived times.
Output ONLY JSON matching this envelope: {{"ops":[...]}}. No markdown or commentary.
Intent-op JSON Schema:
{schema}
Examples:
User: Add a caption 'Welcome!' when I say welcome to the channel
JSON: {{"ops":[{{"op":"add_caption","text":"Welcome!",
  "target":{{"kind":"phrase","text":"welcome to the channel"}}}}]}}
User: Cut the first 30 seconds
JSON: {{"ops":[{{"op":"cut","target":{{"kind":"time","from_start":30}}}}]}}
User: Remove the part about pricing
JSON: {{"ops":[{{"op":"cut","target":{{"kind":"topic","query":"pricing"}}}}]}}
User: Undo
JSON: {{"ops":[{{"op":"undo"}}]}}"""


def user_prompt(instruction: str, context: dict) -> str:
    """Serialize only the instruction and approved compact context fields."""
    compact = {
        "known_caption_texts": context.get("known_caption_texts", []),
        "active_edit_count": context.get("active_edit_count", 0),
        "video_duration_s": context.get("video_duration_s", 0),
    }
    return json.dumps(
        {"instruction": instruction, "context": compact},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
