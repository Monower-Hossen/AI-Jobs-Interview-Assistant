from __future__ import annotations

import io
import math
import os
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np
import pytest
from config.settings import settings
from database.db import get_db_context
from database.models import LiveInterviewMedia, LiveInterviewRecording
from services.auth_service import AuthError
from services.live_interview_media_service import (
    LiveMediaError,
    delete_live_media,
    delete_live_recording,
    ensure_live_recording,
    list_admin_recordings,
    list_user_recordings,
    read_live_media,
    recording_details,
    retry_pending_deletions,
    save_live_media,
    verify_live_media,
)
from services.live_interview_state import (
    RECORDING_ACTIVE,
    get_video_recorder,
    initialize_live_interview_state,
    start_recording,
    stop_recording,
)
from services.live_recording_manifest import (
    CAPTURE_MANIFEST_KEY,
    create_capture_manifest,
    update_audio_manifest,
)
from services.live_webcam_video import (
    VIDEO_DURATION_TOLERANCE_SECONDS,
    LiveVideoRecorder,
    _StopMessage,
    validate_video_bytes,
)
from streamlit.testing.v1 import AppTest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _fake_acquire_resource_lease(*args, **kwargs):
    return SimpleNamespace(lease_id="test-lease"), None


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x01\x00" * 1_600)
    return buffer.getvalue()


_MP4_BYTES: bytes | None = None


def _mp4_bytes() -> bytes:
    global _MP4_BYTES
    if _MP4_BYTES is not None:
        return _MP4_BYTES
    buffer = io.BytesIO()
    with av.open(buffer, mode="w", format="mp4") as container:
        stream = container.add_stream("libx264", rate=30)
        stream.pix_fmt = "yuv420p"
        stream.width = 64
        stream.height = 64
        stream.time_base = Fraction(1, 90_000)
        stream.codec_context.options = {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "crf": "26",
        }
        frame = av.VideoFrame.from_ndarray(
            np.full((64, 64, 3), 120, dtype=np.uint8), format="rgb24"
        )
        frame.time_base = Fraction(1, 90_000)
        for index in range(3):
            frame.pts = index * 3000
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    _MP4_BYTES = buffer.getvalue()
    return _MP4_BYTES


