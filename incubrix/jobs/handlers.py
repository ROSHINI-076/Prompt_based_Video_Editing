"""Registered background job handlers for intake and reserved rendering."""

from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select

from incubrix.adapters.storage.s3 import S3Storage
from incubrix.adapters.stt.factory import get_stt_adapter
from incubrix.config import Settings
from incubrix.db.engine import get_session_factory
from incubrix.db.mapping import transcript_to_record_kwargs
from incubrix.db.models import TranscriptRecord, VideoRecord
from incubrix.manifest import build_intake_manifest


class JobExecutionError(Exception):
    """A job failure with a stable error code and retry policy."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        """Return the public job error shape."""
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "details": self.details,
        }


ProgressCallback = Callable[[int], None]


def _validate_public_https_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise JobExecutionError(
            "INVALID_URL",
            "Video URLs must use HTTPS and cannot contain credentials.",
        )
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise JobExecutionError(
            "URL_UNAVAILABLE", "Video host could not be resolved.", retryable=True
        ) from exc
    if not addresses:
        raise JobExecutionError("INVALID_URL", "Video host has no resolved addresses.")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise JobExecutionError(
                "INVALID_URL",
                "Video URLs cannot resolve to private, loopback, or link-local addresses.",
            )


def _download_url(url: str, destination: Path, settings: Settings) -> None:
    current = url
    redirects = 0
    total = 0
    try:
        with httpx.Client(
            timeout=settings.URL_FETCH_TIMEOUT_S,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            while True:
                _validate_public_https_url(current)
                with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirects >= settings.URL_MAX_REDIRECTS:
                            raise JobExecutionError(
                                "INVALID_URL", "Video URL exceeded redirect limit."
                            )
                        location = response.headers.get("location")
                        if not location:
                            raise JobExecutionError(
                                "INVALID_URL", "Video URL redirect omitted Location."
                            )
                        current = urljoin(current, location)
                        redirects += 1
                        continue
                    response.raise_for_status()
                    length = response.headers.get("content-length")
                    if length and int(length) > settings.MAX_DOWNLOAD_BYTES:
                        raise JobExecutionError(
                            "PAYLOAD_TOO_LARGE", "Video exceeds the download size limit."
                        )
                    with destination.open("wb") as target:
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > settings.MAX_DOWNLOAD_BYTES:
                                raise JobExecutionError(
                                    "PAYLOAD_TOO_LARGE", "Video exceeds the download size limit."
                                )
                            target.write(chunk)
                    return
    except JobExecutionError:
        raise
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise JobExecutionError(
            "URL_UNAVAILABLE", "Video download failed.", retryable=True
        ) from exc
    except httpx.HTTPStatusError as exc:
        retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
        raise JobExecutionError(
            "URL_UNAVAILABLE" if retryable else "INVALID_URL",
            "Video URL returned an unsuccessful response.",
            retryable=retryable,
            details={"status_code": exc.response.status_code},
        ) from exc


def _probe_video(path: Path) -> tuple[float, float, bool, bool]:
    """Read duration, normalized fps, audio presence, and VFR signal with ffprobe."""
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,avg_frame_rate,r_frame_rate",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        data = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise JobExecutionError("INVALID_MEDIA", "Unable to inspect uploaded video.") from exc

    streams = data.get("streams", [])
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if video_stream is None:
        raise JobExecutionError("INVALID_MEDIA", "Uploaded file has no video stream.")

    def frame_rate(value: str | None) -> float:
        if not value or value in {"0/0", "N/A"}:
            return 0.0
        numerator, denominator = value.split("/", maxsplit=1)
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else 0.0

    fps = frame_rate(video_stream.get("avg_frame_rate")) or frame_rate(
        video_stream.get("r_frame_rate")
    )
    duration = float(data.get("format", {}).get("duration", 0.0))
    if fps <= 0 or duration <= 0:
        raise JobExecutionError("INVALID_MEDIA", "Video duration or frame rate is unavailable.")
    average_rate = frame_rate(video_stream.get("avg_frame_rate"))
    nominal_rate = frame_rate(video_stream.get("r_frame_rate"))
    is_vfr = bool(average_rate and nominal_rate and abs(average_rate - nominal_rate) > 0.01)
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    return duration, fps, has_audio, is_vfr


def intake_and_transcribe(
    payload: dict[str, Any],
    progress: ProgressCallback,
    settings: Settings,
) -> dict[str, Any]:
    """Fetch an input, preserve it in content-addressed S3, and persist transcript."""
    stage_timings_s: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix="incubrix-intake-") as temp_dir:
        media_path = Path(temp_dir) / "source"
        storage = S3Storage(settings)
        stage_key = payload.get("stage_key")
        local_path = payload.get("local_path")
        source_url = payload.get("url")
        stage_started = time.perf_counter()
        if stage_key:
            storage.get_to_path(stage_key, media_path)
        elif local_path:
            media_path = Path(local_path)
        elif source_url:
            if urlparse(source_url).scheme == "file":
                if not settings.TEST_MODE:
                    raise JobExecutionError(
                        "INVALID_URL", "file URLs are only enabled in test mode."
                    )
                media_path = Path(urlparse(source_url).path)
            else:
                _download_url(source_url, media_path, settings)
        else:
            raise JobExecutionError("INVALID_JOB_PAYLOAD", "Job input is missing.")
        stage_timings_s["download_s"] = time.perf_counter() - stage_started

        if not media_path.is_file():
            raise JobExecutionError("VIDEO_NOT_FOUND", "Video input file was not available.")
        progress(10)
        extension = Path(payload.get("filename", "video.mp4")).suffix or ".mp4"
        stage_started = time.perf_counter()
        try:
            sha256, source_key = storage.store_source_file(media_path, extension)
        except Exception as exc:
            raise JobExecutionError(
                "STORAGE_UNAVAILABLE", "Could not store source video.", retryable=True
            ) from exc
        stage_timings_s["source_storage_s"] = time.perf_counter() - stage_started
        progress(40)

        stage_started = time.perf_counter()
        duration_s, fps, has_audio, is_vfr = _probe_video(media_path)
        stage_timings_s["probe_s"] = time.perf_counter() - stage_started
        warnings: list[str] = []
        if is_vfr:
            warnings.append(
                "Variable-frame-rate input detected; timeline uses the measured average fps."
            )
        if not has_audio:
            warnings.append("No audio track was detected.")

        transcript_path = settings.STT_MOCK_TRANSCRIPT_PATH
        if settings.STT_PROVIDER == "mock" and not transcript_path:
            transcript_path = str(Path(temp_dir) / "empty-transcript.json")
            Path(transcript_path).write_text('{"words": []}', encoding="utf-8")
        stage_started = time.perf_counter()
        try:
            result = get_stt_adapter(settings).transcribe(transcript_path or media_path)
        except Exception as exc:
            raise JobExecutionError(
                "STT_UNAVAILABLE", "Speech transcription failed.", retryable=True
            ) from exc
        stage_timings_s["transcription_s"] = time.perf_counter() - stage_started
        transcript = result.transcript
        transcript.warnings.extend(warnings)
        progress(80)

        video_id = str(payload.get("video_id") or f"vid_{sha256[:24]}")
        session_factory = get_session_factory(settings.DATABASE_URL)
        stage_started = time.perf_counter()
        try:
            with session_factory.begin() as session:
                video = session.execute(
                    select(VideoRecord).where(VideoRecord.id == video_id)
                ).scalar_one_or_none()
                values = {
                    "source_key": source_key,
                    "sha256": sha256,
                    "duration_s": round(duration_s, 3),
                    "fps": fps,
                    "has_audio": has_audio,
                    "is_vfr": is_vfr,
                    "status": "ready",
                    "warnings": warnings,
                }
                if video is None:
                    video = VideoRecord(id=video_id, **values)
                    session.add(video)
                else:
                    for key, value in values.items():
                        setattr(video, key, value)
                transcript_record = session.get(TranscriptRecord, video_id)
                transcript_values = transcript_to_record_kwargs(
                    transcript,
                    video_id=video_id,
                    stt_provider=result.usage.provider,
                    stt_model=result.usage.model,
                )
                if transcript_record is None:
                    session.add(TranscriptRecord(**transcript_values))
                else:
                    transcript_record.data = transcript_values["data"]
                    transcript_record.stt_provider = result.usage.provider
                    transcript_record.stt_model = result.usage.model
        except Exception as exc:
            raise JobExecutionError(
                "DATABASE_UNAVAILABLE", "Could not persist intake metadata.", retryable=True
            ) from exc
        stage_timings_s["persistence_s"] = time.perf_counter() - stage_started
        if stage_key:
            storage.delete(stage_key)

        manifest = build_intake_manifest(
            job_id=str(payload.get("job_id", "unknown")),
            video_id=video_id,
            source_sha256=sha256,
            duration_s=duration_s,
            stt_usage=result.usage,
            stage_timings_s=stage_timings_s,
            warnings=warnings,
            settings=settings,
        )
        manifest_key = f"manifests/jobs/{manifest['job_id']}.json"
        try:
            storage.put_bytes(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                manifest_key,
                content_type="application/json",
            )
            manifest_url = storage.presigned_get_url(manifest_key)
        except Exception as exc:
            raise JobExecutionError(
                "STORAGE_UNAVAILABLE", "Could not store the intake manifest.", retryable=True
            ) from exc
        progress(100)
        return {
            "video_id": video_id,
            "duration_s": round(duration_s, 3),
            "fps": fps,
            "has_audio": has_audio,
            "sha256": sha256,
            "status": "ready",
            "warnings": warnings,
            "manifest": {"key": manifest_key, "url": manifest_url},
        }


def render_not_implemented(
    payload: dict[str, Any],
    progress: ProgressCallback,
    settings: Settings,
) -> dict[str, Any]:
    """Reserve the render queue handler until the rendering phase."""
    del payload, progress, settings
    raise JobExecutionError("NOT_IMPLEMENTED", "Rendering is not implemented yet.")


def build_handlers(
    settings: Settings,
) -> dict[str, Callable[[dict[str, Any], ProgressCallback], dict]]:
    """Bind configured settings to the handler registry."""
    return {
        "intake_and_transcribe": lambda payload, progress: intake_and_transcribe(
            payload, progress, settings
        ),
        "render": lambda payload, progress: render_not_implemented(payload, progress, settings),
    }
