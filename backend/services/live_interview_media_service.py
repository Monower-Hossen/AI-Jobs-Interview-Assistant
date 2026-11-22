from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import time
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)

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
from services.live_recording_manifest import (
    CAPTURE_MANIFEST_KEY,
    extract_capture_manifest,
    merge_manifest_metadata,
    safe_load_capture_manifest,
)
from services.resource_monitor import acquire_heavy_task

AUDIO_MIME = "audio/wav"
VIDEO_MIME = "video/mp4"
_MEDIA_SUFFIXES = {"audio": ".wav", "video": ".mp4"}
_ACTIVE_DELETION = "active"
_PENDING_DELETION = "pending_delete"
_DELETED_DELETION = "deleted"
_DELETION_STATUSES = {_ACTIVE_DELETION, _PENDING_DELETION, _DELETED_DELETION}
_TEMP_FILE_MIN_AGE_SECONDS = 300


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
    *,
    user_id: int,
    interview_id: int,
    recording_id: int,
    media_type: str,
    unique: bool = False,
) -> str:
    suffix = _MEDIA_SUFFIXES.get(media_type)
    if suffix is None:
        raise LiveMediaError("Unsupported live media type.")
    token = f"_{uuid.uuid4().hex}" if unique else ""
    return (
        f"{int(user_id)}/{int(interview_id)}/{int(recording_id)}/"
        f"{media_type}/recording{token}{suffix}"
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
        return AUDIO_MIME, _sha256_bytes(data)
    if len(data) < 12 or data[4:8] != b"ftyp":
        raise LiveMediaError("The camera recording is not a valid MP4 file.")
    return VIDEO_MIME, _sha256_bytes(data)


def _sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _temp_write_path(path: Path) -> Path:
    return path.with_name(f".tmp_live_media_{uuid.uuid4().hex}.part")


def _atomic_write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temp_write_path(path)
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
        return path
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise LiveMediaError("Could not save the recording to local storage.") from exc


def _safe_unlink(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        logger.warning("live_media.unlink_failed", extra={"event": "live_media.unlink_failed"})
        return False


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


def _is_media_active(record: LiveInterviewMedia | None) -> bool:
    return bool(record is not None and str(getattr(record, "deletion_status", "")) == _ACTIVE_DELETION)


def _is_recording_active(record: LiveInterviewRecording | None) -> bool:
    return bool(
        record is not None and str(getattr(record, "deletion_status", "")) == _ACTIVE_DELETION
    )


def _verify_recording_access(
    db: Any, recording_id: int, actor_user_id: int, session_token: str
) -> LiveInterviewRecording:
    require_owned_session_token(session_token, actor_user_id)
    record = (
        db.query(LiveInterviewRecording)
        .filter(
            LiveInterviewRecording.id == int(recording_id),
            LiveInterviewRecording.owner_user_id == int(actor_user_id),
        )
        .first()
    )
    if not _is_recording_active(record):
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
    old_path: Path | None = None
    new_path: Path | None = None
    committed = False
    try:
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
                unique=True,
            )
            new_path = resolve_storage_key(key)
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
                and str(existing.checksum or "") == checksum
                and str(existing.deletion_status or "") == _ACTIVE_DELETION
                and _safe_key(str(existing.storage_key or ""))
                and resolve_storage_key(str(existing.storage_key)).exists()
            ):
                return int(existing.id)

            existing_payload = dict(getattr(existing, "metadata_json", None) or {}) if existing is not None else {}
            payload = merge_manifest_metadata({**existing_payload, **dict(metadata or {})}, None)
            incoming_manifest = (metadata or {}).get(CAPTURE_MANIFEST_KEY)
            if incoming_manifest is not None:
                validated_manifest = safe_load_capture_manifest(incoming_manifest)
                if validated_manifest is None:
                    raise LiveMediaError("Capture manifest is invalid or unsupported.")
                payload = merge_manifest_metadata(payload, validated_manifest)
            payload.setdefault("consent_policy_version", record.consent_policy_version)
            payload.setdefault("captured_at", _now().isoformat())

            if existing is not None and _safe_key(str(existing.storage_key or "")):
                prev = resolve_storage_key(str(existing.storage_key))
                if prev.exists():
                    old_path = prev

            with acquire_heavy_task("video_processing"):
                _atomic_write(new_path, data)

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
                    deletion_status=_ACTIVE_DELETION,
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
            media_id = int(media.id)
        committed = True
    except Exception:
        if not committed and new_path is not None:
            _safe_unlink(new_path)
        raise

    if committed and old_path is not None and new_path is not None and old_path != new_path:
        if not _safe_unlink(old_path):
            logger.warning(
                "live_media.old_file_cleanup_failed",
                extra={"event": "live_media.old_file_cleanup_failed", "media_id": media_id},
            )

    log_event(
        "live_media_saved",
        message=(
            f"media_id={int(media_id)} interview_id={int(interview_id)} "
            f"type={media_type} bytes={len(data)}"
        ),
        user_id=int(user_id),
    )
    return int(media_id)


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
                "deletion_status": item.deletion_status,
                "metadata": item.metadata_json or {},
                "available": (
                    str(item.deletion_status) == _ACTIVE_DELETION
                    and _safe_key(item.storage_key)
                    and resolve_storage_key(item.storage_key).exists()
                ),
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
            manifests = [extract_capture_manifest(m.get("metadata")) for m in item.get("media", [])]
            manifests = [m for m in manifests if m is not None]
            item["capture_manifest"] = manifests[0] if manifests else None
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
            manifests = [extract_capture_manifest(m.get("metadata")) for m in item.get("media", [])]
            manifests = [m for m in manifests if m is not None]
            item["capture_manifest"] = manifests[0] if manifests else None
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
        query = db.query(LiveInterviewMedia).filter(LiveInterviewMedia.id == int(media_id))
        if actor_admin_id is not None:
            require_admin(actor_admin_id)
        else:
            if actor_user_id is None or not session_token:
                raise LiveMediaError("Authentication is required to open this recording.")
            query = query.filter(LiveInterviewMedia.owner_user_id == int(actor_user_id))
            require_owned_session_token(session_token, actor_user_id)
        record = query.first()
        if not _is_media_active(record):
            raise LiveMediaError("This recording is not available.")
        path = resolve_storage_key(record.storage_key)
        if not path.exists():
            raise LiveMediaError("The recording file is missing.")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise LiveMediaError("The recording file could not be read.") from exc
        if _sha256_bytes(data) != record.checksum:
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
        query = db.query(LiveInterviewMedia).filter(LiveInterviewMedia.id == int(media_id))
        if actor_admin_id is not None:
            require_admin(actor_admin_id)
        else:
            if actor_user_id is None or not session_token:
                raise LiveMediaError("Authentication is required to verify this recording.")
            query = query.filter(LiveInterviewMedia.owner_user_id == int(actor_user_id))
            require_owned_session_token(session_token, actor_user_id)
        record = query.first()
        if not _is_media_active(record):
            raise LiveMediaError("This recording is not available.")
        path = resolve_storage_key(record.storage_key)
        exists = path.exists()
        valid = False
        if exists:
            try:
                valid = _sha256_file(path) == str(record.checksum)
            except OSError:
                valid = False
        return {
            "id": int(record.id),
            "exists": exists,
            "checksum_valid": valid,
            "size_bytes": int(record.size_bytes or 0),
            "media_type": record.media_type,
            "deletion_status": str(record.deletion_status),
        }


