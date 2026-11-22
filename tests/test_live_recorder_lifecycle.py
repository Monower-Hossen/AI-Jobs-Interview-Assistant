from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from services import live_recorder_lifecycle as lifecycle
from services import live_webcam_video as live_video
from services.live_interview_state import (
    ACTIVE_INTERVIEW_ID,
    AUDIO_ERROR,
    MEDIA_ERROR,
    MEDIA_STATUS,
    PENDING_MEDIA_IDS,
    RECORDER_LAST_FAILURE,
    RECORDING_ACTIVE,
    RECORDING_ID,
    TRANSCRIPT,
    TRANSCRIPT_ERROR,
    VIDEO_ERROR,
    add_pending_media_id,
    clear_capture_data,
    get_recorder,
    get_video_recorder,
    initialize_live_interview_state,
    report_recorder_failure,
    set_media_status,
    start_recording,
    stop_recording,
)
from services.live_interview_workflow import LiveInterviewError
from services.live_recording_manifest import (
    CAPTURE_MANIFEST_KEY,
    create_capture_manifest,
    safe_load_capture_manifest,
)
from services.live_webcam_capture import RecordingAudioProcessor, RecordingVideoProcessor
from services.live_webcam_events import BoundedAudioEventQueue

from frontend.user import live_webcam_interview as live_page


class _FakeAudioRecorder:
    def __init__(self, capture_error: str | None = None) -> None:
        self.capture_error = capture_error
        self.cleared = False

    def snapshot(self) -> dict:
        return {
            "capture_error": self.capture_error,
            "sample_rate": 16000,
            "channels": 1,
            "sample_format": "s16",
            "chunk_count": 1,
            "dropped_chunks": 0,
        }

    def clear(self) -> None:
        self.cleared = True


class _FakeVideoRecorder:
    def __init__(self, data: bytes = b"video", error: str | None = None) -> None:
        self.data = data
        self.error = error
        self.cleared = False
        self.finalize_calls = 0

    def finalize(self) -> bytes:
        self.finalize_calls += 1
        if self.error:
            raise LiveInterviewError(self.error)
        return self.data

    def snapshot(self) -> dict:
        return {
            "error": self.error,
            "codec": "h264",
            "container": "mp4",
            "fps": 30,
            "width": 64,
            "height": 64,
            "frames_encoded": 4,
            "dropped_frames": 0,
            "timestamp_adjustments": 0,
            "overflowed": False,
            "duration_seconds": 0.133,
        }

    def clear(self) -> None:
        self.cleared = True


class _GenerationAudioRecorder:
    def __init__(self) -> None:
        self.enabled = True
        self.generation = 1
        self.frames: list[object] = []
        self.error: str | None = None

    def append_frame(self, frame: object) -> None:
        self.frames.append(frame)

    def set_capture_error(self, message: str) -> None:
        self.error = message


class _GenerationVideoRecorder:
    def __init__(self) -> None:
        self.enabled = True
        self.generation = 1
        self.calls: list[int] = []
        self.error: str | None = None

    def enqueue(self, frame: object, *, generation: int) -> None:
        self.calls.append(generation)

    def set_error(self, message: str) -> None:
        self.error = message


def _state() -> dict:
    state: dict = {}
    initialize_live_interview_state(state)
    state[ACTIVE_INTERVIEW_ID] = 42
    state[RECORDING_ID] = 7
    return state


def _active_state(question: int = 1) -> tuple[dict, str]:
    state = _state()
    answer_id = start_recording(state, question, record_video=False)
    return state, answer_id


def _install_st(monkeypatch, state: dict) -> None:
    monkeypatch.setattr(
        live_page,
        "st",
        SimpleNamespace(session_state=state, rerun=lambda: None),
    )


