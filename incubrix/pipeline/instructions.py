"""Natural-language parsing, caching and orchestration around the pure engine."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from pydantic import ValidationError

from incubrix.adapters.base import InstructionNotUnderstoodError, Usage
from incubrix.adapters.embed.base import Embedder
from incubrix.adapters.embed.factory import get_embedder
from incubrix.adapters.llm.base import LLMAdapter
from incubrix.adapters.llm.cache import MemoryParseCache, ParseCache, parse_cache_key
from incubrix.adapters.llm.factory import get_llm_adapter
from incubrix.adapters.llm.prompt import PROMPT_VERSION
from incubrix.config import Settings, get_settings
from incubrix.engine.answers import answer as answer_instruction
from incubrix.engine.answers import handle_instruction
from incubrix.engine.session import SessionManager
from incubrix.pipeline.cost import estimate_llm_cost
from incubrix.pipeline.intents import IntentOps

_DEFAULT_CACHE = MemoryParseCache()


def _compact_context(session: SessionManager) -> dict[str, Any]:
    """Expose only non-transcript context permitted for parser requests."""
    output = session.get_timeline_output()
    captions = sorted({item["text"] for item in output["overlays"]})
    return {
        "known_caption_texts": captions,
        "active_edit_count": len(session.state.stack),
        "video_duration_s": round(session.get_duration_frames() / session.state.source.fps, 3),
    }


def _usage_block(usage: Usage, settings: Settings, cache_hit: bool = False) -> dict:
    result = usage.as_dict()
    result["estimated_cost"] = estimate_llm_cost(usage, settings)
    result["cache_hit"] = cache_hit
    return result


def _rejected_parse(session: SessionManager, usage: Usage) -> dict:
    """Project parser failure into the engine's stable three-way response shape."""
    result = handle_instruction(session, [])
    result.update(
        {
            "status": "rejected",
            "summary": "Instruction could not be understood; no changes were applied.",
            "warnings": [],
            "error": {
                "code": "INSTRUCTION_NOT_UNDERSTOOD",
                "message": "Instruction could not be translated into a supported operation.",
                "details": None,
            },
        }
    )
    result["usage"] = usage.as_dict()
    return result


def run_instruction(
    session: SessionManager,
    text: str,
    answer: str | None = None,
    *,
    llm: LLMAdapter | None = None,
    embedder: Embedder | None = None,
    cache: ParseCache | None = None,
    settings: Settings | None = None,
) -> dict:
    """Parse and apply a user instruction, routing pending answers without an LLM call."""
    config = settings or get_settings()
    actual_embedder = embedder or get_embedder(config)
    if answer is not None or session.state.pending_question is not None:
        answer_text = answer if answer is not None else text
        result = answer_instruction(
            session,
            answer_text,
            embedder=actual_embedder.similarity,
        )
        result["usage"] = Usage(
            provider="none",
            model="none",
        ).as_dict()
        return result

    adapter = llm or get_llm_adapter(config)
    owns_adapter = llm is None
    context = _compact_context(session)
    key = parse_cache_key(
        adapter.provider,
        adapter.model,
        PROMPT_VERSION,
        text,
        context,
    )
    parse_cache = cache or _DEFAULT_CACHE
    cached_ops = parse_cache.get(key)
    cache_hit = cached_ops is not None
    usage = Usage(provider=adapter.provider, model=adapter.model)
    if cached_ops is None:
        try:
            parsed = adapter.parse(text, context)
            validated = IntentOps.model_validate({"ops": parsed.ops})
        except (InstructionNotUnderstoodError, ValidationError):
            return _rejected_parse(session, usage)
        finally:
            if owns_adapter:
                adapter.close()
        cached_ops = [
            operation for operation in validated.model_dump(mode="json", exclude_none=True)["ops"]
        ]
        parse_cache.set(key, cached_ops)
        usage = parsed.usage
    else:
        if owns_adapter:
            adapter.close()
        cached_ops = copy.deepcopy(cached_ops)

    if cached_ops == [{"op": "render"}]:
        result = {
            "status": "applied",
            "operations": [{"op": "render"}],
            "summary": "Render request recognized; rendering is not implemented in this phase.",
            "question": None,
            "options": None,
            "warnings": ["No video was rendered; the renderer is a later project phase."],
            "timeline_hash": session.get_timeline_hash(),
            "duration_s": round(session.get_duration_frames() / session.state.source.fps, 3),
            "time_policy": "current",
            "edit_id": None,
            "error": None,
        }
    else:
        result = handle_instruction(
            session,
            cached_ops,
            instruction=text,
            embedder=actual_embedder.similarity,
        )
    result["usage"] = _usage_block(usage, config, cache_hit)
    return result


def compact_context_digest(context: dict[str, Any]) -> str:
    """Expose a deterministic context digest for callers and diagnostics."""
    payload = json.dumps(context, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
