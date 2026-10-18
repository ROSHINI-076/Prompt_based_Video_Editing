"""Embedder provider registry driven by application settings."""

from __future__ import annotations

from incubrix.adapters.base import AdapterConfigurationError
from incubrix.adapters.embed.base import Embedder
from incubrix.adapters.embed.local import SentenceTransformersEmbedder
from incubrix.adapters.embed.mock import MockEmbedder
from incubrix.config import Settings, get_settings


def get_embedder(settings: Settings | None = None) -> Embedder:
    """Create the configured embedder; optional dependencies remain lazy."""
    config = settings or get_settings()
    if config.EMBEDDER_PROVIDER == "mock":
        return MockEmbedder(config.EMBEDDER_DIMENSIONS, config.EMBEDDER_MODEL)
    if config.EMBEDDER_PROVIDER == "local":
        return SentenceTransformersEmbedder(config.EMBEDDER_MODEL)
    raise AdapterConfigurationError(f"Unsupported embedder provider: {config.EMBEDDER_PROVIDER}.")
