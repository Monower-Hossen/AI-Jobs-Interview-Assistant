from __future__ import annotations

import importlib
import os
import tempfile
import uuid
import zipfile
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, cast

magic: Any | None
try:
    magic = importlib.import_module("magic")
except ImportError:
    magic = None

from config.settings import runtime_path, settings
from database import (
    BugReport,
    ContactMessage,
    EventLog,
    FeatureRequest,
    FeatureRequestVote,
    Notification,
    SupportAttachment,
    SupportFaq,
    SupportFeedbackNote,
    SupportInternalNote,
    SupportMessage,
    SupportTicket,
    User,
    UserFeedback,
    UserSession,
    get_db_context,
    get_readonly_db,
)
from services.auth_service import AuthError, _require_admin_actor, log_event
from services.preferences_service import notification_type_enabled
from sqlalchemy import asc, desc, func, or_
from utils.file_utils import compute_file_hash, get_file_extension, sanitize_filename

SUPPORT_TICKET_STATUSES = ("Open", "In Progress", "Waiting User", "Resolved", "Closed")
SUPPORT_TICKET_CATEGORIES = (
    "General",
    "Account",
    "Interview",
    "Learning",
    "Technical",
    "Billing",
    "Other",
)
SUPPORT_PRIORITIES = ("Low", "Medium", "High", "Urgent")
FEEDBACK_CATEGORIES = ("Platform", "Interview", "Learning", "Support", "Other")
FEEDBACK_STATUSES = ("New", "In Review", "Reviewed", "Responded", "Closed")
FEEDBACK_RATING_LABELS = {
    1: "1 - Very poor",
    2: "2 - Poor",
    3: "3 - Okay",
    4: "4 - Good",
    5: "5 - Excellent",
}
BUG_SEVERITIES = ("Low", "Medium", "High", "Critical")
BUG_STATUSES = ("New", "Confirmed", "In Progress", "Fixed", "Rejected")
BUG_STATUS_ALIASES = {
    "triaged": "Confirmed",
    "won't fix": "Rejected",
    "wont fix": "Rejected",
    "closed": "Rejected",
}
FEATURE_STATUSES = (
    "Submitted",
    "Under Review",
    "Planned",
    "In Development",
    "Released",
    "Rejected",
)
FEATURE_STATUS_ALIASES = {"declined": "Rejected"}
FEATURE_CATEGORIES = ("Interview", "Learning", "Career", "Platform", "Integration", "Other")
CONTACT_CATEGORIES = ("General", "Account", "Technical", "Billing", "Partnership", "Other")
CONTACT_STATUSES = ("New", "Read", "Replied", "Closed")
FAQ_CATEGORIES = ("Getting Started", "Account", "Interviews", "Learning", "Technical", "Other")
SUPPORT_ATTACHMENT_RECORD_TYPES = ("ticket", "feedback", "bug", "feature", "contact")
SUPPORT_EVENT_PREFIXES = (
    "support_",
    "bug_report_",
    "feature_request_",
    "user_feedback_",
    "contact_message_",
)
SUPPORT_UPLOAD_DIR = "data/uploads/support"
SUPPORT_ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg"}
SUPPORT_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
_SUPPORT_MIME_ALIASES = {
    ".docx": {"application/zip", "application/x-zip-compressed"},
    ".jpg": {"image/jpg"},
    ".jpeg": {"image/jpg"},
}
_SUPPORT_MAGIC_EXTENSIONS = {".pdf", ".docx", ".png", ".jpg", ".jpeg"}


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _clean_text(value: object, limit: int, *, required: bool = False, label: str = "Field") -> str:
    text = " ".join(str(value or "").split())
    if required and not text:
        raise ValueError(f"{label} is required.")
    return text[:limit]


def _clean_long_text(
    value: object, limit: int = 8000, *, required: bool = False, label: str = "Field"
) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{label} is required.")
    return text[:limit]


def _normalize_choice(
    value: object,
    choices: tuple[str, ...],
    label: str,
    *,
    default: str | None = None,
) -> str:
    text = _clean_text(value, 60).casefold()
    if not text and default is not None:
        return default
    match = next((choice for choice in choices if choice.casefold() == text), None)
    if match is None:
        raise ValueError(f"Select a valid {label}.")
    return match


def _normalize_rating(value: object) -> int:
    try:
        rating = int(cast(Any, value))
    except (TypeError, ValueError):
        raise ValueError("Select a valid rating.")
    if rating < 1 or rating > 5:
        raise ValueError("Rating must be between 1 and 5.")
    return rating


def _as_positive_int(value: object, label: str) -> int:
    try:
        number = int(cast(Any, value))
    except (TypeError, ValueError):
        raise ValueError(f"{label} is invalid.")
    if number <= 0:
        raise ValueError(f"{label} is invalid.")
    return number


def _new_ref(prefix: str) -> str:
    return f"{prefix}-{_utcnow():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"


def _normalize_status_alias(
    value: object, aliases: dict[str, str], choices: tuple[str, ...]
) -> str:
    text = _clean_text(value, 60)
    mapped = aliases.get(text.casefold(), text)
    return _normalize_choice(mapped, choices, "status")


def _parse_date_bound(value: object, label: str, *, end_of_day: bool = False) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.max if end_of_day else time.min)
    else:
        text = _clean_text(value, 40)
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                day = date.fromisoformat(text[:10])
            except ValueError:
                raise ValueError(f"{label} is invalid.")
            parsed = datetime.combine(day, time.max if end_of_day else time.min)
    if end_of_day and not isinstance(value, (date, datetime)) and parsed.time() == time.min:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed.replace(tzinfo=None)


def _like_pattern(value: object) -> str | None:
    text = _clean_text(value, 120)
    if not text:
        return None
    return f"%{text}%"


def _date_window(column: Any, query: Any, date_from: object, date_to: object) -> Any:
    start = _parse_date_bound(date_from, "Start date")
    end = _parse_date_bound(date_to, "End date", end_of_day=True)
    if start and end and start > end:
        raise ValueError("Start date must be before end date.")
    if start:
        query = query.filter(column >= start)
    if end:
        query = query.filter(column <= end)
    return query


