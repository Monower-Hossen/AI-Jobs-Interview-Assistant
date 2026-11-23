"""Learning progress page."""

import streamlit as st
from services.auth_service import owned_session_id
from services.learning_service import get_learning_snapshot

from frontend.shared.components import empty_state, error_state, page_header
from frontend.shared.navigation import go_to
from frontend.user.learning_progress_view import render_learning_progress


def show_learning_progress() -> None:
    page_header(
        "Learning progress",
        "Study plan completion, assessments, streaks, and achievements",
        ":material/trending_up:",
    )
    token = st.session_state.get("user_session_id")
    session_id = owned_session_id(token, st.session_state.get("user_id")) if token else None
    if not session_id:
        empty_state("No workspace found.")
        return
    try:
        snapshot = get_learning_snapshot(session_id, st.session_state.get("user_id"))
    except Exception as exc:
        error_state("Learning progress could not be loaded.", str(exc))
        return
    render_learning_progress(session_id, snapshot)
    if st.button(
        "Open Learning Center",
        icon=":material/school:",
        width="content",
        key="learning_progress_open_center",
    ):
        go_to("learning_center")


show_learning_progress()