def _install_finalize_dependencies(
    monkeypatch,
    state: dict,
    *,
    audio_bytes: bytes = b"audio",
    video_bytes: bytes = b"video",
    audio_error: str | None = None,
    video_error: str | None = None,
    consent: bool = True,
    save_audio_ok: bool = True,
    save_video_ok: bool = True,
) -> tuple[list[dict], _FakeAudioRecorder, _FakeVideoRecorder]:
    audio = _FakeAudioRecorder(audio_error)
    video = _FakeVideoRecorder(video_bytes, video_error)
    saves: list[dict] = []
    next_id = [100]

    def _save(**kwargs):
        saves.append(kwargs)
        if kwargs["media_type"] == "audio" and not save_audio_ok:
            raise LiveInterviewError("audio save failed")
        if kwargs["media_type"] == "video" and not save_video_ok:
            raise LiveInterviewError("video save failed")
        next_id[0] += 1
        return next_id[0]

    _install_st(monkeypatch, state)
    monkeypatch.setattr(live_page, "get_recorder", lambda _: audio)
    monkeypatch.setattr(live_page, "get_video_recorder", lambda _: video)
    monkeypatch.setattr(live_page, "recording_consent_granted", lambda _: consent)
    monkeypatch.setattr(live_page, "ensure_capture_manifest", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        live_page, "update_capture_manifest_for_recorders", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(live_page, "capture_audio_bytes", lambda _: audio_bytes)
    monkeypatch.setattr(live_page, "transcribe_audio_bytes", lambda _: "transcript")
    monkeypatch.setattr(live_page, "save_live_media", _save)
    return saves, audio, video


def _finalize(state: dict, *, answer_id: str | None = None):
    return live_page._finalize_answer_media(
        interview_id=42,
        question_number=1,
        user_id=3,
        session_token="token",
        answer_id=answer_id,
    )


def test_valid_and_invalid_lifecycle_transitions() -> None:
    state = _state()
    assert lifecycle.transition_recorder_lifecycle(state, lifecycle.STARTING)
    assert lifecycle.transition_recorder_lifecycle(state, lifecycle.RECORDING)
    assert not lifecycle.transition_recorder_lifecycle(state, lifecycle.IDLE)
    snapshot = lifecycle.recorder_lifecycle_snapshot(state)
    assert snapshot["state"] == lifecycle.RECORDING
    assert "Invalid recorder lifecycle transition" in snapshot["state_error"]


def test_start_called_twice_is_idempotent() -> None:
    state = _state()
    first = start_recording(state, 3, record_video=False)
    second = start_recording(state, 3, record_video=False)
    snapshot = lifecycle.recorder_lifecycle_snapshot(state)
    assert first == second
    assert snapshot["state"] == lifecycle.RECORDING
    assert snapshot["generation"] == 1


def test_stop_called_twice_is_idempotent() -> None:
    state, answer_id = _active_state()
    assert stop_recording(state) is True
    assert stop_recording(state) is False
    snapshot = lifecycle.recorder_lifecycle_snapshot(state)
    assert snapshot["answer_id"] == answer_id
    assert snapshot["state"] == lifecycle.STOPPING


def test_finalize_called_twice_does_not_save_twice(monkeypatch) -> None:
    state, answer_id = _active_state()
    stop_recording(state)
    saves, audio, video = _install_finalize_dependencies(monkeypatch, state)

    first = _finalize(state, answer_id=answer_id)
    second = _finalize(state, answer_id=answer_id)

    assert first == ("transcript", None)
    assert second == first
    assert len(saves) == 2
    assert video.finalize_calls == 1
    assert state[MEDIA_STATUS] == "saved"
    assert audio.cleared and video.cleared


def test_concurrent_finalize_calls_claim_once(monkeypatch) -> None:
    state, answer_id = _active_state()
    stop_recording(state)
    saves, _, video = _install_finalize_dependencies(monkeypatch, state)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _finalize(state, answer_id=answer_id), range(2)))

    assert results.count(("transcript", None)) >= 1
    assert len(saves) == 2
    assert video.finalize_calls == 1


