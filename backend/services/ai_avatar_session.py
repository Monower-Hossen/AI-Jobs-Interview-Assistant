from __future__ import annotations

import hashlib
from collections.abc import MutableMapping
from typing import Any

STATE_KEY = "ai_avatar_state"

IDLE = "idle"
SETUP = "setup"
SPEAKING = "speaking"
LISTENING = "listening"
PROCESSING = "processing"
WAITING = "waiting"
PAUSED = "paused"
COMPLETED = "completed"
ERROR = "error"
CANCELLED = "cancelled"

TERMINAL_STATUSES = frozenset({COMPLETED, CANCELLED})
ACTIVE_STATUSES = frozenset(
    {SETUP, SPEAKING, LISTENING, PROCESSING, WAITING, PAUSED, ERROR}
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    IDLE: frozenset({SETUP, SPEAKING, CANCELLED}),
    SETUP: frozenset({SPEAKING, WAITING, PAUSED, ERROR, CANCELLED}),
    SPEAKING: frozenset({WAITING, PAUSED, ERROR, CANCELLED}),
    WAITING: frozenset(
        {LISTENING, SPEAKING, PROCESSING, PAUSED, COMPLETED, ERROR, CANCELLED}
    ),
    LISTENING: frozenset({PROCESSING, WAITING, PAUSED, ERROR, CANCELLED}),
    PROCESSING: frozenset({SPEAKING, WAITING, PAUSED, ERROR, CANCELLED}),
    PAUSED: frozenset({SPEAKING, WAITING, LISTENING, ERROR, CANCELLED}),
    ERROR: frozenset(
        {SPEAKING, WAITING, LISTENING, PROCESSING, PAUSED, COMPLETED, CANCELLED}
    ),
    COMPLETED: frozenset({SETUP}),
    CANCELLED: frozenset({SETUP}),
}


class AvatarSessionError(ValueError):
    pass


def owner_fingerprint(user_id: int, session_token: str) -> str:
    raw = f"{int(user_id)}:{session_token!s}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def default_state() -> dict[str, Any]:
    return {
        "status": IDLE,
        "owner_fingerprint": "",
        "interview_id": None,
        "job_title": "",
        "company": "",
        "question_id": None,
        "question_number": 0,
        "question_text": "",
        "question_category": "",
        "question_difficulty": "",
        "question_started_at": "",
        "tts_key": 0,
        "audio_key": 0,
        "audio_present": False,
        "audio_size": 0,
        "transcript": "",
        "transcript_key": 0,
        "answered_ids": [],
        "can_complete": False,
        "consent_given": False,
        "last_error": "",
        "error_from": "",
    }


def current_ai_avatar_state(state: MutableMapping[str, Any]) -> dict[str, Any]:
    record = state.get(STATE_KEY)
    if isinstance(record, dict):
        return record
    fresh = default_state()
    state[STATE_KEY] = fresh
    return fresh


def initialize_ai_avatar_state(
    state: MutableMapping[str, Any], user_id: int, session_token: str
) -> dict[str, Any]:
    fingerprint = owner_fingerprint(user_id, session_token)
    record = state.get(STATE_KEY)
    if isinstance(record, dict) and record.get("owner_fingerprint") == fingerprint:
        return record
    fresh = default_state()
    fresh["owner_fingerprint"] = fingerprint
    state[STATE_KEY] = fresh
    return fresh


def reset_ai_avatar_state(
    state: MutableMapping[str, Any], user_id: int, session_token: str
) -> dict[str, Any]:
    fingerprint = owner_fingerprint(user_id, session_token)
    fresh = default_state()
    fresh["owner_fingerprint"] = fingerprint
    fresh["status"] = SETUP
    state[STATE_KEY] = fresh
    return fresh


def require_avatar_owner(
    state: MutableMapping[str, Any], user_id: int, session_token: str
) -> dict[str, Any]:
    record = current_ai_avatar_state(state)
    if record.get("owner_fingerprint") != owner_fingerprint(user_id, session_token):
        raise AvatarSessionError("This AI avatar session belongs to a different sign-in.")
    return record


def _transition(record: dict[str, Any], target: str) -> None:
    current = str(record.get("status") or IDLE)
    if current == target:
        return
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise AvatarSessionError(f"Cannot change avatar state from {current} to {target}.")
    record["status"] = target


def _set_question(
    record: dict[str, Any],
    *,
    question_id: int,
    question_number: int,
    question_text: str,
    question_category: str = "",
    question_difficulty: str = "",
) -> None:
    record["question_id"] = int(question_id)
    record["question_number"] = int(question_number)
    record["question_text"] = str(question_text)
    record["question_category"] = str(question_category)
    record["question_difficulty"] = str(question_difficulty)
    record["question_started_at"] = ""
    record["audio_present"] = False
    record["audio_size"] = 0
    record["transcript"] = ""
    record["transcript_key"] = int(record.get("transcript_key") or 0) + 1
    record["tts_key"] = int(record.get("tts_key") or 0) + 1
    record["can_complete"] = False


