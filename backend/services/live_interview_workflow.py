from __future__ import annotations

from typing import Any

from services.live_interview_media_service import LiveMediaError
from services.live_interview_media_service import (
    cleanup_temporary_media as _cleanup_temporary_media,
)
from services.live_interview_media_service import delete_live_media as _delete_live_media
from services.live_interview_media_service import delete_live_recording as _delete_live_recording
from services.live_interview_media_service import list_admin_recordings as _list_admin_recordings
from services.live_interview_media_service import list_user_recordings as _list_user_recordings
from services.live_interview_media_service import read_live_media as _read_live_media
from services.live_interview_media_service import recording_details as _recording_details
from services.live_interview_media_service import (
    retry_pending_deletions as _retry_pending_deletions,
)
from services.live_interview_media_service import (
    run_storage_integrity_diagnostics as _run_storage_integrity_diagnostics,
)
from services.live_interview_media_service import verify_live_media as _verify_live_media
from services.resource_monitor import ResourceBusyError
from services.speech_to_text import get_speech_to_text


class LiveInterviewError(ValueError):
    """Raised when a live interview media operation cannot advance."""


def transcribe_audio_bytes(wav_bytes: bytes, *, transcriber: Any = None) -> str:
    try:
        service = transcriber or get_speech_to_text()
        result = service.transcribe_from_bytes(wav_bytes, format="wav")
    except ResourceBusyError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError(
            "Could not transcribe the captured audio. Check the local Whisper setup and retry."
        ) from exc
    text = str(getattr(result, "text", "") or "").strip()
    if not text:
        raise LiveInterviewError("No speech was detected. Please retry your answer.")
    return text


def delete_live_recording(
    recording_id: int,
    *,
    user_id: int | None = None,
    session_token: str | None = None,
    admin_id: int | None = None,
) -> dict[str, Any]:
    try:
        return _delete_live_recording(
            int(recording_id),
            actor_user_id=user_id,
            session_token=session_token,
            actor_admin_id=admin_id,
        )
    except LiveMediaError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not delete the recording.") from exc


def delete_live_media(
    media_id: int,
    *,
    user_id: int | None = None,
    session_token: str | None = None,
    admin_id: int | None = None,
) -> dict[str, Any]:
    try:
        return _delete_live_media(
            int(media_id),
            actor_user_id=user_id,
            session_token=session_token,
            actor_admin_id=admin_id,
        )
    except LiveMediaError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not delete the recording file.") from exc


def list_live_recordings(*, user_id: int, session_token: str) -> list[dict[str, Any]]:
    try:
        return _list_user_recordings(user_id=int(user_id), session_token=session_token)
    except LiveMediaError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not load your interview recordings.") from exc


def list_admin_live_recordings(
    *,
    admin_id: int,
    owner_user_id: int | None = None,
    status: str | None = None,
    search: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    try:
        return _list_admin_recordings(
            actor_admin_id=int(admin_id),
            owner_user_id=owner_user_id,
            status=status,
            search=search,
            limit=limit,
        )
    except LiveMediaError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not load live interview recordings.") from exc


def read_live_media(
    media_id: int,
    *,
    user_id: int | None = None,
    session_token: str | None = None,
    admin_id: int | None = None,
) -> tuple[bytes, str, str]:
    try:
        return _read_live_media(
            int(media_id),
            actor_user_id=user_id,
            session_token=session_token,
            actor_admin_id=admin_id,
        )
    except LiveMediaError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not open the recording.") from exc


def verify_live_media(
    media_id: int,
    *,
    user_id: int | None = None,
    session_token: str | None = None,
    admin_id: int | None = None,
) -> dict[str, Any]:
    try:
        return _verify_live_media(
            int(media_id),
            actor_user_id=user_id,
            session_token=session_token,
            actor_admin_id=admin_id,
        )
    except LiveMediaError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not verify the recording.") from exc


def get_live_recording_details(
    *,
    interview_id: int,
    user_id: int | None = None,
    session_token: str | None = None,
    admin_id: int | None = None,
) -> dict[str, Any]:
    try:
        return _recording_details(
            interview_id=int(interview_id),
            user_id=user_id,
            session_token=session_token,
            actor_admin_id=admin_id,
        )
    except LiveMediaError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not load interview details.") from exc


def retry_live_media_deletions(
    *,
    user_id: int | None = None,
    session_token: str | None = None,
    admin_id: int | None = None,
    recording_id: int | None = None,
) -> dict[str, Any]:
    try:
        return _retry_pending_deletions(
            actor_user_id=user_id,
            session_token=session_token,
            actor_admin_id=admin_id,
            recording_id=recording_id,
        )
    except LiveMediaError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not retry pending deletions.") from exc


def run_live_storage_integrity_diagnostics(
    *, admin_id: int | None = None, min_temp_age_seconds: int = 300
) -> dict[str, Any]:
    try:
        return _run_storage_integrity_diagnostics(
            actor_admin_id=admin_id,
            min_temp_age_seconds=min_temp_age_seconds,
        )
    except LiveMediaError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not run storage diagnostics.") from exc


def cleanup_live_temporary_media(*, min_age_seconds: int = 300, dry_run: bool = True) -> dict[str, Any]:
    try:
        return _cleanup_temporary_media(min_age_seconds=min_age_seconds, dry_run=dry_run)
    except LiveMediaError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not inspect temporary media files.") from exc