@pytest.mark.parametrize(
    ("audio_error", "video_error", "expected_status"),
    [
        ("audio device failed", None, "partial"),
        (None, "video encoder failed", "partial"),
        ("audio device failed", "video encoder failed", "failed"),
    ],
)
def test_audio_and_video_errors_are_propagated_separately(
    monkeypatch, audio_error, video_error, expected_status
) -> None:
    state, answer_id = _active_state()
    stop_recording(state)
    audio_bytes = b"" if audio_error else b"audio"
    video_bytes = b"" if video_error else b"video"
    _install_finalize_dependencies(
        monkeypatch,
        state,
        audio_bytes=audio_bytes,
        video_bytes=video_bytes,
        audio_error=audio_error,
        video_error=video_error,
    )

    transcript, error = _finalize(state, answer_id=answer_id)

    lifecycle_snapshot = lifecycle.recorder_lifecycle_snapshot(state)
    assert (lifecycle_snapshot["audio_error"] or "") == (audio_error or "")
    assert (lifecycle_snapshot["video_error"] or "") == (video_error or "")
    assert bool(state[AUDIO_ERROR]) == bool(audio_error)
    assert bool(state[VIDEO_ERROR]) == bool(video_error)
    assert state[MEDIA_STATUS] == expected_status
    assert transcript == (None if audio_error else "transcript")
    if audio_error:
        assert error and "audio device failed" in error
    else:
        assert error is None or "video encoder failed" not in error


def test_audio_failure_while_video_continues_saves_video(monkeypatch) -> None:
    state, answer_id = _active_state()
    stop_recording(state)
    saves, _, _ = _install_finalize_dependencies(
        monkeypatch,
        state,
        audio_bytes=b"",
        video_bytes=b"video",
        audio_error="microphone disconnected",
    )

    _finalize(state, answer_id=answer_id)

    assert [item["media_type"] for item in saves] == ["video"]
    assert state[MEDIA_STATUS] == "partial"


def test_video_failure_while_audio_continues_saves_audio(monkeypatch) -> None:
    state, answer_id = _active_state()
    stop_recording(state)
    saves, _, _ = _install_finalize_dependencies(
        monkeypatch,
        state,
        audio_bytes=b"audio",
        video_bytes=b"",
        video_error="encoder stopped",
    )

    _finalize(state, answer_id=answer_id)

    assert [item["media_type"] for item in saves] == ["audio"]
    assert state[MEDIA_STATUS] == "partial"


def test_both_streams_failing_saves_nothing(monkeypatch) -> None:
    state, answer_id = _active_state()
    stop_recording(state)
    saves, _, _ = _install_finalize_dependencies(
        monkeypatch,
        state,
        audio_bytes=b"",
        video_bytes=b"",
        audio_error="microphone failed",
        video_error="camera failed",
    )

    _finalize(state, answer_id=answer_id)

    assert saves == []
    assert state[MEDIA_STATUS] == "failed"
    assert state[PENDING_MEDIA_IDS] == []


@pytest.mark.parametrize(
    ("media_status", "expected_recording_status"),
    [
        ("saved", "completed"),
        ("partial", "interrupted"),
        ("failed", "failed"),
    ],
)
def test_completion_uses_actual_media_outcome(
    monkeypatch, media_status, expected_recording_status
) -> None:
    state, _ = _active_state()
    _install_st(monkeypatch, state)
    monkeypatch.setattr(live_page, "stop_all_streams", lambda *_args, **_kwargs: None)
    completed: list[bool] = []
    recording_statuses: list[str] = []

    def _finalize_stub(**_kwargs):
        set_media_status(state, media_status, "detail")
        return "transcript", None

    monkeypatch.setattr(live_page, "_finalize_answer_media", _finalize_stub)
    monkeypatch.setattr(
        live_page,
        "finish_live_recording",
        lambda **kwargs: recording_statuses.append(kwargs["status"]),
    )
    monkeypatch.setattr(
        live_page,
        "complete_live_interview",
        lambda **kwargs: completed.append(True) or {"report": True},
    )

    live_page._finish_interview(42, 3, "token")

    assert completed == [True]
    assert recording_statuses == [expected_recording_status]
    assert state[RECORDING_ACTIVE] is False


