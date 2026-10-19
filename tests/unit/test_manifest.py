"""Job manifest completeness and secret-exclusion tests."""

from __future__ import annotations

import json

from incubrix.adapters.base import Usage
from incubrix.config import Settings
from incubrix.manifest import build_intake_manifest


def test_intake_manifest_has_required_audit_fields_without_secrets() -> None:
    settings = Settings(
        API_TOKEN="api-test-secret",
        S3_ACCESS_KEY="access-test-secret",
        S3_SECRET_KEY="storage-test-secret",
        LLM_API_KEY="llm-test-secret",
        GROQ_API_KEY="groq-test-secret",
        OPENAI_API_KEY="openai-test-secret",
        HMAC_SECRET="hmac-test-secret",
        CALLBACK_HMAC_SECRET="callback-test-secret",
        STT_PROVIDER="mock",
        LLM_PROVIDER="mock",
        EMBEDDER_PROVIDER="mock",
    )

    manifest = build_intake_manifest(
        job_id="job-1",
        video_id="video-1",
        source_sha256="a" * 64,
        duration_s=60,
        stt_usage=Usage(provider="mock", model="prepared-transcript"),
        stage_timings_s={"transcription_s": 0.25, "probe_s": 0.1},
        warnings=["No audio track was detected."],
        settings=settings,
    )
    serialized = json.dumps(manifest)

    assert manifest["job_id"] == "job-1"
    assert set(manifest["providers"]) == {"stt", "llm", "embedder"}
    assert all(
        {"provider", "model", "version"} <= set(provider)
        for provider in manifest["providers"].values()
    )
    assert manifest["settings"]["min_speed"] == settings.MIN_SPEED
    assert manifest["seed"] is None
    assert manifest["stage_timings_s"] == {
        "probe_s": 0.1,
        "transcription_s": 0.25,
    }
    assert manifest["cost_estimate"]["total"] == 0
    assert manifest["warnings"] == ["No audio track was detected."]
    assert {"license", "commercial_use", "ai_generated"} <= set(manifest["assets"][0])
    assert manifest["ai_generated"] == {
        "source_video": False,
        "transcript": False,
        "rendered_video": False,
    }
    for secret in (
        settings.API_TOKEN,
        settings.S3_ACCESS_KEY,
        settings.S3_SECRET_KEY,
        settings.LLM_API_KEY,
        settings.GROQ_API_KEY,
        settings.OPENAI_API_KEY,
        settings.HMAC_SECRET,
        settings.CALLBACK_HMAC_SECRET,
    ):
        assert secret not in serialized
