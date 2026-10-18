"""SQLAlchemy 2 database models for IncuBrix.

Tables:
- videos: source video metadata and intake record
- transcripts: transcript data associated with a video
- sessions: session record with optimistic concurrency version
- edits: active edit stack entries
- redo_edits: redo stack entries
- pending_questions: pending clarification questions for a session
- parse_cache: cached natural-language parser operation lists
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy declarative models."""

    # Use JSONB for Postgres and standard JSON for SQLite/others
    type_annotation_map = {
        dict[str, Any]: JSON().with_variant(JSONB, "postgresql"),
        list[dict[str, Any]]: JSON().with_variant(JSONB, "postgresql"),
        list[str]: JSON().with_variant(JSONB, "postgresql"),
    }


class VideoRecord(Base):
    """Source video metadata and intake record."""

    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    duration_s: Mapped[float] = mapped_column(Float, nullable=False)
    fps: Mapped[float] = mapped_column(Float, nullable=False)
    has_audio: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_vfr: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ready", nullable=False)
    warnings: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        default=list,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        nullable=False,
    )

    # Relationships
    transcript: Mapped[TranscriptRecord | None] = relationship(
        "TranscriptRecord",
        back_populates="video",
        uselist=False,
        cascade="all, delete-orphan",
    )
    sessions: Mapped[list[SessionRecord]] = relationship(
        "SessionRecord",
        back_populates="video",
        cascade="all, delete-orphan",
    )


class TranscriptRecord(Base):
    """Transcript associated with an intake video."""

    __tablename__ = "transcripts"

    video_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("videos.id", ondelete="CASCADE"),
        primary_key=True,
    )
    data: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
    )
    stt_provider: Mapped[str] = mapped_column(String(64), nullable=False, default="mock")
    stt_model: Mapped[str] = mapped_column(String(64), nullable=False, default="base")
    stt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        nullable=False,
    )

    video: Mapped[VideoRecord] = relationship("VideoRecord", back_populates="transcript")


class SessionRecord(Base):
    """Session record with optimistic concurrency control."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    video_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
        nullable=False,
    )

    video: Mapped[VideoRecord] = relationship("VideoRecord", back_populates="sessions")
    edits: Mapped[list[EditRecord]] = relationship(
        "EditRecord",
        back_populates="session",
        order_by="EditRecord.position",
        cascade="all, delete-orphan",
    )
    redo_edits: Mapped[list[RedoEditRecord]] = relationship(
        "RedoEditRecord",
        back_populates="session",
        order_by="RedoEditRecord.position",
        cascade="all, delete-orphan",
    )
    pending_question: Mapped[PendingQuestionRecord | None] = relationship(
        "PendingQuestionRecord",
        back_populates="session",
        uselist=False,
        cascade="all, delete-orphan",
    )


class EditRecord(Base):
    """Applied edit in the active stack of a session."""

    __tablename__ = "edits"
    __table_args__ = (UniqueConstraint("session_id", "position", name="uq_edits_session_pos"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    edit_id: Mapped[str] = mapped_column(String(32), nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    ops: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
    )
    active_in_stack: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    intent_ops: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        default=list,
        nullable=False,
    )

    session: Mapped[SessionRecord] = relationship("SessionRecord", back_populates="edits")


class RedoEditRecord(Base):
    """Undone edit in the redo stack of a session."""

    __tablename__ = "redo_edits"
    __table_args__ = (UniqueConstraint("session_id", "position", name="uq_redo_edits_session_pos"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    edit_id: Mapped[str] = mapped_column(String(32), nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    ops: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
    )
    intent_ops: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        default=list,
        nullable=False,
    )

    session: Mapped[SessionRecord] = relationship("SessionRecord", back_populates="redo_edits")


class PendingQuestionRecord(Base):
    """Pending clarification question for a session."""

    __tablename__ = "pending_questions"

    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        nullable=False,
    )

    session: Mapped[SessionRecord] = relationship(
        "SessionRecord", back_populates="pending_question"
    )


class ParseCacheRecord(Base):
    """Cached parsed operations for deterministic LLM translation."""

    __tablename__ = "parse_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    ops: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        nullable=False,
    )
