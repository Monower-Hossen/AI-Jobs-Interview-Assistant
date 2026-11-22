from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from database import (
    InterviewSession,
    LearningActivity,
    LearningResource,
    LearningResourceProgress,
    LearningRoadmap,
    LearningTask,
    Notification,
    User,
    UserAchievement,
    UserSession,
    get_db_context,
    get_readonly_db,
)
from services.auth_service import _require_admin_actor, require_owned_session_id
from services.preferences_service import notification_type_enabled
from sqlalchemy import distinct, func

RESOURCE_CATEGORIES = (
    "technical",
    "behavioral",
    "resume",
    "interview_skills",
    "system_design",
    "communication",
)
RESOURCE_DIFFICULTIES = ("beginner", "intermediate", "advanced")
ADMIN_NOTIFICATION_TYPES = ("announcement", "reminder")
NOTIFICATION_SEVERITIES = ("info", "success", "warning")


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _clean_text(value: object, limit: int, *, required: bool = False) -> str:
    text = " ".join(str(value or "").split())
    if required and not text:
        raise ValueError("This field is required.")
    return text[:limit]


def _clean_long_text(value: object, limit: int = 8000) -> str:
    return str(value or "").strip()[:limit]


def _clean_tags(value: object) -> list[str]:
    if isinstance(value, str):
        raw_items: Iterable[object] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = []
    tags: list[str] = []
    for item in raw_items:
        tag = _clean_text(item, 40)
        if tag and tag.lower() not in {existing.lower() for existing in tags}:
            tags.append(tag)
    return tags[:10]


def _clean_url(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Use a complete http:// or https:// URL.")
    return text[:500]


def _normalize_choice(value: object, choices: tuple[str, ...], label: str) -> str:
    text = _clean_text(value, 60).lower()
    match = next((choice for choice in choices if choice.lower() == text), None)
    if match is None:
        raise ValueError(f"Select a valid {label}.")
    return match


def _normalize_status(value: object) -> str:
    return _normalize_choice(value, ("not_started", "in_progress", "completed"), "status")


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _owned_session(session_id: int, actor_user_id: int | None) -> int:
    return require_owned_session_id(int(session_id), actor_user_id)


def _resource_dict(row: LearningResource) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "title": str(row.title),
        "description": str(row.description or ""),
        "category": str(row.category),
        "skill_tags": list(row.skill_tags or []),
        "difficulty": str(row.difficulty),
        "estimated_minutes": int(row.estimated_minutes or 0),
        "content": str(row.content or ""),
        "url": str(row.url or "") if row.url else "",
        "is_active": bool(row.is_active),
        "created_at": row.created_at,
    }


def _notification_dict(row: Notification) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "notification_type": str(row.notification_type),
        "title": str(row.title),
        "message": str(row.message or ""),
        "severity": str(row.severity or "info"),
        "target_page": str(row.target_page or "") if row.target_page else "",
        "is_read": bool(row.is_read),
        "created_at": row.created_at,
        "read_at": row.read_at,
    }


def _achievement_dict(row: UserAchievement) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "badge_key": str(row.badge_key),
        "title": str(row.title),
        "description": str(row.description or ""),
        "points": int(row.points or 0),
        "earned_at": row.earned_at,
    }


def _add_notification(
    db: Any,
    *,
    session_id: int,
    title: str,
    message: str,
    notification_type: str,
    severity: str = "info",
    target_page: str | None = None,
    event_key: str | None = None,
) -> bool:
    if not notification_type_enabled(
        db, session_id=session_id, notification_type=notification_type, severity=severity
    ):
        return False
    key = _clean_text(event_key, 180) if event_key else None
    if key:
        duplicate = (
            db.query(Notification.id)
            .filter(Notification.session_id == session_id, Notification.event_key == key)
            .first()
        )
        if duplicate:
            return False
    db.add(
        Notification(
            session_id=session_id,
            notification_type=_clean_text(notification_type, 60, required=True),
            title=_clean_text(title, 200, required=True),
            message=_clean_long_text(message, 2000),
            severity=_normalize_choice(severity, NOTIFICATION_SEVERITIES, "severity"),
            target_page=_clean_text(target_page, 80) if target_page else None,
            event_key=key,
            is_read=False,
            created_at=_utcnow(),
        )
    )
    return True


