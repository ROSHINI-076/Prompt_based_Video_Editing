"""Build safe, auditable job manifests for stored job results."""

from __future__ import annotations

from typing import Any

from incubrix.adapters.base import Usage
from incubrix.config import Settings
from incubrix.pipeline.cost import estimate_llm_cost, estimate_stt_cost


def build_intake_manifest(
    *,
    job_id: str,
    video_id: str,
    source_sha256: str,
    duration_s: float,
    stt_usage: Usage,
    stage_timings_s: dict[str, float],
    warnings: list[str],
    settings: Settings,
) -> dict[str, Any]:
    """Describe an intake job without serializing credentials or endpoint values."""
    llm_usage = Usage(provider=settings.LLM_PROVIDER, model=settings.LLM_MODEL)
    stt_cost = estimate_stt_cost(duration_s / 60, stt_usage.provider, settings)
    llm_cost = estimate_llm_cost(llm_usage, settings)

    return {
        "schema_version": "1.0",
        "job_id": job_id,
        "job_type": "intake_and_transcribe",
        "video_id": video_id,
        "source": {
            "sha256": source_sha256,
            "duration_s": round(duration_s, 3),
        },
        "providers": {
            "stt": {
                "provider": stt_usage.provider,
                "model": stt_usage.model,
                "version": stt_usage.details.get("version", "not-reported"),
            },
            "llm": {
                "provider": settings.LLM_PROVIDER,
                "model": settings.LLM_MODEL,
                "version": "not-invoked",
            },
            "embedder": {
                "provider": settings.EMBEDDER_PROVIDER,
                "model": settings.EMBEDDER_MODEL,
                "version": "not-invoked",
            },
        },
        "settings": {
            "min_speed": settings.MIN_SPEED,
            "max_speed": settings.MAX_SPEED,
            "min_caption_s": settings.MIN_CAPTION_S,
            "low_confidence_threshold": settings.LOW_CONFIDENCE_THRESHOLD,
            "max_upload_size_mb": settings.MAX_UPLOAD_SIZE_MB,
            "url_fetch_timeout_s": settings.URL_FETCH_TIMEOUT_S,
        },
        "seed": None,
        "stage_timings_s": {
            name: round(seconds, 6) for name, seconds in sorted(stage_timings_s.items())
        },
        "cost_estimate": {
            "currency": "USD",
            "stt": stt_cost,
            "llm": llm_cost,
            "total": round(stt_cost + llm_cost, 8),
        },
        "warnings": list(warnings),
        "assets": [
            {
                "name": "source_video",
                "kind": "user_media",
                "license": "Uploader-provided; rights not independently verified.",
                "commercial_use": "As authorized by the uploader.",
                "ai_generated": False,
            },
            {
                "name": stt_usage.model,
                "kind": "speech_to_text_model",
                "license": _model_license(stt_usage.provider),
                "commercial_use": _commercial_use(stt_usage.provider),
                "ai_generated": False,
            },
            {
                "name": settings.LLM_MODEL,
                "kind": "instruction_parser_model",
                "license": _model_license(settings.LLM_PROVIDER),
                "commercial_use": _commercial_use(settings.LLM_PROVIDER),
                "ai_generated": False,
            },
            {
                "name": settings.EMBEDDER_MODEL,
                "kind": "embedding_model",
                "license": _model_license(settings.EMBEDDER_PROVIDER),
                "commercial_use": _commercial_use(settings.EMBEDDER_PROVIDER),
                "ai_generated": False,
            },
            {
                "name": "transcript",
                "kind": "transcription_output",
                "license": "Generated from the source media; not separately licensed.",
                "commercial_use": "Subject to source-media rights.",
                "ai_generated": stt_usage.provider != "mock",
            },
        ],
        "ai_generated": {
            "source_video": False,
            "transcript": stt_usage.provider != "mock",
            "rendered_video": False,
        },
    }


def _model_license(provider: str) -> str:
    if provider == "mock":
        return "No external model; deterministic mock adapter."
    if provider in {"faster_whisper", "local"}:
        return "Model artifact license must be verified for the configured model."
    return "Provider-managed; consult the selected provider's current terms."


def _commercial_use(provider: str) -> str:
    if provider == "mock":
        return "Permitted; no external model is used."
    return "Not asserted; verify the configured model or provider terms before commercial use."
