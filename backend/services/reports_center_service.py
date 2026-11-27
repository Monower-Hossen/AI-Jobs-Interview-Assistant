from __future__ import annotations

import json
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from config.settings import runtime_path
from database import (
    AdminReportHistory,
    EventLog,
    GeneratedReport,
    InterviewCallRoom,
    InterviewSession,
    JobPost,
    JobSkill,
    RegistrationRequest,
    User,
    UserSession,
    get_db_context,
    get_readonly_db,
)
from services.analytics import get_platform_report
from services.auth_service import log_event, require_admin
from services.live_call_service import CALL_STATES
from sqlalchemy import func
from utils.file_utils import sanitize_filename

CATEGORIES = (
    "User Reports",
    "Interview Reports",
    "Performance Reports",
    "Job Reports",
    "Live Interview Reports",
    "System Activity Reports",
)

REPORT_VIEWS: dict[str, tuple[str, ...]] = {
    "User Reports": (
        "Overall User Report",
        "Individual User Report",
        "User Registration Report",
        "User Engagement Report",
    ),
    "Interview Reports": (
        "Interview History",
        "Interview Type Statistics",
        "Completion Overview",
    ),
    "Performance Reports": (
        "Performance Overview",
        "User Performance Comparison",
        "Performance Trends",
    ),
    "Job Reports": ("Job Posting Overview", "Job Posting Trends", "Popular Categories"),
    "Live Interview Reports": ("Invitation Overview", "Session Overview", "Live History"),
    "System Activity Reports": ("Activity Timeline", "Admin Actions", "Registration History"),
}

_EXPORT_FORMATS = ("csv", "pdf", "xlsx", "json")
_LIVE_EVENT_STATUSES = {
    "live_call.invited": "invited",
    "live_call.accepted": "accepted",
    "live_call.in_progress": "in_progress",
    "live_call.completed": "completed",
    "live_call.cancelled": "cancelled",
    "live_call.declined": "declined",
    "live_call.expired": "expired",
    "live_call.interrupted": "interrupted",
}
_ACTIVITY_GROUPS = {
    "registration": (
        "registration.request_submitted",
        "registration.request_approved",
        "registration.request_rejected",
        "registration.account_created",
        "auth.register",
    ),
    "login": ("auth.login", "auth.login_failed", "auth.logout", "auth.session_start"),
    "profile": ("user.profile_update", "auth.password_change"),
    "admin": ("admin.create_user", "admin.role_change", "admin.status_change"),
    "invitation": (
        "live_call.invited",
        "live_call.accepted",
        "live_call.in_progress",
        "live_call.completed",
        "live_call.cancelled",
        "live_call.declined",
        "live_call.expired",
        "live_call.interrupted",
        "live_call.invitation_deleted",
    ),
    "status": ("admin.status_change", "admin.role_change"),
    "approval": ("registration.request_approved",),
    "rejection": ("registration.request_rejected",),
}
_SECRET_PATTERN = re.compile(
    r"(?i)(token|password|secret|api[_-]?key)\s*[=:]\s*([^\s,;]+)"
)
_ROOM_ID_PATTERN = re.compile(r"lc-[A-Za-z0-9_-]+")


class ReportError(ValueError):
    pass


@dataclass(frozen=True)
class ReportChart:
    title: str
    kind: str
    data: list[dict[str, Any]]
    x: str
    y: str
    alt: str


