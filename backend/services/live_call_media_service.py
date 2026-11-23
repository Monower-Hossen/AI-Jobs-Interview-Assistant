from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from config.settings import settings
from database import get_db_context
from services import live_call_service as calls
from services.resource_monitor import (
    acquire_resource_lease,
    heartbeat_resource_lease,
    release_resource_lease,
)

_MEDIA_TASK = "video_processing"
_STATE_LOCK = threading.RLock()


class LiveCallMediaError(Exception):
    """Base error for live media-session operations."""


class LiveCallMediaBusy(LiveCallMediaError):
    """No bounded media slot is available."""


@dataclass
class MediaSession:
    room_id: str
    lease_id: str
    owner_id: str
    roles: set[str] = field(default_factory=set)
    role_heartbeats: dict[str, datetime] = field(default_factory=dict)
    acquired_at: datetime = field(default_factory=lambda: datetime.now(UTC).replace(tzinfo=None))
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(UTC).replace(tzinfo=None))


_SESSIONS: dict[str, MediaSession] = {}


def _owner_id(room_id: str) -> str:
    return f"live_call:{room_id}"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _release_session(session: MediaSession) -> bool:
    return bool(
        release_resource_lease(
            session.lease_id,
            owner_id=session.owner_id,
            worker_stopped=True,
        )
    )


def _touch_role(session: MediaSession, role: str, now: datetime) -> None:
    session.role_heartbeats[role] = now
    session.last_heartbeat = max(session.role_heartbeats.values(), default=now)


def _stale_timeout(timeout_seconds: Any = None) -> float:
    value = settings.live_media_stale_seconds if timeout_seconds is None else timeout_seconds
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 120.0
    return max(30.0, min(parsed, 900.0))


def _valid_ice_url(value: Any) -> bool:
    url = str(value or "").strip()
    return url.startswith(("stun:", "turn:", "turns:"))


def ice_servers() -> list[dict[str, Any]]:
    servers: list[dict[str, Any]] = []
    stun_urls = [str(url).strip() for url in settings.webrtc_stun_urls if _valid_ice_url(url)]
    turn_urls = [str(url).strip() for url in settings.webrtc_turn_urls if _valid_ice_url(url)]
    for url in stun_urls:
        servers.append({"urls": url})
    username = str(settings.webrtc_turn_username or "").strip()
    credential = str(settings.webrtc_turn_credential or "").strip()
    for url in turn_urls:
        entry: dict[str, Any] = {"urls": url}
        if username:
            entry["username"] = username
        if credential:
            entry["credential"] = credential
            entry["credentialType"] = "password"
        servers.append(entry)
    return servers


def media_config(*, room_id: str, role: str) -> dict[str, Any]:
    return {
        "roomId": str(room_id),
        "role": str(role),
        "initiator": str(role) == calls.ROLE_ADMIN,
        "iceServers": ice_servers(),
        "iceTransportPolicy": str(settings.webrtc_ice_transport_policy),
        "maxReconnectAttempts": int(settings.webrtc_max_reconnect_attempts),
        "reconnectBaseDelaySeconds": float(settings.webrtc_reconnect_base_delay_seconds),
        "video": {
            "width": int(settings.webrtc_video_width),
            "height": int(settings.webrtc_video_height),
            "frameRate": int(settings.webrtc_video_fps),
        },
        "audio": {
            "echoCancellation": bool(settings.webrtc_audio_echo_cancellation),
            "noiseSuppression": bool(settings.webrtc_audio_noise_suppression),
            "autoGainControl": bool(settings.webrtc_audio_auto_gain_control),
        },
    }