@pytest.fixture
def media_db(tmp_path):
    import database.db as db_module
    from database import User, UserSession
    from services import auth_service as auth

    original_database = settings.database_path
    original_media_root = settings.live_media_root
    settings.database_path = str(tmp_path / "media.db")
    settings.live_media_root = str(tmp_path / "live_interviews")
    db_module.engine = None
    db_module.SessionLocal = None
    db_module.init_database()

    with db_module.get_db_context() as db:
        admin = User(
            email="admin@media.com",
            username="admin",
            full_name="Media Admin",
            password_hash=auth.hash_password("Pass1234"),
            role="admin",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        user = User(
            email="owner@media.com",
            username="owner",
            full_name="Media Owner",
            password_hash=auth.hash_password("Pass1234"),
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        other = User(
            email="other@media.com",
            username="other",
            full_name="Other User",
            password_hash=auth.hash_password("Pass1234"),
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        db.add_all([admin, user, other])
        db.flush()
        workspace = UserSession(session_id="media-token", user_id=user.id, label="owner")
        other_workspace = UserSession(
            session_id="other-token", user_id=other.id, label="other"
        )
        db.add_all([workspace, other_workspace])
        db.flush()
        ids = {
            "admin_id": int(admin.id),
            "user_id": int(user.id),
            "other_id": int(other.id),
            "token": "media-token",
            "other_token": "other-token",
            "workspace_id": int(workspace.id),
        }
    yield ids

    db_module.close_database()
    db_module.SessionLocal = None
    settings.database_path = original_database
    settings.live_media_root = original_media_root


def _create_interview(media_db, *, report=None, workspace_id: int | None = None) -> int:
    from database import InterviewSession, get_db_context

    with get_db_context() as db:
        interview = InterviewSession(
            session_id=int(workspace_id if workspace_id is not None else media_db["workspace_id"]),
            interview_type="live_webcam",
            interview_mode="live_webcam",
            answer_mode="audio",
            status="completed",
            report_json=report or {"answers": []},
        )
        db.add(interview)
        db.flush()
        return int(interview.id)


def test_video_recorder_writes_valid_mp4(monkeypatch) -> None:
    from services import live_webcam_video

    monkeypatch.setattr(live_webcam_video, "acquire_resource_lease", _fake_acquire_resource_lease)
    monkeypatch.setattr(live_webcam_video, "end_heavy_task", lambda task, **kwargs: None)

    recorder = LiveVideoRecorder(max_frames=5, queue_maxsize=10)
    frame = av.VideoFrame.from_ndarray(
        np.full((64, 64, 3), 120, dtype=np.uint8), format="rgb24"
    )
    frame.time_base = Fraction(1, 30)
    frame.pts = 0
    recorder.set_enabled(True)
    for pts in range(5):
        frame.pts = pts
        recorder.enqueue(frame)

    data = recorder.finalize()
    size, checksum = validate_video_bytes(data)

    assert size == len(data) > 0
    assert len(checksum) == 64
    with av.open(io.BytesIO(data)) as container:
        stream = container.streams.video[0]
        assert stream.codec_context.name == "h264"
        assert any(True for _ in container.decode(stream))
    assert recorder.snapshot()["frames_encoded"] >= 1
    recorder.clear()
    assert recorder.snapshot()["frames_encoded"] == 0


@pytest.fixture
def _deterministic_video_guardrail(monkeypatch):
    from services import live_webcam_video

    monkeypatch.setattr(live_webcam_video, "acquire_resource_lease", _fake_acquire_resource_lease)
    monkeypatch.setattr(live_webcam_video, "end_heavy_task", lambda task, **kwargs: None)


def _video_frame(*, pts: int | None, time_base: Fraction = Fraction(1, 30), shade: int = 120):
    frame = av.VideoFrame.from_ndarray(
        np.full((64, 64, 3), shade, dtype=np.uint8), format="rgb24"
    )
    frame.time_base = time_base
    frame.pts = pts
    return frame


class _FakeWorker:
    def __init__(self, *, alive: bool = True):
        self.alive = alive
        self.join_calls: list[float | None] = []

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        self.join_calls.append(timeout)


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.mark.parametrize(
    ("time_base", "step", "frames", "expected"),
    [
        (Fraction(1, 90_000), 3000, 40, 40 / 30),
        (Fraction(1, 30), 1, 40, 40 / 30),
        (Fraction(1, 90_000), 3000, 300, 10.0),
    ],
)
def test_video_duration_uses_pts_time_base(time_base, step, frames, expected) -> None:
    recorder = LiveVideoRecorder(fps=30, max_seconds=60)
    start = time.time()
    for index in range(frames):
        recorder._rescale_pts(
            SimpleNamespace(
                pts=index * step,
                time_base=time_base,
                captured_at=start + index / 30,
            )
        )
        recorder._frames_encoded += 1

    duration = recorder.snapshot()["duration_seconds"]

    assert duration == pytest.approx(expected, abs=VIDEO_DURATION_TOLERANCE_SECONDS)


def test_video_duration_handles_bad_and_non_monotonic_timestamps() -> None:
    recorder = LiveVideoRecorder(fps=30, max_seconds=60)
    start = time.time()
    for index, pts in enumerate([0, 1, 1, 0, 2]):
        recorder._rescale_pts(
            SimpleNamespace(pts=pts, time_base=Fraction(1, 30), captured_at=start + index / 30)
        )
    snapshot = recorder.snapshot()
    assert math.isfinite(snapshot["duration_seconds"])
    assert snapshot["duration_seconds"] >= 0.0
    assert snapshot["timestamp_adjustments"] >= 3

    missing = LiveVideoRecorder(fps=30, max_seconds=60)
    for index in range(5):
        missing._rescale_pts(
            SimpleNamespace(pts=None, time_base=None, captured_at=start + index / 30)
        )
    assert missing.snapshot()["duration_seconds"] == pytest.approx(
        5 / 30, abs=VIDEO_DURATION_TOLERANCE_SECONDS
    )

    zero = LiveVideoRecorder(fps=30, max_seconds=60)
    assert zero.snapshot()["duration_seconds"] == 0.0

    negative = LiveVideoRecorder(fps=30, max_seconds=60)
    negative._rescale_pts(
        SimpleNamespace(pts=-5000, time_base=Fraction(1, 30), captured_at=start)
    )
    assert math.isfinite(negative.snapshot()["duration_seconds"])
    assert negative.snapshot()["duration_seconds"] >= 0.0


def test_video_duration_flags_recording_over_maximum() -> None:
    recorder = LiveVideoRecorder(fps=30, max_seconds=1)
    start = time.time()
    for index in range(40):
        recorder._rescale_pts(
            SimpleNamespace(
                pts=index, time_base=Fraction(1, 30), captured_at=start + index / 30
            )
        )

    assert recorder._duration_seconds_locked() > recorder.max_seconds


def test_media_service_rejects_duration_over_maximum(
    media_db, _deterministic_media_service_guardrail
) -> None:
    interview_id = _create_interview(media_db)
    recording_id = ensure_live_recording(
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        consent_given=True,
    )

    with pytest.raises(LiveMediaError, match="exceeds"):
        save_live_media(
            recording_id=recording_id,
            interview_id=interview_id,
            user_id=media_db["user_id"],
            session_token=media_db["token"],
            media_type="video",
            data=_mp4_bytes(),
            duration_seconds=float(settings.live_media_max_seconds) + 1.0,
        )


def test_short_video_duration_is_not_rejected_by_storage(
    media_db, _deterministic_video_guardrail, _deterministic_media_service_guardrail
) -> None:
    recorder = LiveVideoRecorder(max_frames=10, queue_maxsize=10)
    recorder.set_enabled(True)
    for pts in range(5):
        recorder.enqueue(_video_frame(pts=pts * 3000, time_base=Fraction(1, 90_000)))
    data = recorder.finalize()
    duration = recorder.snapshot()["duration_seconds"]

    assert data
    assert duration == pytest.approx(5 / 30, abs=VIDEO_DURATION_TOLERANCE_SECONDS)

    interview_id = _create_interview(media_db)
    recording_id = ensure_live_recording(
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        consent_given=True,
    )
    save_live_media(
        recording_id=recording_id,
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        media_type="video",
        data=data,
        duration_seconds=duration,
    )
    recorder.clear()


def test_video_queue_overflow_drops_oldest_and_preserves_stop(
    monkeypatch, _deterministic_video_guardrail
) -> None:
    started = threading.Event()
    release = threading.Event()
    recorder = LiveVideoRecorder(queue_maxsize=3)
    original_consume = recorder._consume

    def controlled_consume() -> None:
        started.set()
        assert release.wait(2.0)
        original_consume()

    monkeypatch.setattr(recorder, "_consume", controlled_consume)
    recorder.set_enabled(True)
    assert started.wait(1.0)
    for pts in range(7):
        recorder.enqueue(_video_frame(pts=pts))

    with recorder._lock:
        queued_pts = [message.record.pts for message in list(recorder._queue.queue)]
    assert queued_pts == [4, 5, 6]
    assert recorder.snapshot()["dropped_frames"] == 4
    assert recorder.snapshot()["queue_overflow_count"] == 4

    recorder.set_enabled(False)
    assert recorder._control_queue.qsize() == 1
    with recorder._lock:
        assert [message.record.pts for message in list(recorder._queue.queue)] == [4, 5, 6]

    release.set()
    data = recorder.finalize()
    assert data
    assert recorder.snapshot()["frames_encoded"] == 3
    assert recorder.snapshot()["error"] is None


def test_finalize_does_not_close_container_while_worker_is_alive(monkeypatch) -> None:
    from services import live_webcam_video

    ended: list[str] = []
    monkeypatch.setattr(live_webcam_video, "end_heavy_task", lambda task, **kwargs: ended.append(task))
    recorder = LiveVideoRecorder()
    worker = _FakeWorker(alive=True)
    closes: list[int] = []
    recorder._worker = worker
    recorder._container = SimpleNamespace(close=lambda: closes.append(1))
    recorder._stream = SimpleNamespace(encode=list)
    recorder._encoding_lease_held = True

    data = recorder.finalize()

    assert data == b""
    assert closes == []
    assert ended == []
    assert recorder._encoding_lease_held is True
    assert worker.join_calls == [10]
    assert "did not stop" in (recorder.snapshot()["error"] or "")


def test_finalize_timeout_can_be_retried_without_duplicate_release(
    monkeypatch, _deterministic_video_guardrail
) -> None:
    from services import live_webcam_video

    ended: list[str] = []
    monkeypatch.setattr(live_webcam_video, "end_heavy_task", lambda task, **kwargs: ended.append(task))
    recorder = LiveVideoRecorder()
    worker = _FakeWorker(alive=True)
    closes: list[int] = []
    recorder._worker = worker
    recorder._container = SimpleNamespace(close=lambda: closes.append(1))
    recorder._stream = SimpleNamespace(encode=list)
    recorder._buffer = io.BytesIO(_mp4_bytes())
    recorder._encoding_lease_held = True

    first = recorder.finalize()

    assert first == b""
    assert closes == []
    assert ended == []
    assert worker.join_calls == [10]
    assert "did not stop" in (recorder.snapshot()["error"] or "")
    assert recorder.snapshot()["finalized"] is False
    assert recorder._encoding_lease_held is True

    worker.alive = False
    second = recorder.finalize()

    assert second == _mp4_bytes()
    assert closes == [1]
    assert ended == ["video_processing"]
    assert worker.join_calls == [10, 10]
    assert recorder.snapshot()["finalized"] is True
    assert recorder.snapshot()["error"] is None
    assert recorder._encoding_lease_held is False


def test_finalize_releases_lease_once_and_repeated_calls_are_cached(
    monkeypatch, _deterministic_video_guardrail
) -> None:
    from services import live_webcam_video

    ended: list[str] = []
    monkeypatch.setattr(live_webcam_video, "end_heavy_task", lambda task, **kwargs: ended.append(task))
    recorder = LiveVideoRecorder(max_frames=10, queue_maxsize=10)
    recorder.set_enabled(True)
    for pts in range(5):
        recorder.enqueue(_video_frame(pts=pts))

    first = recorder.finalize()
    second = recorder.finalize()
    recorder.clear()

    assert first
    assert first == second
    assert ended == ["video_processing"]
    assert recorder._encoding_lease_held is False


def test_concurrent_finalize_closes_container_once() -> None:
    recorder = LiveVideoRecorder()
    recorder._buffer = io.BytesIO(_mp4_bytes())
    closes: list[int] = []
    recorder._container = SimpleNamespace(close=lambda: closes.append(1))
    recorder._stream = SimpleNamespace(encode=list)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: recorder.finalize(), range(2)))
    third = recorder.finalize()

    assert results[0]
    assert results[0] == results[1] == third == _mp4_bytes()
    assert closes == [1]


def test_failed_finalization_paths_never_return_partial_mp4(monkeypatch) -> None:
    def _raise(*args, **kwargs):
        raise RuntimeError("encoder failure")

    empty = LiveVideoRecorder()
    assert empty.finalize() == b""
    with pytest.raises(ValueError):
        validate_video_bytes(b"")

    flush = LiveVideoRecorder()
    flush._buffer = io.BytesIO(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 40)
    flush._container = SimpleNamespace(close=lambda: None)
    flush._stream = SimpleNamespace(encode=_raise)
    assert flush.finalize() == b""
    assert flush.finalize() == b""
    assert "Could not finalize" in (flush.snapshot()["error"] or "")

    close_failure = LiveVideoRecorder()
    close_failure._buffer = io.BytesIO(_mp4_bytes())
    close_failure._container = SimpleNamespace(close=_raise)
    close_failure._stream = SimpleNamespace(encode=list)
    assert close_failure.finalize() == b""

    with pytest.raises(ValueError):
        validate_video_bytes(_mp4_bytes()[:-24])
    with pytest.raises(ValueError):
        validate_video_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 40)


