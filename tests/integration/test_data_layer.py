"""Integration tests against real Postgres and MinIO containers.

Marked as 'integration':
- Migration idempotency
- Session round-trip with identical timeline_hash
- Optimistic concurrency conflict
- Parse-cache hit returns identical ops
- Source upload and verify_source (deliberately corrupted object fails)
- Presigned URL downloads work
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from incubrix.adapters.storage.s3 import (
    S3Storage,
    SourceIntegrityError,
    compute_file_sha256,
)
from incubrix.config import Settings
from incubrix.db.engine import get_engine, get_session_factory, run_migrations
from incubrix.db.mapping import (
    source_info_to_video_kwargs,
    transcript_to_record_kwargs,
)
from incubrix.db.models import TranscriptRecord, VideoRecord
from incubrix.db.parse_cache import PostgresParseCache
from incubrix.db.repository import (
    ConcurrencyError,
    SessionRepository,
)
from incubrix.engine.models import (
    AddCaptionOp,
    CutOp,
    Sentence,
    SourceInfo,
    SpeedOp,
    Transcript,
    Word,
)

# Integration environment URLs (fallback to localhost when running from host)
DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://incubrix:incubrix@localhost:5432/incubrix",
)
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://localhost:9000")


@pytest.fixture(scope="module")
def pg_session_factory():
    """Run migrations on the integration database and return a session factory."""
    run_migrations(DB_URL)
    return get_session_factory(DB_URL)


@pytest.fixture(scope="module")
def minio_storage():
    """Initialize S3Storage pointing to integration MinIO and ensure bucket."""
    settings = Settings(
        S3_ENDPOINT=S3_ENDPOINT,
        S3_ACCESS_KEY=os.environ.get("S3_ACCESS_KEY", "minioadmin"),
        S3_SECRET_KEY=os.environ.get("S3_SECRET_KEY", "minioadmin"),
        S3_BUCKET=os.environ.get("S3_BUCKET", "incubrix"),
        S3_REGION="us-east-1",
        S3_PRESIGNED_EXPIRY=3600,
    )
    storage = S3Storage(settings)
    storage.ensure_bucket()
    return storage


@pytest.mark.integration
class TestDatabaseIntegration:
    def test_migration_idempotency(self):
        """Running migrations multiple times against real Postgres leaves schema intact."""
        run_migrations(DB_URL)
        run_migrations(DB_URL)

        engine = get_engine(DB_URL)
        from sqlalchemy import inspect

        tables = inspect(engine).get_table_names()
        expected = [
            "alembic_version",
            "edits",
            "jobs",
            "parse_cache",
            "pending_questions",
            "redo_edits",
            "sessions",
            "transcripts",
            "videos",
        ]
        for t in expected:
            assert t in tables, f"Expected table {t} to exist in database"

    def test_session_round_trip_hash_equal(self, pg_session_factory):
        """Create, edit, save to Postgres, reload, and verify identical timeline_hash."""
        unique_id = uuid.uuid4().hex[:8]
        video_id = f"vid_it_{unique_id}"
        session_id = f"sess_it_{unique_id}"

        source = SourceInfo(
            sha256=f"hash_{unique_id}",
            duration_frames=3000,
            fps=30.0,
            has_audio=True,
        )
        words = [
            Word(id="w0001", text="Welcome", start_s=0.0, end_s=0.4),
            Word(id="w0002", text="to", start_s=0.5, end_s=0.7),
            Word(id="w0003", text="the", start_s=0.8, end_s=0.9),
            Word(id="w0004", text="video", start_s=1.0, end_s=1.5),
        ]
        transcript = Transcript(
            words=words,
            sentences=[Sentence(first_word_idx=0, last_word_idx=3)],
            speakers=["host"],
        )

        with pg_session_factory() as session:
            v_kwargs = source_info_to_video_kwargs(
                source, video_id=video_id, source_key=f"sources/{video_id}.mp4"
            )
            t_kwargs = transcript_to_record_kwargs(transcript, video_id=video_id)
            session.add(VideoRecord(**v_kwargs))
            session.add(TranscriptRecord(**t_kwargs))
            session.commit()

        repo = SessionRepository(pg_session_factory)
        created = repo.create_session(video_id=video_id, session_id=session_id)
        mgr = created.manager

        # Apply edit operations
        mgr.push_edit("cut intro", [CutOp(ranges=[(0, 30)])])
        mgr.push_edit(
            "add caption",
            [
                AddCaptionOp(
                    overlay_id="ov_it_1",
                    text="Welcome Video",
                    start_word_id="w0001",
                    end_word_id="w0004",
                )
            ],
        )

        hash_before = mgr.get_timeline_hash()

        # Save to Postgres
        new_version = repo.save_session(session_id, mgr, expected_version=1)
        assert new_version == 2

        # Load from Postgres in a fresh connection
        loaded = repo.load_session(session_id)
        assert loaded.version == 2
        assert len(loaded.manager.state.stack) == 2
        assert loaded.manager.state.stack[0].id == "e1"
        assert loaded.manager.state.stack[1].id == "e2"

        hash_after = loaded.manager.get_timeline_hash()
        assert (
            hash_after == hash_before
        ), "timeline_hash must be identical after database round-trip"

    def test_concurrency_conflict_in_postgres(self, pg_session_factory):
        """Two simultaneous operations on one session raise ConcurrencyError for stale version."""
        unique_id = uuid.uuid4().hex[:8]
        video_id = f"vid_cc_{unique_id}"
        session_id = f"sess_cc_{unique_id}"

        source = SourceInfo(
            sha256=f"hash_cc_{unique_id}", duration_frames=1500, fps=30.0, has_audio=True
        )
        transcript = Transcript(words=[Word(id="w0001", text="Hi", start_s=0.0, end_s=0.5)])

        with pg_session_factory() as session:
            session.add(
                VideoRecord(
                    **source_info_to_video_kwargs(
                        source, video_id=video_id, source_key=f"sources/{video_id}.mp4"
                    )
                )
            )
            session.add(
                TranscriptRecord(**transcript_to_record_kwargs(transcript, video_id=video_id))
            )
            session.commit()

        repo = SessionRepository(pg_session_factory)
        repo.create_session(video_id=video_id, session_id=session_id)

        # Worker A and Worker B both load version 1
        worker_a = repo.load_session(session_id)
        worker_b = repo.load_session(session_id)
        assert worker_a.version == 1
        assert worker_b.version == 1

        # Worker A applies edit and commits first
        worker_a.manager.push_edit("cut 1", [CutOp(ranges=[(0, 15)])])
        repo.save_session(session_id, worker_a.manager, expected_version=worker_a.version)

        # Worker B attempts commit with stale version 1 -> ConcurrencyError
        worker_b.manager.push_edit("speed 1", [SpeedOp(range=(0, 30), factor=1.5)])
        with pytest.raises(ConcurrencyError, match="conflict: expected version 1"):
            repo.save_session(session_id, worker_b.manager, expected_version=worker_b.version)

    def test_parse_cache_hit_returns_identical_ops(self, pg_session_factory):
        """Parse cache persists and returns exact operations with defensive copies."""
        cache = PostgresParseCache(pg_session_factory, provider="groq", model="llama-3.3")
        cache_key = f"cache_key_{uuid.uuid4().hex}"
        ops = [
            {"op": "cut", "target": {"kind": "time", "from_start": 30}},
            {"op": "add_caption", "text": "Hello", "target": {"kind": "phrase", "text": "intro"}},
        ]

        cache.set(cache_key, ops)
        cached = cache.get(cache_key)
        assert cached == ops

        # Defensive copy verification
        cached.append({"op": "render"})
        assert cache.get(cache_key) == ops


@pytest.mark.integration
class TestMinIOIntegration:
    def test_source_upload_and_verify_source(self, minio_storage, tmp_path):
        """Source is stored under content-addressed key and verify_source confirms it."""
        video_content = b"fake MP4 video content for testing " + uuid.uuid4().bytes
        temp_video = tmp_path / "sample.mp4"
        temp_video.write_bytes(video_content)

        expected_sha256 = compute_file_sha256(temp_video)

        # Content-addressed upload
        sha, key = minio_storage.store_source_file(temp_video)
        assert sha == expected_sha256
        assert key == f"sources/{expected_sha256}.mp4"
        assert minio_storage.exists(key) is True

        # Verify source passes
        result = minio_storage.verify_source({"source_key": key, "sha256": expected_sha256})
        assert result.is_valid is True
        assert result.computed_sha256 == expected_sha256
        assert result.error is None
        result.raise_if_invalid()

        # Deliberately corrupt the object in MinIO
        corrupted_content = b"tampered content that changes SHA-256"
        minio_storage.put_bytes(corrupted_content, key)

        # Verification must now fail with a clear error
        corrupt_result = minio_storage.verify_source({"source_key": key, "sha256": expected_sha256})
        assert corrupt_result.is_valid is False
        assert "corrupted" in corrupt_result.error.lower()
        with pytest.raises(SourceIntegrityError, match="corrupted"):
            corrupt_result.raise_if_invalid()

    def test_presigned_url_download_works(self, minio_storage, tmp_path):
        """Presigned GET URL allows direct HTTP download of stored object."""
        payload = b"Presigned download content verification test bytes."
        key = f"downloads/test_{uuid.uuid4().hex}.txt"
        minio_storage.put_bytes(payload, key, content_type="text/plain")

        # Generate presigned URL
        presigned_url = minio_storage.presigned_get_url(key, expires_in=600)
        assert presigned_url.startswith("http")

        # Fetch using HTTP client
        response = httpx.get(presigned_url)
        assert response.status_code == 200
        assert response.content == payload
