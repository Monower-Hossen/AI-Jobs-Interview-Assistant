from __future__ import annotations

from services.live_webcam_audio import LiveAudioRecorder
from services.live_webcam_events import AudioFrameEvent, BoundedAudioEventQueue
from services.live_webcam_video import LiveVideoRecorder
from streamlit_webrtc import AudioProcessorBase, VideoProcessorBase


class PreviewVideoProcessor(VideoProcessorBase):
    def recv(self, frame):
        return frame


class RecordingVideoProcessor(PreviewVideoProcessor):
    def __init__(self, recorder: LiveVideoRecorder):
        super().__init__()
        self._recorder = recorder
        self._generation: int | None = None

    def recv(self, frame):
        if self._recorder.enabled:
            generation = self._recorder.generation
            if self._generation is None:
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
        self._recorder.set_enabled(False)


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
    def __init__(self, event_queue: BoundedAudioEventQueue, recorder: LiveAudioRecorder):
        super().__init__(event_queue)
        self._recorder = recorder
        self._generation: int | None = None

    def recv(self, frame):
        result = super().recv(frame)
        if self._recorder.enabled:
            generation = self._recorder.generation
            if self._generation is None:
                self._generation = generation
            if self._generation == generation:
                try:
                    self._recorder.append_frame(frame)
                except Exception as exc:
                    self._recorder.set_capture_error(str(exc))
        return result

    def on_ended(self) -> None:
        super().on_ended()
        self._recorder.set_enabled(False)
