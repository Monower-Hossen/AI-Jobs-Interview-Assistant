from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import database.db as db_module
import pytest
from config.settings import settings
from database import (
    InterviewCallParticipant,
    InterviewCallRoom,
    InterviewCallSignal,
    User,
    UserSession,
    get_db_context,
)
from services import auth_service as auth
from services import live_call_media_service as media
from services import live_call_service as calls
from services import live_interview_coordination_service as coordination
from streamlit.testing.v1 import AppTest

from frontend.shared import live_call_component as component

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def stage4c_db(tmp_path):
    original_database = settings.database_path
    original_vectors = settings.vector_store_path
    media.clear_media_sessions()
    settings.database_path = str(tmp_path / "stage4c.db")
    settings.vector_store_path = str(tmp_path / "vectors")
    db_module.engine = None
    db_module.SessionLocal = None
    db_module.init_database()

    with db_module.get_db_context() as db:
        admin = User(
            email="admin@stage4c.com",
            username="stage4c-admin",
            full_name="Stage4C Admin",
            password_hash=auth.hash_password("Pass1234"),
            role="admin",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        candidate = User(
            email="candidate@stage4c.com",
            username="stage4c-candidate",
            full_name="Stage4C Candidate",
            password_hash=auth.hash_password("Pass1234"),
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        other = User(
            email="other@stage4c.com",
            username="stage4c-other",
            full_name="Stage4C Other",
            password_hash=auth.hash_password("Pass1234"),
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        db.add_all([admin, candidate, other])
        db.flush()
        db.add_all(
            [
                UserSession(session_id="admin-token", user_id=admin.id, label="admin"),
                UserSession(session_id="candidate-token", user_id=candidate.id, label="candidate"),
                UserSession(session_id="other-token", user_id=other.id, label="other"),
            ]
        )
        ids = {
            "admin": int(admin.id),
            "candidate": int(candidate.id),
            "other": int(other.id),
            "admin_token": "admin-token",
            "candidate_token": "candidate-token",
            "other_token": "other-token",
        }

    yield ids

    media.clear_media_sessions()
    db_module.close_database()
    db_module.engine = None
    db_module.SessionLocal = None
    settings.database_path = original_database
    settings.vector_store_path = original_vectors


def _fake_leases(monkeypatch, released: list[str]) -> None:
    sequence = count(1)

    def fake_acquire(task: str, *, owner_id: str, **kwargs: Any):
        return SimpleNamespace(lease_id=f"{task}-lease-{next(sequence)}"), None

    def fake_release(lease_id: str, **kwargs: Any) -> bool:
        released.append(lease_id)
        return True

    monkeypatch.setattr(media, "acquire_resource_lease", fake_acquire)
    monkeypatch.setattr(media, "release_resource_lease", fake_release)
    monkeypatch.setattr(media, "heartbeat_resource_lease", lambda *args, **kwargs: True)


def _create_started_room(ids: dict[str, Any], title: str = "Stage4C interview") -> dict[str, Any]:
    room = coordination.create_invitation(
        admin_user_id=ids["admin"],
        session_token=ids["admin_token"],
        candidate_user_id=ids["candidate"],
        title=title,
    )
    coordination.accept_invitation(
        room_id=room["room_id"],
        user_id=ids["candidate"],
        session_token=ids["candidate_token"],
    )
    return coordination.start_invitation(
        room_id=room["room_id"],
        user_id=ids["admin"],
        session_token=ids["admin_token"],
    )


def test_component_leave_event_triggers_authenticated_leave(monkeypatch) -> None:
    fake_st = SimpleNamespace(session_state={})
    monkeypatch.setattr(component, "st", fake_st)
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(component, "leave_call", lambda **kwargs: seen.append(kwargs))
    key = component._state_key("lc-leave")
    fake_st.session_state[key] = {
        "outbox": [{"kind": "leave", "payload": {}}],
        "outbox_ack": 0,
    }

    component._enqueue_outbox(
        room_id="lc-leave",
        role=calls.ROLE_CANDIDATE,
        recipient_role=calls.ROLE_ADMIN,
        user_id=9,
        session_token="candidate-token",
    )

    assert seen == [
        {"room_id": "lc-leave", "user_id": 9, "session_token": "candidate-token"}
    ]
    assert fake_st.session_state[key]["outbox"] == []
    assert fake_st.session_state[key]["outbox_ack"] == 1


def test_component_leave_and_join_guards_are_source_verified() -> None:
    source = component._JS
    assert "joining: false" in source
    assert "function beginJoin()" in source
    assert "if (state.joining || state.started)" in source
    assert 'emitSignal("leave", {})' in source
    assert 'catch (error) {\n        cleanup(false);' in source
    assert "state.joining = false;" in source
    assert "if (!cfg.enabled && state.started) {\n    cleanup(false);" in source


def test_media_heartbeat_poll_seconds_is_bounded() -> None:
    value = coordination.media_heartbeat_poll_seconds()
    assert isinstance(value, int)
    assert 5 <= value <= 60


def test_leave_enqueues_hangup_before_marking_left(stage4c_db) -> None:
    room = _create_started_room(stage4c_db)
    coordination.leave_call(
        room_id=room["room_id"],
        user_id=stage4c_db["candidate"],
        session_token=stage4c_db["candidate_token"],
    )

    with get_db_context() as db:
        row = db.query(InterviewCallRoom).filter_by(room_id=room["room_id"]).one()
        signal = (
            db.query(InterviewCallSignal)
            .filter_by(room_id=row.id, signal_type=calls.SIGNAL_HANGUP)
            .order_by(InterviewCallSignal.id.desc())
            .first()
        )
        participant = (
            db.query(InterviewCallParticipant)
            .filter_by(room_id=row.id, user_id=stage4c_db["candidate"])
            .one()
        )
        assert signal is not None
        assert signal.payload["reason"] == "user-left"
        assert participant.status == calls.PARTICIPANT_LEFT
        assert participant.left_at is not None
        assert signal.created_at <= participant.left_at


def test_repeated_leave_is_idempotent(stage4c_db) -> None:
    room = _create_started_room(stage4c_db)
    for _ in range(3):
        coordination.leave_call(
            room_id=room["room_id"],
            user_id=stage4c_db["candidate"],
            session_token=stage4c_db["candidate_token"],
        )

    with get_db_context() as db:
        row = db.query(InterviewCallRoom).filter_by(room_id=room["room_id"]).one()
        count = (
            db.query(InterviewCallSignal)
            .filter_by(
                room_id=row.id,
                signal_type=calls.SIGNAL_HANGUP,
                recipient_user_id=stage4c_db["admin"],
            )
            .count()
        )
    assert count == 1


def test_leave_with_revoked_session_is_rejected(stage4c_db) -> None:
    room = _create_started_room(stage4c_db)
    assert (
        auth.revoke_session(stage4c_db["candidate_token"], stage4c_db["candidate"]) is True
    )

    with pytest.raises(calls.LiveCallPermissionDenied):
        coordination.leave_call(
            room_id=room["room_id"],
            user_id=stage4c_db["candidate"],
            session_token=stage4c_db["candidate_token"],
        )


def test_terminal_cleanup_releases_both_roles_once(stage4c_db, monkeypatch) -> None:
    released: list[str] = []
    _fake_leases(monkeypatch, released)
    room = _create_started_room(stage4c_db)
    coordination.attach_media_session(
        room_id=room["room_id"],
        user_id=stage4c_db["admin"],
        session_token=stage4c_db["admin_token"],
    )
    coordination.attach_media_session(
        room_id=room["room_id"],
        user_id=stage4c_db["candidate"],
        session_token=stage4c_db["candidate_token"],
    )
    assert media.media_session_state(room["room_id"])["roles"] == [
        calls.ROLE_ADMIN,
        calls.ROLE_CANDIDATE,
    ]

    coordination.complete_invitation(
        room_id=room["room_id"],
        user_id=stage4c_db["admin"],
        session_token=stage4c_db["admin_token"],
    )

    assert released == ["video_processing-lease-1"]
    assert media.media_session_state(room["room_id"])["active"] is False
    media.reconcile_room_media(room_id=room["room_id"], terminal=True)
    assert released == ["video_processing-lease-1"]


def test_terminal_room_cannot_reacquire_media(stage4c_db, monkeypatch) -> None:
    released: list[str] = []
    _fake_leases(monkeypatch, released)
    room = _create_started_room(stage4c_db)
    coordination.complete_invitation(
        room_id=room["room_id"],
        user_id=stage4c_db["admin"],
        session_token=stage4c_db["admin_token"],
    )

    with pytest.raises(calls.LiveCallInvalidTransition):
        coordination.attach_media_session(
            room_id=room["room_id"],
            user_id=stage4c_db["admin"],
            session_token=stage4c_db["admin_token"],
        )
    assert released == []
    assert media.media_session_state(room["room_id"])["active"] is False


def test_stale_reconciliation_releases_stale_room_and_protects_active_room(
    stage4c_db, monkeypatch
) -> None:
    released: list[str] = []
    _fake_leases(monkeypatch, released)
    stale_room = _create_started_room(stage4c_db, "Stale room")
    active_room = _create_started_room(stage4c_db, "Active room")
    coordination.attach_media_session(
        room_id=stale_room["room_id"],
        user_id=stage4c_db["admin"],
        session_token=stage4c_db["admin_token"],
    )
    coordination.attach_media_session(
        room_id=active_room["room_id"],
        user_id=stage4c_db["admin"],
        session_token=stage4c_db["admin_token"],
    )

    now = datetime.now(UTC).replace(tzinfo=None)
    stale_at = now - timedelta(seconds=300)
    media._SESSIONS[stale_room["room_id"]].role_heartbeats[calls.ROLE_ADMIN] = stale_at
    media._SESSIONS[stale_room["room_id"]].last_heartbeat = stale_at
    media._SESSIONS[active_room["room_id"]].role_heartbeats[calls.ROLE_ADMIN] = now
    media._SESSIONS[active_room["room_id"]].last_heartbeat = now

    result = media.reconcile_stale_media_sessions(now=now, timeout_seconds=120)

    assert [item["room_id"] for item in result["released"]] == [stale_room["room_id"]]
    assert result["retained"] == [active_room["room_id"]]
    assert released == ["video_processing-lease-1"]
    assert media.media_session_state(stale_room["room_id"])["active"] is False
    assert media.media_session_state(active_room["room_id"])["active"] is True


def test_production_heartbeat_renews_active_session(stage4c_db, monkeypatch) -> None:
    released: list[str] = []
    _fake_leases(monkeypatch, released)
    room = _create_started_room(stage4c_db)
    coordination.attach_media_session(
        room_id=room["room_id"],
        user_id=stage4c_db["admin"],
        session_token=stage4c_db["admin_token"],
    )
    before = media.media_session_state(room["room_id"])

    assert coordination.heartbeat_media_session(
        room_id=room["room_id"],
        user_id=stage4c_db["admin"],
        session_token=stage4c_db["admin_token"],
    )
    after = media.media_session_state(room["room_id"])
    assert after["last_heartbeat"] >= before["last_heartbeat"]
    assert released == []

    coordination.leave_call(
        room_id=room["room_id"],
        user_id=stage4c_db["admin"],
        session_token=stage4c_db["admin_token"],
    )
    assert not coordination.heartbeat_media_session(
        room_id=room["room_id"],
        user_id=stage4c_db["admin"],
        session_token=stage4c_db["admin_token"],
    )
    assert released == ["video_processing-lease-1"]


def test_turn_credentials_are_not_rendered(stage4c_db, monkeypatch) -> None:
    released: list[str] = []
    _fake_leases(monkeypatch, released)
    room = _create_started_room(stage4c_db)
    username = "stage4c-turn-user-should-not-render"
    credential = "stage4c-turn-secret-should-not-render"
    monkeypatch.setattr(settings, "webrtc_turn_urls", ["turn:turn.example:3478"])
    monkeypatch.setattr(settings, "webrtc_turn_username", username)
    monkeypatch.setattr(settings, "webrtc_turn_credential", credential)

    monkeypatch.syspath_prepend(str(PROJECT_ROOT))
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "backend"))
    for module_name in (
        "frontend.shared.live_call_component",
        "frontend.shared.live_invitation_ui",
        "frontend.user.live_interview_invitations",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    account = auth.get_account(stage4c_db["candidate"])
    assert account is not None
    app = AppTest.from_file(
        str(PROJECT_ROOT / "frontend/user/live_interview_invitations.py"),
        default_timeout=30,
    )
    app.session_state["account"] = account
    app.session_state["user_id"] = int(account.id)
    app.session_state["user_session_id"] = stage4c_db["candidate_token"]
    app.run(timeout=60)

    rendered = "\n".join(
        str(element.value)
        for element in app.main
        if getattr(element, "value", None) is not None
    )
    assert not app.exception
    assert room["room_id"] in rendered
    assert username not in rendered
    assert credential not in rendered
