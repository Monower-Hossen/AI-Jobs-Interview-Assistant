"""Interview history with owner-scoped search, filters, comparison and export."""

import json

import streamlit as st
from services import interview_service
from services.auth_service import AuthError

from frontend.shared.components import (
    data_table,
    empty_state,
    error_state,
    kpi_row,
    page_header,
    section,
)
from frontend.shared.navigation import go_to
from frontend.user.interview_page import _ensure_user_session


def _status_options() -> list[str]:
    return ["all", "completed", "in_progress", "abandoned", "created"]


def show_history_page() -> None:
    page_header(
        "Interview history", "Browse, compare and export your past sessions", ":material/history:"
    )
    _ensure_user_session()
    token = st.session_state.user_session_id
    actor_user_id = st.session_state.get("user_id")
    all_rows = interview_service.get_interview_history(token, actor_user_id, limit=100)
    if not all_rows:
        empty_state("No interview sessions yet.", "Your completed interviews will appear here.")
        if st.button("Start an interview", type="primary", width="stretch"):
            go_to("interview_setup")
        return

    types = ["all"] + sorted({row["Type"] for row in all_rows})
    with st.container(border=True):
        st.markdown("**Filters**")
        search_col, type_col, status_col = st.columns(3)
        with search_col:
            search = st.text_input("Search", key="history_search", width="stretch")
        with type_col:
            selected_type = st.selectbox("Interview type", types, key="history_type")
        with status_col:
            selected_status = st.segmented_control(
                "Status", _status_options(), default="all", key="history_status", width="stretch"
            )
        date_col, score_col = st.columns(2)
        with date_col:
            selected_dates = st.date_input("Date range", key="history_dates")
        with score_col:
            minimum_score = st.slider(
                "Minimum score",
                0.0,
                10.0,
                0.0,
                0.5,
                key="history_min_score",
            )
        date_from = selected_dates[0] if isinstance(selected_dates, (list, tuple)) and selected_dates else None
        date_to = selected_dates[1] if isinstance(selected_dates, (list, tuple)) and len(selected_dates) == 2 else None

    history = interview_service.get_interview_history(
        token,
        actor_user_id,
        search=search,
        interview_type=str(selected_type),
        status=str(selected_status),
        minimum_score=float(minimum_score),
        date_from=date_from,
        date_to=date_to,
        limit=100,
    )
    st.caption(f"Showing {len(history)} of {len(all_rows)} sessions")

    selected_id = st.session_state.get("history_selected_id")
    if selected_id is not None and selected_id not in {row["id"] for row in history}:
        st.session_state.pop("history_selected_id", None)
        selected_id = None

    if not history:
        empty_state("No sessions match these filters.", "Adjust the filters or start a new interview.")
        return

    table_rows = [
        {
            "Session": row["Label"],
            "Type": row["Type"],
            "Difficulty": row["Difficulty"],
            "Score": f"{row['Score']:.1f}/10" if row["Score"] is not None else "—",
            "Status": row["Status"],
            "Date": row["Date"],
            "Duration": row["Duration"],
        }
        for row in history
    ]
    data_table(table_rows, "No matching sessions.", alt="Filtered interview sessions")

    scored = [row for row in history if row["Score"] is not None]
    if len(scored) >= 2:
        section("Previous session comparison", ":material/compare_arrows:")
        latest, previous = scored[0], scored[1]
        kpi_row(
            [
                (
                    "Latest overall score",
                    f"{latest['Score']:.1f}/10",
                    f"{latest['Score'] - previous['Score']:+.1f} vs previous",
                )
            ]
        )

    options = {row["Label"]: row["id"] for row in history}
    choice = st.selectbox("Inspect session", list(options), width="stretch")
    if st.button("Open details", type="primary", width="stretch"):
        st.session_state.history_selected_id = options[choice]
        selected_id = options[choice]

    if selected_id:
        _render_details(int(selected_id), token, actor_user_id)


def _render_details(interview_id: int, token: str, actor_user_id: int | None) -> None:
    try:
        detail = interview_service.get_interview_detail(
            interview_id=interview_id, session_token=token, actor_user_id=actor_user_id
        )
    except AuthError as exc:
        error_state("Session not accessible", str(exc))
        return
    session = detail.session
    section(f"Session #{interview_id} details", ":material/article:")

    duration = session["duration_seconds"]
    kpi_row(
        [
            (
                "Overall score",
                f"{session['overall_score']:.1f}/10"
                if session["overall_score"] is not None
                else "—",
                None,
            ),
            ("Status", session["status"].replace("_", " ").title(), None),
            ("Duration", f"{duration // 60}m {duration % 60}s", None),
            ("Questions", len(detail.questions), None),
        ]
    )
    st.caption(
        f"{session['type']} · {session['difficulty'].title()} · "
        f"started {session['started_at'] or 'unknown'} · completed {session['completed_at'] or '—'}"
    )

    if not detail.evaluation_available:
        empty_state(
            "Evaluation is not available yet",
            "Answers are saved. Scores appear after the AI review finishes.",
        )

    if session["strengths"]:
        st.markdown("**Strengths:** " + ", ".join(session["strengths"][:5]))
    if session["weaknesses"]:
        st.markdown("**Improvement areas:** " + ", ".join(session["weaknesses"][:5]))

    for question in detail.questions:
        label = f"Q{question['number']} · {question['category'].replace('_', ' ').title()}"
        with st.expander(label):
            st.markdown("**Question**")
            st.write(question["question"])
            st.markdown("**Your answer**")
            st.write(question["answer"] or "Not answered")
            if question["answer_mode"] == "voice":
                st.caption("Answer captured by voice.")
            if question["score"] is not None:
                st.metric("Score", f"{question['score']:.1f}/10", width="stretch")
            evaluation = question["evaluation"]
            if evaluation is None:
                st.caption("No evaluation data recorded for this answer.")
                continue
            st.markdown("**Strengths:** " + ", ".join(evaluation.strengths[:5]))
            st.markdown("**Improve:** " + ", ".join(evaluation.weaknesses[:5]))
            if evaluation.missing_points:
                st.markdown("**Missing points:** " + ", ".join(evaluation.missing_points[:5]))
            if evaluation.suggested_answer:
                st.markdown("**Better answer**")
                st.write(evaluation.suggested_answer)

    payload = {
        "session": session,
        "answers": [
            {
                "number": question["number"],
                "question": question["question"],
                "answer": question["answer"],
                "score": question["score"],
                "feedback": (
                    question["evaluation"].suggested_answer if question["evaluation"] else None
                ),
            }
            for question in detail.questions
        ],
    }
    back_col, report_col = st.columns(2)
    with back_col:
        if st.button("Back to history", icon=":material/arrow_back:", width="stretch"):
            st.session_state.pop("history_selected_id", None)
            st.rerun()
    with report_col:
        if session["status"] == "completed":
            if st.button("View full report", icon=":material/assessment:", width="stretch"):
                go_to("report")
        elif st.button("Continue interview", icon=":material/play_arrow:", width="stretch"):
            st.session_state.interview_session_id = interview_id
            st.session_state.interview_started = False
            go_to("interview_setup")

    st.download_button(
        "Export session JSON",
        data=json.dumps(payload, indent=2, default=str),
        file_name=f"interview_session_{interview_id}.json",
        mime="application/json",
        icon=":material/download:",
        width="stretch",
    )
