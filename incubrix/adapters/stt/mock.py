"""Offline STT adapter that reads a prepared transcript JSON file."""

from __future__ import annotations

import json
from pathlib import Path

from incubrix.adapters.stt.base import TranscriptionResult, transcript_from_data


class MockSTTAdapter:
    """Load a deterministic prepared transcript from disk."""

    def __init__(self, low_confidence_threshold: float = 0.6) -> None:
        self.low_confidence_threshold = low_confidence_threshold

    def transcribe(self, media_path: str | Path) -> TranscriptionResult:
        """Read transcript JSON and normalize stable word IDs."""
        path = Path(media_path)
        with path.open(encoding="utf-8") as transcript_file:
            data = json.load(transcript_file)
        if not isinstance(data, dict):
            raise ValueError("Prepared transcript JSON must contain an object.")
        return transcript_from_data(
            data,
            low_confidence_threshold=self.low_confidence_threshold,
            model="prepared-transcript",
        )
