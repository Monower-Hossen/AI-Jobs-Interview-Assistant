from __future__ import annotations

import json

import pytest
from services import live_interview_state as state_module
from services.live_interview_state import (
    RECORDING_MANIFEST,
    RECORDING_MANIFEST_RECORDING_ID,
    VIDEO_ERROR,
    ensure_capture_manifest,
    get_capture_manifest,
    initialize_live_interview_state,
    start_recording,
    stop_recording,
    update_capture_manifest_for_recorders,
)
from services.live_recording_manifest import (
    CAPTURE_MANIFEST_KEY,
    ManifestValidationError,
    create_capture_manifest,
    extract_capture_manifest,
    manifest_to_json,
    mark_manifest_finalized,
    merge_manifest_metadata,
    safe_load_capture_manifest,
    update_audio_manifest,
    update_video_manifest,
    validate_capture_manifest,
)


class _FakeAudioRecorder:
    def __init__(self) -> None:
        self.enabled = False

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def clear(self) -> None:
        self.enabled = False

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "chunk_count": 0,
            "dropped_chunks": 0,
            "duration_seconds": 0.0,
            "sample_rate": 16000,
            "channels": 1,
            "sample_format": "s16",
            "byte_count": 0,
            "overflowed": False,
            "capture_error": None,
            "first_received_monotonic_ns": None,
            "last_received_monotonic_ns": None,
        }


class _FakeVideoRecorder:
    def __init__(self, error: str | None = None) -> None:
        self.enabled = False
        self.error = error

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def clear(self) -> None:
        self.enabled = False

    def snapshot(self) -> dict:
        healthy = self.enabled and self.error is None
        return {
            "enabled": self.enabled,
            "worker_alive": healthy,
            "resource_lease_held": healthy,
            "error": self.error,
            "frames_encoded": 0,
            "dropped_frames": 0,
            "queue_overflow_count": 0,
            "timestamp_adjustments": 0,
            "duration_seconds": 0.0,
            "width": 640,
            "height": 480,
            "fps": 30,
            "bytes_written": 0,
            "codec": "libx264",
            "pixel_format": "yuv420p",
            "container": "mp4",
            "overflowed": False,
            "first_received_monotonic_ns": None,
            "last_received_monotonic_ns": None,
        }


def _patch_fake_recorders(monkeypatch, video: _FakeVideoRecorder) -> _FakeAudioRecorder:
    audio = _FakeAudioRecorder()
    monkeypatch.setattr(state_module, "get_recorder", lambda state: audio)
    monkeypatch.setattr(state_module, "get_video_recorder", lambda state: video)
    return audio


def test_manifest_contract_valid_roundtrip() -> None:
    manifest = create_capture_manifest(
        recording_id=10,
        interview_session_id=20,
        policy_version="live-recording-v1",
        started_monotonic_ns=100,
        started_utc="2026-01-01T00:00:00Z",
    )
    encoded = manifest_to_json(manifest)
    loaded = safe_load_capture_manifest(encoded)
    assert loaded is not None
    assert loaded["schema_version"] == 1
    assert loaded["recording_id"] == "10"
    assert loaded["interview_session_id"] == "20"
    assert loaded["synchronization"]["status"] == "independent_streams"
    assert loaded["synchronization"]["guaranteed_sync"] is False


def test_manifest_validation_rejects_invalid_values() -> None:
    base = create_capture_manifest(recording_id=1, interview_session_id=2)
    bad_cases = []
    c1 = json.loads(json.dumps(base))
    c1["audio"]["dropped_chunks"] = -1
    bad_cases.append(c1)
    c2 = json.loads(json.dumps(base))
    c2["audio"]["duration_seconds"] = -0.1
    bad_cases.append(c2)
    c3 = json.loads(json.dumps(base))
    c3["timing"]["recording_started_monotonic_ns"] = 200
    c3["timing"]["recording_ended_monotonic_ns"] = 100
    bad_cases.append(c3)
    c4 = json.loads(json.dumps(base))
    c4["schema_version"] = 999
    bad_cases.append(c4)
    for case in bad_cases[:3]:
        with pytest.raises(ManifestValidationError):
            validate_capture_manifest(case)
    with pytest.raises(ManifestValidationError):
        validate_capture_manifest(c4)


def test_manifest_unknown_version_safe_load_and_extract() -> None:
    assert safe_load_capture_manifest({"schema_version": 999, "recording_id": "1", "interview_session_id": "2"}) is None
    extracted = extract_capture_manifest({"capture_manifest": {"schema_version": 999}})
    assert extracted is not None
    assert extracted["invalid"] is True
    assert extracted["schema_version"] == 999


