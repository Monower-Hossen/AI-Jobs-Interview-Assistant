"""Analytics computed strictly from persisted interview records."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd
from database import (
    GeneratedReport,
    InterviewQuestion,
    InterviewSession,
    User,
    UserSession,
    get_db_context,
    get_readonly_db,
)
from services.auth_service import _require_admin_actor, require_owned_session_id
from sqlalchemy import func


@dataclass
class AnalyticsSnapshot:
    total_interviews: int = 0
    completed_interviews: int = 0
    average_score: float = 0.0
    best_score: float = 0.0
    lowest_score: float = 0.0
    technical_score: float = 0.0
    communication_score: float = 0.0
    problem_solving_score: float = 0.0
    confidence_score: float = 0.0
    readiness_score: float = 0.0
    completion_rate: float = 0.0
    latest_score: float = 0.0
    previous_score: float = 0.0
    progress_percent: float = 0.0
    performance_improvement_percent: float = 0.0
    total_answered: int = 0
    trend: pd.DataFrame | None = None
    category_scores: pd.DataFrame | None = None
    skill_breakdown: dict[str, float] | None = None
    question_scores: pd.DataFrame | None = None
    strong_areas: list[str] | None = None
    weak_areas: list[str] | None = None
    recommendations: list[str] | None = None
    recent_sessions: list[dict] | None = None
    match_score: float | None = None
    scored_interviews: int = 0
    practice_hours: float = 0.0


def _average(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def get_analytics(
    session_id: int,
    actor_user_id: int | None,
    date_from: date | None = None,
    date_to: date | None = None,
    interview_type: str | None = None,
    difficulty: str | None = None,
    minimum_score: float | None = None,
    maximum_score: float | None = None,
) -> AnalyticsSnapshot:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        query = (
            db.query(InterviewSession)
            .filter(InterviewSession.session_id == session_id)
            .order_by(InterviewSession.created_at.asc())
        )
        if date_from is not None:
            query = query.filter(func.date(InterviewSession.created_at) >= date_from)
        if date_to is not None:
            query = query.filter(func.date(InterviewSession.created_at) <= date_to)
        if interview_type is not None:
            query = query.filter(InterviewSession.interview_type == interview_type)
        if difficulty is not None:
            query = query.filter(InterviewSession.difficulty == difficulty)
        sessions = query.all()
        completed = [session for session in sessions if session.status == "completed"]
        if minimum_score is not None:
            completed = [
                s
                for s in completed
                if s.overall_score is not None and s.overall_score >= minimum_score
            ]
        if maximum_score is not None:
            completed = [
                s
                for s in completed
                if s.overall_score is not None and s.overall_score <= maximum_score
            ]

        score_values = [
            session.overall_score for session in completed if session.overall_score is not None
        ]
        technical = [
            session.technical_score for session in completed if session.technical_score is not None
        ]
        communication = [
            session.communication_score
            for session in completed
            if session.communication_score is not None
        ]
        problem_solving = [
            session.problem_solving_score
            for session in completed
            if session.problem_solving_score is not None
        ]

        practice_hours = round(
            sum((item.duration_seconds or 0) for item in completed) / 3600, 1
        )

        session_ids = [session.id for session in sessions]
        questions = (
            db.query(InterviewQuestion)
            .filter(InterviewQuestion.interview_session_id.in_(session_ids))
            .all()
            if session_ids
            else []
        )
        answered = [question for question in questions if question.overall_score is not None]

        category_buckets: dict[str, list[float]] = {}
        skill_buckets: dict[str, list[float]] = {}
        for question in answered:
            category = question.category or "other"
            category_buckets.setdefault(category, []).append(float(question.overall_score))
            for skill_field, skill_name in [
                ("technical_accuracy", "technical"),
                ("communication", "communication"),
                ("confidence", "confidence"),
                ("problem_solving", "problem_solving"),
                ("structure", "structure"),
            ]:
                val = getattr(question, skill_field, None)
                if val is not None:
                    skill_buckets.setdefault(skill_name, []).append(float(val))

        category_means = {key: _average(values) for key, values in category_buckets.items()}
        skill_means = {key: _average(values) for key, values in skill_buckets.items()}
        ordered_skills = sorted(skill_means.items(), key=lambda item: item[1], reverse=True)

        trend_rows = [
            {
                "Session": f"#{index}",
                "Overall": session.overall_score or 0.0,
                "Technical": session.technical_score or 0.0,
                "Communication": session.communication_score or 0.0,
            }
            for index, session in enumerate(completed, 1)
        ]
        recommendations: list[str] = []
        for session in reversed(completed):
            for topic in session.recommended_topics or []:
                if topic not in recommendations:
                    recommendations.append(topic)
            if len(recommendations) >= 6:
                break

        latest = score_values[-1] if score_values else 0.0
        previous = score_values[-2] if len(score_values) > 1 else 0.0
        progress = round(((latest - previous) / previous) * 100, 1) if previous else 0.0

        first_score = score_values[0] if score_values else None
        if first_score is not None and len(score_values) >= 2 and first_score != 0:
            improvement = round(((latest - first_score) / abs(first_score)) * 100, 1)
        else:
            improvement = 0.0

        avg_score = _average(score_values)
        avg_technical = _average(technical)
        avg_communication = _average(communication)
        avg_problem = _average(problem_solving)
        readiness = round(
            avg_score * 0.4 + avg_technical * 0.25 + avg_communication * 0.2 + avg_problem * 0.15,
            2,
        )

        best_score = max(score_values) if score_values else 0.0
        lowest_score = min(score_values) if score_values else 0.0

        category_df = pd.DataFrame(
            {"Category": list(category_means), "Score": list(category_means.values())}
        ) if category_means else pd.DataFrame({"Category": [], "Score": []})

        skill_df: pd.DataFrame | None = None
        if skill_means:
            skill_df = pd.DataFrame(
                {"Skill": list(skill_means), "Score": list(skill_means.values())}
            )
            skill_df = skill_df.sort_values("Score", ascending=False)

        strong_areas = [name for name, score in ordered_skills if score >= 7][:3]
        weak_areas = [name for name, score in ordered_skills if score < 7][-3:]

        recent_sessions = [
            {
                "ID": session.id,
                "Type": session.interview_type or "N/A",
                "Difficulty": session.difficulty or "N/A",
                "Score": session.overall_score,
                "Status": session.status,
                "Date": session.created_at.strftime("%Y-%m-%d") if session.created_at else "",
            }
            for session in sorted(
                sessions, key=lambda item: item.created_at or item.id, reverse=True
            )[:10]
        ]

        return AnalyticsSnapshot(
            total_interviews=len(sessions),
            completed_interviews=len(completed),
            average_score=avg_score,
            best_score=best_score,
            lowest_score=lowest_score,
            technical_score=avg_technical,
            communication_score=avg_communication,
            problem_solving_score=avg_problem,
            confidence_score=_average(list(skill_means.values())) if skill_means else 0.0,
            readiness_score=readiness,
            completion_rate=round(len(completed) / len(sessions) * 100, 1) if sessions else 0.0,
            latest_score=latest,
            previous_score=previous,
            progress_percent=progress,
            performance_improvement_percent=improvement,
            total_answered=len(answered),
            trend=pd.DataFrame(trend_rows) if trend_rows else pd.DataFrame(),
            category_scores=category_df,
            skill_breakdown=skill_df,
            strong_areas=strong_areas,
            weak_areas=weak_areas,
            recommendations=recommendations,
            recent_sessions=recent_sessions,
            match_score=None,
            scored_interviews=len(score_values),
            practice_hours=practice_hours,
        )


@dataclass
class PlatformReportRow:
    interview_id: int
    workspace_id: int
    user_id: int | None
    user_email: str
    user_name: str
    interview_date: str
    interview_type: str
    difficulty: str
    status: str
    overall_score: float | None
    technical_score: float | None
    communication_score: float | None
    problem_solving_score: float | None
    total_questions: int
    answered_questions: int


@dataclass
class PlatformReportSnapshot:
    start_date: date | None = None
    end_date: date | None = None
    total_interviews: int = 0
    completed_interviews: int = 0
    unique_users: int = 0
    total_questions: int = 0
    answered_questions: int = 0
    average_score: float = 0.0
    completion_rate: float = 0.0
    stored_reports: int = 0
    rows: list[PlatformReportRow] = field(default_factory=list)


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _score(value: Any) -> float | None:
    return float(value) if value is not None else None


def get_platform_report_date_range(
    actor_admin_id: int | None,
) -> tuple[date | None, date | None]:
    with get_readonly_db() as db:
        _require_admin_actor(db, actor_admin_id)
        earliest, latest = db.query(
            func.min(InterviewSession.created_at),
            func.max(InterviewSession.created_at),
        ).one()
        return _as_date(earliest), _as_date(latest)


def get_platform_report(
    actor_admin_id: int | None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> PlatformReportSnapshot:
    with get_readonly_db() as db:
        _require_admin_actor(db, actor_admin_id)
        query = (
            db.query(InterviewSession, UserSession, User)
            .join(UserSession, InterviewSession.session_id == UserSession.id)
            .outerjoin(User, UserSession.user_id == User.id)
        )
        if start_date is not None:
            query = query.filter(func.date(InterviewSession.created_at) >= start_date)
        if end_date is not None:
            query = query.filter(func.date(InterviewSession.created_at) <= end_date)
        records = query.order_by(InterviewSession.created_at.desc()).all()

        session_ids = [interview.id for interview, _, _ in records]
        question_counts: dict[int, int] = {}
        answered_counts: dict[int, int] = {}
        stored_reports = 0
        if session_ids:
            questions = (
                db.query(InterviewQuestion)
                .filter(InterviewQuestion.interview_session_id.in_(session_ids))
                .all()
            )
            for question in questions:
                question_session_id = question.interview_session_id
                question_counts[question_session_id] = question_counts.get(question_session_id, 0) + 1
                if question.overall_score is not None:
                    answered_counts[question_session_id] = (
                        answered_counts.get(question_session_id, 0) + 1
                    )
            stored_reports = (
                db.query(GeneratedReport)
                .filter(GeneratedReport.interview_session_id.in_(session_ids))
                .count()
            )

        rows = [
            PlatformReportRow(
                interview_id=interview.id,
                workspace_id=workspace.id,
                user_id=workspace.user_id,
                user_email=user.email if user else "Unassigned",
                user_name=user.full_name if user else "Anonymous workspace",
                interview_date=(
                    interview.created_at.strftime("%Y-%m-%d") if interview.created_at else ""
                ),
                interview_type=interview.interview_type or "N/A",
                difficulty=interview.difficulty or "N/A",
                status=interview.status or "N/A",
                overall_score=_score(interview.overall_score),
                technical_score=_score(interview.technical_score),
                communication_score=_score(interview.communication_score),
                problem_solving_score=_score(interview.problem_solving_score),
                total_questions=question_counts.get(interview.id, 0),
                answered_questions=answered_counts.get(interview.id, 0),
            )
            for interview, workspace, user in records
        ]
        completed = [row for row in rows if row.status == "completed"]
        scores = [row.overall_score for row in completed if row.overall_score is not None]
        user_ids = {row.user_id for row in rows if row.user_id is not None}

        return PlatformReportSnapshot(
            start_date=start_date,
            end_date=end_date,
            total_interviews=len(rows),
            completed_interviews=len(completed),
            unique_users=len(user_ids),
            total_questions=sum(question_counts.values()),
            answered_questions=sum(answered_counts.values()),
            average_score=_average(scores),
            completion_rate=round(len(completed) / len(rows) * 100, 1) if rows else 0.0,
            stored_reports=stored_reports,
            rows=rows,
        )