@dataclass
class ReportResult:
    title: str
    category: str
    generated_at: datetime
    start_date: date | None
    end_date: date | None
    filters: dict[str, Any]
    summary: list[tuple[str, str]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    charts: list[ReportChart] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _display(value: Any) -> str:
    if value is None or value == "":
        return "Not recorded"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "Not recorded"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _count(counts: Counter[str], key: str) -> int:
    return int(counts.get(key, 0))


def _percentage(part: int, total: int) -> str:
    return f"{(part / total * 100):.1f}%" if total else "0.0%"


def _date_chart(
    rows: list[dict[str, Any]], date_key: str, title: str, alt: str
) -> ReportChart | None:
    counts: Counter[str] = Counter()
    for row in rows:
        value = str(row.get(date_key) or "")[:10]
        if value:
            counts[value] += 1
    if not counts:
        return None
    data = [{"Period": key, "Count": counts[key]} for key in sorted(counts)]
    return ReportChart(title, "line", data, "Period", "Count", alt)


def _category_chart(
    rows: list[dict[str, Any]], key: str, title: str, alt: str, limit: int = 12
) -> ReportChart | None:
    counts: Counter[str] = Counter()
    for row in rows:
        value = str(row.get(key) or "Not recorded")
        counts[value] += 1
    if not counts:
        return None
    data = [
        {"Category": name, "Count": count}
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]
    return ReportChart(title, "bar", data, "Category", "Count", alt)


def _score_bands(scores: list[float]) -> ReportChart | None:
    if not scores:
        return None
    bands: Counter[str] = Counter()
    for score in scores:
        bands["0-4" if score < 4 else "4-6" if score < 6 else "6-8" if score < 8 else "8-10"] += 1
    data = [{"Score band": band, "Interviews": bands[band]} for band in ("0-4", "4-6", "6-8", "8-10")]
    return ReportChart(
        "Score distribution",
        "bar",
        data,
        "Score band",
        "Interviews",
        "Interview counts by stored score band",
    )


def _validate(
    actor_admin_id: int | None,
    category: str,
    start_date: date | None,
    end_date: date | None,
    filters: dict[str, Any],
) -> dict[str, Any]:
    require_admin(actor_admin_id)
    if category not in CATEGORIES:
        raise ReportError("Unsupported report category.")
    if start_date and end_date and start_date > end_date:
        raise ReportError("Start date must be on or before end date.")
    normalized = dict(filters or {})
    view = str(normalized.get("view") or REPORT_VIEWS[category][0])
    if view not in REPORT_VIEWS[category]:
        normalized["view"] = REPORT_VIEWS[category][0]
    else:
        normalized["view"] = view
    normalized["user_id"] = _as_int(normalized.get("user_id"))
    normalized["job_status"] = str(normalized.get("job_status") or "all").strip().lower()
    normalized["status"] = str(normalized.get("status") or "all").strip().lower()
    normalized["invitation_status"] = (
        str(normalized.get("invitation_status") or "all").strip().lower()
    )
    normalized["interview_type"] = str(normalized.get("interview_type") or "all").strip()
    normalized["job_category"] = str(normalized.get("job_category") or "all").strip()
    normalized["activity_type"] = str(normalized.get("activity_type") or "all").strip().lower()
    normalized["min_score"] = _as_float(normalized.get("min_score"))
    normalized["max_score"] = _as_float(normalized.get("max_score"))
    if normalized["min_score"] is not None and normalized["max_score"] is not None:
        if normalized["min_score"] > normalized["max_score"]:
            raise ReportError("Minimum score must be less than or equal to maximum score.")
    if category == "User Reports" and view == "Individual User Report" and not normalized["user_id"]:
        raise ReportError("Select an individual user for this report view.")
    return normalized


def _apply_date(query: Any, column: Any, start_date: date | None, end_date: date | None) -> Any:
    if start_date is not None:
        query = query.filter(func.date(column) >= start_date)
    if end_date is not None:
        query = query.filter(func.date(column) <= end_date)
    return query


def _user_report(
    actor_admin_id: int,
    start_date: date | None,
    end_date: date | None,
    filters: dict[str, Any],
) -> ReportResult:
    with get_readonly_db() as db:
        query = _apply_date(db.query(User), User.created_at, start_date, end_date)
        if filters["user_id"] is not None:
            query = query.filter(User.id == filters["user_id"])
        if filters["status"] not in {"", "all"}:
            query = query.filter(User.account_status == filters["status"])
        users = query.order_by(User.created_at.desc()).all()
        user_ids = [int(user.id) for user in users]
        session_ids = [
            row[0]
            for row in db.query(UserSession.id)
            .filter(UserSession.user_id.in_(user_ids or [-1]))
            .all()
        ]
        interview_counts = dict(
            db.query(UserSession.user_id, func.count(InterviewSession.id))
            .join(InterviewSession, InterviewSession.session_id == UserSession.id)
            .filter(UserSession.user_id.in_(user_ids or [-1]))
            .group_by(UserSession.user_id)
            .all()
        )
        report_counts = dict(
            db.query(UserSession.user_id, func.count(GeneratedReport.id))
            .join(GeneratedReport, GeneratedReport.session_id == UserSession.id)
            .filter(UserSession.user_id.in_(user_ids or [-1]))
            .group_by(UserSession.user_id)
            .all()
        )
        registration_query = db.query(RegistrationRequest)
        registration_query = _apply_date(
            registration_query, RegistrationRequest.submitted_at, start_date, end_date
        )
        registration_counts = Counter(
            str(row.status) for row in registration_query.all() if row.status is not None
        )
        rows = [
            {
                "User ID": int(user.id),
                "Name": _display(user.full_name),
                "Email": _display(user.email),
                "Role": _display(user.role),
                "Status": _display(user.account_status),
                "Verified": "Yes" if user.is_verified else "No",
                "Registered": _display(user.created_at),
                "Last login": _display(user.last_login),
                "Interviews": int(interview_counts.get(int(user.id), 0)),
                "Saved reports": int(report_counts.get(int(user.id), 0)),
            }
            for user in users
        ]
        if filters["view"] == "User Registration Report":
            rows.sort(key=lambda row: str(row["Registered"]), reverse=True)
        elif filters["view"] == "User Engagement Report":
            rows.sort(key=lambda row: (row["Interviews"], row["Saved reports"]), reverse=True)
        counts = Counter(str(row["Status"]) for row in rows)
        chart_rows = [{"Registered": str(row["Registered"])} for row in rows]
        charts = [
            chart
            for chart in (
                _date_chart(chart_rows, "Registered", "Registrations over time", "User registrations over time"),
                _category_chart(rows, "Status", "Account status", "Users by account status"),
            )
            if chart is not None
        ]
        summary = [
            ("Total users", str(len(rows))),
            ("Active", str(counts["active"])),
            ("Inactive", str(counts["inactive"])),
            ("Suspended", str(counts["suspended"])),
            ("Pending approvals", str(_count(registration_counts, "pending"))),
            ("Approved requests", str(_count(registration_counts, "approved"))),
            ("Rejected requests", str(_count(registration_counts, "rejected"))),
            ("Workspaces", str(len(session_ids))),
        ]
        notes: list[str] = []
        if not rows:
            notes.append("No user records match the selected filters.")
        return ReportResult(
            "User report",
            "User Reports",
            datetime.now(),
            start_date,
            end_date,
            filters,
            summary,
            ["User ID", "Name", "Email", "Role", "Status", "Verified", "Registered", "Last login", "Interviews", "Saved reports"],
            rows,
            charts,
            notes,
        )


def _platform_rows(
    actor_admin_id: int,
    start_date: date | None,
    end_date: date | None,
    filters: dict[str, Any],
    *,
    scores_only: bool = False,
) -> tuple[list[dict[str, Any]], list[tuple[str, float | None]]]:
    snapshot = get_platform_report(actor_admin_id, start_date, end_date)
    source = snapshot.rows
    rows: list[dict[str, Any]] = []
    dimension_scores: list[tuple[str, float | None]] = []
    with get_readonly_db() as db:
        summaries = dict(
            db.query(InterviewSession.id, InterviewSession.strengths)
            .filter(InterviewSession.id.in_([row.interview_id for row in source] or [-1]))
            .all()
        )
    for row in source:
        if filters["user_id"] is not None and row.user_id != filters["user_id"]:
            continue
        if filters["status"] not in {"", "all"} and row.status != filters["status"]:
            continue
        if filters["interview_type"] not in {"", "all"}:
            if filters["interview_type"].lower() not in row.interview_type.lower():
                continue
        score = row.overall_score
        if scores_only and score is None:
            continue
        if filters["min_score"] is not None and (score is None or score < filters["min_score"]):
            continue
        if filters["max_score"] is not None and (score is None or score > filters["max_score"]):
            continue
        summary_text = str(summaries.get(row.interview_id) or "").strip()
        rows.append(
            {
                "Date": row.interview_date,
                "Candidate": row.user_name,
                "Email": row.user_email,
                "Interview type": row.interview_type,
                "Status": row.status,
                "Score": score,
                "Evaluation summary": summary_text[:240] or "Not recorded",
                "Technical score": row.technical_score,
                "Communication score": row.communication_score,
                "Problem solving score": row.problem_solving_score,
                "Questions": row.total_questions,
                "Answered": row.answered_questions,
            }
        )
        dimension_scores.extend(
            [
                ("Technical", row.technical_score),
                ("Communication", row.communication_score),
                ("Problem solving", row.problem_solving_score),
            ]
        )
    return rows, dimension_scores


def _interview_report(
    actor_admin_id: int,
    start_date: date | None,
    end_date: date | None,
    filters: dict[str, Any],
) -> ReportResult:
    rows, _ = _platform_rows(actor_admin_id, start_date, end_date, filters)
    completed = [row for row in rows if row["Status"] == "completed"]
    scores = [
        float(row["Score"]) for row in rows if row["Score"] is not None
    ]
    statuses = Counter(str(row["Status"]) for row in rows)
    types = Counter(str(row["Interview type"]) for row in rows)
    charts = [
        chart
        for chart in (
            _date_chart(rows, "Date", "Interviews over time", "Interview attempts over time"),
            _category_chart(rows, "Status", "Completion status", "Interviews by status"),
            _category_chart(rows, "Interview type", "Interview types", "Interviews by type"),
            _score_bands(scores),
        )
        if chart is not None
    ]
    summary = [
        ("Total attempts", str(len(rows))),
        ("Completed", str(len(completed))),
        ("Incomplete", str(len(rows) - len(completed))),
        ("Completion rate", _percentage(len(completed), len(rows))),
        ("Average score", f"{sum(scores) / len(scores):.2f}" if scores else "Not recorded"),
        ("Scored interviews", str(len(scores))),
        ("Unique interview types", str(len(types))),
        ("In progress", str(statuses["in_progress"])),
    ]
    notes = ["Live interview invitations are reported in Live Interview Reports."]
    if not rows:
        notes.append("No interview records match the selected filters.")
    return ReportResult(
        "Interview report",
        "Interview Reports",
        datetime.now(),
        start_date,
        end_date,
        filters,
        summary,
        ["Date", "Candidate", "Email", "Interview type", "Status", "Score", "Evaluation summary", "Questions", "Answered"],
        rows,
        charts,
        notes,
    )


def _performance_report(
    actor_admin_id: int,
    start_date: date | None,
    end_date: date | None,
    filters: dict[str, Any],
) -> ReportResult:
    rows, dimensions = _platform_rows(
        actor_admin_id, start_date, end_date, filters, scores_only=True
    )
    scores = [float(row["Score"]) for row in rows if row["Score"] is not None]
    grouped: dict[str, list[float]] = defaultdict(list)
    for name, value in dimensions:
        if value is not None:
            grouped[name].append(float(value))
    chart_rows = [
        {"Dimension": name, "Average score": round(sum(values) / len(values), 2)}
        for name, values in grouped.items()
    ]
    charts = [
        chart
        for chart in (
            _score_bands(scores),
            _date_chart(rows, "Date", "Performance trend", "Stored interview scores over time"),
            ReportChart(
                "Skill dimension averages",
                "bar",
                chart_rows,
                "Dimension",
                "Average score",
                "Average stored scores by evaluation dimension",
            )
            if chart_rows
            else None,
        )
        if chart is not None
    ]
    summary = [
        ("Scored interviews", str(len(scores))),
        ("Average score", f"{sum(scores) / len(scores):.2f}" if scores else "Not recorded"),
        ("Highest score", f"{max(scores):.2f}" if scores else "Not recorded"),
        ("Lowest score", f"{min(scores):.2f}" if scores else "Not recorded"),
        ("Technical average", f"{sum(grouped['Technical']) / len(grouped['Technical']):.2f}" if grouped["Technical"] else "Not recorded"),
        ("Communication average", f"{sum(grouped['Communication']) / len(grouped['Communication']):.2f}" if grouped["Communication"] else "Not recorded"),
        ("Problem-solving average", f"{sum(grouped['Problem solving']) / len(grouped['Problem solving']):.2f}" if grouped["Problem solving"] else "Not recorded"),
    ]
    notes = ["Confidence score is not stored by the current evaluation logic."]
    if not rows:
        notes.append("No scored interviews match the selected filters.")
    return ReportResult(
        "Performance report",
        "Performance Reports",
        datetime.now(),
        start_date,
        end_date,
        filters,
        summary,
        ["Date", "Candidate", "Interview type", "Status", "Score", "Technical score", "Communication score", "Problem solving score", "Evaluation summary"],
        rows,
        charts,
        notes,
    )


def _job_report(
    actor_admin_id: int,
    start_date: date | None,
    end_date: date | None,
    filters: dict[str, Any],
) -> ReportResult:
    require_admin(actor_admin_id)
    with get_readonly_db() as db:
        query = _apply_date(db.query(JobPost), JobPost.created_at, start_date, end_date)
        if filters["job_status"] == "published":
            query = query.filter(JobPost.is_published.is_(True))
        elif filters["job_status"] == "draft":
            query = query.filter(JobPost.is_published.is_(False))
        jobs = query.order_by(JobPost.created_at.desc()).all()
        skill_categories = {
            str(row[0])
            for row in db.query(JobSkill.category).filter(JobSkill.category.isnot(None)).distinct().all()
            if row[0]
        }
        rows: list[dict[str, Any]] = []
        for job in jobs:
            preferences = job.question_preferences or []
            category_text = ", ".join(str(item) for item in preferences)
            if filters["job_category"] not in {"", "all"}:
                if filters["job_category"].lower() not in category_text.lower():
                    continue
            rows.append(
                {
                    "Title": _display(job.title),
                    "Company": _display(job.company),
                    "Status": "Published" if job.is_published else "Draft",
                    "Category": category_text or "Not recorded",
                    "Difficulty": _display(job.interview_difficulty),
                    "Experience level": _display(job.experience_level),
                    "Skills": str(len(job.job_skills)) if hasattr(job, "job_skills") else "",
                    "Posted": _display(job.created_at),
                }
            )
    statuses = Counter(str(row["Status"]) for row in rows)
    charts = [
        chart
        for chart in (
            _date_chart(rows, "Posted", "Posting trend", "Job postings over time"),
            _category_chart(rows, "Status", "Posting status", "Job postings by status"),
            _category_chart(rows, "Category", "Popular categories", "Job postings by category"),
        )
        if chart is not None
    ]
    summary = [
        ("Total postings", str(len(rows))),
        ("Published", str(statuses["Published"])),
        ("Draft", str(statuses["Draft"])),
        ("Expired", "Not tracked"),
        ("Job applications", "Not tracked"),
        ("Distinct categories", str(len({row["Category"] for row in rows}))),
    ]
    notes = [
        "Job expiry and job application tracking are not available in current records.",
        f"Available skill categories: {', '.join(sorted(skill_categories)) or 'none recorded'}.",
    ]
    if not rows:
        notes.append("No job postings match the selected filters.")
    return ReportResult(
        "Job report",
        "Job Reports",
        datetime.now(),
        start_date,
        end_date,
        filters,
        summary,
        ["Title", "Company", "Status", "Category", "Difficulty", "Experience level", "Skills", "Posted"],
        rows,
        charts,
        notes,
    )


def _session_status(status: str) -> str:
    if status == "in_progress":
        return "In progress"
    if status in {"completed", "interrupted"}:
        return "Ended"
    return "Not started"


def _live_event_rows(
    db: Any,
    start_date: date | None,
    end_date: date | None,
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    query = _apply_date(
        db.query(EventLog), EventLog.created_at, start_date, end_date
    ).filter(EventLog.event_type.in_(tuple(_LIVE_EVENT_STATUSES)))
    grouped: dict[str, list[EventLog]] = defaultdict(list)
    for event in query.order_by(EventLog.created_at.asc()).all():
        match = _ROOM_ID_PATTERN.search(str(event.message or ""))
        if match:
            grouped[match.group(0)].append(event)
    user_names = dict(
        db.query(User.id, User.full_name).filter(User.id.in_([e.user_id for events in grouped.values() for e in events if e.user_id is not None] or [-1])).all()
    )
    rows: list[dict[str, Any]] = []
    for room_id, events in grouped.items():
        latest = events[-1]
        status = _LIVE_EVENT_STATUSES[str(latest.event_type)]
        related_user = ""
        if str(latest.event_type) in {"live_call.accepted", "live_call.in_progress"}:
            related_user = _display(user_names.get(latest.user_id, ""))
        if filters["invitation_status"] not in {"", "all"} and status != filters["invitation_status"]:
            continue
        if filters["user_id"] is not None and latest.user_id != filters["user_id"]:
            continue
        rows.append(
            {
                "Created": _display(latest.created_at),
                "Room": room_id,
                "Title": "Not recorded",
                "Candidate": related_user,
                "Invitation status": status.replace("_", " ").title(),
                "Session status": _session_status(status),
                "Accepted": "Not recorded",
                "Started": "Not recorded",
                "Ended": "Not recorded",
                "Duration": "Not recorded",
            }
        )
    return rows


def _live_report(
    actor_admin_id: int,
    start_date: date | None,
    end_date: date | None,
    filters: dict[str, Any],
) -> ReportResult:
    require_admin(actor_admin_id)
    with get_readonly_db() as db:
        query = _apply_date(
            db.query(InterviewCallRoom, User)
            .outerjoin(User, InterviewCallRoom.candidate_user_id == User.id),
            InterviewCallRoom.created_at,
            start_date,
            end_date,
        )
        if filters["user_id"] is not None:
            query = query.filter(InterviewCallRoom.candidate_user_id == filters["user_id"])
        if filters["invitation_status"] not in {"", "all"}:
            query = query.filter(InterviewCallRoom.status == filters["invitation_status"])
        records = query.order_by(InterviewCallRoom.created_at.desc()).all()
        rows: list[dict[str, Any]] = []
        for room, candidate in records:
            status = str(room.status)
            duration: int | str
            if room.started_at and room.ended_at:
                session_status = "Ended"
                duration = max(0, int((room.ended_at - room.started_at).total_seconds()))
            elif room.started_at:
                session_status = "In progress"
                duration = "Not recorded"
            else:
                session_status = "Not started"
                duration = "Not recorded"
            rows.append(
                {
                    "Created": _display(room.created_at),
                    "Room": _display(room.room_id),
                    "Title": _display(room.title),
                    "Candidate": _display(candidate.full_name if candidate else ""),
                    "Invitation status": status.replace("_", " ").title(),
                    "Session status": session_status,
                    "Accepted": _display(room.accepted_at),
                    "Started": _display(room.started_at),
                    "Ended": _display(room.ended_at),
                    "Duration": f"{duration}s" if isinstance(duration, int) else duration,
                }
            )
        existing_rooms = {row["Room"] for row in rows}
        event_rows = _live_event_rows(db, start_date, end_date, filters)
        rows.extend(row for row in event_rows if row["Room"] not in existing_rooms)
    statuses = Counter(
        str(row["Invitation status"]).lower().replace(" ", "_") for row in rows
    )
    session_statuses = Counter(str(row["Session status"]) for row in rows)
    charts = [
        chart
        for chart in (
            _date_chart(rows, "Created", "Invitations over time", "Live invitations over time"),
            _category_chart(rows, "Invitation status", "Invitation status", "Live invitations by status"),
            _category_chart(rows, "Session status", "Session status", "Live sessions by recorded status"),
        )
        if chart is not None
    ]
    summary = [
        ("Total invitations", str(len(rows))),
        ("Pending", str(statuses["invited"])),
        ("Accepted", str(statuses["accepted"])),
        ("In progress", str(statuses["in_progress"])),
        ("Completed", str(statuses["completed"])),
        ("Cancelled", str(statuses["cancelled"])),
        ("Declined", str(statuses["declined"])),
        ("Expired", str(statuses["expired"])),
        ("Interrupted", str(statuses["interrupted"])),
        ("Ended sessions", str(session_statuses["Ended"])),
    ]
    notes = [
        "Session status is derived only from recorded start and end timestamps.",
        "Live WebRTC sessions are runtime state and are not stored as historical records.",
        "Historical invitation rows are reconstructed from real audit events when room records are no longer present.",
    ]
    if not rows:
        notes.append("No live interview invitations match the selected filters.")
    return ReportResult(
        "Live interview report",
        "Live Interview Reports",
        datetime.now(),
        start_date,
        end_date,
        filters,
        summary,
        ["Created", "Room", "Title", "Candidate", "Invitation status", "Session status", "Accepted", "Started", "Ended", "Duration"],
        rows,
        charts,
        notes,
    )


def _safe_message(message: Any) -> str:
    text = str(message or "")
    text = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    return text.replace("\n", " ")[:240]


def _activity_type(event_type: str) -> str:
    for name, values in _ACTIVITY_GROUPS.items():
        if event_type in values:
            return name.replace("_", " ").title()
    if event_type.startswith("user."):
        return "Profile"
    if event_type.startswith("admin."):
        return "Admin"
    if event_type.startswith("registration."):
        return "Registration"
    return "Other"


def _system_activity_report(
    actor_admin_id: int,
    start_date: date | None,
    end_date: date | None,
    filters: dict[str, Any],
) -> ReportResult:
    require_admin(actor_admin_id)
    with get_readonly_db() as db:
        query = _apply_date(db.query(EventLog), EventLog.created_at, start_date, end_date)
        if filters["user_id"] is not None:
            query = query.filter(EventLog.user_id == filters["user_id"])
        activity_type = filters["activity_type"]
        if activity_type not in {"", "all"}:
            if activity_type not in _ACTIVITY_GROUPS:
                raise ReportError("Unsupported activity type.")
            query = query.filter(EventLog.event_type.in_(_ACTIVITY_GROUPS[activity_type]))
        if filters["view"] == "Admin Actions":
            query = query.filter(EventLog.event_type.like("admin.%"))
        elif filters["view"] == "Registration History":
            query = query.filter(
                EventLog.event_type.in_(_ACTIVITY_GROUPS["registration"])
            )
        events = query.order_by(EventLog.created_at.desc()).all()
        user_names = dict(
            db.query(User.id, User.full_name)
            .filter(User.id.in_([event.user_id for event in events if event.user_id is not None] or [-1]))
            .all()
        )
        rows = [
            {
                "Date/time": _display(event.created_at),
                "Activity": _activity_type(str(event.event_type)),
                "Event": str(event.event_type),
                "User": _display(user_names.get(event.user_id, "")) if event.user_id else "System",
                "Level": _display(event.level),
                "Message": _safe_message(event.message),
            }
            for event in events
        ]
    groups = Counter(str(row["Activity"]) for row in rows)
    event_types = Counter(str(row["Event"]) for row in rows)
    charts = [
        chart
        for chart in (
            _date_chart(rows, "Date/time", "Activity over time", "Audit events over time"),
            _category_chart(rows, "Activity", "Activity types", "Audit events by activity type"),
        )
        if chart is not None
    ]
    summary = [
        ("Total events", str(len(rows))),
        ("Login activity", str(groups["Login"])),
        ("Profile updates", str(groups["Profile"])),
        ("Admin actions", str(groups["Admin"])),
        ("Invitation events", str(groups["Invitation"])),
        ("Status changes", str(groups["Status"])),
        ("Approvals", str(event_types["registration.request_approved"])),
        ("Rejections", str(event_types["registration.request_rejected"])),
    ]
    notes = ["Only event types present in the audit log are reported."]
    if not rows:
        notes.append("No activity events match the selected filters.")
    return ReportResult(
        "System activity report",
        "System Activity Reports",
        datetime.now(),
        start_date,
        end_date,
        filters,
        summary,
        ["Date/time", "Activity", "Event", "User", "Level", "Message"],
        rows,
        charts,
        notes,
    )


def build_report(
    *,
    actor_admin_id: int | None,
    category: str,
    start_date: date | None = None,
    end_date: date | None = None,
    filters: dict[str, Any] | None = None,
) -> ReportResult:
    normalized = _validate(actor_admin_id, category, start_date, end_date, filters or {})
    admin_id = int(actor_admin_id) if actor_admin_id is not None else 0
    if category == "User Reports":
        return _user_report(admin_id, start_date, end_date, normalized)
    if category == "Interview Reports":
        return _interview_report(admin_id, start_date, end_date, normalized)
    if category == "Performance Reports":
        return _performance_report(admin_id, start_date, end_date, normalized)
    if category == "Job Reports":
        return _job_report(admin_id, start_date, end_date, normalized)
    if category == "Live Interview Reports":
        return _live_report(admin_id, start_date, end_date, normalized)
    return _system_activity_report(admin_id, start_date, end_date, normalized)


def serialize_filters(result: ReportResult) -> str:
    payload = dict(result.filters)
    payload["start_date"] = result.start_date.isoformat() if result.start_date else None
    payload["end_date"] = result.end_date.isoformat() if result.end_date else None
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def deserialize_filters(payload: str) -> tuple[date | None, date | None, dict[str, Any]]:
    try:
        raw = json.loads(payload or "{}")
    except json.JSONDecodeError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    start_date = _as_date(raw.pop("start_date", None))
    end_date = _as_date(raw.pop("end_date", None))
    return start_date, end_date, raw


def get_report_date_bounds(actor_admin_id: int | None) -> tuple[date | None, date | None]:
    require_admin(actor_admin_id)
    with get_readonly_db() as db:
        pairs = [
            db.query(func.min(User.created_at), func.max(User.created_at)).one(),
            db.query(func.min(EventLog.created_at), func.max(EventLog.created_at)).one(),
            db.query(func.min(JobPost.created_at), func.max(JobPost.created_at)).one(),
            db.query(
                func.min(InterviewCallRoom.created_at), func.max(InterviewCallRoom.created_at)
            ).one(),
            db.query(func.min(GeneratedReport.created_at), func.max(GeneratedReport.created_at)).one(),
        ]
    start_dates: list[date] = [
        item for item in (_as_date(low) for low, _ in pairs) if item is not None
    ]
    end_dates: list[date] = [
        item for item in (_as_date(high) for _, high in pairs) if item is not None
    ]
    return (min(start_dates) if start_dates else None, max(end_dates) if end_dates else None)


def get_filter_options(actor_admin_id: int | None) -> dict[str, list[str]]:
    require_admin(actor_admin_id)
    with get_readonly_db() as db:
        interview_statuses = {
            str(row[0])
            for row in db.query(InterviewSession.status).distinct().all()
            if row[0]
        }
        interview_types = {
            str(row[0])
            for row in db.query(InterviewSession.interview_type).distinct().all()
            if row[0]
        }
        job_categories = {
            str(row[0])
            for row in db.query(JobSkill.category).distinct().all()
            if row[0]
        }
    return {
        "user_status": ["all", "active", "inactive", "suspended"],
        "interview_status": ["all", *sorted(interview_statuses | {"completed", "in_progress"})],
        "interview_type": ["all", *sorted(interview_types)],
        "job_status": ["all", "published", "draft"],
        "job_category": ["all", *sorted(job_categories)],
        "invitation_status": ["all", *sorted(CALL_STATES)],
        "activity_type": ["all", *sorted(_ACTIVITY_GROUPS), "other"],
    }


def get_report_overview(actor_admin_id: int | None) -> dict[str, str]:
    require_admin(actor_admin_id)
    with get_readonly_db() as db:
        admin_rows = db.query(AdminReportHistory).all()
        stored_reports = int(db.query(GeneratedReport).count())
    user_count = sum(1 for row in admin_rows if row.category == "User Reports")
    interview_count = sum(1 for row in admin_rows if row.category == "Interview Reports")
    timestamps = [row.generated_at for row in admin_rows if row.generated_at is not None]
    last_generated = max(timestamps) if timestamps else None
    return {
        "total": str(len(admin_rows) + stored_reports),
        "user_reports": str(user_count),
        "interview_reports": str(interview_count + stored_reports),
        "last_generated": _display(last_generated),
    }


def _history_root(storage_root: Path | None) -> Path:
    root = Path(storage_root) if storage_root else runtime_path("data/reports/admin")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _history_dict(row: AdminReportHistory) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "report_name": str(row.report_name),
        "category": str(row.category),
        "generated_by": str(row.generated_by_user_id or ""),
        "generated_at": _display(row.generated_at),
        "date_range": (
            f"{row.date_start.isoformat()} to {row.date_end.isoformat()}"
            if row.date_start and row.date_end
            else _display(row.date_start or row.date_end)
        ),
        "export_format": str(row.export_format).upper(),
        "status": str(row.status),
        "file_name": str(row.file_name),
        "filter_json": str(row.filter_json or "{}"),
    }


def save_generated_report(
    *,
    actor_admin_id: int | None,
    user_session_id: int | None,
    result: ReportResult,
    export_format: str,
    data: bytes,
    storage_root: Path | None = None,
) -> int:
    require_admin(actor_admin_id)
    normalized_format = str(export_format).lower()
    if normalized_format not in _EXPORT_FORMATS:
        raise ReportError("Unsupported export format.")
    root = _history_root(storage_root)
    stamp = result.generated_at.strftime("%Y%m%d_%H%M%S")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", result.category).strip("_").lower() or "report"
    file_name = sanitize_filename(
        f"admin_{stem}_{stamp}_{uuid.uuid4().hex[:8]}.{normalized_format}"
    )
    path = root / file_name
    path.write_bytes(data)
    try:
        with get_db_context() as db:
            history = AdminReportHistory(
                report_name=result.title,
                category=result.category,
                generated_by_user_id=int(actor_admin_id) if actor_admin_id is not None else None,
                generated_by_session_id=user_session_id,
                generated_at=result.generated_at,
                date_start=result.start_date,
                date_end=result.end_date,
                export_format=normalized_format,
                status="generated",
                filter_json=serialize_filters(result),
                file_name=file_name,
            )
            db.add(history)
            db.flush()
            history_id = int(history.id)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    log_event(
        "admin.report_generated",
        f"Generated {result.category} report as {normalized_format.upper()}.",
        user_id=actor_admin_id,
    )
    return history_id


def list_report_history(
    actor_admin_id: int | None, *, status: str = "all", limit: int = 100
) -> list[dict[str, Any]]:
    require_admin(actor_admin_id)
    safe_limit = max(1, min(500, int(limit)))
    with get_readonly_db() as db:
        query = db.query(AdminReportHistory)
        normalized_status = str(status or "all").strip().lower()
        if normalized_status not in {"", "all"}:
            query = query.filter(AdminReportHistory.status == normalized_status)
        rows = query.order_by(AdminReportHistory.generated_at.desc()).limit(safe_limit).all()
        return [_history_dict(row) for row in rows]


def get_report_history(actor_admin_id: int | None, report_id: int) -> dict[str, Any] | None:
    require_admin(actor_admin_id)
    with get_readonly_db() as db:
        row = db.query(AdminReportHistory).filter(AdminReportHistory.id == int(report_id)).first()
        return _history_dict(row) if row else None


def build_report_from_history(actor_admin_id: int | None, report_id: int) -> ReportResult:
    row = get_report_history(actor_admin_id, report_id)
    if row is None:
        raise ReportError("Generated report not found.")
    start_date, end_date, filters = deserialize_filters(str(row["filter_json"]))
    return build_report(
        actor_admin_id=actor_admin_id,
        category=str(row["category"]),
        start_date=start_date,
        end_date=end_date,
        filters=filters,
    )


def export_report_from_history(
    actor_admin_id: int | None, report_id: int
) -> tuple[bytes, str, str]:
    row = get_report_history(actor_admin_id, report_id)
    if row is None:
        raise ReportError("Generated report not found.")
    from services.report_export_service import export_report_bytes

    result = build_report_from_history(actor_admin_id, report_id)
    export_format = str(row["export_format"]).strip().lower()
    data = export_report_bytes(result, export_format)
    mime = {
        "csv": "text/csv",
        "pdf": "application/pdf",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "json": "application/json",
    }[export_format]
    file_name = str(row["file_name"])
    return data, file_name, mime


def delete_report_history(
    actor_admin_id: int | None, report_id: int, storage_root: Path | None = None
) -> bool:
    require_admin(actor_admin_id)
    with get_db_context() as db:
        row = db.query(AdminReportHistory).filter(AdminReportHistory.id == int(report_id)).first()
        if row is None:
            return False
        file_name = str(row.file_name)
        db.delete(row)
    root = _history_root(storage_root).resolve()
    path = (root / file_name).resolve()
    if root in path.parents:
        path.unlink(missing_ok=True)
    log_event(
        "admin.report_deleted",
        f"Deleted generated report metadata for report #{int(report_id)}.",
        user_id=actor_admin_id,
    )
    return True


def as_report_dict(result: ReportResult) -> dict[str, Any]:
    return asdict(result)
