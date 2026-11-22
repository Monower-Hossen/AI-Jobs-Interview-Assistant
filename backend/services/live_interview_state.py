from __future__ import annotations

import hashlib
from collections.abc import MutableMapping
from datetime import UTC, datetime

from services.job_interview_constants import MAX_ANSWER_SECONDS
from services.live_recorder_lifecycle import (
    AUDIO_ERROR,
    RECORDER_LAST_FAILURE,
    RECORDER_LIFECYCLE,
    RECORDER_LIFECYCLE_CONDITION,
    VIDEO_ERROR,
    abandon_recorder_lifecycle,
    begin_answer,
    current_answer_id,
    initialize_recorder_lifecycle,
    mark_recorder_failure,
    mark_recording,
    mark_stopping,
    record_media_status,
    recorder_lifecycle_snapshot,
    reset_recorder_lifecycle,
    set_stream_errors,
    set_stream_statuses,
)
from services.live_recording_manifest import (
    create_capture_manifest,
    mark_manifest_finalized,
    mark_manifest_started,
    monotonic_now_ns,
    safe_load_capture_manifest,
    update_audio_manifest,
    update_video_manifest,
    utc_now_iso,
)
from services.live_webcam_audio import LiveAudioRecorder
from services.live_webcam_video import LiveVideoRecorder

RECORDING_CONSENT = "live_webcam_recording_consent"
RECORDING_CONSENT_AT = "live_webcam_recording_consent_at"
RECORDING_POLICY_VERSION = "live-recording-v1"
RECORDING_ID = "live_webcam_recording_id"
RECORDING_MANIFEST = "live_webcam_recording_manifest"
RECORDING_MANIFEST_RECORDING_ID = "live_webcam_recording_manifest_recording_id"
VIDEO_RECORDER = "live_webcam_video_recorder"
PENDING_MEDIA_IDS = "live_webcam_pending_media_ids"
MEDIA_STATUS = "live_webcam_media_status"
MEDIA_ERROR = "live_webcam_media_error"

ACTIVE_INTERVIEW_ID = "live_webcam_active_interview_id"
REPORT_ID = "live_webcam_report_id"
INTERVIEW_CONSENT = "live_webcam_interview_consent"
RECORDER = "live_webcam_audio_recorder"
RECORDING_ACTIVE = "live_webcam_recording_active"
RECORDING_QUESTION = "live_webcam_recording_question"
ANSWER_STARTED_AT = "live_webcam_answer_started_at"
TRANSCRIPT = "live_webcam_transcript"
TRANSCRIPT_QUESTION = "live_webcam_transcript_question"
TRANSCRIPT_ERROR = "live_webcam_transcript_error"
SUBMITTED_FINGERPRINT = "live_webcam_submitted_fingerprint"
TTS_COUNTER = "live_webcam_tts_counter"
INTERVIEW_ERROR = "live_webcam_interview_error"

LIVE_INTERVIEW_STATE_KEYS = (
    ACTIVE_INTERVIEW_ID,
    REPORT_ID,
    INTERVIEW_CONSENT,
    RECORDING_CONSENT,
    RECORDING_CONSENT_AT,
    RECORDING_ID,
    RECORDING_MANIFEST,
    RECORDING_MANIFEST_RECORDING_ID,
    RECORDER,
    VIDEO_RECORDER,
    PENDING_MEDIA_IDS,
    MEDIA_STATUS,
    MEDIA_ERROR,
    AUDIO_ERROR,
    VIDEO_ERROR,
    RECORDER_LIFECYCLE,
    RECORDER_LIFECYCLE_CONDITION,
    RECORDER_LAST_FAILURE,
    RECORDING_ACTIVE,
    RECORDING_QUESTION,
    ANSWER_STARTED_AT,
    TRANSCRIPT,
    TRANSCRIPT_QUESTION,
    TRANSCRIPT_ERROR,
    SUBMITTED_FINGERPRINT,
    TTS_COUNTER,
    INTERVIEW_ERROR,
)


