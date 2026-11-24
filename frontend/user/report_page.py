import json
from datetime import datetime

import streamlit as st
from config.settings import runtime_path
from database import get_db_context
from database.models import GeneratedReport, InterviewSession
from services import interview_service
from services.auth_service import AuthError, owned_session_id
from services.report_generator import render_interview_pdf

from frontend.shared.components import (
    empty_state,
    error_state,
    kpi_row,
    page_header,
    section,
    show_improvement_plan,
)
from frontend.shared.navigation import go_to

_CATEGORY_LABELS = {
    "technical": "Technical",
    "hr": "HR",
    "behavioral": "Behavioral",
    "situational": "Situational",
    "project_based": "Project-based",
    "project-based": "Project-based",
    "resume_based": "Resume-based",
    "resume-based": "Resume-based",
    "job_specific": "Job-specific",
    "job-specific": "Job-specific",
}

_DIFFICULTY_LABELS = {
    "easy": "Easy",
    "medium": "Medium",
    "hard": "Hard",
}


def _format_category(value):
    if value is None:
        return "N/A"
    if hasattr(value, "value"):
        value = value.value
    return _CATEGORY_LABELS.get(str(value).lower(), str(value).replace("_", "- ").title())


def _format_difficulty(value):
    if value is None:
        return "N/A"
    if hasattr(value, "value"):
        value = value.value
    return _DIFFICULTY_LABELS.get(str(value).lower(), str(value).title())


def _score_text(value, suffix: str = "/10") -> str:
    if value is None:
        return "—"
    return f"{float(value):.1f}{suffix}"


def _filter_interviews(interviews, start_date, end_date, minimum_score):
    filtered = []
    for interview in interviews:
        created_at = interview.created_at
        if created_at:
            created_date = created_at.date()
            if start_date and created_date < start_date:
                continue
            if end_date and created_date > end_date:
                continue
        score = getattr(interview, "overall_score", None)
        if minimum_score > 0 and (score is None or float(score) < minimum_score):
            continue
        filtered.append(interview)
    return filtered


def _workspace_id() -> int | None:
    token = st.session_state.get("user_session_id")
    if not token:
        return None
    user_id = st.session_state.get("user_id")
    if user_id is not None:
        return owned_session_id(token, user_id)
    with get_db_context() as db:
        from database.models import UserSession

        row = (
            db.query(UserSession)
            .filter(UserSession.session_id == token, UserSession.user_id.is_(None))
            .first()
        )
        return row.id if row else None


