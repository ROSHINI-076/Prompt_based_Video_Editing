"""Optional local faster-whisper adapter (imported only when selected)."""

from __future__ import annotations

import time
from pathlib import Path

from incubrix.adapters.base import AdapterConfigurationError, Usage
from incubrix.adapters.stt.base import TranscriptionResult, transcript_from_data


class FasterWhisperSTTAdapter:
    """Run local faster-whisper with word timestamps."""

    def __init__(
        self,
        model_name: str,
        device: str = "cpu",
        low_confidence_threshold: float = 0.6,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.low_confidence_threshold = low_confidence_threshold
        self._model = None

    def transcribe(self, media_path: str | Path) -> TranscriptionResult:
        """Transcribe lazily so mock-only installs remain lightweight and offline."""
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise AdapterConfigurationError(
                    "faster-whisper is not installed; install the 'stt' extra."
                ) from exc
            self._model = WhisperModel(self.model_name, device=self.device)

        started = time.perf_counter()
        segments, info = self._model.transcribe(str(media_path), word_timestamps=True)
        words: list[dict] = []
        for segment in segments:
            if not segment.words:
                raise ValueError("faster-whisper returned a segment without word timestamps.")
            for item in segment.words:
                probability = float(item.probability or 0.0)
                words.append(
                    {
                        "text": item.word.strip(),
                        "start_s": float(item.start),
                        "end_s": float(item.end),
                        "speaker": "default",
                        "confidence": probability,
                    }
                )
        result = transcript_from_data(
            {"words": words},
            low_confidence_threshold=self.low_confidence_threshold,
            model=self.model_name,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        duration_s = float(getattr(info, "duration", 0.0))
        return TranscriptionResult(
            transcript=result.transcript,
            usage=Usage(
                provider="faster_whisper",
                model=self.model_name,
                latency_ms=result.usage.latency_ms,
                details={"audio_duration_s": duration_s},
            ),
        )