def _calculate_streak(db: Any, session_id: int) -> int:
    rows = (
        db.query(LearningActivity.activity_date)
        .filter(LearningActivity.session_id == session_id)
        .distinct()
        .all()
    )
    days = {converted for row in rows if (converted := _as_date(row[0])) is not None}
    if not days:
        return 0
    today = _utcnow().date()
    cursor = today if today in days else today - timedelta(days=1)
    if cursor not in days:
        return 0
    streak = 0
    while cursor in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _award_badge(
    db: Any,
    *,
    session_id: int,
    badge_key: str,
    title: str,
    description: str,
    points: int,
    earned_badges: list[dict[str, Any]],
) -> None:
    existing = (
        db.query(UserAchievement.id)
        .filter(UserAchievement.session_id == session_id, UserAchievement.badge_key == badge_key)
        .first()
    )
    if existing:
        return
    badge = UserAchievement(
        session_id=session_id,
        badge_key=badge_key,
        title=title,
        description=description,
        points=int(points),
        earned_at=_utcnow(),
    )
    db.add(badge)
    db.flush()
    earned_badges.append(_achievement_dict(badge))


def create_notification(
    *,
    session_id: int,
    actor_user_id: int | None,
    title: str,
    message: str,
    notification_type: str = "system",
    severity: str = "info",
    target_page: str = "learning_center",
    event_key: str | None = None,
) -> bool:
    owned_session_id = _owned_session(session_id, actor_user_id)
    with get_db_context() as db:
        created = _add_notification(
            db,
            session_id=owned_session_id,
            title=title,
            message=message,
            notification_type=notification_type,
            severity=severity,
            target_page=target_page,
            event_key=event_key,
        )
    return created


def log_activity(
    *,
    session_id: int,
    actor_user_id: int | None,
    activity_type: str,
    description: str,
    points: int = 0,
    event_key: str | None = None,
) -> dict[str, Any]:
    owned_session_id = _owned_session(session_id, actor_user_id)
    now = _utcnow()
    key = _clean_text(event_key or activity_type, 160, required=True)
    earned_badges: list[dict[str, Any]] = []
    with get_db_context() as db:
        duplicate = (
            db.query(LearningActivity.id)
            .filter(
                LearningActivity.session_id == owned_session_id,
                LearningActivity.activity_key == key,
            )
            .first()
        )
        if duplicate:
            return {
                "activity_recorded": False,
                "points": 0,
                "streak": _calculate_streak(db, owned_session_id),
                "achievements": earned_badges,
            }
        db.add(
            LearningActivity(
                session_id=owned_session_id,
                activity_date=now.date(),
                activity_type=_clean_text(activity_type, 60, required=True),
                activity_key=key,
                description=_clean_long_text(description, 1000),
                points=max(0, int(points)),
                created_at=now,
            )
        )
        db.flush()
        activity_count = (
            db.query(func.count(LearningActivity.id))
            .filter(LearningActivity.session_id == owned_session_id)
            .scalar()
            or 0
        )
        streak = _calculate_streak(db, owned_session_id)
        _award_badge(
            db,
            session_id=owned_session_id,
            badge_key="first_step",
            title="First step",
            description="Completed your first tracked learning activity.",
            points=5,
            earned_badges=earned_badges,
        )
        if activity_type == "task_completed":
            _award_badge(
                db,
                session_id=owned_session_id,
                badge_key="task_finisher",
                title="Task finisher",
                description="Completed a study plan task.",
                points=10,
                earned_badges=earned_badges,
            )
        if streak >= 3:
            _award_badge(
                db,
                session_id=owned_session_id,
                badge_key="three_day_streak",
                title="Three-day streak",
                description="Practiced on three consecutive days.",
                points=20,
                earned_badges=earned_badges,
            )
        if int(activity_count) >= 10:
            _award_badge(
                db,
                session_id=owned_session_id,
                badge_key="ten_activities",
                title="Dedicated learner",
                description="Completed ten tracked learning activities.",
                points=25,
                earned_badges=earned_badges,
            )
        if activity_type == "resource_completed":
            completed_resources = (
                db.query(func.count(LearningResourceProgress.id))
                .filter(
                    LearningResourceProgress.session_id == owned_session_id,
                    LearningResourceProgress.status == "completed",
                )
                .scalar()
                or 0
            )
            if int(completed_resources) >= 3:
                _award_badge(
                    db,
                    session_id=owned_session_id,
                    badge_key="resource_explorer",
                    title="Resource explorer",
                    description="Completed three learning resources.",
                    points=20,
                    earned_badges=earned_badges,
                )
        for badge in earned_badges:
            _add_notification(
                db,
                session_id=owned_session_id,
                title=f"Achievement unlocked: {badge['title']}",
                message=str(badge["description"]),
                notification_type="achievement",
                severity="success",
                target_page="learning_center",
                event_key=f"achievement:{owned_session_id}:{badge['badge_key']}",
            )
    return {
        "activity_recorded": True,
        "points": max(0, int(points)),
        "streak": streak,
        "achievements": earned_badges,
    }


