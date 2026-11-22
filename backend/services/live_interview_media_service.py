from __future__ import annotations

import hashlib
import io
import json
import os
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from config.settings import settings
from database.db import get_db_context
from database.models import (
    InterviewSession,
    JobPost,
    LiveInterviewMedia,
    LiveInterviewRecording,
    User,
    UserSession,
)
from services.auth_service import log_event, require_admin, require_owned_session_token
from services.resource_monitor import acquire_heavy_task

AUDIO_MIME = "audio/wav"
VIDEO_MIME = "video/mp4"
_MEDIA_SUFFIXES = {"audio": ".wav", "video": ".mp4"}
_ACTIVE_DELETION = "active"


class LiveMediaError(ValueError):
    """Raised when live interview media is invalid or inaccessible."""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _coerce_datetime(value: datetime | str | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise LiveMediaError("Invalid recording timestamp.") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def storage_root() -> Path:
    root = Path(settings.live_media_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


def _storage_key(
    *, user_id: int, interview_id: int, recording_id: int, media_type: str
) -> str:
    suffix = _MEDIA_SUFFIXES.get(media_type)
    if suffix is None:
        raise LiveMediaError("Unsupported live media type.")
    return (
        f"{int(user_id)}/{int(interview_id)}/{int(recording_id)}/"
        f"{media_type}/recording{suffix}"
    )


def resolve_storage_key(storage_key: str) -> Path:
    candidate = PurePosixPath(str(storage_key or ""))
    if candidate.is_absolute() or any(part in {"..", ".", ""} for part in candidate.parts):
        raise LiveMediaError("Invalid media storage key.")
    root = storage_root()
    resolved = (root / Path(*candidate.parts)).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise LiveMediaError("Invalid media storage key.")
    return resolved


def _validate_media(data: bytes, media_type: str, duration_seconds: float) -> tuple[str, str]:
    if media_type not in _MEDIA_SUFFIXES:
        raise LiveMediaError("Unsupported live media type.")
    if not data:
        raise LiveMediaError("The recording is empty.")
    limit = int(settings.live_media_max_bytes)
    if len(data) > limit:
        raise LiveMediaError(f"The recording exceeds the {limit // (1024 * 1024)} MB limit.")
    if duration_seconds < 0 or duration_seconds > float(settings.live_media_max_seconds):
        raise LiveMediaError(
            f"The recording exceeds the {int(settings.live_media_max_seconds) // 60} minute limit."
        )
    if media_type == "audio":
        try:
            with wave.open(io.BytesIO(data), "rb") as wav_file:
                frames = wav_file.getnframes()
                rate = wav_file.getframerate()
                if frames <= 0 or rate <= 0:
                    raise LiveMediaError("The audio recording is invalid.")
        except (EOFError, wave.Error) as exc:
            raise LiveMediaError("The audio recording is not a valid WAV file.") from exc
        return AUDIO_MIME, hashlib.sha256(data).hexdigest()
    if len(data) < 12 or data[4:8] != b"ftyp":
        raise LiveMediaError("The camera recording is not a valid MP4 file.")
    return VIDEO_MIME, hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise LiveMediaError("Could not save the recording to local storage.") from exc


def _verify_interview_access(db: Any, interview_id: int, actor_user_id: int, session_token: str) -> None:
    workspace_id = require_owned_session_token(session_token, actor_user_id)
    row = (
        db.query(InterviewSession)
        .join(UserSession, UserSession.id == InterviewSession.session_id)
        .filter(
            InterviewSession.id == int(interview_id),
            UserSession.id == workspace_id,
            UserSession.user_id == int(actor_user_id),
        )
        .first()
    )
    if row is None:
        raise LiveMediaError("This interview does not belong to your workspace.")


def _verify_recording_access(
    db: Any, recording_id: int, actor_user_id: int, session_token: str
) -> LiveInterviewRecording:
    require_owned_session_token(session_token, actor_user_id)
    record = (
        db.query(LiveInterviewRecording)
        .filter(
            LiveInterviewRecording.id == int(recording_id),
            LiveInterviewRecording.owner_user_id == int(actor_user_id),
            LiveInterviewRecording.deletion_status == _ACTIVE_DELETION,
        )
        .first()
    )
    if record is None:
        raise LiveMediaError("This recording is not available.")
    return record


def ensure_live_recording(
    *,
    interview_id: int,
    user_id: int,
    session_token: str,
    consent_given: bool,
    consent_at: datetime | str | None = None,
    policy_version: str = "live-recording-v1",
) -> int:
    with get_db_context() as db:
        _verify_interview_access(db, int(interview_id), int(user_id), session_token)
        record = (
            db.query(LiveInterviewRecording)
            .filter(LiveInterviewRecording.interview_session_id == int(interview_id))
            .first()
        )
        if record is not None:
            if bool(record.consent_given) and not consent_given:
                raise LiveMediaError(
                    "Recording consent was withdrawn. Existing recordings remain until deleted."
                )
            return int(record.id)
        record = LiveInterviewRecording(
            interview_session_id=int(interview_id),
            owner_user_id=int(user_id),
            consent_given=bool(consent_given),
            consent_at=_coerce_datetime(consent_at) if consent_given else None,
            consent_policy_version=str(policy_version),
            recording_status="recording" if consent_given else "not_consented",
            started_at=_now() if consent_given else None,
        )
        db.add(record)
        db.flush()
        return int(record.id)


def get_live_recording_for_interview(
    *, interview_id: int, user_id: int, session_token: str
) -> dict[str, Any] | None:
    require_owned_session_token(session_token, user_id)
    with get_db_context() as db:
        record = (
            db.query(LiveInterviewRecording)
            .join(InterviewSession, InterviewSession.id == LiveInterviewRecording.interview_session_id)
            .join(UserSession, UserSession.id == InterviewSession.session_id)
            .filter(
                LiveInterviewRecording.interview_session_id == int(interview_id),
                LiveInterviewRecording.owner_user_id == int(user_id),
                LiveInterviewRecording.deletion_status == _ACTIVE_DELETION,
                UserSession.user_id == int(user_id),
            )
            .first()
        )
        if record is None:
            return None
        return {
            "id": int(record.id),
            "consent_given": bool(record.consent_given),
            "consent_at": record.consent_at,
            "recording_status": record.recording_status,
            "consent_policy_version": record.consent_policy_version,
        }


def set_recording_status(
    recording_id: int, *, actor_user_id: int, session_token: str, status: str
) -> None:
    with get_db_context() as db:
        record = _verify_recording_access(db, recording_id, actor_user_id, session_token)
        record.recording_status = str(status)
        if status in {"completed", "interrupted", "failed"}:
            record.finalized_at = _now()


def save_live_media(
    *,
    recording_id: int,
    interview_id: int,
    user_id: int,
    session_token: str,
    media_type: str,
    data: bytes,
    duration_seconds: float,
    metadata: dict[str, Any] | None = None,
) -> int:
    mime_type, checksum = _validate_media(data, media_type, float(duration_seconds))
    with get_db_context() as db:
        record = _verify_recording_access(db, recording_id, user_id, session_token)
        if not record.consent_given:
            raise LiveMediaError("Recording consent is required before saving media.")
        if int(record.interview_session_id) != int(interview_id):
            raise LiveMediaError("This recording does not belong to the interview.")
        key = _storage_key(
            user_id=int(user_id),
            interview_id=int(interview_id),
            recording_id=int(recording_id),
            media_type=media_type,
        )
        path = resolve_storage_key(key)
        existing = (
            db.query(LiveInterviewMedia)
            .filter(
                LiveInterviewMedia.recording_id == int(recording_id),
                LiveInterviewMedia.media_type == media_type,
            )
            .first()
        )
        if (
            existing is not None
            and existing.checksum == checksum
            and existing.deletion_status == _ACTIVE_DELETION
            and path.exists()
        ):
            return int(existing.id)
        with acquire_heavy_task("video_processing"):
            _atomic_write(path, data)
        payload = dict(metadata or {})
        payload.setdefault("consent_policy_version", record.consent_policy_version)
        payload.setdefault("captured_at", _now().isoformat())
        if existing is None:
            media = LiveInterviewMedia(
                recording_id=int(recording_id),
                interview_session_id=int(interview_id),
                owner_user_id=int(user_id),
                media_type=media_type,
                storage_key=key,
                filename=Path(key).name,
                mime_type=mime_type,
                size_bytes=len(data),
                duration_seconds=float(duration_seconds),
                checksum=checksum,
                media_status="ready",
                metadata_json=payload,
                finalized_at=_now(),
            )
            db.add(media)
        else:
            existing.storage_key = key
            existing.filename = Path(key).name
            existing.mime_type = mime_type
            existing.size_bytes = len(data)
            existing.duration_seconds = float(duration_seconds)
            existing.checksum = checksum
            existing.media_status = "ready"
            existing.metadata_json = payload
            existing.finalized_at = _now()
            existing.deletion_status = _ACTIVE_DELETION
            existing.deleted_at = None
            media = existing
        db.flush()
        log_event(
            "live_media_saved",
            message=(
                f"media_id={int(media.id)} interview_id={int(interview_id)} "
                f"type={media_type} bytes={len(data)}"
            ),
            user_id=int(user_id),
        )
        return int(media.id)


def _recording_row(row: LiveInterviewRecording, media: list[LiveInterviewMedia]) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "interview_id": int(row.interview_session_id),
        "owner_user_id": int(row.owner_user_id),
        "consent_given": bool(row.consent_given),
        "consent_at": row.consent_at,
        "consent_policy_version": row.consent_policy_version,
        "recording_status": row.recording_status,
        "deletion_status": row.deletion_status,
        "started_at": row.started_at,
        "finalized_at": row.finalized_at,
        "created_at": row.created_at,
        "media": [
            {
                "id": int(item.id),
                "media_type": item.media_type,
                "filename": item.filename,
                "mime_type": item.mime_type,
                "size_bytes": int(item.size_bytes or 0),
                "duration_seconds": float(item.duration_seconds or 0),
                "checksum": item.checksum,
                "media_status": item.media_status,
                "metadata": item.metadata_json or {},
                "available": resolve_storage_key(item.storage_key).exists()
                if _safe_key(item.storage_key)
                else False,
            }
            for item in media
        ],
    }


def _safe_key(storage_key: str) -> bool:
    try:
        resolve_storage_key(storage_key)
        return True
    except LiveMediaError:
        return False


def list_user_recordings(*, user_id: int, session_token: str) -> list[dict[str, Any]]:
    workspace_id = require_owned_session_token(session_token, user_id)
    with get_db_context() as db:
        rows = (
            db.query(LiveInterviewRecording)
            .join(InterviewSession, InterviewSession.id == LiveInterviewRecording.interview_session_id)
            .join(UserSession, UserSession.id == InterviewSession.session_id)
            .filter(
                LiveInterviewRecording.owner_user_id == int(user_id),
                LiveInterviewRecording.deletion_status == _ACTIVE_DELETION,
                UserSession.id == workspace_id,
            )
            .order_by(LiveInterviewRecording.created_at.desc())
            .all()
        )
        result = []
        for row in rows:
            media = (
                db.query(LiveInterviewMedia)
                .filter(
                    LiveInterviewMedia.recording_id == row.id,
                    LiveInterviewMedia.deletion_status == _ACTIVE_DELETION,
                )
                .all()
            )
            item = _recording_row(row, media)
            session = (
                db.query(InterviewSession).filter(InterviewSession.id == row.interview_session_id).first()
            )
            job = None
            if session is not None and session.job_post_id:
                job = db.query(JobPost).filter(JobPost.id == session.job_post_id).first()
            item["job_title"] = getattr(job, "title", None) or getattr(session, "title", None) or "Job interview"
            item["company"] = getattr(job, "company", "")
            item["interview_status"] = getattr(session, "status", "")
            item["report_json"] = getattr(session, "report_json", None)
            result.append(item)
        return result


def list_admin_recordings(
    *,
    actor_admin_id: int,
    owner_user_id: int | None = None,
    status: str | None = None,
    search: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    require_admin(actor_admin_id)
    with get_db_context() as db:
        query = (
            db.query(LiveInterviewRecording)
            .join(InterviewSession, InterviewSession.id == LiveInterviewRecording.interview_session_id)
            .join(User, User.id == LiveInterviewRecording.owner_user_id)
            .outerjoin(JobPost, JobPost.id == InterviewSession.job_post_id)
        )
        if owner_user_id is not None:
            query = query.filter(LiveInterviewRecording.owner_user_id == int(owner_user_id))
        if status:
            query = query.filter(LiveInterviewRecording.recording_status == str(status))
        if search:
            term = f"%{str(search).strip()}%"
            query = query.filter(
                (User.email.ilike(term))
                | (User.username.ilike(term))
                | (JobPost.title.ilike(term))
            )
        rows = query.order_by(LiveInterviewRecording.created_at.desc()).limit(max(1, int(limit))).all()
        result = []
        for row in rows:
            media = (
                db.query(LiveInterviewMedia)
                .filter(
                    LiveInterviewMedia.recording_id == row.id,
                    LiveInterviewMedia.deletion_status == _ACTIVE_DELETION,
                )
                .all()
            )
            item = _recording_row(row, media)
            owner = db.query(User).filter(User.id == row.owner_user_id).first()
            item["owner_email"] = getattr(owner, "email", "")
            item["owner_username"] = getattr(owner, "username", "")
            item["interview_status"] = (
                db.query(InterviewSession.status)
                .filter(InterviewSession.id == row.interview_session_id)
                .scalar()
            )
            result.append(item)
        return result


def read_live_media(
    media_id: int,
    *,
    actor_user_id: int | None = None,
    session_token: str | None = None,
    actor_admin_id: int | None = None,
) -> tuple[bytes, str, str]:
    with get_db_context() as db:
        query = db.query(LiveInterviewMedia).filter(
            LiveInterviewMedia.id == int(media_id),
            LiveInterviewMedia.deletion_status == _ACTIVE_DELETION,
        )
        if actor_admin_id is not None:
            require_admin(actor_admin_id)
        else:
            if actor_user_id is None or not session_token:
                raise LiveMediaError("Authentication is required to open this recording.")
            query = query.filter(LiveInterviewMedia.owner_user_id == int(actor_user_id))
            require_owned_session_token(session_token, actor_user_id)
        record = query.first()
        if record is None:
            raise LiveMediaError("This recording is not available.")
        path = resolve_storage_key(record.storage_key)
        if not path.exists():
            raise LiveMediaError("The recording file is missing.")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise LiveMediaError("The recording file could not be read.") from exc
        if hashlib.sha256(data).hexdigest() != record.checksum:
            raise LiveMediaError("The recording failed its integrity check.")
        return data, record.mime_type, record.filename


def verify_live_media(
    media_id: int,
    *,
    actor_user_id: int | None = None,
    session_token: str | None = None,
    actor_admin_id: int | None = None,
) -> dict[str, Any]:
    with get_db_context() as db:
        query = db.query(LiveInterviewMedia).filter(
            LiveInterviewMedia.id == int(media_id),
            LiveInterviewMedia.deletion_status == _ACTIVE_DELETION,
        )
        if actor_admin_id is not None:
            require_admin(actor_admin_id)
        else:
            if actor_user_id is None or not session_token:
                raise LiveMediaError("Authentication is required to verify this recording.")
            query = query.filter(LiveInterviewMedia.owner_user_id == int(actor_user_id))
            require_owned_session_token(session_token, actor_user_id)
        record = query.first()
        if record is None:
            raise LiveMediaError("This recording is not available.")
        path = resolve_storage_key(record.storage_key)
        exists = path.exists()
        valid = False
        if exists:
            try:
                valid = hashlib.sha256(path.read_bytes()).hexdigest() == record.checksum
            except OSError:
                valid = False
        return {
            "id": int(record.id),
            "exists": exists,
            "checksum_valid": valid,
            "size_bytes": int(record.size_bytes or 0),
            "media_type": record.media_type,
        }


def _delete_media_rows(db: Any, recording_id: int) -> list[str]:
    paths: list[str] = []
    for item in (
        db.query(LiveInterviewMedia)
        .filter(
            LiveInterviewMedia.recording_id == int(recording_id),
            LiveInterviewMedia.deletion_status == _ACTIVE_DELETION,
        )
        .all()
    ):
        paths.append(item.storage_key)
        item.deletion_status = "deleted"
        item.deleted_at = _now()
    return paths


def _remove_paths(paths: list[str]) -> list[str]:
    failures: list[str] = []
    for key in paths:
        try:
            path = resolve_storage_key(key)
            path.unlink(missing_ok=True)
        except (LiveMediaError, OSError):
            failures.append(key)
    return failures


def delete_live_media(
    media_id: int,
    *,
    actor_user_id: int | None = None,
    session_token: str | None = None,
    actor_admin_id: int | None = None,
) -> None:
    with get_db_context() as db:
        query = db.query(LiveInterviewMedia).filter(
            LiveInterviewMedia.id == int(media_id),
            LiveInterviewMedia.deletion_status == _ACTIVE_DELETION,
        )
        if actor_admin_id is not None:
            require_admin(actor_admin_id)
        else:
            if actor_user_id is None or not session_token:
                raise LiveMediaError("Authentication is required to delete this recording.")
            query = query.filter(LiveInterviewMedia.owner_user_id == int(actor_user_id))
            require_owned_session_token(session_token, actor_user_id)
        record = query.first()
        if record is None:
            raise LiveMediaError("This recording is not available.")
        key = record.storage_key
        record.deletion_status = "deleted"
        record.deleted_at = _now()
    failures = _remove_paths([key])
    if actor_admin_id is not None:
        log_event(
            "admin.media_delete",
            f"Admin deleted live interview media {int(media_id)}",
            actor_admin_id,
            level="warning",
        )
    if failures:
        raise LiveMediaError("The recording record was deleted, but a stored file could not be removed.")


def delete_live_recording(
    recording_id: int,
    *,
    actor_user_id: int | None = None,
    session_token: str | None = None,
    actor_admin_id: int | None = None,
) -> None:
    with get_db_context() as db:
        query = db.query(LiveInterviewRecording).filter(
            LiveInterviewRecording.id == int(recording_id),
            LiveInterviewRecording.deletion_status == _ACTIVE_DELETION,
        )
        if actor_admin_id is not None:
            require_admin(actor_admin_id)
        else:
            if actor_user_id is None or not session_token:
                raise LiveMediaError("Authentication is required to delete this recording.")
            query = query.filter(LiveInterviewRecording.owner_user_id == int(actor_user_id))
            require_owned_session_token(session_token, actor_user_id)
        record = query.first()
        if record is None:
            raise LiveMediaError("This recording is not available.")
        paths = _delete_media_rows(db, int(recording_id))
        record.deletion_status = "deleted"
        record.deleted_at = _now()
        record.recording_status = "deleted"
    failures = _remove_paths(paths)
    if actor_admin_id is not None:
        log_event(
            "admin.recording_delete",
            f"Admin deleted live interview recording {int(recording_id)}",
            actor_admin_id,
            level="warning",
        )
    if failures:
        raise LiveMediaError("The recording record was deleted, but some stored files could not be removed.")


def cleanup_temporary_media() -> int:
    removed = 0
    root = storage_root()
    for candidate in root.rglob("*.tmp"):
        if not candidate.is_file():
            continue
        try:
            candidate.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def recording_details(
    *,
    interview_id: int,
    user_id: int | None = None,
    session_token: str | None = None,
    actor_admin_id: int | None = None,
) -> dict[str, Any]:
    with get_db_context() as db:
        query = db.query(InterviewSession).filter(InterviewSession.id == int(interview_id))
        if actor_admin_id is not None:
            require_admin(actor_admin_id)
        else:
            if user_id is None or not session_token:
                raise LiveMediaError("Authentication is required to open this interview.")
            query = query.filter(
                InterviewSession.session_id == require_owned_session_token(session_token, user_id)
            )
        session = query.first()
        if session is None:
            raise LiveMediaError("This interview does not belong to your workspace.")
        report = session.report_json or {}
        return {
            "report": report if isinstance(report, dict) else {},
            "status": session.status,
            "created_at": session.created_at,
        }


def metadata_preview(metadata: dict[str, Any] | None) -> str:
    payload = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
    return payload[:500]