def initialize_live_interview_state(state: MutableMapping) -> None:
    state.setdefault(ACTIVE_INTERVIEW_ID, None)
    state.setdefault(REPORT_ID, None)
    state.setdefault(INTERVIEW_CONSENT, False)
    state.setdefault(RECORDING_CONSENT, False)
    state.setdefault(RECORDING_CONSENT_AT, None)
    state.setdefault(RECORDING_ID, None)
    state.setdefault(RECORDING_MANIFEST, None)
    state.setdefault(RECORDING_MANIFEST_RECORDING_ID, None)
    state.setdefault(RECORDING_ACTIVE, False)
    state.setdefault(RECORDING_QUESTION, None)
    state.setdefault(ANSWER_STARTED_AT, None)
    state.setdefault(TRANSCRIPT, "")
    state.setdefault(TRANSCRIPT_QUESTION, None)
    state.setdefault(TRANSCRIPT_ERROR, None)
    state.setdefault(SUBMITTED_FINGERPRINT, None)
    state.setdefault(TTS_COUNTER, 0)
    state.setdefault(PENDING_MEDIA_IDS, [])
    state.setdefault(MEDIA_STATUS, "not_started")
    state.setdefault(MEDIA_ERROR, None)
    state.setdefault(AUDIO_ERROR, None)
    state.setdefault(VIDEO_ERROR, None)
    state.setdefault(RECORDER_LAST_FAILURE, None)
    initialize_recorder_lifecycle(state)
    state.setdefault(INTERVIEW_ERROR, None)
    get_recorder(state)
    get_video_recorder(state)


def get_recorder(state: MutableMapping) -> LiveAudioRecorder:
    candidate = state.get(RECORDER)
    if isinstance(candidate, LiveAudioRecorder):
        return candidate
    replacement = LiveAudioRecorder()
    state[RECORDER] = replacement
    return replacement


def get_video_recorder(state: MutableMapping) -> LiveVideoRecorder:
    candidate = state.get(VIDEO_RECORDER)
    if isinstance(candidate, LiveVideoRecorder):
        return candidate
    replacement = LiveVideoRecorder()
    state[VIDEO_RECORDER] = replacement
    return replacement


def reset_answer_state(state: MutableMapping) -> None:
    abandon_recorder_lifecycle(state, reason="The answer buffer was reset before finalization.")
    get_recorder(state).clear()
    get_video_recorder(state).clear()
    state[MEDIA_STATUS] = "idle"
    state[MEDIA_ERROR] = None
    state[AUDIO_ERROR] = None
    state[VIDEO_ERROR] = None
    state[RECORDING_ACTIVE] = False
    state[RECORDING_QUESTION] = None
    state[ANSWER_STARTED_AT] = None
    state[TRANSCRIPT_ERROR] = None
    reset_recorder_lifecycle(state)


def clear_transcript(state: MutableMapping) -> None:
    state[TRANSCRIPT] = ""
    state[TRANSCRIPT_QUESTION] = None
    state[TRANSCRIPT_ERROR] = None


def prepare_for_question(state: MutableMapping, question_number: int) -> None:
    current = state.get(RECORDING_QUESTION) or state.get(TRANSCRIPT_QUESTION)
    if current is None or int(current) == int(question_number):
        return
    reset_answer_state(state)
    clear_transcript(state)
    state[SUBMITTED_FINGERPRINT] = None


def ensure_capture_manifest(
    state: MutableMapping,
    *,
    interview_id: int | str | None,
    recording_id: int | str | None,
    policy_version: str = RECORDING_POLICY_VERSION,
) -> dict | None:
    if interview_id is None or recording_id is None:
        return None
    recording_key = str(recording_id)
    existing = safe_load_capture_manifest(state.get(RECORDING_MANIFEST))
    existing_key = state.get(RECORDING_MANIFEST_RECORDING_ID)
    if existing is not None and str(existing_key or "") == recording_key:
        return existing
    manifest = create_capture_manifest(
        recording_id=recording_key,
        interview_session_id=str(interview_id),
        policy_version=policy_version,
        started_monotonic_ns=monotonic_now_ns(),
        started_utc=utc_now_iso(),
    )
    state[RECORDING_MANIFEST] = manifest
    state[RECORDING_MANIFEST_RECORDING_ID] = recording_key
    return manifest


def get_capture_manifest(state: MutableMapping) -> dict | None:
    return safe_load_capture_manifest(state.get(RECORDING_MANIFEST))


def set_capture_manifest(state: MutableMapping, manifest: dict | None) -> dict | None:
    state[RECORDING_MANIFEST] = safe_load_capture_manifest(manifest) if manifest is not None else None
    return state[RECORDING_MANIFEST]


