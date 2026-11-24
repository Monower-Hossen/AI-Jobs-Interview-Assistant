from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

import streamlit as st
from services import job_interview_service, live_interview_coordination_service

from frontend.shared.components import data_table, empty_state, error_state, page_header, section
from frontend.shared.navigation import go_to
from frontend.user.history_page import render_legacy_history
from frontend.user.interview_page import _ensure_user_session
from frontend.user.live_recording_history import show_live_recording_history

JOB_REPORT_KEY = "_job_interview_report_id"
LIVE_REPORT_KEY = "live_webcam_report_id"

_VOICE_MODE = "voice"
_AVATAR_MODE = "avatar"
_VIDEO_MODE = "video"


def _identity() -> tuple[int, str] | None:
    raw_user_id = st.session_state.get("user_id")
    raw_token = st.session_state.get("user_session_id") or st.session_state.get("session_token")
    if raw_user_id is None:
        return None
    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        return None
    token = str(raw_token) if raw_token else ""
    if not token:
        return None
    return user_id, token


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _format_when(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    text = str(value or "").strip()
    return text or "—"


def _score_text(value: Any) -> str:
    if value is None:
        return "Unavailable"
    try:
        return f"{float(value):.1f}/10"
    except (TypeError, ValueError):
        return "Unavailable"


def _status_text(value: Any) -> str:
    return str(value or "unknown").replace("_", " ").title()


def _date_bounds(value: Any) -> tuple[date | None, date | None]:
    if isinstance(value, (list, tuple)) and value:
        date_from = _as_date(value[0])
        date_to = _as_date(value[1]) if len(value) > 1 else None
        return date_from, date_to
    return None, None


def _apply_filters(
    rows: list[dict[str, Any]],
    *,
    search_fields: tuple[str, ...],
    date_field: str,
    status_field: str = "status",
    score_field: str | None = None,
    search: str = "",
    status: str = "all",
    date_from: date | None = None,
    date_to: date | None = None,
    minimum_score: float = 0.0,
) -> list[dict[str, Any]]:
    needle = str(search or "").strip().lower()
    wanted_status = str(status or "all").strip().lower().replace("_", " ")
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if needle:
            haystack = " ".join(str(row.get(field) or "") for field in search_fields).lower()
            if needle not in haystack:
                continue
        if wanted_status != "all":
            actual = str(row.get(status_field) or "").strip().lower().replace("_", " ")
            if actual != wanted_status:
                continue
        created = _as_date(row.get(date_field))
        if date_from and (created is None or created < date_from):
            continue
        if date_to and (created is None or created > date_to):
            continue
        if minimum_score > 0:
            raw_score = row.get(score_field) if score_field else None
            if raw_score is None:
                continue
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                continue
            if score < minimum_score:
                continue
        filtered.append(row)
    return filtered


def _filter_controls(
    prefix: str, *, statuses: list[str], include_score: bool = True
) -> dict[str, Any]:
    options = ["all", *statuses]
    with st.container(border=True):
        st.markdown("**Filters**")
        if include_score:
            search_col, status_col, date_col, score_col = st.columns([2, 1, 1, 1])
        else:
            search_col, status_col, date_col = st.columns([2, 1, 1])
            score_col = None
        search = search_col.text_input("Search", key=f"{prefix}_search", width="stretch")
        status = status_col.selectbox("Status", options, key=f"{prefix}_status")
        selected_dates = date_col.date_input("Date range", key=f"{prefix}_dates")
        minimum_score = 0.0
        if score_col is not None:
            minimum_score = float(
                score_col.slider(
                    "Minimum score",
                    0.0,
                    10.0,
                    0.0,
                    0.5,
                    key=f"{prefix}_min_score",
                )
            )
        date_from, date_to = _date_bounds(selected_dates)
    return {
        "search": search,
        "status": str(status),
        "date_from": date_from,
        "date_to": date_to,
        "minimum_score": minimum_score,
    }


def _counterpart(room: Mapping[str, Any]) -> str:
    key = "admin_name" if str(room.get("role")) == "candidate" else "candidate_name"
    return str(room.get(key) or "Participant unavailable")


def _report_state(
    row: Mapping[str, Any],
) -> str:
    status = str(row.get("status") or "")
    if status != "completed":
        return "Pending"
    return "Available" if row.get("overall_score") is not None else "Unavailable"


def _render_job_records(
    rows: list[dict[str, Any]],
    *,
    prefix: str,
    empty_title: str,
    empty_detail: str,
    report_key: str,
    report_target: str,
    report_label: str,
) -> None:
    if not rows:
        empty_state(empty_title, empty_detail)
        return
    statuses = sorted({str(row.get("status") or "unknown") for row in rows})
    filters = _filter_controls(prefix, statuses=statuses)
    filtered = _apply_filters(
        rows,
        search_fields=("job_title", "company", "status"),
        date_field="created_at",
        score_field="overall_score",
        **filters,
    )
    st.caption(f"Showing {len(filtered)} of {len(rows)} sessions")
    if not filtered:
        empty_state("No sessions match these filters.", "Adjust the filters or clear the search.")
        return
    table_rows = [
        {
            "Interview": f"{row.get('job_title') or 'Job interview'} · "
            f"{row.get('company') or 'Company unavailable'}",
            "Status": _status_text(row.get("status")),
            "Score": _score_text(row.get("overall_score")),
            "Created": _format_when(row.get("created_at")),
            "Completed": _format_when(row.get("completed_at")),
            "Report": _report_state(row),
        }
        for row in filtered
    ]
    data_table(table_rows, "No matching sessions.", alt=f"{prefix} history records")
    options = {
        f"Interview #{int(row['id'])} · {row.get('job_title') or 'Job interview'}": int(row["id"])
        for row in filtered
    }
    choice = st.selectbox("Select a session", list(options), key=f"{prefix}_select")
    selected = filtered[list(options).index(choice)]
    ready = _report_state(selected) == "Available"
    if not ready:
        st.caption("A report appears here after the interview is completed and scored.")
    if st.button(
        report_label,
        type="primary",
        width="stretch",
        key=f"{prefix}_open_report",
        disabled=not ready,
    ):
        st.session_state[report_key] = options[choice]
        go_to(report_target)


def _load_job_rows(user_id: int, token: str) -> list[dict[str, Any]]:
    try:
        rows = job_interview_service.list_job_interviews(
            actor_user_id=user_id, session_token=token
        )
    except Exception as exc:
        error_state("Saved interviews could not be loaded.", str(exc)[:200])
        return []
    return rows


def _render_job_interview_section(user_id: int, token: str) -> None:
    section("Job Interviews", ":material/videocam:")
    modes = {_VOICE_MODE, _AVATAR_MODE}
    rows = [row for row in _load_job_rows(user_id, token) if row.get("mode") in modes]
    _render_job_records(
        rows,
        prefix="unified_job",
        empty_title="No Job Interview sessions yet.",
        empty_detail="Complete a Job Interview to see it here.",
        report_key=JOB_REPORT_KEY,
        report_target="job_interview",
        report_label="Open Job Interview report",
    )
    section("Earlier practice sessions", ":material/history:")
    render_legacy_history()


def _render_live_webcam_section(user_id: int, token: str) -> None:
    section("Completed and saved sessions", ":material/videocam:")
    rows = [row for row in _load_job_rows(user_id, token) if row.get("mode") == _VIDEO_MODE]
    _render_job_records(
        rows,
        prefix="unified_video",
        empty_title="No Live Webcam sessions yet.",
        empty_detail="Sessions appear here after you start a Live Webcam Interview.",
        report_key=LIVE_REPORT_KEY,
        report_target="live_webcam_interview",
        report_label="Open Live Webcam report",
    )


def _render_live_sessions_section(user_id: int, token: str) -> None:
    section("Candidate live interviews", ":material/calendar_month:")
    try:
        rooms = live_interview_coordination_service.list_invitations(
            user_id=user_id, session_token=token, limit=50
        )
    except Exception as exc:
        error_state("Live interview sessions could not be loaded.", str(exc)[:200])
        return
    if not rooms:
        empty_state(
            "No live interview sessions yet.",
            "Invitations created for your account appear here.",
        )
        return
    statuses = sorted({str(room.get("status") or "unknown") for room in rooms})
    filters = _filter_controls("unified_sessions", statuses=statuses, include_score=False)
    filtered = _apply_filters(
        rooms,
        search_fields=("title", "admin_name", "candidate_name", "status"),
        date_field="created_at",
        **filters,
    )
    st.caption(f"Showing {len(filtered)} of {len(rooms)} sessions")
    if not filtered:
        empty_state("No sessions match these filters.", "Adjust the filters or clear the search.")
        return
    table_rows = [
        {
            "Session": str(room.get("title") or "Live interview"),
            "Status": _status_text(room.get("status")),
            "Your role": _status_text(room.get("role")),
            "Other participant": _counterpart(room),
            "Created": _format_when(room.get("created_at")),
        }
        for room in filtered
    ]
    data_table(table_rows, "No matching sessions.", alt="Live interview sessions")
    options = {
        f"{room.get('title') or 'Live interview'} · {_status_text(room.get('status'))}": index
        for index, room in enumerate(filtered)
    }
    st.selectbox("Select a live session", list(options), key="unified_sessions_select")
    if st.button(
        "Open live interview details",
        type="primary",
        width="stretch",
        key="unified_sessions_open",
    ):
        go_to("live_interview_invitations")


def _render_recordings_section() -> None:
    section("Saved live recordings", ":material/video_file:")
    try:
        show_live_recording_history(show_header=False)
    except Exception as exc:
        error_state("Recordings could not be loaded.", str(exc)[:200])


def show_unified_interview_history() -> None:
    page_header(
        "Interview history",
        "Job interviews, Live Webcam sessions, live interviews, and recordings in one place.",
        ":material/history:",
    )
    try:
        _ensure_user_session()
    except Exception as exc:
        error_state("Workspace access unavailable", str(exc)[:200])
        return
    identity = _identity()
    if identity is None:
        error_state("Please sign in to view your interview history.")
        return
    user_id, token = identity
    tabs = st.tabs(
        ["Job Interview", "Live Webcam", "Live Sessions", "Recordings"],
    )
    with tabs[0]:
        _render_job_interview_section(user_id, token)
    with tabs[1]:
        _render_live_webcam_section(user_id, token)
    with tabs[2]:
        _render_live_sessions_section(user_id, token)
    with tabs[3]:
        _render_recordings_section()
