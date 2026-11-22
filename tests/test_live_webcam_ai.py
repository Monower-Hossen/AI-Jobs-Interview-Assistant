from __future__ import annotations

import io
import struct
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import av
import numpy as np
import pytest
from services.job_interview_service import JobInterviewError
from services.live_interview_state import (
    RECORDING_ACTIVE,
    SUBMITTED_FINGERPRINT,
    TRANSCRIPT,
    TRANSCRIPT_QUESTION,
    answer_fingerprint,
    can_submit_answer,
    get_recorder,
    initialize_live_interview_state,
    mark_answer_submitted,
    prepare_for_question,
    remaining_answer_seconds,
    set_transcript,
    start_recording,
    stop_recording,
)
from services.live_interview_workflow import (
    LiveInterviewError,
    available_jobs,
    complete_live_interview,
    list_live_interviews,
    start_live_interview,
    submit_live_answer,
    transcribe_recorder,
)
from services.live_webcam_audio import (
    NORMALIZED_SAMPLE_RATE,
    AudioChunk,
    LiveAudioRecorder,
    frame_to_chunk,
)
from services.live_webcam_capture import RecordingAudioProcessor
from services.live_webcam_events import BoundedAudioEventQueue
from services.live_webcam_state import (
    CAMERA,
    CONSENT_CAMERA,
    CONSENT_MICROPHONE,
    MICROPHONE,
    initialize_live_webcam_state,
    record_connection_failure,
    set_stream_requested,
    stop_all_streams,
    stream_error,
    stream_requested,
)


def _chunk(*, sample_rate: int = 16_000, channels: int = 1, samples: int = 8) -> AudioChunk:
    values = np.full(channels * samples, 1000, dtype="<i2")
    return AudioChunk(
        pcm=values.tobytes(), sample_rate=sample_rate, channels=channels, samples=samples
    )


def _frame(*, samples: int = 960, channels: int = 2, sample_rate: int = 48_000):
    if channels == 2:
        array = np.arange(samples * 2, dtype="<i2").reshape(1, samples * 2)
        layout = "stereo"
    else:
        array = np.arange(samples, dtype="<i2").reshape(1, samples)
        layout = "mono"
    frame = av.AudioFrame.from_ndarray(array, format="s16", layout=layout)
    frame.sample_rate = sample_rate
    return frame


class _FakeTranscriber:
    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []
        self.failure: Exception | None = None

    def transcribe_from_bytes(self, data: bytes, **kwargs):
        self.calls.append({"data": data, "kwargs": kwargs})
        if self.failure:
            raise self.failure
        return SimpleNamespace(text=self.text)


class _BrokenFrame:
    sample_rate = 48_000

    def to_ndarray(self):
        return np.zeros((2, 2, 2), dtype="<i2")


def test_frame_to_chunk_handles_packed_stereo_metadata():
    frame = _frame(samples=960, channels=2)

    chunk = frame_to_chunk(frame)

    assert chunk.channels == 2
    assert chunk.samples == 960
    assert len(chunk.pcm) == 960 * 2 * 2
    assert struct.unpack("<4h", chunk.pcm[:8]) == (0, 960, 1, 961)


def test_frame_to_chunk_handles_planar_audio():
    array = np.arange(16, dtype="<i2").reshape(2, 8)
    frame = av.AudioFrame.from_ndarray(array, format="s16p", layout="stereo")
    frame.sample_rate = 48_000

    chunk = frame_to_chunk(frame)

    assert chunk.channels == 2
    assert chunk.samples == 8
    assert struct.unpack("<4h", chunk.pcm[:8]) == (0, 8, 1, 9)


def test_recorder_normalizes_to_16k_mono_and_flushes_tail():
    recorder = LiveAudioRecorder()
    recorder.set_enabled(True)
    recorder.append_frame(_frame(samples=960, channels=2))

    before_flush = recorder.snapshot()
    wav_bytes = recorder.to_wav_bytes()
    after_flush = recorder.snapshot()

    assert before_flush["flushed"] is False
    assert after_flush["flushed"] is True
    assert after_flush["sample_rate"] == NORMALIZED_SAMPLE_RATE
    assert after_flush["channels"] == 1
    assert after_flush["duration_seconds"] > 0
    assert recorder.to_wav_bytes() == wav_bytes
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        assert wav_file.getframerate() == NORMALIZED_SAMPLE_RATE
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getnframes() > 0


