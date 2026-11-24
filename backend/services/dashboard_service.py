from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from database import (
    Assessment,
    EventLog,
    InterviewQuestion,
    InterviewSession,
    JobDescription,
    KnowledgeDocument,
    LearningRoadmap,
    LearningTask,
    ResumeAnalysis,
    ResumeJobMatch,
    UserSession,
    get_db_context,
)
from services.analytics import AnalyticsSnapshot, get_analytics
from services.auth_service import require_owned_session_id
from services.learning_service import (
    get_learning_snapshot,
    list_notifications,
    unread_notification_count,
)
from services.live_call_service import ACTIVE_CALL_STATES, ROLE_CANDIDATE
from services.live_interview_coordination_service import list_invitations


@dataclass
class DashboardSnapshot:
    analytics: AnalyticsSnapshot
    resume_score: float | None = None
    practice_hours: float = 0.0
    completed_roadmap_tasks: int = 0
    total_roadmap_tasks: int = 0
    assessment_count: int = 0
    weekly_activity: list[dict] = field(default_factory=list)
    recent_activity: list[dict] = field(default_factory=list)
    recent_feedback: list[dict] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=list)
    interview_history_summary: dict[str, Any] = field(default_factory=dict)
    performance_comparison: dict[str, Any] = field(default_factory=dict)
    skill_gaps: dict[str, Any] = field(default_factory=dict)
    improvement_plan: dict[str, Any] = field(default_factory=dict)
    upcoming_invitations: list[dict] = field(default_factory=list)
    recent_notifications: list[dict] = field(default_factory=list)
    notification_unread_count: int = 0


def _timestamp(value) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return 0.0


