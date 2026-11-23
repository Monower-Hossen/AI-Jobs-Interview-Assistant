from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from config.settings import settings
from database import (
    EventLog,
    InterviewCallParticipant,
    InterviewCallRoom,
    InterviewCallSignal,
    User,
    UserSession,
)
from sqlalchemy import func, update
from sqlalchemy.orm import Session

INVITED = "invited"
ACCEPTED = "accepted"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
CANCELLED = "cancelled"
DECLINED = "declined"
EXPIRED = "expired"
INTERRUPTED = "interrupted"

CALL_STATES = frozenset(
    {INVITED, ACCEPTED, IN_PROGRESS, COMPLETED, CANCELLED, DECLINED, EXPIRED, INTERRUPTED}
)
ACTIVE_CALL_STATES = frozenset({INVITED, ACCEPTED, IN_PROGRESS})
TERMINAL_CALL_STATES = frozenset({COMPLETED, CANCELLED, DECLINED, EXPIRED, INTERRUPTED})

_ALLOWED_TRANSITIONS = {
    INVITED: frozenset({ACCEPTED, CANCELLED, DECLINED, EXPIRED, INTERRUPTED}),
    ACCEPTED: frozenset({IN_PROGRESS, EXPIRED, INTERRUPTED}),
    IN_PROGRESS: frozenset({COMPLETED, INTERRUPTED}),
    COMPLETED: frozenset(),
    CANCELLED: frozenset(),
    DECLINED: frozenset(),
    EXPIRED: frozenset(),
    INTERRUPTED: frozenset(),
}

PARTICIPANT_INVITED = "invited"
PARTICIPANT_JOINED = "joined"
PARTICIPANT_LEFT = "left"
PARTICIPANT_STATUSES = frozenset(
    {PARTICIPANT_INVITED, PARTICIPANT_JOINED, PARTICIPANT_LEFT}
)

ROLE_ADMIN = "admin"
ROLE_CANDIDATE = "candidate"
CALL_ROLES = frozenset({ROLE_ADMIN, ROLE_CANDIDATE})

SIGNAL_OFFER = "offer"
SIGNAL_ANSWER = "answer"
SIGNAL_ICE_CANDIDATE = "ice_candidate"
SIGNAL_ICE_GATHERING_COMPLETE = "ice_gathering_complete"
SIGNAL_HANGUP = "hangup"
SIGNAL_PING = "ping"
SIGNAL_TYPES = frozenset(
    {
        SIGNAL_OFFER,
        SIGNAL_ANSWER,
        SIGNAL_ICE_CANDIDATE,
        SIGNAL_ICE_GATHERING_COMPLETE,
        SIGNAL_HANGUP,
        SIGNAL_PING,
    }
)

DEFAULT_INVITE_TTL_SECONDS = 120
DEFAULT_SIGNAL_TTL_SECONDS = 120
MAX_SIGNAL_PAYLOAD_BYTES = 16_384
MAX_PENDING_SIGNALS_PER_ROOM = 100
MAX_SIGNALS_PER_PARTICIPANT_PER_MINUTE = 30
SIGNAL_RATE_WINDOW_SECONDS = 60
MAX_SDP_CHARS = 12_000
FORBIDDEN_SIGNAL_KEYS = frozenset(
    {"token", "session", "session_token", "password", "secret", "authorization", "credential"}
)


class LiveCallError(Exception):
    """Base error for live-call foundation operations."""


class LiveCallNotFound(LiveCallError):
    """The room does not exist."""


class LiveCallPermissionDenied(LiveCallError):
    """The account or session cannot access the requested room."""


class LiveCallInvalidTransition(LiveCallError):
    """The requested room transition is not allowed."""


class LiveCallExpired(LiveCallError):
    """The invitation is no longer valid."""


class LiveCallInvalidSignal(LiveCallError):
    """The signaling message is malformed or disallowed."""


class LiveCallRateLimited(LiveCallError):
    """Too many signaling messages were submitted."""


