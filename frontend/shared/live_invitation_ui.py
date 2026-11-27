from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import streamlit as st
from services import live_call_media_service as media
from services import live_call_service as calls
from services.live_interview_coordination_service import (
    attach_media_session,
    get_media_config,
    heartbeat_media_session,
    list_invitation_statuses,
    media_heartbeat_poll_seconds,
    receive_signals,
    release_media_session,
    signal_poll_seconds,
)

from frontend.shared.components import confirm_delete
from frontend.shared.live_call_component import render_live_call


def _identity() -> tuple[int, str]:
    return int(st.session_state["user_id"]), str(st.session_state["user_session_id"])


def _signal_key(room_id: str) -> str:
    return f"_live_call_signals_{room_id}"


def _invitation_snapshot_key() -> str:
    return "_live_invitation_status_snapshot"


_REFRESH_SUPPRESS_KEY = "_live_invitation_refresh_suppress_once"


def suppress_next_invitation_refresh() -> None:
    st.session_state[_REFRESH_SUPPRESS_KEY] = True


def _invitation_snapshot(statuses: list[dict[str, Any]]) -> dict[str, str]:
    return {str(row["room_id"]): str(row["status"]) for row in statuses}


@st.fragment(run_every=f"{max(2, min(signal_poll_seconds(), 10))}s")
def render_invitation_refresh(user_id: int, session_token: str) -> None:
    try:
        statuses = list_invitation_statuses(
            user_id=user_id,
            session_token=session_token,
            limit=50,
        )
    except Exception:
        return
    current = _invitation_snapshot(statuses)
    if bool(st.session_state.pop(_REFRESH_SUPPRESS_KEY, False)):
        st.session_state[_invitation_snapshot_key()] = current
        return
    previous = dict(st.session_state.get(_invitation_snapshot_key()) or {})
    st.session_state[_invitation_snapshot_key()] = current
    if not previous:
        return
    changed = any(
        previous.get(room_id) == calls.INVITED and status != calls.INVITED
        for room_id, status in current.items()
    )
    changed = changed or any(room_id not in previous for room_id in current)
    changed = changed or any(room_id not in current for room_id in previous)
    if changed:
        st.rerun()


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def render_room_details(room: dict[str, Any]) -> None:
    columns = st.columns(4)
    columns[0].metric("Status", str(room["status"]).replace("_", " ").title())
    columns[1].metric("Role", str(room.get("role") or "unknown").title())
    columns[2].metric("Pending for you", int(room.get("pending_for_user") or 0))
    columns[3].metric("Queued in room", int(room.get("pending_total") or 0))
    st.caption(
        f"Expires: {room.get('expires_at') or '-'} · "
        f"Administrator: {room.get('admin_name')} · Candidate: {room.get('candidate_name')}"
    )