def _mark_media_pending_delete(item: LiveInterviewMedia) -> None:
    if str(item.deletion_status) != _DELETED_DELETION:
        item.deletion_status = _PENDING_DELETION
        item.deleted_at = _now()


def _mark_media_deleted(item: LiveInterviewMedia) -> None:
    item.deletion_status = _DELETED_DELETION
    item.deleted_at = item.deleted_at or _now()


def _mark_recording_pending_delete(record: LiveInterviewRecording) -> None:
    if str(record.deletion_status) != _DELETED_DELETION:
        record.deletion_status = _PENDING_DELETION
        record.deleted_at = _now()
        if str(record.recording_status) not in {"deleted"}:
            record.recording_status = "pending_delete"


def _mark_recording_deleted(record: LiveInterviewRecording) -> None:
    record.deletion_status = _DELETED_DELETION
    record.deleted_at = record.deleted_at or _now()
    record.recording_status = "deleted"


def _remove_storage_key(storage_key: str) -> tuple[bool, str | None]:
    try:
        path = resolve_storage_key(storage_key)
    except LiveMediaError:
        logger.warning("live_media.invalid_storage_key", extra={"event": "live_media.invalid_storage_key"})
        return False, "invalid_storage_key"
    try:
        if path.exists():
            path.unlink()
        return True, None
    except FileNotFoundError:
        return True, None
    except OSError:
        logger.warning("live_media.unlink_failed", extra={"event": "live_media.unlink_failed"})
        return False, "unlink_failed"


