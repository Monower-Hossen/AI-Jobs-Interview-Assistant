from __future__ import annotations

import io
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
UNIFIED_PAGE = PROJECT_ROOT / "frontend" / "user" / "unified_history.py"


@pytest.fixture
def unified_history_db(tmp_path):
    import database.db as db_module
    from config.settings import settings
    from database import User, UserSession
    from services import auth_service as auth

    original_database = settings.database_path
    original_vectors = settings.vector_store_path
    original_media_root = settings.live_media_root
    settings.database_path = str(tmp_path / "unified_history.db")
    settings.vector_store_path = str(tmp_path / "vectors")
    settings.live_media_root = str(tmp_path / "live_interviews")
    db_module.engine = None
    db_module.SessionLocal = None
    db_module.init_database()

    with db_module.get_db_context() as db:
        admin = User(
            email="admin@history.test",
            username="history-admin",
            full_name="History Admin",
            password_hash=auth.hash_password("Pass1234"),
            role="admin",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        one = User(
            email="one@history.test",
            username="history-one",
            full_name="History One",
            password_hash=auth.hash_password("Pass1234"),
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        two = User(
            email="two@history.test",
            username="history-two",
            full_name="History Two",
            password_hash=auth.hash_password("Pass1234"),
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        three = User(
            email="three@history.test",
            username="history-three",
            full_name="History Three",
            password_hash=auth.hash_password("Pass1234"),
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        db.add_all([admin, one, two, three])
        db.flush()
        workspaces = {
            "admin": UserSession(session_id="history-admin-token", user_id=admin.id, label="admin"),
            "one": UserSession(session_id="history-one-token", user_id=one.id, label="one"),
            "two": UserSession(session_id="history-two-token", user_id=two.id, label="two"),
            "three": UserSession(session_id="history-three-token", user_id=three.id, label="three"),
        }
        db.add_all(workspaces.values())
        db.flush()
        ids = {
            "admin_id": int(admin.id),
            "one_id": int(one.id),
            "two_id": int(two.id),
            "three_id": int(three.id),
            "admin_token": "history-admin-token",
            "one_token": "history-one-token",
            "two_token": "history-two-token",
            "three_token": "history-three-token",
        }
        for key, workspace in workspaces.items():
            ids[f"{key}_workspace_id"] = int(workspace.id)

    yield ids

    db_module.close_database()
    db_module.SessionLocal = None
    settings.database_path = original_database
    settings.vector_store_path = original_vectors
    settings.live_media_root = original_media_root


@pytest.fixture
def deterministic_media():
    from contextlib import nullcontext

    import services.live_interview_media_service as media_service

    original = media_service.acquire_heavy_task
    media_service.acquire_heavy_task = lambda *args, **kwargs: nullcontext()
    yield
    media_service.acquire_heavy_task = original


def _seed_job_row(
    unified_history_db,
    *,
    workspace_key: str,
    mode: str,
    title: str,
    company: str,
    score: float | None = 7.5,
    status: str = "completed",
    created: datetime | None = None,
) -> int:
    from database import InterviewSession, get_db_context

    created = created or datetime.now(UTC).replace(tzinfo=None)
    with get_db_context() as db:
        row = InterviewSession(
            session_id=int(unified_history_db[f"{workspace_key}_workspace_id"]),
            interview_type="Job interview",
            difficulty="medium",
            answer_mode="text",
            interview_mode=mode,
            status=status,
            overall_score=score,
            is_job_interview=True,
            blueprint_json={"target_role": title, "company_context": company},
            report_json={"answers": []},
            started_at=created,
            completed_at=created + timedelta(minutes=30) if status == "completed" else None,
            created_at=created,
        )
        db.add(row)
        db.flush()
        return int(row.id)


def _seed_legacy_row(unified_history_db, *, score: float = 6.0) -> int:
    from database import InterviewSession, get_db_context

    created = datetime.now(UTC).replace(tzinfo=None)
    with get_db_context() as db:
        row = InterviewSession(
            session_id=int(unified_history_db["one_workspace_id"]),
            interview_type="Technical",
            difficulty="medium",
            status="completed",
            overall_score=score,
            started_at=created,
            completed_at=created,
            created_at=created,
        )
        db.add(row)
        db.flush()
        return int(row.id)


def _wave_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x01\x00" * 1_600)
    return buffer.getvalue()


def _seed_recording(unified_history_db, *, workspace_key: str = "one") -> tuple[int, int]:
    from database import InterviewSession, get_db_context
    from services.live_interview_media_service import ensure_live_recording, save_live_media

    user_key = f"{workspace_key}_id"
    token_key = f"{workspace_key}_token"
    created = datetime.now(UTC).replace(tzinfo=None)
    with get_db_context() as db:
        interview = InterviewSession(
            session_id=int(unified_history_db[f"{workspace_key}_workspace_id"]),
            interview_type="live_webcam",
            interview_mode="video",
            answer_mode="audio",
            status="completed",
            report_json={"answers": []},
            created_at=created,
        )
        db.add(interview)
        db.flush()
        interview_id = int(interview.id)
    recording_id = ensure_live_recording(
        interview_id=interview_id,
        user_id=int(unified_history_db[user_key]),
        session_token=str(unified_history_db[token_key]),
        consent_given=True,
        consent_at=created.isoformat(),
    )
    media_id = save_live_media(
        recording_id=recording_id,
        interview_id=interview_id,
        user_id=int(unified_history_db[user_key]),
        session_token=str(unified_history_db[token_key]),
        media_type="audio",
        data=_wave_bytes(),
        duration_seconds=1.0,
        metadata={"question_number": 1},
    )
    return interview_id, media_id


def _app(unified_history_db, monkeypatch, *, user_key: str = "one"):
    from streamlit.testing.v1 import AppTest

    from frontend.user import unified_history

    navigations: list[str] = []
    monkeypatch.setattr(unified_history, "go_to", navigations.append)
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30)
    app.session_state["db_initialized"] = True
    app.session_state["user_id"] = unified_history_db[f"{user_key}_id"]
    app.session_state["user_session_id"] = unified_history_db[f"{user_key}_token"]
    app.session_state["account"] = SimpleNamespace(
        id=unified_history_db[f"{user_key}_id"],
        email=f"{user_key}@history.test",
        full_name=f"History {user_key}",
        role="user",
        account_status="active",
        is_active=True,
        must_change_password=False,
    )
    app.session_state["must_change_password"] = False
    app.run()
    app.switch_page("frontend/user/interview_history.py").run()
    return app, navigations


def _subheads(app) -> str:
    return "\n".join(str(item.value) for item in app.subheader)


def _frames(app) -> str:
    return "\n".join(str(item.value) for item in app.dataframe)


def _markdown(app) -> str:
    return "\n".join(str(item.value) for item in app.markdown)


def _labels(app) -> list[str]:
    return [str(item.label) for item in app.button]


def test_unified_page_renders_all_sections(unified_history_db, monkeypatch, deterministic_media):
    _seed_job_row(
        unified_history_db,
        workspace_key="one",
        mode="avatar",
        title="Avatar Engineer",
        company="Acme",
    )
    _seed_job_row(
        unified_history_db,
        workspace_key="one",
        mode="video",
        title="Webcam Engineer",
        company="Globex",
    )
    _seed_legacy_row(unified_history_db)
    from services.live_interview_coordination_service import create_invitation

    create_invitation(
        admin_user_id=unified_history_db["admin_id"],
        session_token=unified_history_db["admin_token"],
        candidate_user_id=unified_history_db["one_id"],
        title="Platform Engineer Loop",
    )
    _seed_recording(unified_history_db)

    app, _ = _app(unified_history_db, monkeypatch)

    assert not app.exception, [str(item.value) for item in app.exception]
    headings = _subheads(app)
    for heading in (
        "Job Interviews",
        "Earlier practice sessions",
        "Completed and saved sessions",
        "Candidate live interviews",
        "Saved live recordings",
    ):
        assert heading in headings
    frames = _frames(app)
    for value in ("Avatar Engineer", "Webcam Engineer", "Platform Engineer Loop"):
        assert value in frames
    assert "Microphone audio" in _markdown(app)
    labels = _labels(app)
    for label in (
        "Open Job Interview report",
        "Open Live Webcam report",
        "Open live interview details",
        "Open details",
    ):
        assert label in labels


def test_report_buttons_use_distinct_keys_and_stage4a_targets(
    unified_history_db, monkeypatch
):
    from frontend.user.unified_history import JOB_REPORT_KEY, LIVE_REPORT_KEY

    avatar_id = _seed_job_row(
        unified_history_db,
        workspace_key="one",
        mode="avatar",
        title="Avatar Engineer",
        company="Acme",
    )
    video_id = _seed_job_row(
        unified_history_db,
        workspace_key="one",
        mode="video",
        title="Webcam Engineer",
        company="Globex",
    )

    app, navigations = _app(unified_history_db, monkeypatch)
    next(button for button in app.button if button.label == "Open Job Interview report").click().run()
    assert app.session_state[JOB_REPORT_KEY] == avatar_id
    assert navigations == ["job_interview"]

    app, navigations = _app(unified_history_db, monkeypatch)
    next(button for button in app.button if button.label == "Open Live Webcam report").click().run()
    assert app.session_state[LIVE_REPORT_KEY] == video_id
    assert navigations == ["live_webcam_interview"]


def test_tabs_and_report_keys_are_preserved_in_source() -> None:
    source = UNIFIED_PAGE.read_text(encoding="utf-8")
    for label in ("Job Interview", "Live Webcam", "Live Sessions", "Recordings"):
        assert f'"{label}"' in source
    assert 'JOB_REPORT_KEY = "_job_interview_report_id"' in source
    assert 'LIVE_REPORT_KEY = "live_webcam_report_id"' in source
    assert 'report_target="job_interview"' in source
    assert 'report_target="live_webcam_interview"' in source
    assert 'show_live_recording_history(show_header=False)' in source


def test_missing_score_is_not_shown_as_zero(unified_history_db, monkeypatch) -> None:
    _seed_job_row(
        unified_history_db,
        workspace_key="one",
        mode="avatar",
        title="Unscored Avatar",
        company="Acme",
        score=None,
    )
    app, _ = _app(unified_history_db, monkeypatch)

    assert not app.exception, [str(item.value) for item in app.exception]
    frames = _frames(app)
    assert "Unscored Avatar" in frames
    assert "Unavailable" in frames
    assert "0.0/10" not in frames


def test_filters_exclude_unscored_and_search_matches_title() -> None:
    from frontend.user.unified_history import _apply_filters

    rows = [
        {"id": 1, "job_title": "Avatar Engineer", "status": "completed", "created_at": None, "overall_score": 8.0},
        {"id": 2, "job_title": "Avatar Trainee", "status": "completed", "created_at": None, "overall_score": None},
        {"id": 3, "job_title": "Webcam Engineer", "status": "completed", "created_at": None, "overall_score": 0.0},
    ]
    filtered = _apply_filters(
        rows,
        search_fields=("job_title", "status"),
        date_field="created_at",
        score_field="overall_score",
        search="avatar",
        status="completed",
        minimum_score=1.0,
    )
    assert [int(row["id"]) for row in filtered] == [1]


def test_other_users_saved_interviews_are_not_visible(unified_history_db, monkeypatch) -> None:
    _seed_job_row(
        unified_history_db,
        workspace_key="one",
        mode="avatar",
        title="Private Avatar",
        company="Acme",
    )
    app, _ = _app(unified_history_db, monkeypatch, user_key="two")

    assert not app.exception, [str(item.value) for item in app.exception]
    assert "Private Avatar" not in _frames(app)


def test_recording_access_stays_owner_scoped(
    unified_history_db, deterministic_media
) -> None:
    from services.live_interview_media_service import (
        LiveMediaError,
        list_user_recordings,
        read_live_media,
    )

    _, media_id = _seed_recording(unified_history_db)

    with pytest.raises(LiveMediaError):
        read_live_media(
            media_id,
            actor_user_id=unified_history_db["two_id"],
            session_token=unified_history_db["two_token"],
        )
    assert list_user_recordings(
        user_id=unified_history_db["two_id"],
        session_token=unified_history_db["two_token"],
    ) == []


def test_live_session_section_is_owner_scoped(unified_history_db, monkeypatch) -> None:
    from services.live_interview_coordination_service import create_invitation

    create_invitation(
        admin_user_id=unified_history_db["admin_id"],
        session_token=unified_history_db["admin_token"],
        candidate_user_id=unified_history_db["one_id"],
        title="Private Live Loop",
    )

    visible, _ = _app(unified_history_db, monkeypatch, user_key="one")
    hidden, _ = _app(unified_history_db, monkeypatch, user_key="three")

    assert not visible.exception, [str(item.value) for item in visible.exception]
    assert not hidden.exception, [str(item.value) for item in hidden.exception]
    assert "Private Live Loop" in _frames(visible)
    assert "Private Live Loop" not in _frames(hidden)


def test_legacy_routes_and_recording_safeguards_remain_intact() -> None:
    history_source = (PROJECT_ROOT / "frontend" / "user" / "interview_history.py").read_text(
        encoding="utf-8"
    )
    recording_source = (
        PROJECT_ROOT / "frontend" / "user" / "live_recording_history.py"
    ).read_text(encoding="utf-8")
    app_source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    navigation_source = (PROJECT_ROOT / "frontend" / "shared" / "navigation.py").read_text(
        encoding="utf-8"
    )

    assert "show_unified_interview_history()" in history_source
    assert 'if __name__ == "__main__":' in recording_source
    assert "show_live_recording_history(show_header=False)" in UNIFIED_PAGE.read_text(
        encoding="utf-8"
    )
    assert "Delete live recording?" in recording_source
    assert '"_live_media_delete_confirm"' in recording_source
    assert "retry_live_media_deletions" in recording_source
    assert '"Interview History"' in app_source
    assert '"Recording History"' in app_source
    assert '"job_interview"' in navigation_source
    assert 'target != PAGES["live_webcam"]' in navigation_source
