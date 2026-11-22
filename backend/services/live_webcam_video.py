from __future__ import annotations

import hashlib
import io
import queue
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import av
from config.settings import settings
from services.resource_monitor import end_heavy_task, try_begin_heavy_task

VIDEO_CODEC = "libx264"
VIDEO_PIXEL_FORMAT = "yuv420p"
VIDEO_TIME_BASE = Fraction(1, 90_000)
VIDEO_CONTAINER = "mp4"
_STOP = object()


@dataclass(frozen=True)
class VideoFrameRecord:
    pixels: Any
    pts: int | None
    time_base: Fraction | None
    captured_at: float
    width: int
    height: int


class LiveVideoRecorder:
    def __init__(
        self,
        *,
        max_seconds: int | None = None,
        max_bytes: int | None = None,
        max_frames: int = 20_000,
        queue_maxsize: int = 45,
        fps: int = 30,
    ) -> None:
        self.max_seconds = int(max_seconds or settings.live_media_max_seconds)
        self.max_bytes = int(max_bytes or settings.live_media_max_bytes)
        self.max_frames = int(max_frames)
        self.fps = int(fps)
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max(2, int(queue_maxsize)))
        self._lock = threading.RLock()
        self._buffer = io.BytesIO()
        self._container: Any = None
        self._stream: Any = None
        self._worker: threading.Thread | None = None
        self._enabled = False
        self._stop_requested = False
        self._finalized = False
        self._error: str | None = None
        self._encoding_lease_held = False
        self._frames_queued = 0
        self._frames_encoded = 0
        self._dropped_frames = 0
        self._queue_overflow_count = 0
        self._timestamp_adjustments = 0
        self._bytes_written = 0
        self._first_pts_seconds: float | None = None
        self._last_pts = -1
        self._width = 0
        self._height = 0
        self._overflowed = False
        self._first_received_monotonic_ns: int | None = None
        self._last_received_monotonic_ns: int | None = None

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled and not self._finalized

    def set_error(self, message: str) -> None:
        with self._lock:
            self._error = str(message)

    def _release_encoding_lease_locked(self) -> None:
        if self._encoding_lease_held:
            end_heavy_task("video_processing")
            self._encoding_lease_held = False

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            should_enable = bool(enabled)
            self._enabled = should_enable
            if should_enable:
                if self._worker is None or not self._worker.is_alive():
                    if not self._encoding_lease_held:
                        allowed, reason = try_begin_heavy_task("video_processing")
                        if not allowed:
                            self._enabled = False
                            self._error = reason or "Video processing is currently busy."
                            return
                        self._encoding_lease_held = True
                    self._stop_requested = False
                    self._worker = threading.Thread(
                        target=self._consume, name="live-video-encoder", daemon=True
                    )
                    self._worker.start()
            elif not self._stop_requested:
                self._stop_requested = True
                self._queue.put(_STOP)

    def enqueue(self, frame: Any) -> None:
        if not self.enabled:
            return
        received_ns = time.monotonic_ns()
        try:
            pixels = frame.to_ndarray(format=VIDEO_PIXEL_FORMAT).copy(order="C")
            record = VideoFrameRecord(
                pixels=pixels,
                pts=int(frame.pts) if getattr(frame, "pts", None) is not None else None,
                time_base=self._coerce_time_base(getattr(frame, "time_base", None)),
                captured_at=time.time(),
                width=int(frame.width),
                height=int(frame.height),
            )
        except Exception as exc:
            self.set_error(f"Could not read a camera frame: {exc}")
            return
        try:
            self._queue.put_nowait(record)
            with self._lock:
                self._frames_queued += 1
                if self._first_received_monotonic_ns is None:
                    self._first_received_monotonic_ns = received_ns
                self._last_received_monotonic_ns = received_ns
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            with self._lock:
                self._dropped_frames += 1
                self._queue_overflow_count += 1

    @staticmethod
    def _coerce_time_base(value: Any) -> Fraction | None:
        if value is None:
            return None
        try:
            result = Fraction(value)
            return result if result > 0 else None
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    def _rescale_pts(self, record: VideoFrameRecord) -> int:
        if record.pts is None or record.time_base is None:
            seconds = (record.captured_at - self._first_pts_seconds_or_now()) % 3600
        else:
            seconds = float(record.pts * record.time_base)
            if self._first_pts_seconds is None:
                self._first_pts_seconds = seconds
            seconds -= self._first_pts_seconds
        candidate = int(seconds * float(VIDEO_TIME_BASE))
        if candidate <= self._last_pts:
            candidate = self._last_pts + max(1, int(float(VIDEO_TIME_BASE) / self.fps))
            self._timestamp_adjustments += 1
        self._last_pts = candidate
        return candidate

    def _first_pts_seconds_or_now(self) -> float:
        if self._first_pts_seconds is None:
            self._first_pts_seconds = time.time()
        return self._first_pts_seconds

    def _ensure_container(self, record: VideoFrameRecord) -> None:
        if self._container is not None:
            return
        self._container = av.open(self._buffer, mode="w", format="mp4")
        self._stream = self._container.add_stream(VIDEO_CODEC, rate=self.fps)
        self._stream.pix_fmt = VIDEO_PIXEL_FORMAT
        self._stream.width = record.width
        self._stream.height = record.height
        self._stream.time_base = VIDEO_TIME_BASE
        self._stream.codec_context.options = {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "crf": "26",
        }
        with self._lock:
            self._width = record.width
            self._height = record.height

    def _consume(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    return
                if not isinstance(item, VideoFrameRecord):
                    continue
                try:
                    with self._lock:
                        if self._finalized or self._overflowed or self._frames_encoded >= self.max_frames:
                            if self._frames_encoded >= self.max_frames:
                                self._overflowed = True
                            continue
                    self._ensure_container(item)
                    frame = av.VideoFrame.from_ndarray(item.pixels, format=VIDEO_PIXEL_FORMAT)
                    frame.pts = self._rescale_pts(item)
                    frame.time_base = VIDEO_TIME_BASE
                    for packet in self._stream.encode(frame):
                        self._container.mux(packet)
                    with self._lock:
                        self._frames_encoded += 1
                        self._bytes_written = self._buffer.tell()
                        if self._bytes_written > self.max_bytes:
                            self._overflowed = True
                except Exception as exc:
                    self.set_error(f"Could not encode a camera frame: {exc}")
                    return
        finally:
            with self._lock:
                self._release_encoding_lease_locked()

    def finalize(self) -> bytes:
        with self._lock:
            if self._finalized:
                return self._buffer.getvalue()
            self._enabled = False
            if not self._stop_requested:
                self._stop_requested = True
                self._queue.put(_STOP)
            worker = self._worker
            needs_finalize_lease = not self._encoding_lease_held

        if worker is not None and worker.is_alive():
            worker.join(timeout=10)

        acquired_finalize_lease = False
        if needs_finalize_lease:
            allowed, reason = try_begin_heavy_task("video_processing")
            if not allowed:
                raise RuntimeError(reason or "Video processing is currently busy.")
            acquired_finalize_lease = True
        try:
            with self._lock:
                if not self._finalized:
                    try:
                        if self._stream is not None and self._container is not None:
                            for packet in self._stream.encode():
                                self._container.mux(packet)
                    except Exception as exc:
                        self._error = f"Could not finalize the video recording: {exc}"
                    finally:
                        if self._container is not None:
                            try:
                                self._container.close()
                            except Exception as exc:
                                self._error = self._error or f"Could not close the video file: {exc}"
                            self._container = None
                        self._stream = None
                    self._finalized = True
                    self._bytes_written = self._buffer.tell()
                    self._release_encoding_lease_locked()
                return self._buffer.getvalue() if self._error is None else b""
        finally:
            if acquired_finalize_lease:
                end_heavy_task("video_processing")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            duration = 0.0
            if self._last_pts >= 0:
                duration = max(0.0, float(self._last_pts + 1) / float(self.fps))
            elif self._frames_encoded > 0:
                duration = max(0.0, float(self._frames_encoded) / float(self.fps))
            return {
                "enabled": self._enabled,
                "finalized": self._finalized,
                "frames_queued": self._frames_queued,
                "frames_encoded": self._frames_encoded,
                "dropped_frames": self._dropped_frames,
                "queue_overflow_count": self._queue_overflow_count,
                "timestamp_adjustments": self._timestamp_adjustments,
                "bytes_written": self._bytes_written,
                "duration_seconds": round(duration, 3),
                "width": self._width,
                "height": self._height,
                "fps": self.fps,
                "codec": VIDEO_CODEC,
                "pixel_format": VIDEO_PIXEL_FORMAT,
                "container": VIDEO_CONTAINER,
                "overflowed": self._overflowed,
                "error": self._error,
                "first_received_monotonic_ns": self._first_received_monotonic_ns,
                "last_received_monotonic_ns": self._last_received_monotonic_ns,
            }

    def clear(self) -> None:
        with self._lock:
            if not self._stop_requested:
                self._stop_requested = True
                self._queue.put(_STOP)
            worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=10)
        with self._lock:
            self._release_encoding_lease_locked()
            self._reset_locked()

    def _reset_locked(self) -> None:
        if self._container is not None:
            try:
                self._container.close()
            except Exception:
                pass
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._buffer = io.BytesIO()
        self._container = None
        self._stream = None
        self._worker = None
        self._enabled = False
        self._stop_requested = False
        self._finalized = False
        self._error = None
        self._frames_queued = 0
        self._frames_encoded = 0
        self._dropped_frames = 0
        self._queue_overflow_count = 0
        self._timestamp_adjustments = 0
        self._bytes_written = 0
        self._first_pts_seconds = None
        self._last_pts = -1
        self._width = 0
        self._height = 0
        self._overflowed = False
        self._first_received_monotonic_ns = None
        self._last_received_monotonic_ns = None


def validate_video_bytes(data: bytes) -> tuple[int, str]:
    if not data:
        raise ValueError("The camera recording is empty.")
    if len(data) < 12 or data[4:8] != b"ftyp":
        raise ValueError("The camera recording is not a valid MP4 file.")
    digest = hashlib.sha256(data).hexdigest()
    return len(data), digest