def test_recorder_caps_duration_and_preserves_chunk_cap():
    recorder = LiveAudioRecorder(max_seconds=1, max_bytes=1_000_000, max_chunks=100)
    recorder.set_enabled(True)
    recorder.append_chunk(_chunk(sample_rate=10, samples=6))
    recorder.append_chunk(_chunk(sample_rate=10, samples=6))

    snapshot = recorder.snapshot()
    assert snapshot["duration_seconds"] == 1.0
    assert snapshot["chunk_count"] == 2
    assert snapshot["overflowed"] is True
    assert snapshot["byte_count"] == 20
    with wave.open(io.BytesIO(recorder.to_wav_bytes()), "rb") as wav_file:
        assert wav_file.getnframes() == 10

    recorder.clear()
    assert recorder.snapshot()["chunk_count"] == 0
    assert recorder.to_wav_bytes() == b""


def test_recorder_rejects_invalid_frame_without_blocking_later_audio():
    recorder = LiveAudioRecorder()
    recorder.set_enabled(True)

    recorder.append_frame(_BrokenFrame())
    assert recorder.snapshot()["capture_error"]
    assert recorder.snapshot()["byte_count"] == 0

    recorder.append_frame(_frame(samples=960, channels=1))
    assert recorder.snapshot()["byte_count"] > 0


def test_recorder_is_thread_safe_for_chunk_appends():
    recorder = LiveAudioRecorder()
    recorder.set_enabled(True)

    def append_many() -> None:
        for _ in range(50):
            recorder.append_chunk(_chunk())

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: append_many(), range(4)))

    snapshot = recorder.snapshot()
    assert snapshot["chunk_count"] == 200
    assert snapshot["byte_count"] == 200 * 16
    assert snapshot["dropped_chunks"] == 0


def test_recording_processor_stops_capture_on_stream_end():
    recorder = LiveAudioRecorder()
    event_queue = BoundedAudioEventQueue(maxsize=4)
    processor = RecordingAudioProcessor(event_queue, recorder)
    frame = _frame(samples=960, channels=2)

    processor.recv(frame)
    assert recorder.snapshot()["byte_count"] == 0
    assert len(event_queue.drain()) == 1

    recorder.set_enabled(True)
    assert processor.recv(frame) is frame
    assert recorder.snapshot()["byte_count"] > 0
    assert len(event_queue.drain()) == 1

    processor.on_ended()
    assert recorder.enabled is False
    assert event_queue.drain() == []


def test_live_interview_state_lifecycle_and_duplicate_protection():
    state = {}
    initialize_live_interview_state(state)

    start_recording(state, question_number=3)
    assert state[RECORDING_ACTIVE] is True
    assert remaining_answer_seconds(state) > 0

    stop_recording(state)
    set_transcript(state, 3, "  My answer  ")
    assert state[TRANSCRIPT] == "My answer"
    assert can_submit_answer(state, 42, 3, "My answer")

    mark_answer_submitted(state, 42, 3, "My answer")
    assert state[TRANSCRIPT] == ""
    assert not can_submit_answer(state, 42, 3, "My answer")
    assert state[SUBMITTED_FINGERPRINT] == answer_fingerprint(42, 3, "My answer")


def test_prepare_for_question_clears_previous_answer():
    state = {}
    initialize_live_interview_state(state)
    state[RECORDING_ACTIVE] = True
    state[TRANSCRIPT] = "old answer"
    state[TRANSCRIPT_QUESTION] = 1
    get_recorder(state).set_enabled(True)

    prepare_for_question(state, 2)

    assert state[RECORDING_ACTIVE] is False
    assert state[TRANSCRIPT] == ""
    assert state[SUBMITTED_FINGERPRINT] is None
    assert get_recorder(state).enabled is False


def test_transcribe_recorder_uses_local_service_and_clears_buffer():
    recorder = LiveAudioRecorder()
    recorder.set_enabled(True)
    recorder.append_frame(_frame(samples=960, channels=2))
    transcriber = _FakeTranscriber("  transcribed answer  ")

    text = transcribe_recorder(recorder, transcriber=transcriber)

    assert text == "transcribed answer"
    assert transcriber.calls[0]["kwargs"] == {"format": "wav"}
    assert transcriber.calls[0]["data"].startswith(b"RIFF")
    assert recorder.snapshot()["byte_count"] == 0


def test_transcribe_recorder_rejects_empty_silent_and_consent_withdrawn():
    recorder = LiveAudioRecorder()
    with pytest.raises(LiveInterviewError, match="No audio"):
        transcribe_recorder(recorder, transcriber=_FakeTranscriber("anything"))

    recorder.set_enabled(True)
    recorder.append_frame(_frame(samples=960, channels=1))
    with pytest.raises(LiveInterviewError, match="No speech"):
        transcribe_recorder(recorder, transcriber=_FakeTranscriber("   "))

    recorder.set_enabled(True)
    recorder.append_frame(_frame(samples=960, channels=1))
    with pytest.raises(LiveInterviewError, match="Consent"):
        transcribe_recorder(
            recorder, transcriber=_FakeTranscriber("answer"), consent_given=False
        )
    assert recorder.snapshot()["byte_count"] == 0


