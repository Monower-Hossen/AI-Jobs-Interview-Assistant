from __future__ import annotations

import hashlib
from collections.abc import MutableMapping
from datetime import UTC, datetime

from services.job_interview_constants import MAX_ANSWER_SECONDS
from services.live_webcam_audio import LiveAudioRecorder
from services.live_webcam_video import LiveVideoRecorder

RECORDING_CONSENT = "live_webcam_recording_consent"
RECORDING_CONSENT_AT = "live_webcam_recording_consent_at"
RECORDING_POLICY_VERSION = "live-recording-v1"
RECORDING_ID = "live_webcam_recording_id"
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
    RECORDER,
    VIDEO_RECORDER,
    PENDING_MEDIA_IDS,
    MEDIA_STATUS,
    MEDIA_ERROR,
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
    get_recorder(state).clear()
    get_video_recorder(state).clear()
    state[PENDING_MEDIA_IDS] = []
    state[MEDIA_STATUS] = "idle"
    state[MEDIA_ERROR] = None
    state[RECORDING_ACTIVE] = False
    state[RECORDING_QUESTION] = None
    state[ANSWER_STARTED_AT] = None
    state[TRANSCRIPT_ERROR] = None


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


def start_recording(
    state: MutableMapping, question_number: int, *, record_video: bool = False
) -> None:
    reset_answer_state(state)
    clear_transcript(state)
    recorder = get_recorder(state)
    recorder.set_enabled(True)
    get_video_recorder(state).set_enabled(bool(record_video))
    state[RECORDING_ACTIVE] = True
    state[RECORDING_QUESTION] = int(question_number)
    state[ANSWER_STARTED_AT] = datetime.now(UTC).replace(tzinfo=None).isoformat()
    state[INTERVIEW_ERROR] = None


def stop_recording(state: MutableMapping) -> None:
    get_recorder(state).set_enabled(False)
    get_video_recorder(state).set_enabled(False)
    state[RECORDING_ACTIVE] = False


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
    state[PENDING_MEDIA_IDS] = []
    reset_answer_state(state)
    clear_transcript(state)


def clear_interview_state(state: MutableMapping) -> None:
    reset_answer_state(state)
    clear_transcript(state)
    state[ACTIVE_INTERVIEW_ID] = None
    state[REPORT_ID] = None
    state[INTERVIEW_CONSENT] = False
    state[RECORDING_CONSENT] = False
    state[RECORDING_CONSENT_AT] = None
    state[RECORDING_ID] = None
    state[SUBMITTED_FINGERPRINT] = None
    state[TTS_COUNTER] = 0
    state[INTERVIEW_ERROR] = None
