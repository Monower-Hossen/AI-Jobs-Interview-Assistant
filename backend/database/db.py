import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from config.settings import settings
from database.models import Base
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

engine = None
SessionLocal: sessionmaker | None = None
_MAX_DETACH_REFRESH = 50
_COMMIT_SUCCEEDED_KEY = "phase32_commit_succeeded"
_db_operation_gate = threading.RLock()
_gate_condition = threading.Condition(_db_operation_gate)
_gate_local = threading.local()
_gate_depth = 0
_active_sessions = 0


@contextmanager
def database_operation_gate() -> Generator[None, None, None]:
    global _gate_depth
    thread_depth = int(getattr(_gate_local, "depth", 0))
    with _db_operation_gate:
        _gate_depth += 1
        _gate_local.depth = thread_depth + 1
        _gate_condition.notify_all()
        try:
            while _active_sessions > 0:
                if int(getattr(_gate_local, "depth", 0)) <= 1:
                    _gate_condition.wait()
                else:
                    break
            yield
        finally:
            _gate_depth -= 1
            _gate_local.depth = thread_depth
            _gate_condition.notify_all()


@contextmanager
def _session_gate() -> Generator[None, None, None]:
    global _active_sessions
    with _db_operation_gate:
        while _gate_depth > 0 and not getattr(_gate_local, "depth", 0):
            _gate_condition.wait()
        _active_sessions += 1
    try:
        yield
    finally:
        with _db_operation_gate:
            _active_sessions -= 1
            _gate_condition.notify_all()


def _detach_all(db: Session) -> None:
    """Detach every instance with its column values loaded.

    Commits and rollbacks mark column attributes as expired, and closed
    sessions cannot reload them, so callers that keep rows around after the
    context block would hit DetachedInstanceError. Refreshing first makes the
    objects detached but fully readable.
    """
    try:
        db.flush()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass

    instances = list(db.identity_map.values())
    if len(instances) <= _MAX_DETACH_REFRESH:
        for instance in instances:
            try:
                db.refresh(instance)
            except Exception:
                pass

    try:
        db.expunge_all()
    except Exception:
        pass


_ADD_COLUMNS: dict[str, dict[str, str]] = {
    "user_sessions": {
        "user_id": "INTEGER",
        "label": "TEXT",
        "revoked_at": "DATETIME",
    },
    "interview_sessions": {
        "duration_seconds": "INTEGER DEFAULT 0",
        "answer_mode": "TEXT DEFAULT 'text'",
        "job_post_id": "INTEGER REFERENCES job_posts(id) ON DELETE SET NULL",
        "interview_mode": "TEXT DEFAULT 'text'",
        "duration_minutes": "INTEGER DEFAULT 0",
        "consent_given": "INTEGER DEFAULT 0",
        "is_job_interview": "INTEGER DEFAULT 0",
        "blueprint_json": "TEXT",
        "report_json": "TEXT",
    },
    "interview_questions": {
        "problem_solving": "REAL",
        "structure": "REAL",
        "rubric_version": "TEXT DEFAULT 'v2'",
        "ideal_answer": "TEXT",
        "is_follow_up": "INTEGER DEFAULT 0",
        "parent_question_id": (
            "INTEGER REFERENCES interview_questions(id) ON DELETE SET NULL"
        ),
        "focus_skill": "TEXT",
        "difficulty_adjustment": "TEXT",
    },
    "users": {
        "username": "TEXT",
        "phone_number": "TEXT",
        "role": "TEXT DEFAULT 'user'",
        "account_status": "TEXT DEFAULT 'active'",
        "is_active": "INTEGER DEFAULT 1",
        "is_verified": "INTEGER DEFAULT 1",
        "must_change_password": "INTEGER DEFAULT 0",
        "additional_profile_info": "TEXT",
        "created_by_admin_id": "INTEGER",
        "workspace_token": "TEXT",
        "created_at": "DATETIME DEFAULT CURRENT_TIMESTAMP",
        "last_login": "DATETIME",
    },
    "user_settings": {
        "preferred_interview_categories": "TEXT DEFAULT '[]'",
        "learning_preferences": "TEXT DEFAULT '{}'",
        "notification_preferences": "TEXT DEFAULT '{}'",
    },
    "learning_roadmaps": {
        "summary": "TEXT DEFAULT ''",
        "career_target": "TEXT DEFAULT ''",
        "existing_skills": "TEXT DEFAULT '[]'",
        "skill_level": "TEXT DEFAULT 'intermediate'",
        "hours_per_day": "REAL DEFAULT 1.0",
        "duration_weeks": "INTEGER DEFAULT 4",
        "status": "TEXT DEFAULT 'active'",
        "phases": "TEXT DEFAULT '[]'",
        "skill_gap": "TEXT DEFAULT '{}'",
        "recommendations": "TEXT DEFAULT '[]'",
        "topic_mastery": "TEXT DEFAULT '{}'",
        "generation_fingerprint": "TEXT",
    },
    "learning_tasks": {
        "task_type": "TEXT DEFAULT 'daily'",
        "week_number": "INTEGER",
        "day_number": "INTEGER",
        "objective": "TEXT DEFAULT ''",
        "estimated_minutes": "INTEGER DEFAULT 30",
        "priority": "TEXT DEFAULT 'medium'",
    },
    "assessments": {
        "roadmap_id": "INTEGER REFERENCES learning_roadmaps(id) ON DELETE CASCADE",
        "week_number": "INTEGER",
        "question_count": "INTEGER DEFAULT 0",
    },
}


