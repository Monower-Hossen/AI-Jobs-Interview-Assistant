import logging
from typing import Any

import streamlit as st
from services import JobAnalysisResult, ResumeAnalysisResult, ResumeJobMatchResult
from services.auth_service import owned_session_id
from services.interview_generator import generate_interview_questions
from services.llm_service import _error_message as redacted_error_message
from services.question_bank_service import (
    UNAVAILABLE_NOTICE,
    evaluate_practice_answer,
    list_practice_attempts,
)
from services.user_content_service import (
    add_bookmark,
    delete_bookmark,
    list_asked_questions,
    list_bookmarks,
)

from frontend.shared.components import empty_state, error_state, page_header, section

logger = logging.getLogger(__name__)

NO_INDUSTRY = "General / cross-industry"
NO_ROLE = "No specific role"
NO_TOPIC = "General interview"

INDUSTRIES = (
    NO_INDUSTRY,
    "Technology & software",
    "Healthcare & nursing",
    "Education & teaching",
    "Finance & accounting",
    "Marketing & communications",
    "Sales & business development",
    "Design & creative",
    "Engineering & construction",
    "Hospitality & tourism",
    "Human resources",
    "Legal & compliance",
    "Manufacturing & operations",
    "Nonprofit & public service",
    "Media & entertainment",
    "Retail & e-commerce",
)

ROLE_SUGGESTIONS = {
    "technology & software": (
        "Software Engineer",
        "AI/ML Engineer",
        "Data Analyst",
        "Product Manager",
        "QA Engineer",
        "DevOps Engineer",
    ),
    "healthcare & nursing": (
        "Nurse",
        "Physician",
        "Pharmacist",
        "Medical Technician",
        "Healthcare Administrator",
    ),
    "education & teaching": (
        "Teacher",
        "Professor",
        "Tutor",
        "School Administrator",
        "Curriculum Designer",
    ),
    "finance & accounting": (
        "Accountant",
        "Auditor",
        "Financial Analyst",
        "Banker",
        "Bookkeeper",
    ),
    "marketing & communications": (
        "Marketing Executive",
        "Content Strategist",
        "Social Media Manager",
        "PR Specialist",
    ),
    "sales & business development": (
        "Sales Executive",
        "Account Manager",
        "Business Development Manager",
        "Customer Success Manager",
    ),
    "design & creative": (
        "Graphic Designer",
        "UX Designer",
        "Illustrator",
        "Art Director",
    ),
    "engineering & construction": (
        "Civil Engineer",
        "Mechanical Engineer",
        "Electrical Engineer",
        "Project Engineer",
        "Site Manager",
    ),
    "hospitality & tourism": (
        "Hotel Manager",
        "Chef",
        "Tour Guide",
        "Event Coordinator",
    ),
    "human resources": ("HR Manager", "Recruiter", "HR Business Partner"),
    "legal & compliance": ("Lawyer", "Paralegal", "Compliance Officer"),
    "manufacturing & operations": (
        "Production Manager",
        "Quality Engineer",
        "Supply Chain Analyst",
    ),
    "nonprofit & public service": ("Program Manager", "Social Worker", "Policy Analyst"),
    "media & entertainment": ("Journalist", "Video Editor", "Producer"),
    "retail & e-commerce": ("Store Manager", "Merchandiser", "E-commerce Specialist"),
}

GENERAL_ROLES = (
    "Teacher",
    "Nurse",
    "Accountant",
    "Marketing Executive",
    "HR Manager",
    "Civil Engineer",
    "Sales Executive",
    "Graphic Designer",
)