def render_action_buttons(room: dict[str, Any], *, allow_delete: bool = False) -> bool:
    from services.live_interview_coordination_service import (
        accept_invitation,
        cancel_invitation,
        complete_invitation,
        decline_invitation,
        delete_invitation,
        interrupt_invitation,
        leave_invitation,
        start_invitation,
    )

    user_id, session_token = _identity()
    room_id = str(room["room_id"])
    role = str(room.get("role") or "")
    status = str(room["status"])
    columns = st.columns(4)
    changed = False

    def run(action, success_message: str) -> bool:
        return _run_action(action, success_message, room_id)

    if role == calls.ROLE_CANDIDATE and status == calls.INVITED:
        if columns[0].button(
            "Accept & join",
            icon=":material/check:",
            width="stretch",
            key=f"_live_call_accept_{room_id}",
            type="primary",
        ):
            changed = run(
                lambda: accept_invitation(
                    room_id=room_id,
                    user_id=user_id,
                    session_token=session_token,
                ),
                "Invitation accepted. You can join the live call now.",
            )
        st.caption("Accepting records participation; camera access is requested in the live call panel.")
        if columns[1].button(
            "Decline",
            icon=":material/close:",
            width="stretch",
            key=f"_live_call_decline_{room_id}",
        ):
            changed = run(
                lambda: decline_invitation(
                    room_id=room_id,
                    user_id=user_id,
                    session_token=session_token,
                ),
                "Invitation declined.",
            )
    elif role == calls.ROLE_ADMIN and status == calls.INVITED:
        if columns[0].button(
            "Start call",
            icon=":material/play_arrow:",
            width="stretch",
            key=f"_live_call_start_{room_id}",
            type="primary",
            disabled=True,
        ):
            pass
        if columns[1].button(
            "Cancel",
            icon=":material/cancel:",
            width="stretch",
            key=f"_live_call_cancel_{room_id}",
        ):
            changed = run(
                lambda: cancel_invitation(
                    room_id=room_id,
                    user_id=user_id,
                    session_token=session_token,
                ),
                "Invitation cancelled.",
            )
        st.caption("Start becomes available after the candidate accepts.")
    elif role == calls.ROLE_ADMIN and status == calls.ACCEPTED:
        can_start = str(room.get("participant_status") or "") != calls.PARTICIPANT_LEFT
        if columns[0].button(
            "Start call",
            icon=":material/play_arrow:",
            width="stretch",
            key=f"_live_call_start_{room_id}",
            type="primary",
            disabled=not can_start,
        ):
            changed = run(
                lambda: start_invitation(
                    room_id=room_id,
                    user_id=user_id,
                    session_token=session_token,
                ),
                "Live call coordination started.",
            )
        if not can_start:
            st.caption("Start is unavailable because you have left this live call.")
    elif status == calls.IN_PROGRESS:
        if columns[0].button(
            "Complete",
            icon=":material/check_circle:",
            width="stretch",
            key=f"_live_call_complete_{room_id}",
            type="primary",
        ):
            changed = run(
                lambda: complete_invitation(
                    room_id=room_id,
                    user_id=user_id,
                    session_token=session_token,
                ),
                "Live call marked complete.",
            )
        if columns[1].button(
            "Interrupt",
            icon=":material/report_problem:",
            width="stretch",
            key=f"_live_call_interrupt_{room_id}",
        ):
            changed = run(
                lambda: interrupt_invitation(
                    room_id=room_id,
                    user_id=user_id,
                    session_token=session_token,
                ),
                "Live call interrupted.",
            )

    if status in calls.ONGOING_CALL_STATES and columns[3].button(
        "Leave",
        icon=":material/logout:",
        width="stretch",
        key=f"_live_call_leave_{room_id}",
    ):
        changed = run(
            lambda: leave_invitation(
                room_id=room_id,
                user_id=user_id,
                session_token=session_token,
            ),
            "You left the live call.",
        )

    if allow_delete and role == calls.ROLE_ADMIN:
        delete_key = f"_live_invitation_delete_{room_id}"
        target_key = "_live_invitation_delete_target"
        if status in calls.ONGOING_CALL_STATES:
            columns[2].button(
                "Delete invitation",
                icon=":material/delete:",
                width="stretch",
                key=f"_live_invitation_delete_start_{room_id}",
                disabled=True,
            )
            st.caption(
                "Deletion is disabled while the interview is active. End or interrupt the call first."
            )
        else:
            if columns[2].button(
                "Delete invitation",
                icon=":material/delete:",
                width="stretch",
                key=f"_live_invitation_delete_start_{room_id}",
            ):
                st.session_state[target_key] = room_id
                st.session_state[delete_key] = False
                st.session_state[f"{delete_key}_ack"] = False
                st.session_state[f"{delete_key}_resolved"] = False

            if st.session_state.get(target_key) == room_id:
                if st.session_state.get(f"{delete_key}_resolved"):
                    confirmed = bool(st.session_state.get(delete_key))
                    st.session_state.pop(f"{delete_key}_resolved", None)
                    st.session_state.pop(target_key, None)
                    st.session_state[delete_key] = False
                    if confirmed:
                        changed = run(
                            lambda: delete_invitation(
                                room_id=room_id,
                                user_id=user_id,
                                session_token=session_token,
                                confirmation=True,
                            ),
                            "Invitation deleted.",
                        )
                    st.rerun()
                confirmation_label = (
                    "I confirm this pending invitation is no longer needed and understand "
                    "this action is permanent"
                    if status == calls.INVITED
                    else None
                )
                confirm_delete(
                    f"live interview invitation {room_id}",
                    key=delete_key,
                    confirmation_label=confirmation_label,
                )
    return changed


def _run_action(action, success_message: str, room_id: str) -> bool:
    _, session_token = _identity()
    try:
        action()
    except calls.LiveCallError as exc:
        st.error(str(exc), icon=":material/error:")
        return False
    except Exception as exc:
        st.error("The live call action could not be completed.", icon=":material/error:")
        st.caption(str(exc)[:200])
        return False
    try:
        release_media_session(
            room_id=room_id,
            user_id=int(st.session_state["user_id"]),
            session_token=session_token,
        )
    except Exception:
        pass
    st.success(success_message, icon=":material/check_circle:")
    return True


