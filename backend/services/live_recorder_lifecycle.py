from __future__ import annotations

import copy
import threading
import uuid
from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import Any

RECORDER_LIFECYCLE = "live_webcam_recorder_lifecycle"
RECORDER_LIFECYCLE_CONDITION = "live_webcam_recorder_lifecycle_condition"
RECORDER_LAST_FAILURE = "live_webcam_recorder_last_failure"
AUDIO_ERROR = "live_webcam_audio_recording_error"
VIDEO_ERROR = "live_webcam_video_recording_error"

IDLE = "IDLE"
STARTING = "STARTING"
RECORDING = "RECORDING"
STOPPING = "STOPPING"
FINALIZING = "FINALIZING"
SAVED = "SAVED"
PARTIAL = "PARTIAL"
FAILED = "FAILED"
ABANDONED = "ABANDONED"

LIFECYCLE_STATES = frozenset(
    {
        IDLE,
        STARTING,
        RECORDING,
        STOPPING,
        FINALIZING,
        SAVED,
        PARTIAL,
        FAILED,
        ABANDONED,
    }
)

_ALLOWED_TRANSITIONS = {
    IDLE: frozenset({STARTING}),
    STARTING: frozenset({RECORDING, PARTIAL, FAILED, ABANDONED}),
    RECORDING: frozenset({STOPPING, PARTIAL, FAILED, ABANDONED}),
    STOPPING: frozenset({FINALIZING, PARTIAL, FAILED, ABANDONED}),
    FINALIZING: frozenset({SAVED, PARTIAL, FAILED, ABANDONED}),
    SAVED: frozenset({IDLE}),
    PARTIAL: frozenset({IDLE}),
    FAILED: frozenset({IDLE}),
    ABANDONED: frozenset({IDLE}),
}

_ACTIVE_STATES = frozenset({STARTING, RECORDING, STOPPING, FINALIZING})
_TERMINAL_STATES = frozenset({SAVED, PARTIAL, FAILED, ABANDONED})

OPERATION_FINALIZE = "finalize"
OPERATION_TRANSCRIBE = "transcribe"
OPERATION_PERSIST = "persist"
_OPERATION_NAMES = frozenset(
    {OPERATION_FINALIZE, OPERATION_TRANSCRIBE, OPERATION_PERSIST}
)

_MEDIA_STATUS_STATES = {
    "saved": SAVED,
    "not_saved": SAVED,
    "partial": PARTIAL,
    "delete_failed": PARTIAL,
    "failed": FAILED,
    "finalize_failed": FAILED,
    "deleted": ABANDONED,
    "interrupted": ABANDONED,
}


def _default_record() -> dict[str, Any]:
    return {
        "state": IDLE,
        "answer_id": None,
        "interview_id": None,
        "question_number": None,
        "generation": 0,
        "started_at_utc": None,
        "stop_requested": False,
        "audio_status": "not_started",
        "video_status": "not_started",
        "audio_error": None,
        "video_error": None,
        "state_error": None,
        "operations": {},
    }


def _condition(state: MutableMapping) -> threading.Condition:
    candidate = state.get(RECORDER_LIFECYCLE_CONDITION)
    if isinstance(candidate, threading.Condition):
        return candidate
    replacement = threading.Condition(threading.RLock())
    state[RECORDER_LIFECYCLE_CONDITION] = replacement
    return replacement


def _record_locked(state: MutableMapping) -> dict[str, Any]:
    candidate = state.get(RECORDER_LIFECYCLE)
    if not isinstance(candidate, dict) or candidate.get("state") not in LIFECYCLE_STATES:
        candidate = _default_record()
        state[RECORDER_LIFECYCLE] = candidate
    candidate.setdefault("operations", {})
    return candidate


def initialize_recorder_lifecycle(state: MutableMapping) -> dict[str, Any]:
    condition = _condition(state)
    with condition:
        record = _record_locked(state)
        state.setdefault(RECORDER_LAST_FAILURE, None)
        state.setdefault(AUDIO_ERROR, None)
        state.setdefault(VIDEO_ERROR, None)
        return copy.deepcopy(record)