TOPIC_SUGGESTIONS = {
    "technology & software": (
        "Python",
        "SQL",
        "System design",
        "Machine learning",
        "Cybersecurity",
        "Cloud computing",
    ),
    "healthcare & nursing": (
        "Clinical assessment",
        "Patient safety",
        "Medical ethics",
        "Triage",
        "Healthcare documentation",
    ),
    "education & teaching": (
        "Classroom management",
        "Lesson planning",
        "Student assessment",
        "Curriculum design",
    ),
    "finance & accounting": (
        "Financial reporting",
        "Budgeting",
        "Auditing",
        "Risk management",
    ),
    "marketing & communications": (
        "Campaign strategy",
        "SEO",
        "Brand messaging",
        "Marketing analytics",
    ),
    "sales & business development": (
        "Sales pipeline",
        "Negotiation",
        "Customer discovery",
        "CRM",
    ),
    "design & creative": (
        "Design critique",
        "Portfolio review",
        "User research",
        "Typography",
    ),
    "engineering & construction": (
        "Structural design",
        "Project planning",
        "Safety standards",
        "Materials",
    ),
    "hospitality & tourism": (
        "Guest service",
        "Hotel operations",
        "Food safety",
        "Event planning",
    ),
    "human resources": ("Recruitment", "Performance management", "Employee relations"),
    "legal & compliance": ("Contract review", "Legal research", "Regulatory compliance"),
    "manufacturing & operations": (
        "Lean manufacturing",
        "Quality control",
        "Supply chain",
    ),
    "nonprofit & public service": (
        "Grant writing",
        "Program evaluation",
        "Community outreach",
    ),
    "media & entertainment": ("Storytelling", "Editing", "Audience growth"),
    "retail & e-commerce": ("Inventory management", "Merchandising", "Customer experience"),
}

GENERAL_TOPICS = (
    "Technical",
    "Behavioral",
    "HR",
    "Situational",
    "Problem-solving",
    "Project-based",
)

QUESTION_TYPES = (
    "Mixed",
    "Technical",
    "HR and Behavioral",
    "Situational",
    "Problem-solving",
    "Conceptual",
    "Practical",
    "General Interview",
)

EXPERIENCE_LEVELS = (
    "Not specified",
    "Student",
    "Internship",
    "Fresher",
    "Entry Level",
    "Junior",
    "Mid-level",
    "Senior",
    "Experienced Professional",
)

USER_DIFFICULTIES = ("Beginner", "Intermediate", "Advanced", "Mixed")
DIFFICULTY_MAP = {
    "Beginner": "easy",
    "Intermediate": "medium",
    "Advanced": "hard",
    "Mixed": "mixed",
}
QUESTION_COUNTS = (5, 10, 15, 20)

_GENERATED_STATE_KEY = "question_bank_generated"
_PRACTICE_STATE_KEY = "question_bank_practice"
_TABS_STATE_KEY = "question_bank_tabs"
_PRACTICE_TAB_LABEL = "Answer practice"
_GENERATION_ERROR = "Question generation could not complete. Check the AI provider configuration and try again."
_EVALUATION_ERROR = "Your answer could not be submitted. Please try again."
_HISTORY_ERROR = "Practice history is temporarily unavailable."
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


def _catalog_choices(
    mapping: dict[str, tuple[str, ...]], key: Any, fallback: tuple[str, ...]
) -> tuple[str, ...]:
    return mapping.get(_text(key).strip().lower(), fallback)


def _clean_custom(value: Any, limit: int = 160) -> str:
    return " ".join(_text(value).split())[:limit]


def _optional_choice(value: Any, sentinel: str) -> str:
    clean = _clean_custom(value)
    return "" if not clean or clean == sentinel else clean


def _points_from_text(value: Any) -> list[str]:
    return [point.strip() for point in str(value or "").split(",") if point.strip()]


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _practice_payload(
    source: str,
    source_id: int | None,
    question_text: str,
    category: str,
    difficulty: str,
    expected_key_points: list[str],
) -> dict:
    return {
        "source": _text(source) or "generated",
        "source_id": source_id,
        "question_text": str(question_text or "").strip(),
        "category": _text(category) or "technical",
        "difficulty": _text(difficulty) or "medium",
        "expected_key_points": [str(point) for point in expected_key_points or []],
    }


def _practice_state(session_id: int, actor_user_id: int | None) -> dict:
    owner = f"{actor_user_id}:{session_id}"
    state = st.session_state.get(_PRACTICE_STATE_KEY)
    if not isinstance(state, dict) or state.get("owner") != owner:
        state = {
            "owner": owner,
            "question": None,
            "feedback": None,
            "last_answer": None,
        }
        st.session_state[_PRACTICE_STATE_KEY] = state
        st.session_state["qbank_practice_answer"] = ""
    state.setdefault("question", None)
    state.setdefault("feedback", None)
    state.setdefault("last_answer", None)
    return state


