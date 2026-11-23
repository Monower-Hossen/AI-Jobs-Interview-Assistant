from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
import pytest
from config.settings import Settings
from config.settings import settings as runtime_settings
from rag import embeddings as embeddings_module
from services import resource_monitor
from services import speech_to_text as speech_module
from services.live_webcam_video import LiveVideoRecorder
from services.resource_monitor import (
    _LEASE_MANAGER,
    AdmissionRejectionReason,
    LeaseReleaseStatus,
    ResourceBusyError,
    ResourceSnapshot,
    acquire_resource_lease,
    check_heavy_task_allowed,
    get_active_heavy_tasks,
    get_resource_admission_diagnostics,
    heartbeat_resource_lease,
    inspect_stale_leases,
    recover_stale_leases,
    release_resource_lease,
    reset_resource_monitor_state,
    resource_lease,
)
from streamlit.testing.v1 import AppTest


class _Worker:
    def __init__(self, *, alive: bool = True, raises: bool = False) -> None:
        self.alive = alive
        self.raises = raises

    def is_alive(self) -> bool:
        if self.raises:
            raise RuntimeError("worker probe failed")
        return self.alive


def _snapshot(*, available_mb: float | None = 4096.0, cpu_percent: float | None = 10.0) -> ResourceSnapshot:
    return ResourceSnapshot(
        timestamp=time.time(),
        system_ram_total_mb=8192.0,
        system_ram_available_mb=available_mb,
        cpu_percent=cpu_percent,
    )


def _configure(monkeypatch, **values) -> None:
    for name, value in values.items():
        monkeypatch.setattr(runtime_settings, name, value)


@pytest.fixture(autouse=True)
def _reset_resource_state():
    reset_resource_monitor_state()
    yield
    reset_resource_monitor_state()


def test_atomic_final_slot_admission_is_thread_safe(monkeypatch) -> None:
    _configure(monkeypatch, resource_task_audio_analysis_limit=1, resource_max_active_leases=4)
    barrier = threading.Barrier(2)
    results = []
    snapshot = _snapshot()

    def request(owner: str):
        barrier.wait()
        return acquire_resource_lease("audio_analysis", owner_id=owner, snapshot=snapshot)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(request, ("owner-a", "owner-b")))

    admitted = [item[0] for item in results if item[0] is not None]
    rejections = [item[1] for item in results if item[1] is not None]
    assert len(admitted) == 1
    assert len(rejections) == 1
    assert rejections[0].code is AdmissionRejectionReason.TASK_BUSY
    assert get_active_heavy_tasks()["audio_analysis"] == 1
    assert release_resource_lease(admitted[0].lease_id, owner_id=admitted[0].owner_id)
    assert get_active_heavy_tasks()["audio_analysis"] == 0


def test_concurrent_lease_creation_has_unique_ids(monkeypatch) -> None:
    _configure(
        monkeypatch,
        resource_task_audio_analysis_limit=8,
        resource_max_active_leases=8,
    )
    snapshot = _snapshot()

    def request(index: int):
        lease, rejection = acquire_resource_lease(
            "audio_analysis", owner_id=f"owner-{index}", snapshot=snapshot
        )
        assert rejection is None
        return lease

    with ThreadPoolExecutor(max_workers=8) as executor:
        leases = list(executor.map(request, range(8)))

    assert len({lease.lease_id for lease in leases}) == 8
    assert get_active_heavy_tasks()["audio_analysis"] == 8
    for lease in leases:
        assert release_resource_lease(lease.lease_id, owner_id=lease.owner_id)


def test_unknown_task_rejected() -> None:
    lease, rejection = acquire_resource_lease(
        "not_a_task", owner_id="owner", snapshot=_snapshot()
    )
    assert lease is None
    assert rejection is not None
    assert rejection.code is AdmissionRejectionReason.UNKNOWN_TASK


def test_unknown_memory_not_treated_as_unlimited(monkeypatch) -> None:
    monkeypatch.setattr(
        resource_monitor,
        "get_resource_snapshot",
        lambda: _snapshot(available_mb=None),
    )
    lease, rejection = acquire_resource_lease("whisper_transcription", owner_id="owner")
    assert lease is None
    assert rejection is not None
    assert rejection.code is AdmissionRejectionReason.MEMORY_UNKNOWN


