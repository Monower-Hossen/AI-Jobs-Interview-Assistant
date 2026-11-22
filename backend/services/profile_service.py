from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from database import User, UserProfile, get_db_context
from services.auth_service import (
    AuthError,
    log_event,
    require_owned_user_id,
    validate_full_name,
)
from services.preferences_service import get_user_preferences

MAX_HEADLINE_LENGTH = 200
MAX_OBJECTIVE_LENGTH = 2000
MAX_PROFILE_INFO_LENGTH = 2000
MAX_LIST_ITEMS = 40
MAX_ITEM_LENGTH = 160
PROFILE_LIST_FIELDS = (
    "skills",
    "technical_interests",
    "education",
    "work_experience",
    "career_interests",
)


def _clean_line(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _clean_long_text(value: object, limit: int) -> str:
    text = "\n".join(line.strip() for line in str(value or "").splitlines() if line.strip())
    return text[:limit]


def _clean_list(value: object) -> list[str]:
    if value is None:
        return []
    values: Iterable[object]
    if isinstance(value, str):
        values = value.replace(";", "\n").replace(",", "\n").splitlines()
    elif isinstance(value, dict):
        values = value.values()
    elif isinstance(value, Iterable):
        values = value
    else:
        values = [value]
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _clean_line(item, MAX_ITEM_LENGTH)
        key = text.casefold()
        if text and key not in seen:
            cleaned.append(text)
            seen.add(key)
        if len(cleaned) >= MAX_LIST_ITEMS:
            break
    return cleaned


def _profile_payload(user: User, row: UserProfile | None) -> dict[str, Any]:
    return {
        "full_name": str(user.full_name or ""),
        "email": str(user.email or ""),
        "phone_number": str(getattr(user, "phone_number", None) or ""),
        "additional_profile_info": str(getattr(user, "additional_profile_info", None) or ""),
        "headline": str(getattr(row, "headline", None) or "") if row else "",
        "objective": str(getattr(row, "objective", None) or "") if row else "",
        "skills": list(getattr(row, "skills", None) or []) if row else [],
        "technical_interests": list(getattr(row, "technical_interests", None) or []) if row else [],
        "education": list(getattr(row, "education", None) or []) if row else [],
        "work_experience": list(getattr(row, "work_experience", None) or []) if row else [],
        "career_interests": list(getattr(row, "career_interests", None) or []) if row else [],
    }


def _completion_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "Profile basics": bool(
            payload["full_name"] and payload["headline"] and payload["phone_number"]
        ),
        "Career focus": bool(payload["objective"] and payload["career_interests"]),
        "Skills and interests": bool(payload["skills"] and payload["technical_interests"]),
        "Background": bool(payload["education"] and payload["work_experience"]),
        "Profile notes": bool(payload["additional_profile_info"]),
    }
    completed = sum(1 for value in checks.values() if value)
    return {
        "percent": round(100 * completed / len(checks)),
        "completed": completed,
        "total": len(checks),
        "checks": checks,
        "missing": [name for name, value in checks.items() if not value],
    }


def get_user_profile(user_id: int, actor_user_id: int | None) -> dict[str, Any]:
    require_owned_user_id(user_id, actor_user_id)
    with get_db_context() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            raise AuthError("Account not found.")
        row = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
        payload = _profile_payload(user, row)
    payload["completion"] = _completion_from_payload(payload)
    return payload


def update_user_profile(
    user_id: int,
    *,
    full_name: str | None = None,
    phone_number: str | None = None,
    additional_profile_info: str | None = None,
    headline: str | None = None,
    objective: str | None = None,
    skills: object = None,
    technical_interests: object = None,
    education: object = None,
    work_experience: object = None,
    career_interests: object = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    require_owned_user_id(user_id, actor_user_id)
    cleaned: dict[str, Any] = {}
    if full_name is not None:
        cleaned["full_name"] = validate_full_name(full_name)
    if phone_number is not None:
        cleaned["phone_number"] = _clean_line(phone_number, 50)
    if additional_profile_info is not None:
        cleaned["additional_profile_info"] = _clean_long_text(
            additional_profile_info, MAX_PROFILE_INFO_LENGTH
        )
    if headline is not None:
        cleaned["headline"] = _clean_line(headline, MAX_HEADLINE_LENGTH)
    if objective is not None:
        cleaned["objective"] = _clean_long_text(objective, MAX_OBJECTIVE_LENGTH)
    raw_lists = {
        "skills": skills,
        "technical_interests": technical_interests,
        "education": education,
        "work_experience": work_experience,
        "career_interests": career_interests,
    }
    for field, value in raw_lists.items():
        if value is not None:
            cleaned[field] = _clean_list(value)

    with get_db_context() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            raise AuthError("Account not found.")
        row = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
        if row is None:
            row = UserProfile(user_id=user_id)
            db.add(row)
        if "full_name" in cleaned:
            user.full_name = cleaned["full_name"]
        if "phone_number" in cleaned:
            user.phone_number = cleaned["phone_number"]
        if "additional_profile_info" in cleaned:
            user.additional_profile_info = cleaned["additional_profile_info"]
        for field in ("headline", "objective", *PROFILE_LIST_FIELDS):
            if field in cleaned:
                setattr(row, field, cleaned[field])
        payload = _profile_payload(user, row)

    log_event("user.profile_update", user_id=user_id, message="Profile updated")
    payload["completion"] = _completion_from_payload(payload)
    return payload


def get_profile_completion(user_id: int, actor_user_id: int | None) -> dict[str, Any]:
    return get_user_profile(user_id, actor_user_id)["completion"]


def get_profile_recommendations(user_id: int, actor_user_id: int | None) -> list[dict[str, str]]:
    profile = get_user_profile(user_id, actor_user_id)
    preferences = get_user_preferences(user_id, actor_user_id)
    recommendations: list[dict[str, str]] = []
    if not profile["skills"]:
        recommendations.append(
            {
                "title": "Add your core skills",
                "detail": "List the skills you want interview questions to probe.",
                "target_page": "profile",
            }
        )
    if not profile["technical_interests"]:
        recommendations.append(
            {
                "title": "Choose technical interests",
                "detail": "Add focus areas so practice and learning stay aligned with your goals.",
                "target_page": "profile",
            }
        )
    categories = list(preferences.get("preferred_interview_categories") or [])
    if categories:
        recommendations.append(
            {
                "title": f"Practice {categories[0]} interviews",
                "detail": "Your preferred interview type is ready to use in interview practice.",
                "target_page": "interview",
            }
        )
    else:
        recommendations.append(
            {
                "title": "Set preferred interview types",
                "detail": "Choose the interview formats you want to see first.",
                "target_page": "account_settings",
            }
        )
    focus_skills = list((preferences.get("learning_preferences") or {}).get("focus_skills") or [])
    if focus_skills:
        recommendations.append(
            {
                "title": f"Study {focus_skills[0]}",
                "detail": "Build a focused learning plan from your saved learning preferences.",
                "target_page": "learning_roadmap",
            }
        )
    if profile["completion"]["percent"] < 100:
        recommendations.append(
            {
                "title": "Finish your profile",
                "detail": "Complete the remaining profile sections for more tailored guidance.",
                "target_page": "profile",
            }
        )
    return recommendations[:5]