def recorder_lifecycle_snapshot(state: MutableMapping) -> dict[str, Any]:
    condition = _condition(state)
    with condition:
        return copy.deepcopy(_record_locked(state))


def current_answer_id(state: MutableMapping) -> str | None:
    condition = _condition(state)
    with condition:
        answer_id = _record_locked(state).get("answer_id")
        return str(answer_id) if answer_id is not None else None


def is_current_answer(
    state: MutableMapping,
    *,
    answer_id: str | None = None,
    question_number: int | None = None,
) -> bool:
    condition = _condition(state)
    with condition:
        record = _record_locked(state)
        if answer_id is not None:
            existing = record.get("answer_id")
            if existing is None or str(existing) != str(answer_id):
                return False
        if question_number is not None and record.get("question_number") is not None:
            return int(record["question_number"]) == int(question_number)
        return True


def _transition_locked(
    state: MutableMapping,
    record: dict[str, Any],
    target: str,
    *,
    error: str | None = None,
) -> bool:
    if target not in LIFECYCLE_STATES:
        record["state_error"] = f"Unknown recorder lifecycle state: {target}."
        return False
    current = str(record.get("state") or IDLE)
    if current == target:
        if error:
            record["state_error"] = str(error)
        return True
    if target not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
        record["state_error"] = (
            f"Invalid recorder lifecycle transition: {current} -> {target}."
        )
        return False
    record["state"] = target
    record["state_error"] = str(error) if error else None
    _condition(state).notify_all()
    return True


def transition_recorder_lifecycle(
    state: MutableMapping,
    target: str,
    *,
    answer_id: str | None = None,
    error: str | None = None,
) -> bool:
    condition = _condition(state)
    with condition:
        record = _record_locked(state)
        if answer_id is not None and record.get("answer_id") is not None:
            if str(record["answer_id"]) != str(answer_id):
                record["state_error"] = "The recorder answer changed."
                return False
        return _transition_locked(state, record, target, error=error)


def begin_answer(
    state: MutableMapping,
    *,
    question_number: int,
    interview_id: int | str | None = None,
    answer_id: str | None = None,
) -> str:
    condition = _condition(state)
    with condition:
        record = _record_locked(state)
        current_state = str(record.get("state") or IDLE)
        current_question = record.get("question_number")
        existing_id = record.get("answer_id")
        if (
            current_state in _ACTIVE_STATES
            and current_question is not None
            and int(current_question) == int(question_number)
            and existing_id is not None
        ):
            return str(existing_id)
        if current_state in _ACTIVE_STATES:
            _transition_locked(
                state,
                record,
                ABANDONED,
                error="The previous answer ended before it was finalized.",
            )
        if record.get("state") not in {IDLE}:
            _transition_locked(state, record, IDLE)
        _transition_locked(state, record, STARTING)
        token = str(answer_id or uuid.uuid4().hex)
        record.update(
            {
                "answer_id": token,
                "interview_id": None if interview_id is None else str(interview_id),
                "question_number": int(question_number),
                "generation": int(record.get("generation") or 0) + 1,
                "started_at_utc": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                "stop_requested": False,
                "audio_status": "not_started",
                "video_status": "not_started",
                "audio_error": None,
                "video_error": None,
                "state_error": None,
                "operations": {},
            }
        )
        state[AUDIO_ERROR] = None
        state[VIDEO_ERROR] = None
        state[RECORDER_LAST_FAILURE] = None
        condition.notify_all()
        return token


def mark_recording(
    state: MutableMapping,
    *,
    answer_id: str | None = None,
    record_video: bool = False,
) -> bool:
    condition = _condition(state)
    with condition:
        record = _record_locked(state)
        if answer_id is not None and str(record.get("answer_id") or "") != str(answer_id):
            return False
        if not _transition_locked(state, record, RECORDING):
            return False
        record["started_at_utc"] = str(
            record.get("started_at_utc")
            or datetime.now(UTC).replace(tzinfo=None).isoformat()
        )
        record["audio_status"] = "capturing"
        record["video_status"] = "capturing" if record_video else "not_started"
        return True


