from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from services.adaptive_interview_engine import AdaptiveInterviewEngine
from services.ai_avatar_session import (
    LISTENING,
    PROCESSING,
    STATE_KEY,
    AvatarSessionError,
    begin_listening,
    begin_processing,
    clear_audio,
    clear_error,
    current_ai_avatar_state,
    initialize_ai_avatar_state,
    mark_cancelled,
    mark_completed,
    mark_question_spoken,
    mark_ready_to_complete,
    mark_resumed,
    mark_started,
    mark_submitted,
    pause,
    register_audio,
    repeat_question,
    require_avatar_owner,
    reset_ai_avatar_state,
    resume,
    set_error,
    set_transcript,
)
from services.job_interview_service import (
    JobInterviewError,
    JobInterviewState,
    abandon_job_interview,
    complete_job_interview,
    get_job_interview_report,
    get_job_interview_state,
    start_job_interview,
    submit_job_answer,
)
from services.live_interview_workflow import transcribe_audio_bytes
from services.resource_monitor import acquire_heavy_task


class AvatarInterviewError(ValueError):
    pass


def _owner_id(user_id: int, interview_id: int | None = None) -> str:
    if interview_id is None:
        return f"ai_avatar:{int(user_id)}"
    return f"ai_avatar:{int(user_id)}:{int(interview_id)}"


def _interview_id(record: dict[str, Any]) -> int:
    value = record.get("interview_id")
    if value is None:
        raise AvatarInterviewError("Start an AI avatar interview first.")
    return int(value)


def _active_record(
    state: MutableMapping[str, Any], user_id: int, session_token: str
) -> dict[str, Any]:
    try:
        initialize_ai_avatar_state(state, user_id, session_token)
        return require_avatar_owner(state, user_id, session_token)
    except AvatarSessionError as exc:
        raise AvatarInterviewError(str(exc)) from exc


def start_avatar_interview(
    state: MutableMapping[str, Any],
    *,
    user_id: int,
    session_token: str,
    job_id: int,
    difficulty: str,
    duration_minutes: int,
    consent_given: bool,
    engine: AdaptiveInterviewEngine | None = None,
) -> JobInterviewState:
    _active_record(state, user_id, session_token)
    if not consent_given:
        raise AvatarInterviewError("Give consent before starting an AI avatar interview.")
    reset_ai_avatar_state(state, user_id, session_token)
    record = current_ai_avatar_state(state)
    try:
        with acquire_heavy_task("llm_inference", owner_id=_owner_id(user_id)):
            job_state = start_job_interview(
                session_token=session_token,
                actor_user_id=user_id,
                job_id=int(job_id),
                mode="avatar",
                difficulty=str(difficulty),
                duration_minutes=int(duration_minutes),
                consent_given=True,
                engine=engine,
            )
    except Exception as exc:
        set_error(record, str(exc) or "Could not start the AI avatar interview.")
        raise AvatarInterviewError(str(exc) or "Could not start the AI avatar interview.") from exc
    question = job_state.current_question
    if question is None:
        set_error(record, "The AI avatar could not prepare the first question.")
        raise AvatarInterviewError("The AI avatar could not prepare the first question.")
    mark_started(
        record,
        interview_id=job_state.interview_id,
        job_title=job_state.job_title,
        company=job_state.company,
        question_id=int(question.question_number),
        question_number=int(question.question_number),
        question_text=str(question.question_text),
        question_category=str(question.category),
        question_difficulty=str(question.difficulty),
        consent_given=True,
    )
    return job_state


def get_avatar_interview_state(
    state: MutableMapping[str, Any], *, user_id: int, session_token: str
) -> JobInterviewState:
    record = _active_record(state, user_id, session_token)
    return get_job_interview_state(_interview_id(record), user_id, session_token)


def resume_avatar_interview(
    state: MutableMapping[str, Any],
    *,
    interview_id: int,
    user_id: int,
    session_token: str,
) -> JobInterviewState:
    _active_record(state, user_id, session_token)
    job_state = get_job_interview_state(int(interview_id), user_id, session_token)
    if job_state.config.mode != "avatar":
        raise AvatarInterviewError("This interview is not an AI avatar interview.")
    if job_state.is_complete:
        raise AvatarInterviewError("This interview is already completed.")
    record = current_ai_avatar_state(state)
    question = job_state.current_question
    if question is None:
        mark_ready_to_complete(
            record,
            interview_id=job_state.interview_id,
            job_title=job_state.job_title,
            company=job_state.company,
            consent_given=job_state.consent_given,
        )
        return job_state
    mark_resumed(
        record,
        interview_id=job_state.interview_id,
        job_title=job_state.job_title,
        company=job_state.company,
        question_id=int(question.question_number),
        question_number=int(question.question_number),
        question_text=str(question.question_text),
        question_category=str(question.category),
        question_difficulty=str(question.difficulty),
        consent_given=job_state.consent_given,
    )
    return job_state


def mark_avatar_question_spoken(
    state: MutableMapping[str, Any], *, user_id: int, session_token: str
) -> None:
    record = _active_record(state, user_id, session_token)
    mark_question_spoken(record)


def repeat_avatar_question(
    state: MutableMapping[str, Any], *, user_id: int, session_token: str
) -> None:
    record = _active_record(state, user_id, session_token)
    repeat_question(record)


def begin_avatar_listening(
    state: MutableMapping[str, Any], *, user_id: int, session_token: str
) -> None:
    record = _active_record(state, user_id, session_token)
    begin_listening(record)


def pause_avatar_interview(
    state: MutableMapping[str, Any], *, user_id: int, session_token: str
) -> None:
    record = _active_record(state, user_id, session_token)
    pause(record)


