"""Postgres-backed parsed operation cache implementing the ParseCache protocol."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from incubrix.db.engine import get_session_factory
from incubrix.db.models import ParseCacheRecord


class PostgresParseCache:
    """Persistent LLM parsed-intent cache implementing the ParseCache protocol."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        provider: str = "mock",
        model: str = "default",
    ) -> None:
        self._session_factory = session_factory or get_session_factory()
        self._provider = provider
        self._model = model

    def get(self, key: str) -> list[dict[str, Any]] | None:
        """Retrieve parsed operations for a deterministic digest key, if present."""
        with self._session_factory() as session:
            record = session.execute(
                select(ParseCacheRecord).where(ParseCacheRecord.key == key)
            ).scalar_one_or_none()
            if record is None:
                return None
            return copy.deepcopy(record.ops)

    def set(
        self,
        key: str,
        ops: list[dict[str, Any]],
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        """Persist parsed operations under a deterministic digest key."""
        stored_ops = copy.deepcopy(ops)
        prov = provider or self._provider
        mod = model or self._model

        with self._session_factory() as session:
            with session.begin():
                existing = session.execute(
                    select(ParseCacheRecord).where(ParseCacheRecord.key == key)
                ).scalar_one_or_none()

                if existing is not None:
                    existing.ops = stored_ops
                    existing.provider = prov
                    existing.model = mod
                else:
                    session.add(
                        ParseCacheRecord(
                            key=key,
                            ops=stored_ops,
                            provider=prov,
                            model=mod,
                            created_at=datetime.now(timezone.utc),
                        )
                    )
