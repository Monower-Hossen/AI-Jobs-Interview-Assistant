import json
from datetime import date, datetime
from typing import Any

import streamlit as st
from services import live_call_service as calls
from services.live_interview_coordination_service import (
    receive_signals,
    send_signal,
    signal_poll_seconds,
)

_SIGNAL_OPTIONS = [
    calls.SIGNAL_OFFER,
    calls.SIGNAL_ANSWER,
    calls.SIGNAL_ICE_CANDIDATE,
    calls.SIGNAL_ICE_GATHERING_COMPLETE,
    calls.SIGNAL_HANGUP,
    calls.SIGNAL_PING,
]
_DEFAULT_PAYLOADS = {
    calls.SIGNAL_OFFER: '{"type": "offer", "sdp": ""}',
    calls.SIGNAL_ANSWER: '{"type": "answer", "sdp": ""}',
    calls.SIGNAL_ICE_CANDIDATE: '{"candidate": "", "sdp_mid": "", "sdp_mline_index": 0}',
    calls.SIGNAL_ICE_GATHERING_COMPLETE: "{}",
    calls.SIGNAL_HANGUP: '{"reason": "User left the live interview"}',
    calls.SIGNAL_PING: '{"nonce": ""}',
}


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

    if role == calls.ROLE_CANDIDATE and status == calls.INVITED:
        if columns[0].button(
            "Accept",
            icon=":material/check:",
            width="stretch",
            key=f"_live_call_accept_{room_id}",
            type="primary",
        ):
            changed = _run_action(
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
            changed = _run_action(
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
            changed = _run_action(
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
            changed = _run_action(
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
            changed = _run_action(
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
            changed = _run_action(
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
        changed = _run_action(
            lambda: leave_invitation(
                room_id=room_id,
                user_id=user_id,
                session_token=session_token,
            ),
            "You left the live call.",
        )
    return changed


def _run_action(action, success_message: str) -> bool:
    try:
        action()
    except calls.LiveCallError as exc:
        st.error(str(exc), icon=":material/error:")
        return False
    except Exception as exc:
        st.error("The live call action could not be completed.", icon=":material/error:")
        st.caption(str(exc)[:200])
        return False
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


def _manual_poll(room_id: str, user_id: int, session_token: str) -> None:
    try:
        messages = receive_signals(
            room_id=room_id,
            user_id=user_id,
            session_token=session_token,
            limit=50,
        )
    except calls.LiveCallError as exc:
        st.error(str(exc), icon=":material/error:")
        return
    except Exception as exc:
        st.error("Signals could not be checked.", icon=":material/error:")
        st.caption(str(exc)[:200])
        return
    key = _signal_key(room_id)
    history = list(st.session_state.get(key) or [])
    history.extend(messages)
    st.session_state[key] = history[-50:]
    st.caption(f"Received {len(messages)} signaling message(s).")


def render_signaling_panel(room: dict[str, Any]) -> None:
    status = str(room["status"])
    if status not in {calls.ACCEPTED, calls.IN_PROGRESS}:
        return
    room_id = str(room["room_id"])
    role = str(room.get("role") or "")
    recipient = calls.ROLE_CANDIDATE if role == calls.ROLE_ADMIN else calls.ROLE_ADMIN
    user_id, session_token = _identity()
    key = _signal_key(room_id)
    if key not in st.session_state:
        st.session_state[key] = []

    with st.expander("WebRTC signaling", expanded=False):
        st.caption(
            "This panel exchanges signaling JSON only. Audio, video, camera, and microphone "
            "media are not relayed or verified here."
        )
        payload_key = f"_live_call_payload_{room_id}"
        kind_key = f"_live_call_kind_{room_id}"
        if payload_key not in st.session_state:
            st.session_state[payload_key] = _DEFAULT_PAYLOADS[calls.SIGNAL_OFFER]
        kind = st.selectbox(
            "Signal type",
            options=_SIGNAL_OPTIONS,
            key=kind_key,
        )
        if st.button(
            "Use template",
            icon=":material/code:",
            width="stretch",
            key=f"_live_call_template_{room_id}",
        ):
            st.session_state[payload_key] = _DEFAULT_PAYLOADS[str(kind)]
            st.rerun()
        st.text_area(
            "Signaling payload (JSON)",
            key=payload_key,
            height=140,
        )
        send_columns = st.columns(2)
        if send_columns[0].button(
            "Send signal",
            icon=":material/send:",
            width="stretch",
            type="primary",
            key=f"_live_call_send_{room_id}",
        ):
            raw = str(st.session_state.get(payload_key) or "")
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                st.error("Enter a valid JSON object.", icon=":material/error:")
                return
            if not isinstance(payload, dict):
                st.error("The signaling payload must be a JSON object.", icon=":material/error:")
                return
            try:
                send_signal(
                    room_id=room_id,
                    user_id=user_id,
                    session_token=session_token,
                    signal_type=str(kind),
                    recipient_role=recipient,
                    payload=payload,
                )
            except calls.LiveCallError as exc:
                st.error(str(exc), icon=":material/error:")
            except Exception as exc:
                st.error("The signal could not be queued.", icon=":material/error:")
                st.caption(str(exc)[:200])
            else:
                st.success("Signal queued for the other participant.", icon=":material/check_circle:")
        if send_columns[1].button(
            "Check for signals",
            icon=":material/sync:",
            width="stretch",
            key=f"_live_call_receive_{room_id}",
        ):
            _manual_poll(room_id, user_id, session_token)

        _poll_signals_fragment(room_id, user_id, session_token)
        messages = list(st.session_state.get(key) or [])
        if messages:
            st.caption(f"Signals held in this browser: {len(messages)}")
            st.json([_jsonable(message) for message in messages])
            if st.button(
                "Clear held signals",
                icon=":material/delete:",
                width="stretch",
                key=f"_live_call_clear_{room_id}",
            ):
                st.session_state[key] = []
                st.rerun()
        else:
            st.caption("No signaling messages held in this browser.")


def render_room(room: dict[str, Any]) -> bool:
    room_id = str(room["room_id"])
    with st.container(border=True):
        st.markdown(f"### {room.get('title') or 'Live interview'}")
        st.caption(f"Room: {room_id}")
        render_room_details(room)
        changed = render_action_buttons(room)
        render_signaling_panel(room)
    return changed
