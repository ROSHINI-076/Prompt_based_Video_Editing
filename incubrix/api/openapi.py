"""OpenAPI schemas shared by the live application and committed contract."""

from __future__ import annotations

from typing import Any

ERROR_RESPONSE = {
    "description": "Standard API error.",
    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorEnvelope"}}},
}

ERROR_SCHEMA = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {"$ref": "#/components/schemas/Error"},
    },
}

ERROR_DETAILS_SCHEMA = {
    "oneOf": [
        {"type": "object", "additionalProperties": True},
        {"type": "array", "items": {}},
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
    ]
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "ValidationError": {
        "type": "object",
        "title": "ValidationError",
        "required": ["loc", "msg", "type"],
        "properties": {
            "loc": {
                "type": "array",
                "title": "Location",
                "items": {
                    "anyOf": [{"type": "string"}, {"type": "integer"}],
                },
            },
            "msg": {"type": "string", "title": "Message"},
            "type": {"type": "string", "title": "Error Type"},
        },
    },
    "HTTPValidationError": {
        "type": "object",
        "title": "HTTPValidationError",
        "properties": {
            "detail": {
                "type": "array",
                "title": "Detail",
                "items": {"$ref": "#/components/schemas/ValidationError"},
            }
        },
    },
    "Error": {
        "type": "object",
        "required": ["code", "message", "retryable", "details"],
        "properties": {
            "code": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"},
            "message": {"type": "string"},
            "retryable": {"type": "boolean"},
            "details": ERROR_DETAILS_SCHEMA,
        },
    },
    "ErrorEnvelope": ERROR_SCHEMA,
    "VideoMetadata": {
        "type": "object",
        "required": ["id", "sha256", "duration_s", "fps", "has_audio", "status"],
        "properties": {
            "id": {"type": "string"},
            "sha256": {"type": "string"},
            "duration_s": {"type": "number"},
            "fps": {"type": "number"},
            "has_audio": {"type": "boolean"},
            "status": {"type": "string"},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "source_key": {"type": "string"},
        },
    },
    "TranscriptResponse": {
        "type": "object",
        "required": ["video_id", "transcript"],
        "properties": {
            "video_id": {"type": "string"},
            "transcript": {"type": "object", "additionalProperties": True},
        },
    },
    "SessionResponse": {
        "type": "object",
        "required": ["session_id", "video_id", "version"],
        "properties": {
            "session_id": {"type": "string"},
            "video_id": {"type": "string"},
            "version": {"type": "integer"},
            "timeline_hash": {"type": "string"},
            "duration_s": {"type": "number"},
        },
    },
    "TimelineResponse": {
        "type": "object",
        "required": ["source", "segments", "overlays", "timeline_hash"],
        "properties": {
            "source": {"type": "object", "additionalProperties": True},
            "segments": {"type": "array", "items": {"type": "object"}},
            "overlays": {"type": "array", "items": {"type": "object"}},
            "overlay_displays": {"type": "array", "items": {"type": "object"}},
            "effects": {"type": "array", "items": {"type": "object"}},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "timeline_hash": {"type": "string"},
        },
    },
    "HistoryResponse": {
        "type": "object",
        "required": ["stack", "redo", "edit_counter"],
        "properties": {
            "stack": {"type": "array", "items": {"type": "object"}},
            "redo": {"type": "array", "items": {"type": "object"}},
            "edit_counter": {"type": "integer"},
        },
    },
    "JobResponse": {
        "type": "object",
        "required": ["job_id", "type", "status", "progress", "result", "error"],
        "properties": {
            "job_id": {"type": "string"},
            "type": {"type": "string"},
            "status": {"type": "string", "enum": ["queued", "running", "succeeded", "failed"]},
            "progress": {"type": "integer", "minimum": 0, "maximum": 100},
            "result": {"type": ["object", "null"], "additionalProperties": True},
            "error": {"type": ["object", "null"], "additionalProperties": True},
            "attempts": {"type": "integer"},
            "max_attempts": {"type": "integer"},
        },
    },
    "JobAccepted": {
        "type": "object",
        "required": ["job_id"],
        "properties": {"job_id": {"type": "string"}},
    },
    "VideoUrlRequest": {
        "type": "object",
        "required": ["url"],
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "callback_url": {"type": "string", "format": "uri"},
        },
        "additionalProperties": False,
    },
    "SessionRequest": {
        "type": "object",
        "required": ["video_id"],
        "properties": {"video_id": {"type": "string"}},
        "additionalProperties": False,
    },
    "InstructionRequest": {
        "type": "object",
        "properties": {
            "say": {"type": "string"},
            "answer": {"type": "string"},
        },
        "oneOf": [
            {"required": ["say"]},
            {"required": ["answer"]},
        ],
        "additionalProperties": False,
    },
    "InstructionResult": {
        "type": "object",
        "required": ["status", "operations", "summary", "question", "options", "warnings"],
        "properties": {
            "status": {
                "type": "string",
                "enum": ["applied", "needs_clarification", "rejected"],
            },
            "operations": {"type": "array", "items": {"type": "object"}},
            "summary": {"type": "string"},
            "question": {"type": ["string", "null"]},
            "options": {"type": ["array", "null"], "items": {}},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "timeline_hash": {"type": ["string", "null"]},
            "duration_s": {"type": ["number", "null"]},
            "time_policy": {"type": ["string", "null"], "enum": ["current", "original", None]},
            "edit_id": {"type": ["string", "null"]},
            "error": {"type": ["object", "null"]},
        },
    },
}


