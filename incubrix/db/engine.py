"""Database engine, session factory, and safe concurrent migrations.

Provides:
- get_engine: cached SQLAlchemy Engine instance
- get_session_factory: cached sessionmaker instance
- run_migrations: executes Alembic migrations on startup safely, using Postgres
  advisory locks to prevent concurrent migration race conditions between processes.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from incubrix.config import get_settings
from incubrix.db.models import Base

# Stable 64-bit advisory lock key: 'INCUBRIX' in ASCII -> 0x494e435542524958
_ADVISORY_LOCK_ID = 5282218086208535896

_ENGINE_CACHE: dict[str, Engine] = {}
_SESSION_FACTORY_CACHE: dict[str, sessionmaker[Session]] = {}


def get_engine(db_url: str | None = None) -> Engine:
    """Return an Engine instance for the given or configured database URL."""
    url = db_url or get_settings().DATABASE_URL
    if url not in _ENGINE_CACHE:
        connect_args = {}
        if url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            _ENGINE_CACHE[url] = create_engine(url, connect_args=connect_args)
        else:
            settings = get_settings()
            _ENGINE_CACHE[url] = create_engine(
                url,
                pool_size=settings.DB_POOL_SIZE,
                max_overflow=settings.DB_MAX_OVERFLOW,
                pool_pre_ping=True,
            )
    return _ENGINE_CACHE[url]


def get_session_factory(db_url: str | None = None) -> sessionmaker[Session]:
    """Return a sessionmaker factory bound to the Engine for db_url."""
    url = db_url or get_settings().DATABASE_URL
    if url not in _SESSION_FACTORY_CACHE:
        engine = get_engine(url)
        _SESSION_FACTORY_CACHE[url] = sessionmaker(
            bind=engine,
            expire_on_commit=False,
            autoflush=False,
        )
    return _SESSION_FACTORY_CACHE[url]


def get_db_session(db_url: str | None = None) -> Generator[Session, None, None]:
    """Context manager / dependency yielding a database session."""
    factory = get_session_factory(db_url)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _get_alembic_config(db_url: str) -> Config:
    """Locate alembic.ini and construct a Config configured with db_url."""
    # Find alembic.ini in project root
    current = Path(__file__).resolve()
    # Walk up to find alembic.ini
    ini_path: Path | None = None
    for parent in current.parents:
        candidate = parent / "alembic.ini"
        if candidate.exists():
            ini_path = candidate
            break

    if ini_path is None:
        raise FileNotFoundError("alembic.ini not found in project hierarchy.")

    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option(
        "script_location",
        str(ini_path.parent / "incubrix" / "db" / "migrations"),
    )
    return cfg


def run_migrations(db_url: str | None = None) -> None:
    """Run migrations safely to bring the database schema up to 'head'.

    When multiple processes (e.g. API and worker) start concurrently against
    Postgres, this uses a PostgreSQL advisory lock (pg_advisory_lock) so only
    one process performs the schema migration while others wait. Once the lock
    is released, waiting processes call upgrade which cleanly detects that the
    schema is already at head.

    For SQLite (e.g. in-memory test databases), Base.metadata.create_all is used.
    """
    url = db_url or get_settings().DATABASE_URL
    engine = get_engine(url)

    if engine.dialect.name == "sqlite":
        # For SQLite (in-memory or local test file)
        Base.metadata.create_all(engine)
        return

    alembic_cfg = _get_alembic_config(url)

    if engine.dialect.name == "postgresql":
        # Use a dedicated connection to hold the session-level advisory lock
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:lock_id);"), {"lock_id": _ADVISORY_LOCK_ID})
            conn.commit()
            try:
                command.upgrade(alembic_cfg, "head")
            finally:
                conn.execute(
                    text("SELECT pg_advisory_unlock(:lock_id);"), {"lock_id": _ADVISORY_LOCK_ID}
                )
                conn.commit()
    else:
        command.upgrade(alembic_cfg, "head")
