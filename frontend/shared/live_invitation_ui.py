from datetime import date, datetime
from typing import Any

import streamlit as st
from services import live_call_media_service as media
from services import live_call_service as calls
from services.live_interview_coordination_service import (
    attach_media_session,
    get_media_config,
    receive_signals,
    release_media_session,
    signal_poll_seconds,
)

from frontend.shared.live_call_component import render_live_call


def _identity() -> tuple[int, str]:
    return int(st.session_state["user_id"]), str(st.session_state["user_session_id"])


def _signal_key(room_id: str) -> str:
    return f"_live_call_signals_{room_id}"


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


def render_action_buttons(room: dict[str, Any]) -> bool:
    from services.live_interview_coordination_service import (
        accept_invitation,
        cancel_invitation,
        complete_invitation,
        decline_invitation,
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
            "Accept",
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
                "Invitation accepted.",
            )
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
        if columns[0].button(
            "Start call",
            icon=":material/play_arrow:",
            width="stretch",
            key=f"_live_call_start_{room_id}",
            type="primary",
        ):
            changed = run(
                lambda: start_invitation(
                    room_id=room_id,
                    user_id=user_id,
                    session_token=session_token,
                ),
                "Live call coordination started.",
            )
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

    if status in {calls.ACCEPTED, calls.IN_PROGRESS} and columns[3].button(
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


def _poll_signals(room_id: str, user_id: int, session_token: str) -> None:
    try:
        messages = receive_signals(
            room_id=room_id,
            user_id=user_id,
            session_token=session_token,
            limit=50,
        )
    except calls.LiveCallError:
        return
    except Exception:
        return
    if not messages:
        return
    key = _signal_key(room_id)
    history = list(st.session_state.get(key) or [])
    history.extend(messages)
    st.session_state[key] = history[-50:]


@st.fragment(run_every=f"{signal_poll_seconds()}s")
def _poll_signals_fragment(room_id: str, user_id: int, session_token: str) -> None:
    _poll_signals(room_id, user_id, session_token)


def render_live_call_panel(room: dict[str, Any]) -> None:
    status = str(room["status"])
    if status not in {calls.ACCEPTED, calls.IN_PROGRESS}:
        return
    room_id = str(room["room_id"])
    user_id, session_token = _identity()
    key = _signal_key(room_id)
    if key not in st.session_state:
        st.session_state[key] = []

    st.markdown("#### Live call")
    st.caption(
        "Join to request real camera and microphone access. SDP and ICE are exchanged "
        "through the authenticated invitation queue; media flows through the browser peer connection."
    )
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
        st.caption("Only one live media room can use the bounded video slot at a time.")
        return
    except Exception as exc:
        st.error("Live media could not be started.", icon=":material/error:")
        st.caption(str(exc)[:200])
        return

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


def render_room(room: dict[str, Any]) -> bool:
    room_id = str(room["room_id"])
    with st.container(border=True):
        st.markdown(f"### {room.get('title') or 'Live interview'}")
        st.caption(f"Room: {room_id}")
        render_room_details(room)
        changed = render_action_buttons(room)
        render_live_call_panel(room)
    return changed
