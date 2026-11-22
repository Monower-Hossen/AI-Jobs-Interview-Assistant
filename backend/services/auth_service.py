"""Local account authentication with salted password hashing.

Passwords are never stored in plain text and no credentials are ever returned to
the frontend. Session identity lives server-side in Streamlit session state.
"""

import hashlib
import hmac
import logging
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from config.settings import settings
from database import (
    Assessment,
    DocumentChunk,
    EventLog,
    GeneratedReport,
    InterviewProgress,
    InterviewQuestion,
    InterviewSession,
    JobDescription,
    KnowledgeDocument,
    LearningActivity,
    LearningNote,
    LearningResource,
    LearningResourceProgress,
    LearningRoadmap,
    LearningTask,
    Notification,
    QuestionBookmark,
    ResumeAnalysis,
    ResumeJobMatch,
    User,
    UserAchievement,
    UserSession,
    UserSettings,
    get_db_context,
)

logger = logging.getLogger(__name__)

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 240_000
_MAX_VERIFY_ITERATIONS = 1_000_000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")
_ACCOUNT_STATUSES = {"active", "inactive", "suspended"}
_ACCOUNT_ACTIVITY_TYPES = {
    "auth.login": "Signed in",
    "auth.login_failed": "Failed sign-in attempt",
    "auth.password_change": "Password changed",
    "auth.profile_update": "Full name updated",
    "admin.password_reset": "Password reset by an administrator",
    "user.profile_update": "Profile updated",
    "user.preferences_update": "Preferences updated",
}
MAX_FULL_NAME_LENGTH = 200
_MAX_FAILED_LOGINS = 5
_LOGIN_WINDOW_SECONDS = 5 * 60
_LOGIN_LOCKOUT_SECONDS = 60
_MAX_TRACKED_EMAILS = 1024


class AuthError(ValueError):
    """Raised for invalid registration, sign-in, or account management input."""


@dataclass
class _LoginFailures:
    count: int
    window_start: float
    locked_until: float = 0.0


_login_lock = threading.Lock()
_failed_logins: dict[str, _LoginFailures] = {}


def _prune_login_failures(now: float) -> None:
    for email, state in list(_failed_logins.items()):
        if now - state.window_start >= _LOGIN_WINDOW_SECONDS and state.locked_until <= now:
            _failed_logins.pop(email, None)


def _check_login_limit(email: str) -> None:
    """Reject attempts while an email is temporarily locked out."""
    now = time.monotonic()
    with _login_lock:
        state = _failed_logins.get(email)
        if state is None:
            return
        if state.locked_until > now:
            log_event(
                "auth.login_locked",
                message="Sign-in attempt rejected during lockout",
                level="warning",
            )
            remaining = int(state.locked_until - now) + 1
            raise AuthError(f"Too many failed sign-in attempts. Try again in {remaining} seconds.")
        if now - state.window_start >= _LOGIN_WINDOW_SECONDS:
            _failed_logins.pop(email, None)


def _record_login_failure(email: str) -> None:
    now = time.monotonic()
    with _login_lock:
        state = _failed_logins.get(email)
        if state is None or now - state.window_start >= _LOGIN_WINDOW_SECONDS:
            state = _LoginFailures(count=0, window_start=now)
            _failed_logins[email] = state
        state.count += 1
        if state.count >= _MAX_FAILED_LOGINS:
            state.locked_until = now + _LOGIN_LOCKOUT_SECONDS
        if len(_failed_logins) > _MAX_TRACKED_EMAILS:
            _prune_login_failures(now)


def _clear_login_failures(email: str) -> None:
    with _login_lock:
        _failed_logins.pop(email, None)


