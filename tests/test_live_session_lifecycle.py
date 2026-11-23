from __future__ import annotations

from types import SimpleNamespace

import pytest
from services import live_session_lifecycle as session
from services import live_webcam_video as live_video
from services.live_interview_state import (
    ACTIVE_INTERVIEW_ID,
    MEDIA_STATUS,
    PENDING_MEDIA_IDS,
    RECORDING_ACTIVE,
    RECORDING_ID,
    VIDEO_RECORDER,
    add_pending_media_id,
    get_recorder,
    get_video_recorder,
    initialize_live_interview_state,
    start_recording,
    stop_recording,
)
from services.live_interview_workflow import LiveInterviewError
from services.live_recorder_lifecycle import current_answer_id, recorder_lifecycle_snapshot
from services.live_webcam_capture import RecordingAudioProcessor
from services.live_webcam_events import BoundedAudioEventQueue
from services.live_webcam_state import CAMERA, MICROPHONE, initialize_live_webcam_state

from frontend.shared import auth_ui, live_session_cleanup, navigation
from frontend.shared import components as shared_components
from frontend.user import live_webcam_interview as live_page


def _fake_acquire_resource_lease(*args, **kwargs):
    return SimpleNamespace(lease_id="test-lease"), None


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


class _GenerationRecorder:
    def __init__(self) -> None:
        self.enabled = True
        self.generation = 1
        self.frames: list[object] = []
        self.events = 0
        self.error: str | None = None

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def append_frame(self, frame: object) -> None:
        self.frames.append(frame)

    def enqueue(self, frame: object, *, generation: int) -> None:
        self.frames.append((generation, frame))

    def set_capture_error(self, message: str) -> None:
        self.error = message

    def set_error(self, message: str) -> None:
        self.error = message


