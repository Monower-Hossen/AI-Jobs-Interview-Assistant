import streamlit as st
from services import live_call_service as calls
from services.live_interview_coordination_service import list_invitations

from frontend.shared.components import empty_state, error_state, page_header, section
from frontend.shared.live_invitation_ui import render_invitation_refresh, render_room


def show_user_live_interviews() -> None:
    account = st.session_state.get("account")
    if not account or getattr(account, "is_admin", False):
        st.error("Candidate access required.", icon=":material/error:")
        st.stop()
    user_id = int(account.id)
    session_token = str(st.session_state.get("user_session_id") or "")
    page_header(
        "Live Interview Invitations",
        "Review invitations and join authenticated real-time WebRTC interviews.",
        ":material/videocam:",
    )
    st.caption("Connection status is reported by your browser peer connection after you join.")
    render_invitation_refresh(user_id, session_token)

    section("Your invitations", ":material/list:")
    try:
        rooms = list_invitations(
            user_id=user_id,
            session_token=session_token,
            limit=50,
        )
    except calls.LiveCallError as exc:
        error_state("Invitations could not be loaded.", str(exc))
        return
    except Exception as exc:
        error_state("Invitations could not be loaded.", str(exc)[:200])
        return
    if not rooms:
        empty_state(
            "No live interview invitations yet.",
            "Invitations created by an administrator appear here.",
        )
        return
    changed = False
    for room in rooms:
        if render_room(room):
            changed = True
    if changed:
        st.rerun()


show_user_live_interviews()