def test_monitoring_exception_is_contained(monkeypatch) -> None:
    def raise_monitor():
        raise OSError("monitor failed")

    monkeypatch.setattr(resource_monitor, "get_resource_snapshot", raise_monitor)
    allowed, reason = check_heavy_task_allowed("embedding_generation")
    assert allowed is False
    assert reason is not None
    lease, rejection = acquire_resource_lease("embedding_generation", owner_id="owner")
    assert lease is None
    assert rejection is not None
    assert rejection.code is AdmissionRejectionReason.MEMORY_UNKNOWN


def test_high_cpu_rejects_admission() -> None:
    lease, rejection = acquire_resource_lease(
        "audio_analysis",
        owner_id="owner",
        snapshot=_snapshot(cpu_percent=95.0),
    )
    assert lease is None
    assert rejection is not None
    assert rejection.code is AdmissionRejectionReason.CPU_HIGH


def test_low_memory_mode_tightens_heavy_admission(monkeypatch) -> None:
    _configure(monkeypatch, resource_low_memory_mode=True)
    allowed, reason = check_heavy_task_allowed(
        "whisper_transcription", snapshot=_snapshot(available_mb=1200.0)
    )
    assert allowed is False
    assert reason is not None
    diagnostics = get_resource_admission_diagnostics()
    assert diagnostics["resource_pressure"] in {"unknown", "ok", "low", "critical"}


def test_snapshot_cache_avoided_repeat_collection(monkeypatch) -> None:
    calls: list[int] = []

    def counting_snapshot():
        calls.append(1)
        return _snapshot()

    monkeypatch.setattr(resource_monitor, "collect_resource_snapshot", counting_snapshot)
    resource_monitor.get_resource_snapshot()
    resource_monitor.get_resource_snapshot()
    assert len(calls) == 1


def test_settings_clamp_invalid_resource_values() -> None:
    settings = Settings(
        resource_low_available_mb=10,
        resource_critical_available_mb=999999,
        resource_lease_heartbeat_seconds=1,
        resource_max_active_leases=999,
        resource_cpu_admission_threshold_percent=500,
        resource_task_audio_analysis_limit=999,
    )
    assert settings.resource_low_available_mb == 256
    assert settings.resource_critical_available_mb == 255
    assert settings.resource_lease_heartbeat_seconds == 30
    assert settings.resource_max_active_leases == 8
    assert settings.resource_cpu_admission_threshold_percent == 100.0
    assert settings.resource_task_audio_analysis_limit == 8


def test_low_memory_settings_clamp_budgets() -> None:
    settings = Settings(
        resource_low_memory_mode=True,
        resource_max_active_leases=8,
        resource_task_llm_inference_limit=8,
    )
    assert settings.resource_max_active_leases == 2
    assert settings.resource_cpu_admission_threshold_percent <= 70.0
    assert settings.resource_task_llm_inference_limit == 1


def test_heartbeat_and_duplicate_release_are_safe() -> None:
    lease, rejection = acquire_resource_lease(
        "audio_analysis", owner_id="owner", snapshot=_snapshot()
    )
    assert lease is not None
    assert rejection is None
    original_expiry = lease.expires_ns
    assert heartbeat_resource_lease(
        lease.lease_id,
        owner_id="owner",
        now_ns=lease.last_heartbeat_ns + 1_000_000_000,
    )
    assert lease.expires_ns > original_expiry
    assert release_resource_lease(lease.lease_id, owner_id="owner")
    assert release_resource_lease(lease.lease_id, owner_id="owner")
    assert get_active_heavy_tasks()["audio_analysis"] == 0


def test_wrong_owner_release_is_rejected() -> None:
    lease, _ = acquire_resource_lease("audio_analysis", owner_id="owner", snapshot=_snapshot())
    assert lease is not None
    assert not release_resource_lease(lease.lease_id, owner_id="other")
    assert get_active_heavy_tasks()["audio_analysis"] == 1
    assert release_resource_lease(lease.lease_id, owner_id="owner")


