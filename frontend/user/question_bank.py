import streamlit as st
from database import InterviewQuestion, InterviewSession, get_db_context
from services import JobAnalysisResult, ResumeAnalysisResult, ResumeJobMatchResult
from services.auth_service import owned_session_id
from services.interview_generator import generate_interview_questions
from services.user_content_service import add_bookmark, delete_bookmark, list_bookmarks

from frontend.shared.components import empty_state, error_state, page_header, section

TOPICS = (
    "Python",
    "Machine learning",
    "Deep learning",
    "Data science",
    "Generative AI",
    "SQL",
    "System design",
    "Behavioral",
    "HR",
)


def _workspace_id() -> int | None:
    token = st.session_state.get("user_session_id")
    if not token:
        return None
    return owned_session_id(token, st.session_state.get("user_id"))


def _text(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def show_question_bank() -> None:
    page_header(
        "Question bank",
        "Review asked questions, save favorites, and generate focused practice",
        ":material/library_books:",
    )
    session_id = _workspace_id()
    if not session_id:
        empty_state("No workspace found.")
        return

    saved_tab, bookmarks_tab, generator_tab = st.tabs(
        ["Asked questions", "Bookmarks", "Generate practice"]
    )

    with saved_tab:
        with get_db_context() as db:
            rows = (
                db.query(InterviewQuestion)
                .join(
                    InterviewSession,
                    InterviewQuestion.interview_session_id == InterviewSession.id,
                )
                .filter(InterviewSession.session_id == session_id)
                .order_by(InterviewQuestion.asked_at.desc(), InterviewQuestion.id.desc())
                .all()
            )
            questions = [
                {
                    "id": row.id,
                    "Question": row.question_text,
                    "Category": _text(row.category).replace("_", " ").title(),
                    "Difficulty": _text(row.difficulty).title(),
                    "Score": f"{row.overall_score:.1f}/10" if row.overall_score else "Not scored",
                    "Key points": ", ".join(row.expected_key_points or []),
                    "Better answer": row.suggested_answer or "",
                }
                for row in rows
            ]

        if not questions:
            empty_state(
                "No asked questions yet.", "Complete a mock interview to populate the bank."
            )
        else:
            categories = sorted({row["Category"] for row in questions})
            search = (
                st.text_input("Search questions", key="question_bank_search", type="search")
                .strip()
                .lower()
            )
            selected_categories = st.multiselect(
                "Categories", categories, default=categories, key="question_bank_categories"
            )
            difficulty = st.selectbox(
                "Difficulty",
                ["All", "Easy", "Medium", "Hard"],
                key="question_bank_difficulty",
            )
            filtered = [
                row
                for row in questions
                if row["Category"] in selected_categories
                and (difficulty == "All" or row["Difficulty"] == difficulty)
                and (not search or search in row["Question"].lower())
            ]
            st.caption(f"{len(filtered)} of {len(questions)} questions")
            for row in filtered[:40]:
                with st.expander(f"{row['Category']} · {row['Question'][:90]}"):
                    st.write(row["Question"])
                    st.caption(f"Difficulty: {row['Difficulty']} · Score: {row['Score']}")
                    if row["Key points"]:
                        st.write(f"**Key points:** {row['Key points']}")
                    if row["Better answer"]:
                        st.write(f"**Better answer:** {row['Better answer']}")
                    if st.button(
                        "Save to bookmarks",
                        key=f"qbank_save_{row['id']}",
                        icon=":material/bookmark_add:",
                        width="stretch",
                    ):
                        add_bookmark(
                            session_id,
                            row["Question"],
                            category=row["Category"].lower().replace(" ", "_"),
                            difficulty=row["Difficulty"].lower(),
                            expected_key_points=(
                                row["Key points"].split(", ") if row["Key points"] else []
                            ),
                            source="history",
                            actor_user_id=st.session_state.get("user_id"),
                        )
                        st.toast("Question saved.", icon=":material/bookmark:")

    with bookmarks_tab:
        bookmarks = list_bookmarks(session_id, st.session_state.get("user_id"))
        if not bookmarks:
            empty_state("No bookmarks yet.", "Save questions from the other tabs.")
        else:
            st.dataframe(
                [{key: value for key, value in row.items() if key != "id"} for row in bookmarks],
                hide_index=True,
                width="stretch",
            )
            options = {f"{row['Question'][:70]}": row["id"] for row in bookmarks}
            choice = st.selectbox("Bookmark to manage", list(options), key="qbank_bookmark_choice")
            if st.button(
                "Remove bookmark",
                icon=":material/bookmark_remove:",
                key="qbank_delete_bookmark",
                width="stretch",
            ):
                delete_bookmark(options[choice], session_id, st.session_state.get("user_id"))
                st.toast("Bookmark removed.", icon=":material/check_circle:")
                st.rerun()

    with generator_tab:
        section("Generate focused questions", ":material/auto_awesome:")
        col1, col2, col3 = st.columns([2, 1, 1])
        with col1:
            topic = st.selectbox("Topic", TOPICS, key="qbank_topic")
        with col2:
            difficulty = (
                st.segmented_control(
                    "Difficulty", ["easy", "medium", "hard"], default="medium", key="qbank_gen_diff"
                )
                or "medium"
            )
        with col3:
            count = st.number_input("Questions", 3, 10, 5, key="qbank_count")
        if st.button(
            "Generate questions",
            type="primary",
            icon=":material/auto_awesome:",
            width="stretch",
        ):
            with st.spinner("Creating focused questions…"):
                try:
                    from services.knowledge_base import get_knowledge_base

                    context = get_knowledge_base(
                        st.session_state.user_session_id, st.session_state.get("user_id")
                    ).build_context(f"{topic} interview preparation")
                    questions = generate_interview_questions(
                        resume=st.session_state.get("resume_analysis") or ResumeAnalysisResult(),
                        job=st.session_state.get("job_analysis") or JobAnalysisResult(),
                        match=st.session_state.get("match_analysis")
                        or ResumeJobMatchResult(
                            technical_skills_match=0.0,
                            experience_match=0.0,
                            projects_match=0.0,
                            education_match=0.0,
                            overall_match=0.0,
                        ),
                        total_questions=count,
                        difficulty=difficulty or "medium",
                        knowledge_context=f"Focus: {topic}. {context}",
                        interview_type="Question Bank practice",
                    ).questions
                except Exception as exc:
                    error_state("Question generation failed.", str(exc))
                else:
                    if not questions:
                        empty_state("The model returned no questions.")
                    for index, question in enumerate(questions, 1):
                        with st.expander(f"Q{index}: {question.question_text[:90]}"):
                            st.write(question.question_text)
                            if question.expected_key_points:
                                st.caption("Key points: " + ", ".join(question.expected_key_points))
                            if st.button(
                                "Save to bookmarks",
                                icon=":material/bookmark_add:",
                                key=f"qbank_gen_save_{index}",
                                width="stretch",
                            ):
                                add_bookmark(
                                    session_id,
                                    question.question_text,
                                    category=topic.lower().replace(" ", "_"),
                                    difficulty=_text(question.difficulty).lower(),
                                    expected_key_points=list(question.expected_key_points or []),
                                    source="generated",
                                    actor_user_id=st.session_state.get("user_id"),
                                )
                                st.toast("Question saved.", icon=":material/bookmark:")


show_question_bank()
