import pandas as pd
import streamlit as st
from database import UserSession, get_db_context
from services.auth_service import AuthError, owned_session_id
from services.dashboard_service import DashboardSnapshot, get_dashboard_snapshot

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


def _score_text(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):.1f}/10"


def _preparation_status(snapshot: DashboardSnapshot | None) -> str:
    if snapshot is None:
        return "Analyze a resume or start an interview to create your private workspace."
    analytics = snapshot.analytics
    if analytics.total_interviews == 0:
        if snapshot.resume_score is not None:
            return "Resume analyzed. Start a mock interview to build your readiness baseline."
        return "Start a mock interview or analyze a resume to build your preparation baseline."
    parts = [
        f"{analytics.completed_interviews} of {analytics.total_interviews} interviews completed"
    ]
    if analytics.scored_interviews:
        parts.append(f"readiness {analytics.readiness_score:.1f}/10")
    return " · ".join(parts) + "."


def _render_header(account, snapshot: DashboardSnapshot | None) -> None:
    full_name = str(getattr(account, "full_name", "") or "").strip()
    first_name = full_name.split()[0] if full_name else ""
    greeting = f"Welcome back, {first_name}" if first_name else "Welcome back"
    title_column, action_column = st.columns([3, 1], vertical_alignment="bottom")
    with title_column:
        page_header(greeting, _preparation_status(snapshot), ":material/dashboard:")
    with action_column:
        if st.button(
            "Start interview",
            type="primary",
            icon=":material/play_arrow:",
            width="stretch",
            key="dashboard_start_interview",
        ):
            go_to("interview_setup")


def _render_primary_kpis(snapshot: DashboardSnapshot) -> None:
    analytics = snapshot.analytics
    kpi_row(
        [
            ("Total interviews", analytics.total_interviews, None, ":material/forum:"),
            (
                "Completed interviews",
                analytics.completed_interviews,
                None,
                ":material/task_alt:",
            ),
            (
                "Average interview score",
                _metric_text(
                    analytics.average_score if analytics.scored_interviews else None,
                    "/10",
                ),
                None,
                ":material/grade:",
            ),
            (
                "Readiness score",
                _metric_text(
                    analytics.readiness_score if analytics.scored_interviews else None,
                    "/10",
                ),
                None,
                ":material/trending_up:",
            ),
        ]
    )
    if analytics.total_interviews:
        st.caption(
            f"{analytics.completion_rate:.0f}% completion rate · "
            f"{analytics.scored_interviews} scored interview(s)"
        )
    else:
        st.caption("No interviews recorded yet. Start one to build your performance history.")


def _render_secondary_metrics(snapshot: DashboardSnapshot) -> None:
    with st.container(border=True):
        st.caption("Preparation metrics")
        with st.container(horizontal=True, wrap=True):
            st.metric(
                "Resume score",
                _metric_text(snapshot.resume_score, "/100", 0),
                icon=":material/description:",
            )
            st.metric(
                "Job match",
                _metric_text(snapshot.analytics.match_score, "%", 0),
                icon=":material/work:",
            )
            st.metric(
                "Practice hours",
                snapshot.practice_hours,
                icon=":material/schedule:",
            )


def _render_performance_comparison(comparison: dict) -> None:
    if not comparison.get("available"):
        st.caption(str(comparison.get("reason") or "Not enough saved scores to compare."))
        return
    latest = comparison.get("latest_score")
    previous = comparison.get("previous_score")
    difference = comparison.get("difference")
    delta = f"{float(difference):+.1f}" if difference is not None else None
    st.metric("Latest score", _score_text(latest), delta)
    st.caption(
        f"Previous score: {_score_text(previous)} · "
        f"{str(comparison.get('direction') or '').capitalize()}"
    )
    percent_change = comparison.get("percent_change")
    if percent_change is None:
        st.caption("Absolute change only; the previous score was 0.")
    else:
        st.caption(f"{float(percent_change):+.1f}% versus the previous scored interview.")