def test_worker_failure_restart_ignores_stale_generation_messages(
    monkeypatch, _deterministic_video_guardrail
) -> None:
    from services import live_webcam_video

    ended: list[str] = []
    monkeypatch.setattr(live_webcam_video, "end_heavy_task", lambda task, **kwargs: ended.append(task))
    recorder = LiveVideoRecorder(queue_maxsize=10)
    original_ensure = recorder._ensure_container
    failed = False

    def failing_ensure(record) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("encoder boom")
        original_ensure(record)

    monkeypatch.setattr(recorder, "_ensure_container", failing_ensure)
    recorder.set_enabled(True)
    old_generation = recorder.generation
    recorder.enqueue(_video_frame(pts=0))
    assert _wait_until(lambda: recorder.snapshot()["error"] is not None)
    recorder.clear()
    assert ended == ["video_processing"]

    recorder.set_enabled(True)
    new_generation = recorder.generation
    assert new_generation != old_generation
    recorder._control_queue.put(_StopMessage(generation=old_generation))
    recorder.enqueue(_video_frame(pts=1000), generation=old_generation)
    recorder.enqueue(_video_frame(pts=0), generation=new_generation)

    data = recorder.finalize()

    assert data
    assert recorder.snapshot()["frames_encoded"] == 1
    assert recorder.snapshot()["error"] is None


def test_thread_constructor_failure_releases_lease(monkeypatch) -> None:
    from services import live_webcam_video

    released: list[str] = []
    monkeypatch.setattr(live_webcam_video, "acquire_resource_lease", _fake_acquire_resource_lease)
    monkeypatch.setattr(
        live_webcam_video,
        "release_resource_lease",
        lambda lease_id, **kwargs: released.append(lease_id) or True,
    )

    class FailingThread:
        def __init__(self, **kwargs):
            raise RuntimeError("constructor failed")

    monkeypatch.setattr(live_webcam_video.threading, "Thread", FailingThread)
    recorder = LiveVideoRecorder()
    recorder.set_enabled(True)

    assert released == ["test-lease"]
    assert recorder._encoding_lease_held is False
    assert recorder._worker is None
    assert recorder.enabled is False
    assert "Could not start the video encoder" in (recorder.snapshot()["error"] or "")


def test_thread_start_failure_releases_lease(monkeypatch) -> None:
    from services import live_webcam_video

    released: list[str] = []
    monkeypatch.setattr(live_webcam_video, "acquire_resource_lease", _fake_acquire_resource_lease)
    monkeypatch.setattr(
        live_webcam_video,
        "release_resource_lease",
        lambda lease_id, **kwargs: released.append(lease_id) or True,
    )

    class FailingStartThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self) -> None:
            raise RuntimeError("start failed")

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(live_webcam_video.threading, "Thread", FailingStartThread)
    recorder = LiveVideoRecorder()
    recorder.set_enabled(True)

    assert released == ["test-lease"]
    assert recorder._encoding_lease_held is False
    assert recorder._worker is None
    assert recorder.enabled is False
    assert "Could not start the video encoder" in (recorder.snapshot()["error"] or "")


def test_media_service_rejects_incomplete_mp4(
    media_db, _deterministic_media_service_guardrail
) -> None:
    interview_id = _create_interview(media_db)
    recording_id = ensure_live_recording(
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        consent_given=True,
    )

    with pytest.raises(LiveMediaError, match="not a valid MP4"):
        save_live_media(
            recording_id=recording_id,
            interview_id=interview_id,
            user_id=media_db["user_id"],
            session_token=media_db["token"],
            media_type="video",
            data=b"\x00\x00\x00\x20ftypisom" + b"\x00" * 40,
            duration_seconds=1.0,
        )


@pytest.fixture
def _deterministic_media_service_guardrail(monkeypatch):
    from contextlib import nullcontext

    import services.live_interview_media_service as media_service

    monkeypatch.setattr(media_service, "acquire_heavy_task", lambda *a, **k: nullcontext())


def test_recording_state_enables_and_disables_video(_deterministic_video_guardrail) -> None:
    state: dict = {}
    initialize_live_interview_state(state)

    start_recording(state, 1, record_video=True)

    assert state[RECORDING_ACTIVE] is True
    assert get_video_recorder(state).enabled is True

    stop_recording(state)

    assert state[RECORDING_ACTIVE] is False
    assert get_video_recorder(state).enabled is False
    get_video_recorder(state).clear()


def test_media_service_save_read_verify_list_and_delete(media_db, _deterministic_media_service_guardrail) -> None:
    interview_id = _create_interview(
        media_db, report={"answers": [{"question_number": 1, "answer": "answer"}]}
    )
    recording_id = ensure_live_recording(
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        consent_given=True,
        consent_at=datetime.now(UTC).replace(tzinfo=None).isoformat(),
    )
    manifest = update_audio_manifest(
        create_capture_manifest(recording_id=recording_id, interview_session_id=interview_id),
        {
            "chunk_count": 2,
            "dropped_chunks": 0,
            "duration_seconds": 1.0,
            "sample_rate": 16000,
            "channels": 1,
            "sample_format": "s16",
            "byte_count": len(_wav_bytes()),
            "first_received_monotonic_ns": 100,
            "last_received_monotonic_ns": 200,
        },
        status="finalized",
    )
    audio_id = save_live_media(
        recording_id=recording_id,
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        media_type="audio",
        data=_wav_bytes(),
        duration_seconds=1.0,
        metadata={"question_number": 1, CAPTURE_MANIFEST_KEY: manifest},
    )
    video_id = save_live_media(
        recording_id=recording_id,
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        media_type="video",
        data=_mp4_bytes(),
        duration_seconds=1.0,
        metadata={"question_number": 1, CAPTURE_MANIFEST_KEY: manifest},
    )

    audio, mime, filename = read_live_media(
        audio_id,
        actor_user_id=media_db["user_id"],
        session_token=media_db["token"],
    )
    assert audio.startswith(b"RIFF")
    assert mime == "audio/wav"
    assert filename.startswith("recording") and filename.endswith(".wav")
    assert verify_live_media(
        video_id,
        actor_user_id=media_db["user_id"],
        session_token=media_db["token"],
    )["checksum_valid"]

    saved = list_user_recordings(
        user_id=media_db["user_id"], session_token=media_db["token"]
    )
    assert len(saved) == 1
    assert {item["media_type"] for item in saved[0]["media"]} == {"audio", "video"}
    assert all(item["available"] for item in saved[0]["media"])
    assert saved[0]["capture_manifest"]["schema_version"] == 1
    assert saved[0]["media"][0]["metadata"][CAPTURE_MANIFEST_KEY]["recording_id"] == str(recording_id)

    details = recording_details(
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
    )
    assert details["report"]["answers"][0]["answer"] == "answer"

    admin_rows = list_admin_recordings(
        actor_admin_id=media_db["admin_id"], search="owner@"
    )
    assert [int(row["id"]) for row in admin_rows] == [recording_id]

    delete_live_media(
        video_id,
        actor_user_id=media_db["user_id"],
        session_token=media_db["token"],
    )
    remaining = list_user_recordings(
        user_id=media_db["user_id"], session_token=media_db["token"]
    )
    assert [item["media_type"] for item in remaining[0]["media"]] == ["audio"]

    delete_live_recording(
        recording_id,
        actor_user_id=media_db["user_id"],
        session_token=media_db["token"],
    )
    assert list_user_recordings(
        user_id=media_db["user_id"], session_token=media_db["token"]
    ) == []


