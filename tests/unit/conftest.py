"""Shared fixtures for engine unit tests.

Provides a 5-minute synthetic transcript and source info matching
section 13 of the master prompt spec:
  - 0.5 s per word
  - 'welcome to the channel' at 42.0 s
  - 'pricing' mentions at 65 s, 160 s, 195 s
  - two speakers after 200 s
  - fps = 30
"""

from __future__ import annotations

import pytest

from incubrix.engine.models import (
    Sentence,
    SourceInfo,
    Transcript,
    Word,
    make_word_id,
)

FPS = 30.0
DURATION_S = 300.0  # 5 minutes


def _make_word(idx: int, text: str, start_s: float, speaker: str = "Alice") -> Word:
    return Word(
        id=make_word_id(idx),
        text=text,
        start_s=start_s,
        end_s=start_s + 0.4,  # 0.4 s word, 0.1 s gap
        speaker=speaker,
        confidence=1.0,
    )


@pytest.fixture
def source() -> SourceInfo:
    return SourceInfo(
        sha256="abc123",
        duration_frames=int(DURATION_S * FPS),
        fps=FPS,
        has_audio=True,
    )


@pytest.fixture
def transcript() -> Transcript:
    """Build a synthetic 5-minute, 0.5-s-per-word transcript."""
    words: list[Word] = []
    idx = 0

    # Words before the 'welcome' sentence (0–41.5 s)
    t = 0.0
    while t < 42.0:
        words.append(_make_word(idx, "hello", t))
        idx += 1
        t += 0.5

    # 'welcome to the channel' sentence starting at 42.0 s
    for word_text in ("welcome", "to", "the", "channel"):
        words.append(_make_word(idx, word_text, t))
        idx += 1
        t += 0.5

    # Filler until first pricing mention (65 s)
    while t < 65.0:
        words.append(_make_word(idx, "talk", t))
        idx += 1
        t += 0.5

    # First 'pricing' mention at 65 s
    words.append(_make_word(idx, "pricing", t))
    pricing1_idx = idx
    idx += 1
    t += 0.5
    words.append(_make_word(idx, "plan", t))
    idx += 1
    t += 0.5

    # Filler until second pricing mention (160 s)
    while t < 160.0:
        words.append(_make_word(idx, "talk", t))
        idx += 1
        t += 0.5

    # Second 'pricing' mention at 160 s
    words.append(_make_word(idx, "pricing", t))
    pricing2_idx = idx
    idx += 1
    t += 0.5
    words.append(_make_word(idx, "model", t))
    idx += 1
    t += 0.5

    # Filler until third pricing mention (195 s)
    while t < 195.0:
        words.append(_make_word(idx, "talk", t))
        idx += 1
        t += 0.5

    # Third 'pricing' mention at 195 s
    words.append(_make_word(idx, "pricing", t))
    idx += 1
    t += 0.5
    words.append(_make_word(idx, "tier", t))
    idx += 1
    t += 0.5

    # Two speakers after 200 s — switch to "Bob"
    while t < DURATION_S:
        speaker = "Bob" if t >= 200.0 else "Alice"
        words.append(_make_word(idx, "talk", t, speaker=speaker))
        idx += 1
        t += 0.5

    # Build simple sentences (every 10 words)
    sentences = []
    for start in range(0, len(words), 10):
        end = min(start + 9, len(words) - 1)
        sentences.append(Sentence(first_word_idx=start, last_word_idx=end))

    return Transcript(words=words, sentences=sentences, speakers=["Alice", "Bob"])


@pytest.fixture
def welcome_word_ids(transcript: Transcript) -> tuple[str, str]:
    """Return (start_word_id, end_word_id) for 'welcome to the channel'."""
    texts = [w.text for w in transcript.words]
    # find 'welcome' around 42 s
    for i, w in enumerate(transcript.words):
        if w.text == "welcome" and abs(w.start_s - 42.0) < 0.6:
            return (transcript.words[i].id, transcript.words[i + 3].id)
    raise RuntimeError("welcome words not found in fixture")
