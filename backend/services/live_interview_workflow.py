from __future__ import annotations

from datetime import datetime
from typing import Any

from services.adaptive_interview_engine import AdaptiveInterviewEngine
from services.job_interview_service import (
    JobInterviewError,
    JobInterviewState,
    abandon_job_interview,
    complete_job_interview,
    get_job_interview_report,
    get_job_interview_state,
    list_job_interviews,
    start_job_interview,
    submit_job_answer,
)
from services.job_post_service import list_jobs
from services.live_interview_media_service import LiveMediaError
from services.live_interview_media_service import delete_live_media as _delete_live_media
from services.live_interview_media_service import delete_live_recording as _delete_live_recording
from services.live_interview_media_service import ensure_live_recording as _ensure_live_recording
from services.live_interview_media_service import (
    get_live_recording_for_interview as _get_live_recording_for_interview,
)
from services.live_interview_media_service import list_admin_recordings as _list_admin_recordings
from services.live_interview_media_service import list_user_recordings as _list_user_recordings
from services.live_interview_media_service import read_live_media as _read_live_media
from services.live_interview_media_service import recording_details as _recording_details
from services.live_interview_media_service import save_live_media as _save_live_media
from services.live_interview_media_service import set_recording_status as _set_recording_status
from services.live_interview_media_service import verify_live_media as _verify_live_media
from services.live_webcam_audio import LiveAudioRecorder
from services.speech_to_text import get_speech_to_text


class LiveInterviewError(ValueError):
    """Raised when the live webcam interview cannot advance."""


def available_jobs():
    return list_jobs()


def list_live_interviews(*, user_id: int, session_token: str) -> list[dict[str, Any]]:
    try:
        return list_job_interviews(
            actor_user_id=int(user_id), session_token=session_token, completed=False
        )
    except JobInterviewError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not load unfinished interviews.") from exc


def get_live_interview_state(
    interview_id: int,
    *,
    user_id: int,
    session_token: str,
    engine: AdaptiveInterviewEngine | None = None,
) -> JobInterviewState:
    try:
        return get_job_interview_state(interview_id, user_id, session_token, engine=engine)
    except JobInterviewError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("The live interview session is unavailable.") from exc


def start_live_interview(
    *,
    user_id: int,
    session_token: str,
    job_id: int,
    difficulty: str,
    duration_minutes: int,
    camera_consent: bool,
    microphone_consent: bool,
    interview_consent: bool,
    engine: AdaptiveInterviewEngine | None = None,
) -> JobInterviewState:
    if not camera_consent or not microphone_consent or not interview_consent:
        raise LiveInterviewError("Grant camera, microphone, and interview consent first.")
    try:
        return start_job_interview(
            session_token=session_token,
            actor_user_id=user_id,
            job_id=int(job_id),
            mode="video",
            difficulty=str(difficulty),
            duration_minutes=int(duration_minutes),
            consent_given=True,
            engine=engine,
        )
    except JobInterviewError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not start the live webcam interview.") from exc


def capture_audio_bytes(recorder: LiveAudioRecorder) -> bytes:
    wav_bytes = recorder.to_wav_bytes()
    if not wav_bytes:
        raise LiveInterviewError("No audio was captured. Start the answer and speak again.")
    return wav_bytes


def transcribe_audio_bytes(wav_bytes: bytes, *, transcriber: Any = None) -> str:
    try:
        service = transcriber or get_speech_to_text()
        result = service.transcribe_from_bytes(wav_bytes, format="wav")
    except Exception as exc:
        raise LiveInterviewError(
            "Could not transcribe the captured audio. Check the local Whisper setup and retry."
        ) from exc
    text = str(getattr(result, "text", "") or "").strip()
    if not text:
        raise LiveInterviewError("No speech was detected. Please retry your answer.")
    return text


def transcribe_recorder(
    recorder: LiveAudioRecorder,
    *,
    transcriber: Any = None,
    consent_given: bool = True,
) -> str:
    try:
        if not consent_given:
            raise LiveInterviewError("Consent was withdrawn. Audio was discarded.")
        wav_bytes = capture_audio_bytes(recorder)
        return transcribe_audio_bytes(wav_bytes, transcriber=transcriber)
    finally:
        recorder.clear()