def _format_date(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return ""


def _unique(values) -> list[str]:
    result: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _skill_labels(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return _unique(part.strip() for part in value.replace(";", ",").split(","))
    if isinstance(value, dict):
        for key in ("skill", "name", "label", "title"):
            if key in value:
                return _skill_labels(value.get(key))
        return []
    if isinstance(value, (list, tuple, set)):
        labels: list[str] = []
        for entry in value:
            labels.extend(_skill_labels(entry))
        return _unique(labels)
    return []


def _plan_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return "; ".join(
            f"{key}: {_plan_text(item)}" for key, item in value.items() if _plan_text(item)
        )
    if isinstance(value, (list, tuple)):
        return "; ".join(entry for entry in (_plan_text(item) for item in value) if entry)
    return str(value).strip()


def _score_value(item) -> float | None:
    value = getattr(item, "overall_score", None)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def get_dashboard_snapshot(
    session_id: int,
    actor_user_id: int | None,
    *,
    session_token: str | None = None,
    actor_role: str | None = None,
) -> DashboardSnapshot:
    require_owned_session_id(session_id, actor_user_id)
    analytics = get_analytics(session_id, actor_user_id)
    learning_snapshot = get_learning_snapshot(session_id, actor_user_id)
    snapshot = DashboardSnapshot(analytics=analytics)

    with get_db_context() as db:
        workspace = db.query(UserSession).filter(UserSession.id == session_id).first()
        interviews = (
            db.query(InterviewSession)
            .filter(InterviewSession.session_id == session_id)
            .order_by(InterviewSession.created_at.desc())
            .all()
        )
        latest_resume = (
            db.query(ResumeAnalysis)
            .filter(ResumeAnalysis.session_id == session_id)
            .order_by(ResumeAnalysis.created_at.desc(), ResumeAnalysis.id.desc())
            .first()
        )
        latest_job = (
            db.query(JobDescription)
            .filter(JobDescription.session_id == session_id)
            .order_by(JobDescription.created_at.desc(), JobDescription.id.desc())
            .first()
        )
        latest_match = (
            db.query(ResumeJobMatch)
            .join(ResumeAnalysis, ResumeJobMatch.resume_analysis_id == ResumeAnalysis.id)
            .filter(ResumeAnalysis.session_id == session_id)
            .order_by(ResumeJobMatch.created_at.desc(), ResumeJobMatch.id.desc())
            .first()
        )
        roadmaps = (
            db.query(LearningRoadmap)
            .filter(LearningRoadmap.session_id == session_id)
            .order_by(LearningRoadmap.created_at.desc())
            .all()
        )
        roadmap_ids = [roadmap.id for roadmap in roadmaps]
        tasks = (
            db.query(LearningTask).filter(LearningTask.roadmap_id.in_(roadmap_ids)).all()
            if roadmap_ids
            else []
        )
        assessments = (
            db.query(Assessment)
            .filter(Assessment.session_id == session_id)
            .order_by(Assessment.created_at.desc())
            .all()
        )
        feedback_rows = (
            db.query(InterviewQuestion)
            .join(
                InterviewSession,
                InterviewQuestion.interview_session_id == InterviewSession.id,
            )
            .filter(
                InterviewSession.session_id == session_id,
                InterviewQuestion.overall_score.is_not(None),
            )
            .order_by(InterviewQuestion.evaluated_at.desc(), InterviewQuestion.id.desc())
            .limit(5)
            .all()
            if session_id
            else []
        )
        document_count = (
            db.query(KnowledgeDocument).filter(KnowledgeDocument.session_id == session_id).count()
        )
        events = (
            db.query(EventLog)
            .filter(EventLog.user_id == workspace.user_id)
            .order_by(EventLog.created_at.desc())
            .limit(8)
            .all()
            if workspace and workspace.user_id
            else []
        )

        snapshot.resume_score = (
            float(latest_resume.overall_resume_score)
            if latest_resume and latest_resume.overall_resume_score is not None
            else None
        )
        snapshot.practice_hours = round(
            sum(item.duration_seconds or 0 for item in interviews) / 3600, 1
        )
        snapshot.completed_roadmap_tasks = int(learning_snapshot.get("done_tasks") or 0)
        snapshot.total_roadmap_tasks = int(learning_snapshot.get("total_tasks") or 0)
        snapshot.assessment_count = len(assessments)

        completed_rows = [
            item for item in interviews if str(getattr(item, "status", "") or "") == "completed"
        ]
        in_progress_rows = [
            item
            for item in interviews
            if str(getattr(item, "status", "") or "") in {"in_progress", "active", "started"}
        ]
        scored_rows = [item for item in completed_rows if _score_value(item) is not None]
        latest_interview = interviews[0] if interviews else None
        recent_completed = [
            {
                "Type": str(item.interview_type or "Interview"),
                "Score": f"{_score_value(item):.1f}/10" if _score_value(item) is not None else "—",
                "Date": _format_date(item.created_at),
            }
            for item in completed_rows[:3]
        ]
        snapshot.interview_history_summary = {
            "total_sessions": len(interviews),
            "completed_sessions": len(completed_rows),
            "in_progress_sessions": len(in_progress_rows),
            "recent_completed": recent_completed,
            "latest_date": _format_date(getattr(latest_interview, "created_at", None)),
            "latest_score": _score_value(latest_interview) if latest_interview else None,
            "has_data": bool(interviews),
        }

        scores: list[float] = []
        for item in scored_rows:
            value = _score_value(item)
            if value is not None:
                scores.append(value)
        if len(scores) >= 2:
            latest_score = scores[0]
            previous_score = scores[1]
            difference = round(latest_score - previous_score, 2)
            percent_change = (
                round(difference / abs(previous_score) * 100, 1) if previous_score else None
            )
            direction = (
                "improved" if difference > 0 else "declined" if difference < 0 else "unchanged"
            )
            snapshot.performance_comparison = {
                "available": True,
                "latest_score": latest_score,
                "previous_score": previous_score,
                "difference": difference,
                "percent_change": percent_change,
                "direction": direction,
                "reason": "",
            }
        else:
            snapshot.performance_comparison = {
                "available": False,
                "latest_score": None,
                "previous_score": None,
                "difference": None,
                "percent_change": None,
                "direction": "unavailable",
                "reason": "At least two completed interviews with scores are required.",
            }

        missing_skills = _skill_labels(getattr(latest_match, "missing_skills", None))
        missing_skills.extend(_skill_labels(getattr(latest_match, "skill_gaps", None)))
        missing_skills = _unique(missing_skills)
        weak_areas = _unique(analytics.weak_areas or [])
        snapshot.skill_gaps = {
            "missing_skills": missing_skills[:8],
            "weak_areas": weak_areas[:8],
            "items": _unique(missing_skills + weak_areas)[:8],
            "job_title": str(getattr(latest_job, "title", "") or ""),
            "has_data": bool(missing_skills or weak_areas),
        }

        plan_rows = [
            item
            for item in completed_rows
            if getattr(item, "improvement_plan_7_day", None)
            or getattr(item, "improvement_plan_30_day", None)
        ]
        plan_row = plan_rows[0] if plan_rows else None
        seven_day = _plan_text(getattr(plan_row, "improvement_plan_7_day", None))
        thirty_day = _plan_text(getattr(plan_row, "improvement_plan_30_day", None))
        snapshot.improvement_plan = {
            "available": bool(seven_day or thirty_day),
            "seven_day": seven_day[:500],
            "thirty_day": thirty_day[:500],
            "date": _format_date(getattr(plan_row, "created_at", None)),
            "interview_type": str(getattr(plan_row, "interview_type", "") or ""),
        }

        for item in feedback_rows:
            weaknesses = item.weaknesses if isinstance(item.weaknesses, list) else []
            focus = ", ".join(str(entry) for entry in weaknesses[:3] if entry)
            if not focus:
                focus = str(item.technical_feedback or item.communication_feedback or "")[:120]
            snapshot.recent_feedback.append(
                {
                    "Question": (item.question_text or "").strip()[:90] or "Interview question",
                    "Category": item.category or "General",
                    "Score": f"{float(item.overall_score):.1f}/10",
                    "Focus": focus or "Review the full report for details",
                    "Date": (
                        item.evaluated_at.strftime("%Y-%m-%d")
                        if item.evaluated_at
                        else ""
                    ),
                }
            )

        today = datetime.now().date()
        for offset in range(6, -1, -1):
            day = today - timedelta(days=offset)
            count = sum(
                1 for item in interviews if item.created_at and item.created_at.date() == day
            )
            snapshot.weekly_activity.append(
                {"Day": day.strftime("%a"), "Interviews": count, "Date": day.isoformat()}
            )

        activity: list[tuple[float, dict]] = []
        for item in interviews:
            score = (
                f"{item.overall_score:.1f}/10" if item.overall_score is not None else "in progress"
            )
            activity.append(
                (
                    _timestamp(item.created_at),
                    {
                        "Type": "Interview",
                        "Activity": f"{item.interview_type or 'Practice'} · {score}",
                        "Date": item.created_at.strftime("%Y-%m-%d") if item.created_at else "",
                    },
                )
            )
        if latest_resume:
            score = (
                f"{latest_resume.overall_resume_score:.0f}/100"
                if latest_resume.overall_resume_score is not None
                else "analyzed"
            )
            activity.append(
                (
                    _timestamp(latest_resume.created_at),
                    {
                        "Type": "Resume",
                        "Activity": f"Resume analyzed · {score}",
                        "Date": (
                            latest_resume.created_at.strftime("%Y-%m-%d")
                            if latest_resume.created_at
                            else ""
                        ),
                    },
                )
            )
        if latest_job:
            activity.append(
                (
                    _timestamp(latest_job.created_at),
                    {
                        "Type": "Job",
                        "Activity": f"Job analyzed · {latest_job.title or 'Untitled role'}",
                        "Date": (
                            latest_job.created_at.strftime("%Y-%m-%d")
                            if latest_job.created_at
                            else ""
                        ),
                    },
                )
            )
        for item in assessments[:4]:
            activity.append(
                (
                    _timestamp(item.created_at),
                    {
                        "Type": "Assessment",
                        "Activity": f"{item.topic or 'Practice'} · {item.score:.1f}/10",
                        "Date": item.created_at.strftime("%Y-%m-%d") if item.created_at else "",
                    },
                )
            )
        for item in events:
            activity.append(
                (
                    _timestamp(item.created_at),
                    {
                        "Type": "Account",
                        "Activity": item.message or item.event_type,
                        "Date": item.created_at.strftime("%Y-%m-%d") if item.created_at else "",
                    },
                )
            )
        snapshot.recent_activity = [
            payload for _, payload in sorted(activity, key=lambda row: row[0], reverse=True)[:8]
        ]

        if not latest_resume:
            snapshot.recommended_actions.append("Analyze your resume to personalize practice.")
        if not interviews:
            snapshot.recommended_actions.append("Complete a job interview to set a baseline.")
        for area in analytics.weak_areas or []:
            snapshot.recommended_actions.append(f"Practice {area} questions this week.")
        if latest_match and latest_match.missing_skills:
            gaps = [
                str(item.get("skill") if isinstance(item, dict) else item)
                for item in latest_match.missing_skills[:3]
            ]
            gaps = [gap for gap in gaps if gap]
            if gaps:
                snapshot.recommended_actions.append("Close gaps: " + ", ".join(gaps) + ".")
        if tasks and snapshot.completed_roadmap_tasks < snapshot.total_roadmap_tasks:
            snapshot.recommended_actions.append(
                f"Continue your study plan · {snapshot.completed_roadmap_tasks}/"
                f"{snapshot.total_roadmap_tasks} tasks complete."
            )
        if not document_count:
            snapshot.recommended_actions.append("Add study documents to ground AI feedback.")

        snapshot.recommended_actions = list(dict.fromkeys(snapshot.recommended_actions))[:5]

    if actor_user_id is not None and str(actor_role or "").lower() == "admin":
        snapshot.upcoming_invitations = []
    elif actor_user_id is not None and session_token:
        try:
            invitations = list_invitations(
                user_id=actor_user_id,
                session_token=session_token,
                limit=50,
            )
        except Exception:
            invitations = []
        for room in invitations:
            if str(room.get("role") or "") != ROLE_CANDIDATE:
                continue
            if str(room.get("status") or "") not in ACTIVE_CALL_STATES:
                continue
            snapshot.upcoming_invitations.append(
                {
                    "Room": str(room.get("room_id") or ""),
                    "Title": str(room.get("title") or "Live interview"),
                    "Status": str(room.get("status") or ""),
                    "Expires": _format_date(room.get("expires_at")),
                }
            )

    notification_rows = list_notifications(session_id, actor_user_id, limit=5)
    snapshot.notification_unread_count = unread_notification_count(session_id, actor_user_id)
    for row in notification_rows:
        snapshot.recent_notifications.append(
            {
                "Title": str(row.get("title") or "Notification"),
                "Message": str(row.get("message") or "")[:120],
                "State": "Unread" if not row.get("is_read") else "Read",
                "Date": _format_date(row.get("created_at")),
            }
        )

    return snapshot
