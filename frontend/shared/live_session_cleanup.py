from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

import streamlit as st
from services.live_interview_state import (
    MEDIA_STATUS,
    PENDING_MEDIA_IDS,
    clear_capture_buffers,
    current_answer_id,
    set_media_status,
    stop_recording,
)
from services.live_session_lifecycle import (
    LIVE_DISCONNECT_PENDING,
    LIVE_SESSION_PAGE,
    LiveSessionOwner,
    current_live_owner,
    is_live_session_owner,
    mark_recoverable_state,
    owner_matches,
    pop_disconnect_finalization,
    record_teardown,
)
from services.live_webcam_state import stop_all_streams

CAMERA_PLAYING = "_live_webcam_camera_playing"
MICROPHONE_PLAYING = "_live_webcam_microphone_playing"


def _state(state: MutableMapping | None = None) -> MutableMapping:
    return st.session_state if state is None else state


def teardown_live_session_state(
    state: MutableMapping | None = None,
    *,
    reason: str,
    owner: LiveSessionOwner | None = None,
    authorize: bool = True,
) -> dict[str, Any]:
    target = _state(state)
    current = current_live_owner(target)
    if authorize and owner is not None and not owner_matches(current, owner):
        return {"stopped": False, "reason": "unauthorized"}
    if authorize and owner is None and current is not None:
        return {"stopped": False, "reason": "missing owner"}

    answer_id = current_answer_id(target)
    pending = [int(item) for item in (target.get(PENDING_MEDIA_IDS) or [])]
    pop_disconnect_finalization(target)
    stop_recording(target)
    target[CAMERA_PLAYING] = False
    target[MICROPHONE_PLAYING] = False
    stop_all_streams(target)
    clear_capture_buffers(target, reason=reason)
    if str(target.get(MEDIA_STATUS) or "") not in {"saved", "not_saved", "deleted"}:
        set_media_status(target, "interrupted", reason)
    target[LIVE_DISCONNECT_PENDING] = None
    target[LIVE_SESSION_PAGE] = None
    record_teardown(
        target,
        current if owner is None else owner,
        reason,
        answer_id=answer_id,
        pending_media_ids=pending,
    )
    if answer_id or pending:
        mark_recoverable_state(
            target,
            reason=reason,
            answer_id=answer_id,
            pending_media_ids=pending,
        )
    return {
        "stopped": True,
        "reason": reason,
        "answer_id": answer_id,
        "pending_media_ids": pending,
    }


def teardown_for_owner_change(
    previous_owner: LiveSessionOwner,
    *,
    state: MutableMapping | None = None,
    reason: str = "The live session identity changed.",
) -> dict[str, Any]:
    return teardown_live_session_state(
        state,
        reason=reason,
        owner=previous_owner,
        authorize=False,
    )


def teardown_for_auth(
    *,
    user_id: int | None = None,
    session_token: str | None = None,
    state: MutableMapping | None = None,
    reason: str = "Authentication ended while live capture was active.",
) -> dict[str, Any]:
    target = _state(state)
    owner = current_live_owner(target)
    if owner is not None and not is_live_session_owner(target, owner):
        return {"stopped": False, "reason": "unauthorized"}
    return teardown_live_session_state(
        target,
        reason=reason,
        owner=owner,
        authorize=False,
    )


def teardown_for_navigation(
    *, state: MutableMapping | None = None, reason: str = "Navigated away from the live interview."
) -> dict[str, Any]:
    target = _state(state)
    if not target.get(LIVE_SESSION_PAGE):
        return {"stopped": False, "reason": "not on live page"}
    return teardown_for_auth(state=target, reason=reason)


def teardown_for_shutdown(
    *, state: MutableMapping | None = None, reason: str = "Application is shutting down."
) -> dict[str, Any]:
    from services.live_webcam_video import shutdown_all_video_recorders

    result = teardown_for_auth(state=state, reason=reason)
    result["recorders_cleared"] = shutdown_all_video_recorders()
    return result
