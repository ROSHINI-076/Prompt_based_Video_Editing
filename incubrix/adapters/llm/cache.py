"""Parsed-operation cache interface and deterministic in-memory implementation."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Protocol


class ParseCache(Protocol):
    """Store validated operation lists by deterministic digest."""

    def get(self, key: str) -> list[dict] | None: ...

    def set(self, key: str, ops: list[dict]) -> None: ...


class MemoryParseCache:
    """In-memory parse cache that protects stored results from mutation."""

    def __init__(self) -> None:
        self._entries: dict[str, list[dict]] = {}

    def get(self, key: str) -> list[dict] | None:
        """Return a defensive copy for a cache hit."""
        value = self._entries.get(key)
        return copy.deepcopy(value) if value is not None else None

    def set(self, key: str, ops: list[dict]) -> None:
        """Persist an independent copy of the parsed operations."""
        self._entries[key] = copy.deepcopy(ops)


def parse_cache_key(
    provider: str,
    model: str,
    prompt_version: str,
    instruction: str,
    context: dict,
) -> str:
    """Hash provider, model, prompt version, normalized text and compact context."""
    normalized = re.sub(r"\s+", " ", instruction).strip().casefold()
    context_digest = hashlib.sha256(
        json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()
    material = json.dumps(
        [provider, model, prompt_version, normalized, context_digest],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