def update_capture_manifest_for_recorders(
    state: MutableMapping,
    *,
    audio_status: str | None = None,
    video_status: str | None = None,
    finalize: bool = False,
) -> dict | None:
    manifest = get_capture_manifest(state)
    if manifest is None:
        return None
    manifest = mark_manifest_started(manifest)
    audio_snapshot = get_recorder(state).snapshot()
    video_snapshot = get_video_recorder(state).snapshot()
    manifest = update_audio_manifest(manifest, audio_snapshot, status=audio_status)
    manifest = update_video_manifest(manifest, video_snapshot, status=video_status)
    if finalize:
        manifest = mark_manifest_finalized(manifest, ended_monotonic_ns=monotonic_now_ns(), ended_utc=utc_now_iso())
    lifecycle = recorder_lifecycle_snapshot(state)
    manifest.setdefault("diagnostics", {})["recorder_lifecycle"] = {
        "state": lifecycle.get("state"),
        "answer_id": lifecycle.get("answer_id"),
        "question_number": lifecycle.get("question_number"),
    }
    state[RECORDING_MANIFEST] = manifest
    return manifest


def start_recording(
    state: MutableMapping, question_number: int, *, record_video: bool = False
) -> str:
    lifecycle = recorder_lifecycle_snapshot(state)
    active_states = {"STARTING", "RECORDING", "STOPPING", "FINALIZING"}
    existing_id = current_answer_id(state)
    if (
        existing_id
        and lifecycle.get("state") in active_states
        and lifecycle.get("question_number") is not None
        and int(lifecycle["question_number"]) == int(question_number)
    ):
        return existing_id
    reset_answer_state(state)
    clear_transcript(state)
    interview_id = state.get(ACTIVE_INTERVIEW_ID)
    recording_id = state.get(RECORDING_ID)
    answer_id = begin_answer(
        state,
        question_number=int(question_number),
        interview_id=interview_id,
    )
    ensure_capture_manifest(state, interview_id=interview_id, recording_id=recording_id)
    recorder = get_recorder(state)
    video_recorder = get_video_recorder(state)
    recorder.set_enabled(True)
    video_recorder.set_enabled(bool(record_video))
    audio_snapshot = recorder.snapshot()
    video_snapshot = video_recorder.snapshot()
    audio_error = str(audio_snapshot.get("capture_error")) if audio_snapshot.get("capture_error") else None
    video_error = str(video_snapshot.get("error")) if video_snapshot.get("error") else None
    mark_recording(state, answer_id=answer_id, record_video=bool(record_video))
    set_stream_statuses(
        state,
        answer_id=answer_id,
        audio_status="failed" if audio_error else "capturing",
        video_status=("failed" if video_error else "capturing") if record_video else "not_started",
    )
    set_stream_errors(
        state,
        answer_id=answer_id,
        audio_error=audio_error,
        video_error=video_error if record_video else None,
    )
    state[RECORDING_ACTIVE] = True
    state[RECORDING_QUESTION] = int(question_number)
    state[ANSWER_STARTED_AT] = datetime.now(UTC).replace(tzinfo=None).isoformat()
    state[INTERVIEW_ERROR] = None
    update_capture_manifest_for_recorders(
        state,
        audio_status="failed" if audio_error else "capturing",
        video_status=("failed" if video_error else "capturing") if record_video else "not_started",
    )
    return answer_id


def stop_recording(state: MutableMapping) -> bool:
    answer_id = current_answer_id(state)
    changed = mark_stopping(state, answer_id=answer_id)
    get_recorder(state).set_enabled(False)
    get_video_recorder(state).set_enabled(False)
    state[RECORDING_ACTIVE] = False
    lifecycle = recorder_lifecycle_snapshot(state)
    update_capture_manifest_for_recorders(
        state,
        audio_status=lifecycle.get("audio_status") or "stopped",
        video_status=lifecycle.get("video_status") or "stopped",
    )
    return changed


def set_transcript(state: MutableMapping, question_number: int, text: str) -> None:
    state[TRANSCRIPT] = str(text or "").strip()
    state[TRANSCRIPT_QUESTION] = int(question_number)
    state[RECORDING_QUESTION] = int(question_number)
    state[TRANSCRIPT_ERROR] = None


