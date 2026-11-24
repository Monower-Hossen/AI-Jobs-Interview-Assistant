"""Advanced performance analytics from persisted records."""

from datetime import UTC, datetime, timedelta

import plotly.graph_objects as go
import streamlit as st
from services.analytics import get_analytics

from frontend.shared.components import data_table, empty_state, kpi_row, page_header, section
from frontend.shared.navigation import go_to
from frontend.user.interview_page import _ensure_user_session


def _theme_colors() -> tuple[str, str, str]:
    if st.context.theme.type == "dark":
        return "#0B1020", "#94A3B8", "#8B5CF6"
    return "#FFFFFF", "#475569", "#6D28D9"


def show_analytics_page() -> None:
    page_header(
        "Performance analytics",
        "Trends derived from your saved interview records",
        ":material/analytics:",
    )

    # --- Filters ---
    col_filter1, col_filter2, col_filter3, col_filter4, col_filter5 = st.columns(5)
    with col_filter1:
        date_from = st.date_input(
            "From", value=None, format="YYYY-MM-DD", key="analytic_date_from"
        )
    with col_filter2:
        date_to = st.date_input(
            "To", value=None, format="YYYY-MM-DD", key="analytic_date_to"
        )
    with col_filter3:
        interview_type = st.selectbox(
            "Interview type",
            ["All", "Technical", "Behavioral", "Case"],
            index=0,
            key="analytic_interview_type",
        )
    with col_filter4:
        difficulty = st.selectbox(
            "Difficulty",
            ["All", "easy", "medium", "hard"],
            index=0,
            key="analytic_difficulty",
        )
    with col_filter5:
        min_score, max_score = st.slider(
            "Score range",
            0.0,
            10.0,
            (0.0, 10.0),
            key="analytic_score_range",
        )

    snapshot = get_analytics(
        _ensure_user_session(),
        st.session_state.get("user_id"),
        date_from=date_from,
        date_to=date_to,
        interview_type=interview_type if interview_type != "All" else None,
        difficulty=difficulty if difficulty != "All" else None,
        minimum_score=min_score,
        maximum_score=max_score,
    )

    delta = f"{snapshot.progress_percent:+.1f}%" if snapshot.previous_score else None
    performance_imp_value = (
        f"{snapshot.performance_improvement_percent:.1f}%"
        if snapshot.scored_interviews >= 2
        else "—"
    )
    practice_hours_value = f"{snapshot.practice_hours:.1f}" if snapshot.total_interviews else "0.0"
    # type: ignore[list-item]
    kpi_row(
        [
            ("Interviews", snapshot.total_interviews, None),
            ("Completed", snapshot.completed_interviews, None),
            ("Average score", f"{snapshot.average_score:.1f}/10", None),
            ("Best score", f"{snapshot.best_score:.1f}/10", None),
            ("Lowest score", f"{snapshot.lowest_score:.1f}/10", None),
            ("Performance improvement", performance_imp_value, delta),
            ("Readiness", f"{snapshot.readiness_score:.1f}/10", None),
            ("Practice hours", practice_hours_value, None),
        ],
    )
    # type: ignore[list-item]

    if snapshot.total_interviews == 0:
        empty_state("No interview records yet.", "Complete a job interview to unlock analytics.")
        if st.button("Start an interview", type="primary", width="stretch"):
            go_to("interview_setup")
        return

    background, text_color, accent = _theme_colors()

    # --- Performance Overview ---
    section("Performance overview", ":material/trending_up:")
    overview_kpi_rows = [
        ("Total interviews", str(snapshot.total_interviews)),
        ("Completed interviews", str(snapshot.completed_interviews)),
        ("Average score", f"{snapshot.average_score:.1f}/10"),
        ("Best score", f"{snapshot.best_score:.1f}/10"),
        ("Lowest score", f"{snapshot.lowest_score:.1f}/10"),
        ("Performance improvement", performance_imp_value),
        ("Readiness", f"{snapshot.readiness_score:.1f}/10"),
        ("Practice hours", practice_hours_value),
    ]
    # Split into two columns for display
    mid = len(overview_kpi_rows) // 2
    c1, c2 = st.columns(2)
    for i, (label, value) in enumerate(overview_kpi_rows):
        with c1 if i < mid else c2:
            st.markdown(f"**{label}:** {value}")

    # --- Score Progression ---
    section("Score progression", ":material/trending_up:")
    if snapshot.trend is not None and not snapshot.trend.empty:
        figure = go.Figure()
        for column, color in (
            ("Overall", accent),
            ("Technical", "#06B6D4"),
            ("Communication", "#10B981"),
        ):
            figure.add_trace(
                go.Scatter(
                    x=snapshot.trend["Session"],
                    y=snapshot.trend[column],
                    mode="lines+markers",
                    name=column,
                    line={"color": color, "width": 2},
                )
            )
        figure.update_layout(
            paper_bgcolor=background,
            plot_bgcolor=background,
            font={"color": text_color},
            height=320,
            margin={"l": 20, "r": 20, "t": 10, "b": 20},
        )
        st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
    else:
        empty_state("No completed interviews to chart yet.")

    # Weekly activity view
    if snapshot.total_interviews > 0 and snapshot.completed_interviews > 0:
        section("Weekly activity", ":material/calendar_month:")
        today = datetime.now(tz=UTC).astimezone().date()
        weekly_rows = []
        for offset in range(6, -1, -1):
            day = today - timedelta(days=offset)
            count = sum(
                1
                for session in snapshot.recent_sessions
                if session.get("Date")
                and session.get("Date")[:10]
                == day.strftime("%Y-%m-%d")
            )
            weekly_rows.append(
                {"Day": day.strftime("%a"), "Interviews": count, "Date": day.isoformat()}
            )
        data_table(weekly_rows, "No weekly activity data available.")

    # --- Skill-wise Performance ---
    section("Skill-wise performance", ":material/insights:")
    if snapshot.skill_breakdown is not None and not snapshot.skill_breakdown.empty:
        figure = go.Figure(
            go.Bar(
                x=snapshot.skill_breakdown["Skill"],
                y=snapshot.skill_breakdown["Score"],
                marker={"color": accent},
            )
        )
        figure.update_layout(
            paper_bgcolor=background,
            plot_bgcolor=background,
            font={"color": text_color},
            height=300,
            margin={"l": 20, "r": 20, "t": 10, "b": 20},
            xaxis_title="Skill",
            yaxis_title="Average score",
        )
        st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
    else:
        empty_state("No skill data available.")

    # --- Strengths and Weaknesses ---
    section("Strengths and weaknesses", ":material/insights:")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Strongest skills**")
        if snapshot.strong_areas:
            for area in snapshot.strong_areas:
                st.markdown(f"- {area}")
        else:
            empty_state("No strong areas identified yet.")
    with col2:
        st.markdown("**Needs practice**")
        if snapshot.weak_areas:
            for area in snapshot.weak_areas:
                st.markdown(f"- {area}")
        else:
            empty_state("No weak areas identified yet.")

    # Supporting evidence from evaluations
    if snapshot.scored_interviews > 0:
        section("Supporting evidence", ":material/question_mark:")
        feedback_samples = []
        for session in reversed(snapshot.recent_sessions[:3]):
            score = session.get("Score")
            if score is not None:
                feedback_samples.append(f"Session {session.get('ID')}: score {score}/10")
        for sample in feedback_samples[:3]:
            st.caption(sample)

    # --- Interview Trends ---
    section("Interview trends", ":material/bar_chart:")
    completed_flags = [
        1 if session.get("Status") == "completed" else 0
        for session in snapshot.recent_sessions
    ]
    if any(completed_flags):
        scored_sessions = [
            s for s in snapshot.recent_sessions if s.get("Score") is not None
        ]
        figure = go.Figure(
            go.Bar(
                x=[f"#{s.get('ID')}" for s in scored_sessions],
                y=[s.get("Score", 0) or 0 for s in scored_sessions],
                marker={"color": accent},
            )
        )
        figure.update_layout(
            paper_bgcolor=background,
            plot_bgcolor=background,
            font={"color": text_color},
            height=250,
            margin={"l": 20, "r": 20, "t": 10, "b": 20},
            xaxis_title="Interview",
            yaxis_title="Score",
        )
        st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})

    # --- Recommended practice topics ---
    section("Recommended practice topics", ":material/lightbulb:")
    for topic in snapshot.recommendations or []:
        st.markdown(f"- {topic}")
    if not snapshot.recommendations:
        empty_state("Complete an interview to receive recommendations.")

    # --- Sessions ---
    section("Sessions", ":material/history:")
    data_table(snapshot.recent_sessions or [], "No sessions recorded.")