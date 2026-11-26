"""Admin dashboard overview page."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from database import InterviewCallRoom, InterviewSession, JobPost, get_readonly_db
from services import live_call_service as calls
from services.auth_service import get_user_statistics, require_admin
from services.registration_service import (
    get_registration_request_counts,
    list_registration_requests,
)
from sqlalchemy import func

from frontend.shared.components import empty_state, error_state

JOB_READY_THRESHOLD = 6.5
WEEK_COUNT = 8


@dataclass(frozen=True)
class AdminOverview:
    total_users: int
    active_users: int
    published_jobs: int
    draft_jobs: int
    total_interviews: int
    completed_interviews: int
    average_score: float | None
    pass_rate: float | None
    improvement_required: int
    weekly_trend: pd.DataFrame
    registration_counts: dict[str, int]
    live_counts: dict[str, int]


@st.fragment(run_every="30s")
def _notify_new_registration_requests(admin_id: int) -> None:
    pending = list_registration_requests(actor_admin_id=admin_id, status="pending")
    seen_key = f"seen_registration_requests_{admin_id}"
    current_ids = [int(request.id) for request in pending]
    seen_ids = {int(value) for value in st.session_state.get(seen_key, [])}

    if seen_key in st.session_state:
        for request in pending:
            if request.id not in seen_ids:
                st.toast(
                    f"New registration request from {request.full_name} ({request.email})",
                    icon=":material/person_add:",
                )
    st.session_state[seen_key] = current_ids


def _format_score(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def _format_rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _build_weekly_trend(
    rows: Sequence[tuple[datetime | None, str | None, float | None]],
) -> pd.DataFrame:
    today = datetime.now().date()
    current_monday = today - timedelta(days=today.weekday())
    weeks = [current_monday - timedelta(weeks=offset) for offset in range(WEEK_COUNT - 1, -1, -1)]
    totals = [0] * WEEK_COUNT
    score_sums = [0.0] * WEEK_COUNT
    score_counts = [0] * WEEK_COUNT

    for created_at, status, score in rows:
        if created_at is None:
            continue
        created_date = created_at.date() if isinstance(created_at, datetime) else created_at
        week_start = created_date - timedelta(days=created_date.weekday())
        if week_start < weeks[0] or week_start > weeks[-1]:
            continue
        index = (week_start - weeks[0]).days // 7
        totals[index] += 1
        if status == "completed" and score is not None:
            score_sums[index] += float(score)
            score_counts[index] += 1

    averages = [
        round(score_sums[index] / score_counts[index], 2) if score_counts[index] else 0.0
        for index in range(WEEK_COUNT)
    ]
    return pd.DataFrame(
        {
            "Week": [week.strftime("%d %b") for week in weeks],
            "Interviews": totals,
            "Average score": averages,
        }
    )


def _effective_live_status(status: str | None, expires_at: datetime | None, now: datetime) -> str:
    value = str(status or "")
    if value in {calls.INVITED, calls.ACCEPTED} and expires_at is not None and expires_at < now:
        return calls.EXPIRED
    return value


def _load_overview(admin_id: int) -> AdminOverview:
    user_stats = get_user_statistics(actor_admin_id=admin_id)
    registration_counts = get_registration_request_counts(actor_admin_id=admin_id)
    now = datetime.now()

    with get_readonly_db() as db:
        total_jobs = int(db.query(func.count(JobPost.id)).scalar() or 0)
        published_jobs = int(
            db.query(func.count(JobPost.id)).filter(JobPost.is_published.is_(True)).scalar() or 0
        )
        interview_rows = db.query(
            InterviewSession.created_at,
            InterviewSession.status,
            InterviewSession.overall_score,
        ).all()
        live_rows = db.query(
            InterviewCallRoom.status,
            InterviewCallRoom.expires_at,
        ).all()

    completed_rows = [row for row in interview_rows if row.status == "completed"]
    scored = [float(row.overall_score) for row in completed_rows if row.overall_score is not None]
    average_score = round(sum(scored) / len(scored), 2) if scored else None
    passed = sum(1 for score in scored if score >= JOB_READY_THRESHOLD)
    pass_rate = round(passed / len(scored) * 100, 1) if scored else None
    improvement_required = len(scored) - passed

    live_statuses = [_effective_live_status(row.status, row.expires_at, now) for row in live_rows]
    live_counts = {
        "pending": live_statuses.count(calls.INVITED),
        "active": sum(
            1 for status in live_statuses if status in {calls.ACCEPTED, calls.IN_PROGRESS}
        ),
        "completed": live_statuses.count(calls.COMPLETED),
        "cancelled": live_statuses.count(calls.CANCELLED),
    }

    return AdminOverview(
        total_users=int(user_stats.get("total", 0)),
        active_users=int(user_stats.get("active", 0)),
        published_jobs=published_jobs,
        draft_jobs=total_jobs - published_jobs,
        total_interviews=len(interview_rows),
        completed_interviews=len(completed_rows),
        average_score=average_score,
        pass_rate=pass_rate,
        improvement_required=improvement_required,
        weekly_trend=_build_weekly_trend(interview_rows),
        registration_counts=registration_counts,
        live_counts=live_counts,
    )


def _metric_card(label: str, value: str | float, description: str, icon: str) -> None:
    with st.container(border=True, height="stretch"):
        st.markdown(icon)
        st.metric(label, value)
        st.caption(description)


def _render_statistics(overview: AdminOverview) -> None:
    st.subheader("Overview", icon=":material/query_stats:")
    columns = st.columns(4, vertical_alignment="center")
    pending = overview.registration_counts.get("pending", 0)
    with columns[0]:
        _metric_card("Total users", overview.total_users, "Registered accounts", ":material/group:")
    with columns[1]:
        _metric_card(
            "Active jobs",
            overview.published_jobs,
            "Published job postings",
            ":material/work:",
        )
    with columns[2]:
        _metric_card(
            "Total interviews",
            overview.total_interviews,
            "All recorded sessions",
            ":material/analytics:",
        )
    with columns[3]:
        _metric_card(
            "Pending approvals",
            pending,
            "Requests awaiting review",
            ":material/how_to_reg:",
        )


def _render_registration_overview(overview: AdminOverview) -> None:
    counts = overview.registration_counts
    with st.container(border=True):
        st.subheader("Registration requests", icon=":material/how_to_reg:")
        columns = st.columns(3, vertical_alignment="center")
        with columns[0]:
            st.metric("Total pending requests", counts.get("pending", 0))
        with columns[1]:
            st.metric("Approved registrations", counts.get("approved", 0))
        with columns[2]:
            st.metric("Rejected registrations", counts.get("rejected", 0))
        st.page_link(
            "frontend/admin/users.py",
            label="Review registration requests",
            icon=":material/fact_check:",
            width="stretch",
            query_params={"admin_users_view": "Registration Requests"},
        )


def _render_interview_performance(overview: AdminOverview) -> None:
    with st.container(border=True):
        st.subheader("Interview performance", icon=":material/analytics:")
        columns = st.columns(4, vertical_alignment="center")
        with columns[0]:
            st.metric("Total completed interviews", overview.completed_interviews)
        with columns[1]:
            st.metric("Average interview score", _format_score(overview.average_score))
        with columns[2]:
            st.metric("Pass rate", _format_rate(overview.pass_rate))
        with columns[3]:
            st.metric("Improvement required", overview.improvement_required)

        if overview.total_interviews == 0:
            empty_state(
                "No interview activity yet",
                "Weekly volume and score trends appear once interviews are recorded.",
            )
        else:
            chart_data = overview.weekly_trend.set_index("Week")
            st.line_chart(
                chart_data,
                y=["Interviews", "Average score"],
                alt="Weekly interview volume and average score trend",
            )
        st.caption(
            f"Average score and pass rate use completed interviews with recorded scores. "
            f"Job-ready threshold: {JOB_READY_THRESHOLD:g}."
        )


def _render_job_overview(overview: AdminOverview) -> None:
    total_jobs = overview.published_jobs + overview.draft_jobs
    with st.container(border=True):
        st.subheader("Job posting overview", icon=":material/work:")
        columns = st.columns(3, vertical_alignment="center")
        with columns[0]:
            st.metric("Published jobs", overview.published_jobs)
        with columns[1]:
            st.metric("Draft jobs", overview.draft_jobs)
        with columns[2]:
            st.metric("Expired jobs", "—")
        if total_jobs:
            st.caption(f"{overview.published_jobs} of {total_jobs} job postings are published.")
        else:
            st.caption("No job postings yet.")
        st.caption("Job expiry is not tracked in current records, so no expired count is reported.")


def _render_live_overview(overview: AdminOverview) -> None:
    counts = overview.live_counts
    with st.container(border=True):
        st.subheader("Live interview overview", icon=":material/videocam:")
        columns = st.columns(4, vertical_alignment="center")
        with columns[0]:
            st.metric("Pending invitations", counts.get("pending", 0))
        with columns[1]:
            st.metric("Active interviews", counts.get("active", 0))
        with columns[2]:
            st.metric("Completed interviews", counts.get("completed", 0))
        with columns[3]:
            st.metric("Cancelled invitations", counts.get("cancelled", 0))
        st.page_link(
            "frontend/admin/live_interview_invitations.py",
            label="Open live interview invitations",
            icon=":material/arrow_forward:",
            width="stretch",
        )


def _render_quick_actions() -> None:
    st.subheader("Quick actions", icon=":material/bolt:")
    actions = (
        ("Manage users", "frontend/admin/users.py", ":material/group:"),
        ("Create job posting", "frontend/admin/jobs.py", ":material/work:"),
        (
            "Live interview invitations",
            "frontend/admin/live_interview_invitations.py",
            ":material/videocam:",
        ),
        ("View reports", "frontend/admin/reports.py", ":material/summarize:"),
    )
    with st.container(horizontal=True, wrap=True):
        for label, page, icon in actions:
            with st.container(border=True):
                st.page_link(page, label=label, icon=icon, width="stretch")


def show_admin_dashboard() -> None:
    st.title("Dashboard Overview", icon=":material/space_dashboard:")
    st.caption("Welcome back, Administrator")
    st.caption(f"Today · {datetime.now().strftime('%A, %d %B %Y')}")

    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        error_state("Administrator access required.")
        st.stop()
    require_admin(account.id)

    _notify_new_registration_requests(account.id)

    with st.spinner("Loading dashboard overview…"):
        try:
            overview = _load_overview(account.id)
        except Exception as exc:
            error_state("The dashboard overview could not be loaded.", str(exc)[:200])
            st.stop()

    _render_statistics(overview)
    _render_registration_overview(overview)
    _render_interview_performance(overview)
    _render_job_overview(overview)
    _render_live_overview(overview)
    _render_quick_actions()


show_admin_dashboard()
