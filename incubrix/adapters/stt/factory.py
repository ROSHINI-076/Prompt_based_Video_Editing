"""STT provider registry driven by application settings."""

from __future__ import annotations

from incubrix.adapters.base import AdapterConfigurationError
from incubrix.adapters.stt.base import STTAdapter
from incubrix.adapters.stt.faster_whisper import FasterWhisperSTTAdapter
from incubrix.adapters.stt.mock import MockSTTAdapter
from incubrix.config import Settings, get_settings


def get_stt_adapter(settings: Settings | None = None) -> STTAdapter:
    """Create the configured STT adapter without loading heavy modules eagerly."""
    config = settings or get_settings()
    if config.STT_PROVIDER == "mock":
        return MockSTTAdapter(config.LOW_CONFIDENCE_THRESHOLD)
    if config.STT_PROVIDER == "faster_whisper":
        return FasterWhisperSTTAdapter(
            config.STT_MODEL,
            config.STT_DEVICE,
            config.LOW_CONFIDENCE_THRESHOLD,
        )
    raise AdapterConfigurationError(f"Unsupported STT provider: {config.STT_PROVIDER}.")