def record_roadmap_created(
    roadmap_id: int,
    title: str,
    focus_skills: list[str],
    session_id: int,
    actor_user_id: int | None,
) -> None:
    create_notification(
        session_id=session_id,
        actor_user_id=actor_user_id,
        title="Study plan ready",
        message=f"Your study plan '{title}' is ready with {len(focus_skills)} focus areas.",
        notification_type="learning",
        severity="success",
        target_page="learning_roadmap",
        event_key=f"roadmap_created:{roadmap_id}",
    )
    log_activity(
        session_id=session_id,
        actor_user_id=actor_user_id,
        activity_type="roadmap_created",
        description=f"Created study plan: {title}",
        points=5,
        event_key=f"roadmap_created:{roadmap_id}",
    )


def record_task_completion(
    task_id: int,
    roadmap_id: int,
    title: str,
    session_id: int,
    actor_user_id: int | None,
) -> None:
    create_notification(
        session_id=session_id,
        actor_user_id=actor_user_id,
        title="Study task completed",
        message=f"Completed: {title}",
        notification_type="learning",
        severity="success",
        target_page="learning_roadmap",
        event_key=f"task_complete:{task_id}",
    )
    log_activity(
        session_id=session_id,
        actor_user_id=actor_user_id,
        activity_type="task_completed",
        description=f"Completed study task: {title}",
        points=10,
        event_key=f"task_completed:{task_id}",
    )
    with get_readonly_db() as db:
        total = (
            db.query(func.count(LearningTask.id))
            .filter(LearningTask.roadmap_id == roadmap_id)
            .scalar()
            or 0
        )
        done = (
            db.query(func.count(LearningTask.id))
            .filter(LearningTask.roadmap_id == roadmap_id, LearningTask.status == "completed")
            .scalar()
            or 0
        )
    if total and int(total) == int(done):
        create_notification(
            session_id=session_id,
            actor_user_id=actor_user_id,
            title="Study plan complete",
            message="Every task in one of your study plans is complete. Great work.",
            notification_type="achievement",
            severity="success",
            target_page="learning_center",
            event_key=f"roadmap_complete:{roadmap_id}",
        )
        log_activity(
            session_id=session_id,
            actor_user_id=actor_user_id,
            activity_type="roadmap_completed",
            description="Completed every task in a study plan.",
            points=25,
            event_key=f"roadmap_completed:{roadmap_id}",
        )