def mark_stopping(state: MutableMapping, *, answer_id: str | None = None) -> bool:
    condition = _condition(state)
    with condition:
        record = _record_locked(state)
        if answer_id is not None and str(record.get("answer_id") or "") != str(answer_id):
            return False
        current = str(record.get("state") or IDLE)
        if current in {STOPPING, FINALIZING}:
            return False
        if current != RECORDING:
            return False
        record["stop_requested"] = True
        record["audio_status"] = "stopped" if record["audio_status"] != "failed" else "failed"
        record["video_status"] = "stopped" if record["video_status"] != "failed" else "failed"
        return _transition_locked(state, record, STOPPING)


def mark_finalizing(state: MutableMapping, *, answer_id: str | None = None) -> bool:
    condition = _condition(state)
    with condition:
        record = _record_locked(state)
        if answer_id is not None and str(record.get("answer_id") or "") != str(answer_id):
            return False
        current = str(record.get("state") or IDLE)
        if current == FINALIZING:
            return True
        if current not in {RECORDING, STOPPING}:
            return False
        return _transition_locked(state, record, FINALIZING)


def set_stream_statuses(
    state: MutableMapping,
    *,
    answer_id: str | None = None,
    audio_status: str | None = None,
    video_status: str | None = None,
) -> None:
    condition = _condition(state)
    with condition:
        record = _record_locked(state)
        if answer_id is not None and str(record.get("answer_id") or "") != str(answer_id):
            return
        if audio_status is not None:
            record["audio_status"] = str(audio_status)
        if video_status is not None:
            record["video_status"] = str(video_status)
        condition.notify_all()


def set_stream_errors(
    state: MutableMapping,
    *,
    answer_id: str | None = None,
    audio_error: str | None = None,
    video_error: str | None = None,
) -> None:
    condition = _condition(state)
    with condition:
        record = _record_locked(state)
        if answer_id is not None and str(record.get("answer_id") or "") != str(answer_id):
            return
        record["audio_error"] = str(audio_error) if audio_error else None
        record["video_error"] = str(video_error) if video_error else None
        state[AUDIO_ERROR] = record["audio_error"]
        state[VIDEO_ERROR] = record["video_error"]
        condition.notify_all()


def mark_recorder_failure(
    state: MutableMapping,
    message: str,
    *,
    answer_id: str | None = None,
    terminal_state: str = FAILED,
) -> bool:
    condition = _condition(state)
    with condition:
        record = _record_locked(state)
        if answer_id is not None and str(record.get("answer_id") or "") != str(answer_id):
            return False
        detail = str(message)
        state[RECORDER_LAST_FAILURE] = detail
        record["state_error"] = detail
        current = str(record.get("state") or IDLE)
        if current in _ACTIVE_STATES:
            _transition_locked(state, record, terminal_state, error=detail)
        return True


def record_media_status(
    state: MutableMapping, status: str, *, answer_id: str | None = None
) -> None:
    condition = _condition(state)
    with condition:
        record = _record_locked(state)
        if answer_id is not None and str(record.get("answer_id") or "") != str(answer_id):
            return
        target = _MEDIA_STATUS_STATES.get(str(status))
        if target is None:
            return
        current = str(record.get("state") or IDLE)
        if current == target:
            return
        if current in _ACTIVE_STATES:
            _transition_locked(state, record, target)
        elif current in _TERMINAL_STATES and target != current:
            _transition_locked(state, record, IDLE)
            _transition_locked(state, record, target)


def abandon_recorder_lifecycle(
    state: MutableMapping, *, answer_id: str | None = None, reason: str | None = None
) -> bool:
    condition = _condition(state)
    with condition:
        record = _record_locked(state)
        if answer_id is not None and str(record.get("answer_id") or "") != str(answer_id):
            return False
        detail = str(reason) if reason else "The answer was abandoned."
        current = str(record.get("state") or IDLE)
        record["stop_requested"] = True
        if current in _ACTIVE_STATES:
            return _transition_locked(state, record, ABANDONED, error=detail)
        return True