def _render_skill_gaps(skill_gaps: dict, analytics) -> None:
    skill_df = analytics.skill_breakdown
    has_chart = skill_df is not None and not skill_df.empty
    if has_chart:
        st.bar_chart(
            skill_df.sort_values("Score", ascending=True).set_index("Skill")["Score"],
            horizontal=True,
            sort=False,
            alt="Average score per evaluated skill, weakest first",
        )
    if skill_gaps.get("has_data"):
        missing = skill_gaps.get("missing_skills") or []
        weak = skill_gaps.get("weak_areas") or []
        if missing:
            job_title = str(skill_gaps.get("job_title") or "latest job match")
            st.markdown(f"**Missing skills from {job_title}:** {', '.join(missing[:6])}")
        if weak:
            st.markdown(f"**Weaker areas from saved evaluations:** {', '.join(weak[:6])}")
    elif not has_chart:
        st.caption("No saved skill gaps yet. Analyze a job or complete an interview to see them.")
    if has_chart or skill_gaps.get("has_data"):
        if st.button(
            "Prepare skills",
            icon=":material/menu_book:",
            width="content",
            key="dashboard_prepare_skills",
        ):
            go_to("learning_center")


def _render_performance_overview(snapshot: DashboardSnapshot) -> None:
    section("Performance overview", ":material/insights:")
    activity_column, trend_column = st.columns(2)
    with activity_column, st.container(border=True, height="stretch"):
        st.markdown("**Weekly interview activity**")
        weekly = pd.DataFrame(snapshot.weekly_activity)
        if not weekly.empty and weekly["Interviews"].sum():
            st.bar_chart(
                weekly.set_index("Day")["Interviews"],
                alt="Interviews recorded over the last seven days",
            )
        else:
            st.caption("No interviews in the last seven days.")
    with trend_column, st.container(border=True, height="stretch"):
        st.markdown("**Score progression**")
        trend = snapshot.analytics.trend
        columns = [
            column
            for column in ("Overall", "Technical", "Communication")
            if trend is not None and column in trend.columns
        ]
        if (
            snapshot.analytics.scored_interviews
            and trend is not None
            and not trend.empty
            and columns
        ):
            st.line_chart(
                trend.set_index("Session")[columns],
                alt="Overall, technical and communication scores by completed interview",
            )
        else:
            st.caption("Complete an interview with scores to see your progression.")
    comparison_column, gaps_column = st.columns(2)
    with comparison_column, st.container(border=True, height="stretch"):
        st.markdown("**Latest versus previous**")
        _render_performance_comparison(snapshot.performance_comparison)
    with gaps_column, st.container(border=True, height="stretch"):
        st.markdown("**Skill gaps**")
        _render_skill_gaps(snapshot.skill_gaps, snapshot.analytics)


def _render_continue_preparation(snapshot: DashboardSnapshot | None) -> None:
    section("Continue preparation", ":material/bolt:")
    with st.container(border=True):
        with st.container(horizontal=True, wrap=True):
            if st.button(
                "Continue mock interview",
                icon=":material/smart_toy:",
                width="content",
                key="dashboard_continue_mock",
            ):
                go_to("interview_setup")
            if st.button(
                "Resume analysis",
                icon=":material/description:",
                width="content",
                key="dashboard_resume_analysis",
            ):
                go_to("resume")
            if st.button(
                "Job preparation",
                icon=":material/work:",
                width="content",
                key="dashboard_job_preparation",
            ):
                go_to("job")
            if st.button(
                "Continue learning",
                icon=":material/school:",
                width="content",
                key="dashboard_continue_learning",
            ):
                go_to("learning_center")
        progress = snapshot.learning_progress if snapshot is not None else {}
        if int(progress.get("total_tasks") or 0):
            st.caption(f"Study plan: {progress.get('summary')}")
        else:
            st.caption("No study plan yet. Create one in the Learning Center to track progress.")


def _render_recent_activity(snapshot: DashboardSnapshot) -> None:
    section("Recent activity", ":material/history:")
    with st.container(border=True):
        rows = [
            {
                "Interview": row.get("Type") or "Practice",
                "Score": _score_text(row.get("Score")),
                "Status": str(row.get("Status") or "").replace("_", " ").title(),
                "Date": row.get("Date") or "",
            }
            for row in (snapshot.analytics.recent_sessions or [])[:6]
        ]
        if rows:
            st.dataframe(
                pd.DataFrame(rows),
                hide_index=True,
                width="stretch",
                alt="Recent interviews with scores, status and dates",
            )
        else:
            st.caption("No interviews recorded yet. Start a mock interview to see activity here.")
        if st.button(
            "Open history",
            icon=":material/history:",
            width="content",
            key="dashboard_open_history",
        ):
            go_to("history")


def _render_improvement_plan(plan: dict) -> None:
    seven_day = str(plan.get("seven_day") or "")
    thirty_day = str(plan.get("thirty_day") or "")
    if seven_day:
        st.markdown(f"**7-day focus:** {seven_day}")
    if thirty_day:
        st.markdown(f"**30-day focus:** {thirty_day}")
    label = str(plan.get("interview_type") or "saved interview")
    date = str(plan.get("date") or "date unavailable")
    st.caption(f"Most recent saved plan · {label} · {date}")
    if st.button(
        "Open reports",
        icon=":material/assessment:",
        width="content",
        key="dashboard_open_reports",
    ):
        go_to("report")


