"""Embedder contract and cosine similarity helper."""

from __future__ import annotations

from math import sqrt
from typing import Protocol


class Embedder(Protocol):
    """Create text vectors and score query/text pairs."""

    provider: str
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def similarity(self, query: str, text: str) -> float: ...


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Return cosine similarity, defining an empty vector as zero similarity."""
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sqrt(sum(value * value for value in left))
    right_norm = sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


__all__ = ["Embedder", "cosine_similarity"]
