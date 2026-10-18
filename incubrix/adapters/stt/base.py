"""Speech-to-text adapter contract and transcript normalization."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from incubrix.adapters.base import Usage
from incubrix.engine.models import Sentence, Transcript, Word, make_word_id


@dataclass(frozen=True)
class TranscriptionResult:
    """A normalized transcript plus provider usage."""

    transcript: Transcript
    usage: Usage


class STTAdapter(Protocol):
    """Transcribe media into deterministic word-level data."""

    def transcribe(self, media_path: str | Path) -> TranscriptionResult: ...


def transcript_from_data(
    data: Mapping,
    *,
    low_confidence_threshold: float,
    model: str,
    latency_ms: float = 0.0,
) -> TranscriptionResult:
    """Assign stable word IDs, confidence flags and sentence boundaries."""
    raw_words = data.get("words", [])
    if not isinstance(raw_words, list):
        raise ValueError("Transcript JSON field 'words' must be a list.")
    words: list[Word] = []
    for index, raw in enumerate(raw_words):
        if not isinstance(raw, Mapping):
            raise ValueError("Each transcript word must be an object.")
        confidence = float(raw.get("confidence", 1.0))
        flags = list(raw.get("flags", []))
        if confidence < low_confidence_threshold and "low_confidence" not in flags:
            flags.append("low_confidence")
        words.append(
            Word(
                id=make_word_id(index),
                text=str(raw["text"]),
                start_s=float(raw["start_s"]),
                end_s=float(raw["end_s"]),
                speaker=str(raw.get("speaker", "default")),
                confidence=confidence,
                flags=flags,
            )
        )

    raw_sentences = data.get("sentences")
    if raw_sentences:
        sentences = [
            Sentence(
                first_word_idx=int(item["first_word_idx"]),
                last_word_idx=int(item["last_word_idx"]),
            )
            for item in raw_sentences
        ]
    else:
        sentences = _derive_sentences(words)
    transcript = Transcript(
        words=words,
        sentences=sentences,
        speakers=list(data.get("speakers", sorted({word.speaker for word in words}))),
        warnings=list(data.get("warnings", [])),
    )
    return TranscriptionResult(
        transcript=transcript,
        usage=Usage(provider=str(data.get("provider", "mock")), model=model, latency_ms=latency_ms),
    )


def _derive_sentences(words: list[Word]) -> list[Sentence]:
    """Split at terminal punctuation or a one-second pause."""
    sentences: list[Sentence] = []
    first = 0
    for index, word in enumerate(words):
        ends_sentence = word.text.rstrip("\"')]}").endswith((".", "?", "!"))
        long_pause = index + 1 < len(words) and words[index + 1].start_s - word.end_s >= 1.0
        if ends_sentence or long_pause:
            sentences.append(Sentence(first_word_idx=first, last_word_idx=index))
            first = index + 1
    if first < len(words):
        sentences.append(Sentence(first_word_idx=first, last_word_idx=len(words) - 1))
    return sentences


__all__ = ["STTAdapter", "TranscriptionResult", "transcript_from_data"]