def resume_avatar_interview_state(
    state: MutableMapping[str, Any], *, user_id: int, session_token: str
) -> None:
    record = _active_record(state, user_id, session_token)
    resume(record)


def retry_avatar_step(
    state: MutableMapping[str, Any], *, user_id: int, session_token: str
) -> None:
    record = _active_record(state, user_id, session_token)
    previous = str(record.get("error_from") or "")
    clear_error(record)
    if previous == PROCESSING:
        record["status"] = LISTENING


def transcribe_avatar_answer(
    state: MutableMapping[str, Any],
    *,
    audio_bytes: bytes,
    user_id: int,
    session_token: str,
    transcriber: Any = None,
) -> str:
    record = _active_record(state, user_id, session_token)
    if not record.get("consent_given"):
        raise AvatarInterviewError("Give consent before transcribing an answer.")
    status = str(record.get("status") or "")
    if status != "listening":
        raise AvatarInterviewError("Start answering before transcribing your microphone audio.")
    if not audio_bytes:
        set_error(record, "No audio was captured. Start the answer and speak again.")
        raise AvatarInterviewError("No audio was captured. Start the answer and speak again.")
    register_audio(record, audio_bytes)
    begin_processing(record)
    try:
        text = transcribe_audio_bytes(audio_bytes, transcriber=transcriber)
    except Exception as exc:
        message = str(exc) or "Could not transcribe the microphone answer."
        set_error(record, message)
        raise AvatarInterviewError(message) from exc
    set_transcript(record, text)
    clear_audio(record)
    return text


def submit_avatar_answer(
    state: MutableMapping[str, Any],
    *,
    answer: str,
    user_id: int,
    session_token: str,
    engine: AdaptiveInterviewEngine | None = None,
) -> JobInterviewState:
    record = _active_record(state, user_id, session_token)
    status = str(record.get("status") or "")
    if status not in {"waiting", "error"}:
        raise AvatarInterviewError("Review the current question before submitting an answer.")
    text = str(answer or "").strip()
    if not text:
        set_error(record, "Please provide an answer before submitting.")
        raise AvatarInterviewError("Please provide an answer before submitting.")
    interview_id = _interview_id(record)
    question_id = int(record.get("question_id") or 0)
    if question_id <= 0:
        raise AvatarInterviewError("The current question is unavailable.")
    answered = [int(item) for item in (record.get("answered_ids") or [])]
    if question_id in answered:
        raise AvatarInterviewError("This question has already been answered.")
    try:
        with acquire_heavy_task("llm_inference", owner_id=_owner_id(user_id, interview_id)):
            job_state = submit_job_answer(
                interview_id=interview_id,
                question_id=question_id,
                answer=text,
                media_id=None,
                actor_user_id=user_id,
                session_token=session_token,
                answer_started_at=str(record.get("question_started_at") or "") or None,
                engine=engine,
            )
    except Exception as exc:
        message = str(exc) or "The AI avatar could not evaluate this answer."
        set_error(record, message)
        raise AvatarInterviewError(message) from exc
    mark_submitted(record, job_state.current_question)
    return job_state


def complete_avatar_interview(
    state: MutableMapping[str, Any],
    *,
    user_id: int,
    session_token: str,
    engine: AdaptiveInterviewEngine | None = None,
) -> dict[str, Any]:
    record = _active_record(state, user_id, session_token)
    interview_id = _interview_id(record)
    if not record.get("can_complete"):
        job_state = get_job_interview_state(interview_id, user_id, session_token)
        if job_state.current_question is not None:
            raise AvatarInterviewError("Answer the current question before finishing.")
    try:
        with acquire_heavy_task("llm_inference", owner_id=_owner_id(user_id, interview_id)):
            report = complete_job_interview(
                interview_id=interview_id,
                actor_user_id=user_id,
                session_token=session_token,
                engine=engine,
            )
    except Exception as exc:
        message = str(exc) or "Could not generate the AI avatar interview report."
        set_error(record, message)
        raise AvatarInterviewError(message) from exc
    mark_completed(record)
    return report


def get_avatar_report(
    state: MutableMapping[str, Any], *, user_id: int, session_token: str
) -> dict[str, Any]:
    record = _active_record(state, user_id, session_token)
    return get_job_interview_report(
        _interview_id(record), actor_user_id=user_id, session_token=session_token
    )


def cancel_avatar_interview(
    state: MutableMapping[str, Any], *, user_id: int, session_token: str
) -> None:
    record = _active_record(state, user_id, session_token)
    interview_id = record.get("interview_id")
    if interview_id is not None and str(record.get("status") or "") not in {"completed"}:
        try:
            abandon_job_interview(
                interview_id=int(interview_id),
                actor_user_id=user_id,
                session_token=session_token,
            )
        except JobInterviewError:
            pass
    mark_cancelled(record)


def clear_avatar_session(state: MutableMapping[str, Any], *, user_id: int, session_token: str) -> None:
    state.pop(STATE_KEY, None)
    initialize_ai_avatar_state(state, user_id, session_token)


__all__ = [
    "AvatarInterviewError",
    "AvatarSessionError",
    "begin_avatar_listening",
    "cancel_avatar_interview",
    "clear_avatar_session",
    "complete_avatar_interview",
    "get_avatar_interview_state",
    "get_avatar_report",
    "mark_avatar_question_spoken",
    "pause_avatar_interview",
    "repeat_avatar_question",
    "resume_avatar_interview",
    "resume_avatar_interview_state",
    "retry_avatar_step",
    "start_avatar_interview",
    "submit_avatar_answer",
    "transcribe_avatar_answer",
]