def test_media_service_rejects_other_user_and_invalid_media(media_db) -> None:
    interview_id = _create_interview(media_db)
    recording_id = ensure_live_recording(
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        consent_given=True,
    )

    with pytest.raises(LiveMediaError):
        ensure_live_recording(
            interview_id=interview_id,
            user_id=media_db["other_id"],
            session_token=media_db["other_token"],
            consent_given=True,
        )
    with pytest.raises(AuthError):
        ensure_live_recording(
            interview_id=interview_id,
            user_id=media_db["other_id"],
            session_token=media_db["token"],
            consent_given=True,
        )
    with pytest.raises(LiveMediaError):
        save_live_media(
            recording_id=recording_id,
            interview_id=interview_id,
            user_id=media_db["user_id"],
            session_token=media_db["token"],
            media_type="audio",
            data=b"not-wav",
            duration_seconds=1.0,
        )
    with pytest.raises(LiveMediaError):
        save_live_media(
            recording_id=recording_id,
            interview_id=interview_id,
            user_id=media_db["user_id"],
            session_token=media_db["token"],
            media_type="video",
            data=b"not-mp4",
            duration_seconds=1.0,
        )


def test_media_service_manifest_backward_compat_and_invalid(media_db, _deterministic_media_service_guardrail) -> None:
    interview_id = _create_interview(media_db)
    recording_id = ensure_live_recording(
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        consent_given=True,
    )
    save_live_media(
        recording_id=recording_id,
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        media_type="audio",
        data=_wav_bytes(),
        duration_seconds=1.0,
        metadata={"question_number": 1},
    )
    rows = list_user_recordings(user_id=media_db["user_id"], session_token=media_db["token"])
    assert rows[0]["capture_manifest"] is None

    with pytest.raises(LiveMediaError):
        save_live_media(
            recording_id=recording_id,
            interview_id=interview_id,
            user_id=media_db["user_id"],
            session_token=media_db["token"],
            media_type="video",
            data=_mp4_bytes(),
            duration_seconds=1.0,
            metadata={CAPTURE_MANIFEST_KEY: {"schema_version": 999}},
        )


def _create_recording_with_media(
    media_db,
    *,
    user_id: int,
    token: str,
    question_number: int,
    manifest: dict | None = None,
    audio_meta_extra: dict | None = None,
    interview_report: dict | None = None,
) -> tuple[int, int]:
    from database import UserSession, get_db_context

    with get_db_context() as db:
        ws = db.query(UserSession).filter(UserSession.session_id == str(token), UserSession.user_id == int(user_id)).first()
        assert ws is not None
        workspace_id = int(ws.id)
    interview_id = _create_interview(media_db, report=interview_report, workspace_id=workspace_id)
    recording_id = ensure_live_recording(
        interview_id=interview_id,
        user_id=user_id,
        session_token=token,
        consent_given=True,
        consent_at=datetime.now(UTC).replace(tzinfo=None).isoformat(),
    )
    audio_meta = {"question_number": question_number, **(audio_meta_extra or {})}
    if manifest is not None:
        audio_meta[CAPTURE_MANIFEST_KEY] = manifest
    save_live_media(
        recording_id=recording_id,
        interview_id=interview_id,
        user_id=user_id,
        session_token=token,
        media_type="audio",
        data=_wav_bytes(),
        duration_seconds=1.0,
        metadata=audio_meta,
    )
    return interview_id, recording_id


def test_list_admin_recordings_returns_all_rows_and_metadata(media_db, _deterministic_media_service_guardrail) -> None:
    m1 = create_capture_manifest(recording_id="r1", interview_session_id="i1")
    m2 = create_capture_manifest(recording_id="r2", interview_session_id="i2")
    _, rec1 = _create_recording_with_media(
        media_db,
        user_id=media_db["user_id"],
        token=media_db["token"],
        question_number=1,
        manifest=m1,
        audio_meta_extra={"source": "owner-a"},
    )
    _, rec2 = _create_recording_with_media(
        media_db,
        user_id=media_db["other_id"],
        token=media_db["other_token"],
        question_number=2,
        manifest=m2,
        audio_meta_extra={"source": "other-b"},
    )

    rows = list_admin_recordings(actor_admin_id=media_db["admin_id"], limit=10)
    ids = {int(r["id"]) for r in rows}
    assert ids == {rec1, rec2}
    assert len(rows) == 2

    by_id = {int(r["id"]): r for r in rows}
    assert by_id[rec1]["capture_manifest"]["recording_id"] == "r1"
    assert by_id[rec2]["capture_manifest"]["recording_id"] == "r2"
    assert by_id[rec1]["media"][0]["metadata"]["source"] == "owner-a"
    assert by_id[rec2]["media"][0]["metadata"]["source"] == "other-b"
    assert by_id[rec1]["owner_email"] == "owner@media.com"
    assert by_id[rec2]["owner_email"] == "other@media.com"


def test_list_admin_recordings_filters_empty_and_legacy(media_db, _deterministic_media_service_guardrail) -> None:
    _, rec_owner = _create_recording_with_media(
        media_db,
        user_id=media_db["user_id"],
        token=media_db["token"],
        question_number=1,
        manifest=None,
        audio_meta_extra={"legacy": True},
    )
    _, rec_other = _create_recording_with_media(
        media_db,
        user_id=media_db["other_id"],
        token=media_db["other_token"],
        question_number=1,
        manifest=create_capture_manifest(recording_id="x", interview_session_id="y"),
    )

    assert list_admin_recordings(actor_admin_id=media_db["admin_id"], owner_user_id=999999) == []

    owner_rows = list_admin_recordings(actor_admin_id=media_db["admin_id"], owner_user_id=media_db["user_id"])
    assert [int(r["id"]) for r in owner_rows] == [rec_owner]
    assert owner_rows[0]["capture_manifest"] is None
    assert owner_rows[0]["media"][0]["metadata"]["legacy"] is True

    search_rows = list_admin_recordings(actor_admin_id=media_db["admin_id"], search="other@media.com")
    assert [int(r["id"]) for r in search_rows] == [rec_other]
    assert search_rows[0]["capture_manifest"]["recording_id"] == "x"

    status_rows = list_admin_recordings(actor_admin_id=media_db["admin_id"], status="deleted")
    assert status_rows == []


def test_list_admin_recordings_requires_admin(media_db, _deterministic_media_service_guardrail) -> None:
    with pytest.raises(AuthError):
        list_admin_recordings(actor_admin_id=media_db["user_id"])
    with pytest.raises(AuthError):
        list_admin_recordings(actor_admin_id=None)


