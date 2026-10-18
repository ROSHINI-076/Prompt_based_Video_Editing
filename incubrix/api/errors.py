"""Standard HTTP error registry and helpers for the IncuBrix API."""

from __future__ import annotations

from typing import Any


class APIError(Exception):
    """Application-level API error with explicit status and retry metadata."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        retryable: bool = False,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.details = details

    def as_payload(self) -> dict[str, Any]:
        """Return the standard external error body."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
            }
        }


ERRORS: dict[str, dict[str, Any]] = {
    "VALIDATION_ERROR": {"status": 422, "retryable": False},
    "NOT_FOUND": {"status": 404, "retryable": False},
    "UNAUTHORIZED": {"status": 401, "retryable": False},
    "PAYLOAD_TOO_LARGE": {"status": 413, "retryable": False},
    "RATE_LIMITED": {"status": 429, "retryable": False},
    "IDEMPOTENCY_KEY_REUSED": {"status": 409, "retryable": False},
    "SESSION_CONFLICT": {"status": 409, "retryable": True},
    "DATABASE_UNAVAILABLE": {"status": 503, "retryable": True},
    "STORAGE_UNAVAILABLE": {"status": 503, "retryable": True},
    "LLM_UNAVAILABLE": {"status": 503, "retryable": True},
    "INTERNAL_ERROR": {"status": 500, "retryable": True},
    "NOT_IMPLEMENTED": {"status": 501, "retryable": False},
    "INVALID_URL": {"status": 400, "retryable": False},
    "INVALID_MEDIA": {"status": 400, "retryable": False},
    "VIDEO_NOT_FOUND": {"status": 404, "retryable": False},
    "NO_TARGET": {"status": 400, "retryable": False},
    "OUT_OF_RANGE": {"status": 400, "retryable": False},
    "INVALID_SPEED": {"status": 400, "retryable": False},
    "INSTRUCTION_NOT_UNDERSTOOD": {"status": 200, "retryable": False},
    "CUT_ALL": {"status": 200, "retryable": False},
}


def api_error(
    code: str,
    message: str,
    *,
    status: int | None = None,
    retryable: bool | None = None,
    details: Any = None,
) -> APIError:
    """Build an APIError from the registry or explicit arguments."""
    metadata = ERRORS.get(code, {"status": 500, "retryable": True})
    return APIError(
        code,
        message,
        status=status if status is not None else metadata["status"],
        retryable=retryable if retryable is not None else metadata["retryable"],
        details=details,
    )