def refresh_task_reminders(session_id: int, actor_user_id: int | None) -> int:
    owned_session_id = _owned_session(session_id, actor_user_id)
    now = _utcnow()
    due_tasks: list[tuple[int, str, datetime]] = []
    with get_readonly_db() as db:
        rows = (
            db.query(LearningTask, LearningRoadmap)
            .join(LearningRoadmap, LearningTask.roadmap_id == LearningRoadmap.id)
            .filter(
                LearningRoadmap.session_id == owned_session_id,
                LearningTask.status != "completed",
            )
            .all()
        )
        for task, roadmap in rows:
            if not roadmap.created_at:
                continue
            due_at = roadmap.created_at + timedelta(days=int(task.order_index or 0) + 1)
            if due_at <= now + timedelta(days=1):
                due_tasks.append((int(task.id), str(task.title), due_at))
    created = 0
    for task_id, title, due_at in due_tasks:
        status = "due now" if due_at <= now else "due soon"
        if create_notification(
            session_id=owned_session_id,
            actor_user_id=actor_user_id,
            title=f"Study task {status}",
            message=f"'{title}' is {status} (target: {due_at:%Y-%m-%d}).",
            notification_type="task_reminder",
            severity="warning" if due_at <= now else "info",
            target_page="learning_roadmap",
            event_key=f"task_reminder:{task_id}",
        ):
            created += 1
    return created


def list_resources_for_user(session_id: int, actor_user_id: int | None) -> list[dict[str, Any]]:
    owned_session_id = _owned_session(session_id, actor_user_id)
    with get_readonly_db() as db:
        rows = (
            db.query(LearningResource)
            .filter(LearningResource.is_active.is_(True))
            .order_by(
                LearningResource.category,
                LearningResource.difficulty,
                LearningResource.title,
            )
            .all()
        )
        progress = {
            int(row.resource_id): row
            for row in db.query(LearningResourceProgress)
            .filter(LearningResourceProgress.session_id == owned_session_id)
            .all()
        }
    result: list[dict[str, Any]] = []
    for row in rows:
        item = _resource_dict(row)
        tracked = progress.get(int(row.id))
        item["progress_percent"] = int(tracked.progress_percent or 0) if tracked else 0
        item["status"] = str(tracked.status) if tracked else "not_started"
        result.append(item)
    return result


def get_recommended_resources(
    session_id: int, actor_user_id: int | None, limit: int = 4
) -> list[dict[str, Any]]:
    owned_session_id = _owned_session(session_id, actor_user_id)
    with get_readonly_db() as db:
        focus: list[str] = []
        roadmap = (
            db.query(LearningRoadmap)
            .filter(LearningRoadmap.session_id == owned_session_id)
            .order_by(LearningRoadmap.created_at.desc())
            .first()
        )
        if roadmap:
            focus.extend(str(item) for item in roadmap.focus_skills or [])
        interview = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.session_id == owned_session_id,
                InterviewSession.status == "completed",
            )
            .order_by(InterviewSession.completed_at.desc())
            .first()
        )
        if interview:
            focus.extend(str(item) for item in interview.weaknesses or [])
            focus.extend(str(item) for item in interview.recommended_topics or [])
        resources = (
            db.query(LearningResource).filter(LearningResource.is_active.is_(True)).all()
        )
    normalized_focus = [item.strip().lower() for item in focus if item.strip()]
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in resources:
        item = _resource_dict(row)
        tags = [str(tag).strip().lower() for tag in item["skill_tags"] if str(tag).strip()]
        matched = [
            skill
            for skill in normalized_focus
            if any(skill in tag or tag in skill for tag in tags)
        ]
        score = len(matched) * 4 + (1 if any(skill in item["category"] for skill in normalized_focus) else 0)
        item["match_reason"] = (
            "Matches your focus areas: " + ", ".join(matched[:3]) if matched else "Recommended starter content"
        )
        scored.append((score, item))
    scored.sort(key=lambda entry: (-entry[0], entry[1]["estimated_minutes"], entry[1]["title"]))
    return [item for _, item in scored[: max(1, min(int(limit), 12))]]