def _state(user_id: int = 3, token: str = "token") -> tuple[dict, session.LiveSessionOwner]:
    state: dict = {}
    initialize_live_webcam_state(state)
    initialize_live_interview_state(state)
    state[ACTIVE_INTERVIEW_ID] = 42
    state[RECORDING_ID] = 7
    binding = session.bind_live_session(state, user_id=user_id, session_token=token)
    return state, binding.current


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
    save_audio_ok: bool = True,
) -> tuple[list[dict], _FakeAudioRecorder, _FakeVideoRecorder]:
    audio = _FakeAudioRecorder(audio_error)
    video = _FakeVideoRecorder(video_bytes, video_error)
    saves: list[dict] = []
    next_id = [100]

    def _save(**kwargs):
        saves.append(kwargs)
        if kwargs["media_type"] == "audio" and not save_audio_ok:
            raise LiveInterviewError("audio save failed")
        next_id[0] += 1
        return next_id[0]

    _install_st(monkeypatch, state)
    monkeypatch.setattr(live_page, "get_recorder", lambda _: audio)
    monkeypatch.setattr(live_page, "get_video_recorder", lambda _: video)
    monkeypatch.setattr(live_page, "recording_consent_granted", lambda _: True)
    monkeypatch.setattr(live_page, "ensure_capture_manifest", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        live_page, "update_capture_manifest_for_recorders", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(live_page, "capture_audio_bytes", lambda _: audio_bytes)
    monkeypatch.setattr(live_page, "transcribe_audio_bytes", lambda _: "transcript")
    monkeypatch.setattr(live_page, "save_live_media", _save)
    return saves, audio, video


def test_normal_streamlit_rerun_keeps_active_answer() -> None:
    state, owner = _state()
    answer_id = start_recording(state, 1)
    recorder = get_recorder(state)

    session.bind_live_session(state, user_id=3, session_token="token")
    repeated = start_recording(state, 1)

    assert repeated == answer_id
    assert get_recorder(state) is recorder
    assert current_answer_id(state) == answer_id
    assert session.is_live_session_owner(state, owner)
    assert recorder_lifecycle_snapshot(state)["generation"] == 1


def test_multiple_reruns_do_not_create_duplicate_recorders() -> None:
    state, _ = _state()
    first_answer = start_recording(state, 4)
    audio = get_recorder(state)
    video = get_video_recorder(state)

    for _ in range(5):
        session.bind_live_session(state, user_id=3, session_token="token")
        assert start_recording(state, 4) == first_answer

    assert get_recorder(state) is audio
    assert get_video_recorder(state) is video


def test_rerun_after_stop_does_not_restart_answer() -> None:
    state, _ = _state()
    answer_id = start_recording(state, 2)
    assert stop_recording(state) is True
    stopped_state = recorder_lifecycle_snapshot(state)["state"]

    session.bind_live_session(state, user_id=3, session_token="token")

    assert current_answer_id(state) == answer_id
    assert recorder_lifecycle_snapshot(state)["state"] == stopped_state


def test_browser_disconnect_during_recording_queues_safe_finalization() -> None:
    state, owner = _state()
    answer_id = start_recording(state, 1)
    generation = session.bump_stream_generation(state, MICROPHONE, owner=owner)

    assert session.mark_stream_disconnected(
        state,
        MICROPHONE,
        owner=owner,
        answer_id=answer_id,
        stream_generation=generation,
        reason="browser tab closed",
    )
    request = session.request_disconnect_finalization(
        state,
        owner=owner,
        answer_id=answer_id,
        reason="browser tab closed",
        stream_generation=generation,
    )

    assert request is not None
    assert request.answer_id == answer_id
    assert state[session.LIVE_DISCONNECT_PENDING] is request
    assert state[session.LIVE_STREAM_DISCONNECTED][MICROPHONE]["reason"] == "browser tab closed"


def test_browser_reconnect_after_disconnect_clears_recovery_request() -> None:
    state, owner = _state()
    answer_id = start_recording(state, 1)
    generation = session.get_stream_generation(state, MICROPHONE)
    session.mark_stream_disconnected(
        state, MICROPHONE, owner=owner, answer_id=answer_id, stream_generation=generation
    )
    session.request_disconnect_finalization(
        state, owner=owner, answer_id=answer_id, reason="disconnected"
    )

    assert session.clear_disconnect_finalization(
        state, owner=owner, answer_id=answer_id
    )
    assert session.mark_stream_connected(
        state, MICROPHONE, owner=owner, stream_generation=generation
    )
    assert session.pop_disconnect_finalization(state, owner) is None
    assert MICROPHONE not in state[session.LIVE_STREAM_DISCONNECTED]


def test_stale_callback_after_reconnect_is_rejected() -> None:
    state, owner = _state()
    answer_id = start_recording(state, 1)
    old_generation = session.get_stream_generation(state, CAMERA)
    session.bump_stream_generation(state, CAMERA, owner=owner)

    assert not session.can_control_live_recording(
        state,
        owner,
        answer_id=answer_id,
        kind=CAMERA,
        stream_generation=old_generation,
    )
    assert not session.mark_stream_disconnected(
        state,
        CAMERA,
        owner=owner,
        answer_id=answer_id,
        stream_generation=old_generation,
    )


def test_old_generation_callback_after_new_answer_does_not_mutate_recorder() -> None:
    state, owner = _state()
    first_answer = start_recording(state, 1)
    generation = session.get_stream_generation(state, MICROPHONE)
    recorder = _GenerationRecorder()
    processor = RecordingAudioProcessor(
        BoundedAudioEventQueue(maxsize=4),
        recorder,
        generation=recorder.generation,
        is_current=lambda: session.can_control_live_recording(
            state,
            owner,
            answer_id=first_answer,
            kind=MICROPHONE,
            stream_generation=generation,
        ),
    )

    second_answer = start_recording(state, 2)
    processor.recv(object())
    processor.on_ended()

    assert second_answer != first_answer
    assert recorder.frames == []
    assert recorder.events == 0
    assert recorder.enabled is True


def test_two_independent_sessions_record_simultaneously() -> None:
    first, first_owner = _state(user_id=3, token="alpha")
    second, second_owner = _state(user_id=4, token="beta")
    first_answer = start_recording(first, 1)
    second_answer = start_recording(second, 1)

    assert first_owner.owner_key != second_owner.owner_key
    assert first_answer != second_answer
    assert current_answer_id(first) == first_answer
    assert current_answer_id(second) == second_answer


def test_one_session_cannot_stop_another_session_recorder() -> None:
    first, first_owner = _state(user_id=3, token="alpha")
    second, _ = _state(user_id=4, token="beta")
    start_recording(first, 1)
    start_recording(second, 1)

    assert not session.can_control_live_recording(second, first_owner)
    result = live_session_cleanup.teardown_live_session_state(
        second, reason="cross-session stop", owner=first_owner, authorize=True
    )

    assert result["stopped"] is False
    assert recorder_lifecycle_snapshot(second)["state"] == "RECORDING"


def test_navigation_away_during_recording_stops_capture_and_preserves_pending_ids() -> None:
    state, _ = _state()
    start_recording(state, 1)
    add_pending_media_id(state, 55)
    state[session.LIVE_SESSION_PAGE] = "live_webcam"

    result = live_session_cleanup.teardown_for_navigation(state=state)

    assert result["stopped"] is True
    assert state[session.LIVE_SESSION_PAGE] is None
    assert state[RECORDING_ACTIVE] is False
    assert state["_live_webcam_camera_playing"] is False
    assert state["_live_webcam_microphone_playing"] is False
    assert state[PENDING_MEDIA_IDS] == [55]


def test_sign_out_during_recording_tears_down_before_clearing_auth(monkeypatch) -> None:
    state, _ = _state()
    start_recording(state, 1)
    add_pending_media_id(state, 66)
    monkeypatch.setattr(
        auth_ui,
        "st",
        SimpleNamespace(session_state=state, rerun=lambda: None),
    )
    monkeypatch.setattr(
        live_session_cleanup,
        "st",
        SimpleNamespace(session_state=state),
    )
    monkeypatch.setattr(auth_ui, "log_event", lambda *_args, **_kwargs: None)

    auth_ui.sign_out()

    assert state[session.LIVE_SESSION_TEARDOWNS]
    assert state[RECORDING_ACTIVE] is False
    assert state[PENDING_MEDIA_IDS] == [66]
    assert "user_id" not in state
    assert "user_session_id" not in state


def test_authentication_expiration_during_recording_is_recoverable() -> None:
    state, _ = _state()
    answer_id = start_recording(state, 1)
    add_pending_media_id(state, 77)

    result = live_session_cleanup.teardown_for_auth(
        state=state, reason="account deactivated"
    )

    assert result["answer_id"] == answer_id
    assert state[session.LIVE_SESSION_RECOVERABLE]["answer_id"] == answer_id
    assert state[PENDING_MEDIA_IDS] == [77]


def test_relogin_after_cleanup_does_not_restore_old_recording() -> None:
    state, old_owner = _state(user_id=3, token="alpha")
    old_answer = start_recording(state, 1)
    live_session_cleanup.teardown_for_auth(state=state, reason="signed out")

    binding = session.bind_live_session(state, user_id=3, session_token="beta")

    assert binding.changed
    assert binding.current.owner_key != old_owner.owner_key
    assert current_answer_id(state) is None
    assert recorder_lifecycle_snapshot(state)["state"] == "IDLE"
    assert state[PENDING_MEDIA_IDS] == []
    assert old_answer not in [current_answer_id(state)]


def test_repeated_teardown_is_idempotent() -> None:
    state, _ = _state()
    start_recording(state, 1)

    first = live_session_cleanup.teardown_for_auth(state=state, reason="repeat")
    second = live_session_cleanup.teardown_for_auth(state=state, reason="repeat")

    assert first["stopped"] is True
    assert second["stopped"] is True
    assert len(state[session.LIVE_SESSION_TEARDOWNS]) == 1


def test_worker_and_resource_lease_cleanup_after_disconnect(monkeypatch) -> None:
    ended: list[str] = []

    monkeypatch.setattr(live_video, "acquire_resource_lease", _fake_acquire_resource_lease)
    monkeypatch.setattr(live_video, "end_heavy_task", lambda task, **kwargs: ended.append(task))
    state, _ = _state()
    state[VIDEO_RECORDER] = live_video.LiveVideoRecorder()
    start_recording(state, 1, record_video=True)
    assert state[VIDEO_RECORDER].snapshot()["enabled"] is True

    live_session_cleanup.teardown_for_auth(state=state, reason="disconnect")

    assert state[VIDEO_RECORDER].snapshot()["enabled"] is False
    assert ended == ["video_processing"]


def test_failed_finalization_during_disconnect_sets_failed_status(monkeypatch) -> None:
    state, owner = _state()
    answer_id = start_recording(state, 1)
    session.request_disconnect_finalization(
        state, owner=owner, answer_id=answer_id, reason="connection lost"
    )
    _install_finalize_dependencies(
        monkeypatch,
        state,
        audio_bytes=b"",
        video_bytes=b"",
        audio_error="microphone disconnected",
        video_error="camera disconnected",
    )

    assert live_page._handle_disconnect_recovery(3, "token") is True

    assert state[MEDIA_STATUS] == "failed"
    assert state[PENDING_MEDIA_IDS] == []
    assert session.pop_disconnect_finalization(state, owner) is None


def test_partial_media_save_during_disconnect_preserves_saved_ids(monkeypatch) -> None:
    state, owner = _state()
    answer_id = start_recording(state, 1)
    session.request_disconnect_finalization(
        state, owner=owner, answer_id=answer_id, reason="connection lost"
    )
    saves, _, _ = _install_finalize_dependencies(
        monkeypatch,
        state,
        audio_bytes=b"audio",
        video_bytes=b"",
        video_error="encoder stopped",
    )

    assert live_page._handle_disconnect_recovery(3, "token") is True

    assert state[MEDIA_STATUS] == "partial"
    assert len(saves) == 1
    assert len(state[PENDING_MEDIA_IDS]) == 1


@pytest.mark.parametrize("delete_media", [True, False])
def test_pending_media_ids_survive_cleanup_until_delete_decision(
    monkeypatch, delete_media
) -> None:
    state, _ = _state()
    start_recording(state, 1)
    add_pending_media_id(state, 88)
    state[live_page.DELETE_MEDIA_ON_ABANDON] = delete_media
    _install_st(monkeypatch, state)
    monkeypatch.setattr(live_page, "stop_all_streams", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(live_page, "go_to", lambda _page: None)
    monkeypatch.setattr(
        live_page,
        "delete_live_recording",
        lambda _recording_id, **_kwargs: {} if delete_media else (_ for _ in ()).throw(
            AssertionError("delete must not run")
        ),
    )
    monkeypatch.setattr(live_page, "finish_live_recording", lambda **_kwargs: None)
    monkeypatch.setattr(live_page, "abandon_live_interview", lambda **_kwargs: None)

    live_page._perform_abandon(42, 3, "token")

    if delete_media:
        assert state[PENDING_MEDIA_IDS] == []
    else:
        assert state[PENDING_MEDIA_IDS] == [88]


def test_application_shutdown_with_active_recording_clears_video_resources(
    monkeypatch,
) -> None:
    state, _ = _state()
    start_recording(state, 1)
    monkeypatch.setattr(live_video, "shutdown_all_video_recorders", lambda: 3)

    result = live_session_cleanup.teardown_for_shutdown(state=state)

    assert result["recorders_cleared"] == 3
    assert state[RECORDING_ACTIVE] is False
    assert state[session.LIVE_SESSION_TEARDOWNS]


def test_phase331_worker_safety_regression(monkeypatch) -> None:
    ended: list[str] = []
    monkeypatch.setattr(live_video, "acquire_resource_lease", _fake_acquire_resource_lease)
    monkeypatch.setattr(live_video, "end_heavy_task", lambda task, **kwargs: ended.append(task))
    recorder = live_video.LiveVideoRecorder()
    recorder.set_enabled(True)

    first = recorder.finalize()
    second = recorder.finalize()
    recorder.clear()

    assert first == b""
    assert second == b""
    assert ended == ["video_processing"]
    assert recorder.snapshot()["finalized"] is False


def test_phase332_state_machine_and_duplicate_operation_regression() -> None:
    state, _ = _state()
    first = start_recording(state, 3)
    assert start_recording(state, 3) == first
    stop_recording(state)
    assert stop_recording(state) is False
    assert recorder_lifecycle_snapshot(state)["answer_id"] == first


def test_navigation_helpers_avoid_cleanup_on_live_page(monkeypatch) -> None:
    state, _ = _state()
    start_recording(state, 1)
    state[session.LIVE_SESSION_PAGE] = "live_webcam"
    monkeypatch.setattr(navigation.st, "session_state", state)
    monkeypatch.setattr(navigation, "switch_page", lambda _target: None)
    calls: list[str] = []
    monkeypatch.setattr(
        live_session_cleanup,
        "teardown_for_navigation",
        lambda **_kwargs: calls.append("cleanup"),
    )
    monkeypatch.setattr(
        shared_components,
        "page_header",
        lambda *_args, **_kwargs: None,
    )

    navigation.go_to("live_webcam")

    assert calls == []
    assert state[RECORDING_ACTIVE] is True
