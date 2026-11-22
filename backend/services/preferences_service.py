from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from database import User, UserSession, UserSettings, get_db_context
from services.auth_service import log_event, require_owned_user_id

MAX_ROLE_LENGTH = 100
MAX_PHONE_LENGTH = 50
MAX_PROFILE_INFO_LENGTH = 2000
MAX_MODEL_LENGTH = 120
MAX_CATEGORY_COUNT = 5
MAX_LIST_ITEMS = 40
MAX_ITEM_LENGTH = 160
INTERVIEW_TYPES = ("Full Interview", "Technical", "Behavioral", "HR", "Mixed")
DIFFICULTIES = ("easy", "medium", "hard")
PROVIDERS = ("auto", "gemini", "groq", "ollama")
LEARNING_FORMATS = ("mixed", "articles", "videos", "practice")
DEFAULT_NOTIFICATION_PREFERENCES: dict[str, bool] = {
    "interview_reminders": True,
    "learning_updates": True,
    "achievements": True,
    "admin_announcements": True,
    "system_updates": True,
    "email_digest": False,
}
_NOTIFICATION_TYPE_KEYS = {
    "achievement": "achievements",
    "learning": "learning_updates",
    "task_reminder": "interview_reminders",
    "resource_completed": "learning_updates",
    "new_resource": "learning_updates",
    "announcement": "admin_announcements",
    "reminder": "interview_reminders",
    "system": "system_updates",
}
_UNSET = object()


def _clean_text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _json_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return list(parsed) if isinstance(parsed, (list, tuple)) else []
    return []


def _json_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _clean_list(value: object, *, limit: int = MAX_LIST_ITEMS, item_limit: int = MAX_ITEM_LENGTH) -> list[str]:
    values: Iterable[Any]
    if isinstance(value, str):
        parsed = _json_list(value)
        values = parsed if parsed else value.replace(";", "\n").replace(",", "\n").splitlines()
    else:
        values = value if isinstance(value, (list, tuple, set)) else _json_list(value)
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _clean_text(item, item_limit)
        key = text.casefold()
        if text and key not in seen:
            cleaned.append(text)
            seen.add(key)
        if len(cleaned) >= limit:
            break
    return cleaned


def _clean_categories(value: object) -> list[str]:
    categories = _clean_list(value, limit=MAX_CATEGORY_COUNT, item_limit=50)
    return [item for item in categories if item in INTERVIEW_TYPES]


def _clean_learning_preferences(value: object) -> dict[str, Any]:
    raw = _json_dict(value)
    focus_skills = _clean_list(raw.get("focus_skills"), limit=10, item_limit=80)
    try:
        weekly_goal = int(raw.get("weekly_goal", 5))
    except (TypeError, ValueError):
        weekly_goal = 5
    weekly_goal = max(1, min(40, weekly_goal))
    preferred_format = _clean_text(raw.get("preferred_format"), 30).lower()
    if preferred_format not in LEARNING_FORMATS:
        preferred_format = "mixed"
    return {
        "focus_skills": focus_skills,
        "weekly_goal": weekly_goal,
        "preferred_format": preferred_format,
    }


def _clean_notification_preferences(value: object) -> dict[str, bool]:
    raw = _json_dict(value)
    cleaned = dict(DEFAULT_NOTIFICATION_PREFERENCES)
    for key in cleaned:
        if key in raw:
            cleaned[key] = bool(raw[key])
    return cleaned


def _notification_pref_key(notification_type: object) -> str:
    normalized = _clean_text(notification_type, 60).lower()
    return _NOTIFICATION_TYPE_KEYS.get(normalized, "system_updates")


def notification_type_enabled(
    db: Any,
    *,
    session_id: int,
    notification_type: str,
    severity: str = "info",
) -> bool:
    session_row = db.query(UserSession).filter(UserSession.id == session_id).first()
    if session_row is None:
        return True
    settings_row = (
        db.query(UserSettings).filter(UserSettings.user_id == session_row.user_id).first()
    )
    preferences = _clean_notification_preferences(
        getattr(settings_row, "notification_preferences", None) if settings_row else None
    )
    return bool(preferences.get(_notification_pref_key(notification_type), True))


def get_user_preferences(user_id: int, actor_user_id: int | None) -> dict:
    require_owned_user_id(user_id, actor_user_id)
    with get_db_context() as db:
        row = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
        return {
            "theme": row.theme if row else "dark",
            "default_role": row.default_role if row else "",
            "default_difficulty": row.default_difficulty if row else "medium",
            "default_interview_type": row.default_interview_type if row else "Full Interview",
            "llm_provider": row.llm_provider if row else "auto",
            "llm_model": getattr(row, "llm_model", "") if row else "",
            "voice_enabled": bool(row.voice_enabled) if row else True,
            "privacy_share_analytics": (bool(row.privacy_share_analytics) if row else False),
            "preferred_interview_categories": _clean_categories(
                getattr(row, "preferred_interview_categories", []) if row else []
            ),
            "learning_preferences": _clean_learning_preferences(
                getattr(row, "learning_preferences", {}) if row else {}
            ),
            "notification_preferences": _clean_notification_preferences(
                getattr(row, "notification_preferences", {}) if row else {}
            ),
        }


