"""Standalone answer evaluation against the shared advisory rubric."""

import streamlit as st
from database import InterviewQuestion, InterviewSession, get_db_context
from services.answer_evaluator import AnswerEvaluation, evaluate_answer
from services.interview_generator import (
    Difficulty,
    QuestionCategory,
)
from services.interview_generator import (
    InterviewQuestion as QuestionModel,
)
from services.job_analyzer import JobAnalysisResult
from services.resume_analyzer import ResumeAnalysisResult

from frontend.shared.components import (
    data_table,
    empty_state,
    error_state,
    page_header,
    render_score_chart,
    section,
)
from frontend.user.interview_page import _ensure_user_session


def show_evaluation_page() -> None:
    page_header(
        "Answer evaluation",
        "Evaluate any answer with the same rubric used in job interviews",
        ":material/fact_check:",
    )
    st.caption("AI-generated scores are advisory estimates, not objective ground truth.")

    tab_new, tab_history = st.tabs(["Evaluate an answer", "Recent evaluations"])

    with tab_new:
        with st.container(border=True):
            source = st.segmented_control(
                "Question source", ["Type your own", "From past interview"], default="Type your own"
            )
            if source == "Type your own":
                question_text = st.text_area("Question", height=90, key="eval_question")
                key_points = st.text_input(
                    "Expected key points (comma separated)", key="eval_points"
                )
                category = st.selectbox(
                    "Category", ["technical", "hr", "behavioral", "situational", "project_based"]
                )
                difficulty = st.segmented_control(
                    "Difficulty", ["easy", "medium", "hard"], default="medium"
                )
            else:
                recent = _recent_questions()
                if not recent:
                    empty_state(
                        "No answered interview questions found yet.",
                        "Complete a job interview first.",
                    )
                    return
                labels = {
                    f"{item['question_text'][:70]} (id {item['id']})": item for item in recent
                }
                selected = st.selectbox("Question", list(labels))
                item = labels[selected]
                question_text = item["question_text"]
                key_points = ", ".join(item.get("expected_key_points") or [])
                category = item.get("category", "technical")
                difficulty = item.get("difficulty", "medium")

            answer = st.text_area("Candidate answer", height=200, key="eval_answer")
            if st.button(
                "Evaluate answer", type="primary", icon=":material/auto_awesome:", width="stretch"
            ):
                if not question_text.strip() or not answer.strip():
                    st.warning("Provide both a question and an answer.")
                else:
                    try:
                        try:
                            category_enum = QuestionCategory(str(category))
                        except ValueError:
                            category_enum = QuestionCategory.TECHNICAL
                        try:
                            difficulty_enum = Difficulty(str(difficulty or "medium"))
                        except ValueError:
                            difficulty_enum = Difficulty.MEDIUM
                        model = QuestionModel(
                            question_number=1,
                            category=category_enum,
                            difficulty=difficulty_enum,
                            question_text=question_text,
                            expected_key_points=[
                                point.strip() for point in key_points.split(",") if point.strip()
                            ],
                        )
                        with st.spinner("Evaluating…"):
                            evaluation = evaluate_answer(
                                question=model,
                                answer=answer,
                                resume=st.session_state.get("resume_analysis")
                                or ResumeAnalysisResult(),
                                job=st.session_state.get("job_analysis") or JobAnalysisResult(),
                                mode="behavioral" if category == "behavioral" else "standard",
                            )
                    except Exception as exc:
                        error_state("Evaluation failed.", str(exc))
                    else:
                        st.session_state.last_evaluation = evaluation
                        st.session_state.last_evaluation_question = question_text

        saved_evaluation = st.session_state.get("last_evaluation")
        if isinstance(saved_evaluation, AnswerEvaluation):
            section("Evaluation result", ":material/analytics:")
            render_score_chart(saved_evaluation.dimension_scores(), "Answer evaluation scores")
            st.metric("Overall", f"{saved_evaluation.overall_score:.1f}/10", border=True)
            tabs = st.tabs(["Strengths", "Improvements", "Ideal answer", "Feedback"])
            with tabs[0]:
                _bullets(saved_evaluation.strengths)
            with tabs[1]:
                _bullets(
                    saved_evaluation.weaknesses
                    + saved_evaluation.missing_points
                    + saved_evaluation.incorrect_points
                )
            with tabs[2]:
                st.write(saved_evaluation.ideal_answer or saved_evaluation.suggested_answer)
            with tabs[3]:
                st.write(saved_evaluation.communication_feedback)
                st.write(saved_evaluation.technical_feedback)

    with tab_history:
        rows = _recent_questions()
        if not rows:
            empty_state("No evaluations have been saved yet.")
        else:
            section("Recently evaluated answers", ":material/history:")
            data_table(rows)


def _bullets(items: list[str]) -> None:
    if items:
        for item in items:
            st.markdown(f"- {item}")
    else:
        empty_state("Nothing recorded for this section.")


def _recent_questions() -> list[dict]:
    session_id = _ensure_user_session()
    with get_db_context() as db:
        rows = (
            db.query(InterviewQuestion)
            .join(InterviewQuestion.interview_session)
            .filter(
                InterviewSession.session_id == session_id,
                InterviewQuestion.answer_text.isnot(None),
                InterviewQuestion.overall_score.isnot(None),
            )
            .order_by(InterviewQuestion.evaluated_at.desc())
            .limit(20)
            .all()
        )
        return [
            {
                "id": row.id,
                "question_text": row.question_text,
                "category": row.category,
                "expected_key_points": row.expected_key_points or [],
                "overall_score": row.overall_score,
                "date": row.evaluated_at.strftime("%Y-%m-%d %H:%M") if row.evaluated_at else "",
            }
            for row in rows
        ]
