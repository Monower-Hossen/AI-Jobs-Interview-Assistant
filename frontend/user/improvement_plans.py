"""Improvement plans page."""

import streamlit as st
from database import UserSession, get_db_context
from database.models import InterviewSession
from services import interview_service
from services.auth_service import AuthError, owned_session_id

from frontend.shared.components import (
    empty_state,
    error_state,
    page_header,
    show_improvement_plan,
)
from frontend.shared.navigation import go_to


def _workspace_id() -> int | None:
    token = st.session_state.get("user_session_id")
    if not token:
        return None
    user_id = st.session_state.get("user_id")
    if user_id is not None:
        return owned_session_id(token, user_id)
    with get_db_context() as db:
        row = (
            db.query(UserSession)
            .filter(UserSession.session_id == token, UserSession.user_id.is_(None))
            .first()
        )
        return row.id if row else None


def show_improvement_plans() -> None:
    page_header(
        "Improvement plans",
        "Personalized 7-day and 30-day plans from your completed interviews",
        ":material/checklist:",
    )
    session_id = _workspace_id()
    if session_id is None:
        empty_state("No workspace found", "Complete an interview to create a private workspace.")
        return

    token = st.session_state.user_session_id
    actor_user_id = st.session_state.get("user_id")
    try:
        rows = interview_service.get_interview_history(
            token, actor_user_id, status="completed", limit=500
        )
    except AuthError as exc:
        error_state("Workspace access unavailable", str(exc))
        return

    labels = {row["id"]: row["Label"] for row in rows}
    plans: list[InterviewSession] = []
    if labels:
        with get_db_context() as db:
            interviews = (
                db.query(InterviewSession)
                .filter(
                    InterviewSession.id.in_(list(labels)),
                    InterviewSession.session_id == session_id,
                )
                .order_by(InterviewSession.created_at.desc(), InterviewSession.id.desc())
                .all()
            )
            plans = [
                item
                for item in interviews
                if item.improvement_plan_7_day or item.improvement_plan_30_day
            ]
    if not plans:
        empty_state(
            "No improvement plans yet",
            "Complete a scored interview to receive a personalized plan.",
        )
        if st.button(
            "Start an interview",
            type="primary",
            width="content",
            key="improvement_plans_start_interview",
        ):
            go_to("interview_setup")
        return

    options = {labels.get(item.id, f"Session #{item.id}"): item for item in plans}
    choice = st.selectbox("Select interview", list(options), key="improvement_plan_interview")
    interview = options[str(choice)]
    show_improvement_plan("7-day improvement plan", interview.improvement_plan_7_day)
    show_improvement_plan("30-day improvement plan", interview.improvement_plan_30_day)
    if st.button(
        "Open full report",
        icon=":material/assessment:",
        width="content",
        key="improvement_plans_open_report",
    ):
        go_to("report")


show_improvement_plans()