def _start_practice(payload: dict) -> None:
    session_id = _workspace_id()
    if not session_id:
        return
    state = _practice_state(session_id, st.session_state.get("user_id"))
    state["question"] = payload
    state["feedback"] = None
    state["last_answer"] = None
    st.session_state["qbank_practice_answer"] = ""
    st.session_state[_TABS_STATE_KEY] = _PRACTICE_TAB_LABEL


def _clear_practice() -> None:
    state = st.session_state.get(_PRACTICE_STATE_KEY)
    if isinstance(state, dict):
        state["question"] = None
        state["feedback"] = None
        state["last_answer"] = None
    st.session_state["qbank_practice_answer"] = ""


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
            "industry": "",
            "role": "",
            "question_type": "",
            "experience_level": "",
        }
        st.session_state[_GENERATED_STATE_KEY] = state
    if not isinstance(state.get("questions"), list):
        state["questions"] = []
    for key in (
        "topic",
        "difficulty",
        "notice",
        "industry",
        "role",
        "question_type",
        "experience_level",
    ):
        state.setdefault(key, "")
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


def _bookmark_rows(session_id: int, actor_user_id: int | None) -> list[dict]:
    try:
        return list_bookmarks(session_id, actor_user_id)
    except Exception as exc:
        logger.warning("Bookmark load failed: %s", redacted_error_message(exc))
        error_state("Bookmarks are temporarily unavailable.", "Try again in a moment.")
        return []


def _question_label(text: Any, limit: int = 72) -> str:
    clean = " ".join(str(text or "").split())
    return f"{clean[:limit]}…" if len(clean) > limit else clean


def _score_text(value: Any) -> str:
    try:
        return f"{float(value):.0f}/100"
    except (TypeError, ValueError):
        return "N/A"


def _render_feedback(result: dict) -> None:
    if str(result.get("status") or "") == "unavailable":
        st.warning(str(result.get("notice") or UNAVAILABLE_NOTICE), icon=":material/info:")
        return
    evaluation = result.get("evaluation")
    if not isinstance(evaluation, dict):
        error_state("Feedback is unavailable.", "Try this answer again.")
        return
    st.metric("Overall score", _score_text(evaluation.get("overall_score")))
    st.caption("AI-generated scores are advisory, not objective truth.")
    with st.container(border=True):
        score_col1, score_col2 = st.columns(2)
        score_col1.metric("Accuracy", _score_text(evaluation.get("accuracy")))
        score_col2.metric("Relevance", _score_text(evaluation.get("relevance")))
        score_col3, score_col4 = st.columns(2)
        score_col3.metric("Clarity", _score_text(evaluation.get("clarity")))
        score_col4.metric("Completeness", _score_text(evaluation.get("completeness")))
    for title, field in (
        ("Strengths", "strengths"),
        ("Missing points", "missing_points"),
        ("Improvements", "improvements"),
    ):
        values = [str(item) for item in evaluation.get(field) or []]
        if values:
            st.write(f"**{title}**")
            for value in values:
                st.markdown(f"- {value}")
    improved = str(evaluation.get("improved_answer") or "")
    if improved:
        with st.expander("Stronger example answer"):
            st.write(improved)
    final = str(evaluation.get("final_feedback") or "")
    if final:
        st.info(final, icon=":material/lightbulb:")


def _practice_history(session_id: int) -> list[dict]:
    try:
        return list_practice_attempts(session_id, st.session_state.get("user_id"))
    except Exception as exc:
        logger.warning("Practice history load failed: %s", redacted_error_message(exc))
        error_state(_HISTORY_ERROR, "Try again in a moment.")
        return []