def _sanitize_username(base: str) -> str:
    cleaned = "".join(ch for ch in (base or "").lower() if ch.isalnum() or ch in {"_", ".", "-"})
    cleaned = cleaned.strip("._-")
    return cleaned or "user"


def _backfill_users(bind: Any) -> None:
    inspector = inspect(bind)
    if "users" not in inspector.get_table_names():
        return
    with bind.begin() as connection:
        # Sync status/is_active for existing rows
        connection.execute(text("""
                UPDATE users
                SET account_status = CASE
                    WHEN COALESCE(account_status, '') = '' THEN
                        CASE WHEN is_active THEN 'active' ELSE 'inactive' END
                    ELSE account_status
                END
                """))
        connection.execute(text("""
                UPDATE users
                SET is_active = CASE WHEN account_status = 'active' THEN 1 ELSE 0 END
                """))
        connection.execute(
            text("UPDATE users SET must_change_password = 0 WHERE must_change_password IS NULL")
        )
        connection.execute(text("UPDATE users SET is_verified = 1 WHERE is_verified IS NULL"))
        connection.execute(
            text("UPDATE users SET role = 'user' WHERE role IS NULL OR TRIM(role) = ''")
        )
        connection.execute(text("UPDATE users SET is_active = 0 WHERE is_active IS NULL"))

        rows = connection.execute(
            text("SELECT id, email, username FROM users ORDER BY id")
        ).fetchall()
        used = {str(row[2]).strip().lower() for row in rows if row[2]}
        for row in rows:
            user_id, email, username = row[0], (row[1] or ""), row[2]
            if username and str(username).strip():
                continue
            local = _sanitize_username(str(email).split("@", 1)[0])
            candidate = local or f"user{user_id}"
            suffix = 0
            final = candidate
            while final.lower() in used:
                suffix += 1
                final = f"{candidate}{suffix}"
            used.add(final.lower())
            connection.execute(
                text("UPDATE users SET username = :username WHERE id = :id"),
                {"username": final, "id": user_id},
            )

        # Create unique index for usernames if missing (SQLite: multiple NULLs allowed)
        indexes = {idx["name"] for idx in inspector.get_indexes("users")}
        if "ix_users_username_unique" not in indexes:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username_unique ON users(username)"
                )
            )


def _migrate_schema(bind: Any) -> None:
    """Add newer columns to databases created by earlier versions.

    ``create_all`` never alters existing tables, so this keeps historic data intact
    while making the schema match the current ORM models.
    """
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    with bind.begin() as connection:
        for table, columns in _ADD_COLUMNS.items():
            if table not in existing_tables:
                continue
            present = {column["name"] for column in inspector.get_columns(table)}
            for name, definition in columns.items():
                if name not in present:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
        if "users" in existing_tables:
            indexes = {idx["name"] for idx in inspector.get_indexes("users")}
            if "ix_users_workspace_token_unique" not in indexes:
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_workspace_token_unique "
                        "ON users(workspace_token)"
                    )
                )
    _backfill_users(bind)


def init_database() -> None:
    global engine, SessionLocal

    if engine is not None:
        engine.dispose()

    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite:///{settings.database_path}"

    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
        pool_pre_ping=True,
        echo=False,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    SessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
        expire_on_commit=False,
    )

    Base.metadata.create_all(bind=engine)
    _migrate_schema(engine)


@contextmanager
def get_db_context() -> Generator[Session, None, None]:
    """Transactional session that stays readable after the block exits.

    ORM objects are detached on close instead of expired, so UI code can keep
    rendering rows fetched inside the ``with`` block.
    """
    with _session_gate():
        if SessionLocal is None:
            init_database()

        assert SessionLocal is not None
        db = SessionLocal()
        db.info[_COMMIT_SUCCEEDED_KEY] = False
        try:
            yield db
            db.commit()
            db.info[_COMMIT_SUCCEEDED_KEY] = True
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                _detach_all(db)
            finally:
                db.close()


@contextmanager
def get_readonly_db() -> Generator[Session, None, None]:
    """Read-only session context: no commit, rollback on error, safe for reporting."""
    with _session_gate():
        if SessionLocal is None:
            init_database()

        assert SessionLocal is not None
        db = SessionLocal()
        try:
            yield db
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                _detach_all(db)
            finally:
                db.close()


def create_session() -> Session:
    """Create a new SQLAlchemy Session without a context manager."""
    if SessionLocal is None:
        init_database()

    assert SessionLocal is not None
    return SessionLocal()


@contextmanager
def session_scope(*, readonly: bool = False) -> Generator[Session, None, None]:
    """Explicit transaction boundary helper for service-layer operations."""
    if readonly:
        with get_readonly_db() as db:
            yield db
    else:
        with get_db_context() as db:
            yield db


def close_database() -> None:
    global engine
    if engine:
        engine.dispose()
        engine = None