@dataclass(frozen=True)
class Account:
    """Account model for local authentication and admin views."""

    id: int
    email: str
    username: str | None
    full_name: str
    role: str
    account_status: str
    is_active: bool
    must_change_password: bool
    created_at: datetime | None = None
    last_login: datetime | None = None
    phone_number: str | None = None
    additional_profile_info: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _normalize_status(status: str | None, is_active: bool | None = None) -> str:
    raw = (status or "").strip().lower()
    if raw in _ACCOUNT_STATUSES:
        return raw
    if is_active is None:
        return "active"
    return "active" if is_active else "inactive"


def _sync_status_fields(user: User, status: str) -> None:
    normalized = _normalize_status(status)
    user.account_status = normalized
    user.is_active = normalized == "active"


def _to_account(user: User) -> Account:
    status = _normalize_status(
        getattr(user, "account_status", None), bool(getattr(user, "is_active", False))
    )
    return Account(
        id=int(user.id),
        email=str(user.email),
        username=getattr(user, "username", None),
        full_name=str(user.full_name),
        role=str(user.role),
        account_status=status,
        is_active=status == "active",
        must_change_password=bool(getattr(user, "must_change_password", False)),
        created_at=getattr(user, "created_at", None),
        last_login=getattr(user, "last_login", None),
        phone_number=getattr(user, "phone_number", None),
        additional_profile_info=getattr(user, "additional_profile_info", None),
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != _ALGORITHM:
            return False
        iteration_count = int(iterations)
        if iteration_count <= 0 or iteration_count > _MAX_VERIFY_ITERATIONS:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iteration_count
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def validate_email(email: str) -> str:
    normalized = (email or "").strip().lower()
    if not _EMAIL_RE.match(normalized):
        raise AuthError("Enter a valid email address.")
    return normalized


def validate_username(username: str) -> str:
    normalized = (username or "").strip().lower()
    if not normalized:
        raise AuthError("Username is required.")
    if not _USERNAME_RE.match(normalized):
        raise AuthError(
            "Username must be 3-30 characters and contain only letters, numbers, '.', '_' or '-'."
        )
    return normalized


def validate_password(password: str) -> str:
    value = password or ""
    if len(value) < settings.password_min_length:
        raise AuthError(f"Password must be at least {settings.password_min_length} characters.")
    if not re.search(r"[A-Za-z]", value) or not re.search(r"\d", value):
        raise AuthError("Password must include at least one letter and one number.")
    return value


def validate_full_name(full_name: str) -> str:
    name = (full_name or "").strip()
    if not name:
        raise AuthError("Full name is required.")
    if len(name) > MAX_FULL_NAME_LENGTH:
        raise AuthError(f"Full name must be at most {MAX_FULL_NAME_LENGTH} characters.")
    return name


def register(email: str, password: str, full_name: str) -> Account:
    if not settings.enable_registration:
        raise AuthError("Registration is disabled by the administrator.")
    # Public registration is disabled - only admins can create accounts
    raise AuthError(
        "Public registration is disabled. Please contact the administrator to create your account."
    )


def _ensure_unique_user_fields(
    db, email: str, username: str, exclude_user_id: int | None = None
) -> None:
    email_q = db.query(User).filter(User.email == email)
    if exclude_user_id is not None:
        email_q = email_q.filter(User.id != exclude_user_id)
    if email_q.first():
        raise AuthError("An account with this email already exists.")

    username_q = db.query(User).filter(User.username == username)
    if exclude_user_id is not None:
        username_q = username_q.filter(User.id != exclude_user_id)
    if username_q.first():
        raise AuthError("An account with this username already exists.")


def _require_admin_actor(db, actor_admin_id: int | None) -> User:
    """Verify the acting account is an active admin using the database, not UI state."""
    if actor_admin_id is None:
        raise AuthError("Administrator access required.")
    actor = db.query(User).filter(User.id == actor_admin_id).first()
    if (
        actor is None
        or str(actor.role) != "admin"
        or _normalize_status(getattr(actor, "account_status", None), bool(actor.is_active))
        != "active"
    ):
        raise AuthError("Administrator access required.")
    return cast(User, actor)


def require_admin(actor_admin_id: int | None) -> None:
    """Public server-side role check for admin pages and services."""
    with get_db_context() as db:
        _require_admin_actor(db, actor_admin_id)


def require_owned_user_id(user_id: int, actor_user_id: int | None) -> int:
    if actor_user_id is None or int(actor_user_id) != int(user_id):
        raise AuthError("This account does not belong to the signed-in user.")
    return int(user_id)


def require_owned_session_id(session_id: int, actor_user_id: int | None) -> int:
    if actor_user_id is None:
        raise AuthError("Sign in to access this workspace.")
    with get_db_context() as db:
        row = db.query(UserSession).filter(UserSession.id == session_id).first()
        if row is None or row.user_id != int(actor_user_id):
            raise AuthError("This workspace does not belong to the signed-in account.")
    return int(session_id)


def require_owned_session_token(session_token: str, actor_user_id: int | None) -> int:
    if actor_user_id is None:
        raise AuthError("Sign in to access this workspace.")
    with get_db_context() as db:
        row = db.query(UserSession).filter(UserSession.session_id == session_token).first()
        if row is None or row.user_id != int(actor_user_id):
            raise AuthError("This workspace does not belong to the signed-in account.")
        return cast(int, row.id)


def create_user(
    email: str,
    password: str,
    full_name: str,
    actor_admin_id: int | None = None,
    created_by_admin_id: int | None = None,
    username: str | None = None,
    phone_number: str | None = None,
    account_status: str = "active",
    additional_profile_info: str | None = None,
    must_change_password: bool = True,
) -> Account:
    """Create a new user account (admin-controlled)."""
    normalized_email = validate_email(email)
    validate_password(password)
    normalized_username = validate_username(username or normalized_email.split("@")[0])
    normalized_name = validate_full_name(full_name)
    status = _normalize_status(account_status)
    if status not in _ACCOUNT_STATUSES:
        raise AuthError("Unsupported account status.")
    phone = (phone_number or "").strip() or None
    extra = (additional_profile_info or "").strip() or None

    with get_db_context() as db:
        admin = _require_admin_actor(db, actor_admin_id)
        _ensure_unique_user_fields(db, normalized_email, normalized_username)

        user = User(
            email=normalized_email,
            username=normalized_username,
            full_name=normalized_name,
            phone_number=phone,
            password_hash=hash_password(password),
            role="user",
            is_verified=True,
            must_change_password=bool(must_change_password),
            additional_profile_info=extra,
            created_by_admin_id=(
                created_by_admin_id if created_by_admin_id is not None else admin.id
            ),
        )
        _sync_status_fields(user, status)
        db.add(user)
        db.flush()
        db.add(
            EventLog(
                user_id=user.id,
                event_type="admin.create_user",
                message=f"User account created ({status})",
            )
        )
        return _to_account(user)


def admin_create_user(
    *,
    actor_admin_id: int | None,
    full_name: str,
    email: str,
    username: str,
    password: str,
    confirm_password: str,
    phone_number: str | None = None,
    account_status: str = "active",
    additional_profile_info: str | None = None,
    created_by_admin_id: int | None = None,
) -> Account:
    """Create an account after verifying the acting admin against the database."""
    if password != confirm_password:
        raise AuthError("Passwords do not match.")
    return create_user(
        email=email,
        password=password,
        full_name=full_name,
        actor_admin_id=actor_admin_id,
        created_by_admin_id=created_by_admin_id,
        username=username,
        phone_number=phone_number,
        account_status=account_status,
        additional_profile_info=additional_profile_info,
        must_change_password=True,
    )


def authenticate(email: str, password: str) -> Account | None:
    normalized_email = (email or "").strip().lower()
    _check_login_limit(normalized_email)
    account: Account | None = None
    failed_user_id: int | None = None
    with get_db_context() as db:
        user = db.query(User).filter(User.email == normalized_email).first()
        if (
            not user
            or _normalize_status(
                getattr(user, "account_status", None), bool(getattr(user, "is_active", False))
            )
            != "active"
            or not verify_password(password, cast(str, user.password_hash))
        ):
            failed_user_id = user.id if user else None
            _record_login_failure(normalized_email)
        else:
            cast(Any, user).last_login = datetime.now()
            db.add(EventLog(user_id=user.id, event_type="auth.login", message="Signed in"))
            account = _to_account(user)

    if account is None:
        log_event(
            "auth.login_failed",
            user_id=failed_user_id,
            message="Failed sign-in attempt",
            level="warning",
        )
        return None

    _clear_login_failures(normalized_email)
    return account


def get_account(user_id: int) -> Account | None:
    with get_db_context() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return None
        status = _normalize_status(
            getattr(user, "account_status", None), bool(getattr(user, "is_active", False))
        )
        if status != "active":
            return None
        return _to_account(user)


def get_account_for_admin(user_id: int, *, actor_admin_id: int | None) -> Account | None:
    with get_db_context() as db:
        _require_admin_actor(db, actor_admin_id)
        user = db.query(User).filter(User.id == user_id).first()
        return _to_account(user) if user else None


def get_account_by_email(email: str) -> Account | None:
    """Return the active account for an email, if one exists."""
    normalized_email = (email or "").strip().lower()
    with get_db_context() as db:
        user = db.query(User).filter(User.email == normalized_email).first()
        if not user:
            return None
        if (
            _normalize_status(
                getattr(user, "account_status", None), bool(getattr(user, "is_active", False))
            )
            != "active"
        ):
            return None
        return _to_account(user) if user else None


def list_users(*, actor_admin_id: int | None) -> list[Account]:
    with get_db_context() as db:
        _require_admin_actor(db, actor_admin_id)
        return [_to_account(user) for user in db.query(User).order_by(User.id).all()]


def get_user_statistics(*, actor_admin_id: int | None) -> dict[str, int]:
    """Return live account counts for the admin dashboard."""
    with get_db_context() as db:
        _require_admin_actor(db, actor_admin_id)
        new_since = datetime.now() - timedelta(days=7)
        return {
            "total": int(db.query(User).count()),
            "active": int(db.query(User).filter(User.account_status == "active").count()),
            "inactive": int(db.query(User).filter(User.account_status == "inactive").count()),
            "suspended": int(db.query(User).filter(User.account_status == "suspended").count()),
            "new_last_7_days": int(db.query(User).filter(User.created_at >= new_since).count()),
        }


def list_events(limit: int = 20, *, actor_admin_id: int | None) -> list[EventLog]:
    """Return the most recent event log entries for admin activity views."""
    with get_db_context() as db:
        _require_admin_actor(db, actor_admin_id)
        return list(db.query(EventLog).order_by(EventLog.created_at.desc()).limit(limit).all())


def get_account_activity(
    user_id: int, actor_user_id: int | None, *, limit: int = 8
) -> list[dict[str, Any]]:
    require_owned_user_id(user_id, actor_user_id)
    safe_limit = max(1, min(50, int(limit)))
    with get_db_context() as db:
        rows = (
            db.query(EventLog)
            .filter(EventLog.user_id == user_id, EventLog.event_type.in_(_ACCOUNT_ACTIVITY_TYPES))
            .order_by(EventLog.created_at.desc())
            .limit(safe_limit)
            .all()
        )
        return [
            {
                "event_type": str(row.event_type),
                "label": _ACCOUNT_ACTIVITY_TYPES[str(row.event_type)],
                "message": str(row.message or "")[:200],
                "level": str(row.level or "info"),
                "created_at": row.created_at,
            }
            for row in rows
        ]


def set_user_role(user_id: int, role: str, *, actor_admin_id: int | None) -> None:
    if role not in {"user", "admin"}:
        raise AuthError("Unsupported role.")
    with get_db_context() as db:
        admin = _require_admin_actor(db, actor_admin_id)
        if user_id == admin.id and role != "admin":
            raise AuthError("You cannot change your own role.")
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise AuthError("User not found.")
        user.role = role
        db.add(
            EventLog(
                user_id=user_id,
                event_type="admin.role_change",
                message=f"Role set to {role}",
            )
        )


def set_user_status(user_id: int, status: str, *, actor_admin_id: int | None) -> None:
    normalized = _normalize_status(status)
    if normalized not in _ACCOUNT_STATUSES:
        raise AuthError("Unsupported account status.")
    with get_db_context() as db:
        admin = _require_admin_actor(db, actor_admin_id)
        if user_id == admin.id and normalized != "active":
            raise AuthError("You cannot deactivate your own account.")
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise AuthError("User not found.")
        _sync_status_fields(user, normalized)
        db.add(
            EventLog(
                user_id=user_id,
                event_type="admin.status_change",
                message=f"Status set to {normalized}",
            )
        )


def set_user_active(user_id: int, is_active: bool, *, actor_admin_id: int | None) -> None:
    set_user_status(user_id, "active" if is_active else "inactive", actor_admin_id=actor_admin_id)


def update_user_account(
    user_id: int,
    *,
    actor_admin_id: int | None,
    full_name: str,
    email: str,
    username: str,
    phone_number: str | None = None,
    account_status: str | None = None,
    additional_profile_info: str | None = None,
) -> Account:
    normalized_email = validate_email(email)
    normalized_username = validate_username(username)
    normalized_name = validate_full_name(full_name)
    phone = (phone_number or "").strip() or None
    extra = (additional_profile_info or "").strip() or None
    normalized_status = None
    if account_status is not None:
        normalized_status = _normalize_status(account_status)
        if normalized_status not in _ACCOUNT_STATUSES:
            raise AuthError("Unsupported account status.")
    with get_db_context() as db:
        admin = _require_admin_actor(db, actor_admin_id)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise AuthError("User not found.")
        if normalized_status and user_id == admin.id and normalized_status != "active":
            raise AuthError("You cannot deactivate your own account.")
        _ensure_unique_user_fields(
            db, normalized_email, normalized_username, exclude_user_id=user_id
        )
        user.full_name = normalized_name
        user.email = normalized_email
        user.username = normalized_username
        user.phone_number = phone
        user.additional_profile_info = extra
        if normalized_status is not None:
            _sync_status_fields(user, normalized_status)
        db.add(
            EventLog(
                user_id=user_id, event_type="admin.update_user", message="User details updated"
            )
        )
        return _to_account(user)


def delete_user(user_id: int, *, actor_admin_id: int | None) -> None:
    with get_db_context() as db:
        admin = _require_admin_actor(db, actor_admin_id)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise AuthError("User not found.")
        if str(user.role) == "admin":
            raise AuthError("Admin accounts cannot be deleted from this page.")
        if user_id == admin.id:
            raise AuthError("You cannot delete your own account.")

        session_ids = [
            row.id for row in db.query(UserSession.id).filter(UserSession.user_id == user_id).all()
        ]
        interview_ids = [
            row.id
            for row in db.query(InterviewSession.id)
            .filter(InterviewSession.session_id.in_(session_ids))
            .all()
        ]
        resume_ids = [
            row.id
            for row in db.query(ResumeAnalysis.id)
            .filter(ResumeAnalysis.session_id.in_(session_ids))
            .all()
        ]
        job_ids = [
            row.id
            for row in db.query(JobDescription.id)
            .filter(JobDescription.session_id.in_(session_ids))
            .all()
        ]
        roadmap_ids = [
            row.id
            for row in db.query(LearningRoadmap.id)
            .filter(LearningRoadmap.session_id.in_(session_ids))
            .all()
        ]

        db.query(LearningResourceProgress).filter(
            LearningResourceProgress.session_id.in_(session_ids)
        ).delete(synchronize_session=False)
        db.query(LearningActivity).filter(LearningActivity.session_id.in_(session_ids)).delete(
            synchronize_session=False
        )
        db.query(UserAchievement).filter(UserAchievement.session_id.in_(session_ids)).delete(
            synchronize_session=False
        )
        db.query(Notification).filter(Notification.session_id.in_(session_ids)).delete(
            synchronize_session=False
        )
        db.query(LearningTask).filter(LearningTask.roadmap_id.in_(roadmap_ids)).delete(
            synchronize_session=False
        )
        db.query(LearningRoadmap).filter(LearningRoadmap.session_id.in_(session_ids)).delete(
            synchronize_session=False
        )
        db.query(InterviewQuestion).filter(
            InterviewQuestion.interview_session_id.in_(interview_ids)
        ).delete(synchronize_session=False)
        db.query(GeneratedReport).filter(GeneratedReport.session_id.in_(session_ids)).delete(
            synchronize_session=False
        )
        db.query(ResumeJobMatch).filter(
            ResumeJobMatch.resume_analysis_id.in_(resume_ids)
        ).delete(synchronize_session=False)
        db.query(ResumeJobMatch).filter(
            ResumeJobMatch.job_description_id.in_(job_ids)
        ).delete(synchronize_session=False)
        db.query(InterviewSession).filter(InterviewSession.session_id.in_(session_ids)).delete(
            synchronize_session=False
        )
        db.query(DocumentChunk).filter(DocumentChunk.session_id.in_(session_ids)).delete(
            synchronize_session=False
        )
        db.query(KnowledgeDocument).filter(KnowledgeDocument.session_id.in_(session_ids)).delete(
            synchronize_session=False
        )
        db.query(Assessment).filter(Assessment.session_id.in_(session_ids)).delete(
            synchronize_session=False
        )
        db.query(InterviewProgress).filter(
            InterviewProgress.session_id.in_(session_ids)
        ).delete(synchronize_session=False)
        db.query(QuestionBookmark).filter(QuestionBookmark.session_id.in_(session_ids)).delete(
            synchronize_session=False
        )
        db.query(LearningNote).filter(LearningNote.session_id.in_(session_ids)).delete(
            synchronize_session=False
        )
        db.query(UserSettings).filter(UserSettings.user_id == user_id).delete(
            synchronize_session=False
        )
        db.query(UserSession).filter(UserSession.user_id == user_id).delete(
            synchronize_session=False
        )
        db.query(EventLog).filter(EventLog.user_id == user_id).update(
            {EventLog.user_id: None}, synchronize_session=False
        )
        db.query(LearningResource).filter(LearningResource.created_by_admin_id == user_id).update(
            {LearningResource.created_by_admin_id: None}, synchronize_session=False
        )
        db.query(User).filter(User.created_by_admin_id == user_id).update(
            {User.created_by_admin_id: None}, synchronize_session=False
        )
        db.delete(user)


def admin_reset_password(user_id: int, new_password: str, *, actor_admin_id: int | None) -> None:
    validate_password(new_password)
    with get_db_context() as db:
        _require_admin_actor(db, actor_admin_id)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise AuthError("User not found.")
        user.password_hash = hash_password(new_password)
        user.must_change_password = True
        db.add(
            EventLog(
                user_id=user_id,
                event_type="admin.password_reset",
                message="Password reset by admin",
            )
        )


def is_user_role(account: Account) -> bool:
    return account.role == "user"


def is_admin_role(account: Account) -> bool:
    return account.role == "admin"


def change_password(
    user_id: int, current: str, new_password: str, *, actor_user_id: int | None = None
) -> None:
    if actor_user_id is not None:
        require_owned_user_id(user_id, actor_user_id)
    validate_password(new_password)
    with get_db_context() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not verify_password(current, cast(str, user.password_hash)):
            raise AuthError("Current password is incorrect.")
        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        db.add(
            EventLog(user_id=user_id, event_type="auth.password_change", message="Password changed")
        )


def forgot_password(email: str) -> None:
    raise AuthError("Self-service password reset is disabled. Contact an administrator.")


def reset_password(token: str, new_password: str) -> Account:
    raise AuthError("Self-service password reset is disabled. Contact an administrator.")


def activate_user(token: str) -> Account | None:
    raise AuthError("Self-service activation is disabled. Contact an administrator.")


def resend_activation(user_id: int) -> None:
    raise AuthError("Self-service activation is disabled. Contact an administrator.")


def update_profile(user_id: int, full_name: str, *, actor_user_id: int | None = None) -> None:
    """Update a full name after checking it is non-empty and within the column limit."""
    if actor_user_id is not None:
        require_owned_user_id(user_id, actor_user_id)
    name = validate_full_name(full_name)
    with get_db_context() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            cast(Any, user).full_name = name
            db.add(
                EventLog(
                    user_id=user_id,
                    event_type="auth.profile_update",
                    message="Profile updated",
                )
            )


def log_event(
    event_type: str, message: str = "", user_id: int | None = None, level: str = "info"
) -> None:
    try:
        with get_db_context() as db:
            db.add(
                EventLog(
                    user_id=user_id, event_type=event_type, message=message[:1000], level=level
                )
            )
    except Exception as exc:  # logging must never break the feature it observes
        logger.debug("Could not write event log: %s", exc)


def _safe_workspace_token(token: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", token)[:80] or "default"


def _move_vector_store(old_token: str, new_token: str) -> None:
    old_dir = Path(settings.vector_store_path) / _safe_workspace_token(old_token)
    new_dir = Path(settings.vector_store_path) / _safe_workspace_token(new_token)
    if old_dir == new_dir or not old_dir.exists() or new_dir.exists():
        return
    try:
        new_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_dir), str(new_dir))
    except OSError as exc:
        logger.warning("Could not migrate vector store for workspace token: %s", exc)