def start_live_recording(
    *,
    interview_id: int,
    user_id: int,
    session_token: str,
    consent_given: bool,
    consent_at: str | None,
    policy_version: str,
) -> int:
    try:
        return _ensure_live_recording(
            interview_id=int(interview_id),
            user_id=int(user_id),
            session_token=session_token,
            consent_given=bool(consent_given),
            consent_at=consent_at,
            policy_version=policy_version,
        )
    except LiveMediaError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not initialize the recording record.") from exc


def get_live_recording_for_interview(
    *, interview_id: int, user_id: int, session_token: str
) -> dict[str, Any] | None:
    try:
        return _get_live_recording_for_interview(
            interview_id=int(interview_id), user_id=int(user_id), session_token=session_token
        )
    except LiveMediaError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not load the recording consent for this interview.") from exc


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
    try:
        return _save_live_media(
            recording_id=int(recording_id),
            interview_id=int(interview_id),
            user_id=int(user_id),
            session_token=session_token,
            media_type=media_type,
            data=data,
            duration_seconds=float(duration_seconds),
            metadata=metadata,
        )
    except LiveMediaError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not save the interview recording.") from exc


def finish_live_recording(
    *,
    recording_id: int,
    user_id: int,
    session_token: str,
    status: str,
) -> None:
    try:
        _set_recording_status(
            int(recording_id),
            actor_user_id=int(user_id),
            session_token=session_token,
            status=status,
        )
    except LiveMediaError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not update the recording status.") from exc


def delete_live_media(
    media_id: int,
    *,
    user_id: int | None = None,
    session_token: str | None = None,
    admin_id: int | None = None,
) -> None:
    try:
        _delete_live_media(
            int(media_id),
            actor_user_id=user_id,
            session_token=session_token,
            actor_admin_id=admin_id,
        )
    except LiveMediaError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not delete the recording.") from exc


def delete_live_recording(
    recording_id: int,
    *,
    user_id: int | None = None,
    session_token: str | None = None,
    admin_id: int | None = None,
) -> None:
    try:
        _delete_live_recording(
            int(recording_id),
            actor_user_id=user_id,
            session_token=session_token,
            actor_admin_id=admin_id,
        )
    except LiveMediaError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not delete the recording.") from exc


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


def submit_live_answer(
    *,
    interview_id: int,
    question_number: int,
    transcript: str,
    user_id: int,
    session_token: str,
    answer_started_at: datetime | str | None,
    consent_given: bool = True,
    engine: AdaptiveInterviewEngine | None = None,
) -> JobInterviewState:
    text = str(transcript or "").strip()
    if not text:
        raise LiveInterviewError("Please provide an answer before submitting.")
    if not consent_given:
        raise LiveInterviewError("Consent was withdrawn. The answer was not scored or saved.")
    try:
        return submit_job_answer(
            interview_id=int(interview_id),
            question_id=int(question_number),
            answer=text,
            media_id=None,
            actor_user_id=int(user_id),
            session_token=session_token,
            answer_started_at=answer_started_at,
            engine=engine,
        )
    except JobInterviewError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError(
            "Could not score or save the answer. No answer was saved; review it and retry."
        ) from exc


def complete_live_interview(
    *,
    interview_id: int,
    user_id: int,
    session_token: str,
    consent_given: bool = True,
    engine: AdaptiveInterviewEngine | None = None,
) -> dict[str, Any]:
    if not consent_given:
        raise LiveInterviewError("Consent was withdrawn. The final report was not generated.")
    try:
        return complete_job_interview(
            interview_id=int(interview_id),
            actor_user_id=int(user_id),
            session_token=session_token,
            engine=engine,
        )
    except JobInterviewError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not generate the final report.") from exc


def get_live_report(
    *, interview_id: int, user_id: int, session_token: str
) -> dict[str, Any]:
    try:
        return get_job_interview_report(
            int(interview_id), actor_user_id=int(user_id), session_token=session_token
        )
    except JobInterviewError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("The final report is unavailable.") from exc


def abandon_live_interview(
    *, interview_id: int, user_id: int, session_token: str
) -> None:
    try:
        abandon_job_interview(
            interview_id=int(interview_id),
            actor_user_id=int(user_id),
            session_token=session_token,
        )
    except JobInterviewError as exc:
        raise LiveInterviewError(str(exc)) from exc
    except Exception as exc:
        raise LiveInterviewError("Could not save the unfinished interview.") from exc
