from __future__ import annotations

from typing import Any

from database.db import get_db_context
from database.models import JobPost, JobSkill
from services.auth_service import log_event, require_admin
from services.job_interview_constants import (
    DIFFICULTIES,
    INTERVIEW_CATEGORIES,
)


class JobPostError(ValueError):
    """Raised when job posting data is invalid."""


def _clean_text(value: Any, field: str, *, required: bool = False, limit: int = 200) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise JobPostError(f"{field} is required.")
    return text[:limit]


def _clean_list(value: Any, limit: int = 30, item_limit: int = 160) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace("\n", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(part).strip() for part in value]
    else:
        raise JobPostError("Expected a list of values.")
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        part = " ".join(part.split())
        key = part.lower()
        if part and key not in seen:
            seen.add(key)
            result.append(part[:item_limit])
        if len(result) >= limit:
            break
    return result


def _normalize_categories(value: Any) -> list[str]:
    categories = _clean_list(value, limit=len(INTERVIEW_CATEGORIES), item_limit=40)
    normalized: list[str] = []
    lookup = {item.lower(): item for item in INTERVIEW_CATEGORIES}
    for category in categories:
        canonical = lookup.get(category.lower())
        if canonical and canonical not in normalized:
            normalized.append(canonical)
    if not normalized:
        return list(INTERVIEW_CATEGORIES)
    return normalized


def _normalize_difficulty(value: Any) -> str:
    normalized = str(value or "medium").strip().lower()
    if normalized not in DIFFICULTIES:
        raise JobPostError("Unsupported interview difficulty.")
    return normalized


def _validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    title = _clean_text(payload.get("title"), "Job title", required=True, limit=200)
    company = _clean_text(payload.get("company"), "Company", required=True, limit=200)
    description = _clean_text(
        payload.get("description"), "Job description", required=True, limit=20000
    )
    return {
        "title": title,
        "company": company,
        "description": description,
        "responsibilities": _clean_list(payload.get("responsibilities"), limit=30),
        "required_skills": _clean_list(payload.get("required_skills"), limit=40),
        "preferred_skills": _clean_list(payload.get("preferred_skills"), limit=40),
        "experience_level": _clean_text(
            payload.get("experience_level"), "Experience level", limit=120
        ),
        "education": _clean_text(payload.get("education"), "Education", limit=200),
        "interview_difficulty": _normalize_difficulty(payload.get("interview_difficulty")),
        "question_preferences": _normalize_categories(payload.get("question_preferences")),
        "is_published": bool(payload.get("is_published", False)),
    }


def _sync_skill_rows(db: Any, job: JobPost, payload: dict[str, Any]) -> None:
    db.query(JobSkill).filter(JobSkill.job_post_id == job.id).delete()
    for name in payload["required_skills"]:
        db.add(
            JobSkill(
                job_post_id=job.id,
                name=name,
                skill_type="required",
                category="technical",
                source="admin",
            )
        )
    for name in payload["preferred_skills"]:
        db.add(
            JobSkill(
                job_post_id=job.id,
                name=name,
                skill_type="preferred",
                category="technical",
                source="admin",
            )
        )


def create_job_post(*, actor_admin_id: int | None, **payload: Any) -> JobPost:
    require_admin(actor_admin_id)
    values = _validate_payload(payload)
    with get_db_context() as db:
        job = JobPost(
            created_by_admin_id=int(actor_admin_id or 0),
            **values,
        )
        db.add(job)
        db.flush()
        _sync_skill_rows(db, job, values)
        db.flush()
        log_event("admin.create_job", f"Created job posting: {job.title}", actor_admin_id)
        return job


def update_job_post(*, actor_admin_id: int | None, job_id: int, **payload: Any) -> JobPost:
    require_admin(actor_admin_id)
    values = _validate_payload(payload)
    with get_db_context() as db:
        job = db.query(JobPost).filter(JobPost.id == int(job_id)).first()
        if job is None:
            raise JobPostError("Job posting not found.")
        for key, value in values.items():
            setattr(job, key, value)
        job.blueprint_json = None
        _sync_skill_rows(db, job, values)
        db.flush()
        log_event("admin.update_job", f"Updated job posting: {job.title}", actor_admin_id)
        return job


def set_job_published(*, actor_admin_id: int | None, job_id: int, is_published: bool) -> JobPost:
    require_admin(actor_admin_id)
    with get_db_context() as db:
        job = db.query(JobPost).filter(JobPost.id == int(job_id)).first()
        if job is None:
            raise JobPostError("Job posting not found.")
        job.is_published = bool(is_published)
        db.flush()
        log_event(
            "admin.publish_job" if is_published else "admin.unpublish_job",
            f"{job.title}: published={is_published}",
            actor_admin_id,
        )
        return job


def delete_job_post(*, actor_admin_id: int | None, job_id: int) -> None:
    require_admin(actor_admin_id)
    with get_db_context() as db:
        job = db.query(JobPost).filter(JobPost.id == int(job_id)).first()
        if job is None:
            raise JobPostError("Job posting not found.")
        title = job.title
        db.query(JobSkill).filter(JobSkill.job_post_id == job.id).delete()
        db.delete(job)
        db.flush()
        log_event("admin.delete_job", f"Deleted job posting: {title}", actor_admin_id)


def get_job_post(
    job_id: int, *, actor_admin_id: int | None = None, require_published: bool = False
) -> JobPost:
    with get_db_context() as db:
        job = db.query(JobPost).filter(JobPost.id == int(job_id)).first()
        if job is None:
            raise JobPostError("Job posting not found.")
        if require_published and not job.is_published and actor_admin_id is None:
            raise JobPostError("This job posting is not available.")
        if require_published and actor_admin_id is not None:
            require_admin(actor_admin_id)
        return job


def list_jobs(
    *,
    actor_admin_id: int | None = None,
    include_unpublished: bool = False,
    search: str = "",
    company: str = "",
    skill: str = "",
    difficulty: str = "",
) -> list[JobPost]:
    if include_unpublished:
        require_admin(actor_admin_id)
    with get_db_context() as db:
        query = db.query(JobPost)
        if not include_unpublished:
            query = query.filter(JobPost.is_published.is_(True))
        if search.strip():
            term = f"%{search.strip().lower()}%"
            query = query.filter(
                (JobPost.title.ilike(term))
                | (JobPost.company.ilike(term))
                | (JobPost.description.ilike(term))
            )
        if company.strip():
            query = query.filter(JobPost.company.ilike(f"%{company.strip()}%"))
        if difficulty.strip():
            query = query.filter(JobPost.interview_difficulty == difficulty.strip().lower())
        if skill.strip():
            skill_term = f"%{skill.strip().lower()}%"
            skill_ids = [
                row[0]
                for row in db.query(JobSkill.job_post_id)
                .filter(JobSkill.name.ilike(skill_term))
                .distinct()
                .all()
            ]
            query = query.filter(JobPost.id.in_(skill_ids or [-1]))
        return query.order_by(JobPost.created_at.desc(), JobPost.id.desc()).all()


def get_job_skills(job_id: int) -> list[JobSkill]:
    with get_db_context() as db:
        return (
            db.query(JobSkill)
            .filter(JobSkill.job_post_id == int(job_id))
            .order_by(JobSkill.skill_type, JobSkill.name)
            .all()
        )


def store_blueprint(job_id: int, blueprint: dict[str, Any]) -> None:
    with get_db_context() as db:
        job = db.query(JobPost).filter(JobPost.id == int(job_id)).first()
        if job is not None:
            job.blueprint_json = blueprint
            db.flush()
