"""Generate original synthetic test videos with local ffmpeg or the Compose image."""

from __future__ import annotations

import subprocess
from pathlib import Path


def generate_video(destination: Path, duration_s: int = 1) -> None:
    """Generate a small A/V clip, falling back to the ffmpeg-enabled API image."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s=16x16:r=30:d={duration_s}",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-t",
        str(duration_s),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "40",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "8k",
    ]
    try:
        subprocess.run(
            ["ffmpeg", *args, str(destination)],
            check=True,
            capture_output=True,
            text=True,
            timeout=max(30, duration_s),
        )
    except OSError:
        with destination.open("wb") as output:
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    "api",
                    "ffmpeg",
                    *args,
                    "-f",
                    "mp4",
                    "-movflags",
                    "frag_keyframe+empty_moov",
                    "pipe:1",
                ],
                check=True,
                stdout=output,
                stderr=subprocess.PIPE,
                timeout=max(60, duration_s * 2),
            )
