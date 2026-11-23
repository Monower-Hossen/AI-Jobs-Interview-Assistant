from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import database.db as db_module
import pytest
from config.settings import settings
from database import InterviewCallRoom, User, UserSession, get_db_context
from services import auth_service as auth
from services import live_call_service as calls
from services import live_interview_coordination_service as coordination
from streamlit.testing.v1 import AppTest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def stage3_db(tmp_path):
    original_database = settings.database_path
    original_vectors = settings.vector_store_path
    settings.database_path = str(tmp_path / "stage3.db")
    settings.vector_store_path = str(tmp_path / "vectors")
    db_module.engine = None
    db_module.SessionLocal = None
    db_module.init_database()

    with db_module.get_db_context() as db:
        admin = User(
            email="admin@stage3.com",
            username="stage3-admin",
            full_name="Stage3 Admin",
            password_hash=auth.hash_password("Pass1234"),
            role="admin",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        candidate = User(
            email="candidate@stage3.com",
            username="stage3-candidate",
            full_name="Stage3 Candidate",
            password_hash=auth.hash_password("Pass1234"),
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        other = User(
            email="other@stage3.com",
            username="stage3-other",
            full_name="Stage3 Other",
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

    db_module.close_database()
    db_module.engine = None
    db_module.SessionLocal = None
    settings.database_path = original_database
    settings.vector_store_path = original_vectors


def _create(stage3_db, title: str = "Backend interview") -> dict:
    return coordination.create_invitation(
        admin_user_id=stage3_db["admin"],
        session_token=stage3_db["admin_token"],
        candidate_user_id=stage3_db["candidate"],
        title=title,
    )


def test_invitation_workflow_hides_tokens_and_uses_authenticated_acceptance(stage3_db) -> None:
    room = _create(stage3_db)

    assert room["status"] == calls.INVITED
    assert room["role"] == calls.ROLE_ADMIN
    assert room["title"] == "Backend interview"
    assert "invite_token_hash" not in room
    assert "invite_token" not in room
    assert stage3_db["candidate_token"] not in repr(room)

    candidate_rooms = coordination.list_invitations(
        user_id=stage3_db["candidate"],
        session_token=stage3_db["candidate_token"],
    )
    assert len(candidate_rooms) == 1
    assert candidate_rooms[0]["role"] == calls.ROLE_CANDIDATE

    accepted = coordination.accept_invitation(
        room_id=room["room_id"],
        user_id=stage3_db["candidate"],
        session_token=stage3_db["candidate_token"],
    )
    assert accepted["status"] == calls.ACCEPTED

    started = coordination.start_invitation(
        room_id=room["room_id"],
        user_id=stage3_db["admin"],
        session_token=stage3_db["admin_token"],
    )
    assert started["status"] == calls.IN_PROGRESS

    completed = coordination.complete_invitation(
        room_id=room["room_id"],
        user_id=stage3_db["candidate"],
        session_token=stage3_db["candidate_token"],
    )
    assert completed["status"] == calls.COMPLETED
    assert completed["ended_at"] is not None


def test_wrong_account_and_revoked_session_cannot_access_room(stage3_db) -> None:
    room = _create(stage3_db)

    with pytest.raises(calls.LiveCallPermissionDenied):
        coordination.get_invitation(
            room_id=room["room_id"],
            user_id=stage3_db["other"],
            session_token=stage3_db["other_token"],
        )

    assert auth.revoke_session(stage3_db["candidate_token"], stage3_db["candidate"]) is True
    assert auth.owned_session_id(stage3_db["candidate_token"], stage3_db["candidate"]) is None
    with pytest.raises(calls.LiveCallPermissionDenied):
        coordination.get_invitation(
            room_id=room["room_id"],
            user_id=stage3_db["candidate"],
            session_token=stage3_db["candidate_token"],
        )


def test_signal_roundtrip_and_payload_hardening(stage3_db) -> None:
    room = _create(stage3_db)
    coordination.accept_invitation(
        room_id=room["room_id"],
        user_id=stage3_db["candidate"],
        session_token=stage3_db["candidate_token"],
    )
    coordination.start_invitation(
        room_id=room["room_id"],
        user_id=stage3_db["admin"],
        session_token=stage3_db["admin_token"],
    )

    sent = coordination.send_signal(
        room_id=room["room_id"],
        user_id=stage3_db["admin"],
        session_token=stage3_db["admin_token"],
        signal_type=calls.SIGNAL_OFFER,
        recipient_role=calls.ROLE_CANDIDATE,
        payload={"sdp": "v=0\r\nfake-session-description"},
    )
    assert sent["signal_type"] == calls.SIGNAL_OFFER
    assert "token" not in repr(sent)

    received = coordination.receive_signals(
        room_id=room["room_id"],
        user_id=stage3_db["candidate"],
        session_token=stage3_db["candidate_token"],
    )
    assert len(received) == 1
    assert received[0]["sender_role"] == calls.ROLE_ADMIN
    assert received[0]["payload"]["sdp"].startswith("v=0")
    assert coordination.receive_signals(
        room_id=room["room_id"],
        user_id=stage3_db["candidate"],
        session_token=stage3_db["candidate_token"],
    ) == []

    with pytest.raises(calls.LiveCallInvalidSignal):
        coordination.send_signal(
            room_id=room["room_id"],
            user_id=stage3_db["admin"],
            session_token=stage3_db["admin_token"],
            signal_type=calls.SIGNAL_ICE_CANDIDATE,
            recipient_role=calls.ROLE_CANDIDATE,
            payload={"candidate": "candidate:1", "session_token": "should-not-pass"},
        )
    with pytest.raises(calls.LiveCallInvalidSignal):
        coordination.send_signal(
            room_id=room["room_id"],
            user_id=stage3_db["admin"],
            session_token=stage3_db["admin_token"],
            signal_type=calls.SIGNAL_OFFER,
            recipient_role=calls.ROLE_CANDIDATE,
            payload={"sdp": "v=0", "unexpected": True},
        )


def test_terminal_state_cannot_be_reopened_and_cancel_is_pending_only(stage3_db) -> None:
    room = _create(stage3_db)
    cancelled = coordination.cancel_invitation(
        room_id=room["room_id"],
        user_id=stage3_db["admin"],
        session_token=stage3_db["admin_token"],
    )
    assert cancelled["status"] == calls.CANCELLED

    with pytest.raises(calls.LiveCallInvalidTransition):
        coordination.accept_invitation(
            room_id=room["room_id"],
            user_id=stage3_db["candidate"],
            session_token=stage3_db["candidate_token"],
        )
    with pytest.raises(calls.LiveCallInvalidTransition):
        coordination.cancel_invitation(
            room_id=room["room_id"],
            user_id=stage3_db["admin"],
            session_token=stage3_db["admin_token"],
        )


def test_expired_room_blocks_signaling(stage3_db) -> None:
    room = _create(stage3_db)
    coordination.accept_invitation(
        room_id=room["room_id"],
        user_id=stage3_db["candidate"],
        session_token=stage3_db["candidate_token"],
    )
    with get_db_context() as db:
        row = db.query(InterviewCallRoom).filter_by(room_id=room["room_id"]).first()
        row.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        row.updated_at = row.expires_at

    with pytest.raises(calls.LiveCallExpired):
        coordination.send_signal(
            room_id=room["room_id"],
            user_id=stage3_db["admin"],
            session_token=stage3_db["admin_token"],
            signal_type=calls.SIGNAL_PING,
            recipient_role=calls.ROLE_CANDIDATE,
            payload={},
        )


def test_signal_poll_seconds_is_bounded() -> None:
    value = coordination.signal_poll_seconds()
    assert isinstance(value, int)
    assert 1 <= value <= 30


def _render_page(monkeypatch, page_path: str, account, token: str) -> AppTest:
    monkeypatch.syspath_prepend(str(PROJECT_ROOT))
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "backend"))
    app = AppTest.from_file(str(PROJECT_ROOT / page_path), default_timeout=30)
    app.session_state["account"] = account
    app.session_state["user_id"] = int(account.id)
    app.session_state["user_session_id"] = token
    app.run(timeout=60)
    return app


def _visible_text(app: AppTest) -> str:
    parts: list[str] = []
    for element in app.main:
        value = getattr(element, "value", None)
        if value is not None:
            parts.append(str(value))
    return "\n".join(parts)


def test_candidate_page_renders_invitation_without_exposing_session_token(stage3_db, monkeypatch) -> None:
    room = _create(stage3_db)
    account = auth.get_account(stage3_db["candidate"])
    assert account is not None
    app = _render_page(
        monkeypatch,
        "frontend/user/live_interview_invitations.py",
        account,
        stage3_db["candidate_token"],
    )

    assert not app.exception
    rendered = _visible_text(app)
    assert room["room_id"] in rendered
    assert "Backend interview" in rendered
    assert stage3_db["candidate_token"] not in rendered
    assert "real-time WebRTC interviews" in rendered


def test_admin_page_renders_invitation_form_and_existing_room(stage3_db, monkeypatch) -> None:
    room = _create(stage3_db)
    account = auth.get_account(stage3_db["admin"])
    assert account is not None
    app = _render_page(
        monkeypatch,
        "frontend/admin/live_interview_invitations.py",
        account,
        stage3_db["admin_token"],
    )

    assert not app.exception
    rendered = _visible_text(app)
    assert room["room_id"] in rendered
    assert "Invite a candidate" in rendered
    assert stage3_db["admin_token"] not in rendered