def bind_session_to_account(account: Account, legacy_session_id: str | None = None) -> str:
    """Return a private workspace token for the account, preserving prior data."""
    old_token: str | None = None
    token_value = ""
    with get_db_context() as db:
        user = db.query(User).filter(User.id == account.id).first()
        if user is None:
            raise AuthError("Account not found.")

        token = getattr(user, "workspace_token", None)
        if not token or str(token).startswith("user-"):
            old_token = str(token or f"user-{account.id}")
            token = secrets.token_urlsafe(24)
            user.workspace_token = token

        owned = db.query(UserSession).filter(UserSession.session_id == token).first()
        if owned is None:
            owned = db.query(UserSession).filter(UserSession.user_id == account.id).first()
            if owned is not None:
                if owned.session_id != token:
                    old_token = old_token or str(owned.session_id)
                    owned.session_id = str(token)
            else:
                owned = UserSession(
                    session_id=str(token), user_id=account.id, label=account.email
                )
                db.add(owned)
        owned.user_id = account.id
        db.flush()

        if legacy_session_id and legacy_session_id != token:
            legacy = (
                db.query(UserSession).filter(UserSession.session_id == legacy_session_id).first()
            )
            if legacy and legacy.id != owned.id:
                legacy.user_id = account.id
        token_value = str(token)

    if old_token and old_token != token_value:
        _move_vector_store(old_token, token_value)
    return token_value


def owned_session_id(token: str, user_id: int | None) -> int | None:
    """Resolve a workspace token to its session id only for the matching owner."""
    with get_db_context() as db:
        row = db.query(UserSession).filter(UserSession.session_id == token).first()
        if row is None or row.user_id != user_id:
            return None
        return cast(int, row.id)