def delete_live_media(
    media_id: int,
    *,
    actor_user_id: int | None = None,
    session_token: str | None = None,
    actor_admin_id: int | None = None,
) -> dict[str, Any]:
    with get_db_context() as db:
        query = db.query(LiveInterviewMedia).filter(LiveInterviewMedia.id == int(media_id))
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
        status = str(record.deletion_status or "")
        if status == _DELETED_DELETION:
            return {"id": int(media_id), "status": _DELETED_DELETION, "pending": False}
        if status not in {_ACTIVE_DELETION, _PENDING_DELETION}:
            raise LiveMediaError("This recording is not available.")
        key = str(record.storage_key)
        _mark_media_pending_delete(record)

    removed, err = _remove_storage_key(key)
    if removed:
        with get_db_context() as db:
            current = db.query(LiveInterviewMedia).filter(LiveInterviewMedia.id == int(media_id)).first()
            if current is not None:
                _mark_media_deleted(current)
        if actor_admin_id is not None:
            log_event(
                "admin.media_delete",
                f"Admin deleted live interview media {int(media_id)}",
                actor_admin_id,
                level="warning",
            )
        return {"id": int(media_id), "status": _DELETED_DELETION, "pending": False}

    logger.warning(
        "live_media.delete_pending",
        extra={"event": "live_media.delete_pending", "media_id": int(media_id), "error": err},
    )
    raise LiveMediaError("The recording deletion is pending because a stored file could not be removed.")


def _collect_pending_media_for_recording(db: Any, recording_id: int) -> list[LiveInterviewMedia]:
    return (
        db.query(LiveInterviewMedia)
        .filter(
            LiveInterviewMedia.recording_id == int(recording_id),
            LiveInterviewMedia.deletion_status.in_([_ACTIVE_DELETION, _PENDING_DELETION]),
        )
        .all()
    )


def delete_live_recording(
    recording_id: int,
    *,
    actor_user_id: int | None = None,
    session_token: str | None = None,
    actor_admin_id: int | None = None,
) -> dict[str, Any]:
    with get_db_context() as db:
        query = db.query(LiveInterviewRecording).filter(LiveInterviewRecording.id == int(recording_id))
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
        rec_status = str(record.deletion_status or "")
        if rec_status == _DELETED_DELETION:
            pending = _collect_pending_media_for_recording(db, int(recording_id))
            if not pending:
                return {"id": int(recording_id), "status": _DELETED_DELETION, "pending_media": []}
        elif rec_status not in {_ACTIVE_DELETION, _PENDING_DELETION}:
            raise LiveMediaError("This recording is not available.")

        media_rows = _collect_pending_media_for_recording(db, int(recording_id))
        for item in media_rows:
            _mark_media_pending_delete(item)
        _mark_recording_pending_delete(record)
        keys = [(int(item.id), str(item.storage_key)) for item in media_rows]

    pending_media: list[int] = []
    for media_id, key in keys:
        removed, err = _remove_storage_key(key)
        if removed:
            with get_db_context() as db:
                current = db.query(LiveInterviewMedia).filter(LiveInterviewMedia.id == int(media_id)).first()
                if current is not None:
                    _mark_media_deleted(current)
        else:
            pending_media.append(int(media_id))
            logger.warning(
                "live_media.delete_pending",
                extra={
                    "event": "live_media.delete_pending",
                    "recording_id": int(recording_id),
                    "media_id": int(media_id),
                    "error": err,
                },
            )

    if not pending_media:
        with get_db_context() as db:
            current = (
                db.query(LiveInterviewRecording)
                .filter(LiveInterviewRecording.id == int(recording_id))
                .first()
            )
            if current is not None:
                remaining = _collect_pending_media_for_recording(db, int(recording_id))
                if not remaining:
                    _mark_recording_deleted(current)
        if actor_admin_id is not None:
            log_event(
                "admin.recording_delete",
                f"Admin deleted live interview recording {int(recording_id)}",
                actor_admin_id,
                level="warning",
            )
        return {"id": int(recording_id), "status": _DELETED_DELETION, "pending_media": []}

    raise LiveMediaError("The recording deletion is pending because some stored files could not be removed.")


