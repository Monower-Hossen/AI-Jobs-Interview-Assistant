from __future__ import annotations

import queue
from dataclasses import dataclass

LIVE_AUDIO_QUEUE_MAXSIZE = 32


@dataclass(frozen=True)
class AudioFrameEvent:
    frame_count: int
    samples: int
    channels: int


class BoundedAudioEventQueue:
    def __init__(self, maxsize: int = LIVE_AUDIO_QUEUE_MAXSIZE):
        if int(maxsize) < 1:
            raise ValueError("maxsize must be at least 1")
        self._queue: queue.Queue[AudioFrameEvent] = queue.Queue(maxsize=int(maxsize))

    def publish(self, event: AudioFrameEvent) -> None:
        try:
            self._queue.put_nowait(event)
            return
        except queue.Full:
            pass
        try:
            self._queue.get_nowait()
        except queue.Empty:
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            return

    def drain(self) -> list[AudioFrameEvent]:
        events: list[AudioFrameEvent] = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                return events

    def clear(self) -> None:
        self.drain()

    def __len__(self) -> int:
        return self._queue.qsize()
