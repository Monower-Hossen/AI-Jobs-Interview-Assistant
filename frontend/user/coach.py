import streamlit as st
from database import Assessment, InterviewQuestion, InterviewSession, get_db_context
from services.auth_service import owned_session_id

from frontend.shared.components import empty_state, page_header, section
from frontend.user.behavioral_page import show_behavioral_page
from frontend.user.technical_page import show_technical_page


def _workspace_id() -> int | None:
    token = st.session_state.get("user_session_id")
    if not token:
        return None
    return owned_session_id(token, st.session_state.get("user_id"))


def show_coach() -> None:
    page_header(
        "AI interview coach",
        "Practice STAR answers, review technical work, and reuse saved feedback",
        ":material/psychology:",
    )
    session_id = _workspace_id()

    star_tab, technical_tab, feedback_tab = st.tabs(
        ["STAR practice", "Technical practice", "Saved feedback"]
    )
    with star_tab:
        show_behavioral_page(embedded=True)
    with technical_tab:
        show_technical_page(embedded=True)
    with feedback_tab:
        section("Latest answer feedback", ":material/feedback:")
        if not session_id:
            empty_state("No workspace found.")
            return
        with get_db_context() as db:
            questions = (
                db.query(InterviewQuestion)
                .join(
                    InterviewSession,
                    InterviewQuestion.interview_session_id == InterviewSession.id,
                )
                .filter(
                    InterviewSession.session_id == session_id,
                    InterviewQuestion.overall_score.is_not(None),
                )
                .order_by(InterviewQuestion.evaluated_at.desc(), InterviewQuestion.id.desc())
                .limit(6)
                .all()
            )
            assessments = (
                db.query(Assessment)
                .filter(Assessment.session_id == session_id, Assessment.status == "completed")
                .order_by(Assessment.created_at.desc())
                .limit(4)
                .all()
            )
            answer_rows = [
                {
                    "Question": (row.question_text or "")[:90],
                    "Score": f"{row.overall_score:.1f}/10",
                    "Improvements": ", ".join((row.weaknesses or [])[:3]) or "None recorded",
                    "Better answer": row.suggested_answer or "Not generated",
                }
                for row in questions
            ]
            assessment_rows = [
                {
                    "Topic": row.topic,
                    "Score": f"{row.score:.1f}/10",
                    "Date": row.created_at.strftime("%Y-%m-%d") if row.created_at else "",
                }
                for row in assessments
            ]

        if answer_rows:
            for row in answer_rows:
                with st.expander(f"{row['Question']} · {row['Score']}"):
                    st.write(f"**Improve:** {row['Improvements']}")
                    st.write(f"**Better answer:** {row['Better answer']}")
        else:
            empty_state("No evaluated answers yet.", "Complete a mock interview to build feedback.")

        section("Practice assessments", ":material/analytics:")
        if assessment_rows:
            st.dataframe(assessment_rows, hide_index=True, width="stretch")
        else:
            empty_state("No saved assessments yet.")


show_coach()