def _poll_signals(room_id: str, user_id: int, session_token: str) -> bool:
    media.reconcile_stale_media_sessions()
    try:
        messages = receive_signals(
            room_id=room_id,
            user_id=user_id,
            session_token=session_token,
            limit=50,
        )
    except calls.LiveCallError:
        return False
    except Exception:
        return False
    if not messages:
        return False
    key = _signal_key(room_id)
    history = list(st.session_state.get(key) or [])
    history.extend(messages)
    st.session_state[key] = history[-50:]
    return True


@st.fragment(run_every=f"{min(signal_poll_seconds(), media_heartbeat_poll_seconds())}s")
def _poll_signals_fragment(room_id: str, user_id: int, session_token: str) -> None:
    received = _poll_signals(room_id, user_id, session_token)
    try:
        heartbeat_media_session(
            room_id=room_id,
            user_id=user_id,
            session_token=session_token,
        )
    except Exception:
        pass
    if received:
        st.rerun()


def render_live_call_panel(room: dict[str, Any]) -> None:
    status = str(room["status"])
    room_id = str(room["room_id"])
    if status in calls.TERMINAL_CALL_STATES:
        media.reconcile_room_media(room_id=room_id, terminal=True)
        return
    if status not in {calls.ACCEPTED, calls.IN_PROGRESS}:
        return
    user_id, session_token = _identity()
    key = _signal_key(room_id)
    if key not in st.session_state:
        st.session_state[key] = []

    st.markdown("#### Live call")
    st.caption(
        "Join to request real camera and microphone access. SDP and ICE are exchanged "
        "through the authenticated invitation queue; media flows through the browser peer connection."
    )
    role = str(room.get("role") or "")
    if status == calls.IN_PROGRESS:
        if role == calls.ROLE_ADMIN:
            from services.live_interview_coordination_service import complete_invitation

            if st.button(
                "End interview",
                icon=":material/call_end:",
                width="stretch",
                key=f"_live_call_end_{room_id}",
                type="primary",
            ):
                changed = _run_action(
                    lambda: complete_invitation(
                        room_id=room_id,
                        user_id=user_id,
                        session_token=session_token,
                    ),
                    "Interview ended.",
                    room_id,
                )
                if changed:
                    st.rerun()
        else:
            st.caption("The administrator can end the interview.")
    _poll_signals_fragment(room_id, user_id, session_token)

    enabled = str(room.get("participant_status") or "") != calls.PARTICIPANT_LEFT
    try:
        media_state = attach_media_session(
            room_id=room_id,
            user_id=user_id,
            session_token=session_token,
        )
        config = get_media_config(
            room_id=room_id,
            user_id=user_id,
            session_token=session_token,
        )
    except calls.LiveCallError as exc:
        st.error(str(exc), icon=":material/error:")
        return
    except media.LiveCallMediaError as exc:
        st.error(str(exc), icon=":material/error:")
        diagnostics = media.media_slot_diagnostics()
        if diagnostics["active_room_count"]:
            rooms = ", ".join(str(item["room_id"]) for item in diagnostics["sessions"])
            st.caption(f"Active WebRTC room: {rooms}. Leave or end it before retrying.")
        else:
            st.caption("Stale WebRTC session recovery is automatic. Retry in a few seconds.")
        return
    except Exception as exc:
        st.error("Live media could not be started.", icon=":material/error:")
        st.caption(str(exc)[:200])
        return

    heartbeat_media_session(
        room_id=room_id,
        user_id=user_id,
        session_token=session_token,
    )
    st.caption(f"Room media slot active: {len(media_state.get('roles') or [])} participant role(s).")
    incoming = [_jsonable(message) for message in list(st.session_state.get(key) or [])]
    render_live_call(
        room=room,
        incoming=incoming,
        media_config=config,
        enabled=enabled,
        user_id=user_id,
        session_token=session_token,
    )


def render_room(
    room: dict[str, Any],
    *,
    allow_delete: bool = False,
    selection_checkbox: Callable[[], None] | None = None,
) -> bool:
    room_id = str(room["room_id"])
    media.reconcile_stale_media_sessions()
    with st.container(border=True):
        if selection_checkbox is not None:
            selection_checkbox()
        st.markdown(f"### {room.get('title') or 'Live interview'}")
        st.caption(f"Room: {room_id}")
        render_room_details(room)
        changed = render_action_buttons(room, allow_delete=allow_delete)
        render_live_call_panel(room)
    return changed
