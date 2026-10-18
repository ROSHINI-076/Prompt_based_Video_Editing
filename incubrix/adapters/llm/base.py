"""LLM parser contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from incubrix.adapters.base import Usage


@dataclass(frozen=True)
class ParseResult:
    """Validated operations and model-usage metadata."""

    ops: list[dict[str, Any]]
    usage: Usage


class LLMAdapter(Protocol):
    """Translate natural language into validated structured intent only."""

    provider: str
    model: str

    def parse(self, instruction: str, context: dict[str, Any]) -> ParseResult: ...

    def close(self) -> None: ...


__all__ = ["LLMAdapter", "ParseResult"]
