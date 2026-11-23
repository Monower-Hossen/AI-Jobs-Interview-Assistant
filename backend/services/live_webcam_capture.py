from __future__ import annotations

from collections.abc import Callable

from services.live_webcam_audio import LiveAudioRecorder
from services.live_webcam_events import AudioFrameEvent, BoundedAudioEventQueue
from services.live_webcam_video import LiveVideoRecorder
from streamlit_webrtc import AudioProcessorBase, VideoProcessorBase


class PreviewVideoProcessor(VideoProcessorBase):
    def recv(self, frame):
        return frame


class RecordingVideoProcessor(PreviewVideoProcessor):
    def __init__(
        self,
        recorder: LiveVideoRecorder,
        *,
        generation: int | None = None,
        is_current: Callable[[], bool] | None = None,
        on_ended: Callable[[], None] | None = None,
    ):
        super().__init__()
        self._recorder = recorder
        self._generation = generation
        self._generation_pinned = generation is not None
        self._is_current = is_current
        self._on_ended = on_ended

    def recv(self, frame):
        if self._is_current is not None and not self._is_current():
            return frame
        if self._recorder.enabled:
            generation = self._recorder.generation
            if self._generation is None or (
                not self._generation_pinned and self._generation != generation
            ):
                self._generation = generation
            if self._generation == generation:
                try:
                    self._recorder.enqueue(frame, generation=generation)
                except Exception as exc:
                    self._recorder.set_error(str(exc))
        return frame

    async def recv_queued(self, frames):
        result = []
        for frame in frames:
            result.append(self.recv(frame))
        return result

    def on_ended(self) -> None:
        if self._is_current is not None and not self._is_current():
            return
        if self._generation is not None and self._generation != self._recorder.generation:
            return
        self._recorder.set_enabled(False)
        if self._on_ended is not None:
            self._on_ended()


class SilentAudioProcessor(AudioProcessorBase):
    def __init__(self, event_queue: BoundedAudioEventQueue):
        super().__init__()
        self._events = event_queue
        self._frame_count = 0

    def recv(self, frame):
        self._frame_count += 1
        layout = getattr(frame, "layout", None)
        channels = len(getattr(layout, "channels", ()) or ()) or 1
        samples = int(getattr(frame, "samples", 0) or 0)
        self._events.publish(
            AudioFrameEvent(
                frame_count=self._frame_count,
                samples=samples,
                channels=channels,
            )
        )
        return frame

    def on_ended(self) -> None:
        self._events.clear()


class RecordingAudioProcessor(SilentAudioProcessor):
    def __init__(
        self,
        event_queue: BoundedAudioEventQueue,
        recorder: LiveAudioRecorder,
        *,
        generation: int | None = None,
        is_current: Callable[[], bool] | None = None,
        on_ended: Callable[[], None] | None = None,
    ):
        super().__init__(event_queue)
        self._recorder = recorder
        self._generation = generation
        self._generation_pinned = generation is not None
        self._is_current = is_current
        self._on_ended = on_ended

    def recv(self, frame):
        if self._is_current is not None and not self._is_current():
            return frame
        result = super().recv(frame)
        if self._recorder.enabled:
            generation = self._recorder.generation
            if self._generation is None or (
                not self._generation_pinned and self._generation != generation
            ):
                self._generation = generation
            if self._generation == generation:
                try:
                    self._recorder.append_frame(frame)
                except Exception as exc:
                    self._recorder.set_capture_error(str(exc))
        return result

    def on_ended(self) -> None:
        if self._is_current is not None and not self._is_current():
            return
        if self._generation is not None and self._generation != self._recorder.generation:
            return
        super().on_ended()
        self._recorder.set_enabled(False)
        if self._on_ended is not None:
            self._on_ended()