def record_resource_progress(
    *,
    resource_id: int,
    session_id: int,
    actor_user_id: int | None,
    progress_percent: int,
    status: str = "in_progress",
) -> dict[str, Any]:
    owned_session_id = _owned_session(session_id, actor_user_id)
    now = _utcnow()
    requested_status = _normalize_status(status)
    try:
        percent = max(0, min(100, int(progress_percent)))
    except (TypeError, ValueError) as exc:
        raise ValueError("Progress must be a whole number.") from exc
    if percent >= 100 or requested_status == "completed":
        percent = 100
        final_status = "completed"
    elif percent > 0 or requested_status == "in_progress":
        final_status = "in_progress"
    else:
        final_status = "not_started"
    newly_completed = False
    title = ""
    with get_db_context() as db:
        resource = (
            db.query(LearningResource)
            .filter(LearningResource.id == resource_id, LearningResource.is_active.is_(True))
            .first()
        )
        if resource is None:
            raise ValueError("Learning resource is not available.")
        title = str(resource.title)
        progress = (
            db.query(LearningResourceProgress)
            .filter(
                LearningResourceProgress.session_id == owned_session_id,
                LearningResourceProgress.resource_id == resource_id,
            )
            .first()
        )
        if progress is None:
            progress = LearningResourceProgress(
                session_id=owned_session_id,
                resource_id=resource_id,
                progress_percent=0,
                status="not_started",
                created_at=now,
            )
            db.add(progress)
        already_complete = progress.status == "completed"
        progress.progress_percent = percent
        progress.status = final_status
        progress.last_viewed_at = now
        progress.updated_at = now
        if final_status == "completed":
            progress.completed_at = progress.completed_at or now
            newly_completed = not already_complete
        else:
            progress.completed_at = None
    if newly_completed:
        create_notification(
            session_id=owned_session_id,
            actor_user_id=actor_user_id,
            title="Learning resource completed",
            message=f"Completed: {title}",
            notification_type="resource_completed",
            severity="success",
            target_page="learning_center",
            event_key=f"resource_completed:{resource_id}",
        )
        log_activity(
            session_id=owned_session_id,
            actor_user_id=actor_user_id,
            activity_type="resource_completed",
            description=f"Completed resource: {title}",
            points=10,
            event_key=f"resource_completed:{resource_id}",
        )
    return {"progress_percent": percent, "status": final_status, "completed": newly_completed}


def list_resource_progress(session_id: int, actor_user_id: int | None) -> list[dict[str, Any]]:
    owned_session_id = _owned_session(session_id, actor_user_id)
    with get_readonly_db() as db:
        rows = (
            db.query(LearningResourceProgress, LearningResource)
            .join(LearningResource, LearningResourceProgress.resource_id == LearningResource.id)
            .filter(LearningResourceProgress.session_id == owned_session_id)
            .order_by(LearningResourceProgress.updated_at.desc())
            .all()
        )
    return [
        {
            "resource_id": int(resource.id),
            "title": str(resource.title),
            "category": str(resource.category),
            "status": str(progress.status),
            "progress_percent": int(progress.progress_percent or 0),
            "completed_at": progress.completed_at,
            "updated_at": progress.updated_at,
        }
        for progress, resource in rows
    ]


