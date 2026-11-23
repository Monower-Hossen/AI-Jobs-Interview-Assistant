from pathlib import Path
from types import SimpleNamespace

import pytest
from services.live_webcam_capture import (
    PreviewVideoProcessor,
    RecordingAudioProcessor,
    RecordingVideoProcessor,
    SilentAudioProcessor,
)
from services.live_webcam_events import AudioFrameEvent, BoundedAudioEventQueue
from services.live_webcam_state import (
    AUDIO_EVENT_QUEUE,
    CAMERA,
    CONSENT_CAMERA,
    CONSENT_MICROPHONE,
    LIVE_WEBCAM_STATE_KEYS,
    MICROPHONE,
    can_start_stream,
    consent_granted,
    drain_audio_events,
    initialize_live_webcam_state,
    media_error_message,
    reset_live_webcam_state,
    set_stream_error,
    set_stream_requested,
    stream_error,
    stream_requested,
    stream_status,
)
from streamlit.testing.v1 import AppTest

from frontend.user import live_webcam_interview as live_page

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeLayout:
    channels = ("left", "right")


class _FakeFrame:
    samples = 960
    layout = _FakeLayout()


def test_initialize_and_reset_live_webcam_state():
    state = {}
    initialize_live_webcam_state(state)

    assert all(key in state for key in LIVE_WEBCAM_STATE_KEYS)
    assert not consent_granted(state, CAMERA)
    assert not stream_requested(state, MICROPHONE)

    state[CONSENT_CAMERA] = True
    set_stream_requested(state, CAMERA, True)
    set_stream_error(state, CAMERA, "denied")
    state[AUDIO_EVENT_QUEUE].publish(AudioFrameEvent(1, 960, 2))

    reset_live_webcam_state(state)

    assert not consent_granted(state, CAMERA)
    assert not stream_requested(state, CAMERA)
    assert stream_error(state, CAMERA) is None
    assert drain_audio_events(state) == []


def test_capture_requires_consent_and_tracks_requested_state():
    state = {}
    initialize_live_webcam_state(state)

    assert not can_start_stream(state, CAMERA)
    assert not can_start_stream(state, MICROPHONE)

    state[CONSENT_CAMERA] = True
    assert can_start_stream(state, CAMERA)
    assert not can_start_stream(state, MICROPHONE)

    set_stream_requested(state, CAMERA, True)
    assert stream_requested(state, CAMERA)
    assert not can_start_stream(state, CAMERA)

    set_stream_requested(state, CAMERA, False)
    assert not stream_requested(state, CAMERA)
    assert can_start_stream(state, CAMERA)


def test_stopping_microphone_clears_buffered_events_and_error():
    state = {}
    initialize_live_webcam_state(state)
    state[AUDIO_EVENT_QUEUE].publish(AudioFrameEvent(2, 960, 2))
    set_stream_error(state, MICROPHONE, "device lost")
    set_stream_requested(state, MICROPHONE, True)
    assert len(state[AUDIO_EVENT_QUEUE]) == 1

    set_stream_requested(state, MICROPHONE, False)

    assert drain_audio_events(state) == []
    assert stream_error(state, MICROPHONE) is None


def test_bounded_audio_queue_drops_oldest_event():
    event_queue = BoundedAudioEventQueue(maxsize=2)
    for index in (1, 2, 3):
        event_queue.publish(AudioFrameEvent(index, 960, 2))

    assert [event.frame_count for event in event_queue.drain()] == [2, 3]
    assert event_queue.drain() == []

    with pytest.raises(ValueError):
        BoundedAudioEventQueue(maxsize=0)


def test_audio_processor_publishes_metadata_only():
    event_queue = BoundedAudioEventQueue(maxsize=4)
    processor = SilentAudioProcessor(event_queue)
    frame = _FakeFrame()

    result = processor.recv(frame)

    assert result is frame
    events = event_queue.drain()
    assert len(events) == 1
    assert events[0] == AudioFrameEvent(frame_count=1, samples=960, channels=2)
    assert all(isinstance(value, int) for value in vars(events[0]).values())

    processor.on_ended()
    assert event_queue.drain() == []


def test_video_processor_returns_frame_unchanged():
    processor = PreviewVideoProcessor()
    frame = object()

    assert processor.recv(frame) is frame


def test_stream_status_and_error_messages():
    off = stream_status(
        CAMERA, consent=False, requested=False, playing=False, signalling=False
    )
    connected = stream_status(
        CAMERA, consent=True, requested=True, playing=True, signalling=False
    )
    connecting = stream_status(
        MICROPHONE, consent=True, requested=True, playing=False, signalling=True
    )
    denied = stream_status(
        MICROPHONE,
        consent=True,
        requested=True,
        playing=False,
        signalling=False,
        error_reason="NotAllowedError",
    )

    assert off["label"] == "Off"
    assert off["tone"] == "info"
    assert connected["label"] == "Connected"
    assert connected["tone"] == "success"
    assert connecting["label"] == "Connecting"
    assert "permission" in denied["message"].lower()
    assert "No microphone device" in media_error_message(MICROPHONE, "NotFoundError")


