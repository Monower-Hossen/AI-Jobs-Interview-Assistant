from dataclasses import dataclass, field
from datetime import datetime, timedelta

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


def _timestamp(value) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return 0.0


def get_dashboard_snapshot(session_id: int, actor_user_id: int | None) -> DashboardSnapshot:
    require_owned_session_id(session_id, actor_user_id)
    analytics = get_analytics(session_id, actor_user_id)
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
        snapshot.completed_roadmap_tasks = sum(1 for task in tasks if task.status == "completed")
        snapshot.total_roadmap_tasks = len(tasks)
        snapshot.assessment_count = len(assessments)
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
            snapshot.recommended_actions.append("Complete a mock interview to set a baseline.")
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

    return snapshot
