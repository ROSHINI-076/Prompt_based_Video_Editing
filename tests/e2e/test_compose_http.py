"""End-to-end verification against the real Docker Compose API and worker."""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

from incubrix.config import Settings
from tests.media import generate_video

pytestmark = pytest.mark.e2e


def test_compose_upload_job_manifest_and_http_result_links(tmp_path) -> None:
    base_url = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000")
    token = Settings().API_TOKEN or os.environ.get("API_TOKEN", "")
    assert token, "API_TOKEN must be configured by the test runner."
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Correlation-Id": "compose-e2e",
        "Idempotency-Key": f"compose-e2e-{uuid.uuid4().hex}",
    }

    with httpx.Client(base_url=base_url, timeout=10) as client:
        health = client.get("/health", headers=headers)
        assert health.status_code == 200, health.text
        assert health.headers["x-correlation-id"] == "compose-e2e"
        readiness = client.get("/ready", headers=headers)
        assert readiness.status_code == 200, readiness.text

        source = tmp_path / "compose-e2e.mp4"
        generate_video(source)
        with source.open("rb") as media:
            accepted = client.post(
                "/api/v1/videos",
                files={"file": (source.name, media, "video/mp4")},
                headers=headers,
            )
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]

        deadline = time.monotonic() + 90
        job = None
        while time.monotonic() < deadline:
            response = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
            assert response.status_code == 200, response.text
            job = response.json()
            if job["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.25)

        assert job is not None
        assert job["status"] == "succeeded", job
        assert job["progress"] == 100
        result = job["result"]
        assert result["manifest"]["key"] == f"manifests/jobs/{job_id}.json"
        assert result["manifest"]["url"].startswith("http")
        video = client.get(f"/api/v1/videos/{result['video_id']}", headers=headers)
        transcript = client.get(f"/api/v1/videos/{result['video_id']}/transcript", headers=headers)
        assert video.status_code == 200, video.text
        assert transcript.status_code == 200, transcript.text
