from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any

from services.live_recorder_lifecycle import (
    current_answer_id,
    recorder_lifecycle_snapshot,
)

LIVE_SESSION_OWNER = "live_webcam_session_owner"
LIVE_SESSION_OWNER_USER_ID = "live_webcam_session_owner_user_id"
LIVE_SESSION_ID = "live_webcam_session_id"
LIVE_SESSION_GENERATION = "live_webcam_session_generation"
LIVE_SESSION_HEARTBEAT = "live_webcam_session_heartbeat_ns"
LIVE_SESSION_TEARDOWNS = "live_webcam_session_teardowns"
LIVE_SESSION_RECOVERABLE = "live_webcam_session_recoverable"
LIVE_SESSION_PAGE = "live_webcam_session_page"
LIVE_STREAM_GENERATIONS = "live_webcam_stream_generations"
LIVE_STREAM_DISCONNECTED = "live_webcam_stream_disconnected"
LIVE_DISCONNECT_PENDING = "live_webcam_disconnect_pending"

LIVE_SESSION_KEYS = (
    LIVE_SESSION_OWNER,
    LIVE_SESSION_OWNER_USER_ID,
    LIVE_SESSION_ID,
    LIVE_SESSION_GENERATION,
    LIVE_SESSION_HEARTBEAT,
    LIVE_SESSION_TEARDOWNS,
    LIVE_SESSION_RECOVERABLE,
    LIVE_SESSION_PAGE,
    LIVE_STREAM_GENERATIONS,
    LIVE_STREAM_DISCONNECTED,
    LIVE_DISCONNECT_PENDING,
)

DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 30.0

_lock = threading.RLock()


@dataclass(frozen=True)
class LiveSessionOwner:
    owner_key: str
    user_id: int | None
    session_id: str
    generation: int


@dataclass(frozen=True)
class SessionBinding:
    previous: LiveSessionOwner | None
    current: LiveSessionOwner
    changed: bool


@dataclass(frozen=True)
class DisconnectRequest:
    answer_id: str
    question_number: int | None
    reason: str
    stream_generation: int | None
    requested_at_ns: int


def _owner_key(user_id: int | str | None, session_token: str | None) -> str:
    payload = f"{user_id or ''}|{session_token or ''}".encode()
    return hashlib.sha256(payload).hexdigest()


def initialize_live_session_state(state: MutableMapping) -> None:
    with _lock:
        state.setdefault(LIVE_SESSION_OWNER, None)
        state.setdefault(LIVE_SESSION_OWNER_USER_ID, None)
        state.setdefault(LIVE_SESSION_ID, None)
        state.setdefault(LIVE_SESSION_GENERATION, 0)
        state.setdefault(LIVE_SESSION_HEARTBEAT, None)
        state.setdefault(LIVE_SESSION_TEARDOWNS, [])
        state.setdefault(LIVE_SESSION_RECOVERABLE, None)
        state.setdefault(LIVE_SESSION_PAGE, None)
        state.setdefault(LIVE_STREAM_GENERATIONS, {})
        state.setdefault(LIVE_STREAM_DISCONNECTED, {})
        state.setdefault(LIVE_DISCONNECT_PENDING, None)


def current_live_owner(state: MutableMapping) -> LiveSessionOwner | None:
    initialize_live_session_state(state)
    key = state.get(LIVE_SESSION_OWNER)
    if not key:
        return None
    raw_user_id = state.get(LIVE_SESSION_OWNER_USER_ID)
    try:
        user_id = int(raw_user_id) if raw_user_id is not None else None
    except (TypeError, ValueError):
        user_id = None
    return LiveSessionOwner(
        owner_key=str(key),
        user_id=user_id,
        session_id=str(state.get(LIVE_SESSION_ID) or ""),
        generation=int(state.get(LIVE_SESSION_GENERATION) or 0),
    )


def identity_matches_live_session(
    state: MutableMapping, *, user_id: int | str | None, session_token: str | None
) -> bool:
    owner = current_live_owner(state)
    return bool(owner and owner.owner_key == _owner_key(user_id, session_token))


