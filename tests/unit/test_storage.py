"""Unit tests for S3 storage adapter and source verification with mocks."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from incubrix.adapters.storage.s3 import (
    S3Storage,
    SourceIntegrityError,
    SourceVerificationResult,
    compute_file_sha256,
)
from incubrix.config import Settings


@pytest.fixture
def test_settings():
    return Settings(
        S3_ENDPOINT="http://localhost:9000",
        S3_ACCESS_KEY="minioadmin",
        S3_SECRET_KEY="minioadmin",
        S3_BUCKET="incubrix-test",
        S3_REGION="us-east-1",
        S3_PRESIGNED_EXPIRY=1800,
    )


class TestComputeFileSha256:
    def test_compute_file_sha256(self, tmp_path):
        test_file = tmp_path / "hello.txt"
        test_file.write_bytes(b"hello world")
        import hashlib

        expected = hashlib.sha256(b"hello world").hexdigest()
        assert compute_file_sha256(test_file) == expected


class TestSourceVerificationResult:
    def test_raise_if_invalid_passes_when_valid(self):
        res = SourceVerificationResult(
            is_valid=True,
            recorded_sha256="abc",
            computed_sha256="abc",
            source_key="sources/abc.mp4",
        )
        res.raise_if_invalid()

    def test_raise_if_invalid_raises_when_invalid(self):
        res = SourceVerificationResult(
            is_valid=False,
            recorded_sha256="abc",
            computed_sha256="xyz",
            source_key="sources/abc.mp4",
            error="SHA-256 mismatch",
        )
        with pytest.raises(SourceIntegrityError, match="SHA-256 mismatch"):
            res.raise_if_invalid()


class TestS3StorageMocked:
    @patch("boto3.client")
    def test_ensure_bucket_idempotent(self, mock_boto, test_settings):
        mock_client = MagicMock()
        mock_boto.return_value = mock_client

        storage = S3Storage(test_settings)

        # Bucket exists
        mock_client.head_bucket.return_value = {}
        storage.ensure_bucket()
        mock_client.head_bucket.assert_called_with(Bucket="incubrix-test")

        # Bucket does not exist -> create
        mock_client.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadBucket",
        )
        storage.ensure_bucket()
        mock_client.create_bucket.assert_called_with(Bucket="incubrix-test")

    @patch("boto3.client")
    def test_exists(self, mock_boto, test_settings):
        mock_client = MagicMock()
        mock_boto.return_value = mock_client

        storage = S3Storage(test_settings)
        mock_client.head_object.return_value = {}
        assert storage.exists("test.mp4") is True

        mock_client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadObject",
        )
        assert storage.exists("missing.mp4") is False

    @patch("boto3.client")
    def test_presigned_url(self, mock_boto, test_settings):
        mock_client = MagicMock()
        mock_boto.return_value = mock_client
        mock_client.generate_presigned_url.return_value = "https://minio/signed"

        storage = S3Storage(test_settings)
        url = storage.presigned_get_url("video.mp4")
        assert url == "https://minio/signed"
        mock_client.generate_presigned_url.assert_called_with(
            ClientMethod="get_object",
            Params={"Bucket": "incubrix-test", "Key": "video.mp4"},
            ExpiresIn=1800,
        )

    @patch("boto3.client")
    def test_verify_source_match(self, mock_boto, test_settings):
        mock_client = MagicMock()
        mock_boto.return_value = mock_client
        import hashlib

        data = b"video data chunk 123"
        sha = hashlib.sha256(data).hexdigest()

        mock_client.get_object.return_value = {"Body": io.BytesIO(data)}

        storage = S3Storage(test_settings)
        result = storage.verify_source({"source_key": "sources/abc.mp4", "sha256": sha})
        assert result.is_valid is True
        assert result.computed_sha256 == sha
        assert result.error is None

    @patch("boto3.client")
    def test_verify_source_corrupted_fails(self, mock_boto, test_settings):
        mock_client = MagicMock()
        mock_boto.return_value = mock_client

        recorded_sha = "1111111111111111111111111111111111111111111111111111111111111111"
        corrupted_data = b"corrupted bytes"

        mock_client.get_object.return_value = {"Body": io.BytesIO(corrupted_data)}

        storage = S3Storage(test_settings)
        result = storage.verify_source({"source_key": "sources/abc.mp4", "sha256": recorded_sha})
        assert result.is_valid is False
        assert "corrupted" in result.error.lower()
        with pytest.raises(SourceIntegrityError):
            result.raise_if_invalid()