def retry_pending_deletions(
    *,
    actor_user_id: int | None = None,
    session_token: str | None = None,
    actor_admin_id: int | None = None,
    recording_id: int | None = None,
) -> dict[str, Any]:
    """Retry pending deletions. Public boundary revalidates authz; idempotent."""
    with get_db_context() as db:
        if actor_admin_id is not None:
            require_admin(actor_admin_id)
            rec_query = db.query(LiveInterviewRecording).filter(
                LiveInterviewRecording.deletion_status == _PENDING_DELETION
            )
            med_query = db.query(LiveInterviewMedia).filter(
                LiveInterviewMedia.deletion_status == _PENDING_DELETION
            )
        else:
            if actor_user_id is None or not session_token:
                raise LiveMediaError("Authentication is required to retry deletions.")
            require_owned_session_token(session_token, actor_user_id)
            rec_query = db.query(LiveInterviewRecording).filter(
                LiveInterviewRecording.deletion_status == _PENDING_DELETION,
                LiveInterviewRecording.owner_user_id == int(actor_user_id),
            )
            med_query = db.query(LiveInterviewMedia).filter(
                LiveInterviewMedia.deletion_status == _PENDING_DELETION,
                LiveInterviewMedia.owner_user_id == int(actor_user_id),
            )
        if recording_id is not None:
            rec_query = rec_query.filter(LiveInterviewRecording.id == int(recording_id))
            med_query = med_query.filter(LiveInterviewMedia.recording_id == int(recording_id))
        rec_ids = [int(r.id) for r in rec_query.all()]
        med_ids = [int(m.id) for m in med_query.all()]

    results: dict[str, Any] = {"recordings": [], "media": []}
    for rid in rec_ids:
        try:
            results["recordings"].append(
                delete_live_recording(
                    rid,
                    actor_user_id=actor_user_id,
                    session_token=session_token,
                    actor_admin_id=actor_admin_id,
                )
            )
        except LiveMediaError as exc:
            results["recordings"].append({"id": rid, "status": _PENDING_DELETION, "error": str(exc)})
    for mid in med_ids:
        try:
            results["media"].append(
                delete_live_media(
                    mid,
                    actor_user_id=actor_user_id,
                    session_token=session_token,
                    actor_admin_id=actor_admin_id,
                )
            )
        except LiveMediaError as exc:
            results["media"].append({"id": mid, "status": _PENDING_DELETION, "error": str(exc)})
    return results


