import logging

import streamlit as st
from services import JobAnalysisResult, ResumeAnalysisResult, ResumeJobMatchResult
from services.auth_service import owned_session_id
from services.interview_generator import generate_interview_questions
from services.llm_service import _error_message as redacted_error_message
from services.user_content_service import (
    add_bookmark,
    delete_bookmark,
    list_asked_questions,
    list_bookmarks,
)

from frontend.shared.components import empty_state, error_state, page_header, section

logger = logging.getLogger(__name__)

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

_GENERATED_STATE_KEY = "question_bank_generated"
_GENERATION_ERROR = "Question generation could not complete. Check the AI provider configuration and try again."
_SAVE_ERROR = "This question could not be saved. Please try again."
_DELETE_ERROR = "This bookmark could not be removed. Please try again."
_DELETE_MISSING = "This bookmark is no longer available. Refresh the list and try again."


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


def _generated_state(session_id: int, actor_user_id: int | None) -> dict:
    owner = f"{actor_user_id}:{session_id}"
    state = st.session_state.get(_GENERATED_STATE_KEY)
    if not isinstance(state, dict) or state.get("owner") != owner:
        state = {
            "owner": owner,
            "questions": [],
            "topic": "",
            "difficulty": "",
            "notice": "",
        }
        st.session_state[_GENERATED_STATE_KEY] = state
    if not isinstance(state.get("questions"), list):
        state["questions"] = []
    state.setdefault("topic", "")
    state.setdefault("difficulty", "")
    state.setdefault("notice", "")
    return state


def _serialize_generated_question(question, index: int) -> dict:
    return {
        "number": index,
        "question_text": str(question.question_text or "").strip(),
        "category": _text(question.category),
        "difficulty": _text(question.difficulty),
        "expected_key_points": [
            str(point) for point in (question.expected_key_points or [])
        ],
    }


def _save_bookmark(
    session_id: int,
    question_text: str,
    category: str,
    difficulty: str,
    expected_key_points: list[str],
    source: str,
) -> None:
    try:
        add_bookmark(
            session_id,
            question_text,
            category=category,
            difficulty=difficulty,
            expected_key_points=expected_key_points,
            source=source,
            actor_user_id=st.session_state.get("user_id"),
        )
    except Exception as exc:
        logger.warning("Bookmark save failed: %s", redacted_error_message(exc))
        st.error(_SAVE_ERROR, icon=":material/error:")
    else:
        st.toast("Question saved.", icon=":material/bookmark:")


def _asked_question_rows(session_id: int) -> list[dict]:
    try:
        rows = list_asked_questions(session_id, st.session_state.get("user_id"))
    except Exception as exc:
        logger.warning("Asked question load failed: %s", redacted_error_message(exc))
        error_state("Asked questions are temporarily unavailable.", "Try again in a moment.")
        return []
    return [
        {
            "id": row["id"],
            "Question": str(row["question_text"] or ""),
            "Category": _text(row["category"]).replace("_", " ").title(),
            "Difficulty": _text(row["difficulty"]).title(),
            "Score": (
                f"{float(row['overall_score']):.1f}/10" if row["overall_score"] else "Not scored"
            ),
            "Key points": ", ".join(str(point) for point in row["expected_key_points"]),
            "Better answer": str(row["suggested_answer"] or ""),
        }
        for row in rows
    ]