def _render_practice_history(session_id: int) -> None:
    with st.expander("Recent practice history"):
        rows = _practice_history(session_id)
        if not rows:
            empty_state("No practice attempts yet.", "Evaluate an answer to start your history.")
            return
        for row in rows:
            score = (
                _score_text(row["overall_score"])
                if row["status"] == "evaluated"
                else "Feedback unavailable"
            )
            with st.expander(f"{score} · {str(row['question_text'])[:70]}"):
                st.caption(f"{_text(row['source'])} · {row['submitted_at']}")
                st.write("**Your answer**")
                st.write(str(row["answer_text"] or ""))
                if row["status"] == "evaluated":
                    if row["final_feedback"]:
                        st.info(str(row["final_feedback"]), icon=":material/lightbulb:")
                    if row["improved_answer"]:
                        with st.expander("Stronger example answer"):
                            st.write(str(row["improved_answer"]))
                else:
                    st.caption(UNAVAILABLE_NOTICE)


def _render_practice(session_id: int, state: dict) -> None:
    section("Answer practice", ":material/quiz:")
    question = state.get("question")
    if not isinstance(question, dict) or not str(question.get("question_text") or "").strip():
        empty_state("Choose a question using the Practice button in another Question Bank tab.")
    else:
        question_text = str(question["question_text"]).strip()
        category = _text(question.get("category")) or "technical"
        difficulty = _text(question.get("difficulty")) or "medium"
        points = [str(point) for point in question.get("expected_key_points") or []]
        st.write(f"**Question:** {question_text}")
        st.caption(f"{category} · {difficulty}")
        if points:
            st.caption("Key points: " + ", ".join(points))
        st.button(
            "Close practice",
            key="qbank_practice_close",
            icon=":material/close:",
            on_click=_clear_practice,
            width="content",
        )
        with st.form("qbank_practice_form"):
            answer = st.text_area(
                "Your answer",
                key="qbank_practice_answer",
                height=180,
                max_chars=12000,
                placeholder="Write your answer, then evaluate it for feedback.",
            )
            submitted = st.form_submit_button(
                "Evaluate answer",
                type="primary",
                icon=":material/send:",
                key="qbank_practice_submit",
                width="stretch",
            )
        if submitted:
            clean_answer = str(answer or "").strip()
            if not clean_answer:
                st.error("Write an answer before evaluating.", icon=":material/error:")
            else:
                fingerprint = (question_text, clean_answer)
                if state.get("last_answer") == fingerprint:
                    st.info(
                        "This answer was already submitted. Edit your answer to try again.",
                        icon=":material/info:",
                    )
                else:
                    with st.spinner("Evaluating your answer…"):
                        try:
                            result = evaluate_practice_answer(
                                session_id=session_id,
                                actor_user_id=st.session_state.get("user_id"),
                                question_text=question_text,
                                answer_text=clean_answer,
                                category=category,
                                difficulty=difficulty,
                                expected_key_points=points,
                                source=_text(question.get("source")) or "generated",
                                source_id=_optional_int(question.get("source_id")),
                            )
                        except Exception as exc:
                            logger.warning("Practice answer failed: %s", redacted_error_message(exc))
                            st.error(_EVALUATION_ERROR, icon=":material/error:")
                        else:
                            state["feedback"] = result
                            state["last_answer"] = fingerprint
                            if result.get("status") == "evaluated":
                                st.success(
                                    "Answer evaluated and saved.",
                                    icon=":material/check_circle:",
                                )
        feedback = state.get("feedback")
        if isinstance(feedback, dict):
            _render_feedback(feedback)
    _render_practice_history(session_id)


