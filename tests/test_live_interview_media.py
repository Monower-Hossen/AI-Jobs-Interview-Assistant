from __future__ import annotations

import io
import os
import time
import wave
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
from services.live_recording_manifest import (
    CAPTURE_MANIFEST_KEY,
    create_capture_manifest,
    update_audio_manifest,
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

    monkeypatch.setattr(live_webcam_video, "try_begin_heavy_task", lambda task, snapshot=None: (True, None))
    monkeypatch.setattr(live_webcam_video, "end_heavy_task", lambda task: None)

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

    monkeypatch.setattr(live_webcam_video, "try_begin_heavy_task", lambda task, snapshot=None: (True, None))
    monkeypatch.setattr(live_webcam_video, "end_heavy_task", lambda task: None)


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
    assert any("stale" in c for c in report2["removed"])
    assert not stale.exists()
    assert fresh.exists()


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