def get_learning_snapshot(session_id: int, actor_user_id: int | None) -> dict[str, Any]:
    owned_session_id = _owned_session(session_id, actor_user_id)
    with get_readonly_db() as db:
        total_tasks = (
            db.query(func.count(LearningTask.id))
            .join(LearningRoadmap, LearningTask.roadmap_id == LearningRoadmap.id)
            .filter(LearningRoadmap.session_id == owned_session_id)
            .scalar()
            or 0
        )
        done_tasks = (
            db.query(func.count(LearningTask.id))
            .join(LearningRoadmap, LearningTask.roadmap_id == LearningRoadmap.id)
            .filter(
                LearningRoadmap.session_id == owned_session_id,
                LearningTask.status == "completed",
            )
            .scalar()
            or 0
        )
        completed_resources = (
            db.query(func.count(LearningResourceProgress.id))
            .filter(
                LearningResourceProgress.session_id == owned_session_id,
                LearningResourceProgress.status == "completed",
            )
            .scalar()
            or 0
        )
        activity_points = (
            db.query(func.coalesce(func.sum(LearningActivity.points), 0))
            .filter(LearningActivity.session_id == owned_session_id)
            .scalar()
            or 0
        )
        badge_points = (
            db.query(func.coalesce(func.sum(UserAchievement.points), 0))
            .filter(UserAchievement.session_id == owned_session_id)
            .scalar()
            or 0
        )
        activities = (
            db.query(func.count(LearningActivity.id))
            .filter(LearningActivity.session_id == owned_session_id)
            .scalar()
            or 0
        )
        unread = (
            db.query(func.count(Notification.id))
            .filter(Notification.session_id == owned_session_id, Notification.is_read.is_(False))
            .scalar()
            or 0
        )
        badges = (
            db.query(UserAchievement)
            .filter(UserAchievement.session_id == owned_session_id)
            .order_by(UserAchievement.earned_at.desc())
            .all()
        )
        streak = _calculate_streak(db, owned_session_id)
    return {
        "total_tasks": int(total_tasks),
        "done_tasks": int(done_tasks),
        "completed_resources": int(completed_resources),
        "points": int(activity_points) + int(badge_points),
        "activities": int(activities),
        "unread_notifications": int(unread),
        "streak": streak,
        "achievements": [_achievement_dict(badge) for badge in badges],
    }


def get_achievements(session_id: int, actor_user_id: int | None) -> list[dict[str, Any]]:
    owned_session_id = _owned_session(session_id, actor_user_id)
    with get_readonly_db() as db:
        rows = (
            db.query(UserAchievement)
            .filter(UserAchievement.session_id == owned_session_id)
            .order_by(UserAchievement.earned_at.desc())
            .all()
        )
    return [_achievement_dict(row) for row in rows]