def test_completion_with_no_media_consent_marks_recording_interrupted(monkeypatch) -> None:
    state, _ = _active_state()
    _install_st(monkeypatch, state)
    monkeypatch.setattr(live_page, "stop_all_streams", lambda *_args, **_kwargs: None)
    statuses: list[str] = []

    def _finalize_stub(**_kwargs):
        set_media_status(state, "not_saved", "No consent")
        return None, None

    monkeypatch.setattr(live_page, "_finalize_answer_media", _finalize_stub)
    monkeypatch.setattr(
        live_page,
        "finish_live_recording",
        lambda **kwargs: statuses.append(kwargs["status"]),
    )
    monkeypatch.setattr(live_page, "complete_live_interview", lambda **kwargs: {})

    live_page._finish_interview(42, 3, "token")

    assert statuses == ["interrupted"]


def test_session_error_during_active_recording_is_reported_not_silently_discarded() -> None:
    state, answer_id = _active_state()
    add_pending_media_id(state, 55)

    report_recorder_failure(state, "session state unavailable", answer_id=answer_id)
    clear_capture_data(state)

    assert lifecycle.recorder_lifecycle_snapshot(state)["state"] == lifecycle.FAILED
    assert state[RECORDER_LAST_FAILURE] == "session state unavailable"
    assert state[MEDIA_ERROR] == "session state unavailable"
    assert state[MEDIA_STATUS] == "failed"
    assert state[PENDING_MEDIA_IDS] == [55]
    assert get_recorder(state).snapshot()["byte_count"] == 0
    assert get_video_recorder(state).snapshot()["frames_encoded"] == 0