def update_user_preferences(
    user_id: int,
    *,
    theme: str | object = _UNSET,
    default_role: str | object = _UNSET,
    default_difficulty: str | object = _UNSET,
    default_interview_type: str | object = _UNSET,
    llm_provider: str | object = _UNSET,
    llm_model: str | object = _UNSET,
    voice_enabled: bool | object = _UNSET,
    privacy_share_analytics: bool | object = _UNSET,
    preferred_interview_categories: object = _UNSET,
    learning_preferences: object = _UNSET,
    notification_preferences: object = _UNSET,
    actor_user_id: int | None = None,
) -> dict:
    require_owned_user_id(user_id, actor_user_id)
    with get_db_context() as db:
        row = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
        if not row:
            row = UserSettings(user_id=user_id)
            db.add(row)

        role = _clean_text(
            default_role if default_role is not _UNSET else getattr(row, "default_role", ""),
            MAX_ROLE_LENGTH,
        )

        raw_difficulty = (
            default_difficulty
            if default_difficulty is not _UNSET
            else getattr(row, "default_difficulty", "medium")
        )
        difficulty = _clean_text(raw_difficulty, 20).lower()
        if difficulty not in DIFFICULTIES:
            if default_difficulty is _UNSET:
                difficulty = "medium"
            else:
                raise ValueError("Invalid default difficulty.")

        raw_interview_type = (
            default_interview_type
            if default_interview_type is not _UNSET
            else getattr(row, "default_interview_type", "Full Interview")
        )
        interview_type = _clean_text(raw_interview_type, 50)
        if interview_type not in INTERVIEW_TYPES:
            if default_interview_type is _UNSET:
                interview_type = "Full Interview"
            else:
                raise ValueError("Invalid default interview type.")

        raw_theme = theme if theme is not _UNSET else getattr(row, "theme", "dark")
        resolved_theme = "light" if raw_theme == "light" else "dark"

        raw_provider = (
            llm_provider if llm_provider is not _UNSET else getattr(row, "llm_provider", "auto")
        )
        provider = _clean_text(raw_provider, 40)
        if provider not in PROVIDERS:
            if llm_provider is _UNSET:
                provider = "auto"
            else:
                raise ValueError("Invalid provider.")

        raw_model = llm_model if llm_model is not _UNSET else getattr(row, "llm_model", "")
        model = _clean_text(raw_model, MAX_MODEL_LENGTH)

        voice = bool(getattr(row, "voice_enabled", True))
        if voice_enabled is not _UNSET:
            voice = bool(voice_enabled)
        analytics = bool(getattr(row, "privacy_share_analytics", False))
        if privacy_share_analytics is not _UNSET:
            analytics = bool(privacy_share_analytics)

        categories = _clean_categories(
            getattr(row, "preferred_interview_categories", [])
            if preferred_interview_categories is _UNSET
            else preferred_interview_categories
        )
        learning = _clean_learning_preferences(
            getattr(row, "learning_preferences", {})
            if learning_preferences is _UNSET
            else learning_preferences
        )
        notifications = _clean_notification_preferences(
            getattr(row, "notification_preferences", {})
            if notification_preferences is _UNSET
            else notification_preferences
        )

        row.theme = resolved_theme
        row.default_role = role
        row.default_difficulty = difficulty
        row.default_interview_type = interview_type
        row.llm_provider = provider
        row.llm_model = model
        row.voice_enabled = voice
        row.privacy_share_analytics = analytics
        row.preferred_interview_categories = categories
        row.learning_preferences = learning
        row.notification_preferences = notifications

        preferences = {
            "theme": resolved_theme,
            "default_role": role,
            "default_difficulty": difficulty,
            "default_interview_type": interview_type,
            "llm_provider": provider,
            "llm_model": model,
            "voice_enabled": voice,
            "privacy_share_analytics": analytics,
            "preferred_interview_categories": categories,
            "learning_preferences": learning,
            "notification_preferences": notifications,
        }

    log_event("user.preferences_update", user_id=user_id, message="Preferences updated")
    return preferences


def update_account_details(
    user_id: int,
    *,
    phone_number: str = "",
    additional_profile_info: str = "",
    actor_user_id: int | None = None,
) -> None:
    require_owned_user_id(user_id, actor_user_id)
    phone = str(phone_number).strip()[:MAX_PHONE_LENGTH]
    info = str(additional_profile_info).strip()[:MAX_PROFILE_INFO_LENGTH]
    with get_db_context() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("Account not found.")
        user.phone_number = phone
        user.additional_profile_info = info
    log_event("user.profile_update", user_id=user_id, message="Interview profile updated")
