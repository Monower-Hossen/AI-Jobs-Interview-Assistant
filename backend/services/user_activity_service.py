"""Admin user activity analytics computed from persisted records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from database import (
    EventLog,
    GeneratedReport,
    InterviewSession,
    LearningActivity,
    LiveInterviewRecording,
    QuestionPracticeAttempt,
    ResumeAnalysis,
    User,
    UserSession,
    get_readonly_db,
)
from services.auth_service import AuthError, _require_admin_actor
from sqlalchemy import or_

ACTIVITY_DATE_RANGES = ("Last 7 Days", "Last 30 Days", "Last 90 Days", "All Time")
_RANGE_DAYS = {"Last 7 Days": 7, "Last 30 Days": 30, "Last 90 Days": 90}
_LOGIN_EVENTS = {
    "auth.login",
    "auth.login_failed",
    "auth.login_locked",
    "auth.logout",
    "auth.session_start",
}
_MIN_TIMESTAMP = 0.0


@dataclass(frozen=True)
class ActivityWindow:
    label: str
    start_at: datetime | None
    end_at: datetime | None


@dataclass(frozen=True)
class UserActivityRow:
    user_id: int
    full_name: str
    email: str
    username: str
    role: str
    account_status: str
    activity_status: str
    created_at: datetime | None
    last_login: datetime | None
    last_active: datetime | None
    total_interviews: int
    completed_interviews: int
    average_score: float | None
    logged_events: int
    session_count: int
    resume_uploads: int
    learning_activities: int
    practice_attempts: int
    reports: int
    live_interviews: int


@dataclass(frozen=True)
class ActivityEvent:
    user_id: int
    user_name: str
    activity_type: str
    description: str
    timestamp: datetime | None


@dataclass(frozen=True)
class SessionDetail:
    user_id: int
    user_name: str
    session_id: int
    label: str
    created_at: datetime | None
    last_active: datetime | None
    revoked_at: datetime | None
    interview_count: int


@dataclass(frozen=True)
class InterviewDetail:
    user_id: int
    user_name: str
    interview_id: int
    interview_type: str
    status: str
    score: float | None
    created_at: datetime | None


@dataclass(frozen=True)
class UserActivityReport:
    window: ActivityWindow
    rows: list[UserActivityRow]
    timeline: list[ActivityEvent]
    login_history: list[ActivityEvent]
    sessions: list[SessionDetail]
    interviews: list[InterviewDetail]
    selected_user_id: int | None = None
    total_interviews: int = 0
    completed_interviews: int = 0
    average_score: float | None = None
    total_logged_events: int = 0


def _average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _text(value: Any, limit: int = 300) -> str:
    return str(value or "").strip()[:limit]


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    return None


def _latest(values: list[datetime | None]) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def resolve_activity_window(label: str) -> ActivityWindow:
    normalized = label if label in ACTIVITY_DATE_RANGES else "Last 30 Days"
    if normalized == "All Time":
        return ActivityWindow(normalized, None, None)
    end_at = datetime.now()
    start_at = end_at - timedelta(days=_RANGE_DAYS[normalized])
    return ActivityWindow(normalized, start_at, end_at)


def _window_filters(column: Any, window: ActivityWindow) -> list[Any]:
    filters: list[Any] = []
    if window.start_at is not None:
        filters.append(column >= window.start_at)
    if window.end_at is not None:
        filters.append(column <= window.end_at)
    return filters


def _in_window(value: datetime | None, window: ActivityWindow) -> bool:
    if value is None:
        return False
    if window.start_at is not None and value < window.start_at:
        return False
    return not (window.end_at is not None and value > window.end_at)


def _session_filters(window: ActivityWindow) -> list[Any]:
    if window.start_at is None or window.end_at is None:
        return []
    return [
        or_(
            UserSession.created_at >= window.start_at,
            UserSession.updated_at >= window.start_at,
            UserSession.last_active >= window.start_at,
        ),
        or_(
            UserSession.created_at <= window.end_at,
            UserSession.updated_at <= window.end_at,
            UserSession.last_active <= window.end_at,
        ),
    ]


def _enabled_account(user: User) -> bool:
    status = _text(getattr(user, "account_status", "active"), 20).lower() or "active"
    return status == "active" and bool(getattr(user, "is_active", True))


def _event_sort_key(event: ActivityEvent) -> float:
    return event.timestamp.timestamp() if event.timestamp else _MIN_TIMESTAMP


def get_user_activity_report(
    actor_admin_id: int | None,
    date_range: str = "Last 30 Days",
    user_id: int | None = None,
) -> UserActivityReport:
    window = resolve_activity_window(date_range)
    with get_readonly_db() as db:
        _require_admin_actor(db, actor_admin_id)
        users_query = db.query(User)
        if user_id is not None:
            users_query = users_query.filter(User.id == user_id)
        users = users_query.order_by(User.id).all()
        if user_id is not None and not users:
            raise AuthError("User not found.")

        user_map = {int(user.id): user for user in users}
        user_ids = list(user_map)
        if not user_ids:
            return UserActivityReport(
                window=window, rows=[], timeline=[], login_history=[], sessions=[], interviews=[]
            )

        sessions = (
            db.query(UserSession)
            .filter(UserSession.user_id.in_(user_ids), *_session_filters(window))
            .all()
        )
        sessions_by_id = {int(session.id): session for session in sessions}
        session_ids = list(sessions_by_id)
        session_owner = {
            int(session.id): int(session.user_id) for session in sessions if session.user_id
        }

        events: list[EventLog] = []
        interviews: list[InterviewSession] = []
        resumes: list[ResumeAnalysis] = []
        learning: list[LearningActivity] = []
        practice: list[QuestionPracticeAttempt] = []
        reports: list[GeneratedReport] = []
        live: list[LiveInterviewRecording] = []

        if user_ids:
            events = (
                db.query(EventLog)
                .filter(
                    EventLog.user_id.in_(user_ids), *_window_filters(EventLog.created_at, window)
                )
                .all()
            )
        if session_ids:
            interviews = (
                db.query(InterviewSession)
                .filter(
                    InterviewSession.session_id.in_(session_ids),
                    *_window_filters(InterviewSession.created_at, window),
                )
                .all()
            )
            resumes = (
                db.query(ResumeAnalysis)
                .filter(
                    ResumeAnalysis.session_id.in_(session_ids),
                    *_window_filters(ResumeAnalysis.created_at, window),
                )
                .all()
            )
            if window.start_at is not None and window.end_at is not None:
                learning = (
                    db.query(LearningActivity)
                    .filter(
                        LearningActivity.session_id.in_(session_ids),
                        LearningActivity.activity_date >= window.start_at.date(),
                        LearningActivity.activity_date <= window.end_at.date(),
                    )
                    .all()
                )
            else:
                learning = (
                    db.query(LearningActivity)
                    .filter(LearningActivity.session_id.in_(session_ids))
                    .all()
                )
            practice = (
                db.query(QuestionPracticeAttempt)
                .filter(
                    QuestionPracticeAttempt.session_id.in_(session_ids),
                    *_window_filters(QuestionPracticeAttempt.submitted_at, window),
                )
                .all()
            )
            reports = (
                db.query(GeneratedReport)
                .filter(
                    GeneratedReport.session_id.in_(session_ids),
                    *_window_filters(GeneratedReport.created_at, window),
                )
                .all()
            )
        live = (
            db.query(LiveInterviewRecording)
            .filter(
                LiveInterviewRecording.owner_user_id.in_(user_ids),
                *_window_filters(LiveInterviewRecording.created_at, window),
            )
            .all()
        )

        grouped: dict[int, dict[str, Any]] = {
            user_id_value: {
                "timestamps": [],
                "total_interviews": 0,
                "completed_interviews": 0,
                "scores": [],
                "logged_events": 0,
                "session_count": 0,
                "resume_uploads": 0,
                "learning_activities": 0,
                "practice_attempts": 0,
                "reports": 0,
                "live_interviews": 0,
            }
            for user_id_value in user_ids
        }
        timeline: list[ActivityEvent] = []

        def add_event(
            owner_id: int, activity_type: str, description: str, timestamp: datetime | None
        ) -> None:
            if owner_id not in user_map:
                return
            timeline.append(
                ActivityEvent(
                    user_id=owner_id,
                    user_name=str(user_map[owner_id].full_name),
                    activity_type=activity_type,
                    description=_text(description),
                    timestamp=timestamp,
                )
            )
            if _in_window(timestamp, window):
                grouped[owner_id]["timestamps"].append(timestamp)

        for session in sessions:
            owner_id = int(session.user_id) if session.user_id else None
            if owner_id is None or owner_id not in grouped:
                continue
            grouped[owner_id]["session_count"] += 1
            add_event(
                owner_id,
                "Workspace session",
                f"Session {session.id}: {_text(session.label, 120) or 'workspace'}",
                _as_datetime(session.created_at),
            )

        for event in events:
            owner_id = int(event.user_id) if event.user_id else None
            if owner_id is None or owner_id not in grouped:
                continue
            grouped[owner_id]["logged_events"] += 1
            add_event(owner_id, _text(event.event_type, 60), _text(event.message), event.created_at)

        for interview in interviews:
            owner_id = session_owner.get(int(interview.session_id))
            if owner_id is None or owner_id not in grouped:
                continue
            grouped[owner_id]["total_interviews"] += 1
            if _text(interview.status).lower() == "completed":
                grouped[owner_id]["completed_interviews"] += 1
            if interview.overall_score is not None:
                grouped[owner_id]["scores"].append(float(interview.overall_score))
            score = (
                f" — {float(interview.overall_score):.1f}/10"
                if interview.overall_score is not None
                else ""
            )
            add_event(
                owner_id,
                "Interview",
                f"{_text(interview.interview_type, 50) or 'Interview'} "
                f"({_text(interview.status, 30) or 'unknown'}){score}",
                interview.created_at,
            )
            if interview.completed_at is not None:
                grouped[owner_id]["timestamps"].append(interview.completed_at)

        for resume in resumes:
            owner_id = session_owner.get(int(resume.session_id))
            if owner_id is None or owner_id not in grouped:
                continue
            grouped[owner_id]["resume_uploads"] += 1
            add_event(
                owner_id,
                "Resume upload",
                _text(resume.file_name, 200) or "Resume analysed",
                resume.created_at,
            )

        for item in learning:
            owner_id = session_owner.get(int(item.session_id))
            if owner_id is None or owner_id not in grouped:
                continue
            grouped[owner_id]["learning_activities"] += 1
            add_event(
                owner_id,
                "Learning activity",
                _text(item.description, 200) or _text(item.activity_type, 60),
                _as_datetime(item.activity_date),
            )

        for attempt in practice:
            owner_id = session_owner.get(int(attempt.session_id))
            if owner_id is None or owner_id not in grouped:
                continue
            grouped[owner_id]["practice_attempts"] += 1
            score = (
                f" — {float(attempt.overall_score):.1f}/10"
                if attempt.overall_score is not None
                else ""
            )
            add_event(
                owner_id,
                "Question practice",
                f"{_text(attempt.category, 50) or 'Practice question'} "
                f"({_text(attempt.status, 30) or 'submitted'}){score}",
                attempt.submitted_at,
            )

        for report in reports:
            owner_id = session_owner.get(int(report.session_id))
            if owner_id is None or owner_id not in grouped:
                continue
            grouped[owner_id]["reports"] += 1
            add_event(
                owner_id,
                "Report generated",
                _text(report.file_name, 200) or _text(report.report_type, 40),
                report.created_at,
            )

        for recording in live:
            owner_id = int(recording.owner_user_id)
            if owner_id not in grouped:
                continue
            grouped[owner_id]["live_interviews"] += 1
            add_event(
                owner_id,
                "Live interview",
                f"Recording {_text(recording.recording_status, 30) or 'created'}",
                recording.created_at,
            )
            if recording.finalized_at is not None:
                grouped[owner_id]["timestamps"].append(recording.finalized_at)

        rows: list[UserActivityRow] = []
        for user in users:
            owner_id = int(user.id)
            bucket = grouped[owner_id]
            last_login = getattr(user, "last_login", None)
            if _in_window(last_login, window):
                bucket["timestamps"].append(last_login)
            last_active = _latest(bucket["timestamps"])
            rows.append(
                UserActivityRow(
                    user_id=owner_id,
                    full_name=str(user.full_name),
                    email=str(user.email),
                    username=str(getattr(user, "username", "") or ""),
                    role=str(getattr(user, "role", "user") or "user"),
                    account_status=_text(getattr(user, "account_status", "active"), 20) or "active",
                    activity_status="Active"
                    if _enabled_account(user) and last_active
                    else "Inactive",
                    created_at=getattr(user, "created_at", None),
                    last_login=last_login,
                    last_active=last_active,
                    total_interviews=int(bucket["total_interviews"]),
                    completed_interviews=int(bucket["completed_interviews"]),
                    average_score=_average(bucket["scores"]),
                    logged_events=int(bucket["logged_events"]),
                    session_count=int(bucket["session_count"]),
                    resume_uploads=int(bucket["resume_uploads"]),
                    learning_activities=int(bucket["learning_activities"]),
                    practice_attempts=int(bucket["practice_attempts"]),
                    reports=int(bucket["reports"]),
                    live_interviews=int(bucket["live_interviews"]),
                )
            )

        timeline.sort(key=_event_sort_key, reverse=True)
        login_history = sorted(
            (event for event in timeline if event.activity_type in _LOGIN_EVENTS),
            key=_event_sort_key,
            reverse=True,
        )
        session_details = [
            SessionDetail(
                user_id=int(session.user_id) if session.user_id else 0,
                user_name=str(user_map[int(session.user_id)].full_name) if session.user_id else "",
                session_id=int(session.id),
                label=_text(session.label, 200),
                created_at=session.created_at,
                last_active=session.last_active,
                revoked_at=session.revoked_at,
                interview_count=sum(
                    1 for interview in interviews if int(interview.session_id) == int(session.id)
                ),
            )
            for session in sessions
            if session.user_id and int(session.user_id) in user_map
        ]
        interview_details = [
            InterviewDetail(
                user_id=session_owner.get(int(interview.session_id), 0),
                user_name=str(user_map[session_owner[int(interview.session_id)]].full_name)
                if int(interview.session_id) in session_owner
                else "",
                interview_id=int(interview.id),
                interview_type=_text(interview.interview_type, 50) or "N/A",
                status=_text(interview.status, 30) or "N/A",
                score=float(interview.overall_score)
                if interview.overall_score is not None
                else None,
                created_at=interview.created_at,
            )
            for interview in interviews
        ]
        interview_details.sort(
            key=lambda item: item.created_at.timestamp() if item.created_at else _MIN_TIMESTAMP,
            reverse=True,
        )
        all_scores = [
            float(interview.overall_score)
            for interview in interviews
            if interview.overall_score is not None
        ]
        return UserActivityReport(
            window=window,
            rows=rows,
            timeline=timeline[:200],
            login_history=login_history[:100],
            sessions=session_details,
            interviews=interview_details,
            selected_user_id=user_id,
            total_interviews=len(interviews),
            completed_interviews=sum(
                1 for interview in interviews if _text(interview.status).lower() == "completed"
            ),
            average_score=_average(all_scores),
            total_logged_events=len(events),
        )
