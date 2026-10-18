"""Unit tests for SessionRepository and PostgresParseCache with SQLite in-memory."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from incubrix.db.mapping import (
    source_info_to_video_kwargs,
    transcript_to_record_kwargs,
)
from incubrix.db.models import Base, TranscriptRecord, VideoRecord
from incubrix.db.parse_cache import PostgresParseCache
from incubrix.db.repository import (
    ConcurrencyError,
    SessionNotFoundError,
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


@pytest.fixture
def sqlite_session_factory():
    """Create an in-memory SQLite engine and sessionmaker with all tables created."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    return factory


@pytest.fixture
def seeded_db(sqlite_session_factory):
    """Seed video and transcript records for session repository testing."""
    source = SourceInfo(
        sha256="test_sha256_hash_1234567890",
        duration_frames=3000,
        fps=30.0,
        has_audio=True,
    )
    words = [
        Word(id="w0001", text="Welcome", start_s=0.0, end_s=0.4),
        Word(id="w0002", text="to", start_s=0.5, end_s=0.7),
        Word(id="w0003", text="the", start_s=0.8, end_s=0.9),
        Word(id="w0004", text="show", start_s=1.0, end_s=1.5),
        Word(id="w0005", text="everyone", start_s=1.6, end_s=2.0),
    ]
    transcript = Transcript(
        words=words,
        sentences=[Sentence(first_word_idx=0, last_word_idx=4)],
        speakers=["speaker_1"],
    )

    with sqlite_session_factory() as session:
        v_kwargs = source_info_to_video_kwargs(
            source, video_id="vid_1", source_key="sources/test.mp4"
        )
        t_kwargs = transcript_to_record_kwargs(transcript, video_id="vid_1")
        session.add(VideoRecord(**v_kwargs))
        session.add(TranscriptRecord(**t_kwargs))
        session.commit()

    return sqlite_session_factory


class TestSessionRepository:
    def test_create_and_load_empty_session(self, seeded_db):
        repo = SessionRepository(seeded_db)
        created = repo.create_session(video_id="vid_1", session_id="s1")
        assert created.session_id == "s1"
        assert created.version == 1
        assert len(created.manager.state.stack) == 0

        loaded = repo.load_session("s1")
        assert loaded.session_id == "s1"
        assert loaded.version == 1
        assert loaded.manager.get_timeline_hash() == created.manager.get_timeline_hash()

    def test_load_nonexistent_session_raises(self, seeded_db):
        repo = SessionRepository(seeded_db)
        with pytest.raises(SessionNotFoundError, match="Session 'missing' not found"):
            repo.load_session("missing")

    def test_save_and_reload_timeline_hash_identical(self, seeded_db):
        """The timeline_hash after load must equal the hash before save."""
        repo = SessionRepository(seeded_db)
        created = repo.create_session(video_id="vid_1", session_id="s2")
        mgr = created.manager

        # Apply instructions
        mgr.push_edit("cut intro", [CutOp(ranges=[(0, 30)])])
        mgr.push_edit(
            "add caption",
            [
                AddCaptionOp(
                    overlay_id="ov1",
                    text="Show Intro",
                    start_word_id="w0002",
                    end_word_id="w0004",
                )
            ],
        )

        hash_before = mgr.get_timeline_hash()

        # Save session
        new_version = repo.save_session("s2", mgr, expected_version=1)
        assert new_version == 2

        # Reload session from repository
        loaded = repo.load_session("s2")
        assert loaded.version == 2
        assert len(loaded.manager.state.stack) == 2
        assert loaded.manager.edit_counter == 2

        hash_after = loaded.manager.get_timeline_hash()
        assert hash_after == hash_before

    def test_undo_redo_stack_persisted_correctly(self, seeded_db):
        repo = SessionRepository(seeded_db)
        repo.create_session(video_id="vid_1", session_id="s3")
        loaded = repo.load_session("s3")
        mgr = loaded.manager

        mgr.push_edit("edit 1", [CutOp(ranges=[(0, 30)])])
        mgr.push_edit("edit 2", [SpeedOp(range=(30, 60), factor=2.0)])
        mgr.undo()  # edit 2 moved to redo stack

        assert len(mgr.state.stack) == 1
        assert len(mgr.state.redo) == 1

        repo.save_session("s3", mgr, expected_version=1)

        # Reload
        reloaded = repo.load_session("s3")
        assert len(reloaded.manager.state.stack) == 1
        assert len(reloaded.manager.state.redo) == 1
        assert reloaded.manager.state.stack[0].id == "e1"
        assert reloaded.manager.state.redo[0].id == "e2"

        # Redo in reloaded session
        redone = reloaded.manager.redo_edit()
        assert redone is not None
        assert redone.id == "e2"
        assert len(reloaded.manager.state.stack) == 2
        assert len(reloaded.manager.state.redo) == 0

    def test_pending_question_persisted_and_cleared(self, seeded_db):
        repo = SessionRepository(seeded_db)
        repo.create_session(video_id="vid_1", session_id="s4")
        loaded = repo.load_session("s4")
        mgr = loaded.manager

        # Set a pending question
        mgr.state.pending_question = {
            "question": "Which pricing mention?",
            "options": [{"choice": "1", "snippet": "cost"}],
        }
        repo.save_session("s4", mgr, expected_version=1)

        reloaded = repo.load_session("s4")
        assert reloaded.manager.state.pending_question == mgr.state.pending_question

        # Clear pending question
        reloaded.manager.state.pending_question = None
        repo.save_session("s4", reloaded.manager, expected_version=2)

        final_load = repo.load_session("s4")
        assert final_load.manager.state.pending_question is None

    def test_optimistic_concurrency_conflict(self, seeded_db):
        """A save with a stale version raises ConcurrencyError."""
        repo = SessionRepository(seeded_db)
        repo.create_session(video_id="vid_1", session_id="s5")

        # Two clients load version 1
        client_a = repo.load_session("s5")
        client_b = repo.load_session("s5")

        assert client_a.version == 1
        assert client_b.version == 1

        # Client A modifies and saves successfully -> version becomes 2
        client_a.manager.push_edit("cut 1", [CutOp(ranges=[(0, 30)])])
        v_a = repo.save_session("s5", client_a.manager, expected_version=client_a.version)
        assert v_a == 2

        # Client B tries to save with stale version 1 -> ConcurrencyError
        client_b.manager.push_edit("speed 1", [SpeedOp(range=(0, 60), factor=2.0)])
        with pytest.raises(ConcurrencyError, match="conflict: expected version 1"):
            repo.save_session("s5", client_b.manager, expected_version=client_b.version)


class TestPostgresParseCache:
    def test_cache_miss_returns_none(self, sqlite_session_factory):
        cache = PostgresParseCache(sqlite_session_factory)
        assert cache.get("nonexistent_key_123") is None

    def test_cache_set_and_get(self, sqlite_session_factory):
        cache = PostgresParseCache(sqlite_session_factory, provider="mock", model="llama")
        ops = [{"op": "cut", "from_start": 30}]
        cache.set("key_abc", ops)

        result = cache.get("key_abc")
        assert result == ops
        # Defensive copy check: mutating result doesn't mutate cache
        result.append({"op": "render"})
        assert cache.get("key_abc") == ops

    def test_cache_overwrite(self, sqlite_session_factory):
        cache = PostgresParseCache(sqlite_session_factory)
        cache.set("key_xyz", [{"op": "cut", "from_start": 10}])
        cache.set("key_xyz", [{"op": "cut", "from_start": 20}])
        assert cache.get("key_xyz") == [{"op": "cut", "from_start": 20}]