def test_delete_live_media_pending_retry_and_idempotent(media_db, _deterministic_media_service_guardrail, monkeypatch) -> None:
    from services import live_interview_media_service as svc

    _, recording_id = _create_recording_with_media(
        media_db, user_id=media_db["user_id"], token=media_db["token"], question_number=1
    )
    row = list_user_recordings(user_id=media_db["user_id"], session_token=media_db["token"])[0]
    media_id = int(row["media"][0]["id"])

    real_unlink = Path.unlink

    def fail_unlink(self, *args, **kwargs):
        if self.name.startswith("recording") and self.suffix == ".wav":
            raise OSError("locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(LiveMediaError, match="pending"):
        delete_live_media(
            media_id, actor_user_id=media_db["user_id"], session_token=media_db["token"]
        )

    with get_db_context() as db:
        from database.models import LiveInterviewMedia

        rec = db.query(LiveInterviewMedia).filter(LiveInterviewMedia.id == media_id).first()
        assert rec is not None
        assert rec.deletion_status == "pending_delete"

    # retry fails while still locked; retry API reports per-item failures
    retry_report = svc.retry_pending_deletions(
        actor_user_id=media_db["user_id"],
        session_token=media_db["token"],
        recording_id=recording_id,
    )
    assert any(item.get("status") == "pending_delete" for item in retry_report["recordings"] + retry_report["media"])

    monkeypatch.setattr(Path, "unlink", real_unlink)
    result = svc.retry_pending_deletions(
        actor_user_id=media_db["user_id"], session_token=media_db["token"], recording_id=recording_id
    )
    assert result["recordings"] or result["media"]
    assert delete_live_media(
        media_id, actor_user_id=media_db["user_id"], session_token=media_db["token"]
    )["status"] == "deleted"


def test_delete_recording_partial_failure_is_retryable(media_db, _deterministic_media_service_guardrail, monkeypatch) -> None:
    from services import live_interview_media_service as svc

    interview_id = _create_interview(media_db)
    recording_id = ensure_live_recording(
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        consent_given=True,
    )
    save_live_media(
        recording_id=recording_id,
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        media_type="audio",
        data=_wav_bytes(),
        duration_seconds=1.0,
    )
    save_live_media(
        recording_id=recording_id,
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        media_type="video",
        data=_mp4_bytes(),
        duration_seconds=1.0,
    )
    real_unlink = Path.unlink

    def fail_video(self, *args, **kwargs):
        if self.suffix == ".mp4":
            raise OSError("locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_video)
    with pytest.raises(LiveMediaError, match="pending"):
        delete_live_recording(
            recording_id, actor_user_id=media_db["user_id"], session_token=media_db["token"]
        )

    monkeypatch.setattr(Path, "unlink", real_unlink)
    res = svc.retry_pending_deletions(
        actor_user_id=media_db["user_id"], session_token=media_db["token"], recording_id=recording_id
    )
    assert any(r.get("status") == "deleted" for r in res["recordings"])
    rows = list_user_recordings(user_id=media_db["user_id"], session_token=media_db["token"])
    assert rows == []


def test_delete_rejects_unauthorized_user(media_db, _deterministic_media_service_guardrail) -> None:
    _, recording_id = _create_recording_with_media(
        media_db, user_id=media_db["user_id"], token=media_db["token"], question_number=1
    )
    row = list_user_recordings(user_id=media_db["user_id"], session_token=media_db["token"])[0]
    media_id = int(row["media"][0]["id"])
    with pytest.raises(LiveMediaError):
        delete_live_media(
            media_id, actor_user_id=media_db["other_id"], session_token=media_db["other_token"]
        )
    with pytest.raises(LiveMediaError):
        delete_live_recording(
            recording_id, actor_user_id=media_db["other_id"], session_token=media_db["other_token"]
        )


def test_save_replacement_keeps_old_file_on_db_commit_failure(media_db, _deterministic_media_service_guardrail, monkeypatch) -> None:
    from services import live_interview_media_service as svc

    interview_id = _create_interview(media_db)
    recording_id = ensure_live_recording(
        interview_id=interview_id, user_id=media_db["user_id"], session_token=media_db["token"], consent_given=True
    )
    old_bytes = _wav_bytes()
    media_id = save_live_media(
        recording_id=recording_id,
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        media_type="audio",
        data=old_bytes,
        duration_seconds=1.0,
    )
    old_path = None
    with get_db_context() as db:
        from database.models import LiveInterviewMedia

        rec = db.query(LiveInterviewMedia).filter(LiveInterviewMedia.id == media_id).first()
        assert rec is not None
        old_path = svc.resolve_storage_key(rec.storage_key)
        old_checksum = rec.checksum
    assert old_path is not None and old_path.exists()

    from database import db as db_module

    real_get_db_context = db_module.get_db_context
    calls = {"n": 0}

    @contextmanager
    def flaky_get_db_context():
        calls["n"] += 1
        with real_get_db_context() as db:
            if calls["n"] == 1:
                def boom():
                    raise RuntimeError("commit fail")

                db.commit = boom  # type: ignore[method-assign]
            yield db

    monkeypatch.setattr(db_module, "get_db_context", flaky_get_db_context)
    monkeypatch.setattr(svc, "get_db_context", flaky_get_db_context)
    new_bytes = _wav_bytes()
    with pytest.raises(RuntimeError, match="commit fail"):
        save_live_media(
            recording_id=recording_id,
            interview_id=interview_id,
            user_id=media_db["user_id"],
            session_token=media_db["token"],
            media_type="audio",
            data=new_bytes + b"\x00\x01",
            duration_seconds=1.0,
        )

    assert old_path.exists()
    assert old_path.read_bytes() == old_bytes
    with get_db_context() as db:
        from database.models import LiveInterviewMedia

        rec = db.query(LiveInterviewMedia).filter(LiveInterviewMedia.id == media_id).first()
        assert rec is not None
        assert rec.checksum == old_checksum
        assert svc.resolve_storage_key(rec.storage_key).read_bytes() == old_bytes


def test_save_new_file_compensated_on_db_failure(media_db, _deterministic_media_service_guardrail, monkeypatch) -> None:
    from services import live_interview_media_service as svc

    interview_id = _create_interview(media_db)
    recording_id = ensure_live_recording(
        interview_id=interview_id, user_id=media_db["user_id"], session_token=media_db["token"], consent_given=True
    )
    real_get_db_context = svc.get_db_context

    @contextmanager
    def flaky_get_db_context():
        with real_get_db_context() as db:
            yield db
            raise RuntimeError("commit fail")

    monkeypatch.setattr(svc, "get_db_context", flaky_get_db_context)
    root = Path(settings.live_media_root)
    before = {p for p in root.rglob("*") if p.is_file()} if root.exists() else set()
    with pytest.raises(RuntimeError, match="commit fail"):
        save_live_media(
            recording_id=recording_id,
            interview_id=interview_id,
            user_id=media_db["user_id"],
            session_token=media_db["token"],
            media_type="audio",
            data=_wav_bytes(),
            duration_seconds=1.0,
        )
    after = {p for p in root.rglob("*") if p.is_file()} if root.exists() else set()
    assert after == before


def test_cleanup_temporary_media_dry_run_age_and_fresh(media_db, _deterministic_media_service_guardrail) -> None:
    from services import live_interview_media_service as svc

    root = Path(settings.live_media_root)
    target_dir = root / "1" / "2" / "3" / "audio"
    target_dir.mkdir(parents=True, exist_ok=True)
    stale = target_dir / ".tmp_live_media_stale.part"
    fresh = target_dir / ".tmp_live_media_fresh.part"
    stale.write_bytes(b"old")
    fresh.write_bytes(b"new")
    old_time = time.time() - 3600
    os.utime(stale, (old_time, old_time))

    report = svc.cleanup_temporary_media(min_age_seconds=300, dry_run=True)
    assert any("stale" in c for c in report["candidates"])
    assert any(s.get("reason") == "fresh" for s in report["skipped"])
    assert stale.exists() and fresh.exists()

    report2 = svc.cleanup_temporary_media(min_age_seconds=300, dry_run=False)
    assert report2["refused"] is True
    assert "active-writer registry" in report2["error"]
    assert report2["removed"] == []
    assert any("stale" in c for c in report2["candidates"])
    assert stale.exists() and fresh.exists()


def test_storage_integrity_diagnostics_dry_run_no_mutation(media_db, _deterministic_media_service_guardrail) -> None:
    from services import live_interview_media_service as svc

    _, recording_id = _create_recording_with_media(
        media_db, user_id=media_db["user_id"], token=media_db["token"], question_number=1
    )
    with get_db_context() as db:
        from database.models import LiveInterviewMedia

        rec = db.query(LiveInterviewMedia).filter(LiveInterviewMedia.recording_id == recording_id).first()
        assert rec is not None
        path = svc.resolve_storage_key(rec.storage_key)
        db_size = rec.size_bytes
        db_checksum = rec.checksum
    path.unlink()

    diag = svc.run_storage_integrity_diagnostics(actor_admin_id=media_db["admin_id"])
    assert diag["read_only"] is True
    assert any(m["recording_id"] == recording_id for m in diag["missing_files"])

    with get_db_context() as db:
        from database.models import LiveInterviewMedia

        rec = db.query(LiveInterviewMedia).filter(LiveInterviewMedia.recording_id == recording_id).first()
        assert rec is not None
        assert rec.size_bytes == db_size
        assert rec.checksum == db_checksum
        assert rec.deletion_status == "active"


@pytest.mark.parametrize(
    ("page_path", "session"),
    [
        (
            "frontend/user/live_recording_history.py",
            {"user_id": "user_id", "token": "token"},
        ),
        (
            "frontend/admin/live_media.py",
            {"account": True},
        ),
    ],
)
def test_media_pages_render_without_exceptions(media_db, _deterministic_media_service_guardrail, page_path, session) -> None:
    interview_id = _create_interview(media_db)
    recording_id = ensure_live_recording(
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        consent_given=True,
    )
    save_live_media(
        recording_id=recording_id,
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        media_type="audio",
        data=_wav_bytes(),
        duration_seconds=1.0,
    )
    at = AppTest.from_file(str(PROJECT_ROOT / page_path))
    at.session_state["user_id"] = media_db["user_id"]
    at.session_state["user_session_id"] = media_db["token"]
    if "account" in session:
        at.session_state["account"] = SimpleNamespace(
            id=media_db["admin_id"], is_admin=True
        )
    at.run(timeout=60)

    assert not at.exception


def _media_rows(recording_id: int) -> list[dict]:
    with get_db_context() as db:
        rows = (
            db.query(LiveInterviewMedia)
            .filter(LiveInterviewMedia.recording_id == int(recording_id))
            .order_by(LiveInterviewMedia.id.asc())
            .all()
        )
        return [
            {
                "id": int(row.id),
                "storage_key": str(row.storage_key),
                "deletion_status": str(row.deletion_status),
                "checksum": str(row.checksum),
                "size_bytes": int(row.size_bytes or 0),
            }
            for row in rows
        ]


def _set_media_status(media_id: int, status: str, *, storage_key: str | None = None) -> None:
    with get_db_context() as db:
        row = db.query(LiveInterviewMedia).filter(LiveInterviewMedia.id == int(media_id)).first()
        assert row is not None
        row.deletion_status = status
        if storage_key is not None:
            row.storage_key = storage_key


def _set_recording_status(recording_id: int, status: str) -> None:
    with get_db_context() as db:
        row = (
            db.query(LiveInterviewRecording)
            .filter(LiveInterviewRecording.id == int(recording_id))
            .first()
        )
        assert row is not None
        row.deletion_status = status


def _make_media_pending(media_db, media_id: int, path: Path, monkeypatch) -> None:
    real_unlink = Path.unlink

    def refusing_unlink(self, *args, **kwargs):
        if self == path:
            raise OSError("locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refusing_unlink)
    try:
        with pytest.raises(LiveMediaError, match="pending"):
            delete_live_media(
                media_id,
                actor_user_id=media_db["user_id"],
                session_token=media_db["token"],
            )
    finally:
        monkeypatch.setattr(Path, "unlink", real_unlink)


def _tree_snapshot(root: Path) -> list[tuple[str, bool, int, int, bytes]]:
    snapshot: list[tuple[str, bool, int, int, bytes]] = []
    for candidate in [root, *root.rglob("*")]:
        stat = candidate.stat()
        snapshot.append(
            (
                str(candidate),
                candidate.is_dir(),
                stat.st_mtime_ns,
                stat.st_size,
                candidate.read_bytes() if candidate.is_file() else b"",
            )
        )
    return sorted(snapshot)


def test_pending_and_deleted_media_cannot_be_reactivated_by_save(
    media_db, _deterministic_media_service_guardrail, monkeypatch
) -> None:
    from services import live_interview_media_service as svc

    interview_id, recording_id = _create_recording_with_media(
        media_db, user_id=media_db["user_id"], token=media_db["token"], question_number=1
    )
    with get_db_context() as db:
        row = db.query(LiveInterviewMedia).filter(LiveInterviewMedia.recording_id == recording_id).first()
        assert row is not None
        media_id = int(row.id)
        key = str(row.storage_key)
        path = svc.resolve_storage_key(key)
        original_bytes = path.read_bytes()

    _make_media_pending(media_db, media_id, path, monkeypatch)

    root = Path(settings.live_media_root)
    before = {candidate: candidate.read_bytes() for candidate in root.rglob("*") if candidate.is_file()}
    with pytest.raises(LiveMediaError, match="pending deletion"):
        save_live_media(
            recording_id=recording_id,
            interview_id=interview_id,
            user_id=media_db["user_id"],
            session_token=media_db["token"],
            media_type="audio",
            data=_wav_bytes() + b"\x00\x01",
            duration_seconds=1.0,
        )
    after = {candidate: candidate.read_bytes() for candidate in root.rglob("*") if candidate.is_file()}
    assert after == before
    pending = _media_rows(recording_id)[0]
    assert pending["deletion_status"] == "pending_delete"
    assert pending["storage_key"] == key
    assert path.read_bytes() == original_bytes

    _set_media_status(media_id, "deleted")
    with pytest.raises(LiveMediaError, match="deleted"):
        save_live_media(
            recording_id=recording_id,
            interview_id=interview_id,
            user_id=media_db["user_id"],
            session_token=media_db["token"],
            media_type="audio",
            data=_wav_bytes() + b"\x00\x02",
            duration_seconds=1.0,
        )
    deleted = _media_rows(recording_id)[0]
    assert deleted["deletion_status"] == "deleted"
    assert deleted["storage_key"] == key
    assert path.read_bytes() == original_bytes


def test_ensure_live_recording_rejects_pending_and_deleted_records(
    media_db, _deterministic_media_service_guardrail
) -> None:
    interview_id = _create_interview(media_db)
    recording_id = ensure_live_recording(
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        consent_given=True,
    )
    assert (
        ensure_live_recording(
            interview_id=interview_id,
            user_id=media_db["user_id"],
            session_token=media_db["token"],
            consent_given=True,
        )
        == recording_id
    )

    _set_recording_status(recording_id, "pending_delete")
    with pytest.raises(LiveMediaError, match="pending deletion"):
        ensure_live_recording(
            interview_id=interview_id,
            user_id=media_db["user_id"],
            session_token=media_db["token"],
            consent_given=True,
        )

    _set_recording_status(recording_id, "deleted")
    with pytest.raises(LiveMediaError, match="deleted"):
        ensure_live_recording(
            interview_id=interview_id,
            user_id=media_db["user_id"],
            session_token=media_db["token"],
            consent_given=True,
        )


def test_committed_save_survives_post_commit_context_failure(
    media_db, _deterministic_media_service_guardrail, monkeypatch
) -> None:
    from services import live_interview_media_service as svc

    interview_id = _create_interview(media_db)
    recording_id = ensure_live_recording(
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        consent_given=True,
    )
    real_context = svc.get_db_context

    @contextmanager
    def fail_after_commit():
        with real_context() as db:
            yield db
        raise RuntimeError("post-commit failure")

    monkeypatch.setattr(svc, "get_db_context", fail_after_commit)
    root = Path(settings.live_media_root)
    before = {candidate for candidate in root.rglob("*") if candidate.is_file()} if root.exists() else set()
    with pytest.raises(RuntimeError, match="post-commit failure"):
        save_live_media(
            recording_id=recording_id,
            interview_id=interview_id,
            user_id=media_db["user_id"],
            session_token=media_db["token"],
            media_type="audio",
            data=_wav_bytes(),
            duration_seconds=1.0,
        )
    after = {candidate for candidate in root.rglob("*") if candidate.is_file()}
    rows = _media_rows(recording_id)
    assert len(rows) == 1
    assert after - before == {svc.resolve_storage_key(rows[0]["storage_key"])}
    assert svc.resolve_storage_key(rows[0]["storage_key"]).exists()


def test_concurrent_saves_for_one_media_slot_have_one_winner(
    media_db, _deterministic_media_service_guardrail, monkeypatch
) -> None:
    from services import live_interview_media_service as svc

    interview_id, recording_id = _create_recording_with_media(
        media_db, user_id=media_db["user_id"], token=media_db["token"], question_number=1
    )
    barrier = threading.Barrier(2, timeout=15)
    real_atomic = svc._atomic_write

    def paused_atomic(path, data):
        barrier.wait()
        return real_atomic(path, data)

    monkeypatch.setattr(svc, "_atomic_write", paused_atomic)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                save_live_media,
                recording_id=recording_id,
                interview_id=interview_id,
                user_id=media_db["user_id"],
                session_token=media_db["token"],
                media_type="audio",
                data=_wav_bytes() + bytes([index]),
                duration_seconds=1.0,
            )
            for index in range(2)
        ]
        successes = []
        errors = []
        for future in futures:
            try:
                successes.append(future.result(timeout=25))
            except LiveMediaError as exc:
                errors.append(str(exc))

    assert len(successes) == 1
    assert len(errors) == 1
    assert "changed while saving" in errors[0]
    rows = _media_rows(recording_id)
    assert len(rows) == 1
    assert svc.resolve_storage_key(rows[0]["storage_key"]).exists()
    files = [candidate for candidate in Path(settings.live_media_root).rglob("*") if candidate.is_file()]
    assert len(files) == 1


def test_concurrent_save_and_delete_for_one_media_slot_rejects_save(
    media_db, _deterministic_media_service_guardrail, monkeypatch
) -> None:
    from services import live_interview_media_service as svc

    interview_id, recording_id = _create_recording_with_media(
        media_db, user_id=media_db["user_id"], token=media_db["token"], question_number=1
    )
    media_id = _media_rows(recording_id)[0]["id"]
    save_ready = threading.Event()
    delete_done = threading.Event()
    real_atomic = svc._atomic_write

    def paused_atomic(path, data):
        save_ready.set()
        assert delete_done.wait(timeout=15)
        return real_atomic(path, data)

    monkeypatch.setattr(svc, "_atomic_write", paused_atomic)
    with ThreadPoolExecutor(max_workers=2) as executor:
        save_future = executor.submit(
            save_live_media,
            recording_id=recording_id,
            interview_id=interview_id,
            user_id=media_db["user_id"],
            session_token=media_db["token"],
            media_type="audio",
            data=_wav_bytes() + b"\x11",
            duration_seconds=1.0,
        )
        assert save_ready.wait(timeout=15)
        delete_live_media(
            media_id,
            actor_user_id=media_db["user_id"],
            session_token=media_db["token"],
        )
        delete_done.set()
        with pytest.raises(LiveMediaError, match="changed while saving"):
            save_future.result(timeout=25)

    rows = _media_rows(recording_id)
    assert len(rows) == 1
    assert rows[0]["deletion_status"] == "deleted"
    files = [candidate for candidate in Path(settings.live_media_root).rglob("*") if candidate.is_file()]
    assert files == []


def test_stale_writer_changes_storage_key_before_save_commits(
    media_db, _deterministic_media_service_guardrail, monkeypatch
) -> None:
    from services import live_interview_media_service as svc

    interview_id, recording_id = _create_recording_with_media(
        media_db, user_id=media_db["user_id"], token=media_db["token"], question_number=1
    )
    original = _media_rows(recording_id)[0]
    original_path = svc.resolve_storage_key(original["storage_key"])
    save_ready = threading.Event()
    replace_done = threading.Event()
    real_atomic = svc._atomic_write

    def paused_atomic(path, data):
        save_ready.set()
        assert replace_done.wait(timeout=15)
        return real_atomic(path, data)

    monkeypatch.setattr(svc, "_atomic_write", paused_atomic)
    with ThreadPoolExecutor(max_workers=2) as executor:
        save_future = executor.submit(
            save_live_media,
            recording_id=recording_id,
            interview_id=interview_id,
            user_id=media_db["user_id"],
            session_token=media_db["token"],
            media_type="audio",
            data=_wav_bytes() + b"\x22",
            duration_seconds=1.0,
        )
        assert save_ready.wait(timeout=15)
        replacement_key = svc._storage_key(
            user_id=media_db["user_id"],
            interview_id=interview_id,
            recording_id=recording_id,
            media_type="audio",
            unique=True,
        )
        replacement_path = svc.resolve_storage_key(replacement_key)
        replacement_path.write_bytes(b"replacement")
        _set_media_status(
            original["id"], "active", storage_key=replacement_key
        )
        replace_done.set()
        with pytest.raises(LiveMediaError, match="changed while saving"):
            save_future.result(timeout=25)

    rows = _media_rows(recording_id)
    assert len(rows) == 1
    assert rows[0]["storage_key"] == replacement_key
    assert original_path.exists()
    assert replacement_path.read_bytes() == b"replacement"
    files = {candidate for candidate in Path(settings.live_media_root).rglob("*") if candidate.is_file()}
    assert files == {original_path, replacement_path}


def test_concurrent_deletion_while_save_prepares_replacement_is_conflict(
    media_db, _deterministic_media_service_guardrail, monkeypatch
) -> None:
    from services import live_interview_media_service as svc

    interview_id, recording_id = _create_recording_with_media(
        media_db, user_id=media_db["user_id"], token=media_db["token"], question_number=1
    )
    media_id = _media_rows(recording_id)[0]["id"]
    original_path = svc.resolve_storage_key(_media_rows(recording_id)[0]["storage_key"])
    save_ready = threading.Event()
    delete_done = threading.Event()
    real_atomic = svc._atomic_write
    real_unlink = Path.unlink

    def paused_atomic(path, data):
        save_ready.set()
        assert delete_done.wait(timeout=15)
        return real_atomic(path, data)

    def refuse_only_original(self, *args, **kwargs):
        if self == original_path:
            raise OSError("locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(svc, "_atomic_write", paused_atomic)
    monkeypatch.setattr(Path, "unlink", refuse_only_original)
    with ThreadPoolExecutor(max_workers=2) as executor:
        save_future = executor.submit(
            save_live_media,
            recording_id=recording_id,
            interview_id=interview_id,
            user_id=media_db["user_id"],
            session_token=media_db["token"],
            media_type="audio",
            data=_wav_bytes() + b"\x33",
            duration_seconds=1.0,
        )
        assert save_ready.wait(timeout=15)
        with pytest.raises(LiveMediaError, match="pending"):
            delete_live_media(
                media_id,
                actor_user_id=media_db["user_id"],
                session_token=media_db["token"],
            )
        delete_done.set()
        with pytest.raises(LiveMediaError, match="changed while saving"):
            save_future.result(timeout=25)

    monkeypatch.setattr(Path, "unlink", real_unlink)
    rows = _media_rows(recording_id)
    assert rows[0]["deletion_status"] == "pending_delete"
    files = {candidate for candidate in Path(settings.live_media_root).rglob("*") if candidate.is_file()}
    assert files == {original_path}


def test_destructive_temporary_cleanup_is_refused_for_active_writers(
    media_db, _deterministic_media_service_guardrail
) -> None:
    from services import live_interview_media_service as svc

    root = Path(settings.live_media_root)
    root.mkdir(parents=True, exist_ok=True)
    stale = root / ".tmp_live_media_stale.part"
    fresh = root / ".tmp_live_media_fresh.part"
    unrelated = root / "kept.wav"
    stale.write_bytes(b"old")
    fresh.write_bytes(b"new")
    unrelated.write_bytes(b"keep")
    old_time = time.time() - 3600
    os.utime(stale, (old_time, old_time))

    report = svc.cleanup_temporary_media(min_age_seconds=300, dry_run=True)
    assert report["refused"] is False
    assert report["candidates"] == [".tmp_live_media_stale.part"]
    assert any(item.get("reason") == "fresh" for item in report["skipped"])
    assert stale.exists() and fresh.exists() and unrelated.exists()

    report2 = svc.cleanup_temporary_media(min_age_seconds=300, dry_run=False)
    assert report2["refused"] is True
    assert "active-writer registry" in report2["error"]
    assert report2["removed"] == []
    assert report2["candidates"] == [".tmp_live_media_stale.part"]
    assert stale.exists() and fresh.exists() and unrelated.exists()


def test_retry_pending_deletions_for_owner_and_admin(
    media_db, _deterministic_media_service_guardrail, monkeypatch
) -> None:
    from services import live_interview_media_service as svc

    _, recording_id = _create_recording_with_media(
        media_db, user_id=media_db["user_id"], token=media_db["token"], question_number=1
    )
    media_id = _media_rows(recording_id)[0]["id"]
    path = svc.resolve_storage_key(_media_rows(recording_id)[0]["storage_key"])
    _make_media_pending(media_db, media_id, path, monkeypatch)
    assert path.exists()

    report = retry_pending_deletions(
        actor_user_id=media_db["user_id"], session_token=media_db["token"]
    )
    assert any(item.get("id") == media_id and item.get("status") == "deleted" for item in report["media"])
    assert _media_rows(recording_id)[0]["deletion_status"] == "deleted"
    assert not path.exists()

    other_report = retry_pending_deletions(
        actor_user_id=media_db["other_id"], session_token=media_db["other_token"]
    )
    assert other_report == {"recordings": [], "media": []}


def test_retry_pending_deletions_admin_and_missing_file(
    media_db, _deterministic_media_service_guardrail, monkeypatch
) -> None:
    from services import live_interview_media_service as svc

    _, recording_id = _create_recording_with_media(
        media_db, user_id=media_db["user_id"], token=media_db["token"], question_number=1
    )
    media_id = _media_rows(recording_id)[0]["id"]
    path = svc.resolve_storage_key(_media_rows(recording_id)[0]["storage_key"])
    path.unlink()
    _set_media_status(media_id, "pending_delete")

    report = retry_pending_deletions(actor_admin_id=media_db["admin_id"])
    assert any(item.get("id") == media_id and item.get("status") == "deleted" for item in report["media"])
    assert _media_rows(recording_id)[0]["deletion_status"] == "deleted"


def test_retry_pending_deletions_partial_failure_is_reported(
    media_db, _deterministic_media_service_guardrail, monkeypatch
) -> None:
    from services import live_interview_media_service as svc

    _, recording_id = _create_recording_with_media(
        media_db, user_id=media_db["user_id"], token=media_db["token"], question_number=1
    )
    interview_id = _media_rows(recording_id)[0]["id"]
    media_id = int(interview_id)
    path = svc.resolve_storage_key(_media_rows(recording_id)[0]["storage_key"])
    with get_db_context() as db:
        second = LiveInterviewMedia(
            recording_id=int(recording_id),
            interview_session_id=db.query(LiveInterviewRecording)
            .filter(LiveInterviewRecording.id == int(recording_id))
            .first()
            .interview_session_id,
            owner_user_id=media_db["user_id"],
            media_type="video",
            storage_key="1/2/3/video/second.mp4",
            filename="second.mp4",
            mime_type="video/mp4",
            size_bytes=4,
            checksum="checksum",
            media_status="ready",
            metadata_json={},
            finalized_at=datetime.now(UTC).replace(tzinfo=None),
            deletion_status="pending_delete",
        )
        db.add(second)
    path.unlink()
    _set_media_status(media_id, "pending_delete")
    second_path = svc.resolve_storage_key("1/2/3/video/second.mp4")
    second_path.parent.mkdir(parents=True, exist_ok=True)
    second_path.write_bytes(b"video")
    real_unlink = Path.unlink

    def refuse_second(self, *args, **kwargs):
        if self == second_path:
            raise OSError("locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_second)
    report = retry_pending_deletions(
        actor_user_id=media_db["user_id"], session_token=media_db["token"]
    )
    monkeypatch.setattr(Path, "unlink", real_unlink)

    statuses = {int(item["id"]): item.get("status") for item in report["media"]}
    assert statuses[media_id] == "deleted"
    assert statuses[int(second.id)] == "pending_delete"
    assert second_path.exists()


def test_diagnostics_do_not_create_missing_root(
    media_db, _deterministic_media_service_guardrail, monkeypatch
) -> None:
    from services import live_interview_media_service as svc

    root = Path(settings.live_media_root)
    missing = root / "missing-diagnostics-root"
    assert not missing.exists()
    monkeypatch.setattr(settings, "live_media_root", str(missing))

    diag = svc.run_storage_integrity_diagnostics(actor_admin_id=media_db["admin_id"])
    assert diag["read_only"] is True
    assert diag["storage_root_exists"] is False
    assert any(item.get("result") == "storage_root_missing" for item in diag["unverified"])
    assert not missing.exists()


def test_diagnostics_report_io_errors_without_aborting(
    media_db, _deterministic_media_service_guardrail, monkeypatch
) -> None:
    from services import live_interview_media_service as svc

    _, recording_id = _create_recording_with_media(
        media_db, user_id=media_db["user_id"], token=media_db["token"], question_number=1
    )
    media_id = _media_rows(recording_id)[0]["id"]
    target = svc.resolve_storage_key(_media_rows(recording_id)[0]["storage_key"])
    real_exists = Path.exists

    def flaky_exists(self, *args, **kwargs):
        if self == target:
            raise OSError("access denied")
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", flaky_exists)
    diag = svc.run_storage_integrity_diagnostics(actor_admin_id=media_db["admin_id"])
    monkeypatch.setattr(Path, "exists", real_exists)

    assert any(
        item.get("result") == "unverified_inspection_error" and item.get("media_id") == media_id
        for item in diag["unverified"]
    )
    assert real_exists(target)


def test_diagnostics_detect_checksum_mismatch_without_mutation(
    media_db, _deterministic_media_service_guardrail
) -> None:
    from services import live_interview_media_service as svc

    _, recording_id = _create_recording_with_media(
        media_db, user_id=media_db["user_id"], token=media_db["token"], question_number=1
    )
    path = svc.resolve_storage_key(_media_rows(recording_id)[0]["storage_key"])
    path.write_bytes(path.read_bytes() + b"\x44")
    root = Path(settings.live_media_root)
    before = _tree_snapshot(root)

    diag = svc.run_storage_integrity_diagnostics(actor_admin_id=media_db["admin_id"])

    assert any(item.get("recording_id") == recording_id for item in diag["checksum_mismatches"])
    assert _tree_snapshot(root) == before


def test_diagnostics_are_read_only_and_keep_temp_candidates(
    media_db, _deterministic_media_service_guardrail
) -> None:
    from services import live_interview_media_service as svc

    _create_recording_with_media(
        media_db, user_id=media_db["user_id"], token=media_db["token"], question_number=1
    )
    root = Path(settings.live_media_root)
    stale = root / ".tmp_live_media_stale.part"
    stale.write_bytes(b"old")
    old_time = time.time() - 3600
    os.utime(stale, (old_time, old_time))
    before = _tree_snapshot(root)

    diag = svc.run_storage_integrity_diagnostics(actor_admin_id=media_db["admin_id"])

    assert diag["read_only"] is True
    assert diag["temp_candidates"] == [".tmp_live_media_stale.part"]
    assert _tree_snapshot(root) == before
