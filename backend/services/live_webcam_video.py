from __future__ import annotations

import hashlib
import io
import logging
import math
import queue
import threading
import time
import uuid
import weakref
from dataclasses import dataclass

logger = logging.getLogger(__name__)
from fractions import Fraction
from typing import Any

import av
from config.settings import settings
from services.resource_monitor import (
    LeaseReleaseStatus,
    acquire_resource_lease,
    end_heavy_task,
    heartbeat_resource_lease,
    release_resource_lease,
)

VIDEO_CODEC = "libx264"
VIDEO_PIXEL_FORMAT = "yuv420p"
VIDEO_TIME_BASE = Fraction(1, 90_000)
VIDEO_DURATION_TOLERANCE_SECONDS = 0.05
VIDEO_CONTAINER = "mp4"
FINALIZE_TIMEOUT_ERROR = "The video encoder did not stop in time. Finalization can be retried."
LEASE_HEARTBEAT_INTERVAL_SECONDS = 30.0

_ACTIVE_VIDEO_RECORDERS: weakref.WeakSet[LiveVideoRecorder] = weakref.WeakSet()
_ACTIVE_VIDEO_RECORDERS_LOCK = threading.RLock()


@dataclass(frozen=True)
class VideoFrameRecord:
    pixels: Any
    pts: int | None
    time_base: Fraction | None
    captured_at: float
    width: int
    height: int


@dataclass(frozen=True)
class _FrameMessage:
    generation: int
    record: VideoFrameRecord