@pytest.mark.parametrize("delete_media", [True, False])
def test_abandonment_honors_delete_policy_without_persisting_active_answer(
    monkeypatch, delete_media
) -> None:
    state, _ = _active_state()
    state[live_page.DELETE_MEDIA_ON_ABANDON] = delete_media
    add_pending_media_id(state, 55)
    _install_st(monkeypatch, state)
    monkeypatch.setattr(live_page, "stop_all_streams", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(live_page, "go_to", lambda _page: None)
    deleted: list[int] = []
    finished: list[str] = []
    abandoned: list[int] = []

    def _unexpected_finalize(**_kwargs):
        raise AssertionError("abandonment must not persist active answer media")

    monkeypatch.setattr(live_page, "_finalize_answer_media", _unexpected_finalize)
    monkeypatch.setattr(
        live_page,
        "delete_live_recording",
        lambda recording_id, **_kwargs: deleted.append(int(recording_id)) or {},
    )
    monkeypatch.setattr(
        live_page,
        "finish_live_recording",
        lambda **kwargs: finished.append(kwargs["status"]),
    )
    monkeypatch.setattr(
        live_page,
        "abandon_live_interview",
        lambda **kwargs: abandoned.append(int(kwargs["interview_id"])),
    )

    live_page._perform_abandon(42, 3, "token")

    assert abandoned == [42]
    assert deleted == ([7] if delete_media else [])
    assert finished == ([] if delete_media else ["interrupted"])
    if delete_media:
        assert state[PENDING_MEDIA_IDS] == []
    assert state[RECORDING_ACTIVE] is False
    assert state[TRANSCRIPT] == ""


def test_failed_answer_does_not_block_new_answer() -> None:
    state, first = _active_state(1)
    add_pending_media_id(state, 77)
    set_media_status(state, "failed", "encoder failed")

    second = start_recording(state, 2, record_video=False)

    assert second != first
    assert state[PENDING_MEDIA_IDS] == [77]
    assert state[MEDIA_ERROR] is None
    assert state[MEDIA_STATUS] == "idle"
    snapshot = lifecycle.recorder_lifecycle_snapshot(state)
    assert snapshot["state"] == lifecycle.RECORDING
    assert snapshot["question_number"] == 2


def test_stale_audio_and_video_callbacks_are_ignored() -> None:
    audio_recorder = _GenerationAudioRecorder()
    video_recorder = _GenerationVideoRecorder()
    audio_processor = RecordingAudioProcessor(
        BoundedAudioEventQueue(maxsize=4), audio_recorder
    )
    video_processor = RecordingVideoProcessor(video_recorder)
    frame = object()

    audio_processor.recv(frame)
    video_processor.recv(frame)
    audio_recorder.generation = 2
    video_recorder.generation = 2
    audio_processor.recv(frame)
    video_processor.recv(frame)

    assert len(audio_recorder.frames) == 1
    assert video_recorder.calls == [1]
    assert audio_recorder.error is None
    assert video_recorder.error is None


def test_duplicate_transcription_and_persistence_claims_are_rejected() -> None:
    state, _answer_id = _active_state()
    assert lifecycle.claim_answer_operation(state, lifecycle.OPERATION_PERSIST)
    assert not lifecycle.claim_answer_operation(state, lifecycle.OPERATION_PERSIST)
    lifecycle.complete_answer_operation(
        state, lifecycle.OPERATION_PERSIST, result={"saved": [11]}
    )
    assert lifecycle.operation_result(state, lifecycle.OPERATION_PERSIST) == {"saved": [11]}
    assert lifecycle.claim_answer_operation(state, lifecycle.OPERATION_TRANSCRIBE)
    assert not lifecycle.claim_answer_operation(state, lifecycle.OPERATION_TRANSCRIBE)


def test_phase331_video_finalize_is_idempotent_and_releases_lease(monkeypatch) -> None:
    ended: list[str] = []

    monkeypatch.setattr(live_video, "try_begin_heavy_task", lambda _task: (True, None))
    monkeypatch.setattr(live_video, "end_heavy_task", ended.append)
    recorder = live_video.LiveVideoRecorder()
    recorder.set_enabled(True)

    first = recorder.finalize()
    second = recorder.finalize()
    recorder.clear()

    assert first == b""
    assert second == b""
    assert ended == ["video_processing"]
    assert recorder.snapshot()["finalized"] is False


def test_capture_manifest_schema_stays_version_one_with_lifecycle_metadata() -> None:
    state, _ = _active_state()
    manifest = create_capture_manifest(recording_id=7, interview_session_id=42)
    state["live_webcam_recording_manifest"] = manifest
    state["live_webcam_recording_manifest_recording_id"] = "7"

    from services.live_interview_state import update_capture_manifest_for_recorders

    updated = update_capture_manifest_for_recorders(state, finalize=True)

    assert updated is not None
    assert updated["schema_version"] == 1
    assert updated["synchronization"]["guaranteed_sync"] is False
    assert updated["diagnostics"]["recorder_lifecycle"]["state"] == lifecycle.RECORDING
    assert safe_load_capture_manifest(manifest) is not None
    assert CAPTURE_MANIFEST_KEY in {"capture_manifest"}


def test_invalid_finalize_for_stale_answer_does_not_run(monkeypatch) -> None:
    state, answer_id = _active_state()
    stop_recording(state)
    saves, _, video = _install_finalize_dependencies(monkeypatch, state)

    transcript, error = _finalize(state, answer_id=f"stale-{answer_id}")

    assert transcript is None
    assert error and "no longer active" in error
    assert saves == []
    assert video.finalize_calls == 0
    assert state[TRANSCRIPT_ERROR] is None