def test_expired_lease_dry_run_and_recovery() -> None:
    lease, _ = acquire_resource_lease("audio_analysis", owner_id="owner", snapshot=_snapshot())
    assert lease is not None
    expired_at = lease.expires_ns + 1

    dry = inspect_stale_leases(now_ns=expired_at, dry_run=True)
    assert dry["expired_count"] == 1
    assert dry["recoverable_count"] == 1
    assert dry["recovered_count"] == 0
    assert get_active_heavy_tasks()["audio_analysis"] == 1

    recovered = recover_stale_leases(now_ns=expired_at)
    assert recovered["recovered_count"] == 1
    assert get_active_heavy_tasks()["audio_analysis"] == 0
    assert recover_stale_leases(now_ns=expired_at)["recovered_count"] == 0


def test_expired_lease_with_alive_worker_is_preserved() -> None:
    worker = _Worker(alive=True)
    lease, _ = acquire_resource_lease(
        "audio_analysis", owner_id="owner", snapshot=_snapshot(), worker=worker
    )
    assert lease is not None
    expired_at = lease.expires_ns + 1
    result = inspect_stale_leases(now_ns=expired_at, dry_run=True)
    assert result["preserved_count"] == 1
    assert result["recoverable_count"] == 0
    assert not release_resource_lease(lease.lease_id, worker_stopped=None)
    worker.alive = False
    assert release_resource_lease(lease.lease_id, worker_stopped=None)


def test_expired_lease_with_unknown_worker_is_preserved() -> None:
    worker = _Worker(raises=True)
    lease, _ = acquire_resource_lease(
        "audio_analysis", owner_id="owner", snapshot=_snapshot(), worker=worker
    )
    assert lease is not None
    expired_at = lease.expires_ns + 1
    result = inspect_stale_leases(now_ns=expired_at, dry_run=True)
    assert result["preserved_count"] == 1
    assert result["preserved"][0]["worker_status"] == "unknown"
    assert get_active_heavy_tasks()["audio_analysis"] == 1
    assert release_resource_lease(lease.lease_id, worker_stopped=True)


def test_exception_in_lease_context_rolls_back() -> None:
    captured: dict[str, object] = {}
    with (
        pytest.raises(ValueError),
        resource_lease("audio_analysis", owner_id="owner", snapshot=_snapshot()) as lease,
    ):
        captured["lease"] = lease
        raise ValueError("failed startup")
    lease = captured["lease"]
    assert isinstance(lease, resource_monitor.ResourceLease)
    assert _LEASE_MANAGER._leases[lease.lease_id].release_status is LeaseReleaseStatus.ROLLED_BACK
    assert get_active_heavy_tasks()["audio_analysis"] == 0


def test_successful_lease_context_releases() -> None:
    with resource_lease("audio_analysis", owner_id="owner", snapshot=_snapshot()):
        assert get_active_heavy_tasks()["audio_analysis"] == 1
    assert get_active_heavy_tasks()["audio_analysis"] == 0


def test_recovery_is_session_scoped_and_idempotent(monkeypatch) -> None:
    _configure(
        monkeypatch,
        resource_task_audio_analysis_limit=2,
        resource_max_active_leases=4,
    )
    snapshot = _snapshot()
    lease_a, _ = acquire_resource_lease("audio_analysis", owner_id="session-a", snapshot=snapshot)
    lease_b, _ = acquire_resource_lease("audio_analysis", owner_id="session-b", snapshot=snapshot)
    assert lease_a is not None
    assert lease_b is not None

    expired_at = lease_a.expires_ns + 1
    heartbeat_resource_lease(lease_b.lease_id, owner_id="session-b", now_ns=expired_at - 1)
    assert not release_resource_lease(lease_a.lease_id, owner_id="session-b")
    assert get_active_heavy_tasks()["audio_analysis"] == 2

    dry = inspect_stale_leases(now_ns=expired_at, dry_run=True)
    assert [item["lease_id"] for item in dry["recoverable"]] == [lease_a.lease_id]
    recovered = recover_stale_leases(now_ns=expired_at)
    assert recovered["recovered_count"] == 1
    assert get_active_heavy_tasks()["audio_analysis"] == 1
    assert release_resource_lease(lease_b.lease_id, owner_id="session-b")


