from __future__ import annotations

import hashlib
from typing import Any

from config.settings import settings
from database import User, get_db_context
from services import live_call_media_service as media
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


def _participant_key(room_id: str, user_id: Any, role: str) -> str:
    digest = hashlib.sha256(f"{room_id}:{user_id}:{role}".encode()).hexdigest()
    return f"{str(role)[:1]}:{digest[:10]}"


def _participant_status(db, room, user_id: int) -> str | None:
    row = (
        db.query(calls.InterviewCallParticipant)
        .filter(
            calls.InterviewCallParticipant.room_id == room.id,
            calls.InterviewCallParticipant.user_id == int(user_id),
        )
        .first()
    )
    return str(row.status) if row is not None else None


def _reconcile_media_if_terminal(summary: dict[str, Any]) -> None:
    if str(summary.get("status") or "") in calls.TERMINAL_CALL_STATES:
        media.reconcile_room_media(room_id=str(summary["room_id"]), terminal=True)


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
        summaries: list[dict[str, Any]] = []
        for room in rooms:
            summary = _room_dict(db, room, role=_role_for(room, parsed_user_id))
            summary["participant_status"] = _participant_status(db, room, parsed_user_id)
            summary.update(
                calls.get_call_signal_summary(
                    db,
                    str(room.room_id),
                    user_id=parsed_user_id,
                    session_token=session_token,
                )
            )
            _reconcile_media_if_terminal(summary)
            summaries.append(summary)
        return summaries


def list_invitation_statuses(
    *,
    user_id: Any,
    session_token: Any,
    limit: int = 50,
) -> list[dict[str, Any]]:
    with get_db_context() as db:
        calls.expire_stale_invites(db, limit=100)
        return calls.list_user_call_statuses(
            db,
            user_id=user_id,
            session_token=session_token,
            limit=limit,
        )


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
        _reconcile_media_if_terminal(summary)
        return summary


def accept_invitation(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
) -> dict[str, Any]:
    with get_db_context() as db:
        try:
            room = calls.accept_invite_authenticated(
                db,
                room_id,
                user_id=user_id,
                session_token=session_token,
            )
        except calls.LiveCallExpired:
            calls.expire_stale_invites(db, room_id=room_id, limit=10)
            db.commit()
            raise
        except calls.LiveCallInvalidTransition:
            db.rollback()
            current = get_invitation(
                room_id=room_id,
                user_id=user_id,
                session_token=session_token,
            )
            already_joined = (
                str(current.get("status")) in {calls.ACCEPTED, calls.IN_PROGRESS}
                and str(current.get("participant_status")) == calls.PARTICIPANT_JOINED
            )
            if not already_joined:
                raise
            return current
        summary = _room_dict(db, room, role=calls.ROLE_CANDIDATE)
        summary["participant_status"] = calls.PARTICIPANT_JOINED
        _reconcile_media_if_terminal(summary)
        return summary


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
        summary = _room_dict(db, room, role=calls.ROLE_CANDIDATE)
        _reconcile_media_if_terminal(summary)
        return summary


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
        summary = _room_dict(db, room, role=calls.ROLE_ADMIN)
        _reconcile_media_if_terminal(summary)
        return summary


def delete_invitation(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
    confirmation: bool,
) -> dict[str, Any]:
    with get_db_context() as db:
        summary = calls.delete_invitation(
            db,
            room_id,
            user_id=user_id,
            session_token=session_token,
            confirmation=confirmation,
        )
        media.reconcile_room_media(room_id=str(summary["room_id"]), terminal=True)
        return summary


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
        summary = _room_dict(db, room, role=calls.ROLE_ADMIN)
        _reconcile_media_if_terminal(summary)
        return summary


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
        summary = _room_dict(db, room)
        _reconcile_media_if_terminal(summary)
        return summary


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
        summary = _room_dict(db, room)
        _reconcile_media_if_terminal(summary)
        return summary


def leave_call(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
) -> dict[str, Any]:
    role: str | None = None
    with get_db_context() as db:
        room = calls.leave_call_with_hangup(
            db,
            room_id,
            user_id=user_id,
            session_token=session_token,
        )
        role = _role_for(room, int(user_id))
        summary = _room_dict(db, room, role=role)
        _reconcile_media_if_terminal(summary)
    if role is not None:
        media.release_media_session(room_id=room_id, role=role)
    return summary


def leave_invitation(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
) -> dict[str, Any]:
    return leave_call(
        room_id=room_id,
        user_id=user_id,
        session_token=session_token,
    )


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


def media_heartbeat_poll_seconds() -> int:
    try:
        value = int(settings.live_media_heartbeat_seconds)
    except (TypeError, ValueError):
        return 15
    return max(5, min(value, 60))


def get_media_config(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
) -> dict[str, Any]:
    room = get_invitation(
        room_id=room_id,
        user_id=user_id,
        session_token=session_token,
    )
    role = str(room["role"])
    return media.media_config(
        room_id=room_id,
        role=role,
        participant_key=_participant_key(room_id, user_id, role),
    )


def attach_media_session(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
) -> dict[str, Any]:
    room = get_invitation(
        room_id=room_id,
        user_id=user_id,
        session_token=session_token,
    )
    return media.attach_media_session(
        room_id=room_id,
        user_id=user_id,
        session_token=session_token,
        role=str(room["role"]),
    )


def release_media_session(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
) -> dict[str, Any]:
    room = get_invitation(
        room_id=room_id,
        user_id=user_id,
        session_token=session_token,
    )
    return media.release_media_session(room_id=room_id, role=str(room["role"]))


def reconcile_room_media(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
) -> dict[str, Any]:
    room = get_invitation(
        room_id=room_id,
        user_id=user_id,
        session_token=session_token,
    )
    return media.reconcile_room_media(
        room_id=room_id,
        terminal=str(room["status"]) in calls.TERMINAL_CALL_STATES,
    )


def reconcile_stale_media_sessions() -> dict[str, Any]:
    return media.reconcile_stale_media_sessions()


def heartbeat_media_session(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
) -> bool:
    room = get_invitation(
        room_id=room_id,
        user_id=user_id,
        session_token=session_token,
    )
    role = str(room["role"])
    if str(room["status"]) in calls.TERMINAL_CALL_STATES:
        media.reconcile_room_media(room_id=room_id, terminal=True)
        return False
    if str(room.get("participant_status") or "") == calls.PARTICIPANT_LEFT:
        media.release_media_session(room_id=room_id, role=role)
        return False
    return media.heartbeat_media_session(room_id=room_id, role=role)