def bind_live_session(
    state: MutableMapping, *, user_id: int | str | None, session_token: str | None
) -> SessionBinding:
    initialize_live_session_state(state)
    with _lock:
        previous = current_live_owner(state)
        key = _owner_key(user_id, session_token)
        if previous and previous.owner_key == key:
            mark_session_heartbeat(state)
            current = LiveSessionOwner(
                owner_key=key,
                user_id=int(user_id) if user_id is not None else None,
                session_id=previous.session_id,
                generation=previous.generation,
            )
            return SessionBinding(previous=previous, current=current, changed=False)
        try:
            normalized_user_id = int(user_id) if user_id is not None else None
        except (TypeError, ValueError):
            normalized_user_id = None
        current = LiveSessionOwner(
            owner_key=key,
            user_id=normalized_user_id,
            session_id=uuid.uuid4().hex,
            generation=int(previous.generation if previous else 0) + 1,
        )
        state[LIVE_SESSION_OWNER] = current.owner_key
        state[LIVE_SESSION_OWNER_USER_ID] = normalized_user_id
        state[LIVE_SESSION_ID] = current.session_id
        state[LIVE_SESSION_GENERATION] = current.generation
        state[LIVE_STREAM_GENERATIONS] = {}
        state[LIVE_STREAM_DISCONNECTED] = {}
        state[LIVE_DISCONNECT_PENDING] = None
        mark_session_heartbeat(state)
        return SessionBinding(previous=previous, current=current, changed=True)


def owner_matches(owner: LiveSessionOwner | None, candidate: LiveSessionOwner | None) -> bool:
    return bool(
        owner
        and candidate
        and owner.owner_key == candidate.owner_key
        and owner.session_id == candidate.session_id
        and owner.generation == candidate.generation
    )


def is_live_session_owner(
    state: MutableMapping, owner: LiveSessionOwner | None
) -> bool:
    return owner_matches(current_live_owner(state), owner)


def can_control_live_recording(
    state: MutableMapping,
    owner: LiveSessionOwner | None,
    *,
    answer_id: str | None = None,
    stream_generation: int | None = None,
    kind: str | None = None,
) -> bool:
    if owner is not None and not is_live_session_owner(state, owner):
        return False
    if answer_id is not None and current_answer_id(state) != str(answer_id):
        return False
    if kind is not None and stream_generation is not None:
        if get_stream_generation(state, kind) != int(stream_generation):
            return False
    return True


def mark_session_heartbeat(state: MutableMapping, *, now_ns: int | None = None) -> None:
    initialize_live_session_state(state)
    state[LIVE_SESSION_HEARTBEAT] = int(now_ns if now_ns is not None else time.monotonic_ns())


def is_session_heartbeat_stale(
    state: MutableMapping,
    *,
    now_ns: int | None = None,
    timeout_seconds: float = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
) -> bool:
    initialize_live_session_state(state)
    raw = state.get(LIVE_SESSION_HEARTBEAT)
    if not raw:
        return True
    try:
        heartbeat = int(raw)
    except (TypeError, ValueError):
        return True
    current = int(now_ns if now_ns is not None else time.monotonic_ns())
    return (current - heartbeat) / 1_000_000_000 > float(timeout_seconds)


def get_stream_generation(state: MutableMapping, kind: str) -> int:
    initialize_live_session_state(state)
    generations = state.setdefault(LIVE_STREAM_GENERATIONS, {})
    return int(generations.get(str(kind), 0) or 0)


def bump_stream_generation(
    state: MutableMapping,
    kind: str,
    *,
    owner: LiveSessionOwner | None = None,
    expected_generation: int | None = None,
) -> int:
    initialize_live_session_state(state)
    if owner is not None and not is_live_session_owner(state, owner):
        return get_stream_generation(state, kind)
    if expected_generation is not None and get_stream_generation(state, kind) != int(
        expected_generation
    ):
        return get_stream_generation(state, kind)
    with _lock:
        generations = state.setdefault(LIVE_STREAM_GENERATIONS, {})
        value = int(generations.get(str(kind), 0) or 0) + 1
        generations[str(kind)] = value
        disconnected = state.setdefault(LIVE_STREAM_DISCONNECTED, {})
        disconnected.pop(str(kind), None)
        return value


def mark_stream_connected(
    state: MutableMapping,
    kind: str,
    *,
    owner: LiveSessionOwner | None = None,
    stream_generation: int | None = None,
) -> bool:
    if owner is not None and not is_live_session_owner(state, owner):
        return False
    if stream_generation is not None and get_stream_generation(state, kind) != int(
        stream_generation
    ):
        return False
    initialize_live_session_state(state)
    with _lock:
        state.setdefault(LIVE_STREAM_DISCONNECTED, {}).pop(str(kind), None)
        return True


def mark_stream_disconnected(
    state: MutableMapping,
    kind: str,
    *,
    owner: LiveSessionOwner | None = None,
    answer_id: str | None = None,
    stream_generation: int | None = None,
    reason: str = "The browser stream disconnected.",
    now_ns: int | None = None,
) -> bool:
    if owner is not None and not is_live_session_owner(state, owner):
        return False
    if answer_id is not None and current_answer_id(state) != str(answer_id):
        return False
    if stream_generation is not None and get_stream_generation(state, kind) != int(
        stream_generation
    ):
        return False
    initialize_live_session_state(state)
    with _lock:
        disconnected = state.setdefault(LIVE_STREAM_DISCONNECTED, {})
        disconnected[str(kind)] = {
            "reason": str(reason),
            "answer_id": str(answer_id) if answer_id else None,
            "stream_generation": (
                int(stream_generation) if stream_generation is not None else None
            ),
            "at_ns": int(now_ns if now_ns is not None else time.monotonic_ns()),
        }
    return True


