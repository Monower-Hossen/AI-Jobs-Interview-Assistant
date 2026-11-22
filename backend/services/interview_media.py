from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import magic
from config.settings import runtime_path
from database.db import get_db_context
from database.models import InterviewMedia, InterviewSession, UserSession
from pydub import AudioSegment
from services.auth_service import require_owned_session_token
from services.job_interview_constants import MAX_UPLOAD_BYTES
from services.speech_to_text import transcribe_audio

logger = logging.getLogger(__name__)

_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm"}
_VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".m4v"}
_MIME_PREFIXES = {"audio": "audio/", "video": "video/"}


class MediaError(ValueError):
    """Raised when interview media is invalid or inaccessible."""


def _validate_media(data: bytes, filename: str, kind: str) -> str:
    if kind not in {"audio", "video"}:
        raise MediaError("Unsupported media type.")
    if not data:
        raise MediaError("The recording is empty. Please try again.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise MediaError("The recording is too large (250 MB maximum).")

    suffix = Path(filename or "").suffix.lower()
    allowed = _AUDIO_SUFFIXES if kind == "audio" else _VIDEO_SUFFIXES
    if suffix not in allowed:
        raise MediaError(f"Unsupported file format. Use: {', '.join(sorted(allowed))}.")

    try:
        detected = str(magic.from_buffer(data[:4096], mime=True) or "").lower()
    except Exception as exc:
        logger.debug("MIME detection unavailable: %s", exc)
        detected = ""
    if detected and not detected.startswith(_MIME_PREFIXES[kind]):
        raise MediaError(f"This file does not look like a {kind} recording.")
    return suffix


def _verify_interview_access(
    db: Any, interview_id: int, actor_user_id: int, session_token: str
) -> None:
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
        raise MediaError("This interview does not belong to your workspace.")


def save_interview_media(
    *,
    data: bytes,
    filename: str,
    kind: str,
    interview_id: int,
    actor_user_id: int,
    session_token: str,
    consent_given: bool,
) -> InterviewMedia:
    if not consent_given:
        raise MediaError("Give recording consent before uploading interview media.")
    suffix = _validate_media(data, filename, kind)
    with get_db_context() as db:
        _verify_interview_access(db, interview_id, actor_user_id, session_token)
        target_dir = (
            runtime_path("data/secure/interview_media")
            / str(int(actor_user_id))
            / str(int(interview_id))
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        stored = target_dir / f"{uuid.uuid4().hex}{suffix}"
        stored.write_bytes(data)
        record = InterviewMedia(
            interview_session_id=int(interview_id),
            owner_user_id=int(actor_user_id),
            media_type=kind,
            storage_path=str(stored),
            original_name=Path(filename).name[:255],
            mime_type=magic.from_buffer(data[:4096], mime=True),
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            consent_given=True,
        )
        db.add(record)
        db.flush()
        return record


def attach_media_to_answer(media_id: int, answer_id: int) -> None:
    with get_db_context() as db:
        record = db.query(InterviewMedia).filter(InterviewMedia.id == int(media_id)).first()
        if record is None:
            raise MediaError("Recording not found.")
        record.answer_id = int(answer_id)
        db.flush()


def get_media_for_answer(
    answer_id: int, actor_user_id: int, session_token: str
) -> InterviewMedia | None:
    require_owned_session_token(session_token, actor_user_id)
    with get_db_context() as db:
        return (
            db.query(InterviewMedia)
            .filter(
                InterviewMedia.answer_id == int(answer_id),
                InterviewMedia.owner_user_id == int(actor_user_id),
            )
            .first()
        )


def read_media(media_id: int, *, actor_user_id: int, session_token: str) -> tuple[bytes, str, str]:
    require_owned_session_token(session_token, actor_user_id)
    with get_db_context() as db:
        record = (
            db.query(InterviewMedia)
            .filter(
                InterviewMedia.id == int(media_id),
                InterviewMedia.owner_user_id == int(actor_user_id),
            )
            .first()
        )
        if record is None:
            raise MediaError("Recording not found.")
        path = Path(record.storage_path)
        if not path.exists():
            raise MediaError("Recording file is missing.")
        return path.read_bytes(), record.mime_type or "application/octet-stream", record.media_type


def transcribe_media(media_id: int, *, actor_user_id: int, session_token: str) -> str:
    require_owned_session_token(session_token, actor_user_id)
    with get_db_context() as db:
        record = (
            db.query(InterviewMedia)
            .filter(
                InterviewMedia.id == int(media_id),
                InterviewMedia.owner_user_id == int(actor_user_id),
            )
            .first()
        )
        if record is None:
            raise MediaError("Recording not found.")
        path = Path(record.storage_path)
        if not path.exists():
            raise MediaError("Recording file is missing.")
        media_type = record.media_type
        stored_path = str(path)

    temp_path: str | None = None
    if media_type == "video":
        try:
            audio = AudioSegment.from_file(stored_path)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                temp_path = handle.name
            audio.export(temp_path, format="wav")
            source_path = temp_path
        except Exception as exc:
            logger.warning("Video audio extraction failed: %s", exc)
            raise MediaError(
                "Could not extract speech from this video. Please type your answer."
            ) from exc
    elif media_type == "audio":
        source_path = stored_path
    else:
        raise MediaError("Unsupported media type.")

    try:
        result = transcribe_audio(source_path)
    except Exception as exc:
        logger.warning("Media transcription failed: %s", exc)
        raise MediaError(
            "Could not transcribe this recording. Please type or edit your answer."
        ) from exc
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
    return result.text.strip()


def delete_interview_media(media_id: int, *, actor_user_id: int, session_token: str) -> None:
    require_owned_session_token(session_token, actor_user_id)
    with get_db_context() as db:
        record = (
            db.query(InterviewMedia)
            .filter(
                InterviewMedia.id == int(media_id),
                InterviewMedia.owner_user_id == int(actor_user_id),
            )
            .first()
        )
        if record is None:
            raise MediaError("Recording not found.")
        try:
            Path(record.storage_path).unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove media file %s", record.storage_path)
        db.delete(record)
        db.flush()
