from __future__ import annotations

from typing import Any

from config.settings import settings
from database import User, get_db_context
from services import live_call_service as calls


def _user_label(db, user_id: int) -> str:
    user = db.query(User).filter(User.id == int(user_id)).first()
    if user is None:
        return "Unknown"
    return str(user.full_name or user.email or f"User {user_id}")


def _room_dict(db, room, *, role: str | None = None) -> dict[str, Any]:
    summary = {
        "room_id": str(room.room_id),
        "title": str(room.title or "Live interview"),
        "status": str(room.status),
        "role": role,
        "admin_user_id": int(room.admin_user_id),
        "candidate_user_id": int(room.candidate_user_id),
        "admin_name": _user_label(db, room.admin_user_id),
        "candidate_name": _user_label(db, room.candidate_user_id),
        "created_at": room.created_at,
        "accepted_at": room.accepted_at,
        "expires_at": room.expires_at,
        "started_at": room.started_at,
        "ended_at": room.ended_at,
    }
    return summary


def _role_for(room, user_id: int) -> str:
    if int(room.admin_user_id) == int(user_id):
        return calls.ROLE_ADMIN
    if int(room.candidate_user_id) == int(user_id):
        return calls.ROLE_CANDIDATE
    return "unknown"


def create_invitation(
    *,
    admin_user_id: Any,
    session_token: Any,
    candidate_user_id: Any,
    title: str = "",
    invite_ttl_seconds: int | None = None,
) -> dict[str, Any]:
    with get_db_context() as db:
        kwargs: dict[str, Any] = {}
        if invite_ttl_seconds is not None:
            kwargs["invite_ttl_seconds"] = int(invite_ttl_seconds)
        room, _token = calls.create_call_room(
            db,
            admin_user_id=admin_user_id,
            candidate_user_id=candidate_user_id,
            admin_session_token=session_token,
            title=title,
            **kwargs,
        )
        return _room_dict(db, room, role=calls.ROLE_ADMIN)


def list_invitations(
    *,
    user_id: Any,
    session_token: Any,
    limit: int = 50,
) -> list[dict[str, Any]]:
    with get_db_context() as db:
        calls.expire_stale_invites(db, limit=100)
        rooms = calls.list_user_calls(
            db,
            user_id=user_id,
            session_token=session_token,
            limit=limit,
        )
        parsed_user_id = int(user_id)
        return [_room_dict(db, room, role=_role_for(room, parsed_user_id)) for room in rooms]


def get_invitation(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
) -> dict[str, Any]:
    with get_db_context() as db:
        calls.expire_stale_invites(db, room_id=room_id, limit=10)
        access = calls.require_call_access(db, room_id, user_id, session_token)
        summary = _room_dict(db, access.room, role=access.role)
        summary.update(
            calls.get_call_signal_summary(
                db,
                room_id,
                user_id=user_id,
                session_token=session_token,
            )
        )
        summary["participant_status"] = str(access.participant.status)
        return summary


def accept_invitation(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
) -> dict[str, Any]:
    with get_db_context() as db:
        room = calls.accept_invite_authenticated(
            db,
            room_id,
            user_id=user_id,
            session_token=session_token,
        )
        return _room_dict(db, room, role=calls.ROLE_CANDIDATE)


def decline_invitation(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
) -> dict[str, Any]:
    with get_db_context() as db:
        room = calls.decline_invite(
            db,
            room_id,
            user_id=user_id,
            session_token=session_token,
        )
        return _room_dict(db, room, role=calls.ROLE_CANDIDATE)


def cancel_invitation(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
) -> dict[str, Any]:
    with get_db_context() as db:
        room = calls.cancel_invite(
            db,
            room_id,
            user_id=user_id,
            session_token=session_token,
        )
        return _room_dict(db, room, role=calls.ROLE_ADMIN)


def start_invitation(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
) -> dict[str, Any]:
    with get_db_context() as db:
        room = calls.start_call(
            db,
            room_id,
            user_id=user_id,
            session_token=session_token,
        )
        return _room_dict(db, room, role=calls.ROLE_ADMIN)


def complete_invitation(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
) -> dict[str, Any]:
    with get_db_context() as db:
        room = calls.complete_call(
            db,
            room_id,
            user_id=user_id,
            session_token=session_token,
        )
        return _room_dict(db, room)


def interrupt_invitation(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
) -> dict[str, Any]:
    with get_db_context() as db:
        room = calls.interrupt_call(
            db,
            room_id,
            user_id=user_id,
            session_token=session_token,
        )
        return _room_dict(db, room)


def leave_invitation(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
) -> dict[str, Any]:
    with get_db_context() as db:
        room = calls.mark_participant_left(
            db,
            room_id,
            user_id=user_id,
            session_token=session_token,
        )
        return _room_dict(db, room)


def send_signal(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
    signal_type: str,
    recipient_role: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    with get_db_context() as db:
        signal = calls.enqueue_signal(
            db,
            room_id,
            user_id=user_id,
            session_token=session_token,
            signal_type=signal_type,
            recipient_role=recipient_role,
            payload=payload,
        )
        return {
            "id": int(signal.id),
            "signal_type": str(signal.signal_type),
            "recipient_role": str(recipient_role),
            "expires_at": signal.expires_at,
        }


def receive_signals(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
    limit: int = 50,
) -> list[dict[str, Any]]:
    with get_db_context() as db:
        return calls.drain_signals(
            db,
            room_id,
            user_id=user_id,
            session_token=session_token,
            limit=limit,
        )


def signal_poll_seconds() -> int:
    try:
        value = int(settings.live_signal_poll_seconds)
    except (TypeError, ValueError):
        return 3
    return max(1, min(value, 30))