def _activity_rows(
    db: Any,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
) -> list[Any]:
    query = db.query(EventLog).filter(
        or_(*[EventLog.event_type.like(f"{prefix}%") for prefix in SUPPORT_EVENT_PREFIXES])
    )
    if since:
        query = query.filter(EventLog.created_at >= since)
    if until:
        query = query.filter(EventLog.created_at <= until)
    query = query.order_by(desc(EventLog.created_at), desc(EventLog.id))
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def _support_activity(
    db: Any,
    *,
    limit: int = 8,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    rows = _activity_rows(db, since=since, until=until, limit=limit)
    return [
        {
            "event_type": str(row.event_type),
            "message": str(row.message or ""),
            "actor": _account_name(db, row.user_id),
            "level": str(row.level or "info"),
            "created_at": row.created_at,
        }
        for row in rows
    ]


def _attachments_for(db: Any, record_type: str, record_id: int) -> list[dict[str, Any]]:
    rows = (
        db.query(SupportAttachment)
        .filter(
            SupportAttachment.record_type == record_type,
            SupportAttachment.record_id == record_id,
        )
        .order_by(asc(SupportAttachment.created_at), asc(SupportAttachment.id))
        .all()
    )
    return [_attachment_dict(row) for row in rows]


def _record_history(db: Any, reference: str, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = (
        db.query(EventLog)
        .filter(
            EventLog.message.like(f"%{reference}%"),
            or_(
                EventLog.event_type.like("support_%"),
                EventLog.event_type.like("bug_report_%"),
                EventLog.event_type.like("feature_request_%"),
                EventLog.event_type.like("user_feedback_%"),
                EventLog.event_type.like("contact_message_%"),
            ),
        )
        .order_by(desc(EventLog.created_at), desc(EventLog.id))
        .limit(limit)
        .all()
    )
    return [
        {
            "event_type": str(row.event_type),
            "message": str(row.message or ""),
            "actor": _account_name(db, row.user_id),
            "created_at": row.created_at,
        }
        for row in rows
    ]


def _require_user(db: Any, actor_user_id: int | None) -> int:
    if actor_user_id is None:
        raise AuthError("Sign in to access support.")
    user_id = _as_positive_int(actor_user_id, "Account")
    row = db.query(User).filter(User.id == user_id).first()
    status = str(getattr(row, "account_status", "active") or "active").casefold() if row else ""
    if row is None or status != "active" or not bool(getattr(row, "is_active", True)):
        raise AuthError("Sign in to access support.")
    return user_id


def _require_admin(db: Any, actor_admin_id: int | None) -> User:
    return _require_admin_actor(db, actor_admin_id)


def _account_name(db: Any, user_id: int | None) -> str:
    if user_id is None:
        return "System"
    row = db.query(User).filter(User.id == user_id).first()
    if row is None:
        return "Unknown user"
    return str(row.full_name or row.username or row.email)


def _account_contact(db: Any, user_id: int | None) -> tuple[str, str]:
    if user_id is None:
        return "", ""
    row = db.query(User).filter(User.id == user_id).first()
    if row is None:
        return "", ""
    return str(row.username or ""), str(row.email or "")


def _emit(events: list[tuple[str, str, int | None, str]]) -> None:
    for event_type, message, user_id, level in events:
        log_event(event_type, message, user_id, level)


def _is_docx_container(file_path: str) -> bool:
    try:
        with zipfile.ZipFile(file_path) as archive:
            names = set(archive.namelist())
            return "[Content_Types].xml" in names and "word/document.xml" in names
    except Exception:
        return False


def _signature_valid(file_path: str, ext: str) -> bool:
    with open(file_path, "rb") as handle:
        head = handle.read(512)
    if ext == ".pdf":
        return head.startswith(b"%PDF-")
    if ext == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == ".docx":
        return _is_docx_container(file_path)
    return b"\x00" not in head


def _validate_support_file(file_path: str, ext: str) -> None:
    if magic is not None:
        try:
            detected = str(magic.from_file(file_path, mime=True) or "").lower().split(";")[0]
        except Exception:
            detected = ""
        allowed = {SUPPORT_MIME_TYPES[ext], *_SUPPORT_MIME_ALIASES.get(ext, set())}
        if detected and (detected in allowed or (ext == ".docx" and _is_docx_container(file_path))):
            return
        if ext in _SUPPORT_MAGIC_EXTENSIONS and _signature_valid(file_path, ext):
            return
        raise ValueError(f"Rejected MIME type for attachment: {ext}")
    if not _signature_valid(file_path, ext):
        raise ValueError(f"Rejected attachment contents: {ext}")


def _store_uploaded_file(uploaded_file: Any, stored_paths: list[str]) -> tuple[str, str, str, int]:
    base_dir = runtime_path(SUPPORT_UPLOAD_DIR)
    os.makedirs(base_dir, exist_ok=True)
    original_name = str(getattr(uploaded_file, "name", "attachment"))
    safe_name = sanitize_filename(original_name)
    ext = get_file_extension(safe_name).lower()
    if ext not in SUPPORT_ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported attachment extension: {ext or 'none'}")

    if hasattr(uploaded_file, "getbuffer"):
        data = bytes(uploaded_file.getbuffer())
    elif hasattr(uploaded_file, "read"):
        data = uploaded_file.read()
        if hasattr(data, "encode"):
            data = data.encode("utf-8")
        data = bytes(data)
    else:
        raise ValueError("Attachment must be an uploaded file.")

    max_bytes = settings.max_file_size_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise ValueError(f"Attachment exceeds the {settings.max_file_size_mb} MB limit.")

    with tempfile.NamedTemporaryFile(dir=base_dir, delete=False) as handle:
        handle.write(data)
        temp_path = handle.name
    try:
        _validate_support_file(temp_path, ext)
        target_path = os.path.join(base_dir, safe_name)
        counter = 1
        stem = Path(safe_name).stem
        while os.path.exists(target_path):
            target_path = os.path.join(base_dir, f"{stem}_{counter}{ext}")
            counter += 1
        os.replace(temp_path, target_path)
        stored_paths.append(target_path)
        return target_path, safe_name, SUPPORT_MIME_TYPES[ext], len(data)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _remove_files(paths: list[str]) -> None:
    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _attachment_rows(
    attachments: object,
    *,
    record_type: str,
    record_id: int,
    uploader_user_id: int | None,
    uploader_admin_id: int | None,
    stored_paths: list[str],
) -> list[SupportAttachment]:
    if attachments is None:
        return []
    items = list(attachments) if isinstance(attachments, (list, tuple)) else [attachments]
    rows: list[SupportAttachment] = []
    for item in items:
        file_path, safe_name, mime_type, size_bytes = _store_uploaded_file(item, stored_paths)
        original_name = str(getattr(item, "name", safe_name))
        rows.append(
            SupportAttachment(
                record_type=record_type,
                record_id=record_id,
                original_filename=original_name[:255],
                safe_filename=safe_name,
                file_path=file_path,
                mime_type=mime_type,
                extension=get_file_extension(safe_name).lower(),
                size_bytes=size_bytes,
                checksum=compute_file_hash(file_path),
                uploader_user_id=uploader_user_id,
                uploader_admin_id=uploader_admin_id,
                created_at=_utcnow(),
            )
        )
    return rows


def _add_notification(
    db: Any,
    *,
    session_id: int,
    title: str,
    message: str,
    event_key: str,
    target_page: str,
    notification_type: str = "system",
    severity: str = "info",
) -> None:
    if not notification_type_enabled(
        db,
        session_id=session_id,
        notification_type=notification_type,
        severity=severity,
    ):
        return
    key = _clean_text(event_key, 180)
    duplicate = (
        db.query(Notification.id)
        .filter(Notification.session_id == session_id, Notification.event_key == key)
        .first()
    )
    if duplicate:
        return
    db.add(
        Notification(
            session_id=session_id,
            notification_type=notification_type,
            title=_clean_text(title, 200),
            message=_clean_long_text(message, 2000),
            severity=severity,
            target_page=target_page,
            event_key=key,
            is_read=False,
            created_at=_utcnow(),
        )
    )
    db.flush()


def _notify_user(
    db: Any,
    *,
    user_id: int,
    title: str,
    message: str,
    event_key: str,
    target_page: str,
    severity: str = "info",
) -> None:
    sessions = (
        db.query(UserSession.id)
        .filter(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .all()
    )
    for session_row in sessions:
        _add_notification(
            db,
            session_id=int(session_row.id),
            title=title,
            message=message,
            event_key=f"{event_key}:session-{session_row.id}",
            target_page=target_page,
            severity=severity,
        )


def _notify_admins(
    db: Any,
    *,
    title: str,
    message: str,
    event_key: str,
    target_page: str,
    severity: str = "info",
) -> None:
    sessions = (
        db.query(UserSession.id)
        .join(User, UserSession.user_id == User.id)
        .filter(
            User.role == "admin",
            func.coalesce(User.account_status, "active") == "active",
            User.is_active.is_(True),
            UserSession.revoked_at.is_(None),
        )
        .all()
    )
    for session_row in sessions:
        _add_notification(
            db,
            session_id=int(session_row.id),
            title=title,
            message=message,
            event_key=f"{event_key}:admin-session-{session_row.id}",
            target_page=target_page,
            severity=severity,
        )


def _notify_all_users(
    db: Any,
    *,
    title: str,
    message: str,
    event_key: str,
    target_page: str,
) -> None:
    sessions = (
        db.query(UserSession.id)
        .join(User, UserSession.user_id == User.id)
        .filter(
            User.role != "admin",
            func.coalesce(User.account_status, "active") == "active",
            User.is_active.is_(True),
            UserSession.revoked_at.is_(None),
        )
        .all()
    )
    for session_row in sessions:
        _add_notification(
            db,
            session_id=int(session_row.id),
            title=title,
            message=message,
            event_key=f"{event_key}:user-session-{session_row.id}",
            target_page=target_page,
        )


def _attachment_dict(row: SupportAttachment) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "file_name": str(row.original_filename),
        "safe_filename": str(row.safe_filename),
        "mime_type": str(row.mime_type),
        "size_bytes": int(row.size_bytes or 0),
        "created_at": row.created_at,
    }


def _message_dict(db: Any, row: SupportMessage) -> dict[str, Any]:
    role = str(row.author_role)
    author_id = row.author_admin_id if role == "admin" else row.author_user_id
    return {
        "id": int(row.id),
        "message": str(row.message or ""),
        "author_role": role,
        "author_name": "System" if role == "system" else _account_name(db, author_id),
        "message_type": str(row.message_type),
        "created_at": row.created_at,
    }


def _ticket_counts(db: Any, ticket_id: int) -> tuple[int, bool, bool]:
    messages = db.query(SupportMessage).filter(SupportMessage.ticket_id == ticket_id).all()
    has_unread_admin_reply = any(
        str(row.author_role) == "admin" and not bool(row.is_read_by_user) for row in messages
    )
    has_unread_user_reply = any(
        str(row.author_role) == "user" and not bool(row.is_read_by_admin) for row in messages
    )
    return len(messages), has_unread_admin_reply, has_unread_user_reply


def _ticket_dict(
    db: Any,
    row: SupportTicket,
    *,
    detail: bool = False,
    include_internal: bool = False,
) -> dict[str, Any]:
    message_count, has_unread_admin_reply, has_unread_user_reply = _ticket_counts(db, int(row.id))
    username, user_email = _account_contact(db, row.user_id)
    result: dict[str, Any] = {
        "id": int(row.id),
        "ticket_id": str(row.ticket_id),
        "subject": str(row.subject),
        "category": str(row.category),
        "description": str(row.description or ""),
        "priority": str(row.priority),
        "status": str(row.status),
        "user_id": int(row.user_id),
        "user_name": _account_name(db, row.user_id),
        "user_username": username,
        "user_email": user_email,
        "assigned_admin_id": int(row.assigned_admin_id) if row.assigned_admin_id else None,
        "assigned_admin_name": (
            _account_name(db, row.assigned_admin_id) if row.assigned_admin_id else "Unassigned"
        ),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "last_activity_at": row.last_activity_at,
        "resolved_at": row.resolved_at,
        "message_count": message_count,
        "has_unread_admin_reply": has_unread_admin_reply,
        "has_unread_user_reply": has_unread_user_reply,
    }
    if detail:
        messages = (
            db.query(SupportMessage)
            .filter(SupportMessage.ticket_id == row.id)
            .order_by(asc(SupportMessage.created_at), asc(SupportMessage.id))
            .all()
        )
        result["messages"] = [_message_dict(db, message) for message in messages]
        attachments = (
            db.query(SupportAttachment)
            .filter(
                SupportAttachment.record_type == "ticket",
                SupportAttachment.record_id == row.id,
            )
            .order_by(asc(SupportAttachment.created_at), asc(SupportAttachment.id))
            .all()
        )
        result["attachments"] = [_attachment_dict(attachment) for attachment in attachments]
        result["resolution_history"] = [
            {
                "message": str(message.message or ""),
                "created_at": message.created_at,
            }
            for message in messages
            if str(message.message_type) == "status_change"
        ]
        if include_internal:
            notes = (
                db.query(SupportInternalNote)
                .filter(SupportInternalNote.ticket_id == row.id)
                .order_by(asc(SupportInternalNote.created_at), asc(SupportInternalNote.id))
                .all()
            )
            result["internal_notes"] = [
                {
                    "id": int(note.id),
                    "note": str(note.note or ""),
                    "author_name": _account_name(db, note.admin_user_id),
                    "created_at": note.created_at,
                }
                for note in notes
            ]
    return result


def _feedback_dict(db: Any, row: UserFeedback, *, detail: bool = False) -> dict[str, Any]:
    rating = int(row.rating or 5)
    username, user_email = _account_contact(db, row.user_id)
    result: dict[str, Any] = {
        "id": int(row.id),
        "feedback_id": str(row.feedback_id),
        "user_id": int(row.user_id),
        "user_name": _account_name(db, row.user_id),
        "user_username": username,
        "user_email": user_email,
        "category": str(row.category),
        "rating": rating,
        "rating_label": FEEDBACK_RATING_LABELS.get(rating, str(rating)),
        "details": str(row.details or ""),
        "status": str(row.status),
        "admin_response": str(row.admin_response or ""),
        "admin_response_at": row.responded_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if detail:
        result["attachments"] = _attachments_for(db, "feedback", int(row.id))
        notes = (
            db.query(SupportFeedbackNote)
            .filter(SupportFeedbackNote.feedback_id == row.id)
            .order_by(asc(SupportFeedbackNote.created_at), asc(SupportFeedbackNote.id))
            .all()
        )
        result["internal_notes"] = [
            {
                "id": int(note.id),
                "note": str(note.note or ""),
                "author_name": _account_name(db, note.admin_user_id),
                "created_at": note.created_at,
            }
            for note in notes
        ]
    return result


def _bug_dict(db: Any, row: BugReport, *, detail: bool = False) -> dict[str, Any]:
    username, user_email = _account_contact(db, row.user_id)
    result: dict[str, Any] = {
        "id": int(row.id),
        "report_id": str(row.report_id),
        "user_id": int(row.user_id),
        "user_name": _account_name(db, row.user_id),
        "user_username": username,
        "user_email": user_email,
        "title": str(row.title),
        "feature": str(row.feature),
        "description": str(row.description or ""),
        "steps_to_reproduce": str(row.steps_to_reproduce or ""),
        "expected_result": str(row.expected_result or ""),
        "actual_result": str(row.actual_result or ""),
        "severity": str(row.severity),
        "status": str(row.status),
        "admin_notes": str(row.admin_notes or ""),
        "resolution_details": str(row.resolution_details or ""),
        "resolved_at": row.resolved_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if detail:
        result["attachments"] = _attachments_for(db, "bug", int(row.id))
        result["resolution_history"] = _record_history(db, str(row.report_id))
    return result


def _feature_dict(
    db: Any,
    row: FeatureRequest,
    *,
    actor_user_id: int | None = None,
    vote_count: int | None = None,
    detail: bool = False,
) -> dict[str, Any]:
    if vote_count is None:
        vote_count = int(
            db.query(FeatureRequestVote.id).filter(FeatureRequestVote.request_id == row.id).count()
        )
    voted = False
    if actor_user_id is not None:
        voted = (
            db.query(FeatureRequestVote.id)
            .filter(
                FeatureRequestVote.request_id == row.id,
                FeatureRequestVote.user_id == actor_user_id,
            )
            .first()
            is not None
        )
    result: dict[str, Any] = {
        "id": int(row.id),
        "request_id": str(row.request_id),
        "user_id": int(row.user_id),
        "user_name": _account_name(db, row.user_id),
        "title": str(row.title),
        "description": str(row.description or ""),
        "category": str(row.category),
        "status": str(row.status),
        "admin_notes": str(row.admin_notes or ""),
        "vote_count": int(vote_count),
        "voted": voted,
        "published": bool(row.published),
        "announced": bool(row.announced),
        "released_at": row.released_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if detail:
        result["attachments"] = _attachments_for(db, "feature", int(row.id))
    return result


def _contact_dict(db: Any, row: ContactMessage, *, detail: bool = False) -> dict[str, Any]:
    username, user_email = _account_contact(db, row.user_id)
    result: dict[str, Any] = {
        "id": int(row.id),
        "reference_id": str(row.reference_id),
        "user_id": int(row.user_id),
        "user_name": _account_name(db, row.user_id),
        "user_username": username,
        "user_email": user_email,
        "subject": str(row.subject),
        "category": str(row.category),
        "message": str(row.message or ""),
        "status": str(row.status),
        "is_read_by_admin": bool(row.is_read_by_admin),
        "admin_reply": str(row.admin_reply or ""),
        "replied_at": row.replied_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if detail:
        result["attachments"] = _attachments_for(db, "contact", int(row.id))
    return result


def _faq_dict(db: Any, row: SupportFaq) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "faq_id": str(row.faq_id),
        "category": str(row.category),
        "question": str(row.question),
        "answer": str(row.answer or ""),
        "display_order": int(row.display_order or 0),
        "is_published": bool(row.is_published),
        "created_by_admin_name": _account_name(db, row.created_by_admin_id),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _average(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def _breakdown(rows: list[Any], attribute: str) -> dict[str, int]:
    return dict(Counter(str(getattr(row, attribute)) for row in rows))


def list_my_tickets(actor_user_id: int) -> list[dict[str, Any]]:
    with get_db_context() as db:
        user_id = _require_user(db, actor_user_id)
        rows = (
            db.query(SupportTicket)
            .filter(SupportTicket.user_id == user_id)
            .order_by(desc(SupportTicket.updated_at), desc(SupportTicket.id))
            .all()
        )
        return [_ticket_dict(db, row) for row in rows]


def create_ticket(
    actor_user_id: int,
    *,
    subject: str,
    category: str,
    description: str,
    priority: str = "Medium",
    attachments: object = None,
) -> dict[str, Any]:
    clean_subject = _clean_text(subject, 200, required=True, label="Subject")
    clean_category = _normalize_choice(category, SUPPORT_TICKET_CATEGORIES, "category")
    clean_description = _clean_long_text(description, 8000, required=True, label="Description")
    clean_priority = _normalize_choice(priority, SUPPORT_PRIORITIES, "priority", default="Medium")
    stored_paths: list[str] = []
    events: list[tuple[str, str, int | None, str]] = []
    try:
        with get_db_context() as db:
            user_id = _require_user(db, actor_user_id)
            ticket = SupportTicket(
                ticket_id=_new_ref("TKT"),
                user_id=user_id,
                subject=clean_subject,
                category=clean_category,
                description=clean_description,
                priority=clean_priority,
                status="Open",
                last_activity_at=_utcnow(),
                created_at=_utcnow(),
            )
            db.add(ticket)
            db.flush()
            db.add_all(
                _attachment_rows(
                    attachments,
                    record_type="ticket",
                    record_id=int(ticket.id),
                    uploader_user_id=user_id,
                    uploader_admin_id=None,
                    stored_paths=stored_paths,
                )
            )
            db.add(
                SupportMessage(
                    ticket_id=ticket.id,
                    author_user_id=user_id,
                    author_role="user",
                    message_type="reply",
                    message=clean_description,
                    is_read_by_user=True,
                    is_read_by_admin=False,
                    created_at=_utcnow(),
                )
            )
            _notify_admins(
                db,
                title="New support ticket",
                message=f"{ticket.ticket_id}: {clean_subject}",
                event_key=f"support-ticket-created-{ticket.ticket_id}",
                target_page="admin_support_tickets",
            )
            result = _ticket_dict(db, ticket)
            events.append(
                ("support_ticket_created", f"{ticket.ticket_id}: {clean_subject}", user_id, "info")
            )
        _emit(events)
        return result
    except Exception:
        _remove_files(stored_paths)
        raise


def get_user_ticket(ticket_id: int, actor_user_id: int) -> dict[str, Any]:
    with get_db_context() as db:
        user_id = _require_user(db, actor_user_id)
        row = (
            db.query(SupportTicket)
            .filter(
                SupportTicket.id == _as_positive_int(ticket_id, "Ticket"),
                SupportTicket.user_id == user_id,
            )
            .first()
        )
        if row is None:
            raise AuthError("This ticket is not available.")
        messages = db.query(SupportMessage).filter(SupportMessage.ticket_id == row.id).all()
        for message in messages:
            if str(message.author_role) == "admin":
                message.is_read_by_user = True
        return _ticket_dict(db, row, detail=True, include_internal=False)


def add_ticket_message(
    ticket_id: int,
    actor_user_id: int,
    *,
    message: str,
    attachments: object = None,
) -> dict[str, Any]:
    clean_message = _clean_long_text(message, 8000, required=True, label="Message")
    stored_paths: list[str] = []
    events: list[tuple[str, str, int | None, str]] = []
    try:
        with get_db_context() as db:
            user_id = _require_user(db, actor_user_id)
            ticket = (
                db.query(SupportTicket)
                .filter(
                    SupportTicket.id == _as_positive_int(ticket_id, "Ticket"),
                    SupportTicket.user_id == user_id,
                )
                .first()
            )
            if ticket is None:
                raise AuthError("This ticket is not available.")
            if str(ticket.status) in ("Resolved", "Closed"):
                raise ValueError("Reopen the ticket before adding another reply.")
            reply = SupportMessage(
                ticket_id=ticket.id,
                author_user_id=user_id,
                author_role="user",
                message_type="reply",
                message=clean_message,
                is_read_by_user=True,
                is_read_by_admin=False,
                created_at=_utcnow(),
            )
            db.add(reply)
            db.flush()
            db.add_all(
                _attachment_rows(
                    attachments,
                    record_type="ticket",
                    record_id=int(ticket.id),
                    uploader_user_id=user_id,
                    uploader_admin_id=None,
                    stored_paths=stored_paths,
                )
            )
            ticket.updated_at = _utcnow()
            ticket.last_activity_at = _utcnow()
            _notify_admins(
                db,
                title="New ticket reply",
                message=f"{ticket.ticket_id}: {clean_message[:180]}",
                event_key=f"support-ticket-reply-{reply.id}",
                target_page="admin_support_tickets",
            )
            result = _ticket_dict(db, ticket, detail=True, include_internal=False)
            events.append(("support_ticket_reply", f"{ticket.ticket_id} replied", user_id, "info"))
        _emit(events)
        return result
    except Exception:
        _remove_files(stored_paths)
        raise


def close_ticket(ticket_id: int, actor_user_id: int) -> dict[str, Any]:
    events: list[tuple[str, str, int | None, str]] = []
    with get_db_context() as db:
        user_id = _require_user(db, actor_user_id)
        ticket = (
            db.query(SupportTicket)
            .filter(
                SupportTicket.id == _as_positive_int(ticket_id, "Ticket"),
                SupportTicket.user_id == user_id,
            )
            .first()
        )
        if ticket is None:
            raise AuthError("This ticket is not available.")
        previous = str(ticket.status)
        ticket.status = "Closed"
        ticket.resolved_at = _utcnow()
        ticket.updated_at = _utcnow()
        ticket.last_activity_at = _utcnow()
        db.add(
            SupportMessage(
                ticket_id=ticket.id,
                author_role="system",
                message_type="status_change",
                message=f"Status changed from {previous} to Closed by the user.",
                metadata_json={"from": previous, "to": "Closed"},
                is_read_by_user=True,
                is_read_by_admin=False,
                created_at=_utcnow(),
            )
        )
        _notify_admins(
            db,
            title="Support ticket closed by user",
            message=f"{ticket.ticket_id} was closed.",
            event_key=f"support-ticket-closed-{ticket.id}",
            target_page="admin_support_tickets",
        )
        result = _ticket_dict(db, ticket)
        events.append(("support_ticket_closed", str(ticket.ticket_id), user_id, "info"))
    _emit(events)
    return result


def get_attachment_download(
    attachment_id: int,
    *,
    actor_user_id: int | None = None,
    actor_admin_id: int | None = None,
) -> dict[str, Any]:
    with get_db_context() as db:
        if actor_admin_id is not None:
            _require_admin(db, actor_admin_id)
        elif actor_user_id is not None:
            user_id = _require_user(db, actor_user_id)
        else:
            raise AuthError("Access to this attachment is not allowed.")
        row = (
            db.query(SupportAttachment)
            .filter(SupportAttachment.id == _as_positive_int(attachment_id, "Attachment"))
            .first()
        )
        if row is None:
            raise AuthError("This attachment is not available.")
        owner_id: int | None = None
        if str(row.record_type) == "ticket":
            parent = (
                db.query(SupportTicket.user_id).filter(SupportTicket.id == row.record_id).first()
            )
            owner_id = int(parent.user_id) if parent else None
        elif str(row.record_type) == "feedback":
            parent = db.query(UserFeedback.user_id).filter(UserFeedback.id == row.record_id).first()
            owner_id = int(parent.user_id) if parent else None
        elif str(row.record_type) == "bug":
            parent = db.query(BugReport.user_id).filter(BugReport.id == row.record_id).first()
            owner_id = int(parent.user_id) if parent else None
        elif str(row.record_type) == "feature":
            parent = (
                db.query(FeatureRequest.user_id).filter(FeatureRequest.id == row.record_id).first()
            )
            owner_id = int(parent.user_id) if parent else None
        elif str(row.record_type) == "contact":
            parent = (
                db.query(ContactMessage.user_id).filter(ContactMessage.id == row.record_id).first()
            )
            owner_id = int(parent.user_id) if parent else None
        if actor_admin_id is None and owner_id != user_id:
            raise AuthError("Access to this attachment is not allowed.")
        resolved_path = Path(str(row.file_path)).resolve()
        base_dir = runtime_path(SUPPORT_UPLOAD_DIR).resolve()
        if not resolved_path.is_relative_to(base_dir) or not resolved_path.is_file():
            raise AuthError("This attachment is not available.")
        return {
            "id": int(row.id),
            "file_name": str(row.original_filename),
            "mime_type": str(row.mime_type),
            "size_bytes": int(row.size_bytes or 0),
            "content": resolved_path.read_bytes(),
        }


def create_feedback(
    actor_user_id: int,
    *,
    category: str,
    rating: int,
    details: str,
    attachments: object = None,
) -> dict[str, Any]:
    clean_category = _normalize_choice(category, FEEDBACK_CATEGORIES, "category")
    clean_rating = _normalize_rating(rating)
    clean_details = _clean_long_text(details, 8000, required=True, label="Details")
    stored_paths: list[str] = []
    events: list[tuple[str, str, int | None, str]] = []
    try:
        with get_db_context() as db:
            user_id = _require_user(db, actor_user_id)
            row = UserFeedback(
                feedback_id=_new_ref("FB"),
                user_id=user_id,
                category=clean_category,
                rating=clean_rating,
                details=clean_details,
                status="New",
                created_at=_utcnow(),
            )
            db.add(row)
            db.flush()
            db.add_all(
                _attachment_rows(
                    attachments,
                    record_type="feedback",
                    record_id=int(row.id),
                    uploader_user_id=user_id,
                    uploader_admin_id=None,
                    stored_paths=stored_paths,
                )
            )
            _notify_admins(
                db,
                title="New user feedback",
                message=f"{row.feedback_id}: {clean_details[:180]}",
                event_key=f"support-feedback-created-{row.feedback_id}",
                target_page="admin_user_feedback",
                severity="success",
            )
            result = _feedback_dict(db, row)
            events.append(("user_feedback_submitted", str(row.feedback_id), user_id, "info"))
        _emit(events)
        return result
    except Exception:
        _remove_files(stored_paths)
        raise


def list_my_feedback(actor_user_id: int) -> list[dict[str, Any]]:
    with get_db_context() as db:
        user_id = _require_user(db, actor_user_id)
        rows = (
            db.query(UserFeedback)
            .filter(UserFeedback.user_id == user_id)
            .order_by(desc(UserFeedback.updated_at), desc(UserFeedback.id))
            .all()
        )
        return [_feedback_dict(db, row) for row in rows]


def list_feedback(
    actor_admin_id: int,
    status: str | None = None,
    *,
    search: str | None = None,
    category: str | None = None,
    rating: int | None = None,
    date_from: object = None,
    date_to: object = None,
) -> list[dict[str, Any]]:
    with get_db_context() as db:
        _require_admin(db, actor_admin_id)
        query = db.query(UserFeedback).join(User, UserFeedback.user_id == User.id)
        if status and status != "All":
            query = query.filter(
                UserFeedback.status == _normalize_choice(status, FEEDBACK_STATUSES, "status")
            )
        pattern = _like_pattern(search)
        if pattern:
            query = query.filter(
                or_(
                    func.lower(UserFeedback.feedback_id).like(pattern),
                    func.lower(UserFeedback.details).like(pattern),
                    func.lower(User.username).like(pattern),
                    func.lower(User.email).like(pattern),
                )
            )
        if category and category != "All":
            query = query.filter(
                UserFeedback.category
                == _normalize_choice(category, FEEDBACK_CATEGORIES, "category")
            )
        if rating not in (None, "", "All"):
            query = query.filter(UserFeedback.rating == _normalize_rating(rating))
        query = _date_window(UserFeedback.created_at, query, date_from, date_to)
        rows = query.order_by(desc(UserFeedback.updated_at), desc(UserFeedback.id)).all()
        return [_feedback_dict(db, row) for row in rows]


def get_feedback_detail(feedback_id: int, actor_admin_id: int) -> dict[str, Any]:
    with get_db_context() as db:
        _require_admin(db, actor_admin_id)
        row = (
            db.query(UserFeedback)
            .filter(UserFeedback.id == _as_positive_int(feedback_id, "Feedback"))
            .first()
        )
        if row is None:
            raise AuthError("Feedback record not found.")
        return _feedback_dict(db, row, detail=True)


def set_feedback_status(feedback_id: int, actor_admin_id: int, *, status: str) -> dict[str, Any]:
    clean_status = _normalize_choice(status, FEEDBACK_STATUSES, "status")
    events: list[tuple[str, str, int | None, str]] = []
    with get_db_context() as db:
        admin = _require_admin(db, actor_admin_id)
        row = (
            db.query(UserFeedback)
            .filter(UserFeedback.id == _as_positive_int(feedback_id, "Feedback"))
            .first()
        )
        if row is None:
            raise AuthError("Feedback record not found.")
        row.status = clean_status
        row.updated_at = _utcnow()
        result = _feedback_dict(db, row, detail=True)
        events.append(("user_feedback_status_updated", str(row.feedback_id), admin.id, "info"))
    _emit(events)
    return result


def add_feedback_note(feedback_id: int, actor_admin_id: int, *, note: str) -> dict[str, Any]:
    clean_note = _clean_long_text(note, 8000, required=True, label="Note")
    events: list[tuple[str, str, int | None, str]] = []
    with get_db_context() as db:
        admin = _require_admin(db, actor_admin_id)
        row = (
            db.query(UserFeedback)
            .filter(UserFeedback.id == _as_positive_int(feedback_id, "Feedback"))
            .first()
        )
        if row is None:
            raise AuthError("Feedback record not found.")
        db.add(
            SupportFeedbackNote(
                feedback_id=row.id,
                admin_user_id=admin.id,
                note=clean_note,
                created_at=_utcnow(),
            )
        )
        row.updated_at = _utcnow()
        result = _feedback_dict(db, row, detail=True)
        events.append(("user_feedback_internal_note", str(row.feedback_id), admin.id, "info"))
    _emit(events)
    return result


def respond_feedback(
    feedback_id: int,
    actor_admin_id: int,
    *,
    response: str,
    status: str = "Responded",
) -> dict[str, Any]:
    clean_response = _clean_long_text(response, 8000, required=True, label="Response")
    clean_status = _normalize_choice(status, FEEDBACK_STATUSES, "status", default="Responded")
    events: list[tuple[str, str, int | None, str]] = []
    with get_db_context() as db:
        admin = _require_admin(db, actor_admin_id)
        row = (
            db.query(UserFeedback)
            .filter(UserFeedback.id == _as_positive_int(feedback_id, "Feedback"))
            .first()
        )
        if row is None:
            raise AuthError("Feedback record not found.")
        row.admin_response = clean_response
        row.status = clean_status
        row.responded_by_admin_id = admin.id
        row.responded_at = _utcnow()
        row.updated_at = _utcnow()
        _notify_user(
            db,
            user_id=int(row.user_id),
            title="Feedback response",
            message=clean_response[:500],
            event_key=f"support-feedback-response-{row.id}",
            target_page="my_feedback",
            severity="success",
        )
        result = _feedback_dict(db, row, detail=True)
        events.append(("user_feedback_responded", str(row.feedback_id), admin.id, "info"))
    _emit(events)
    return result


def create_bug_report(
    actor_user_id: int,
    *,
    title: str,
    feature: str,
    description: str,
    steps: str,
    expected: str,
    actual: str,
    severity: str,
    attachments: object = None,
) -> dict[str, Any]:
    clean_title = _clean_text(title, 200, required=True, label="Title")
    clean_feature = _clean_text(feature, 120, required=True, label="Affected feature")
    clean_description = _clean_long_text(description, 8000, required=True, label="Description")
    clean_steps = _clean_long_text(steps, 8000)
    clean_expected = _clean_long_text(expected, 4000)
    clean_actual = _clean_long_text(actual, 4000)
    clean_severity = _normalize_choice(severity, BUG_SEVERITIES, "severity")
    stored_paths: list[str] = []
    events: list[tuple[str, str, int | None, str]] = []
    try:
        with get_db_context() as db:
            user_id = _require_user(db, actor_user_id)
            row = BugReport(
                report_id=_new_ref("BUG"),
                user_id=user_id,
                title=clean_title,
                feature=clean_feature,
                description=clean_description,
                steps_to_reproduce=clean_steps,
                expected_result=clean_expected,
                actual_result=clean_actual,
                severity=clean_severity,
                status="New",
                created_at=_utcnow(),
            )
            db.add(row)
            db.flush()
            db.add_all(
                _attachment_rows(
                    attachments,
                    record_type="bug",
                    record_id=int(row.id),
                    uploader_user_id=user_id,
                    uploader_admin_id=None,
                    stored_paths=stored_paths,
                )
            )
            _notify_admins(
                db,
                title="New bug report",
                message=f"{row.report_id}: {clean_title}",
                event_key=f"support-bug-created-{row.report_id}",
                target_page="admin_bug_reports",
                severity="warning",
            )
            result = _bug_dict(db, row)
            events.append(("bug_report_submitted", str(row.report_id), user_id, "info"))
        _emit(events)
        return result
    except Exception:
        _remove_files(stored_paths)
        raise


def list_my_bug_reports(actor_user_id: int) -> list[dict[str, Any]]:
    with get_db_context() as db:
        user_id = _require_user(db, actor_user_id)
        rows = (
            db.query(BugReport)
            .filter(BugReport.user_id == user_id)
            .order_by(desc(BugReport.updated_at), desc(BugReport.id))
            .all()
        )
        return [_bug_dict(db, row) for row in rows]


def list_bug_reports(
    actor_admin_id: int,
    status: str | None = None,
    *,
    search: str | None = None,
    severity: str | None = None,
    feature: str | None = None,
    date_from: object = None,
    date_to: object = None,
) -> list[dict[str, Any]]:
    with get_db_context() as db:
        _require_admin(db, actor_admin_id)
        query = db.query(BugReport).join(User, BugReport.user_id == User.id)
        if status and status != "All":
            query = query.filter(
                BugReport.status
                == _normalize_status_alias(status, BUG_STATUS_ALIASES, BUG_STATUSES)
            )
        pattern = _like_pattern(search)
        if pattern:
            query = query.filter(
                or_(
                    func.lower(BugReport.report_id).like(pattern),
                    func.lower(BugReport.title).like(pattern),
                    func.lower(BugReport.description).like(pattern),
                    func.lower(BugReport.feature).like(pattern),
                    func.lower(User.username).like(pattern),
                )
            )
        if severity and severity != "All":
            query = query.filter(
                BugReport.severity == _normalize_choice(severity, BUG_SEVERITIES, "severity")
            )
        if feature and feature != "All":
            query = query.filter(BugReport.feature == _clean_text(feature, 120))
        query = _date_window(BugReport.created_at, query, date_from, date_to)
        rows = query.order_by(desc(BugReport.updated_at), desc(BugReport.id)).all()
        return [_bug_dict(db, row) for row in rows]


def get_bug_report(report_id: int, actor_admin_id: int) -> dict[str, Any]:
    with get_db_context() as db:
        _require_admin(db, actor_admin_id)
        row = (
            db.query(BugReport)
            .filter(BugReport.id == _as_positive_int(report_id, "Bug report"))
            .first()
        )
        if row is None:
            raise AuthError("Bug report not found.")
        return _bug_dict(db, row, detail=True)


def update_bug_report(
    report_id: int,
    actor_admin_id: int,
    *,
    status: str,
    admin_notes: str = "",
    resolution_details: str = "",
) -> dict[str, Any]:
    clean_status = _normalize_status_alias(status, BUG_STATUS_ALIASES, BUG_STATUSES)
    clean_notes = _clean_long_text(admin_notes, 8000)
    clean_resolution = _clean_long_text(resolution_details, 8000)
    events: list[tuple[str, str, int | None, str]] = []
    with get_db_context() as db:
        admin = _require_admin(db, actor_admin_id)
        row = (
            db.query(BugReport)
            .filter(BugReport.id == _as_positive_int(report_id, "Bug report"))
            .first()
        )
        if row is None:
            raise AuthError("Bug report not found.")
        previous_status = str(row.status)
        row.status = clean_status
        row.admin_notes = clean_notes
        row.resolution_details = clean_resolution
        if clean_status == "Fixed":
            row.resolved_at = _utcnow()
            row.resolved_by_admin_id = admin.id
        else:
            row.resolved_at = None
            row.resolved_by_admin_id = None
        row.updated_at = _utcnow()
        _notify_user(
            db,
            user_id=int(row.user_id),
            title="Bug report updated",
            message=f"{row.report_id} is now {clean_status}.",
            event_key=f"support-bug-status-{row.id}-{clean_status}-{row.updated_at}",
            target_page="report_bug",
        )
        result = _bug_dict(db, row, detail=True)
        events.append(
            (
                "bug_report_updated",
                f"{row.report_id}: {previous_status} -> {clean_status}",
                admin.id,
                "info",
            )
        )
    _emit(events)
    return result


def create_feature_request(
    actor_user_id: int,
    *,
    title: str,
    description: str,
    category: str,
    attachments: object = None,
) -> dict[str, Any]:
    clean_title = _clean_text(title, 200, required=True, label="Title")
    clean_description = _clean_long_text(description, 8000, required=True, label="Description")
    clean_category = _normalize_choice(category, FEATURE_CATEGORIES, "category")
    stored_paths: list[str] = []
    events: list[tuple[str, str, int | None, str]] = []
    try:
        with get_db_context() as db:
            user_id = _require_user(db, actor_user_id)
            row = FeatureRequest(
                request_id=_new_ref("FR"),
                user_id=user_id,
                title=clean_title,
                description=clean_description,
                category=clean_category,
                status="Submitted",
                published=True,
                created_at=_utcnow(),
            )
            db.add(row)
            db.flush()
            db.add_all(
                _attachment_rows(
                    attachments,
                    record_type="feature",
                    record_id=int(row.id),
                    uploader_user_id=user_id,
                    uploader_admin_id=None,
                    stored_paths=stored_paths,
                )
            )
            db.add(
                FeatureRequestVote(
                    request_id=row.id,
                    user_id=user_id,
                    created_at=_utcnow(),
                )
            )
            db.flush()
            _notify_admins(
                db,
                title="New feature request",
                message=f"{row.request_id}: {clean_title}",
                event_key=f"support-feature-created-{row.request_id}",
                target_page="admin_feature_requests",
            )
            result = _feature_dict(db, row, actor_user_id=user_id, vote_count=1)
            events.append(("feature_request_submitted", str(row.request_id), user_id, "info"))
        _emit(events)
        return result
    except Exception:
        _remove_files(stored_paths)
        raise


def list_feature_requests(actor_user_id: int) -> list[dict[str, Any]]:
    with get_db_context() as db:
        user_id = _require_user(db, actor_user_id)
        rows = (
            db.query(FeatureRequest)
            .filter(FeatureRequest.published.is_(True))
            .order_by(desc(FeatureRequest.updated_at), desc(FeatureRequest.id))
            .all()
        )
        return [_feature_dict(db, row, actor_user_id=user_id) for row in rows]


def toggle_feature_request_vote(request_id: int, actor_user_id: int) -> bool:
    events: list[tuple[str, str, int | None, str]] = []
    with get_db_context() as db:
        user_id = _require_user(db, actor_user_id)
        row = (
            db.query(FeatureRequest)
            .filter(
                FeatureRequest.id == _as_positive_int(request_id, "Feature request"),
                FeatureRequest.published.is_(True),
            )
            .first()
        )
        if row is None:
            raise AuthError("This feature request is not available.")
        vote = (
            db.query(FeatureRequestVote)
            .filter(
                FeatureRequestVote.request_id == row.id,
                FeatureRequestVote.user_id == user_id,
            )
            .first()
        )
        if vote is not None:
            db.delete(vote)
            voted = False
            action = "vote removed"
        else:
            db.add(FeatureRequestVote(request_id=row.id, user_id=user_id, created_at=_utcnow()))
            voted = True
            action = "vote added"
            _notify_user(
                db,
                user_id=int(row.user_id),
                title="Feature request vote",
                message=f"Someone voted for {row.title}.",
                event_key=f"support-feature-vote-{row.id}-{user_id}",
                target_page="feature_requests",
            )
        row.updated_at = _utcnow()
        events.append(("feature_request_vote", f"{row.request_id} {action}", user_id, "info"))
    _emit(events)
    return voted


def list_admin_feature_requests(
    actor_admin_id: int,
    status: str | None = None,
    *,
    search: str | None = None,
    category: str | None = None,
    date_from: object = None,
    date_to: object = None,
) -> list[dict[str, Any]]:
    with get_db_context() as db:
        _require_admin(db, actor_admin_id)
        query = (
            db.query(
                FeatureRequest,
                func.count(FeatureRequestVote.id).label("vote_count"),
            )
            .join(User, FeatureRequest.user_id == User.id)
            .outerjoin(FeatureRequestVote, FeatureRequestVote.request_id == FeatureRequest.id)
            .group_by(FeatureRequest.id)
        )
        if status and status != "All":
            query = query.filter(
                FeatureRequest.status
                == _normalize_status_alias(status, FEATURE_STATUS_ALIASES, FEATURE_STATUSES)
            )
        pattern = _like_pattern(search)
        if pattern:
            query = query.filter(
                or_(
                    func.lower(FeatureRequest.request_id).like(pattern),
                    func.lower(FeatureRequest.title).like(pattern),
                    func.lower(FeatureRequest.description).like(pattern),
                    func.lower(User.username).like(pattern),
                )
            )
        if category and category != "All":
            query = query.filter(
                FeatureRequest.category
                == _normalize_choice(category, FEATURE_CATEGORIES, "category")
            )
        query = _date_window(FeatureRequest.created_at, query, date_from, date_to)
        rows = query.order_by(desc(FeatureRequest.updated_at), desc(FeatureRequest.id)).all()
        return [_feature_dict(db, row, vote_count=int(vote_count)) for row, vote_count in rows]


def get_feature_request(request_id: int, actor_admin_id: int) -> dict[str, Any]:
    with get_db_context() as db:
        _require_admin(db, actor_admin_id)
        row = (
            db.query(FeatureRequest)
            .filter(FeatureRequest.id == _as_positive_int(request_id, "Feature request"))
            .first()
        )
        if row is None:
            raise AuthError("Feature request not found.")
        vote_count = int(
            db.query(FeatureRequestVote.id).filter(FeatureRequestVote.request_id == row.id).count()
        )
        return _feature_dict(db, row, vote_count=vote_count, detail=True)


def update_feature_request(
    request_id: int,
    actor_admin_id: int,
    *,
    status: str,
    admin_notes: str = "",
    published: bool = False,
    announced: bool = False,
) -> dict[str, Any]:
    clean_status = _normalize_status_alias(status, FEATURE_STATUS_ALIASES, FEATURE_STATUSES)
    clean_notes = _clean_long_text(admin_notes, 8000)
    events: list[tuple[str, str, int | None, str]] = []
    with get_db_context() as db:
        admin = _require_admin(db, actor_admin_id)
        row = (
            db.query(FeatureRequest)
            .filter(FeatureRequest.id == _as_positive_int(request_id, "Feature request"))
            .first()
        )
        if row is None:
            raise AuthError("Feature request not found.")
        previous_published = bool(row.published)
        previous_status = str(row.status)
        row.status = clean_status
        row.admin_notes = clean_notes
        row.published = bool(published)
        row.announced = bool(announced)
        if clean_status == "Released":
            row.released_at = _utcnow()
        else:
            row.released_at = None
        row.updated_at = _utcnow()
        if bool(announced) or (bool(published) and not previous_published):
            _notify_user(
                db,
                user_id=int(row.user_id),
                title="Feature request update",
                message=f"{row.title} is now {clean_status}.",
                event_key=f"support-feature-status-{row.id}-{clean_status}",
                target_page="feature_requests",
                severity="success",
            )
        if clean_status != previous_status and not bool(announced):
            _notify_user(
                db,
                user_id=int(row.user_id),
                title="Feature request update",
                message=f"{row.title} is now {clean_status}.",
                event_key=f"support-feature-status-{row.id}-{clean_status}",
                target_page="feature_requests",
            )
        result = _feature_dict(db, row, detail=True)
        events.append(
            (
                "feature_request_updated",
                f"{row.request_id}: {previous_status} -> {clean_status}",
                admin.id,
                "info",
            )
        )
    _emit(events)
    return result


def create_contact_message(
    actor_user_id: int,
    *,
    subject: str,
    category: str,
    message: str,
    attachments: object = None,
) -> dict[str, Any]:
    clean_subject = _clean_text(subject, 200, required=True, label="Subject")
    clean_category = _normalize_choice(category, CONTACT_CATEGORIES, "category")
    clean_message = _clean_long_text(message, 8000, required=True, label="Message")
    stored_paths: list[str] = []
    events: list[tuple[str, str, int | None, str]] = []
    try:
        with get_db_context() as db:
            user_id = _require_user(db, actor_user_id)
            row = ContactMessage(
                reference_id=_new_ref("MSG"),
                user_id=user_id,
                subject=clean_subject,
                category=clean_category,
                message=clean_message,
                status="New",
                is_read_by_admin=False,
                created_at=_utcnow(),
            )
            db.add(row)
            db.flush()
            db.add_all(
                _attachment_rows(
                    attachments,
                    record_type="contact",
                    record_id=int(row.id),
                    uploader_user_id=user_id,
                    uploader_admin_id=None,
                    stored_paths=stored_paths,
                )
            )
            _notify_admins(
                db,
                title="New contact message",
                message=f"{row.reference_id}: {clean_subject}",
                event_key=f"support-contact-created-{row.reference_id}",
                target_page="admin_contact_messages",
            )
            result = _contact_dict(db, row)
            events.append(("contact_message_submitted", str(row.reference_id), user_id, "info"))
        _emit(events)
        return result
    except Exception:
        _remove_files(stored_paths)
        raise


def list_my_contact_messages(actor_user_id: int) -> list[dict[str, Any]]:
    with get_db_context() as db:
        user_id = _require_user(db, actor_user_id)
        rows = (
            db.query(ContactMessage)
            .filter(ContactMessage.user_id == user_id)
            .order_by(desc(ContactMessage.updated_at), desc(ContactMessage.id))
            .all()
        )
        return [_contact_dict(db, row) for row in rows]


def list_contact_messages(
    actor_admin_id: int,
    unread_only: bool = False,
    *,
    search: str | None = None,
    read_filter: str | None = None,
    status: str | None = None,
    date_from: object = None,
    date_to: object = None,
) -> list[dict[str, Any]]:
    with get_db_context() as db:
        _require_admin(db, actor_admin_id)
        query = db.query(ContactMessage).join(User, ContactMessage.user_id == User.id)
        if unread_only:
            query = query.filter(ContactMessage.is_read_by_admin.is_(False))
        if read_filter in ("read", "unread"):
            query = query.filter(ContactMessage.is_read_by_admin.is_(read_filter == "read"))
        if status and status != "All":
            query = query.filter(
                ContactMessage.status == _normalize_choice(status, CONTACT_STATUSES, "status")
            )
        pattern = _like_pattern(search)
        if pattern:
            query = query.filter(
                or_(
                    func.lower(ContactMessage.reference_id).like(pattern),
                    func.lower(ContactMessage.subject).like(pattern),
                    func.lower(ContactMessage.message).like(pattern),
                    func.lower(User.username).like(pattern),
                    func.lower(User.full_name).like(pattern),
                    func.lower(User.email).like(pattern),
                )
            )
        query = _date_window(ContactMessage.created_at, query, date_from, date_to)
        rows = query.order_by(desc(ContactMessage.updated_at), desc(ContactMessage.id)).all()
        return [_contact_dict(db, row) for row in rows]


def get_contact_message(message_id: int, actor_admin_id: int) -> dict[str, Any]:
    with get_db_context() as db:
        _require_admin(db, actor_admin_id)
        row = (
            db.query(ContactMessage)
            .filter(ContactMessage.id == _as_positive_int(message_id, "Contact message"))
            .first()
        )
        if row is None:
            raise AuthError("Contact message not found.")
        return _contact_dict(db, row, detail=True)


def reply_contact_message(message_id: int, actor_admin_id: int, *, reply: str) -> dict[str, Any]:
    clean_reply = _clean_long_text(reply, 8000, required=True, label="Reply")
    events: list[tuple[str, str, int | None, str]] = []
    with get_db_context() as db:
        admin = _require_admin(db, actor_admin_id)
        row = (
            db.query(ContactMessage)
            .filter(ContactMessage.id == _as_positive_int(message_id, "Contact message"))
            .first()
        )
        if row is None:
            raise AuthError("Contact message not found.")
        row.admin_reply = clean_reply
        row.status = "Replied"
        row.is_read_by_admin = True
        row.replied_by_admin_id = admin.id
        row.replied_at = _utcnow()
        row.updated_at = _utcnow()
        _notify_user(
            db,
            user_id=int(row.user_id),
            title="Support reply",
            message=clean_reply[:500],
            event_key=f"support-contact-reply-{row.id}",
            target_page="contact_support",
            severity="success",
        )
        result = _contact_dict(db, row, detail=True)
        events.append(("contact_message_replied", str(row.reference_id), admin.id, "info"))
    _emit(events)
    return result


def set_contact_read(message_id: int, actor_admin_id: int, *, is_read: bool) -> dict[str, Any]:
    events: list[tuple[str, str, int | None, str]] = []
    with get_db_context() as db:
        admin = _require_admin(db, actor_admin_id)
        row = (
            db.query(ContactMessage)
            .filter(ContactMessage.id == _as_positive_int(message_id, "Contact message"))
            .first()
        )
        if row is None:
            raise AuthError("Contact message not found.")
        previous = bool(row.is_read_by_admin)
        row.is_read_by_admin = bool(is_read)
        if is_read and str(row.status) == "New":
            row.status = "Read"
        elif not is_read and str(row.status) in ("Read", "Replied", "Closed"):
            row.status = "New"
        if previous != bool(is_read):
            row.updated_at = _utcnow()
        result = _contact_dict(db, row, detail=True)
        events.append(
            (
                "contact_message_read" if is_read else "contact_message_unread",
                str(row.reference_id),
                admin.id,
                "info",
            )
        )
    _emit(events)
    return result


def mark_contact_read(message_id: int, actor_admin_id: int) -> dict[str, Any]:
    return set_contact_read(message_id, actor_admin_id, is_read=True)


def set_contact_status(message_id: int, actor_admin_id: int, *, status: str) -> dict[str, Any]:
    clean_status = _normalize_choice(status, CONTACT_STATUSES, "status")
    events: list[tuple[str, str, int | None, str]] = []
    with get_db_context() as db:
        admin = _require_admin(db, actor_admin_id)
        row = (
            db.query(ContactMessage)
            .filter(ContactMessage.id == _as_positive_int(message_id, "Contact message"))
            .first()
        )
        if row is None:
            raise AuthError("Contact message not found.")
        row.status = clean_status
        row.updated_at = _utcnow()
        result = _contact_dict(db, row, detail=True)
        events.append(("contact_message_status_updated", str(row.reference_id), admin.id, "info"))
    _emit(events)
    return result


def delete_contact_message(message_id: int, actor_admin_id: int) -> dict[str, Any]:
    events: list[tuple[str, str, int | None, str]] = []
    removed_paths: list[str] = []
    with get_db_context() as db:
        admin = _require_admin(db, actor_admin_id)
        row = (
            db.query(ContactMessage)
            .filter(ContactMessage.id == _as_positive_int(message_id, "Contact message"))
            .first()
        )
        if row is None:
            raise AuthError("Contact message not found.")
        reference = str(row.reference_id)
        attachments = (
            db.query(SupportAttachment)
            .filter(
                SupportAttachment.record_type == "contact",
                SupportAttachment.record_id == row.id,
            )
            .all()
        )
        base_dir = runtime_path(SUPPORT_UPLOAD_DIR).resolve()
        for item in attachments:
            candidate = Path(str(item.file_path or "")).resolve()
            if candidate.is_relative_to(base_dir) and candidate.is_file():
                removed_paths.append(str(candidate))
        db.query(SupportAttachment).filter(
            SupportAttachment.record_type == "contact",
            SupportAttachment.record_id == row.id,
        ).delete(synchronize_session=False)
        db.delete(row)
        events.append(("contact_message_deleted", reference, admin.id, "warning"))
    _remove_files(removed_paths)
    _emit(events)
    return {"id": int(message_id), "reference_id": reference, "deleted": True}


def list_support_tickets(
    actor_admin_id: int,
    status: str | None = None,
    priority: str | None = None,
    assignee_user_id: int | None = None,
    *,
    search: str | None = None,
    category: str | None = None,
    date_from: object = None,
    date_to: object = None,
) -> list[dict[str, Any]]:
    with get_db_context() as db:
        _require_admin(db, actor_admin_id)
        query = db.query(SupportTicket).join(User, SupportTicket.user_id == User.id)
        if status and status != "All":
            query = query.filter(
                SupportTicket.status == _normalize_choice(status, SUPPORT_TICKET_STATUSES, "status")
            )
        if priority and priority != "All":
            query = query.filter(
                SupportTicket.priority
                == _normalize_choice(priority, SUPPORT_PRIORITIES, "priority")
            )
        if assignee_user_id:
            query = query.filter(SupportTicket.assigned_admin_id == int(assignee_user_id))
        pattern = _like_pattern(search)
        if pattern:
            query = query.filter(
                or_(
                    func.lower(SupportTicket.ticket_id).like(pattern),
                    func.lower(SupportTicket.subject).like(pattern),
                    func.lower(User.username).like(pattern),
                    func.lower(User.full_name).like(pattern),
                    func.lower(User.email).like(pattern),
                )
            )
        if category and category != "All":
            query = query.filter(
                SupportTicket.category
                == _normalize_choice(category, SUPPORT_TICKET_CATEGORIES, "category")
            )
        query = _date_window(SupportTicket.created_at, query, date_from, date_to)
        rows = query.order_by(desc(SupportTicket.updated_at), desc(SupportTicket.id)).all()
        return [_ticket_dict(db, row) for row in rows]


def get_admin_ticket(ticket_id: int, actor_admin_id: int) -> dict[str, Any]:
    with get_db_context() as db:
        _require_admin(db, actor_admin_id)
        row = (
            db.query(SupportTicket)
            .filter(SupportTicket.id == _as_positive_int(ticket_id, "Ticket"))
            .first()
        )
        if row is None:
            raise AuthError("Ticket not found.")
        messages = db.query(SupportMessage).filter(SupportMessage.ticket_id == row.id).all()
        for message in messages:
            if str(message.author_role) == "user":
                message.is_read_by_admin = True
        return _ticket_dict(db, row, detail=True, include_internal=True)


def admin_reply_ticket(
    ticket_id: int,
    actor_admin_id: int,
    *,
    message: str = "",
    is_internal: bool = False,
    attachments: object = None,
) -> dict[str, Any]:
    clean_message = _clean_long_text(message, 8000, required=True, label="Message")
    if is_internal and attachments is not None:
        raise ValueError("Attachments cannot be added to a private internal note.")
    stored_paths: list[str] = []
    events: list[tuple[str, str, int | None, str]] = []
    try:
        with get_db_context() as db:
            admin = _require_admin(db, actor_admin_id)
            ticket = (
                db.query(SupportTicket)
                .filter(SupportTicket.id == _as_positive_int(ticket_id, "Ticket"))
                .first()
            )
            if ticket is None:
                raise AuthError("Ticket not found.")
            if is_internal:
                db.add(
                    SupportInternalNote(
                        ticket_id=ticket.id,
                        admin_user_id=admin.id,
                        note=clean_message,
                        created_at=_utcnow(),
                    )
                )
                events.append(
                    ("support_ticket_internal_note", str(ticket.ticket_id), admin.id, "info")
                )
            else:
                reply = SupportMessage(
                    ticket_id=ticket.id,
                    author_admin_id=admin.id,
                    author_role="admin",
                    message_type="reply",
                    message=clean_message,
                    is_read_by_user=False,
                    is_read_by_admin=True,
                    created_at=_utcnow(),
                )
                db.add(reply)
                db.flush()
                db.add_all(
                    _attachment_rows(
                        attachments,
                        record_type="ticket",
                        record_id=int(ticket.id),
                        uploader_user_id=None,
                        uploader_admin_id=int(admin.id),
                        stored_paths=stored_paths,
                    )
                )
                _notify_user(
                    db,
                    user_id=int(ticket.user_id),
                    title="Support ticket reply",
                    message=clean_message[:500],
                    event_key=f"support-ticket-admin-reply-{reply.id}",
                    target_page="my_support_tickets",
                    severity="success",
                )
                events.append(("support_ticket_reply", str(ticket.ticket_id), admin.id, "info"))
            ticket.updated_at = _utcnow()
            ticket.last_activity_at = _utcnow()
            result = _ticket_dict(db, ticket, detail=True, include_internal=True)
        _emit(events)
        return result
    except Exception:
        _remove_files(stored_paths)
        raise


def update_ticket_status(
    ticket_id: int,
    actor_admin_id: int,
    *,
    status: str,
    assigned_admin_id: int | None = None,
    clear_assignee: bool = False,
    priority: str | None = None,
) -> dict[str, Any]:
    clean_status = _normalize_choice(status, SUPPORT_TICKET_STATUSES, "status")
    clean_priority = (
        _normalize_choice(priority, SUPPORT_PRIORITIES, "priority")
        if priority not in (None, "", "All")
        else None
    )
    events: list[tuple[str, str, int | None, str]] = []
    with get_db_context() as db:
        admin = _require_admin(db, actor_admin_id)
        ticket = (
            db.query(SupportTicket)
            .filter(SupportTicket.id == _as_positive_int(ticket_id, "Ticket"))
            .first()
        )
        if ticket is None:
            raise AuthError("Ticket not found.")
        if assigned_admin_id is not None:
            assignee = (
                db.query(User)
                .filter(User.id == int(assigned_admin_id), User.role == "admin")
                .first()
            )
            if assignee is None:
                raise AuthError("Select a valid administrator.")
        previous_status = str(ticket.status)
        previous_assignee = ticket.assigned_admin_id
        previous_priority = str(ticket.priority)
        ticket.status = clean_status
        if clear_assignee:
            ticket.assigned_admin_id = None
        elif assigned_admin_id is not None:
            ticket.assigned_admin_id = int(assigned_admin_id)
        if clean_priority is not None:
            ticket.priority = clean_priority
        if clean_status in ("Resolved", "Closed"):
            ticket.resolved_at = _utcnow()
        else:
            ticket.resolved_at = None
        ticket.updated_at = _utcnow()
        ticket.last_activity_at = _utcnow()
        change_text = f"Status changed from {previous_status} to {clean_status}."
        if previous_assignee != ticket.assigned_admin_id:
            change_text += " Assignee updated."
        if clean_priority is not None and clean_priority != previous_priority:
            change_text += f" Priority changed from {previous_priority} to {clean_priority}."
        db.add(
            SupportMessage(
                ticket_id=ticket.id,
                author_admin_id=admin.id,
                author_role="system",
                message_type="status_change",
                message=change_text,
                metadata_json={
                    "from": previous_status,
                    "to": clean_status,
                    "assigned_admin_id": ticket.assigned_admin_id,
                    "priority": ticket.priority,
                },
                is_read_by_user=False,
                is_read_by_admin=True,
                created_at=_utcnow(),
            )
        )
        _notify_user(
            db,
            user_id=int(ticket.user_id),
            title="Support ticket updated",
            message=change_text,
            event_key=f"support-ticket-status-{ticket.id}-{clean_status}-{ticket.updated_at}",
            target_page="my_support_tickets",
        )
        result = _ticket_dict(db, ticket, detail=True, include_internal=True)
        events.append(("support_ticket_updated", str(ticket.ticket_id), admin.id, "info"))
    _emit(events)
    return result


def reopen_ticket(ticket_id: int, actor_admin_id: int) -> dict[str, Any]:
    with get_readonly_db() as db:
        _require_admin(db, actor_admin_id)
        row = (
            db.query(SupportTicket)
            .filter(SupportTicket.id == _as_positive_int(ticket_id, "Ticket"))
            .first()
        )
        if row is None:
            raise AuthError("Ticket not found.")
    return update_ticket_status(ticket_id, actor_admin_id, status="Open")


def list_admin_users(actor_admin_id: int) -> list[dict[str, Any]]:
    with get_db_context() as db:
        _require_admin(db, actor_admin_id)
        rows = (
            db.query(User)
            .filter(
                User.role == "admin",
                func.coalesce(User.account_status, "active") == "active",
                User.is_active.is_(True),
            )
            .order_by(User.full_name)
            .all()
        )
        return [{"id": int(row.id), "name": _account_name(db, row.id)} for row in rows]


def list_published_faqs(
    *, search: str | None = None, category: str | None = None
) -> list[dict[str, Any]]:
    with get_db_context() as db:
        query = db.query(SupportFaq).filter(SupportFaq.is_published.is_(True))
        pattern = _like_pattern(search)
        if pattern:
            query = query.filter(
                or_(
                    func.lower(SupportFaq.question).like(pattern),
                    func.lower(SupportFaq.answer).like(pattern),
                )
            )
        if category and category != "All":
            query = query.filter(SupportFaq.category == _clean_text(category, 60))
        rows = query.order_by(asc(SupportFaq.display_order), asc(SupportFaq.id)).all()
        return [_faq_dict(db, row) for row in rows]


def list_all_faqs(
    actor_admin_id: int,
    *,
    search: str | None = None,
    category: str | None = None,
    published: str | None = None,
) -> list[dict[str, Any]]:
    with get_db_context() as db:
        _require_admin(db, actor_admin_id)
        query = db.query(SupportFaq)
        pattern = _like_pattern(search)
        if pattern:
            query = query.filter(
                or_(
                    func.lower(SupportFaq.faq_id).like(pattern),
                    func.lower(SupportFaq.question).like(pattern),
                    func.lower(SupportFaq.answer).like(pattern),
                )
            )
        if category and category != "All":
            query = query.filter(SupportFaq.category == _clean_text(category, 60))
        if published in ("published", "draft"):
            query = query.filter(SupportFaq.is_published.is_(published == "published"))
        rows = query.order_by(asc(SupportFaq.display_order), asc(SupportFaq.id)).all()
        return [_faq_dict(db, row) for row in rows]


def list_faq_categories(actor_admin_id: int) -> list[str]:
    with get_db_context() as db:
        _require_admin(db, actor_admin_id)
        rows = db.query(SupportFaq.category).distinct().all()
        saved = sorted(str(row[0]) for row in rows if row[0])
    return sorted({*FAQ_CATEGORIES, *saved})


def upsert_faq(
    actor_admin_id: int,
    *,
    faq_id: int | None = None,
    category: str,
    question: str,
    answer: str,
    display_order: int = 0,
    is_published: bool = False,
) -> dict[str, Any]:
    clean_category = _clean_text(category, 60, required=True, label="Category")
    clean_question = _clean_text(question, 300, required=True, label="Question")
    clean_answer = _clean_long_text(answer, 8000, required=True, label="Answer")
    events: list[tuple[str, str, int | None, str]] = []
    with get_db_context() as db:
        admin = _require_admin(db, actor_admin_id)
        row = None
        if faq_id is not None:
            row = (
                db.query(SupportFaq)
                .filter(SupportFaq.id == _as_positive_int(faq_id, "FAQ"))
                .first()
            )
            if row is None:
                raise AuthError("FAQ not found.")
        else:
            row = SupportFaq(faq_id=_new_ref("FAQ"), created_by_admin_id=admin.id)
            db.add(row)
        row.category = clean_category
        row.question = clean_question
        row.answer = clean_answer
        row.display_order = int(display_order)
        row.is_published = bool(is_published)
        row.updated_by_admin_id = admin.id
        row.updated_at = _utcnow()
        db.flush()
        if bool(is_published):
            _notify_all_users(
                db,
                title="Help center update",
                message=clean_question,
                event_key=f"support-faq-published-{row.id}-{row.updated_at}",
                target_page="help_center",
            )
        result = _faq_dict(db, row)
        action = "updated" if faq_id is not None else "created"
        events.append(("support_faq_" + action, str(row.faq_id), admin.id, "info"))
    _emit(events)
    return result


def set_faq_published(faq_id: int, actor_admin_id: int, *, published: bool) -> dict[str, Any]:
    events: list[tuple[str, str, int | None, str]] = []
    with get_db_context() as db:
        admin = _require_admin(db, actor_admin_id)
        row = db.query(SupportFaq).filter(SupportFaq.id == _as_positive_int(faq_id, "FAQ")).first()
        if row is None:
            raise AuthError("FAQ not found.")
        row.is_published = bool(published)
        row.updated_by_admin_id = admin.id
        row.updated_at = _utcnow()
        if bool(published):
            _notify_all_users(
                db,
                title="Help center update",
                message=str(row.question),
                event_key=f"support-faq-published-{row.id}-{row.updated_at}",
                target_page="help_center",
            )
        result = _faq_dict(db, row)
        events.append(
            (
                "support_faq_published" if published else "support_faq_unpublished",
                str(row.faq_id),
                admin.id,
                "info",
            )
        )
    _emit(events)
    return result


def delete_faq(faq_id: int, actor_admin_id: int) -> dict[str, Any]:
    events: list[tuple[str, str, int | None, str]] = []
    with get_db_context() as db:
        admin = _require_admin(db, actor_admin_id)
        row = db.query(SupportFaq).filter(SupportFaq.id == _as_positive_int(faq_id, "FAQ")).first()
        if row is None:
            raise AuthError("FAQ not found.")
        reference = str(row.faq_id)
        db.delete(row)
        events.append(("support_faq_deleted", reference, admin.id, "warning"))
    _emit(events)
    return {"id": int(faq_id), "faq_id": reference, "deleted": True}


def get_support_dashboard_metrics(actor_admin_id: int) -> dict[str, Any]:
    with get_readonly_db() as db:
        _require_admin(db, actor_admin_id)
        tickets = db.query(SupportTicket).all()
        messages = db.query(SupportMessage).all()
        feedback = db.query(UserFeedback).all()
        bugs = db.query(BugReport).all()
        requests = db.query(FeatureRequest).all()
        faqs = db.query(SupportFaq).all()
        contacts = db.query(ContactMessage).all()
        first_response_hours: list[float] = []
        resolution_hours: list[float] = []
        for ticket in tickets:
            replies = [
                row.created_at
                for row in messages
                if row.ticket_id == ticket.id
                and str(row.author_role) == "admin"
                and str(row.message_type) == "reply"
                and row.created_at
            ]
            if replies and ticket.created_at:
                delta = min(replies) - ticket.created_at
                first_response_hours.append(max(delta.total_seconds() / 3600, 0))
            if ticket.resolved_at and ticket.created_at:
                delta = ticket.resolved_at - ticket.created_at
                resolution_hours.append(max(delta.total_seconds() / 3600, 0))
        top_requests = (
            db.query(
                FeatureRequest,
                func.count(FeatureRequestVote.id).label("vote_count"),
            )
            .outerjoin(FeatureRequestVote, FeatureRequestVote.request_id == FeatureRequest.id)
            .group_by(FeatureRequest.id)
            .order_by(desc("vote_count"), desc(FeatureRequest.created_at))
            .limit(5)
            .all()
        )
        return {
            "total_tickets": len(tickets),
            "open_tickets": sum(1 for row in tickets if str(row.status) == "Open"),
            "in_progress_tickets": sum(
                1 for row in tickets if str(row.status) in ("In Progress", "Waiting User")
            ),
            "resolved_tickets": sum(1 for row in tickets if str(row.status) == "Resolved"),
            "closed_tickets": sum(1 for row in tickets if str(row.status) == "Closed"),
            "avg_first_response_hours": _average(first_response_hours),
            "avg_resolution_hours": _average(resolution_hours),
            "unread_contacts": sum(1 for row in contacts if not bool(row.is_read_by_admin)),
            "unread_user_messages": sum(
                1
                for row in messages
                if str(row.author_role) == "user" and not bool(row.is_read_by_admin)
            ),
            "new_feedback": sum(1 for row in feedback if str(row.status) == "New"),
            "pending_feedback": sum(
                1 for row in feedback if str(row.status) in ("New", "In Review", "Reviewed")
            ),
            "open_bugs": sum(1 for row in bugs if str(row.status) not in ("Fixed", "Rejected")),
            "pending_bug_reports": sum(
                1 for row in bugs if str(row.status) in ("New", "Confirmed", "In Progress")
            ),
            "pending_feature_requests": sum(
                1 for row in requests if str(row.status) in ("Submitted", "Under Review")
            ),
            "published_faqs": sum(1 for row in faqs if bool(row.is_published)),
            "recent_activity": _support_activity(db, limit=8),
            "tickets_by_status": _breakdown(tickets, "status"),
            "tickets_by_category": _breakdown(tickets, "category"),
            "tickets_by_priority": _breakdown(tickets, "priority"),
            "feedback_avg_rating": _average([float(row.rating) for row in feedback if row.rating]),
            "top_feature_requests": [
                {
                    "id": int(row.id),
                    "title": str(row.title),
                    "vote_count": int(vote_count),
                    "status": str(row.status),
                }
                for row, vote_count in top_requests
            ],
        }


def get_support_analytics(
    actor_admin_id: int,
    days: int = 30,
    *,
    date_from: object = None,
    date_to: object = None,
) -> dict[str, Any]:
    window_days = days if days in (7, 30, 90) else 30
    with get_readonly_db() as db:
        _require_admin(db, actor_admin_id)
        start = _parse_date_bound(date_from, "Start date")
        end = _parse_date_bound(date_to, "End date", end_of_day=True)
        if start and end and start > end:
            raise ValueError("Start date must be before end date.")
        if start is None and end is None:
            end = _utcnow()
            start = end - timedelta(days=window_days)
        elif start is None:
            assert end is not None
            start = end - timedelta(days=window_days)
        elif end is None:
            end = _utcnow()
        assert start is not None and end is not None

        tickets = db.query(SupportTicket).all()
        messages = db.query(SupportMessage).all()
        feedback = db.query(UserFeedback).all()
        bugs = db.query(BugReport).all()
        requests = db.query(FeatureRequest).all()
        contacts = db.query(ContactMessage).all()

        def in_window(rows: list[Any]) -> list[Any]:
            return [row for row in rows if row.created_at and start <= row.created_at <= end]

        recent_tickets = in_window(tickets)
        recent_messages = in_window(messages)
        recent_feedback = in_window(feedback)
        recent_bugs = in_window(bugs)
        recent_requests = in_window(requests)
        recent_contacts = in_window(contacts)

        first_day = start.date()
        last_day = end.date()
        if (last_day - first_day).days > 365:
            first_day = last_day - timedelta(days=365)
        day_sequence = [
            first_day + timedelta(days=offset) for offset in range((last_day - first_day).days + 1)
        ]

        def daily_series(rows: list[Any]) -> list[dict[str, Any]]:
            counts = Counter(row.created_at.date().isoformat() for row in rows if row.created_at)
            return [
                {"date": day.isoformat(), "count": int(counts.get(day.isoformat(), 0))}
                for day in day_sequence
            ]

        ticket_counts = Counter(
            row.created_at.date().isoformat() for row in recent_tickets if row.created_at
        )
        cumulative_before = sum(1 for row in tickets if row.created_at and row.created_at < start)
        cumulative_series: list[dict[str, Any]] = []
        running_total = cumulative_before
        for day in day_sequence:
            running_total += int(ticket_counts.get(day.isoformat(), 0))
            cumulative_series.append({"date": day.isoformat(), "count": running_total})

        resolution_hours = [
            max((row.resolved_at - row.created_at).total_seconds() / 3600, 0)
            for row in recent_tickets
            if row.resolved_at and row.created_at
        ]
        activity_rows = _activity_rows(db, since=start, until=end)
        activity_counts = Counter(
            row.created_at.date().isoformat() for row in activity_rows if row.created_at
        )
        activity_series = [
            {"date": day.isoformat(), "count": int(activity_counts.get(day.isoformat(), 0))}
            for day in day_sequence
        ]
        request_ids = {int(row.id) for row in recent_requests}
        if request_ids:
            top_requests = (
                db.query(
                    FeatureRequest,
                    func.count(FeatureRequestVote.id).label("vote_count"),
                )
                .outerjoin(FeatureRequestVote, FeatureRequestVote.request_id == FeatureRequest.id)
                .filter(FeatureRequest.id.in_(request_ids))
                .group_by(FeatureRequest.id)
                .order_by(desc("vote_count"), desc(FeatureRequest.created_at))
                .limit(10)
                .all()
            )
        else:
            top_requests = []

        return {
            "generated_at": _utcnow(),
            "days": window_days if not date_from and not date_to else None,
            "date_from": start,
            "date_to": end,
            "tickets_created": len(recent_tickets),
            "messages_sent": len(recent_messages),
            "feedback_submitted": len(recent_feedback),
            "bugs_submitted": len(recent_bugs),
            "feature_requests_submitted": len(recent_requests),
            "contact_messages_received": len(recent_contacts),
            "total_tickets": len(recent_tickets),
            "resolved_tickets": sum(1 for row in recent_tickets if str(row.status) == "Resolved"),
            "closed_tickets": sum(1 for row in recent_tickets if str(row.status) == "Closed"),
            "avg_resolution_hours": _average(resolution_hours),
            "feedback_avg_rating": _average(
                [float(row.rating) for row in recent_feedback if row.rating]
            ),
            "status_breakdown": _breakdown(recent_tickets, "status"),
            "category_breakdown": _breakdown(recent_tickets, "category"),
            "priority_breakdown": _breakdown(recent_tickets, "priority"),
            "feedback_rating_breakdown": dict(
                Counter(str(int(row.rating)) for row in recent_feedback if row.rating)
            ),
            "bug_status_breakdown": _breakdown(recent_bugs, "status"),
            "bug_severity_breakdown": _breakdown(recent_bugs, "severity"),
            "feature_status_breakdown": _breakdown(recent_requests, "status"),
            "contact_status_breakdown": _breakdown(recent_contacts, "status"),
            "severity_breakdown": _breakdown(recent_bugs, "severity"),
            "top_voted_requests": [
                {
                    "id": int(row.id),
                    "title": str(row.title),
                    "vote_count": int(vote_count),
                    "status": str(row.status),
                }
                for row, vote_count in top_requests
            ],
            "daily_tickets": daily_series(recent_tickets),
            "cumulative_tickets": cumulative_series,
            "daily_messages": daily_series(recent_messages),
            "daily_feedback": daily_series(recent_feedback),
            "daily_bugs": daily_series(recent_bugs),
            "daily_feature_requests": daily_series(recent_requests),
            "daily_contacts": daily_series(recent_contacts),
            "daily_activity": activity_series,
            "recent_activity": _support_activity(db, limit=8, since=start, until=end),
        }
