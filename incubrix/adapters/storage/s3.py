"""S3-compatible storage adapter and source verification (Must-have 1).

Features:
- Streaming uploads and downloads (no loading whole videos into memory).
- Content-addressed source storage under sha256 key, never overwritten.
- verify_source: streams object from S3, re-hashes, and compares to intake SHA-256.
- Idempotent bucket creation.
- Presigned download URLs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from incubrix.adapters.base import AdapterConfigurationError, AdapterError
from incubrix.config import Settings, get_settings


class StorageError(AdapterError):
    """Base error for storage operations."""


class SourceIntegrityError(StorageError):
    """Raised when a source file's SHA-256 does not match the intake record."""


@dataclass
class SourceVerificationResult:
    """Outcome of verifying a stored source against its recorded SHA-256."""

    is_valid: bool
    recorded_sha256: str
    computed_sha256: str
    source_key: str
    error: str | None = None

    def raise_if_invalid(self) -> None:
        """Raise SourceIntegrityError if verification failed."""
        if not self.is_valid:
            raise SourceIntegrityError(self.error or "Source SHA-256 verification failed.")


def compute_file_sha256(file_path: str | Path, chunk_size: int = 65536) -> str:
    """Compute SHA-256 of a local file by streaming chunks (low memory)."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


class S3Storage:
    """S3-compatible object storage adapter."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._config = settings or get_settings()
        self._bucket = self._config.S3_BUCKET
        self._expiry = self._config.S3_PRESIGNED_EXPIRY

        try:
            self._client = boto3.client(
                "s3",
                endpoint_url=self._config.S3_ENDPOINT,
                aws_access_key_id=self._config.S3_ACCESS_KEY,
                aws_secret_access_key=self._config.S3_SECRET_KEY,
                region_name=self._config.S3_REGION,
                config=BotoConfig(signature_version="s3v4"),
            )
        except Exception as exc:
            raise AdapterConfigurationError(f"Failed to initialize S3 client: {exc}") from exc

    @property
    def bucket(self) -> str:
        """Return the configured bucket name."""
        return self._bucket

    def ensure_bucket(self) -> None:
        """Create the configured bucket if it does not already exist (idempotent)."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchBucket"):
                kwargs: dict[str, Any] = {"Bucket": self._bucket}
                if self._config.S3_REGION and self._config.S3_REGION != "us-east-1":
                    kwargs["CreateBucketConfiguration"] = {
                        "LocationConstraint": self._config.S3_REGION,
                    }
                try:
                    self._client.create_bucket(**kwargs)
                except ClientError as create_exc:
                    create_code = create_exc.response.get("Error", {}).get("Code", "")
                    if create_code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                        raise StorageError(
                            f"Failed to create bucket '{self._bucket}': {create_exc}"
                        ) from create_exc
            else:
                raise StorageError(f"Error checking bucket '{self._bucket}': {exc}") from exc

    def exists(self, key: str) -> bool:
        """Return True if an object exists in the bucket, False otherwise."""
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise StorageError(f"Error checking object '{key}': {exc}") from exc

    def put_file(
        self,
        file_path: str | Path,
        key: str,
        content_type: str | None = None,
    ) -> str:
        """Upload a file to S3 using streaming chunks."""
        extra_args: dict[str, str] = {}
        if content_type:
            extra_args["ContentType"] = content_type

        try:
            self._client.upload_file(
                Filename=str(file_path),
                Bucket=self._bucket,
                Key=key,
                ExtraArgs=extra_args or None,
            )
            return key
        except Exception as exc:
            raise StorageError(f"Failed to upload '{file_path}' to '{key}': {exc}") from exc

    def put_bytes(
        self,
        data: bytes,
        key: str,
        content_type: str | None = None,
    ) -> str:
        """Upload raw bytes to S3."""
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": data,
        }
        if content_type:
            kwargs["ContentType"] = content_type

        try:
            self._client.put_object(**kwargs)
            return key
        except Exception as exc:
            raise StorageError(f"Failed to put bytes to '{key}': {exc}") from exc

    def get_to_path(self, key: str, destination_path: str | Path) -> Path:
        """Download an S3 object directly to a local destination file path."""
        dest = Path(destination_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._client.download_file(
                Bucket=self._bucket,
                Key=key,
                Filename=str(dest),
            )
            return dest
        except Exception as exc:
            raise StorageError(
                f"Failed to download '{key}' to '{destination_path}': {exc}"
            ) from exc

    def open_stream(self, key: str) -> BinaryIO:
        """Open a streaming read handle to an S3 object body."""
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return response["Body"]
        except Exception as exc:
            raise StorageError(f"Failed to stream object '{key}': {exc}") from exc

    def presigned_get_url(self, key: str, expires_in: int | None = None) -> str:
        """Generate a presigned GET URL for an object."""
        exp = expires_in if expires_in is not None else self._expiry
        try:
            return self._client.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=exp,
            )
        except Exception as exc:
            raise StorageError(f"Failed to generate presigned URL for '{key}': {exc}") from exc

    def delete(self, key: str) -> None:
        """Delete an object from S3 (never used for intake source videos)."""
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            raise StorageError(f"Failed to delete object '{key}': {exc}") from exc

    def store_source_file(
        self,
        file_path: str | Path,
        extension: str = ".mp4",
    ) -> tuple[str, str]:
        """Store a source video under a content-addressed key (sha256).

        Must-have 1: The source is stored once and never overwritten.
        Streaming hashing and upload (no loading whole videos into memory).

        Returns:
            tuple[sha256, source_key]
        """
        p = Path(file_path)
        if not p.is_file():
            raise FileNotFoundError(f"Source file not found: {file_path}")

        # Stream hash the file
        sha256_hash = compute_file_sha256(p)
        ext = extension if extension.startswith(".") else f".{extension}"
        source_key = f"sources/{sha256_hash}{ext}"

        # If already stored under its content-addressed key, do not overwrite
        if not self.exists(source_key):
            self.put_file(p, source_key, content_type="video/mp4")

        return sha256_hash, source_key

    def verify_source(
        self,
        video_or_key: Any,
        expected_sha256: str | None = None,
        chunk_size: int = 65536,
    ) -> SourceVerificationResult:
        """Re-hash a stored source video by streaming from S3 and compare with recorded SHA-256.

        Must-have 1 verification:
        Streams the object chunk by chunk, recomputes SHA-256 and compares it
        to the intake hash. Never loads the entire video into memory.
        """
        if hasattr(video_or_key, "source_key") and hasattr(video_or_key, "sha256"):
            source_key = video_or_key.source_key
            recorded_sha = video_or_key.sha256
        elif isinstance(video_or_key, dict):
            source_key = video_or_key["source_key"]
            recorded_sha = video_or_key["sha256"]
        elif isinstance(video_or_key, str):
            source_key = video_or_key
            if not expected_sha256:
                raise ValueError("expected_sha256 must be provided when passing a key string.")
            recorded_sha = expected_sha256
        else:
            raise ValueError(f"Unsupported video argument: {type(video_or_key)}")

        hasher = hashlib.sha256()
        try:
            body = self.open_stream(source_key)
            while chunk := body.read(chunk_size):
                hasher.update(chunk)
        except Exception as exc:
            return SourceVerificationResult(
                is_valid=False,
                recorded_sha256=recorded_sha,
                computed_sha256="",
                source_key=source_key,
                error=f"Failed to read source object '{source_key}': {exc}",
            )

        computed_sha = hasher.hexdigest()
        is_valid = computed_sha == recorded_sha
        error = (
            None
            if is_valid
            else (
                f"Source video corrupted! Recorded SHA-256 is '{recorded_sha}', "
                f"but stored object '{source_key}' computed '{computed_sha}'."
            )
        )

        return SourceVerificationResult(
            is_valid=is_valid,
            recorded_sha256=recorded_sha,
            computed_sha256=computed_sha,
            source_key=source_key,
            error=error,
        )