def test_live_webcam_page_renders_and_respects_consent():
    at = AppTest.from_file(
        str(PROJECT_ROOT / "frontend" / "user" / "live_webcam_interview.py")
    )
    at.session_state["user_id"] = 1
    at.session_state["user_session_id"] = "test-token"
    at.session_state[CONSENT_CAMERA] = True
    at.session_state[CONSENT_MICROPHONE] = True
    at.run(timeout=60)

    assert not at.exception
    assert at.title[0].value == "AI live webcam interview"
    assert at.checkbox(key=CONSENT_CAMERA).value is True
    assert at.checkbox(key=CONSENT_MICROPHONE).value is True
    assert at.button(key="_live_webcam_start_camera")
    assert at.button(key="_live_webcam_stop_camera")


class _GenerationVideoRecorder:
    def __init__(self) -> None:
        self.enabled = True
        self.generation = 1
        self.enqueued: list[int] = []
        self.errors: list[str] = []

    def enqueue(self, frame, *, generation: int) -> None:
        self.enqueued.append(generation)

    def set_error(self, message: str) -> None:
        self.errors.append(message)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled


class _GenerationAudioRecorder:
    def __init__(self) -> None:
        self.enabled = True
        self.generation = 1
        self.frames = 0
        self.errors: list[str] = []

    def append_frame(self, frame) -> None:
        self.frames += 1

    def set_capture_error(self, message: str) -> None:
        self.errors.append(message)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled


def test_recording_video_processor_relatches_auto_generation() -> None:
    recorder = _GenerationVideoRecorder()
    ended: list[int] = []
    processor = RecordingVideoProcessor(
        recorder,
        is_current=lambda: True,
        on_ended=lambda: ended.append(1),
    )
    frame = object()

    assert processor.recv(frame) is frame
    recorder.generation = 2
    assert processor.recv(frame) is frame
    assert recorder.enqueued == [1, 2]

    processor.on_ended()

    assert ended == [1]
    assert recorder.enabled is False


def test_recording_audio_processor_relatches_auto_generation() -> None:
    event_queue = BoundedAudioEventQueue(maxsize=4)
    recorder = _GenerationAudioRecorder()
    processor = RecordingAudioProcessor(
        event_queue,
        recorder,
        is_current=lambda: True,
    )

    processor.recv(_FakeFrame())
    recorder.generation = 2
    processor.recv(_FakeFrame())

    assert recorder.frames == 2
    assert len(event_queue.drain()) == 2

    processor.on_ended()

    assert recorder.enabled is False
    assert event_queue.drain() == []


def test_pinned_generation_processor_ignores_new_recording() -> None:
    recorder = _GenerationVideoRecorder()
    processor = RecordingVideoProcessor(
        recorder,
        generation=1,
        is_current=lambda: True,
    )
    frame = object()
    recorder.generation = 2

    assert processor.recv(frame) is frame
    assert recorder.enqueued == []

    processor.on_ended()

    assert recorder.enabled is True


def test_webrtc_streamer_uses_processor_factories_without_ended_callbacks(monkeypatch) -> None:
    class _ContextState:
        playing = False
        signalling = False

    class _Context:
        state = _ContextState()

    calls: list[dict] = []

    def _fake_streamer(**kwargs):
        calls.append(kwargs)
        return _Context()

    monkeypatch.setattr(live_page, "webrtc_streamer", _fake_streamer)
    monkeypatch.setattr(live_page, "st", SimpleNamespace(session_state={}))
    monkeypatch.setattr(live_page, "get_video_recorder", lambda _: SimpleNamespace(generation=3))
    monkeypatch.setattr(live_page, "get_recorder", lambda _: SimpleNamespace(generation=4))
    monkeypatch.setattr(
        live_page,
        "get_audio_event_queue",
        lambda _: BoundedAudioEventQueue(maxsize=2),
    )
    monkeypatch.setattr(live_page, "current_live_owner", lambda _: None)
    monkeypatch.setattr(live_page, "current_answer_id", lambda _: None)
    monkeypatch.setattr(
        live_page,
        "_stream_callbacks",
        lambda _kind: (1, lambda: True, lambda: None),
    )
    monkeypatch.setattr(live_page, "_sync_stream_context", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(live_page, "_render_stream_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(live_page, "_render_audio_activity", lambda: None)

    live_page._render_video_stream(False)
    live_page._render_audio_stream(False)

    video_kwargs, audio_kwargs = calls
    assert "on_video_ended" not in video_kwargs
    assert "on_audio_ended" not in audio_kwargs
    assert video_kwargs["video_processor_factory"]()._generation is None
    assert audio_kwargs["audio_processor_factory"]()._generation is None
