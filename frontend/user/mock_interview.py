import streamlit as st
from services import interview_service
from services.auth_service import AuthError

from frontend.user.interview_page import (
    reset_interview_state,
    show_interview_page,
    show_interview_setup_page,
)


def _active_interview_state() -> bool:
    if not st.session_state.get("interview_started") or not st.session_state.get(
        "interview_questions"
    ):
        return False
    interview_id = st.session_state.get("interview_session_id")
    if not interview_id:
        return False
    try:
        detail = interview_service.get_interview_detail(
            interview_id=int(interview_id),
            session_token=str(st.session_state.get("user_session_id") or ""),
            actor_user_id=st.session_state.get("user_id"),
        )
    except AuthError:
        reset_interview_state()
        return False
    if detail.session["status"] == "completed":
        reset_interview_state()
        return False
    return True


if _active_interview_state():
    show_interview_page()
else:
    show_interview_setup_page()
