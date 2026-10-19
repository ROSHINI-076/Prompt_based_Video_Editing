"""OpenAPI contract generation and route coverage tests."""

from __future__ import annotations

from pathlib import Path

import yaml

from incubrix.api.app import app


def test_committed_openapi_yaml_matches_live_schema() -> None:
    committed = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "openapi.yaml").read_text(encoding="utf-8")
    )

    assert committed == app.openapi()
    assert committed["openapi"] == "3.1.0"


def test_all_api_operations_document_security_responses_and_post_bodies() -> None:
    schema = app.openapi()
    api_routes = {
        (path, method)
        for path, path_item in schema["paths"].items()
        if path.startswith("/api/v1/")
        for method in path_item
        if method in {"get", "post"}
    }
    assert api_routes == {
        ("/api/v1/videos", "post"),
        ("/api/v1/videos/{video_id}", "get"),
        ("/api/v1/videos/{video_id}/transcript", "get"),
        ("/api/v1/sessions", "post"),
        ("/api/v1/sessions/{session_id}", "get"),
        ("/api/v1/sessions/{session_id}/instructions", "post"),
        ("/api/v1/sessions/{session_id}/timeline", "get"),
        ("/api/v1/sessions/{session_id}/history", "get"),
        ("/api/v1/sessions/{session_id}/render", "post"),
        ("/api/v1/jobs/{job_id}", "get"),
    }
    for path, method in api_routes:
        operation = schema["paths"][path][method]
        assert operation["security"] == [{"BearerAuth": []}]
        assert "200" in operation["responses"] or "202" in operation["responses"]
        assert "401" in operation["responses"]
        if method == "post":
            assert any(
                parameter["name"] == "Idempotency-Key" and parameter["required"]
                for parameter in operation["parameters"]
            )
        if method == "post" and path != "/api/v1/sessions/{session_id}/render":
            assert "requestBody" in operation
    assert "/health" in schema["paths"]
    assert "/ready" in schema["paths"]