def mark_started(
    record: dict[str, Any],
    *,
    interview_id: int,
    job_title: str,
    company: str,
    question_id: int,
    question_number: int,
    question_text: str,
    question_category: str = "",
    question_difficulty: str = "",
    consent_given: bool = True,
) -> None:
    record["interview_id"] = int(interview_id)
    record["job_title"] = str(job_title)
    record["company"] = str(company)
    record["answered_ids"] = []
    record["last_error"] = ""
    record["error_from"] = ""
    _set_question(
        record,
        question_id=question_id,
        question_number=question_number,
        question_text=question_text,
        question_category=question_category,
        question_difficulty=question_difficulty,
    )
    record["consent_given"] = bool(consent_given)
    record["status"] = SETUP
    _transition(record, SPEAKING)


def mark_resumed(
    record: dict[str, Any],
    *,
    interview_id: int,
    job_title: str,
    company: str,
    question_id: int,
    question_number: int,
    question_text: str,
    question_category: str = "",
    question_difficulty: str = "",
    consent_given: bool = True,
) -> None:
    record["interview_id"] = int(interview_id)
    record["job_title"] = str(job_title)
    record["company"] = str(company)
    record["consent_given"] = bool(consent_given)
    record["last_error"] = ""
    record["error_from"] = ""
    _set_question(
        record,
        question_id=question_id,
        question_number=question_number,
        question_text=question_text,
        question_category=question_category,
        question_difficulty=question_difficulty,
    )
    record["status"] = SETUP
    _transition(record, SPEAKING)


def mark_question_spoken(record: dict[str, Any]) -> None:
    _transition(record, WAITING)


def repeat_question(record: dict[str, Any]) -> None:
    record["tts_key"] = int(record.get("tts_key") or 0) + 1
    record["last_error"] = ""
    record["status"] = WAITING
    _transition(record, SPEAKING)


def begin_listening(record: dict[str, Any]) -> None:
    record["last_error"] = ""
    _transition(record, LISTENING)


def begin_processing(record: dict[str, Any]) -> None:
    _transition(record, PROCESSING)


def register_audio(record: dict[str, Any], audio_bytes: bytes) -> None:
    if not audio_bytes:
        raise AvatarSessionError("No audio was captured.")
    record["audio_present"] = True
    record["audio_size"] = len(audio_bytes)
    record["audio_key"] = int(record.get("audio_key") or 0) + 1


def clear_audio(record: dict[str, Any]) -> None:
    record["audio_present"] = False
    record["audio_size"] = 0
    record["audio_key"] = int(record.get("audio_key") or 0) + 1


def set_transcript(record: dict[str, Any], text: str) -> None:
    record["transcript"] = str(text or "")
    record["transcript_key"] = int(record.get("transcript_key") or 0) + 1
    record["last_error"] = ""
    _transition(record, WAITING)


def mark_submitted(record: dict[str, Any], next_question: Any | None) -> None:
    question_id = int(record.get("question_id") or 0)
    answered = [int(item) for item in (record.get("answered_ids") or [])]
    if question_id in answered:
        raise AvatarSessionError("This question has already been answered.")
    answered.append(question_id)
    record["answered_ids"] = answered
    record["transcript"] = ""
    record["transcript_key"] = int(record.get("transcript_key") or 0) + 1
    clear_audio(record)
    if next_question is None:
        record["can_complete"] = True
        _transition(record, WAITING)
        return
    _set_question(
        record,
        question_id=int(next_question.question_number),
        question_number=int(next_question.question_number),
        question_text=str(next_question.question_text),
        question_category=str(getattr(next_question, "category", "") or ""),
        question_difficulty=str(getattr(next_question, "difficulty", "") or ""),
    )
    record["status"] = WAITING
    _transition(record, SPEAKING)


def mark_ready_to_complete(
    record: dict[str, Any],
    *,
    interview_id: int,
    job_title: str,
    company: str,
    consent_given: bool = True,
) -> None:
    record["interview_id"] = int(interview_id)
    record["job_title"] = str(job_title)
    record["company"] = str(company)
    record["consent_given"] = bool(consent_given)
    record["question_id"] = None
    record["question_number"] = 0
    record["question_text"] = ""
    record["last_error"] = ""
    record["error_from"] = ""
    record["can_complete"] = True
    record["status"] = SETUP
    _transition(record, WAITING)


def mark_completed(record: dict[str, Any]) -> None:
    _transition(record, COMPLETED)
    record["last_error"] = ""


def mark_cancelled(record: dict[str, Any]) -> None:
    record["status"] = CANCELLED
    record["last_error"] = ""


def pause(record: dict[str, Any]) -> None:
    _transition(record, PAUSED)


def resume(record: dict[str, Any]) -> None:
    _transition(record, WAITING)


def set_error(record: dict[str, Any], message: str) -> None:
    record["error_from"] = str(record.get("status") or IDLE)
    record["last_error"] = str(message or "The AI avatar interview could not continue.")
    record["status"] = ERROR


def clear_error(record: dict[str, Any]) -> None:
    record["last_error"] = ""
    record["status"] = str(record.get("error_from") or WAITING)


def is_terminal(record: dict[str, Any]) -> bool:
    return str(record.get("status") or IDLE) in TERMINAL_STATUSES