def _bookmark_options(bookmarks: list[dict]) -> dict[str, int]:
    options: dict[str, int] = {}
    for row in bookmarks:
        label = f"{_question_label(row['Question'])} · #{row['id']}"
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
    practice_state = _practice_state(session_id, actor_user_id)
    asked_rows = _asked_question_rows(session_id)
    bookmark_rows = _bookmark_rows(session_id, actor_user_id)

    practice_tab, saved_tab, bookmarks_tab, generator_tab = st.tabs(
        [
            _PRACTICE_TAB_LABEL,
            "Asked questions",
            "Bookmarks",
            "Generate practice",
        ],
        key=_TABS_STATE_KEY,
        on_change="rerun",
    )

    with practice_tab:
        _render_practice(session_id, practice_state)

    with saved_tab:
        questions = asked_rows
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
                    with st.container(horizontal=True):
                        st.button(
                            "Practice",
                            key=f"qbank_practice_history_{row['id']}",
                            icon=":material/quiz:",
                            on_click=_start_practice,
                            args=(
                                _practice_payload(
                                    "history",
                                    int(row["id"]),
                                    row["Question"],
                                    row["Category"].lower().replace(" ", "_"),
                                    row["Difficulty"].lower(),
                                    _points_from_text(row["Key points"]),
                                ),
                            ),
                            width="content",
                        )
                        if st.button(
                            "Save to bookmarks",
                            key=f"qbank_save_{row['id']}",
                            icon=":material/bookmark_add:",
                            width="content",
                        ):
                            _save_bookmark(
                                session_id,
                                row["Question"],
                                category=row["Category"].lower().replace(" ", "_"),
                                difficulty=row["Difficulty"].lower(),
                                expected_key_points=_points_from_text(row["Key points"]),
                                source="history",
                            )

    with bookmarks_tab:
        bookmarks = bookmark_rows
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
            selected = next(
                (row for row in bookmarks if int(row["id"]) == options[choice]), None
            )
            with st.container(horizontal=True):
                st.button(
                    "Practice selected",
                    icon=":material/quiz:",
                    key="qbank_practice_bookmark",
                    on_click=_start_practice,
                    args=(
                        _practice_payload(
                            "bookmark",
                            int(selected["id"]) if selected else None,
                            str(selected["Question"]) if selected else "",
                            str(selected["Category"]) if selected else "technical",
                            str(selected["Difficulty"]) if selected else "medium",
                            _points_from_text(selected["Key points"]) if selected else [],
                        ),
                    ),
                    width="content",
                )
                if st.button(
                    "Remove bookmark",
                    icon=":material/bookmark_remove:",
                    key="qbank_delete_bookmark",
                    width="content",
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
        section("Generate interview questions", ":material/auto_awesome:")
        with st.container(border=True):
            st.caption("Choose suggestions or type any custom value.")
            industry_choice = st.selectbox(
                "Industry or sector",
                INDUSTRIES,
                key="qbank_industry",
                index=None,
                accept_new_options=True,
                placeholder="Select or type an industry",
            )
            role_options = (NO_ROLE,) + _catalog_choices(
                ROLE_SUGGESTIONS, industry_choice, GENERAL_ROLES
            )
            role_choice = st.selectbox(
                "Job role or profession",
                role_options,
                key="qbank_role",
                accept_new_options=True,
                placeholder="Select or type a job role",
            )
            topic_options = (NO_TOPIC,) + _catalog_choices(
                TOPIC_SUGGESTIONS, industry_choice, GENERAL_TOPICS
            )
            topic_choice = st.selectbox(
                "Topic or subject",
                topic_options,
                key="qbank_topic",
                accept_new_options=True,
                placeholder="Select or type a topic",
            )
            with st.container(horizontal=True):
                question_type = st.selectbox(
                    "Question type",
                    QUESTION_TYPES,
                    key="qbank_question_type",
                )
                experience_level = st.selectbox(
                    "Experience level",
                    EXPERIENCE_LEVELS,
                    key="qbank_experience_level",
                )
            with st.container(horizontal=True):
                difficulty = (
                    st.segmented_control(
                        "Difficulty",
                        USER_DIFFICULTIES,
                        default="Intermediate",
                        key="qbank_gen_difficulty",
                    )
                    or "Intermediate"
                )
                count = (
                    st.segmented_control(
                        "Questions",
                        QUESTION_COUNTS,
                        default=5,
                        key="qbank_count",
                    )
                    or 5
                )
        if st.button(
            "Generate questions",
            type="primary",
            icon=":material/auto_awesome:",
            key="qbank_generate_questions",
            width="stretch",
        ):
            try:
                clean_count = int(count)
            except (TypeError, ValueError):
                clean_count = 0
            clean_difficulty_label = _text(difficulty)
            if clean_difficulty_label not in DIFFICULTY_MAP:
                clean_difficulty_label = "Intermediate"
            clean_question_type = _text(question_type)
            if clean_question_type not in QUESTION_TYPES:
                clean_question_type = "Mixed"
            clean_experience = _text(experience_level)
            if clean_experience not in EXPERIENCE_LEVELS:
                clean_experience = "Not specified"
            if clean_count not in QUESTION_COUNTS:
                st.error("Choose 5, 10, 15, or 20 questions.", icon=":material/error:")
            else:
                clean_industry = _optional_choice(industry_choice, NO_INDUSTRY)
                clean_role = _optional_choice(role_choice, NO_ROLE)
                clean_topic = _optional_choice(topic_choice, NO_TOPIC)
                focus_query = " ".join(
                    part
                    for part in (
                        clean_industry,
                        clean_role,
                        clean_topic,
                        clean_question_type,
                        "" if clean_experience == "Not specified" else clean_experience,
                    )
                    if part
                ) or "general interview practice"
                with st.spinner("Creating focused questions…"):
                    try:
                        from services.knowledge_base import get_knowledge_base

                        context = get_knowledge_base(
                            st.session_state.user_session_id, actor_user_id
                        ).build_context(f"{focus_query} interview preparation")
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
                            difficulty=DIFFICULTY_MAP[clean_difficulty_label],
                            knowledge_context=f"Focus: {focus_query}. {context}",
                            interview_type="Universal interview practice",
                            industry=clean_industry,
                            target_role=clean_role,
                            topic=clean_topic,
                            question_type=clean_question_type,
                            experience_level=clean_experience,
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
                                    "topic": clean_topic or NO_TOPIC,
                                    "difficulty": clean_difficulty_label,
                                    "notice": "",
                                    "industry": clean_industry or NO_INDUSTRY,
                                    "role": clean_role or NO_ROLE,
                                    "question_type": clean_question_type,
                                    "experience_level": clean_experience,
                                }
                            )
        if st.button(
            "Clear generated questions",
            icon=":material/delete:",
            key="qbank_clear_generated",
            disabled=not state["questions"] and not state["notice"],
            width="stretch",
        ):
            state.update(
                {
                    "questions": [],
                    "topic": "",
                    "difficulty": "",
                    "notice": "",
                    "industry": "",
                    "role": "",
                    "question_type": "",
                    "experience_level": "",
                }
            )
            st.rerun()

        if state["notice"]:
            if state["questions"]:
                st.warning(f"{state['notice']} Previous results are still shown below.")
            else:
                error_state("Question generation failed.", state["notice"])
        if not state["questions"]:
            empty_state(
                "No generated questions yet.",
                "Choose an industry, role, topic, and question type, then generate a batch.",
            )
        else:
            st.caption(
                " · ".join(
                    (
                        f"Industry: {state['industry'] or 'any'}",
                        f"Role: {state['role'] or 'general'}",
                        f"Topic: {state['topic'] or 'general'}",
                        f"Type: {state['question_type'] or 'Mixed'}",
                        f"Experience: {state['experience_level'] or 'Not specified'}",
                        f"Difficulty: {state['difficulty'] or 'Intermediate'}",
                        f"{len(state['questions'])} questions",
                    )
                )
            )
            for question in state["questions"]:
                question_text = str(question["question_text"])
                category = str(question.get("category") or "technical")
                difficulty = str(question.get("difficulty") or "medium")
                with st.expander(f"Q{question['number']}: {question_text[:90]}"):
                    st.write(question_text)
                    if question["expected_key_points"]:
                        st.caption(
                            "Key points: " + ", ".join(question["expected_key_points"])
                        )
                    with st.container(horizontal=True):
                        st.button(
                            "Practice",
                            icon=":material/quiz:",
                            key=f"qbank_practice_generated_{question['number']}",
                            on_click=_start_practice,
                            args=(
                                _practice_payload(
                                    "generated",
                                    int(question["number"]),
                                    question_text,
                                    category,
                                    difficulty,
                                    list(question["expected_key_points"]),
                                ),
                            ),
                            width="content",
                        )
                        if st.button(
                            "Save to bookmarks",
                            icon=":material/bookmark_add:",
                            key=f"qbank_gen_save_{question['number']}",
                            width="content",
                        ):
                            _save_bookmark(
                                session_id,
                                question_text,
                                category=category,
                                difficulty=difficulty,
                                expected_key_points=list(question["expected_key_points"]),
                                source="generated",
                            )


show_question_bank()
