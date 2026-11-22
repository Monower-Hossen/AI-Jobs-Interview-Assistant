"""STAR-based behavioral interview preparation and evaluation UI."""

from datetime import datetime

import streamlit as st
from database import Assessment, get_db_context
from services.answer_evaluator import evaluate_answer
from services.behavioral_service import (
    CATEGORIES,
    BehavioralQuestion,
    BehavioralService,
)
from services.job_analyzer import JobAnalysisResult
from services.resume_analyzer import ResumeAnalysisResult

from frontend.shared.components import (
    empty_state,
    error_state,
    page_header,
    render_score_chart,
    section,
    success_toast,
)
from frontend.user.interview_page import _ensure_user_session


def show_behavioral_page(embedded: bool = False) -> None:
    if not embedded:
        page_header(
            "Behavioral interview",
            "Practice structured STAR answers for common behavioral themes",
            ":material/groups:",
        )
        st.caption("STAR = Situation, Task, Action, Result. Scores are AI-generated guidance.")

    with st.container(border=True):
        col1, col2, col3 = st.columns([2, 1, 1])
        with col1:
            category = st.selectbox("Theme", CATEGORIES)
        with col2:
            difficulty = st.segmented_control(
                "Difficulty",
                ["easy", "medium", "hard"],
                default="medium",
                key="behavioral_difficulty_control",
            )
        with col3:
            count = st.number_input("Questions", 1, 5, 3, key="behavioral_question_count")
        if st.button(
            "Generate STAR practice",
            type="primary",
            icon=":material/auto_awesome:",
            width="stretch",
        ):
            try:
                with st.spinner("Creating behavioral questions…"):
                    questions = BehavioralService().generate(
                        category, difficulty or "medium", count
                    )
            except Exception as exc:
                error_state("Question generation failed.", str(exc))
                return
            else:
                st.session_state.behavioral_questions = [q.model_dump() for q in questions]
                st.session_state.behavioral_answers = {}
                st.session_state.behavioral_evaluations = {}
                st.session_state.behavioral_topic = f"Behavioral · {category}"
                st.session_state.behavioral_difficulty = difficulty or "medium"
                success_toast("Practice set ready.")

    questions = st.session_state.get("behavioral_questions", [])
    if not questions:
        empty_state(
            "Generate a practice set to begin.",
            "Choose a theme such as Leadership or Conflict resolution.",
        )
        return

    service = BehavioralService()
    for index, raw in enumerate(questions, 1):
        section(f"Question {index}", ":material/chat:")
        with st.container(border=True):
            st.markdown(f"**{raw['question_text']}**")
            answer = st.text_area("Your STAR answer", height=180, key=f"behavioral_answer_{index}")
            if st.button(
                "Evaluate answer", type="primary", key=f"behavioral_submit_{index}", width="stretch"
            ):
                if not answer.strip():
                    st.warning("Write an answer first.")
                else:
                    try:
                        with st.spinner("Evaluating against the STAR rubric…"):
                            question = BehavioralQuestion(**raw)
                            resume = (
                                st.session_state.get("resume_analysis") or ResumeAnalysisResult()
                            )
                            job = st.session_state.get("job_analysis") or JobAnalysisResult()
                            evaluation = evaluate_answer(
                                question=question.to_interview_question(),
                                answer=answer,
                                resume=resume,
                                job=job,
                                mode="behavioral",
                            )
                    except Exception as exc:
                        error_state("Evaluation failed.", str(exc))
                    else:
                        st.session_state.behavioral_answers[index] = answer
                        st.session_state.behavioral_evaluations[index] = evaluation
                        success_toast("Evaluation ready.")

        evaluation = st.session_state.behavioral_evaluations.get(index)
        if evaluation:
            render_score_chart(evaluation.dimension_scores(), "Behavioral rubric scores")
            st.metric("Overall", f"{evaluation.overall_score:.1f}/10", border=True, width="stretch")
            st.markdown(
                "**STAR gaps:** " + ", ".join(evaluation.missing_points or ["None identified"])
            )
            st.markdown("**Improved answer**")
            st.write(evaluation.suggested_answer)
            if st.button("Generate STAR example", key=f"star_example_{index}", width="stretch"):
                try:
                    with st.spinner("Preparing an example…"):
                        feedback = "; ".join(evaluation.weaknesses + evaluation.incorrect_points)
                        example = service.star_example(
                            raw["question_text"],
                            st.session_state.behavioral_answers[index],
                            feedback,
                        )
                except Exception as exc:
                    error_state("Could not create an example.", str(exc))
                else:
                    st.markdown(example)

    stored_evaluations = st.session_state.get("behavioral_evaluations", {})
    stored_answers = st.session_state.get("behavioral_answers", {})
    indices = sorted(set(stored_evaluations) & set(stored_answers))
    evaluations = [stored_evaluations[index] for index in indices]
    answers = [stored_answers[index] for index in indices]
    if evaluations and st.button("Save practice session", type="primary", width="stretch"):
        _save_practice(questions, answers, evaluations)


def _save_practice(questions: list[dict], answers: list, evaluations: list) -> None:
    session_id = _ensure_user_session()
    feedback = [
        {
            "overall_score": evaluation.overall_score,
            "missing_points": evaluation.missing_points,
            "strengths": evaluation.strengths,
            "suggested_answer": evaluation.suggested_answer,
        }
        for evaluation in evaluations
    ]
    with get_db_context() as db:
        db.add(
            Assessment(
                session_id=session_id,
                topic=st.session_state.get("behavioral_topic", "Behavioral · STAR"),
                difficulty=st.session_state.get("behavioral_difficulty", "medium"),
                questions=questions,
                answers=answers,
                feedback=feedback,
                score=sum(item.overall_score for item in evaluations) / len(evaluations),
                max_score=10.0,
                status="completed",
                completed_at=datetime.now(),
            )
        )