def list_notifications(
    session_id: int,
    actor_user_id: int | None,
    *,
    unread_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    owned_session_id = _owned_session(session_id, actor_user_id)
    query_limit = max(1, min(int(limit), 100))
    with get_readonly_db() as db:
        query = db.query(Notification).filter(Notification.session_id == owned_session_id)
        if unread_only:
            query = query.filter(Notification.is_read.is_(False))
        rows = query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(
            query_limit
        ).all()
    return [_notification_dict(row) for row in rows]


def unread_notification_count(session_id: int, actor_user_id: int | None) -> int:
    owned_session_id = _owned_session(session_id, actor_user_id)
    with get_readonly_db() as db:
        count = (
            db.query(func.count(Notification.id))
            .filter(Notification.session_id == owned_session_id, Notification.is_read.is_(False))
            .scalar()
            or 0
        )
    return int(count)


def mark_notification_read(
    notification_id: int, session_id: int, actor_user_id: int | None
) -> None:
    owned_session_id = _owned_session(session_id, actor_user_id)
    with get_db_context() as db:
        row = (
            db.query(Notification)
            .filter(Notification.id == notification_id, Notification.session_id == owned_session_id)
            .first()
        )
        if row is None:
            raise ValueError("Notification not found.")
        row.is_read = True
        row.read_at = row.read_at or _utcnow()


def mark_all_notifications_read(session_id: int, actor_user_id: int | None) -> int:
    owned_session_id = _owned_session(session_id, actor_user_id)
    now = _utcnow()
    with get_db_context() as db:
        updated = (
            db.query(Notification)
            .filter(Notification.session_id == owned_session_id, Notification.is_read.is_(False))
            .update({Notification.is_read: True, Notification.read_at: now}, synchronize_session=False)
        )
    return int(updated or 0)


def delete_notification(notification_id: int, session_id: int, actor_user_id: int | None) -> None:
    owned_session_id = _owned_session(session_id, actor_user_id)
    with get_db_context() as db:
        row = (
            db.query(Notification)
            .filter(Notification.id == notification_id, Notification.session_id == owned_session_id)
            .first()
        )
        if row is None:
            raise ValueError("Notification not found.")
        db.delete(row)


def _validate_resource_payload(payload: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    validated: dict[str, Any] = {}
    if not partial or "title" in payload:
        validated["title"] = _clean_text(payload.get("title"), 200, required=True)
    if not partial or "description" in payload:
        validated["description"] = _clean_long_text(payload.get("description"), 2000)
    if not partial or "category" in payload:
        validated["category"] = _normalize_choice(
            payload.get("category"), RESOURCE_CATEGORIES, "category"
        )
    if not partial or "difficulty" in payload:
        validated["difficulty"] = _normalize_choice(
            payload.get("difficulty"), RESOURCE_DIFFICULTIES, "difficulty"
        )
    if not partial or "estimated_minutes" in payload:
        try:
            minutes = int(payload.get("estimated_minutes") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Estimated duration must be a whole number.") from exc
        if not 5 <= minutes <= 600:
            raise ValueError("Estimated duration must be between 5 and 600 minutes.")
        validated["estimated_minutes"] = minutes
    if not partial or "skill_tags" in payload:
        validated["skill_tags"] = _clean_tags(payload.get("skill_tags"))
    if not partial or "content" in payload:
        validated["content"] = _clean_long_text(payload.get("content"), 8000)
    if not partial or "url" in payload:
        validated["url"] = _clean_url(payload.get("url"))
    return validated


def _active_session_ids(db: Any) -> list[int]:
    rows = (
        db.query(UserSession.id)
        .join(User, User.id == UserSession.user_id)
        .filter(User.is_active.is_(True), User.account_status == "active")
        .all()
    )
    return [int(row[0]) for row in rows]


def _broadcast_notification(
    *,
    title: str,
    message: str,
    notification_type: str,
    severity: str,
    target_page: str,
    event_key: str,
) -> int:
    with get_db_context() as db:
        recipients = 0
        for session_id in _active_session_ids(db):
            if _add_notification(
                db,
                session_id=session_id,
                title=title,
                message=message,
                notification_type=notification_type,
                severity=severity,
                target_page=target_page,
                event_key=f"{event_key}:{session_id}",
            ):
                recipients += 1
    return recipients


def admin_list_resources(
    actor_admin_id: int | None, *, include_inactive: bool = False
) -> list[dict[str, Any]]:
    with get_readonly_db() as db:
        _require_admin_actor(db, actor_admin_id)
        query = db.query(LearningResource)
        if not include_inactive:
            query = query.filter(LearningResource.is_active.is_(True))
        rows = query.order_by(LearningResource.category, LearningResource.title).all()
    return [_resource_dict(row) for row in rows]


def admin_create_resource(actor_admin_id: int | None, payload: dict[str, Any]) -> int:
    values = _validate_resource_payload(payload)
    with get_db_context() as db:
        admin = _require_admin_actor(db, actor_admin_id)
        row = LearningResource(
            created_by_admin_id=int(admin.id),
            is_active=True,
            created_at=_utcnow(),
            updated_at=_utcnow(),
            **values,
        )
        db.add(row)
        db.flush()
        resource_id = int(row.id)
    _broadcast_notification(
        title="New learning resource",
        message=f"'{values['title']}' was added to the learning library.",
        notification_type="new_resource",
        severity="info",
        target_page="learning_center",
        event_key=f"new_resource:{resource_id}",
    )
    return resource_id


def admin_update_resource(
    actor_admin_id: int | None, resource_id: int, payload: dict[str, Any]
) -> None:
    values = _validate_resource_payload(payload, partial=True)
    with get_db_context() as db:
        _require_admin_actor(db, actor_admin_id)
        row = db.query(LearningResource).filter(LearningResource.id == resource_id).first()
        if row is None:
            raise ValueError("Learning resource not found.")
        for field, value in values.items():
            setattr(row, field, value)
        row.updated_at = _utcnow()


def admin_set_resource_active(
    actor_admin_id: int | None, resource_id: int, *, is_active: bool
) -> None:
    with get_db_context() as db:
        _require_admin_actor(db, actor_admin_id)
        row = db.query(LearningResource).filter(LearningResource.id == resource_id).first()
        if row is None:
            raise ValueError("Learning resource not found.")
        row.is_active = bool(is_active)
        row.updated_at = _utcnow()


def admin_broadcast_notification(
    actor_admin_id: int | None,
    *,
    title: str,
    message: str,
    notification_type: str = "announcement",
    severity: str = "info",
    target_page: str = "learning_center",
    campaign_key: str | None = None,
) -> int:
    with get_db_context() as db:
        _require_admin_actor(db, actor_admin_id)
    clean_type = _normalize_choice(
        notification_type, ADMIN_NOTIFICATION_TYPES, "notification type"
    )
    clean_severity = _normalize_choice(severity, NOTIFICATION_SEVERITIES, "severity")
    key = _clean_text(campaign_key, 80) or uuid.uuid4().hex[:12]
    return _broadcast_notification(
        title=_clean_text(title, 200, required=True),
        message=_clean_long_text(message, 2000),
        notification_type=f"admin_{clean_type}",
        severity=clean_severity,
        target_page=_clean_text(target_page, 80, required=True),
        event_key=f"admin_{clean_type}:{key}",
    )


def admin_learning_analytics(actor_admin_id: int | None) -> dict[str, Any]:
    with get_readonly_db() as db:
        _require_admin_actor(db, actor_admin_id)
        active_resources = (
            db.query(func.count(LearningResource.id))
            .filter(LearningResource.is_active.is_(True))
            .scalar()
            or 0
        )
        archived_resources = (
            db.query(func.count(LearningResource.id))
            .filter(LearningResource.is_active.is_(False))
            .scalar()
            or 0
        )
        by_category_rows = (
            db.query(LearningResource.category, func.count(LearningResource.id))
            .filter(LearningResource.is_active.is_(True))
            .group_by(LearningResource.category)
            .all()
        )
        progress_rows = (
            db.query(LearningResourceProgress.status, func.count(LearningResourceProgress.id))
            .group_by(LearningResourceProgress.status)
            .all()
        )
        active_learners = (
            db.query(func.count(distinct(LearningResourceProgress.session_id)))
            .scalar()
            or 0
        )
        achievements = (
            db.query(func.count(UserAchievement.id)).scalar() or 0
        )
        notifications = db.query(func.count(Notification.id)).scalar() or 0
        unread = (
            db.query(func.count(Notification.id))
            .filter(Notification.is_read.is_(False))
            .scalar()
            or 0
        )
    return {
        "active_resources": int(active_resources),
        "archived_resources": int(archived_resources),
        "resources_by_category": {str(key): int(value) for key, value in by_category_rows},
        "progress_by_status": {str(key): int(value) for key, value in progress_rows},
        "active_learners": int(active_learners),
        "achievements": int(achievements),
        "notifications": int(notifications),
        "unread_notifications": int(unread),
    }


def admin_notification_broadcasts(actor_admin_id: int | None) -> list[dict[str, Any]]:
    with get_readonly_db() as db:
        _require_admin_actor(db, actor_admin_id)
        rows = (
            db.query(Notification)
            .filter(
                Notification.notification_type.in_(
                    ["admin_announcement", "admin_reminder", "new_resource"]
                )
            )
            .order_by(Notification.created_at.desc())
            .all()
        )
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.event_key or f"legacy:{row.id}")
        entry = grouped.setdefault(
            key,
            {
                "campaign": key,
                "title": str(row.title),
                "type": str(row.notification_type),
                "created_at": row.created_at,
                "recipients": 0,
            },
        )
        entry["recipients"] = int(entry["recipients"]) + 1
    return sorted(grouped.values(), key=lambda item: str(item["campaign"]), reverse=True)[:20]


def admin_notification_summary(actor_admin_id: int | None) -> dict[str, Any]:
    with get_readonly_db() as db:
        _require_admin_actor(db, actor_admin_id)
        rows = (
            db.query(Notification.notification_type, func.count(Notification.id))
            .group_by(Notification.notification_type)
            .all()
        )
        total = db.query(func.count(Notification.id)).scalar() or 0
    return {
        "total": int(total),
        "by_type": {str(key): int(value) for key, value in rows},
    }