def reset_recorder_lifecycle(state: MutableMapping) -> None:
    condition = _condition(state)
    with condition:
        record = _record_locked(state)
        current = str(record.get("state") or IDLE)
        if current in _ACTIVE_STATES:
            _transition_locked(
                state,
                record,
                ABANDONED,
                error="Recorder state was reset before finalization.",
            )
        replacement = _default_record()
        replacement["generation"] = int(record.get("generation") or 0)
        state[RECORDER_LIFECYCLE] = replacement
        condition.notify_all()


def _operation_token(record: dict[str, Any], answer_id: str | None) -> str:
    if answer_id is not None:
        return str(answer_id)
    existing = record.get("answer_id")
    return str(existing) if existing is not None else "legacy"


def claim_answer_operation(
    state: MutableMapping,
    operation: str,
    *,
    answer_id: str | None = None,
) -> bool:
    if operation not in _OPERATION_NAMES:
        return False
    condition = _condition(state)
    with condition:
        record = _record_locked(state)
        if answer_id is not None:
            existing = record.get("answer_id")
            if existing is not None and str(existing) != str(answer_id):
                return False
        token = _operation_token(record, answer_id)
        operations = record["operations"]
        op = operations.get(operation)
        if not isinstance(op, dict):
            op = {"answer_id": None, "status": "idle", "result": None}
            operations[operation] = op
        if op.get("answer_id") == token and op.get("status") in {"active", "completed"}:
            return False
        op.update({"answer_id": token, "status": "active", "result": None})
        condition.notify_all()
        return True


def complete_answer_operation(
    state: MutableMapping,
    operation: str,
    *,
    answer_id: str | None = None,
    result: Any = None,
) -> None:
    if operation not in _OPERATION_NAMES:
        return
    condition = _condition(state)
    with condition:
        record = _record_locked(state)
        token = _operation_token(record, answer_id)
        op = record["operations"].get(operation)
        if not isinstance(op, dict) or op.get("answer_id") != token:
            return
        op["status"] = "completed"
        op["result"] = copy.deepcopy(result)
        condition.notify_all()


def operation_result(
    state: MutableMapping,
    operation: str,
    *,
    answer_id: str | None = None,
    wait: bool = False,
    timeout: float = 5.0,
) -> Any:
    if operation not in _OPERATION_NAMES:
        return None
    condition = _condition(state)
    with condition:
        record = _record_locked(state)
        token = _operation_token(record, answer_id)
        op = record["operations"].get(operation)
        if not isinstance(op, dict) or op.get("answer_id") != token:
            return None
        if wait and op.get("status") == "active":
            timed_out = condition.wait(timeout)
            while op.get("status") == "active" and not timed_out:
                timed_out = condition.wait(timeout)
        if op.get("status") != "completed":
            return None
        return copy.deepcopy(op.get("result"))


def claim_finalization(
    state: MutableMapping,
    *,
    answer_id: str | None = None,
    question_number: int | None = None,
) -> tuple[bool, Any]:
    if not is_current_answer(
        state, answer_id=answer_id, question_number=question_number
    ):
        return False, None
    cached = operation_result(
        state,
        OPERATION_FINALIZE,
        answer_id=answer_id,
        wait=True,
        timeout=5.0,
    )
    if cached is not None:
        return False, cached
    snapshot = recorder_lifecycle_snapshot(state)
    if snapshot.get("answer_id") is None and snapshot.get("state") == IDLE:
        transition_recorder_lifecycle(state, STARTING)
        transition_recorder_lifecycle(state, RECORDING)
        transition_recorder_lifecycle(state, STOPPING)
    if not mark_finalizing(state, answer_id=answer_id):
        current = recorder_lifecycle_snapshot(state).get("state")
        if current not in {FINALIZING, SAVED, PARTIAL, FAILED, ABANDONED}:
            return False, None
    return claim_answer_operation(
        state, OPERATION_FINALIZE, answer_id=answer_id
    ), None
