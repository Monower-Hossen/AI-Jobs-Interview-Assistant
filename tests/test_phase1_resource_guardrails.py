from __future__ import annotations

import time

import pytest

from backend.config.settings import Settings
from backend.services.resource_monitor import (
    ResourceBusyError,
    ResourceSnapshot,
    acquire_heavy_task,
    acquire_resource_lease,
    check_heavy_task_allowed,
    end_heavy_task,
    format_resource_status,
    get_resource_admission_diagnostics,
    get_resource_snapshot,
    release_resource_lease,
    reset_resource_monitor_state,
    try_begin_heavy_task,
)


def _snapshot(available_mb: float = 3000.0) -> ResourceSnapshot:
    return ResourceSnapshot(
        timestamp=time.time(),
        system_ram_total_mb=8192.0,
        system_ram_available_mb=available_mb,
        system_ram_used_mb=5192.0,
        process_rss_mb=500.0,
        cpu_percent=20.0,
        gpu_available=False,
    )


def test_settings_phase1_safe_defaults(monkeypatch):
    monkeypatch.delenv("ENABLE_OLLAMA", raising=False)
    monkeypatch.delenv("WHISPER_MODEL", raising=False)
    monkeypatch.delenv("WHISPER_DEVICE", raising=False)
    monkeypatch.delenv("EMBEDDING_DEVICE", raising=False)
    s = Settings()
    assert s.enable_ollama is False
    assert s.whisper_model == "base"
    assert s.whisper_device == "cpu"
    assert s.embedding_device == "cpu"
    assert s.resource_guardrails_enabled is True
    assert s.resource_allow_concurrent_heavy_ml is False
    assert s.allow_cuda_inference is False


def test_settings_low_memory_mode_downgrades_whisper(monkeypatch):
    monkeypatch.setenv("RESOURCE_LOW_MEMORY_MODE", "true")
    monkeypatch.setenv("WHISPER_MODEL", "base")
    s = Settings()
    assert s.whisper_model == "tiny"


def test_settings_cuda_inference_blocked_by_default(monkeypatch):
    monkeypatch.setenv("WHISPER_DEVICE", "cuda")
    monkeypatch.setenv("EMBEDDING_DEVICE", "cuda")
    monkeypatch.delenv("ALLOW_CUDA_INFERENCE", raising=False)
    s = Settings()
    assert s.whisper_device == "cpu"
    assert s.embedding_device == "cpu"


def test_resource_snapshot_cache(monkeypatch):
    import backend.config.settings as settings_mod
    import backend.services.resource_monitor as rm

    reset_resource_monitor_state()
    calls = {"n": 0}

    def fake_collect():
        calls["n"] += 1
        return _snapshot()

    monkeypatch.setattr(rm, "collect_resource_snapshot", fake_collect)
    monkeypatch.setattr(settings_mod.settings, "resource_monitor_cache_seconds", 60)
    a = get_resource_snapshot(force=True)
    b = get_resource_snapshot()
    assert a.timestamp == b.timestamp
    assert calls["n"] == 1
    reset_resource_monitor_state()


def test_guardrail_blocks_concurrent_whisper(monkeypatch):
    import backend.config.settings as settings_mod
    import backend.services.resource_monitor as rm

    reset_resource_monitor_state()
    monkeypatch.setattr(rm, "get_resource_snapshot", lambda force=False: _snapshot())
    monkeypatch.setattr(settings_mod.settings, "resource_guardrails_enabled", True)

    with acquire_heavy_task("whisper_transcription"):
        allowed, reason = check_heavy_task_allowed("whisper_transcription")
        assert not allowed
        assert "already running" in (reason or "").lower()
        with pytest.raises(ResourceBusyError), acquire_heavy_task("whisper_transcription"):
            pass
    reset_resource_monitor_state()


def test_guardrail_blocks_embedding_during_video(monkeypatch):
    import backend.config.settings as settings_mod
    import backend.services.resource_monitor as rm

    reset_resource_monitor_state()
    monkeypatch.setattr(rm, "get_resource_snapshot", lambda force=False: _snapshot())
    monkeypatch.setattr(settings_mod.settings, "resource_guardrails_enabled", True)

    with acquire_heavy_task("video_processing"):
        allowed, reason = check_heavy_task_allowed("embedding_generation")
        assert not allowed
        assert "video" in (reason or "").lower()
    reset_resource_monitor_state()


def test_guardrail_blocks_on_critical_memory(monkeypatch):
    import backend.config.settings as settings_mod
    import backend.services.resource_monitor as rm

    reset_resource_monitor_state()
    monkeypatch.setattr(
        rm,
        "get_resource_snapshot",
        lambda force=False: _snapshot(available_mb=500.0),
    )
    monkeypatch.setattr(settings_mod.settings, "resource_guardrails_enabled", True)
    with pytest.raises(ResourceBusyError), acquire_heavy_task("embedding_generation"):
        pass
    reset_resource_monitor_state()


def test_format_resource_status_no_sensitive_data():
    snap = _snapshot()
    text = format_resource_status(snap)
    assert "RAM" in text
    assert "CPU" in text
    assert "password" not in text.lower()

def test_try_begin_and_end_heavy_task(monkeypatch):
    import backend.config.settings as settings_mod
    import backend.services.resource_monitor as rm

    reset_resource_monitor_state()
    monkeypatch.setattr(settings_mod.settings, "resource_guardrails_enabled", True)
    monkeypatch.setattr(rm, "get_resource_snapshot", lambda force=False: _snapshot())
    allowed, reason = try_begin_heavy_task("video_processing")
    assert allowed and reason is None
    allowed2, reason2 = check_heavy_task_allowed("embedding_generation")
    assert not allowed2
    assert "video" in (reason2 or "").lower()
    end_heavy_task("video_processing")
    allowed3, _ = check_heavy_task_allowed("embedding_generation")
    assert allowed3
    reset_resource_monitor_state()


def test_acquire_heavy_task_with_owner_releases_its_lease(monkeypatch) -> None:
    import backend.config.settings as settings_mod
    import backend.services.resource_monitor as rm

    reset_resource_monitor_state()
    monkeypatch.setattr(settings_mod.settings, "resource_guardrails_enabled", True)
    monkeypatch.setattr(rm, "get_resource_snapshot", lambda force=False: _snapshot())

    with acquire_heavy_task("video_processing", owner_id="owner-a"):
        assert get_resource_admission_diagnostics()["active_lease_count"] == 1

    assert get_resource_admission_diagnostics()["active_lease_count"] == 0
    assert check_heavy_task_allowed("video_processing")[0]
    reset_resource_monitor_state()


def test_end_heavy_task_without_legacy_lease_does_not_decrement_active_count(monkeypatch) -> None:
    import backend.config.settings as settings_mod
    import backend.services.resource_monitor as rm

    reset_resource_monitor_state()
    monkeypatch.setattr(settings_mod.settings, "resource_guardrails_enabled", True)
    monkeypatch.setattr(rm, "get_resource_snapshot", lambda force=False: _snapshot())
    lease, rejection = acquire_resource_lease(
        "video_processing",
        owner_id="owner-b",
        snapshot=_snapshot(),
    )
    assert lease is not None and rejection is None

    end_heavy_task("video_processing")

    assert get_resource_admission_diagnostics()["active_lease_count"] == 1
    assert release_resource_lease(lease.lease_id, owner_id="owner-b")
    assert get_resource_admission_diagnostics()["active_lease_count"] == 0
    reset_resource_monitor_state()
