import pandas as pd
import streamlit as st
from database import UserSession, get_db_context
from services.auth_service import AuthError, owned_session_id
from services.dashboard_service import get_dashboard_snapshot

from frontend.shared.components import empty_state, kpi_row, page_header, section
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


def _metric_text(value: float | None, suffix: str, decimals: int = 1) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{decimals}f}{suffix}"


def _render_quick_actions() -> None:
    section("Quick actions", ":material/bolt:")
    with st.container(horizontal=True, wrap=True):
        if st.button("Analyze resume", icon=":material/description:", width="content"):
            go_to("resume")
        if st.button("Match a job", icon=":material/work:", width="content"):
            go_to("job")
        if st.button("Start interview", icon=":material/forum:", width="content", type="primary"):
            go_to("interview_setup")
        if st.button("Open question bank", icon=":material/quiz:", width="content"):
            go_to("question_bank")
        if st.button("View reports", icon=":material/assessment:", width="content"):
            go_to("report")


def show_dashboard() -> None:
    page_header(
        "Overview",
        "A private, data-backed view of your interview preparation",
        ":material/dashboard:",
    )
    account = st.session_state.get("account")
    full_name = getattr(account, "full_name", "")
    if full_name:
        st.caption(f"Welcome back, {full_name}.")
    st.caption("Built from saved workspace records only. No demo data.")

    session_id = _workspace_id()
    if session_id is None:
        empty_state("No workspace yet", "Analyze a resume or start an interview to create one.")
        _render_quick_actions()
        return

    try:
        snapshot = get_dashboard_snapshot(session_id, st.session_state.get("user_id"))
    except AuthError as exc:
        empty_state("Workspace access unavailable", str(exc))
        _render_quick_actions()
        return

    analytics = snapshot.analytics
    has_activity = (
        snapshot.resume_score is not None
        or analytics.total_interviews > 0
        or snapshot.assessment_count > 0
    )
    if not has_activity:
        empty_state(
            "No activity yet",
            "Analyze a resume or start a mock interview to build your overview.",
        )

    kpi_row(
        [
            ("Total interviews", analytics.total_interviews, None),
            ("Completed interviews", analytics.completed_interviews, None),
            (
                "Average score",
                _metric_text(
                    analytics.average_score if analytics.scored_interviews else None,
                    "/10",
                ),
                None,
            ),
            ("Resume score", _metric_text(snapshot.resume_score, "/100", 0), None),
            ("Job match", _metric_text(analytics.match_score, "%", 0), None),
            (
                "Readiness",
                _metric_text(
                    analytics.readiness_score if analytics.scored_interviews else None,
                    "/10",
                ),
                None,
            ),
            ("Practice hours", snapshot.practice_hours, None),
        ]
    )
    if analytics.total_interviews:
        st.caption(
            f"{analytics.completed_interviews} of {analytics.total_interviews} interviews "
            f"completed · {analytics.completion_rate:.0f}% completion rate"
        )
    else:
        st.caption("No interviews recorded yet. Start one to build your performance history.")

    section("Performance trends", ":material/trending_up:")
    activity_column, trend_column = st.columns(2)
    with activity_column, st.container(border=True, height="stretch"):
        st.markdown("**Weekly activity**")
        weekly = pd.DataFrame(snapshot.weekly_activity)
        if not weekly.empty and weekly["Interviews"].sum():
            st.bar_chart(
                weekly.set_index("Day")["Interviews"],
                alt="Interviews recorded over the last seven days",
            )
        else:
            st.caption("No interviews in the last seven days.")

    with trend_column, st.container(border=True, height="stretch"):
        st.markdown("**Score trend**")
        trend = analytics.trend
        columns = [
            column
            for column in ("Overall", "Technical", "Communication")
            if trend is not None and column in trend.columns
        ]
        if analytics.scored_interviews and trend is not None and not trend.empty and columns:
            st.line_chart(
                trend.set_index("Session")[columns],
                alt="Overall, technical and communication scores by completed interview",
            )
        else:
            st.caption("Complete an interview with scores to see your trend.")

    section("Recent interview activity", ":material/history:")
    with st.container(border=True):
        if snapshot.recent_activity:
            st.dataframe(
                pd.DataFrame(snapshot.recent_activity),
                hide_index=True,
                width="stretch",
                alt="Recent saved interviews, analyses and assessments",
            )
        else:
            st.caption("No recent activity recorded.")

    section("Recent feedback", ":material/feedback:")
    with st.container(border=True):
        if snapshot.recent_feedback:
            st.dataframe(
                pd.DataFrame(snapshot.recent_feedback),
                hide_index=True,
                width="stretch",
                alt="Recent evaluated interview feedback from saved answers",
            )
        else:
            st.caption("No evaluated feedback yet. Complete an interview to see improvement notes.")

    section("Recommended next steps", ":material/checklist:")
    with st.container(border=True):
        if snapshot.recommended_actions:
            for action in snapshot.recommended_actions:
                st.markdown(f"- {action}")
        else:
            st.caption("No recommendations yet. Your saved results will suggest next steps.")

    _render_quick_actions()


show_dashboard()