def set_transcript_error(state: MutableMapping, message: str) -> None:
    state[TRANSCRIPT_ERROR] = str(message)


def recording_elapsed_seconds(state: MutableMapping) -> float:
    raw = state.get(ANSWER_STARTED_AT)
    if not raw:
        return 0.0
    try:
        started = datetime.fromisoformat(str(raw))
    except ValueError:
        return 0.0
    if started.tzinfo is not None:
        started = started.astimezone(UTC).replace(tzinfo=None)
    now = datetime.now(UTC).replace(tzinfo=None)
    return max(0.0, (now - started).total_seconds())


def remaining_answer_seconds(state: MutableMapping) -> int:
    return max(0, int(MAX_ANSWER_SECONDS - recording_elapsed_seconds(state)))


def recording_consent_granted(state: MutableMapping) -> bool:
    return bool(state.get(RECORDING_CONSENT, False)) and bool(state.get(RECORDING_CONSENT_AT))


def set_recording_consent(state: MutableMapping, granted: bool) -> None:
    state[RECORDING_CONSENT] = bool(granted)
    state[RECORDING_CONSENT_AT] = (
        datetime.now(UTC).replace(tzinfo=None).isoformat() if granted else None
    )


def set_media_status(state: MutableMapping, status: str, error: str | None = None) -> None:
    state[MEDIA_STATUS] = str(status)
    state[MEDIA_ERROR] = str(error) if error else None
    record_media_status(state, str(status), answer_id=current_answer_id(state))


def add_pending_media_id(state: MutableMapping, media_id: int) -> None:
    pending = list(state.get(PENDING_MEDIA_IDS) or [])
    if int(media_id) not in [int(item) for item in pending]:
        pending.append(int(media_id))
    state[PENDING_MEDIA_IDS] = pending


def answer_fingerprint(interview_id: int, question_number: int, text: str) -> str:
    payload = f"{int(interview_id)}|{int(question_number)}|{str(text or '').strip()}".encode()
    return hashlib.sha256(payload).hexdigest()


def can_submit_answer(
    state: MutableMapping, interview_id: int, question_number: int, text: str
) -> bool:
    return bool(str(text or "").strip()) and state.get(SUBMITTED_FINGERPRINT) != answer_fingerprint(
        interview_id, question_number, text
    )


def mark_answer_submitted(
    state: MutableMapping, interview_id: int, question_number: int, text: str
) -> None:
    state[SUBMITTED_FINGERPRINT] = answer_fingerprint(interview_id, question_number, text)
    record_media_status(state, str(state.get(MEDIA_STATUS) or "saved"))
    state[PENDING_MEDIA_IDS] = []
    reset_answer_state(state)
    clear_transcript(state)


def clear_interview_state(state: MutableMapping) -> None:
    reset_answer_state(state)
    state[PENDING_MEDIA_IDS] = []
    clear_transcript(state)
    state[ACTIVE_INTERVIEW_ID] = None
    state[REPORT_ID] = None
    state[INTERVIEW_CONSENT] = False
    state[RECORDING_CONSENT] = False
    state[RECORDING_CONSENT_AT] = None
    state[RECORDING_ID] = None
    state[RECORDING_MANIFEST] = None
    state[RECORDING_MANIFEST_RECORDING_ID] = None
    state[SUBMITTED_FINGERPRINT] = None
    state[TTS_COUNTER] = 0
    state[INTERVIEW_ERROR] = None


def get_recorder_lifecycle(state: MutableMapping) -> dict:
    return recorder_lifecycle_snapshot(state)


def report_recorder_failure(
    state: MutableMapping, message: str, *, answer_id: str | None = None
) -> None:
    detail = str(message)
    mark_recorder_failure(state, detail, answer_id=answer_id)
    state[RECORDING_ACTIVE] = False
    state[MEDIA_STATUS] = "failed"
    state[MEDIA_ERROR] = detail


def clear_capture_data(state: MutableMapping) -> None:
    get_recorder(state).clear()
    get_video_recorder(state).clear()
    state[RECORDING_ACTIVE] = False


def clear_capture_buffers(state: MutableMapping, *, reason: str | None = None) -> None:
    abandon_recorder_lifecycle(state, reason=reason)
    clear_capture_data(state)
    reset_recorder_lifecycle(state)