def request_disconnect_finalization(
    state: MutableMapping,
    *,
    owner: LiveSessionOwner | None = None,
    answer_id: str | None,
    question_number: int | None = None,
    reason: str,
    stream_generation: int | None = None,
    now_ns: int | None = None,
) -> DisconnectRequest | None:
    if not answer_id:
        return None
    if owner is not None and not is_live_session_owner(state, owner):
        return None
    if current_answer_id(state) != str(answer_id):
        return None
    snapshot = recorder_lifecycle_snapshot(state)
    if snapshot.get("state") not in {"STARTING", "RECORDING", "STOPPING", "FINALIZING"}:
        return None
    request = DisconnectRequest(
        answer_id=str(answer_id),
        question_number=(
            int(snapshot.get("question_number"))
            if snapshot.get("question_number") is not None
            else (int(question_number) if question_number is not None else None)
        ),
        reason=str(reason),
        stream_generation=(
            int(stream_generation) if stream_generation is not None else None
        ),
        requested_at_ns=int(now_ns if now_ns is not None else time.monotonic_ns()),
    )
    initialize_live_session_state(state)
    with _lock:
        state[LIVE_DISCONNECT_PENDING] = request
    return request


def clear_disconnect_finalization(
    state: MutableMapping,
    *,
    owner: LiveSessionOwner | None = None,
    answer_id: str | None = None,
) -> bool:
    if owner is not None and not is_live_session_owner(state, owner):
        return False
    request = state.get(LIVE_DISCONNECT_PENDING)
    if request is None:
        return False
    if isinstance(request, DisconnectRequest):
        current_answer = request.answer_id
    elif isinstance(request, dict):
        current_answer = str(request.get("answer_id") or "")
    else:
        current_answer = ""
    if answer_id is not None and current_answer != str(answer_id):
        return False
    state[LIVE_DISCONNECT_PENDING] = None
    return True


def pop_disconnect_finalization(
    state: MutableMapping, owner: LiveSessionOwner | None = None
) -> DisconnectRequest | None:
    if owner is not None and not is_live_session_owner(state, owner):
        return None
    initialize_live_session_state(state)
    with _lock:
        request = state.get(LIVE_DISCONNECT_PENDING)
        state[LIVE_DISCONNECT_PENDING] = None
    if isinstance(request, DisconnectRequest):
        return request
    if isinstance(request, dict):
        try:
            return DisconnectRequest(
                answer_id=str(request["answer_id"]),
                question_number=(
                    int(request["question_number"])
                    if request.get("question_number") is not None
                    else None
                ),
                reason=str(request.get("reason") or ""),
                stream_generation=(
                    int(request["stream_generation"])
                    if request.get("stream_generation") is not None
                    else None
                ),
                requested_at_ns=int(request.get("requested_at_ns") or 0),
            )
        except (KeyError, TypeError, ValueError):
            return None
    return None


def record_teardown(
    state: MutableMapping,
    owner: LiveSessionOwner | None,
    reason: str,
    *,
    answer_id: str | None = None,
    pending_media_ids: list[int] | None = None,
    now_ns: int | None = None,
) -> dict[str, Any]:
    initialize_live_session_state(state)
    with _lock:
        entries = state.setdefault(LIVE_SESSION_TEARDOWNS, [])
        generation = int(owner.generation) if owner else int(state.get(LIVE_SESSION_GENERATION) or 0)
        for entry in entries:
            if entry.get("generation") == generation and entry.get("reason") == str(reason):
                return dict(entry)
        entry = {
            "generation": generation,
            "reason": str(reason),
            "answer_id": str(answer_id) if answer_id else None,
            "pending_media_ids": [int(item) for item in (pending_media_ids or [])],
            "at_ns": int(now_ns if now_ns is not None else time.monotonic_ns()),
        }
        entries.append(entry)
        return dict(entry)


def mark_recoverable_state(
    state: MutableMapping,
    *,
    reason: str,
    answer_id: str | None = None,
    pending_media_ids: list[int] | None = None,
) -> dict[str, Any]:
    entry = {
        "reason": str(reason),
        "answer_id": str(answer_id) if answer_id else None,
        "pending_media_ids": [int(item) for item in (pending_media_ids or [])],
    }
    initialize_live_session_state(state)
    state[LIVE_SESSION_RECOVERABLE] = entry
    return dict(entry)


def clear_recoverable_state(state: MutableMapping) -> None:
    initialize_live_session_state(state)
    state[LIVE_SESSION_RECOVERABLE] = None