def _bookmark_options(bookmarks: list[dict]) -> dict[str, int]:
    options: dict[str, int] = {}
    for row in bookmarks:
        question = str(row["Question"])
        label = f"{question[:70]}{'…' if len(question) > 70 else ''} · #{row['id']}"
        options[label] = int(row["id"])
    return options


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
    actor_user_id = st.session_state.get("user_id")
    state = _generated_state(session_id, actor_user_id)

    saved_tab, bookmarks_tab, generator_tab = st.tabs(
        ["Asked questions", "Bookmarks", "Generate practice"]
    )

    with saved_tab:
        questions = _asked_question_rows(session_id)
        if not questions:
            empty_state(
                "No asked questions yet.", "Complete a job interview to populate the bank."
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
                        _save_bookmark(
                            session_id,
                            row["Question"],
                            category=row["Category"].lower().replace(" ", "_"),
                            difficulty=row["Difficulty"].lower(),
                            expected_key_points=(
                                row["Key points"].split(", ") if row["Key points"] else []
                            ),
                            source="history",
                        )

    with bookmarks_tab:
        try:
            bookmarks = list_bookmarks(session_id, actor_user_id)
        except Exception as exc:
            logger.warning("Bookmark load failed: %s", redacted_error_message(exc))
            error_state("Bookmarks are temporarily unavailable.", "Try again in a moment.")
            bookmarks = []
        if not bookmarks:
            empty_state("No bookmarks yet.", "Save questions from the other tabs.")
        else:
            st.dataframe(
                [{key: value for key, value in row.items() if key != "id"} for row in bookmarks],
                hide_index=True,
                width="stretch",
            )
            options = _bookmark_options(bookmarks)
            choice = st.selectbox(
                "Bookmark to manage", list(options), key="qbank_bookmark_choice"
            )
            if st.button(
                "Remove bookmark",
                icon=":material/bookmark_remove:",
                key="qbank_delete_bookmark",
                width="stretch",
            ):
                try:
                    removed = delete_bookmark(options[choice], session_id, actor_user_id)
                except Exception as exc:
                    logger.warning("Bookmark delete failed: %s", redacted_error_message(exc))
                    st.error(_DELETE_ERROR, icon=":material/error:")
                else:
                    if removed:
                        st.toast("Bookmark removed.", icon=":material/check_circle:")
                    else:
                        st.warning(_DELETE_MISSING, icon=":material/info:")
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
            key="qbank_generate_questions",
            width="stretch",
        ):
            clean_count = int(count)
            if not 3 <= clean_count <= 10:
                st.error("Choose between 3 and 10 questions.", icon=":material/error:")
            else:
                with st.spinner("Creating focused questions…"):
                    try:
                        from services.knowledge_base import get_knowledge_base

                        context = get_knowledge_base(
                            st.session_state.user_session_id, actor_user_id
                        ).build_context(f"{topic} interview preparation")
                        generated = generate_interview_questions(
                            resume=st.session_state.get("resume_analysis")
                            or ResumeAnalysisResult(),
                            job=st.session_state.get("job_analysis") or JobAnalysisResult(),
                            match=st.session_state.get("match_analysis")
                            or ResumeJobMatchResult(
                                technical_skills_match=0.0,
                                experience_match=0.0,
                                projects_match=0.0,
                                education_match=0.0,
                                overall_match=0.0,
                            ),
                            total_questions=clean_count,
                            difficulty=difficulty or "medium",
                            knowledge_context=f"Focus: {topic}. {context}",
                            interview_type="Question Bank practice",
                        ).questions
                    except Exception as exc:
                        logger.warning(
                            "Question generation failed: %s", redacted_error_message(exc)
                        )
                        state["notice"] = _GENERATION_ERROR
                    else:
                        if not generated:
                            state["notice"] = "No questions were generated. Previous results are unchanged."
                        else:
                            state.update(
                                {
                                    "questions": [
                                        _serialize_generated_question(question, index)
                                        for index, question in enumerate(generated, 1)
                                    ],
                                    "topic": str(topic),
                                    "difficulty": _text(difficulty),
                                    "notice": "",
                                }
                            )
        if st.button(
            "Clear generated questions",
            icon=":material/delete:",
            key="qbank_clear_generated",
            disabled=not state["questions"] and not state["notice"],
            width="stretch",
        ):
            state.update({"questions": [], "topic": "", "difficulty": "", "notice": ""})
            st.rerun()

        if state["notice"]:
            if state["questions"]:
                st.warning(f"{state['notice']} Previous results are still shown below.")
            else:
                error_state("Question generation failed.", state["notice"])
        if not state["questions"]:
            empty_state("No generated questions yet.", "Choose a topic and generate a batch.")
        else:
            st.caption(
                f"Generated for {state['topic']} · {state['difficulty']} · "
                f"{len(state['questions'])} questions"
            )
            for question in state["questions"]:
                question_text = str(question["question_text"])
                with st.expander(f"Q{question['number']}: {question_text[:90]}"):
                    st.write(question_text)
                    if question["expected_key_points"]:
                        st.caption(
                            "Key points: " + ", ".join(question["expected_key_points"])
                        )
                    if st.button(
                        "Save to bookmarks",
                        icon=":material/bookmark_add:",
                        key=f"qbank_gen_save_{question['number']}",
                        width="stretch",
                    ):
                        _save_bookmark(
                            session_id,
                            question_text,
                            category=str(state["topic"]).lower().replace(" ", "_"),
                            difficulty=str(state["difficulty"]).lower(),
                            expected_key_points=list(question["expected_key_points"]),
                            source="generated",
                        )


show_question_bank()
