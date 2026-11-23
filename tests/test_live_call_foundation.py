from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from database import (
    InterviewCallParticipant,
    InterviewCallRoom,
    InterviewCallSignal,
    get_db_context,
)
from services.live_call_service import (
    ACCEPTED,
    COMPLETED,
    EXPIRED,
    IN_PROGRESS,
    INVITED,
    ROLE_ADMIN,
    ROLE_CANDIDATE,
    SIGNAL_OFFER,
    LiveCallExpired,
    LiveCallInvalidTransition,
    LiveCallPermissionDenied,
    accept_invite,
    cancel_invite,
    complete_call,
    create_call_room,
    drain_signals,
    enqueue_signal,
    expire_stale_invites,
    require_call_access,
    start_call,
)


@pytest.fixture
def live_call_db(tmp_path):
    import database.db as db_module
    from config.settings import settings
    from database import User, UserSession
    from services import auth_service as auth

    original_database = settings.database_path
    original_vectors = settings.vector_store_path
    settings.database_path = str(tmp_path / "live-calls.db")
    settings.vector_store_path = str(tmp_path / "vectors")
    db_module.engine = None
    db_module.SessionLocal = None
    db_module.init_database()

    with db_module.get_db_context() as db:
        admin = User(
            email="admin@live.com",
            username="live-admin",
            full_name="Live Admin",
            password_hash=auth.hash_password("Pass1234"),
            role="admin",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        candidate = User(
            email="candidate@live.com",
            username="live-candidate",
            full_name="Live Candidate",
            password_hash=auth.hash_password("Pass1234"),
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        other = User(
            email="other@live.com",
            username="live-other",
            full_name="Other User",
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


def test_create_room_hashes_invite_and_scopes_access(live_call_db) -> None:
    with get_db_context() as db:
        room, token = create_call_room(
            db,
            admin_user_id=live_call_db["admin"],
            candidate_user_id=live_call_db["candidate"],
            admin_session_token=live_call_db["admin_token"],
            title="  Backend interview  ",
        )

        assert room.status == INVITED
        assert (room.title or "") == "Backend interview"
        assert token not in room.invite_token_hash
        assert len(room.invite_token_hash) == 64
        assert db.query(InterviewCallParticipant).count() == 2

        admin_access = require_call_access(
            db,
            room.room_id,
            live_call_db["admin"],
            live_call_db["admin_token"],
        )
        candidate_access = require_call_access(
            db,
            room.room_id,
            live_call_db["candidate"],
            live_call_db["candidate_token"],
        )
        assert admin_access.role == ROLE_ADMIN
        assert candidate_access.role == ROLE_CANDIDATE

        with pytest.raises(LiveCallPermissionDenied):
            require_call_access(
                db,
                room.room_id,
                live_call_db["other"],
                live_call_db["other_token"],
            )
        with pytest.raises(LiveCallPermissionDenied):
            require_call_access(
                db,
                room.room_id,
                live_call_db["candidate"],
                "wrong-token",
            )


def test_accept_invite_requires_candidate_and_valid_token(live_call_db) -> None:
    with get_db_context() as db:
        room, token = create_call_room(
            db,
            admin_user_id=live_call_db["admin"],
            candidate_user_id=live_call_db["candidate"],
            admin_session_token=live_call_db["admin_token"],
        )

        with pytest.raises(LiveCallPermissionDenied):
            accept_invite(
                db,
                room.room_id,
                token,
                user_id=live_call_db["admin"],
                session_token=live_call_db["admin_token"],
            )
        with pytest.raises(LiveCallPermissionDenied):
            accept_invite(
                db,
                room.room_id,
                "wrong-token",
                user_id=live_call_db["candidate"],
                session_token=live_call_db["candidate_token"],
            )

        accepted = accept_invite(
            db,
            room.room_id,
            token,
            user_id=live_call_db["candidate"],
            session_token=live_call_db["candidate_token"],
        )

        assert accepted.status == ACCEPTED
        access = require_call_access(
            db,
            room.room_id,
            live_call_db["candidate"],
            live_call_db["candidate_token"],
        )
        assert access.participant.status == "joined"
        assert access.participant.joined_at is not None

        with pytest.raises(LiveCallInvalidTransition):
            accept_invite(
                db,
                room.room_id,
                token,
                user_id=live_call_db["candidate"],
                session_token=live_call_db["candidate_token"],
            )


def test_start_and_complete_follow_room_transitions(live_call_db) -> None:
    with get_db_context() as db:
        room, token = create_call_room(
            db,
            admin_user_id=live_call_db["admin"],
            candidate_user_id=live_call_db["candidate"],
            admin_session_token=live_call_db["admin_token"],
        )
        accept_invite(
            db,
            room.room_id,
            token,
            user_id=live_call_db["candidate"],
            session_token=live_call_db["candidate_token"],
        )

        with pytest.raises(LiveCallPermissionDenied):
            start_call(
                db,
                room.room_id,
                user_id=live_call_db["candidate"],
                session_token=live_call_db["candidate_token"],
            )

        started = start_call(
            db,
            room.room_id,
            user_id=live_call_db["admin"],
            session_token=live_call_db["admin_token"],
        )
        assert started.status == IN_PROGRESS

        with pytest.raises(LiveCallPermissionDenied):
            complete_call(
                db,
                room.room_id,
                user_id=live_call_db["other"],
                session_token=live_call_db["other_token"],
            )

        completed = complete_call(
            db,
            room.room_id,
            user_id=live_call_db["candidate"],
            session_token=live_call_db["candidate_token"],
        )
        assert completed.status == COMPLETED
        assert completed.ended_at is not None

        with pytest.raises(LiveCallInvalidTransition):
            cancel_invite(
                db,
                room.room_id,
                user_id=live_call_db["admin"],
                session_token=live_call_db["admin_token"],
            )


def test_expired_invite_cannot_be_accepted(live_call_db) -> None:
    with get_db_context() as db:
        room, token = create_call_room(
            db,
            admin_user_id=live_call_db["admin"],
            candidate_user_id=live_call_db["candidate"],
            admin_session_token=live_call_db["admin_token"],
            invite_ttl_seconds=30,
        )
        room.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        db.flush()

        with pytest.raises(LiveCallExpired):
            accept_invite(
                db,
                room.room_id,
                token,
                user_id=live_call_db["candidate"],
                session_token=live_call_db["candidate_token"],
            )

        assert room.status == EXPIRED

        second_room, _token = create_call_room(
            db,
            admin_user_id=live_call_db["admin"],
            candidate_user_id=live_call_db["candidate"],
            admin_session_token=live_call_db["admin_token"],
            invite_ttl_seconds=30,
        )
        second_room.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        db.flush()

        assert expire_stale_invites(db) == 1
        assert second_room.status == EXPIRED


def test_signals_are_authenticated_and_drained_once(live_call_db) -> None:
    with get_db_context() as db:
        room, token = create_call_room(
            db,
            admin_user_id=live_call_db["admin"],
            candidate_user_id=live_call_db["candidate"],
            admin_session_token=live_call_db["admin_token"],
        )
        accept_invite(
            db,
            room.room_id,
            token,
            user_id=live_call_db["candidate"],
            session_token=live_call_db["candidate_token"],
        )
        start_call(
            db,
            room.room_id,
            user_id=live_call_db["admin"],
            session_token=live_call_db["admin_token"],
        )

        signal = enqueue_signal(
            db,
            room.room_id,
            user_id=live_call_db["admin"],
            session_token=live_call_db["admin_token"],
            signal_type=SIGNAL_OFFER,
            recipient_role=ROLE_CANDIDATE,
            payload={"sdp": "fake-offer"},
        )
        assert signal.recipient_user_id == live_call_db["candidate"]
        assert db.query(InterviewCallSignal).count() == 1

        drained = drain_signals(
            db,
            room.room_id,
            user_id=live_call_db["candidate"],
            session_token=live_call_db["candidate_token"],
        )
        assert len(drained) == 1
        assert drained[0]["signal_type"] == SIGNAL_OFFER
        assert drained[0]["sender_role"] == ROLE_ADMIN
        assert drained[0]["payload"] == {"sdp": "fake-offer"}
        assert drain_signals(
            db,
            room.room_id,
            user_id=live_call_db["candidate"],
            session_token=live_call_db["candidate_token"],
        ) == []

        with pytest.raises(LiveCallPermissionDenied):
            enqueue_signal(
                db,
                room.room_id,
                user_id=live_call_db["admin"],
                session_token=live_call_db["admin_token"],
                signal_type=SIGNAL_OFFER,
                recipient_role=ROLE_ADMIN,
                payload={},
            )
        with pytest.raises(LiveCallPermissionDenied):
            drain_signals(
                db,
                room.room_id,
                user_id=live_call_db["other"],
                session_token=live_call_db["other_token"],
            )


def test_delete_user_cleans_live_call_rows(live_call_db) -> None:
    from services import auth_service

    with get_db_context() as db:
        room, _token = create_call_room(
            db,
            admin_user_id=live_call_db["admin"],
            candidate_user_id=live_call_db["candidate"],
            admin_session_token=live_call_db["admin_token"],
        )
        room_id = room.room_id

    auth_service.delete_user(
        live_call_db["candidate"],
        actor_admin_id=live_call_db["admin"],
    )

    with get_db_context() as db:
        assert db.query(InterviewCallRoom).filter(InterviewCallRoom.room_id == room_id).count() == 0
        assert db.query(InterviewCallParticipant).count() == 0
        assert db.query(InterviewCallSignal).count() == 0
