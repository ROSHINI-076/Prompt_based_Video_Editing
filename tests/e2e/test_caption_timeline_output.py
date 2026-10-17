"""End-to-end checks for caption timing in the public engine output."""

from __future__ import annotations

from incubrix.engine.models import (
    AddCaptionOp,
    SourceInfo,
    SpeedOp,
    Transcript,
    Word,
    make_word_id,
)
from incubrix.engine.session import SessionManager


def test_replayed_4x_caption_uses_min_duration_in_public_output():
    """Replay plus output serialization must preserve the minimum display time."""
    fps = 30.0
    transcript = Transcript(
        words=[
            Word(
                id=make_word_id(i),
                text=f"word{i}",
                start_s=i * 0.5,
                end_s=i * 0.5 + 0.4,
            )
            for i in range(20)
        ]
    )
    session = SessionManager(
        SourceInfo(sha256="fixture", duration_frames=300, fps=fps),
        transcript,
    )
    session.push_edit(
        "caption words 2-3",
        [
            AddCaptionOp(
                overlay_id="ov1",
                text="Caption",
                start_word_id=make_word_id(2),
                end_word_id=make_word_id(3),
            )
        ],
    )
    session.push_edit(
        "speed caption range 4x",
        [SpeedOp(range=(30, 60), factor=4.0)],
    )

    outputs = [session.get_timeline_output() for _ in range(3)]
    displays = [output["overlay_displays"][0] for output in outputs]

    assert displays[0]["end_s"] - displays[0]["start_s"] >= 0.7
    assert len({output["timeline_hash"] for output in outputs}) == 1
    assert displays[0] == displays[1] == displays[2]
    assert outputs[0]["timeline_hash"] == session.get_timeline_hash()
