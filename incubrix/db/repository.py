"""Session repository: persistent session storage with optimistic concurrency control.

Guarantees:
- load_session(session_id) rebuilds the engine SessionManager deterministically
  via replay(source, stack).
- save_session saves in ONE transaction (edits, redo stack, pending questions,
  and incremented version).
- Optimistic concurrency control via sessions.version. Stale version raises
  ConcurrencyError.
- The timeline_hash after load equals the hash before save.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from incubrix.db.engine import get_session_factory
from incubrix.db.mapping import (
    edit_record_to_edit,
    edit_to_edit_record_kwargs,
    edit_to_redo_record_kwargs,
    transcript_record_to_transcript,
    video_to_source_info,
)
from incubrix.db.models import (
    EditRecord,
    PendingQuestionRecord,
    RedoEditRecord,
    SessionRecord,
    TranscriptRecord,
    VideoRecord,
)
from incubrix.engine.models import SessionState
from incubrix.engine.session import SessionManager


class SessionNotFoundError(Exception):
    """Raised when a session_id does not exist in the database."""


class ConcurrencyError(Exception):
    """Raised when an update fails due to a stale optimistic concurrency version."""

    retryable = True


@dataclass
class LoadedSession:
    """Result of loading a session from the repository."""

    session_id: str
    video_id: str
    manager: SessionManager
    version: int


class SessionRepository:
    """Repository managing session persistence and optimistic concurrency."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()

    def create_session(
        self,
        video_id: str,
        session_id: str | None = None,
        db: Session | None = None,
    ) -> LoadedSession:
        """Create a new session record for a video and initialize its SessionManager."""
        import uuid

        def _do_create(s: Session) -> LoadedSession:
            # Verify video and transcript exist
            video = s.execute(
                select(VideoRecord).where(VideoRecord.id == video_id)
            ).scalar_one_or_none()
            if video is None:
                raise ValueError(f"Video '{video_id}' not found.")
            transcript_rec = s.execute(
                select(TranscriptRecord).where(TranscriptRecord.video_id == video_id)
            ).scalar_one_or_none()
            if transcript_rec is None:
                raise ValueError(f"Transcript for video '{video_id}' not found.")

            sid = session_id or f"sess_{uuid.uuid4().hex[:12]}"
            session_rec = SessionRecord(id=sid, video_id=video_id, version=1)
            s.add(session_rec)
            s.flush()

            source = video_to_source_info(video)
            transcript = transcript_record_to_transcript(transcript_rec)
            manager = SessionManager(source=source, transcript=transcript)
            return LoadedSession(session_id=sid, video_id=video_id, manager=manager, version=1)

        if db is not None:
            return _do_create(db)
        with self._session_factory() as session:
            with session.begin():
                return _do_create(session)

    def load_session(self, session_id: str, db: Session | None = None) -> LoadedSession:
        """Load session state and rebuild SessionManager deterministically from history."""

        def _do_load(s: Session) -> LoadedSession:
            session_rec = s.execute(
                select(SessionRecord).where(SessionRecord.id == session_id)
            ).scalar_one_or_none()
            if session_rec is None:
                raise SessionNotFoundError(f"Session '{session_id}' not found.")

            video = s.execute(
                select(VideoRecord).where(VideoRecord.id == session_rec.video_id)
            ).scalar_one_or_none()
            if video is None:
                raise ValueError(
                    f"Video '{session_rec.video_id}' for session '{session_id}' not found."
                )

            transcript_rec = s.execute(
                select(TranscriptRecord).where(TranscriptRecord.video_id == session_rec.video_id)
            ).scalar_one_or_none()
            if transcript_rec is None:
                raise ValueError(f"Transcript for video '{session_rec.video_id}' not found.")

            edits = (
                s.execute(
                    select(EditRecord)
                    .where(EditRecord.session_id == session_id)
                    .order_by(EditRecord.position.asc())
                )
                .scalars()
                .all()
            )

            redo_edits = (
                s.execute(
                    select(RedoEditRecord)
                    .where(RedoEditRecord.session_id == session_id)
                    .order_by(RedoEditRecord.position.asc())
                )
                .scalars()
                .all()
            )

            pending_q = s.execute(
                select(PendingQuestionRecord).where(PendingQuestionRecord.session_id == session_id)
            ).scalar_one_or_none()

            source = video_to_source_info(video)
            transcript = transcript_record_to_transcript(transcript_rec)
            stack = [edit_record_to_edit(e) for e in edits]
            redo = [edit_record_to_edit(r) for r in redo_edits]

            # Reconstruct monotonic edit_counter from highest numeric edit ID
            edit_counter = 0
            for item in stack + redo:
                if item.id.startswith("e") and item.id[1:].isdigit():
                    edit_counter = max(edit_counter, int(item.id[1:]))

            pending_question = pending_q.payload if pending_q is not None else None

            # Reconstruct pure SessionState
            state = SessionState(
                source=source,
                transcript=transcript,
                stack=stack,
                redo=redo,
                edit_counter=edit_counter,
                pending_question=pending_question,
            )
            manager = SessionManager(source=source, transcript=transcript)
            manager._state = state

            return LoadedSession(
                session_id=session_id,
                video_id=session_rec.video_id,
                manager=manager,
                version=session_rec.version,
            )

        if db is not None:
            return _do_load(db)
        with self._session_factory() as session:
            return _do_load(session)

    def save_session(
        self,
        session_id: str,
        manager: SessionManager,
        expected_version: int,
        db: Session | None = None,
    ) -> int:
        """Persist session changes in ONE transaction with optimistic concurrency check.

        Raises ConcurrencyError if expected_version does not match current version.
        Returns the new version number (expected_version + 1).
        """

        def _do_save(s: Session) -> int:
            new_version = expected_version + 1
            now = datetime.now(timezone.utc)

            # Optimistic concurrency check: update version where version == expected_version
            stmt = (
                update(SessionRecord)
                .where(SessionRecord.id == session_id, SessionRecord.version == expected_version)
                .values(version=new_version, updated_at=now)
            )
            result = s.execute(stmt)

            if result.rowcount == 0:
                # Check if session exists to provide accurate error message
                current = s.execute(
                    select(SessionRecord.version).where(SessionRecord.id == session_id)
                ).scalar_one_or_none()
                if current is None:
                    raise SessionNotFoundError(f"Session '{session_id}' not found.")
                raise ConcurrencyError(
                    f"Session '{session_id}' conflict: expected version {expected_version}, "
                    f"but current version is {current}."
                )

            # Replace edit stack
            s.execute(delete(EditRecord).where(EditRecord.session_id == session_id))
            for pos, edit in enumerate(manager.state.stack):
                kwargs = edit_to_edit_record_kwargs(
                    edit,
                    session_id=session_id,
                    position=pos,
                    active_in_stack=True,
                )
                s.add(EditRecord(**kwargs))

            # Replace redo stack
            s.execute(delete(RedoEditRecord).where(RedoEditRecord.session_id == session_id))
            for pos, edit in enumerate(manager.state.redo):
                kwargs = edit_to_redo_record_kwargs(
                    edit,
                    session_id=session_id,
                    position=pos,
                )
                s.add(RedoEditRecord(**kwargs))

            # Replace pending question
            s.execute(
                delete(PendingQuestionRecord).where(PendingQuestionRecord.session_id == session_id)
            )
            if manager.state.pending_question is not None:
                s.add(
                    PendingQuestionRecord(
                        session_id=session_id,
                        payload=manager.state.pending_question,
                        created_at=now,
                    )
                )

            s.flush()
            return new_version

        if db is not None:
            return _do_save(db)
        with self._session_factory() as session:
            with session.begin():
                return _do_save(session)
