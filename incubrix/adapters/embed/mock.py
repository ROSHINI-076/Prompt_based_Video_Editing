"""Stable hash-vector embedder for offline tests and default operation."""

from __future__ import annotations

import hashlib
import re

from incubrix.adapters.embed.base import cosine_similarity


class MockEmbedder:
    """Build deterministic bag-of-token vectors without model downloads."""

    provider = "mock"

    def __init__(self, dimensions: int = 128, model: str = "hashing-v1") -> None:
        if dimensions < 1:
            raise ValueError("Mock embedding dimensions must be positive.")
        self.dimensions = dimensions
        self.model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Hash normalized tokens into a fixed-size signed vector."""
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in re.findall(r"[a-z0-9']+", text.casefold()):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimensions
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[index] += sign
            vectors.append(vector)
        return vectors

    def similarity(self, query: str, text: str) -> float:
        """Compare two strings using the same deterministic token vectors."""
        query_vector, text_vector = self.embed([query, text])
        return cosine_similarity(query_vector, text_vector)
