"""Shared provider-neutral adapter results and errors."""

from __future__ import annotations

from dataclasses import dataclass, field


class AdapterError(Exception):
    """Base class for provider failures with safe, non-secret messages."""


class AdapterConfigurationError(AdapterError):
    """A selected adapter is not installed or is misconfigured."""


class AdapterTransientError(AdapterError):
    """A provider failed transiently and the caller may retry."""

    retryable = True


class InstructionNotUnderstoodError(AdapterError):
    """An LLM response could not be parsed into valid intent operations."""


@dataclass(frozen=True)
class Usage:
    """Usage metadata returned from one adapter invocation."""

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    details: dict[str, int | float | str] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, int | float | str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tokens": self.tokens,
            "latency_ms": round(self.latency_ms, 3),
            **self.details,
        }