def test_transcribe_recorder_reports_transcription_failure():
    recorder = LiveAudioRecorder()
    recorder.set_enabled(True)
    recorder.append_frame(_frame(samples=960, channels=1))
    transcriber = _FakeTranscriber("unused")
    transcriber.failure = RuntimeError("model unavailable")

    with pytest.raises(LiveInterviewError, match="Could not transcribe"):
        transcribe_recorder(recorder, transcriber=transcriber)
    assert recorder.snapshot()["byte_count"] == 0


def test_start_live_interview_requires_all_consents():
    with pytest.raises(LiveInterviewError, match="consent"):
        start_live_interview(
            user_id=1,
            session_token="token",
            job_id=1,
            difficulty="medium",
            duration_minutes=20,
            camera_consent=True,
            microphone_consent=False,
            interview_consent=True,
        )


def test_workflow_wraps_service_errors(monkeypatch):
    import services.live_interview_workflow as workflow

    def _raise(**kwargs):
        raise JobInterviewError("stale session")

    monkeypatch.setattr(workflow, "start_job_interview", _raise)
    with pytest.raises(LiveInterviewError, match="stale session"):
        start_live_interview(
            user_id=1,
            session_token="token",
            job_id=1,
            difficulty="medium",
            duration_minutes=20,
            camera_consent=True,
            microphone_consent=True,
            interview_consent=True,
        )


def test_submit_live_answer_rejects_blank_and_withdrawn_consent():
    with pytest.raises(LiveInterviewError, match="provide an answer"):
        submit_live_answer(
            interview_id=1,
            question_number=1,
            transcript="   ",
            user_id=1,
            session_token="token",
            answer_started_at=None,
        )
    with pytest.raises(LiveInterviewError, match="Consent"):
        submit_live_answer(
            interview_id=1,
            question_number=1,
            transcript="answer",
            user_id=1,
            session_token="token",
            answer_started_at=None,
            consent_given=False,
        )


def test_evaluation_failure_is_reported_without_saving(monkeypatch):
    import services.live_interview_workflow as workflow

    def _raise(**kwargs):
        raise RuntimeError("evaluation provider failed")

    monkeypatch.setattr(workflow, "submit_job_answer", _raise)
    with pytest.raises(LiveInterviewError, match="No answer was saved"):
        submit_live_answer(
            interview_id=1,
            question_number=1,
            transcript="answer",
            user_id=1,
            session_token="token",
            answer_started_at=None,
        )


def test_complete_live_interview_requires_consent():
    with pytest.raises(LiveInterviewError, match="Consent"):
        complete_live_interview(
            interview_id=1, user_id=1, session_token="token", consent_given=False
        )


def test_available_jobs_and_live_listing_delegate(monkeypatch):
    import services.live_interview_workflow as workflow

    sentinel = object()
    monkeypatch.setattr(workflow, "list_jobs", lambda: sentinel)
    assert available_jobs() is sentinel

    rows = [{"id": 7, "status": "in_progress"}]
    monkeypatch.setattr(workflow, "list_job_interviews", lambda **kwargs: rows)
    assert list_live_interviews(user_id=1, session_token="token") == rows


def test_stop_all_streams_clears_requested_flags_and_connection_error():
    state = {}
    initialize_live_webcam_state(state)
    state[CONSENT_CAMERA] = True
    state[CONSENT_MICROPHONE] = True
    set_stream_requested(state, CAMERA, True)
    set_stream_requested(state, MICROPHONE, True)

    stop_all_streams(state)

    assert not stream_requested(state, CAMERA)
    assert not stream_requested(state, MICROPHONE)
    assert stream_error(state, CAMERA) is None
    assert stream_error(state, MICROPHONE) is None


def test_connection_failure_recorded_only_after_grace_period():
    state = {}
    initialize_live_webcam_state(state)
    set_stream_requested(state, CAMERA, True)
    kwargs = {"playing": False, "signalling": False, "grace_seconds": 0.0}

    assert record_connection_failure(state, CAMERA, **kwargs) is True
    assert stream_error(state, CAMERA) == "permission or device unavailable"
    assert record_connection_failure(state, CAMERA, **kwargs) is False

    set_stream_requested(state, MICROPHONE, True)
    state["live_webcam_microphone_requested_at"] = datetime.now(UTC).replace(
        tzinfo=None
    ).isoformat()
    assert (
        record_connection_failure(
            state,
            MICROPHONE,
            playing=False,
            signalling=False,
            grace_seconds=600,
        )
        is False
    )
