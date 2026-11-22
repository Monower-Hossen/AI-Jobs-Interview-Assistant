from __future__ import annotations

import io
import wave
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np
import pytest
from config.settings import settings
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
from services.live_webcam_video import LiveVideoRecorder, validate_video_bytes
from streamlit.testing.v1 import AppTest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x01\x00" * 1_600)
    return buffer.getvalue()


def _mp4_bytes() -> bytes:
    return b"\x00\x00\x00\x20ftypisom" + b"\x00" * 40


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


def _create_interview(media_db, *, report=None) -> int:
    from database import InterviewSession, get_db_context

    with get_db_context() as db:
        interview = InterviewSession(
            session_id=media_db["workspace_id"],
            interview_type="live_webcam",
            interview_mode="live_webcam",
            answer_mode="audio",
            status="completed",
            report_json=report or {"answers": []},
        )
        db.add(interview)
        db.flush()
        return int(interview.id)


def test_video_recorder_writes_valid_mp4() -> None:
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


def test_recording_state_enables_and_disables_video() -> None:
    state: dict = {}
    initialize_live_interview_state(state)

    start_recording(state, 1, record_video=True)

    assert state[RECORDING_ACTIVE] is True
    assert get_video_recorder(state).enabled is True

    stop_recording(state)

    assert state[RECORDING_ACTIVE] is False
    assert get_video_recorder(state).enabled is False
    get_video_recorder(state).clear()


def test_media_service_save_read_verify_list_and_delete(media_db) -> None:
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
    audio_id = save_live_media(
        recording_id=recording_id,
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        media_type="audio",
        data=_wav_bytes(),
        duration_seconds=1.0,
    )
    video_id = save_live_media(
        recording_id=recording_id,
        interview_id=interview_id,
        user_id=media_db["user_id"],
        session_token=media_db["token"],
        media_type="video",
        data=_mp4_bytes(),
        duration_seconds=1.0,
    )

    audio, mime, filename = read_live_media(
        audio_id,
        actor_user_id=media_db["user_id"],
        session_token=media_db["token"],
    )
    assert audio.startswith(b"RIFF")
    assert mime == "audio/wav"
    assert filename == "recording.wav"
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
def test_media_pages_render_without_exceptions(media_db, page_path, session) -> None:
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