def cleanup_temporary_media(
    *,
    min_age_seconds: int = _TEMP_FILE_MIN_AGE_SECONDS,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Report stale temp media candidates. Dry-run by default; never deletes fresh files."""
    now = time.time()
    root = storage_root()
    candidates: list[str] = []
    skipped: list[dict[str, str]] = []
    removed: list[str] = []
    errors: list[dict[str, str]] = []
    min_age = max(0, int(min_age_seconds))

    for candidate in root.rglob(".tmp_live_media_*.part"):
        try:
            resolved = candidate.resolve()
            if resolved == root or not resolved.is_relative_to(root):
                skipped.append({"path": str(candidate), "reason": "outside_root"})
                continue
            if not candidate.is_file():
                skipped.append({"path": str(candidate), "reason": "not_file"})
                continue
            age = now - candidate.stat().st_mtime
            if age < min_age:
                skipped.append({"path": str(candidate), "reason": "fresh"})
                continue
            rel = str(resolved.relative_to(root)).replace("\\", "/")
            candidates.append(rel)
            if dry_run:
                continue
            try:
                candidate.unlink()
                removed.append(rel)
            except OSError:
                errors.append({"path": rel, "error": "unlink_failed"})
        except OSError:
            errors.append({"path": str(candidate), "error": "stat_failed"})

    return {
        "dry_run": bool(dry_run),
        "min_age_seconds": min_age,
        "candidates": candidates,
        "skipped": skipped,
        "removed": removed,
        "errors": errors,
    }


def run_storage_integrity_diagnostics(
    *,
    actor_admin_id: int | None = None,
    min_temp_age_seconds: int = _TEMP_FILE_MIN_AGE_SECONDS,
) -> dict[str, Any]:
    """Read-only diagnostics. No DB/file mutations, no auto-repair/delete."""
    if actor_admin_id is not None:
        require_admin(actor_admin_id)

    root = storage_root()
    report: dict[str, Any] = {
        "read_only": True,
        "missing_files": [],
        "checksum_mismatches": [],
        "size_mismatches": [],
        "invalid_storage_keys": [],
        "pending_deletions": [],
        "incomplete_records": [],
        "legacy_without_manifest": [],
        "candidate_orphans": [],
        "temp_candidates": [],
        "unverified": [],
    }

    referenced: set[str] = set()
    with get_db_context() as db:
        rows = db.query(LiveInterviewMedia).all()
        for item in rows:
            key = str(item.storage_key or "")
            status = str(item.deletion_status or "")
            rec = {
                "media_id": int(item.id),
                "recording_id": int(item.recording_id),
                "owner_user_id": int(item.owner_user_id),
                "media_type": str(item.media_type),
                "deletion_status": status,
            }
            if not _safe_key(key):
                report["invalid_storage_keys"].append({**rec, "result": "invalid_storage_key"})
                continue
            path = resolve_storage_key(key)
            try:
                rel = str(path.resolve().relative_to(root)).replace("\\", "/")
            except Exception:
                report["invalid_storage_keys"].append({**rec, "result": "outside_root"})
                continue
            referenced.add(rel)
            if status == _PENDING_DELETION:
                report["pending_deletions"].append(rec)
            if str(item.media_status) not in {"ready"}:
                report["incomplete_records"].append({**rec, "media_status": str(item.media_status)})
            manifest = extract_capture_manifest(item.metadata_json if isinstance(item.metadata_json, dict) else {})
            if manifest is None:
                report["legacy_without_manifest"].append(rec)
            if status == _DELETED_DELETION:
                continue
            if not path.exists():
                report["missing_files"].append({**rec, "result": "confirmed_missing_file"})
                continue
            try:
                actual_size = path.stat().st_size
                expected_size = int(item.size_bytes or 0)
                if actual_size != expected_size:
                    report["size_mismatches"].append(
                        {**rec, "expected_size": expected_size, "actual_size": actual_size}
                    )
                actual_checksum = _sha256_file(path)
                if actual_checksum != str(item.checksum):
                    report["checksum_mismatches"].append({**rec, "result": "confirmed_checksum_mismatch"})
            except OSError:
                report["unverified"].append({**rec, "result": "unverified_io_error"})

    try:
        for candidate in root.rglob("*"):
            if not candidate.is_file():
                continue
            if candidate.name.startswith(".tmp_live_media_") and candidate.suffix == ".part":
                continue
            try:
                rel = str(candidate.resolve().relative_to(root)).replace("\\", "/")
            except Exception:
                logger.debug("live_media.diagnostics_skip_path", exc_info=True)
                continue
            if rel not in referenced:
                report["candidate_orphans"].append(
                    {"relative_path": rel, "result": "candidate_orphan", "note": "No DB row found; do not auto-delete"}
                )
    except OSError:
        report["unverified"].append({"result": "unverified_scan_error"})

    temp_report = cleanup_temporary_media(min_age_seconds=min_temp_age_seconds, dry_run=True)
    report["temp_candidates"] = temp_report.get("candidates", [])
    report["temp_skipped"] = temp_report.get("skipped", [])
    report["temp_errors"] = temp_report.get("errors", [])
    return report


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