def test_diagnostics_contains_required_counts_and_no_owner_secret() -> None:
    lease, _ = acquire_resource_lease("whisper_transcription", owner_id="session-secret", snapshot=_snapshot())
    assert lease is not None
    diagnostics = get_resource_admission_diagnostics()
    assert diagnostics["active_lease_count"] == 1
    assert diagnostics["active_leases_by_task"]["whisper_transcription"] == 1
    assert "expired_lease_count" in diagnostics
    assert "recovered_lease_count" in diagnostics
    assert "monitoring_available" in diagnostics
    assert diagnostics["leases"][0]["owner_key"] != "session-secret"
    assert "session-secret" not in str(diagnostics)
    release_resource_lease(lease.lease_id, owner_id="session-secret")


def test_whisper_lease_covers_lazy_segment_iteration(monkeypatch, tmp_path) -> None:
    active = {"count": 0}

    @contextmanager
    def tracking(task):
        active["count"] += 1
        try:
            yield
        finally:
            active["count"] -= 1

    monkeypatch.setattr(speech_module, "acquire_heavy_task", tracking)
    monkeypatch.setattr(speech_module, "is_audio_file", lambda path: True)

    class FakeModel:
        def transcribe(self, *args, **kwargs):
            def segments():
                assert active["count"] == 1
                yield SimpleNamespace(text="hello", start=0.0, end=1.0)

            return segments(), SimpleNamespace(language="en", duration=1.0)

    engine = speech_module.SpeechToText()
    engine._model = FakeModel()
    engine._model_config = ("fake", "cpu", "int8")
    monkeypatch.setattr(engine, "_load_model", lambda *args, **kwargs: None)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fake")

    result = engine.transcribe(str(audio))
    assert result.text == "hello"
    assert active["count"] == 0


def test_embedding_model_load_is_admitted(monkeypatch) -> None:
    active = {"count": 0}

    @contextmanager
    def tracking(task):
        active["count"] += 1
        try:
            yield
        finally:
            active["count"] -= 1

    class FakeSentenceTransformer:
        def __init__(self, *args, **kwargs):
            assert active["count"] == 1

        def get_embedding_dimension(self) -> int:
            return 3

        def encode(self, texts, **kwargs):
            return np.zeros((len(texts), 3), dtype=float)

    monkeypatch.setattr(embeddings_module, "acquire_heavy_task", tracking)
    monkeypatch.setattr(embeddings_module, "SentenceTransformer", FakeSentenceTransformer)
    embeddings_module.reset_embedding_model()
    try:
        model = embeddings_module.get_embedding_model()
        assert active["count"] == 0
        result = model.encode(["one", "two"])
        assert result.shape == (2, 3)
        assert active["count"] == 0
    finally:
        embeddings_module.reset_embedding_model()


def test_video_recorder_lease_uses_owner_and_releases_after_stop(monkeypatch) -> None:
    monkeypatch.setattr(resource_monitor, "get_resource_snapshot", _snapshot)
    recorder = LiveVideoRecorder(queue_maxsize=2)
    try:
        recorder.set_enabled(True)
        assert recorder._encoding_lease_id
        assert recorder.is_alive()
        assert get_active_heavy_tasks()["video_processing"] == 1
        wrong = release_resource_lease(
            recorder._encoding_lease_id,
            owner_id="wrong-owner",
            worker_stopped=True,
        )
        assert not wrong
        recorder.set_enabled(False)
        assert recorder.finalize() == b""
        assert get_active_heavy_tasks()["video_processing"] == 0
    finally:
        recorder.clear()


def test_resource_lease_context_rejection_raises_busy_error(monkeypatch) -> None:
    _configure(monkeypatch, resource_task_audio_analysis_limit=1, resource_max_active_leases=4)
    lease, _ = acquire_resource_lease("audio_analysis", owner_id="owner", snapshot=_snapshot())
    assert lease is not None
    with (
        pytest.raises(ResourceBusyError),
        resource_lease("audio_analysis", owner_id="other", snapshot=_snapshot()),
    ):
        pass
    release_resource_lease(lease.lease_id, owner_id="owner")


def test_diagnostics_reruns_safely_in_streamlit() -> None:
    at = AppTest.from_string(
        """
import streamlit as st
from services.resource_monitor import get_resource_admission_diagnostics
diagnostics = get_resource_admission_diagnostics()
st.metric("active leases", diagnostics["active_lease_count"])
"""
    ).run(timeout=30)
    assert not at.exception
    at.run(timeout=30)
    assert not at.exception