@dataclass(frozen=True)
class _StopMessage:
    generation: int


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
        self._queue_maxsize = max(2, int(queue_maxsize))
        self._queue: queue.Queue[_FrameMessage] = queue.Queue(maxsize=self._queue_maxsize)
        self._control_queue: queue.Queue[_StopMessage] = queue.Queue(maxsize=1)
        self._lock = threading.RLock()
        self._finalize_lock = threading.Lock()
        self._buffer = io.BytesIO()
        self._container: Any = None
        self._stream: Any = None
        self._worker: threading.Thread | None = None
        self._enabled = False
        self._stop_requested = False
        self._finalized = False
        self._finalize_error: str | None = None
        self._finalized_data: bytes | None = None
        self._error: str | None = None
        self._encoding_lease_held = False
        self._encoding_lease_id: str | None = None
        self._encoding_lease_owner = f"live-video:{uuid.uuid4().hex}"
        self._last_lease_heartbeat_ns = time.monotonic_ns()
        self._frames_queued = 0
        self._frames_encoded = 0
        self._dropped_frames = 0
        self._queue_overflow_count = 0
        self._timestamp_adjustments = 0
        self._bytes_written = 0
        self._first_pts_seconds: float | None = None
        self._first_output_pts: int | None = None
        self._last_pts = -1
        self._width = 0
        self._height = 0
        self._overflowed = False
        self._generation = 0
        self._first_received_monotonic_ns: int | None = None
        self._last_received_monotonic_ns: int | None = None
        with _ACTIVE_VIDEO_RECORDERS_LOCK:
            _ACTIVE_VIDEO_RECORDERS.add(self)

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled and not self._finalized

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def is_alive(self) -> bool:
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    def set_error(self, message: str) -> None:
        with self._lock:
            self._error = str(message)

    def _release_encoding_lease_locked(self) -> None:
        if self._encoding_lease_held:
            end_heavy_task(
                "video_processing",
                lease_id=self._encoding_lease_id,
                owner_id=self._encoding_lease_owner,
                worker_stopped=True,
            )
            self._encoding_lease_held = False
            self._encoding_lease_id = None

    def _frame_ticks(self) -> int:
        return max(1, round(float(1 / VIDEO_TIME_BASE) / max(1, self.fps)))

    def _signal_stop_locked(self, generation: int) -> None:
        while True:
            try:
                self._control_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._control_queue.put_nowait(_StopMessage(generation=generation))
        except queue.Full:
            pass

    def _prepare_new_generation_locked(self) -> None:
        self._generation += 1
        self._queue = queue.Queue(maxsize=self._queue_maxsize)
        self._control_queue = queue.Queue(maxsize=1)

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            should_enable = bool(enabled)
            if should_enable:
                if self._worker is not None and not self._worker.is_alive():
                    self._release_encoding_lease_locked()
                    self._reset_locked()
                if self._finalized and self._worker is None:
                    self._release_encoding_lease_locked()
                    self._reset_locked()
                if self._worker is None:
                    if not self._encoding_lease_held:
                        try:
                            lease, rejection = acquire_resource_lease(
                                "video_processing",
                                owner_id=self._encoding_lease_owner,
                                worker=self,
                            )
                        except Exception as exc:
                            lease, rejection = None, None
                            self._error = str(exc)
                        if lease is None:
                            self._enabled = False
                            self._error = self._error or (
                                rejection.message
                                if rejection is not None
                                else "Video processing is currently busy."
                            )
                            return
                        self._encoding_lease_held = True
                        self._encoding_lease_id = lease.lease_id
                    try:
                        self._prepare_new_generation_locked()
                        self._enabled = True
                        self._stop_requested = False
                        self._worker = threading.Thread(
                            target=self._consume, name="live-video-encoder", daemon=True
                        )
                        self._worker.start()
                    except Exception as exc:
                        self._worker = None
                        self._enabled = False
                        self._error = f"Could not start the video encoder: {exc}"
                        if self._encoding_lease_held and self._encoding_lease_id:
                            release_resource_lease(
                                self._encoding_lease_id,
                                owner_id=self._encoding_lease_owner,
                                worker_stopped=True,
                                release_status=LeaseReleaseStatus.ROLLED_BACK,
                            )
                            self._encoding_lease_held = False
                            self._encoding_lease_id = None
                    return
                if self._stop_requested:
                    self._enabled = False
                    self._error = self._error or "The video encoder is stopping. Try again shortly."
                    return
                self._enabled = True
            else:
                self._enabled = False
                if self._worker is not None and not self._stop_requested:
                    self._stop_requested = True
                    self._signal_stop_locked(self._generation)

    def enqueue(self, frame: Any, *, generation: int | None = None) -> None:
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
        with self._lock:
            if not self._enabled or self._stop_requested or self._finalized:
                return
            if generation is not None and generation != self._generation:
                return
            message = _FrameMessage(generation=self._generation, record=record)
            dropped: _FrameMessage | None = None
            try:
                self._queue.put_nowait(message)
            except queue.Full:
                try:
                    dropped = self._queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._queue.put_nowait(message)
                except queue.Full:
                    return
                self._dropped_frames += 1
                self._queue_overflow_count += 1
            self._frames_queued += 1
            if dropped is None and self._first_received_monotonic_ns is None:
                self._first_received_monotonic_ns = received_ns
            self._last_received_monotonic_ns = received_ns
            if (
                self._encoding_lease_held
                and self._encoding_lease_id
                and received_ns - self._last_lease_heartbeat_ns
                >= int(LEASE_HEARTBEAT_INTERVAL_SECONDS * 1_000_000_000)
            ):
                if heartbeat_resource_lease(
                    self._encoding_lease_id,
                    owner_id=self._encoding_lease_owner,
                    now_ns=received_ns,
                ):
                    self._last_lease_heartbeat_ns = received_ns

    @staticmethod
    def _coerce_time_base(value: Any) -> Fraction | None:
        if value is None:
            return None
        try:
            result = Fraction(value)
            if not math.isfinite(float(result)):
                return None
            return result if result > 0 else None
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            return None

    def _rescale_pts(self, record: VideoFrameRecord) -> int:
        if record.pts is None or record.time_base is None:
            if self._first_pts_seconds is None:
                self._first_pts_seconds = float(record.captured_at)
            seconds = float(record.captured_at) - self._first_pts_seconds
        else:
            pts_seconds = float(record.pts) * float(record.time_base)
            if not math.isfinite(pts_seconds):
                pts_seconds = float(record.captured_at)
                if self._first_pts_seconds is None:
                    self._first_pts_seconds = pts_seconds
                seconds = pts_seconds - self._first_pts_seconds
            else:
                if self._first_pts_seconds is None:
                    self._first_pts_seconds = pts_seconds
                seconds = pts_seconds - self._first_pts_seconds
        if not math.isfinite(seconds) or seconds < 0:
            seconds = 0.0
            self._timestamp_adjustments += 1
        candidate = int(seconds * float(1 / VIDEO_TIME_BASE))
        if self._first_output_pts is None:
            self._first_output_pts = candidate
        if candidate <= self._last_pts:
            candidate = self._last_pts + self._frame_ticks()
            self._timestamp_adjustments += 1
        self._last_pts = candidate
        return candidate

    def _duration_seconds_locked(self) -> float:
        if self._last_pts < 0:
            return 0.0
        first = self._first_output_pts if self._first_output_pts is not None else 0
        ticks = max(0, self._last_pts - first + self._frame_ticks())
        duration = float(ticks) * float(VIDEO_TIME_BASE)
        if not math.isfinite(duration) or duration < 0:
            return 0.0
        return duration

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

    def _encode_record(self, record: VideoFrameRecord) -> None:
        self._ensure_container(record)
        frame = av.VideoFrame.from_ndarray(record.pixels, format=VIDEO_PIXEL_FORMAT)
        frame.pts = self._rescale_pts(record)
        frame.time_base = VIDEO_TIME_BASE
        for packet in self._stream.encode(frame):
            self._container.mux(packet)
        with self._lock:
            self._frames_encoded += 1
            self._bytes_written = self._buffer.tell()
            if self._bytes_written > self.max_bytes:
                self._overflowed = True
            if self._duration_seconds_locked() > float(self.max_seconds):
                self._overflowed = True

    def _consume(self) -> None:
        with self._lock:
            generation = self._generation
        stopping = False
        while True:
            if not stopping:
                try:
                    control = self._control_queue.get_nowait()
                    if control.generation == generation:
                        stopping = True
                except queue.Empty:
                    pass
            try:
                message = self._queue.get_nowait()
            except queue.Empty:
                if stopping:
                    return
                try:
                    control = self._control_queue.get(timeout=0.05)
                    if control.generation == generation:
                        stopping = True
                except queue.Empty:
                    continue
                continue
            if message.generation != generation:
                continue
            try:
                self._encode_record(message.record)
            except Exception as exc:
                self.set_error(f"Could not encode a camera frame: {exc}")
                return

    def finalize(self) -> bytes:
        with self._finalize_lock:
            with self._lock:
                if self._finalized:
                    return self._finalized_data or b""
                self._enabled = False
                if self._worker is not None and not self._stop_requested:
                    self._stop_requested = True
                    self._signal_stop_locked(self._generation)
                worker = self._worker
            if worker is not None:
                worker.join(timeout=10)
                if worker.is_alive():
                    with self._lock:
                        self._error = FINALIZE_TIMEOUT_ERROR
                        self._finalize_error = FINALIZE_TIMEOUT_ERROR
                    return b""
            with self._lock:
                if self._finalized:
                    return self._finalized_data or b""
                if self._finalize_error == FINALIZE_TIMEOUT_ERROR:
                    self._error = None
                    self._finalize_error = None
                try:
                    if (
                        self._stream is not None
                        and self._container is not None
                        and self._error is None
                        and not self._overflowed
                    ):
                        for packet in self._stream.encode():
                            self._container.mux(packet)
                except Exception as exc:
                    self._error = self._error or f"Could not finalize the video recording: {exc}"
                try:
                    if self._container is not None:
                        self._container.close()
                except Exception as exc:
                    self._error = self._error or f"Could not close the video file: {exc}"
                finally:
                    self._container = None
                    self._stream = None
                self._release_encoding_lease_locked()
                self._worker = None
                self._finalized = True
                data = b""
                if self._error is None and not self._overflowed:
                    data = bytes(self._buffer.getvalue())
                if data:
                    try:
                        validate_video_bytes(data)
                    except ValueError as exc:
                        self._error = self._error or str(exc)
                        data = b""
                if data:
                    self._finalized_data = data
                else:
                    self._finalize_error = self._error or "The camera recording is incomplete."
                return data

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "finalized": self._finalized,
                "generation": self._generation,
                "frames_queued": self._frames_queued,
                "frames_encoded": self._frames_encoded,
                "dropped_frames": self._dropped_frames,
                "queue_overflow_count": self._queue_overflow_count,
                "timestamp_adjustments": self._timestamp_adjustments,
                "bytes_written": self._bytes_written,
                "duration_seconds": round(self._duration_seconds_locked(), 3),
                "width": self._width,
                "height": self._height,
                "fps": self.fps,
                "codec": VIDEO_CODEC,
                "pixel_format": VIDEO_PIXEL_FORMAT,
                "container": VIDEO_CONTAINER,
                "overflowed": self._overflowed,
                "resource_lease_held": self._encoding_lease_held,
                "resource_lease_id": self._encoding_lease_id,
                "worker_alive": self._worker is not None and self._worker.is_alive(),
                "error": self._error,
                "first_received_monotonic_ns": self._first_received_monotonic_ns,
                "last_received_monotonic_ns": self._last_received_monotonic_ns,
            }

    def clear(self) -> None:
        with self._finalize_lock:
            with self._lock:
                if self._worker is not None and not self._stop_requested:
                    self._stop_requested = True
                    self._signal_stop_locked(self._generation)
                worker = self._worker
            if worker is not None:
                worker.join(timeout=10)
                if worker.is_alive():
                    with self._lock:
                        self._error = "The video encoder did not stop in time. Clear can be retried."
                    return
            with self._lock:
                if self._container is not None:
                    try:
                        self._container.close()
                    except Exception:
                        pass
                self._release_encoding_lease_locked()
                self._reset_locked()

    def _reset_locked(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        while True:
            try:
                self._control_queue.get_nowait()
            except queue.Empty:
                break
        self._buffer = io.BytesIO()
        self._container = None
        self._stream = None
        self._worker = None
        self._enabled = False
        self._stop_requested = False
        self._finalized = False
        self._finalize_error = None
        self._finalized_data = None
        self._error = None
        self._frames_queued = 0
        self._frames_encoded = 0
        self._dropped_frames = 0
        self._queue_overflow_count = 0
        self._timestamp_adjustments = 0
        self._bytes_written = 0
        self._first_pts_seconds = None
        self._first_output_pts = None
        self._last_pts = -1
        self._width = 0
        self._height = 0
        self._overflowed = False
        self._first_received_monotonic_ns = None
        self._last_received_monotonic_ns = None


def _validate_mp4_boxes(data: bytes) -> set[bytes]:
    offset = 0
    box_types: set[bytes] = set()
    while offset < len(data):
        if len(data) - offset < 8:
            raise ValueError("The camera recording is not a complete MP4 file.")
        size = int.from_bytes(data[offset : offset + 4], "big")
        box_type = bytes(data[offset + 4 : offset + 8])
        header_size = 8
        if size == 1:
            if len(data) - offset < 16:
                raise ValueError("The camera recording is not a complete MP4 file.")
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header_size = 16
        elif size == 0:
            size = len(data) - offset
        if size < header_size or offset + size > len(data):
            raise ValueError("The camera recording is not a complete MP4 file.")
        box_types.add(box_type)
        offset += size
    if b"ftyp" not in box_types or b"moov" not in box_types or b"mdat" not in box_types:
        raise ValueError("The camera recording is not a complete MP4 file.")
    return box_types


def validate_video_bytes(data: bytes) -> tuple[int, str]:
    if not data:
        raise ValueError("The camera recording is empty.")
    if len(data) < 12 or data[4:8] != b"ftyp":
        raise ValueError("The camera recording is not a valid MP4 file.")
    _validate_mp4_boxes(data)
    try:
        with av.open(io.BytesIO(data), mode="r") as container:
            if not container.streams.video:
                raise ValueError("The camera recording does not contain a video stream.")
            stream = container.streams.video[0]
            codec = getattr(getattr(stream, "codec_context", None), "name", None)
            if not codec:
                raise ValueError("The camera recording has no readable video stream.")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The camera recording is not a complete MP4 file.") from exc
    digest = hashlib.sha256(data).hexdigest()
    return len(data), digest


def shutdown_all_video_recorders() -> int:
    with _ACTIVE_VIDEO_RECORDERS_LOCK:
        recorders = list(_ACTIVE_VIDEO_RECORDERS)
    cleared = 0
    for recorder in recorders:
        try:
            recorder.clear()
            cleared += 1
        except Exception as exc:
            logger.warning("Could not clear a live video recorder at shutdown: %s", exc)
    return cleared