def attach_media_session(
    *,
    room_id: str,
    user_id: Any,
    session_token: Any,
    role: str,
) -> dict[str, Any]:
    room_id = str(room_id)
    role = str(role)
    with get_db_context() as db:
        access = calls.require_call_access(db, room_id, user_id, session_token)
        if str(access.role) != role:
            raise calls.LiveCallPermissionDenied("Live call role does not match this account.")
        if str(access.room.status) not in {calls.ACCEPTED, calls.IN_PROGRESS}:
            raise calls.LiveCallInvalidTransition("Media requires an accepted live call.")
        if str(access.participant.status) == calls.PARTICIPANT_LEFT:
            raise calls.LiveCallInvalidTransition("You have left this live call.")

    now = _now()
    with _STATE_LOCK:
        session = _SESSIONS.get(room_id)
        if session is not None:
            if role not in session.roles:
                if len(session.roles) >= 2:
                    raise LiveCallMediaBusy("This live call already has two media participants.")
                session.roles.add(role)
            _touch_role(session, role, now)
            heartbeat_resource_lease(session.lease_id, owner_id=session.owner_id)
            return media_session_state(room_id)

        owner = _owner_id(room_id)
        lease, rejection = acquire_resource_lease(
            _MEDIA_TASK,
            owner_id=owner,
            worker_probe="is_alive",
        )
        if lease is None:
            message = rejection.message if rejection is not None else "System is busy. Please try again."
            raise LiveCallMediaBusy(message)
        session = MediaSession(
            room_id=room_id,
            lease_id=str(lease.lease_id),
            owner_id=owner,
            roles={role},
            role_heartbeats={role: now},
            acquired_at=now,
            last_heartbeat=now,
        )
        _SESSIONS[room_id] = session
        return media_session_state(room_id)


def release_media_session(*, room_id: str, role: str) -> dict[str, Any]:
    room_id = str(room_id)
    role = str(role)
    with _STATE_LOCK:
        session = _SESSIONS.get(room_id)
        if session is None:
            return {"active": False, "room_id": room_id, "roles": [], "lease_released": False}
        session.roles.discard(role)
        session.role_heartbeats.pop(role, None)
        if not session.roles:
            _SESSIONS.pop(room_id, None)
            released = _release_session(session)
            return {
                "active": False,
                "room_id": room_id,
                "roles": [],
                "lease_released": released,
            }
        session.last_heartbeat = max(
            session.role_heartbeats.values(),
            default=session.last_heartbeat,
        )
        return media_session_state(room_id)


def heartbeat_media_session(*, room_id: str, role: str | None = None) -> bool:
    with _STATE_LOCK:
        session = _SESSIONS.get(str(room_id))
        if session is None:
            return False
        roles = [str(role)] if role is not None else sorted(session.roles)
        if not roles:
            return False
        if role is not None and str(role) not in session.roles:
            return False
        now = _now()
        for entry in roles:
            _touch_role(session, entry, now)
        return bool(heartbeat_resource_lease(session.lease_id, owner_id=session.owner_id))


def reconcile_room_media(*, room_id: str, terminal: bool = False) -> dict[str, Any]:
    room_id = str(room_id)
    with _STATE_LOCK:
        session = _SESSIONS.get(room_id)
        if session is None:
            return {
                "active": False,
                "room_id": room_id,
                "roles": [],
                "lease_released": False,
                "changed": False,
            }
        if not terminal:
            state = media_session_state(room_id)
            state["lease_released"] = False
            state["changed"] = False
            return state
        _SESSIONS.pop(room_id, None)
        released = _release_session(session)
        return {
            "active": False,
            "room_id": room_id,
            "roles": [],
            "lease_released": released,
            "changed": True,
        }


def reconcile_stale_media_sessions(
    *,
    now: datetime | None = None,
    timeout_seconds: Any = None,
) -> dict[str, Any]:
    current = now or _now()
    timeout = _stale_timeout(timeout_seconds)
    released: list[dict[str, Any]] = []
    retained: list[str] = []
    with _STATE_LOCK:
        for room_id, session in list(_SESSIONS.items()):
            latest = max(session.role_heartbeats.values(), default=session.last_heartbeat)
            idle_seconds = max(0.0, (current - latest).total_seconds())
            if idle_seconds <= timeout:
                retained.append(room_id)
                continue
            _SESSIONS.pop(room_id, None)
            released.append(
                {
                    "room_id": room_id,
                    "idle_seconds": idle_seconds,
                    "lease_released": _release_session(session),
                }
            )
    return {
        "checked": len(released) + len(retained),
        "timeout_seconds": timeout,
        "retained": retained,
        "released": released,
    }


def media_session_state(room_id: str) -> dict[str, Any]:
    with _STATE_LOCK:
        session = _SESSIONS.get(str(room_id))
        if session is None:
            return {
                "active": False,
                "room_id": str(room_id),
                "roles": [],
                "lease_released": False,
            }
        return {
            "active": True,
            "room_id": session.room_id,
            "roles": sorted(session.roles),
            "acquired_at": session.acquired_at,
            "last_heartbeat": session.last_heartbeat,
            "lease_released": False,
        }


def clear_media_sessions() -> None:
    with _STATE_LOCK:
        for room_id in list(_SESSIONS):
            session = _SESSIONS.pop(room_id)
            _release_session(session)