def test_manifest_audio_video_updates_and_offset() -> None:
    manifest = create_capture_manifest(recording_id=1, interview_session_id=2, started_monotonic_ns=100)
    manifest = update_audio_manifest(
        manifest,
        {
            "chunk_count": 3,
            "dropped_chunks": 1,
            "duration_seconds": 1.5,
            "sample_rate": 16000,
            "channels": 1,
            "sample_format": "s16",
            "byte_count": 48000,
            "overflowed": False,
            "capture_error": None,
            "first_received_monotonic_ns": 1_000_000_000,
            "last_received_monotonic_ns": 2_500_000_000,
        },
        status="finalized",
    )
    manifest = update_video_manifest(
        manifest,
        {
            "frames_encoded": 30,
            "dropped_frames": 2,
            "queue_overflow_count": 1,
            "timestamp_adjustments": 0,
            "duration_seconds": 1.0,
            "width": 640,
            "height": 480,
            "fps": 30,
            "bytes_written": 12345,
            "codec": "libx264",
            "pixel_format": "yuv420p",
            "container": "mp4",
            "overflowed": False,
            "error": None,
            "first_received_monotonic_ns": 900_000_000,
            "last_received_monotonic_ns": 1_900_000_000,
        },
        status="finalized",
    )
    manifest = mark_manifest_finalized(manifest, ended_monotonic_ns=3_000_000_000, ended_utc="2026-01-01T00:00:03Z")
    assert manifest["audio"]["status"] == "finalized"
    assert manifest["video"]["status"] == "finalized"
    assert manifest["audio"]["received_chunks"] == 3
    assert manifest["video"]["encoded_frames"] == 30
    assert manifest["synchronization"]["status"] == "estimated_alignment"
    assert manifest["synchronization"]["guaranteed_sync"] is False
    assert manifest["synchronization"]["offset_method"] == "server_receive_monotonic_first_frame_delta"
    assert manifest["synchronization"]["audio_video_offset_seconds"] == 0.1


def test_manifest_merge_preserves_existing_metadata() -> None:
    manifest = create_capture_manifest(recording_id=1, interview_session_id=2)
    merged = merge_manifest_metadata({"question_number": 3, "existing": True}, manifest)
    assert merged["question_number"] == 3
    assert merged["existing"] is True
    assert merged[CAPTURE_MANIFEST_KEY]["schema_version"] == 1


def test_interview_state_manifest_initialized_once_and_survives_rerun_like_reinit() -> None:
    state: dict = {}
    initialize_live_interview_state(state)
    state["live_webcam_active_interview_id"] = 55
    state["live_webcam_recording_id"] = 77
    m1 = ensure_capture_manifest(state, interview_id=55, recording_id=77)
    assert m1 is not None
    state[RECORDING_MANIFEST]["audio"]["received_chunks"] = 9
    # Simulate Streamlit rerun reinitializing defaults
    initialize_live_interview_state(state)
    m2 = ensure_capture_manifest(state, interview_id=55, recording_id=77)
    assert m2 is not None
    assert m2["audio"]["received_chunks"] == 9
    assert state[RECORDING_MANIFEST_RECORDING_ID] == "77"


def test_interview_state_manifest_updates_from_recorders(monkeypatch) -> None:
    video = _FakeVideoRecorder()
    _patch_fake_recorders(monkeypatch, video)
    state: dict = {}
    initialize_live_interview_state(state)
    state["live_webcam_active_interview_id"] = 5
    state["live_webcam_recording_id"] = 6
    ensure_capture_manifest(state, interview_id=5, recording_id=6)
    start_recording(state, 1, record_video=True)
    assert video.enabled is True
    manifest = get_capture_manifest(state)
    assert manifest is not None
    assert manifest["audio"]["status"] == "capturing"
    assert manifest["video"]["status"] == "capturing"
    stop_recording(state)
    assert video.enabled is False
    manifest = update_capture_manifest_for_recorders(state, finalize=True)
    assert manifest is not None
    assert manifest["audio"]["status"] in {"stopped", "finalized", "capturing", "partial"}
    assert manifest["timing"]["recording_ended_monotonic_ns"] is not None


def test_interview_state_manifest_marks_resource_denied_video_failed(monkeypatch) -> None:
    denial = "Available system memory is critically low."
    video = _FakeVideoRecorder(error=denial)
    _patch_fake_recorders(monkeypatch, video)
    state: dict = {}
    initialize_live_interview_state(state)
    state["live_webcam_active_interview_id"] = 5
    state["live_webcam_recording_id"] = 6
    ensure_capture_manifest(state, interview_id=5, recording_id=6)
    start_recording(state, 1, record_video=True)
    manifest = get_capture_manifest(state)
    assert manifest is not None
    assert manifest["video"]["status"] == "failed"
    assert manifest["video"]["capture_error"] == denial
    assert state[VIDEO_ERROR] == denial
