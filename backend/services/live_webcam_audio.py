from __future__ import annotations

import io
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass

import numpy as np
from services.job_interview_constants import MAX_ANSWER_SECONDS

MAX_LIVE_AUDIO_BYTES = 64 * 1024 * 1024
MAX_LIVE_AUDIO_CHUNKS = 32768
AUDIO_SAMPLE_WIDTH = 2
NORMALIZED_SAMPLE_RATE = 16000
NORMALIZED_CHANNELS = 1
NORMALIZED_SAMPLE_FORMAT = "s16"


@dataclass(frozen=True)
class AudioChunk:
    pcm: bytes
    sample_rate: int
    channels: int
    samples: int


def _as_array(frame) -> np.ndarray:
    array = np.asarray(frame.to_ndarray())
    if array.ndim == 1:
        array = array.reshape(1, -1)
    if array.ndim != 2 or array.shape[0] < 1 or array.shape[1] < 1:
        raise ValueError("Unsupported audio frame shape.")
    return array


def _geometry(frame, array: np.ndarray) -> tuple[int, int, np.ndarray]:
    layout = getattr(frame, "layout", None)
    channels = len(getattr(layout, "channels", ()) or ()) or int(getattr(frame, "channels", 0) or 0)
    samples = int(getattr(frame, "samples", 0) or 0)
    if channels < 1:
        channels = int(array.shape[0])
    if samples < 1:
        samples = int(array.shape[1] // max(channels, 1))
    if array.shape[0] == channels and array.shape[1] == samples:
        return channels, samples, array
    if array.shape[0] == 1 and channels > 1 and array.shape[1] == samples * channels:
        return channels, samples, array.reshape(channels, samples)
    if channels > 1 and array.shape[0] == samples and array.shape[1] == channels:
        return channels, samples, array.T
    raise ValueError("Audio frame array does not match its channel layout.")


def _to_s16(array: np.ndarray) -> np.ndarray:
    if np.issubdtype(array.dtype, np.floating):
        array = np.clip(array, -1.0, 1.0) * 32767.0
    elif np.issubdtype(array.dtype, np.unsignedinteger):
        array = np.clip(array.astype(np.int32) - 32768, -32768, 32767)
    elif np.issubdtype(array.dtype, np.signedinteger):
        array = np.clip(array, -32768, 32767)
    else:
        raise ValueError("Unsupported audio sample dtype.")
    return np.ascontiguousarray(array.astype("<i2", copy=False))


def frame_to_chunk(frame) -> AudioChunk:
    array = _as_array(frame)
    channels, samples, array = _geometry(frame, array)
    array = _to_s16(array)
    sample_rate = int(getattr(frame, "sample_rate", 0) or 0)
    if sample_rate < 1:
        raise ValueError("Audio frame is missing a sample rate.")
    pcm = array.T.tobytes() if channels > 1 else np.ascontiguousarray(array[0]).tobytes()
    return AudioChunk(pcm=pcm, sample_rate=sample_rate, channels=channels, samples=samples)


class AudioNormalizer:
    def __init__(self):
        try:
            import av
        except ImportError as exc:
            raise ValueError("Audio normalization requires the installed 'av' package.") from exc
        self._resampler = av.AudioResampler(
            format="s16", layout="mono", rate=NORMALIZED_SAMPLE_RATE
        )

    def resample(self, frame) -> list[AudioChunk]:
        frames = self._resampler.resample(frame) or []
        return [
            frame_to_chunk(item)
            for item in frames
            if int(getattr(item, "samples", 0) or 0) > 0
        ]

    def flush(self) -> list[AudioChunk]:
        frames = self._resampler.resample(None) or []
        return [
            frame_to_chunk(item)
            for item in frames
            if int(getattr(item, "samples", 0) or 0) > 0
        ]


class LiveAudioRecorder:
    def __init__(
        self,
        *,
        max_seconds: int = MAX_ANSWER_SECONDS,
        max_bytes: int = MAX_LIVE_AUDIO_BYTES,
        max_chunks: int = MAX_LIVE_AUDIO_CHUNKS,
    ):
        if int(max_seconds) < 1 or int(max_bytes) < 1 or int(max_chunks) < 1:
            raise ValueError("Recorder limits must be positive.")
        self._max_seconds = int(max_seconds)
        self._max_bytes = int(max_bytes)
        self._max_chunks = int(max_chunks)
        self._chunks: deque[AudioChunk] = deque()
        self._lock = threading.Lock()
        self._sample_rate: int | None = None
        self._channels: int | None = None
        self._total_samples = 0
        self._total_bytes = 0
        self._dropped_chunks = 0
        self._overflowed = False
        self._enabled = False
        self._capture_error: str | None = None
        self._flushed = False
        self._normalizer = AudioNormalizer()
        self._first_received_monotonic_ns: int | None = None
        self._last_received_monotonic_ns: int | None = None

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)

    @property
    def capture_error(self) -> str | None:
        with self._lock:
            return self._capture_error

    def set_capture_error(self, message: str | None) -> None:
        with self._lock:
            self._capture_error = str(message) if message else None

    def append_frame(self, frame) -> None:
        received_ns = time.monotonic_ns()
        with self._lock:
            if not self._enabled or self._flushed:
                return
            try:
                chunks = self._normalizer.resample(frame)
            except Exception as exc:
                self._capture_error = f"Audio conversion failed: {exc}"
                return
            for chunk in chunks:
                self._append_chunk_locked(chunk, received_ns=received_ns)

    def flush(self) -> None:
        with self._lock:
            if self._flushed:
                return
            self._flushed = True
            try:
                chunks = self._normalizer.flush()
            except Exception as exc:
                self._capture_error = f"Audio finalization failed: {exc}"
                return
            for chunk in chunks:
                self._append_chunk_locked(chunk)

    def append_chunk(self, chunk: AudioChunk) -> None:
        with self._lock:
            self._append_chunk_locked(chunk, received_ns=time.monotonic_ns())

    def _append_chunk_locked(self, chunk: AudioChunk, *, received_ns: int | None = None) -> None:
        if not isinstance(chunk, AudioChunk):
            raise TypeError("chunk must be an AudioChunk")
        if chunk.sample_rate < 1 or chunk.channels < 1 or chunk.samples < 1:
            raise ValueError("Invalid audio chunk dimensions.")
        expected_bytes = chunk.samples * chunk.channels * AUDIO_SAMPLE_WIDTH
        if len(chunk.pcm) != expected_bytes:
            raise ValueError("Audio chunk payload does not match its dimensions.")
        if not self._enabled:
            return
        if self._sample_rate is None:
            self._sample_rate = int(chunk.sample_rate)
            self._channels = int(chunk.channels)
        elif chunk.sample_rate != self._sample_rate or chunk.channels != self._channels:
            self._dropped_chunks += 1
            return
        remaining_samples = int(self._max_seconds * self._sample_rate) - self._total_samples
        if remaining_samples <= 0:
            self._overflowed = True
            return
        allowed_samples = min(chunk.samples, remaining_samples)
        remaining_bytes = self._max_bytes - self._total_bytes
        if remaining_bytes <= 0:
            self._overflowed = True
            return
        allowed_samples = min(
            allowed_samples,
            remaining_bytes // (int(self._channels) * AUDIO_SAMPLE_WIDTH),
        )
        if allowed_samples <= 0:
            self._overflowed = True
            return
        if len(self._chunks) >= self._max_chunks:
            self._overflowed = True
            self._dropped_chunks += 1
            return
        if allowed_samples < chunk.samples:
            keep_bytes = allowed_samples * int(self._channels) * AUDIO_SAMPLE_WIDTH
            chunk = AudioChunk(
                pcm=chunk.pcm[:keep_bytes],
                sample_rate=chunk.sample_rate,
                channels=chunk.channels,
                samples=allowed_samples,
            )
            self._overflowed = True
        self._chunks.append(chunk)
        self._total_samples += chunk.samples
        self._total_bytes += len(chunk.pcm)
        if received_ns is not None:
            if self._first_received_monotonic_ns is None:
                self._first_received_monotonic_ns = int(received_ns)
            self._last_received_monotonic_ns = int(received_ns)

    def to_wav_bytes(self) -> bytes:
        self.flush()
        with self._lock:
            if not self._chunks or self._sample_rate is None or self._channels is None:
                return b""
            payload = b"".join(chunk.pcm for chunk in self._chunks)
            sample_rate = int(self._sample_rate)
            channels = int(self._channels)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(AUDIO_SAMPLE_WIDTH)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(payload)
        return buffer.getvalue()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            duration = self._total_samples / self._sample_rate if self._sample_rate else 0.0
            return {
                "sample_rate": self._sample_rate or NORMALIZED_SAMPLE_RATE,
                "channels": self._channels or NORMALIZED_CHANNELS,
                "sample_format": NORMALIZED_SAMPLE_FORMAT,
                "duration_seconds": round(duration, 3),
                "byte_count": self._total_bytes,
                "chunk_count": len(self._chunks),
                "dropped_chunks": self._dropped_chunks,
                "overflowed": self._overflowed,
                "enabled": self._enabled,
                "capture_error": self._capture_error,
                "flushed": self._flushed,
                "first_received_monotonic_ns": self._first_received_monotonic_ns,
                "last_received_monotonic_ns": self._last_received_monotonic_ns,
            }

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._sample_rate = None
            self._channels = None
            self._total_samples = 0
            self._total_bytes = 0
            self._dropped_chunks = 0
            self._overflowed = False
            self._enabled = False
            self._capture_error = None
            self._flushed = False
            self._normalizer = AudioNormalizer()
            self._first_received_monotonic_ns = None
            self._last_received_monotonic_ns = None