@dataclass(frozen=True)
class CallAccess:
    room: InterviewCallRoom
    participant: InterviewCallParticipant
    role: str


def _coerce_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC).replace(tzinfo=None)
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _affected_rows(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def _hash_invite_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _max_pending_signals() -> int:
    return _bounded_int(
        settings.live_signal_max_pending_per_room,
        MAX_PENDING_SIGNALS_PER_ROOM,
        10,
        1000,
    )


def _max_participant_signals() -> int:
    return _bounded_int(
        settings.live_signal_max_per_participant_per_minute,
        MAX_SIGNALS_PER_PARTICIPANT_PER_MINUTE,
        1,
        300,
    )


def _load_active_user(db: Session, user_id: Any) -> User:
    try:
        parsed_id = int(user_id)
    except (TypeError, ValueError) as exc:
        raise LiveCallPermissionDenied("Account is not available for live calls.") from exc

    user = (
        db.query(User)
        .filter(
            User.id == parsed_id,
            User.is_active.is_(True),
            User.account_status == "active",
        )
        .first()
    )
    if user is None:
        raise LiveCallPermissionDenied("Account is not available for live calls.")
    return user


def _require_session(db: Session, user_id: int, session_token: Any) -> str:
    token = str(session_token or "").strip()
    if not token:
        raise LiveCallPermissionDenied("A valid session is required for live calls.")
    row = (
        db.query(UserSession)
        .filter(
            UserSession.session_id == token,
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise LiveCallPermissionDenied("Session is not valid for this account.")
    return token


def _require_admin(db: Session, admin_user_id: Any, session_token: Any) -> User:
    user = _load_active_user(db, admin_user_id)
    if str(user.role) != "admin":
        raise LiveCallPermissionDenied("Only an administrator can create a live call room.")
    _require_session(db, int(user.id), session_token)
    return user


def _require_candidate(db: Session, candidate_user_id: Any) -> User:
    user = _load_active_user(db, candidate_user_id)
    if str(user.role) != "user":
        raise LiveCallPermissionDenied("The invited account must be a regular user.")
    return user


def _get_room(db: Session, room_id: Any) -> InterviewCallRoom:
    room = db.query(InterviewCallRoom).filter(InterviewCallRoom.room_id == str(room_id)).first()
    if room is None:
        raise LiveCallNotFound("Live call not found.")
    return room


def _get_participant(
    db: Session, room: InterviewCallRoom, user_id: int
) -> InterviewCallParticipant:
    participant = (
        db.query(InterviewCallParticipant)
        .filter(
            InterviewCallParticipant.room_id == room.id,
            InterviewCallParticipant.user_id == user_id,
        )
        .first()
    )
    if participant is None:
        raise LiveCallPermissionDenied("You are not a participant in this live call.")
    return participant


def _get_participant_by_role(
    db: Session, room: InterviewCallRoom, role: str
) -> InterviewCallParticipant:
    participant = (
        db.query(InterviewCallParticipant)
        .filter(
            InterviewCallParticipant.room_id == room.id,
            InterviewCallParticipant.role == role,
        )
        .first()
    )
    if participant is None:
        raise LiveCallNotFound("Live call participant not found.")
    return participant


def _mark_all_left(db: Session, room: InterviewCallRoom, now: datetime) -> None:
    participants = (
        db.query(InterviewCallParticipant)
        .filter(InterviewCallParticipant.room_id == room.id)
        .all()
    )
    for participant in participants:
        if participant.status != PARTICIPANT_LEFT:
            participant.status = PARTICIPANT_LEFT
            participant.left_at = now
            participant.updated_at = now


def _predecessor_states(target: str) -> frozenset[str]:
    return frozenset(
        source for source, targets in _ALLOWED_TRANSITIONS.items() if target in targets
    )


def _transition(
    db: Session,
    room: InterviewCallRoom,
    target: str,
    *,
    actor_user_id: int,
    now: datetime,
    expected: set[str] | frozenset[str] | None = None,
) -> InterviewCallRoom:
    sources = frozenset(expected) if expected is not None else _predecessor_states(target)
    values: dict[str, Any] = {"status": target, "updated_at": now}
    if target == ACCEPTED:
        values["accepted_at"] = now
    elif target == IN_PROGRESS:
        values["started_at"] = now
    elif target in TERMINAL_CALL_STATES:
        values["ended_at"] = now
    result = db.execute(
        update(InterviewCallRoom)
        .where(
            InterviewCallRoom.id == int(room.id),
            InterviewCallRoom.status.in_(sources),
        )
        .values(**values)
    )
    if _affected_rows(result) != 1:
        raise LiveCallInvalidTransition("The live call state changed. Retry the operation.")
    db.expire(room)
    db.refresh(room)
    db.add(
        EventLog(
            user_id=actor_user_id,
            event_type=f"live_call.{target}",
            message=f"Live call {room.room_id} changed to {target}.",
        )
    )
    return room


def _fail_if_expired(db: Session, room: InterviewCallRoom, now: datetime) -> None:
    if str(room.status) not in {INVITED, ACCEPTED} or room.expires_at is None:
        return
    if now <= room.expires_at:
        return
    _transition(
        db,
        room,
        EXPIRED,
        actor_user_id=int(room.admin_user_id),
        now=now,
        expected={str(room.status)},
    )
    _mark_all_left(db, room, now)
    raise LiveCallExpired("The live call invitation has expired.")


def _validate_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise LiveCallInvalidSignal("Signal payload must be a dictionary.")
    if any(str(key).lower() in FORBIDDEN_SIGNAL_KEYS for key in payload):
        raise LiveCallInvalidSignal("Signal payload contains a forbidden field.")
    try:
        encoded = json.dumps(payload, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise LiveCallInvalidSignal("Signal payload must be JSON serializable.") from exc
    if len(encoded.encode("utf-8")) > MAX_SIGNAL_PAYLOAD_BYTES:
        raise LiveCallInvalidSignal("Signal payload is too large.")
    return dict(payload)


def _validate_signal_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    clean = _validate_payload(payload)
    allowed: set[str]
    required: set[str]
    if kind in {SIGNAL_OFFER, SIGNAL_ANSWER}:
        allowed = {"sdp", "type"}
        required = {"sdp"}
        sdp = clean.get("sdp")
        if not isinstance(sdp, str) or not sdp or len(sdp) > MAX_SDP_CHARS:
            raise LiveCallInvalidSignal("Signal sdp is invalid.")
        expected_type = SIGNAL_OFFER if kind == SIGNAL_OFFER else SIGNAL_ANSWER
        if "type" in clean and clean["type"] != expected_type:
            raise LiveCallInvalidSignal("Signal description type does not match.")
    elif kind == SIGNAL_ICE_CANDIDATE:
        allowed = {"candidate", "sdp_mid", "sdp_mline_index"}
        required = {"candidate"}
        candidate = clean.get("candidate")
        if not isinstance(candidate, str) or not candidate or len(candidate) > 4000:
            raise LiveCallInvalidSignal("ICE candidate is invalid.")
        if "sdp_mid" in clean and not isinstance(clean["sdp_mid"], str):
            raise LiveCallInvalidSignal("ICE sdp_mid is invalid.")
        if "sdp_mline_index" in clean and (
            isinstance(clean["sdp_mline_index"], bool)
            or not isinstance(clean["sdp_mline_index"], int)
        ):
            raise LiveCallInvalidSignal("ICE sdp_mline_index is invalid.")
    elif kind == SIGNAL_ICE_GATHERING_COMPLETE:
        allowed = {"sdp_mid"}
        required = set()
        if "sdp_mid" in clean and not isinstance(clean["sdp_mid"], str):
            raise LiveCallInvalidSignal("ICE sdp_mid is invalid.")
    elif kind == SIGNAL_HANGUP:
        allowed = {"reason"}
        required = set()
        if "reason" in clean and (
            not isinstance(clean["reason"], str) or len(clean["reason"]) > 200
        ):
            raise LiveCallInvalidSignal("Hangup reason is invalid.")
    elif kind == SIGNAL_PING:
        allowed = {"nonce"}
        required = set()
        if "nonce" in clean and (
            not isinstance(clean["nonce"], str) or len(clean["nonce"]) > 64
        ):
            raise LiveCallInvalidSignal("Ping nonce is invalid.")
    else:
        raise LiveCallInvalidSignal("Unsupported live call signal type.")
    unexpected = set(clean) - allowed
    if unexpected:
        raise LiveCallInvalidSignal("Signal payload contains unexpected fields.")
    missing = required - set(clean)
    if missing:
        raise LiveCallInvalidSignal("Signal payload is missing required fields.")
    return clean


def require_call_access(
    db: Session, room_id: str, user_id: Any, session_token: Any
) -> CallAccess:
    user = _load_active_user(db, user_id)
    _require_session(db, int(user.id), session_token)
    room = _get_room(db, room_id)
    participant = _get_participant(db, room, int(user.id))
    role = str(participant.role)
    if role == ROLE_ADMIN and int(room.admin_user_id) != int(user.id):
        raise LiveCallPermissionDenied("You are not a participant in this live call.")
    if role == ROLE_CANDIDATE and int(room.candidate_user_id) != int(user.id):
        raise LiveCallPermissionDenied("You are not a participant in this live call.")
    if role not in CALL_ROLES:
        raise LiveCallPermissionDenied("Live call role is not valid.")
    return CallAccess(room=room, participant=participant, role=role)


def create_call_room(
    db: Session,
    *,
    admin_user_id: Any,
    candidate_user_id: Any,
    admin_session_token: Any,
    title: str | None = None,
    invite_ttl_seconds: int = DEFAULT_INVITE_TTL_SECONDS,
    now: datetime | None = None,
) -> tuple[InterviewCallRoom, str]:
    current = _coerce_now(now)
    admin = _require_admin(db, admin_user_id, admin_session_token)
    candidate = _require_candidate(db, candidate_user_id)
    if int(admin.id) == int(candidate.id):
        raise LiveCallPermissionDenied("An administrator cannot invite themselves.")

    ttl = _bounded_int(
        invite_ttl_seconds,
        DEFAULT_INVITE_TTL_SECONDS,
        30,
        3600,
    )
    invite_token = secrets.token_urlsafe(32)
    room = InterviewCallRoom(
        room_id=f"lc-{secrets.token_urlsafe(12)}",
        admin_user_id=int(admin.id),
        candidate_user_id=int(candidate.id),
        title=str(title).strip()[:200] if title else None,
        status=INVITED,
        invite_token_hash=_hash_invite_token(invite_token),
        expires_at=current + timedelta(seconds=ttl),
        created_at=current,
        updated_at=current,
    )
    db.add(room)
    db.flush()

    db.add_all(
        [
            InterviewCallParticipant(
                room_id=room.id,
                user_id=int(admin.id),
                role=ROLE_ADMIN,
                status=PARTICIPANT_INVITED,
                created_at=current,
                updated_at=current,
            ),
            InterviewCallParticipant(
                room_id=room.id,
                user_id=int(candidate.id),
                role=ROLE_CANDIDATE,
                status=PARTICIPANT_INVITED,
                created_at=current,
                updated_at=current,
            ),
        ]
    )
    db.add(
        EventLog(
            user_id=int(admin.id),
            event_type="live_call.invited",
            message=f"Live call {room.room_id} was invited.",
        )
    )
    db.flush()
    return room, invite_token


def list_user_calls(
    db: Session,
    *,
    user_id: Any,
    session_token: Any,
    limit: int = 50,
) -> list[InterviewCallRoom]:
    user = _load_active_user(db, user_id)
    _require_session(db, int(user.id), session_token)
    capped = _bounded_int(limit, 50, 1, 100)
    return (
        db.query(InterviewCallRoom)
        .filter(
            (InterviewCallRoom.admin_user_id == int(user.id))
            | (InterviewCallRoom.candidate_user_id == int(user.id))
        )
        .order_by(InterviewCallRoom.created_at.desc(), InterviewCallRoom.id.desc())
        .limit(capped)
        .all()
    )


def _accept_room(access: CallAccess, current: datetime, db: Session) -> InterviewCallRoom:
    participant_result = db.execute(
        update(InterviewCallParticipant)
        .where(
            InterviewCallParticipant.id == int(access.participant.id),
            InterviewCallParticipant.status == PARTICIPANT_INVITED,
        )
        .values(status=PARTICIPANT_JOINED, joined_at=current, updated_at=current)
    )
    if _affected_rows(participant_result) != 1:
        raise LiveCallInvalidTransition("This invitation cannot be accepted again.")
    db.expire(access.participant)
    db.refresh(access.participant)
    return _transition(
        db,
        access.room,
        ACCEPTED,
        actor_user_id=int(access.participant.user_id),
        now=current,
        expected={INVITED},
    )


def accept_invite(
    db: Session,
    room_id: str,
    invite_token: Any,
    *,
    user_id: Any,
    session_token: Any,
    now: datetime | None = None,
) -> InterviewCallRoom:
    current = _coerce_now(now)
    access = require_call_access(db, room_id, user_id, session_token)
    if access.role != ROLE_CANDIDATE:
        raise LiveCallPermissionDenied("Only the invited user can accept this call.")

    _fail_if_expired(db, access.room, current)
    if str(access.room.status) != INVITED:
        raise LiveCallInvalidTransition("This invitation is no longer pending.")

    supplied = str(invite_token or "").strip()
    if not supplied or not hmac.compare_digest(
        str(access.room.invite_token_hash), _hash_invite_token(supplied)
    ):
        raise LiveCallPermissionDenied("The live call invitation is invalid.")
    return _accept_room(access, current, db)


def accept_invite_authenticated(
    db: Session,
    room_id: str,
    *,
    user_id: Any,
    session_token: Any,
    now: datetime | None = None,
) -> InterviewCallRoom:
    current = _coerce_now(now)
    access = require_call_access(db, room_id, user_id, session_token)
    if access.role != ROLE_CANDIDATE:
        raise LiveCallPermissionDenied("Only the invited user can accept this call.")
    _fail_if_expired(db, access.room, current)
    if str(access.room.status) != INVITED:
        raise LiveCallInvalidTransition("This invitation is no longer pending.")
    return _accept_room(access, current, db)


def decline_invite(
    db: Session,
    room_id: str,
    *,
    user_id: Any,
    session_token: Any,
    now: datetime | None = None,
) -> InterviewCallRoom:
    current = _coerce_now(now)
    access = require_call_access(db, room_id, user_id, session_token)
    if access.role != ROLE_CANDIDATE:
        raise LiveCallPermissionDenied("Only the invited user can decline this call.")
    _fail_if_expired(db, access.room, current)
    if str(access.room.status) != INVITED:
        raise LiveCallInvalidTransition("This invitation is no longer pending.")

    _mark_all_left(db, access.room, current)
    return _transition(
        db,
        access.room,
        DECLINED,
        actor_user_id=int(access.participant.user_id),
        now=current,
        expected={INVITED},
    )


def cancel_invite(
    db: Session,
    room_id: str,
    *,
    user_id: Any,
    session_token: Any,
    now: datetime | None = None,
) -> InterviewCallRoom:
    current = _coerce_now(now)
    access = require_call_access(db, room_id, user_id, session_token)
    if access.role != ROLE_ADMIN:
        raise LiveCallPermissionDenied("Only the administrator can cancel this call.")
    _fail_if_expired(db, access.room, current)
    if str(access.room.status) != INVITED:
        raise LiveCallInvalidTransition("Only a pending invitation can be cancelled.")

    _mark_all_left(db, access.room, current)
    return _transition(
        db,
        access.room,
        CANCELLED,
        actor_user_id=int(access.participant.user_id),
        now=current,
        expected={INVITED},
    )


def start_call(
    db: Session,
    room_id: str,
    *,
    user_id: Any,
    session_token: Any,
    now: datetime | None = None,
) -> InterviewCallRoom:
    current = _coerce_now(now)
    access = require_call_access(db, room_id, user_id, session_token)
    if access.role != ROLE_ADMIN:
        raise LiveCallPermissionDenied("Only the administrator can start this call.")

    _fail_if_expired(db, access.room, current)
    if str(access.room.status) != ACCEPTED:
        raise LiveCallInvalidTransition("The invitation has not been accepted.")

    candidate = _get_participant_by_role(db, access.room, ROLE_CANDIDATE)
    if str(candidate.status) != PARTICIPANT_JOINED:
        raise LiveCallInvalidTransition("The invited user has not joined the call.")

    admin_result = db.execute(
        update(InterviewCallParticipant)
        .where(
            InterviewCallParticipant.id == int(access.participant.id),
            InterviewCallParticipant.status != PARTICIPANT_LEFT,
        )
        .values(status=PARTICIPANT_JOINED, joined_at=current, updated_at=current)
    )
    if _affected_rows(admin_result) != 1:
        raise LiveCallInvalidTransition("The administrator has left this call.")
    db.expire(access.participant)
    db.refresh(access.participant)
    return _transition(
        db,
        access.room,
        IN_PROGRESS,
        actor_user_id=int(access.participant.user_id),
        now=current,
        expected={ACCEPTED},
    )


def complete_call(
    db: Session,
    room_id: str,
    *,
    user_id: Any,
    session_token: Any,
    now: datetime | None = None,
) -> InterviewCallRoom:
    current = _coerce_now(now)
    access = require_call_access(db, room_id, user_id, session_token)
    if str(access.room.status) != IN_PROGRESS:
        raise LiveCallInvalidTransition("Only an active live call can be completed.")

    _mark_all_left(db, access.room, current)
    return _transition(
        db,
        access.room,
        COMPLETED,
        actor_user_id=int(access.participant.user_id),
        now=current,
        expected={IN_PROGRESS},
    )


def interrupt_call(
    db: Session,
    room_id: str,
    *,
    user_id: Any,
    session_token: Any,
    now: datetime | None = None,
) -> InterviewCallRoom:
    current = _coerce_now(now)
    access = require_call_access(db, room_id, user_id, session_token)
    _fail_if_expired(db, access.room, current)
    source = str(access.room.status)
    if source not in {INVITED, ACCEPTED, IN_PROGRESS}:
        raise LiveCallInvalidTransition("This live call is already finished.")

    _mark_all_left(db, access.room, current)
    return _transition(
        db,
        access.room,
        INTERRUPTED,
        actor_user_id=int(access.participant.user_id),
        now=current,
        expected={source},
    )


def mark_participant_left(
    db: Session,
    room_id: str,
    *,
    user_id: Any,
    session_token: Any,
    now: datetime | None = None,
) -> InterviewCallRoom:
    current = _coerce_now(now)
    access = require_call_access(db, room_id, user_id, session_token)
    if access.participant.status != PARTICIPANT_LEFT:
        access.participant.status = PARTICIPANT_LEFT
        access.participant.left_at = current
        access.participant.updated_at = current

    active = (
        db.query(InterviewCallParticipant)
        .filter(
            InterviewCallParticipant.room_id == access.room.id,
            InterviewCallParticipant.status != PARTICIPANT_LEFT,
        )
        .count()
    )
    source = str(access.room.status)
    if active == 0 and source in {ACCEPTED, IN_PROGRESS}:
        _transition(
            db,
            access.room,
            INTERRUPTED,
            actor_user_id=int(access.participant.user_id),
            now=current,
            expected={source},
        )
    return access.room


def _validate_signaling_room(db: Session, access: CallAccess, current: datetime) -> None:
    if str(access.participant.status) == PARTICIPANT_LEFT:
        raise LiveCallInvalidTransition("You have left this live call.")
    source = str(access.room.status)
    if source not in {ACCEPTED, IN_PROGRESS}:
        raise LiveCallInvalidTransition("Signals require an accepted live call.")
    if access.room.expires_at is not None and current > access.room.expires_at:
        raise LiveCallExpired("The live call invitation has expired.")


def enqueue_signal(
    db: Session,
    room_id: str,
    *,
    user_id: Any,
    session_token: Any,
    signal_type: str,
    recipient_role: str,
    payload: dict[str, Any],
    signal_ttl_seconds: int = DEFAULT_SIGNAL_TTL_SECONDS,
    now: datetime | None = None,
) -> InterviewCallSignal:
    current = _coerce_now(now)
    access = require_call_access(db, room_id, user_id, session_token)
    _validate_signaling_room(db, access, current)

    kind = str(signal_type)
    if kind not in SIGNAL_TYPES:
        raise LiveCallInvalidSignal("Unsupported live call signal type.")

    role = str(recipient_role)
    if role not in CALL_ROLES:
        raise LiveCallInvalidSignal("Unsupported live call recipient role.")
    if role == access.role:
        raise LiveCallPermissionDenied("A participant cannot signal itself.")

    recipient = _get_participant_by_role(db, access.room, role)
    if int(recipient.user_id) == int(access.participant.user_id):
        raise LiveCallPermissionDenied("A participant cannot signal itself.")
    if str(recipient.status) == PARTICIPANT_LEFT:
        raise LiveCallInvalidTransition("The recipient has left this live call.")

    clean_payload = _validate_signal_payload(kind, payload)
    pending = int(
        db.query(func.count(InterviewCallSignal.id))
        .filter(
            InterviewCallSignal.room_id == access.room.id,
            InterviewCallSignal.consumed_at.is_(None),
        )
        .scalar()
        or 0
    )
    if pending >= _max_pending_signals():
        raise LiveCallRateLimited("The signaling queue for this room is full.")

    window_start = current - timedelta(seconds=SIGNAL_RATE_WINDOW_SECONDS)
    recent = int(
        db.query(func.count(InterviewCallSignal.id))
        .filter(
            InterviewCallSignal.room_id == access.room.id,
            InterviewCallSignal.sender_user_id == int(access.participant.user_id),
            InterviewCallSignal.created_at >= window_start,
        )
        .scalar()
        or 0
    )
    if recent >= _max_participant_signals():
        raise LiveCallRateLimited("Too many signaling messages. Try again shortly.")

    ttl = _bounded_int(signal_ttl_seconds, DEFAULT_SIGNAL_TTL_SECONDS, 15, 300)
    signal = InterviewCallSignal(
        room_id=access.room.id,
        sender_user_id=int(access.participant.user_id),
        recipient_user_id=int(recipient.user_id),
        signal_type=kind,
        payload=clean_payload,
        expires_at=current + timedelta(seconds=ttl),
        created_at=current,
    )
    db.add(signal)
    db.flush()
    return signal


def drain_signals(
    db: Session,
    room_id: str,
    *,
    user_id: Any,
    session_token: Any,
    limit: int = 50,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    current = _coerce_now(now)
    access = require_call_access(db, room_id, user_id, session_token)
    _validate_signaling_room(db, access, current)
    capped = _bounded_int(limit, 50, 1, 100)
    rows = (
        db.query(InterviewCallSignal)
        .filter(
            InterviewCallSignal.room_id == access.room.id,
            InterviewCallSignal.recipient_user_id == int(access.participant.user_id),
            InterviewCallSignal.consumed_at.is_(None),
        )
        .order_by(InterviewCallSignal.created_at, InterviewCallSignal.id)
        .limit(capped)
        .all()
    )
    if not rows:
        return []

    expired_ids = [
        int(row.id)
        for row in rows
        if row.expires_at is not None and current > row.expires_at
    ]
    if expired_ids:
        db.execute(
            update(InterviewCallSignal)
            .where(
                InterviewCallSignal.id.in_(expired_ids),
                InterviewCallSignal.consumed_at.is_(None),
            )
            .values(consumed_at=current)
        )

    valid_ids = [int(row.id) for row in rows if int(row.id) not in set(expired_ids)]
    if not valid_ids:
        return []
    claimed = db.execute(
        update(InterviewCallSignal)
        .where(
            InterviewCallSignal.id.in_(valid_ids),
            InterviewCallSignal.consumed_at.is_(None),
        )
        .values(consumed_at=current)
    )
    if _affected_rows(claimed) != len(valid_ids):
        raise LiveCallInvalidTransition("Signals changed during delivery. Retry the operation.")

    claimed_rows = (
        db.query(InterviewCallSignal)
        .filter(InterviewCallSignal.id.in_(valid_ids))
        .order_by(InterviewCallSignal.created_at, InterviewCallSignal.id)
        .all()
    )
    drained: list[dict[str, Any]] = []
    for signal in claimed_rows:
        sender_role = "unknown"
        if int(signal.sender_user_id) == int(access.room.admin_user_id):
            sender_role = ROLE_ADMIN
        elif int(signal.sender_user_id) == int(access.room.candidate_user_id):
            sender_role = ROLE_CANDIDATE
        drained.append(
            {
                "id": int(signal.id),
                "signal_type": str(signal.signal_type),
                "sender_role": sender_role,
                "payload": dict(signal.payload or {}),
                "created_at": signal.created_at,
            }
        )
    return drained


def get_call_signal_summary(
    db: Session,
    room_id: str,
    *,
    user_id: Any,
    session_token: Any,
) -> dict[str, Any]:
    access = require_call_access(db, room_id, user_id, session_token)
    pending_total = int(
        db.query(func.count(InterviewCallSignal.id))
        .filter(
            InterviewCallSignal.room_id == access.room.id,
            InterviewCallSignal.consumed_at.is_(None),
        )
        .scalar()
        or 0
    )
    pending_for_user = int(
        db.query(func.count(InterviewCallSignal.id))
        .filter(
            InterviewCallSignal.room_id == access.room.id,
            InterviewCallSignal.recipient_user_id == int(access.participant.user_id),
            InterviewCallSignal.consumed_at.is_(None),
        )
        .scalar()
        or 0
    )
    latest = (
        db.query(InterviewCallSignal.signal_type)
        .filter(InterviewCallSignal.room_id == access.room.id)
        .order_by(InterviewCallSignal.created_at.desc(), InterviewCallSignal.id.desc())
        .first()
    )
    return {
        "pending_total": pending_total,
        "pending_for_user": pending_for_user,
        "latest_signal_type": str(latest[0]) if latest else None,
    }


def expire_stale_invites(
    db: Session,
    *,
    now: datetime | None = None,
    room_id: str | None = None,
    limit: int = 100,
) -> int:
    current = _coerce_now(now)
    query = db.query(InterviewCallRoom).filter(
        InterviewCallRoom.status.in_([INVITED, ACCEPTED]),
        InterviewCallRoom.expires_at < current,
    )
    if room_id is not None:
        query = query.filter(InterviewCallRoom.room_id == str(room_id))
    rooms = query.limit(_bounded_int(limit, 100, 1, 500)).all()
    expired = 0
    for room in rooms:
        source = str(room.status)
        try:
            _transition(
                db,
                room,
                EXPIRED,
                actor_user_id=int(room.admin_user_id),
                now=current,
                expected={source},
            )
        except LiveCallInvalidTransition:
            continue
        _mark_all_left(db, room, current)
        expired += 1
    return expired