def _stored_pdf(
    interview: InterviewSession, session_id: int, report_data: dict, file_name: str
) -> bytes:
    directory = runtime_path("data/reports") / str(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    file_path = directory / file_name
    with get_db_context() as db:
        existing = (
            db.query(GeneratedReport)
            .filter(
                GeneratedReport.session_id == session_id,
                GeneratedReport.interview_session_id == interview.id,
                GeneratedReport.file_name == file_name,
            )
            .first()
        )
        if existing and existing.file_path and runtime_path(existing.file_path).exists():
            return runtime_path(existing.file_path).read_bytes()
        pdf_bytes = render_interview_pdf(report_data)
        file_path.write_bytes(pdf_bytes)
        if not existing:
            db.add(
                GeneratedReport(
                    session_id=session_id,
                    interview_session_id=interview.id,
                    report_type="interview",
                    file_name=file_name,
                    file_path=str(file_path),
                )
            )
        return pdf_bytes


def _filter_dates(key: str) -> tuple:
    dates = st.date_input("Report date range", key=key)
    if isinstance(dates, (list, tuple)) and len(dates) == 2:
        return dates[0], dates[1]
    if isinstance(dates, (list, tuple)) and dates:
        return dates[0], dates[0]
    return dates if dates else None, dates if dates else None


def show_report_page() -> None:
    page_header(
        "Interview reports",
        "Review scores, feedback and action plans from completed interviews",
        ":material/assessment:",
    )
    session_id = _workspace_id()
    if session_id is None:
        empty_state("No workspace found", "Start an interview to create a private workspace.")
        return

    token = st.session_state.user_session_id
    actor_user_id = st.session_state.get("user_id")
    try:
        all_rows = interview_service.get_interview_history(token, actor_user_id, limit=500)
        date_from, date_to = _filter_dates("report_dates")
        minimum_score = float(
            st.slider("Minimum overall score", 0.0, 10.0, 0.0, 0.5, key="report_min_score")
        )
        rows = interview_service.get_interview_history(
            token,
            actor_user_id,
            status="completed",
            date_from=date_from,
            date_to=date_to,
            minimum_score=minimum_score,
            limit=500,
        )
    except AuthError as exc:
        error_state("Workspace access unavailable", str(exc))
        return

    if not all_rows:
        empty_state("No interview data found", "Start an interview to build your reports.")
        if st.button("Start an interview", type="primary", width="stretch"):
            go_to("interview_setup")
        return
    if not rows:
        empty_state("No reports match these filters", "Adjust the date range or minimum score.")
        return

    st.caption(f"Showing {len(rows)} of {len(all_rows)} completed interviews")
    options = {row["Label"]: row["id"] for row in rows}
    selected = st.selectbox("Select interview", list(options), format_func=lambda key: key)
    selected_id = options[str(selected)]
    with get_db_context() as db:
        selected_interview = (
            db.query(InterviewSession)
            .filter(InterviewSession.id == selected_id, InterviewSession.session_id == session_id)
            .first()
        )
    if selected_interview is None:
        error_state("Selected interview could not be loaded", "Reload the page and try again.")
        return
    show_interview_report(selected_interview, session_id)


def show_interview_report(interview: InterviewSession, session_id: int) -> None:
    section("Overall performance", ":material/analytics:")
    metrics = [
        ("Overall", interview.overall_score),
        ("Technical", interview.technical_score),
        ("Communication", interview.communication_score),
        ("HR/Behavioral", interview.hr_score),
        ("Problem Solving", interview.problem_solving_score),
    ]
    kpi_row(
        [
            (
                label,
                _score_text(score),
                get_score_label(float(score)) if score is not None else None,
            )
            for label, score in metrics
        ]
    )
    if all(score is not None for _, score in metrics):
        show_radar_chart(metrics)
    else:
        st.caption("Some rubric scores are unavailable for this interview.")

    if not detail_has_evaluation(interview):
        empty_state(
            "No evaluation data for this interview",
            "Question answers are still available in the Question details tab.",
        )

    section("Feedback and next steps", ":material/lightbulb:")
    tabs = st.tabs(
        [
            "Strengths",
            "Improvement areas",
            "Missed concepts",
            "Recommended topics",
            "Recommended projects",
            "Practice questions",
            "7-day plan",
            "30-day plan",
            "Question details",
        ]
    )
    with tabs[0]:
        show_list_section("Strengths", interview.strengths)
    with tabs[1]:
        show_list_section("Improvement areas", interview.weaknesses)
    with tabs[2]:
        show_list_section("Frequently missed concepts", interview.missed_concepts)
    with tabs[3]:
        show_list_section("Recommended topics", interview.recommended_topics)
    with tabs[4]:
        show_list_section("Recommended projects", interview.recommended_projects)
    with tabs[5]:
        show_list_section("Recommended practice questions", interview.recommended_questions)
    with tabs[6]:
        show_improvement_plan("7-day improvement plan", interview.improvement_plan_7_day)
    with tabs[7]:
        show_improvement_plan("30-day improvement plan", interview.improvement_plan_30_day)
    with tabs[8]:
        show_question_details(interview.id, session_id)

    created_at = interview.created_at or datetime.now()
    report_data = {
        "interview_date": created_at.isoformat(),
        "interview_type": interview.interview_type,
        "difficulty": interview.difficulty,
        "scores": {
            "overall": interview.overall_score,
            "technical": interview.technical_score,
            "communication": interview.communication_score,
            "hr": interview.hr_score,
            "problem_solving": interview.problem_solving_score,
        },
        "strengths": interview.strengths,
        "weaknesses": interview.weaknesses,
        "missed_concepts": interview.missed_concepts,
        "recommended_topics": interview.recommended_topics,
        "recommended_projects": interview.recommended_projects,
        "recommended_questions": interview.recommended_questions,
        "improvement_plan_7_day": interview.improvement_plan_7_day,
        "improvement_plan_30_day": interview.improvement_plan_30_day,
    }
    stamp = f"{created_at.strftime('%Y%m%d_%H%M')}_{interview.id}"
    pdf_name = f"interview_report_{stamp}.pdf"
    json_name = f"interview_report_{stamp}.json"
    try:
        pdf_bytes = _stored_pdf(interview, session_id, report_data, pdf_name)
    except Exception as exc:
        pdf_bytes = b""
        error_state("PDF generation is unavailable for this report", str(exc))

    download_col, back_col = st.columns(2)
    with download_col:
        st.download_button(
            "Download PDF report",
            data=pdf_bytes,
            file_name=pdf_name,
            mime="application/pdf",
            icon=":material/picture_as_pdf:",
            width="stretch",
        )
        st.download_button(
            "Download JSON report",
            data=json.dumps(report_data, indent=2, default=str),
            file_name=json_name,
            mime="application/json",
            icon=":material/download:",
            width="stretch",
        )
    with back_col:
        if st.button("Back to history", icon=":material/arrow_back:", width="stretch"):
            go_to("history")
        if st.button("Practice again", icon=":material/replay:", width="stretch"):
            go_to("interview_setup")


def detail_has_evaluation(interview: InterviewSession) -> bool:
    return any(
        value is not None
        for value in (
            interview.overall_score,
            interview.technical_score,
            interview.communication_score,
            interview.hr_score,
            interview.problem_solving_score,
        )
    )


def get_score_label(score: float) -> str:
    if score >= 8.5:
        return "Excellent"
    if score >= 7:
        return "Good"
    if score >= 5:
        return "Average"
    return "Needs improvement"


def show_radar_chart(metrics):
    import plotly.graph_objects as go

    categories = [label for label, _ in metrics]
    values = [float(score) for _, score in metrics]
    fig = go.Figure()
    fig.add_trace(go.Scatterpolar(r=values, theta=categories, fill="toself", name="Scores"))
    fig.update_layout(
        polar={"radialaxis": {"visible": True, "range": [0, 10]}},
        showlegend=False,
        height=400,
        title="Skill radar",
    )
    st.plotly_chart(fig, width="stretch", alt="Radar chart of interview rubric scores")


def show_list_section(title, items):
    if items:
        for item in items:
            st.markdown(f"- {item}")
    else:
        st.caption(f"No {title.lower()} recorded")


def show_question_details(interview_id, session_id) -> None:
    try:
        detail = interview_service.get_interview_detail(
            interview_id=int(interview_id),
            session_token=st.session_state.user_session_id,
            actor_user_id=st.session_state.get("user_id"),
        )
    except AuthError as exc:
        error_state("This interview is not accessible", str(exc))
        return
    for question in detail.questions:
        category = _format_category(question["category"])
        difficulty = _format_difficulty(question["difficulty"])
        with st.expander(f"Q{question['number']} · {category} · {difficulty}"):
            st.markdown("**Question**")
            st.write(question["question"])
            if question["expected_key_points"]:
                st.caption("Expected points: " + ", ".join(question["expected_key_points"]))
            st.markdown("**Your answer**")
            st.write(question["answer"] or "Not answered")
            evaluation = question["evaluation"]
            if evaluation is None:
                st.caption("No evaluation data recorded for this answer.")
                continue
            st.metric("Overall", f"{evaluation.overall_score:.1f}/10", width="stretch")
            kpi_row(
                [
                    ("Technical", f"{evaluation.technical_accuracy:.1f}/10", None),
                    ("Relevance", f"{evaluation.relevance:.1f}/10", None),
                    ("Communication", f"{evaluation.communication:.1f}/10", None),
                    ("Confidence", f"{evaluation.confidence:.1f}/10", None),
                    ("Completeness", f"{evaluation.completeness:.1f}/10", None),
                ]
            )
            st.markdown("**Strengths:** " + ", ".join(evaluation.strengths[:5]))
            st.markdown("**Improve:** " + ", ".join(evaluation.weaknesses[:5]))
            if evaluation.suggested_answer:
                st.markdown("**Better answer**")
                st.write(evaluation.suggested_answer)


show_report_page()