def apply_openapi_contract(document: dict[str, Any]) -> dict[str, Any]:
    """Add explicit request, success, and standard-error schemas to app routes."""
    document["openapi"] = "3.1.0"
    components = document.setdefault("components", {})
    components.setdefault("schemas", {}).update(SCHEMAS)
    components.setdefault("securitySchemes", {})["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
    }

    for path, path_item in document["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post"}:
                continue
            if path.startswith("/api/v1/"):
                operation["security"] = [{"BearerAuth": []}]
                if method == "post":
                    operation.setdefault("parameters", []).append(
                        {
                            "name": "Idempotency-Key",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string", "maxLength": 255},
                        }
                    )
            _document_request_body(path, method, operation)
            success_status, response_schema = _response_contract(path, method)
            responses = operation.setdefault("responses", {})
            responses[success_status] = {
                "description": _success_description(path, method),
                "content": {
                    "application/json": {"schema": response_schema},
                },
            }
            for status in ("401", "404", "405", "409", "413", "422", "429", "500", "503"):
                if status == "404" and path in {"/health", "/ready"}:
                    continue
                responses[status] = ERROR_RESPONSE

    return document


def _document_request_body(path: str, method: str, operation: dict[str, Any]) -> None:
    if method != "post":
        return
    if path == "/api/v1/videos":
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/VideoUrlRequest"}},
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "url": {"type": "string", "format": "uri"},
                            "callback_url": {"type": "string", "format": "uri"},
                        },
                        "oneOf": [{"required": ["file"]}, {"required": ["url"]}],
                    }
                },
            },
        }
    elif path == "/api/v1/sessions":
        operation["requestBody"] = _json_request_body("SessionRequest")
    elif path.endswith("/instructions"):
        operation["requestBody"] = _json_request_body("InstructionRequest")


def _json_request_body(schema_name: str) -> dict[str, Any]:
    return {
        "required": True,
        "content": {
            "application/json": {"schema": {"$ref": f"#/components/schemas/{schema_name}"}}
        },
    }


def _response_contract(path: str, method: str) -> tuple[str, dict[str, Any]]:
    if method == "post" and path in {
        "/api/v1/videos",
        "/api/v1/sessions/{session_id}/render",
    }:
        return "202", {"$ref": "#/components/schemas/JobAccepted"}
    if method == "post" and path.endswith("/instructions"):
        return "200", {"$ref": "#/components/schemas/InstructionResult"}
    if method == "get" and path.startswith("/api/v1/videos/{video_id}/transcript"):
        return "200", {"$ref": "#/components/schemas/TranscriptResponse"}
    if method == "get" and path == "/api/v1/videos/{video_id}":
        return "200", {"$ref": "#/components/schemas/VideoMetadata"}
    if method == "post" and path == "/api/v1/sessions":
        return "200", {"$ref": "#/components/schemas/SessionResponse"}
    if method == "get" and path == "/api/v1/sessions/{session_id}":
        return "200", {"$ref": "#/components/schemas/SessionResponse"}
    if method == "get" and path.endswith("/timeline"):
        return "200", {"$ref": "#/components/schemas/TimelineResponse"}
    if method == "get" and path.endswith("/history"):
        return "200", {"$ref": "#/components/schemas/HistoryResponse"}
    if method == "get" and path.startswith("/api/v1/jobs/"):
        return "200", {"$ref": "#/components/schemas/JobResponse"}
    if method == "get" and path == "/health":
        return "200", {"type": "object", "properties": {"status": {"type": "string"}}}
    if method == "get" and path == "/ready":
        return "200", {"type": "object", "properties": {"status": {"type": "string"}}}
    return "200", {"type": "object", "additionalProperties": True}


def _success_description(path: str, method: str) -> str:
    if method == "post" and path.endswith("/render"):
        return "Render job accepted. Rendering is not implemented until Stage 5."
    if method == "post" and path == "/api/v1/videos":
        return "Video intake and transcription job accepted."
    if method == "post" and path.endswith("/instructions"):
        return "Instruction result: applied, needs clarification, or rejected."
    return "Successful response."