def _render_recommendations(snapshot: DashboardSnapshot) -> None:
    section("AI recommendations", ":material/auto_awesome:")
    with st.container(border=True):
        weak = snapshot.skill_gaps.get("weak_areas") or []
        if weak:
            st.markdown("**Focus areas:** " + ", ".join(weak[:6]))
        if snapshot.recommended_actions:
            for action in snapshot.recommended_actions:
                st.markdown(f"- {action}")
        else:
            st.caption("No recommendations yet. Your saved results will suggest next steps.")
        if snapshot.improvement_plan.get("available"):
            with st.expander("Latest improvement plan", icon=":material/checklist:"):
                _render_improvement_plan(snapshot.improvement_plan)
        else:
            st.caption("No saved improvement plan yet. Your next scored report may include one.")


def _render_invitations(rows: list[dict]) -> None:
    if not rows:
        st.caption("No pending candidate invitations.")
    else:
        for row in rows[:4]:
            st.markdown(
                f"**{row.get('Title') or 'Live interview'}** · "
                f"{row.get('Status') or 'invited'} · expires {row.get('Expires') or 'unknown'}"
            )
    if st.button(
        "Open invitations",
        icon=":material/calendar_month:",
        width="content",
        key="dashboard_open_invitations",
    ):
        go_to("live_interview_invitations")


def _render_notifications(snapshot: DashboardSnapshot) -> None:
    unread = int(snapshot.notification_unread_count or 0)
    if unread:
        st.caption(f"{unread} unread notification(s).")
    rows = snapshot.recent_notifications or []
    if rows:
        for row in rows[:4]:
            st.markdown(
                f"**{row.get('Title') or 'Notification'}** · {row.get('State') or ''} · "
                f"{row.get('Date') or ''}"
            )
            message = str(row.get("Message") or "")
            if message:
                st.caption(message)
    else:
        st.caption("No saved notifications.")
    if st.button(
        "Open notifications",
        icon=":material/notifications:",
        width="content",
        key="dashboard_open_notifications",
    ):
        go_to("notifications")


def _render_upcoming_activities(snapshot: DashboardSnapshot) -> None:
    section("Upcoming activities", ":material/event_available:")
    invitation_column, notification_column = st.columns(2)
    with invitation_column, st.container(border=True, height="stretch"):
        st.markdown("**Pending live interviews**")
        _render_invitations(snapshot.upcoming_invitations)
    with notification_column, st.container(border=True, height="stretch"):
        st.markdown("**Recent notifications**")
        _render_notifications(snapshot)


def show_dashboard() -> None:
    account = st.session_state.get("account")
    session_id = _workspace_id()
    snapshot: DashboardSnapshot | None = None
    if session_id is not None:
        try:
            snapshot = get_dashboard_snapshot(
                session_id,
                st.session_state.get("user_id"),
                session_token=st.session_state.get("user_session_id"),
                actor_role=getattr(account, "role", None),
            )
        except AuthError as exc:
            _render_header(account, None)
            empty_state("Workspace access unavailable", str(exc))
            _render_continue_preparation(None)
            return

    _render_header(account, snapshot)
    if snapshot is None:
        empty_state("No workspace yet", "Analyze a resume or start an interview to create one.")
        _render_continue_preparation(None)
        return

    analytics = snapshot.analytics
    has_activity = (
        snapshot.resume_score is not None
        or analytics.total_interviews > 0
        or snapshot.assessment_count > 0
        or bool(snapshot.interview_history_summary.get("has_data"))
        or bool(snapshot.skill_gaps.get("has_data"))
        or bool(snapshot.improvement_plan.get("available"))
        or bool(snapshot.upcoming_invitations)
        or bool(snapshot.recent_notifications)
        or snapshot.notification_unread_count > 0
    )
    if not has_activity:
        empty_state(
            "No activity yet",
            "Analyze a resume or start a mock interview to build your overview.",
        )

    _render_primary_kpis(snapshot)
    _render_secondary_metrics(snapshot)
    _render_performance_overview(snapshot)
    _render_continue_preparation(snapshot)
    _render_recent_activity(snapshot)
    _render_recommendations(snapshot)
    _render_upcoming_activities(snapshot)
    st.caption("Built from saved workspace records only. No demo data.")


show_dashboard()
