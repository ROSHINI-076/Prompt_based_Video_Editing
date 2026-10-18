"""Optional sentence-transformers embedder with lazy model loading."""

from __future__ import annotations

from incubrix.adapters.base import AdapterConfigurationError
from incubrix.adapters.embed.base import cosine_similarity


class SentenceTransformersEmbedder:
    """Load the configured sentence-transformers model on first use."""

    provider = "sentence_transformers"

    def __init__(self, model_name: str) -> None:
        self.model = model_name
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise AdapterConfigurationError(
                    "sentence-transformers is not installed; install the 'embed' extra."
                ) from exc
            self._model = SentenceTransformer(self.model)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Encode text locally; no model is downloaded at module import time."""
        vectors = self._load().encode(texts, convert_to_numpy=True)
        return [[float(value) for value in vector] for vector in vectors]

    def similarity(self, query: str, text: str) -> float:
        """Return cosine similarity between the query and candidate sentence."""
        left, right = self.embed([query, text])
        return cosine_similarity(left, right)
