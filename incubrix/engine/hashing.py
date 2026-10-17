"""Deterministic timeline hashing.

timeline_hash = sha256(canonical_json(timeline_without_warnings))

Canonical JSON rules:
- Sorted keys at every level
- Compact separators (',', ':') — no spaces
- Excludes 'warnings' field (may contain non-deterministic text)
- All numbers derived from integer frames (no float drift)

The hash is the primary determinism guarantee: the same source + stack
must produce the same hash on every run.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Produce deterministic JSON: sorted keys, compact separators."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _strip_warnings(obj: Any) -> Any:
    """Recursively remove all 'warnings' keys from a dict/list structure."""
    if isinstance(obj, dict):
        return {k: _strip_warnings(v) for k, v in obj.items() if k != "warnings"}
    if isinstance(obj, list):
        return [_strip_warnings(item) for item in obj]
    return obj


def timeline_to_hashable(timeline) -> dict:
    """Convert a Timeline to a dict suitable for hashing.

    Excludes the 'warnings' field, which may contain non-deterministic
    or run-specific text.
    """
    data = timeline.model_dump()
    return _strip_warnings(data)


def timeline_hash(timeline) -> str:
    """Compute the SHA-256 hash of the timeline's canonical JSON.

    This is the determinism guarantee: same source info + same edit stack
    → same timeline_hash on every run.
    """
    hashable = timeline_to_hashable(timeline)
    canonical = canonical_json(hashable)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
