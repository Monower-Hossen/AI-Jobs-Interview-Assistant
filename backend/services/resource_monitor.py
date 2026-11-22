"""Lightweight Windows-compatible resource monitoring and heavy-task guardrails.

Phase 1: baseline profiling and guardrails only. No sensitive user data is collected.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    psutil = None  # type: ignore[assignment]


class ResourceBusyError(RuntimeError):
    """Raised when a heavy operation cannot start due to guardrails."""


class MemoryPressureError(RuntimeError):
    """Raised when available memory is too low for a heavy operation."""


@dataclass(frozen=True)
class ResourceSnapshot:
    timestamp: float
    system_ram_total_mb: float | None = None
    system_ram_available_mb: float | None = None
    system_ram_used_mb: float | None = None
    process_rss_mb: float | None = None
    cpu_percent: float | None = None
    gpu_total_vram_mb: float | None = None
    gpu_used_vram_mb: float | None = None
    gpu_free_vram_mb: float | None = None
    gpu_name: str | None = None
    gpu_available: bool = False
    gpu_error: str | None = None
    monitor_errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def memory_pressure(self) -> str:
        available = self.system_ram_available_mb
        if available is None:
            return "unknown"
        try:
            from config.settings import settings

            critical = float(getattr(settings, "resource_critical_available_mb", 900))
            low = float(getattr(settings, "resource_low_available_mb", 1600))
        except Exception:
            critical, low = 900.0, 1600.0
        if available <= critical:
            return "critical"
        if available <= low:
            return "low"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "system_ram_total_mb": self.system_ram_total_mb,
            "system_ram_available_mb": self.system_ram_available_mb,
            "system_ram_used_mb": self.system_ram_used_mb,
            "process_rss_mb": self.process_rss_mb,
            "cpu_percent": self.cpu_percent,
            "gpu_total_vram_mb": self.gpu_total_vram_mb,
            "gpu_used_vram_mb": self.gpu_used_vram_mb,
            "gpu_free_vram_mb": self.gpu_free_vram_mb,
            "gpu_name": self.gpu_name,
            "gpu_available": self.gpu_available,
            "gpu_error": self.gpu_error,
            "memory_pressure": self.memory_pressure,
            "monitor_errors": list(self.monitor_errors),
        }


_HEAVY_TASK_LABELS = {
    "whisper_transcription": "Speech transcription",
    "video_processing": "Video processing",
    "embedding_generation": "Embedding generation",
}

_STATE_LOCK = threading.RLock()
_ACTIVE: dict[str, int] = {
    "whisper_transcription": 0,
    "video_processing": 0,
    "embedding_generation": 0,
}
_CACHE_LOCK = threading.RLock()
_CACHED_SNAPSHOT: ResourceSnapshot | None = None
_CACHED_AT: float = 0.0


def reset_resource_monitor_state() -> None:
    """Reset cache and active counters. Intended for tests only."""
    global _CACHED_SNAPSHOT, _CACHED_AT
    with _STATE_LOCK:
        for key in _ACTIVE:
            _ACTIVE[key] = 0
    with _CACHE_LOCK:
        _CACHED_SNAPSHOT = None
        _CACHED_AT = 0.0


def _mb(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value) / (1024 * 1024), 1)
    except Exception:
        return None


def _get_windows_memory_ctypes() -> tuple[float | None, float | None, float | None]:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return None, None, None
        total = _mb(stat.ullTotalPhys)
        available = _mb(stat.ullAvailPhys)
        used = round(total - available, 1) if total is not None and available is not None else None
        return total, available, used
    except Exception:
        return None, None, None


def _get_process_rss_mb() -> float | None:
    if psutil is not None:
        try:
            return _mb(psutil.Process(os.getpid()).memory_info().rss)
        except Exception:
            pass
    try:
        import ctypes
        import ctypes.wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.wintypes.DWORD),
                ("PageFaultCount", ctypes.wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return _mb(counters.WorkingSetSize)
    except Exception:
        pass
    return None


def _get_system_memory() -> tuple[float | None, float | None, float | None, str | None]:
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            return _mb(vm.total), _mb(vm.available), _mb(vm.used), None
        except Exception as exc:
            logger.debug("psutil memory read failed: %s", exc)
    total, available, used = _get_windows_memory_ctypes()
    if total is not None or available is not None:
        return total, available, used, None
    return None, None, None, "System memory metrics unavailable"


def _get_cpu_percent() -> tuple[float | None, str | None]:
    if psutil is None:
        return None, None
    try:
        return round(float(psutil.cpu_percent(interval=None)), 1), None
    except Exception as exc:
        return None, f"CPU metrics unavailable: {exc}"


def _get_gpu_snapshot() -> dict[str, Any]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return {
            "gpu_available": False,
            "gpu_error": "GPU monitoring unavailable (nvidia-smi not found)",
        }
    try:
        completed = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=name,memory.total,memory.used,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        logger.debug("nvidia-smi failed: %s", exc)
        return {"gpu_available": False, "gpu_error": "GPU monitoring unavailable"}

    if completed.returncode != 0:
        return {"gpu_available": False, "gpu_error": "GPU monitoring unavailable"}

    line = (completed.stdout or "").strip().splitlines()
    if not line:
        return {"gpu_available": False, "gpu_error": "GPU monitoring unavailable"}
    parts = [p.strip() for p in line[0].split(",")]
    if len(parts) < 4:
        return {"gpu_available": False, "gpu_error": "GPU monitoring unavailable"}
    try:
        total = float(parts[1])
        used = float(parts[2])
        free = float(parts[3])
    except Exception:
        return {"gpu_available": False, "gpu_error": "GPU monitoring unavailable"}
    return {
        "gpu_available": True,
        "gpu_name": parts[0],
        "gpu_total_vram_mb": round(total, 1),
        "gpu_used_vram_mb": round(used, 1),
        "gpu_free_vram_mb": round(free, 1),
        "gpu_error": None,
    }


def collect_resource_snapshot() -> ResourceSnapshot:
    errors: list[str] = []
    total, available, used, mem_err = _get_system_memory()
    if mem_err:
        errors.append(mem_err)
    process_rss = _get_process_rss_mb()
    cpu, cpu_err = _get_cpu_percent()
    if cpu_err:
        errors.append(cpu_err)

    gpu: dict[str, Any]
    try:
        gpu = _get_gpu_snapshot()
    except Exception as exc:  # defensive: never crash app
        logger.debug("GPU snapshot failed: %s", exc)
        gpu = {"gpu_available": False, "gpu_error": "GPU monitoring unavailable"}
    if gpu.get("gpu_error"):
        errors.append(str(gpu["gpu_error"]))

    return ResourceSnapshot(
        timestamp=time.time(),
        system_ram_total_mb=total,
        system_ram_available_mb=available,
        system_ram_used_mb=used,
        process_rss_mb=process_rss,
        cpu_percent=cpu,
        gpu_total_vram_mb=gpu.get("gpu_total_vram_mb"),
        gpu_used_vram_mb=gpu.get("gpu_used_vram_mb"),
        gpu_free_vram_mb=gpu.get("gpu_free_vram_mb"),
        gpu_name=gpu.get("gpu_name"),
        gpu_available=bool(gpu.get("gpu_available")),
        gpu_error=gpu.get("gpu_error"),
        monitor_errors=tuple(errors),
    )


def get_resource_snapshot(*, force: bool = False, max_age_seconds: float | None = None) -> ResourceSnapshot:
    global _CACHED_SNAPSHOT, _CACHED_AT
    try:
        from config.settings import settings

        ttl = float(getattr(settings, "resource_monitor_cache_seconds", 15))
    except Exception:
        ttl = 15.0
    if max_age_seconds is not None:
        ttl = float(max_age_seconds)
    now = time.time()
    with _CACHE_LOCK:
        if not force and _CACHED_SNAPSHOT is not None and (now - _CACHED_AT) <= max(0.0, ttl):
            return _CACHED_SNAPSHOT
    snapshot = collect_resource_snapshot()
    with _CACHE_LOCK:
        _CACHED_SNAPSHOT = snapshot
        _CACHED_AT = snapshot.timestamp
    return snapshot


def get_active_heavy_tasks() -> dict[str, int]:
    with _STATE_LOCK:
        return dict(_ACTIVE)


def _task_label(name: str) -> str:
    return _HEAVY_TASK_LABELS.get(name, name.replace("_", " ").title())


def check_heavy_task_allowed(
    task: str, *, snapshot: ResourceSnapshot | None = None
) -> tuple[bool, str | None]:
    if task not in _ACTIVE:
        return True, None
    try:
        from config.settings import settings

        enabled = bool(getattr(settings, "resource_guardrails_enabled", True))
        critical_mb = float(getattr(settings, "resource_critical_available_mb", 900))
        allow_concurrent = bool(getattr(settings, "resource_allow_concurrent_heavy_ml", False))
    except Exception:
        enabled = True
        critical_mb = 900.0
        allow_concurrent = False

    with _STATE_LOCK:
        active = dict(_ACTIVE)

    if active.get(task, 0) > 0:
        return False, f"{_task_label(task)} is already running. Please wait for it to finish."

    if not allow_concurrent:
        if task == "whisper_transcription" and active.get("video_processing", 0) > 0:
            return False, "Video processing is running. Please wait before starting transcription."
        if task == "video_processing" and active.get("whisper_transcription", 0) > 0:
            return False, "Speech transcription is running. Please wait before processing video."
        if task == "embedding_generation" and active.get("video_processing", 0) > 0:
            return False, "Video processing is running. Please wait before generating embeddings."
        if task == "video_processing" and active.get("embedding_generation", 0) > 0:
            return False, "Embedding generation is running. Please wait before processing video."

    if enabled:
        snap = snapshot or get_resource_snapshot()
        available = snap.system_ram_available_mb
        if available is not None and available <= critical_mb:
            return (
                False,
                "Available system memory is critically low. Try again after closing other apps or use low-memory mode.",
            )
    return True, None


def try_begin_heavy_task(task: str, *, snapshot: ResourceSnapshot | None = None) -> tuple[bool, str | None]:
    """Non-context-manager lease for long-running work. Caller must end it."""
    allowed, reason = check_heavy_task_allowed(task, snapshot=snapshot)
    if not allowed:
        return False, reason
    with _STATE_LOCK:
        _ACTIVE[task] = _ACTIVE.get(task, 0) + 1
    return True, None


def end_heavy_task(task: str) -> None:
    with _STATE_LOCK:
        _ACTIVE[task] = max(0, _ACTIVE.get(task, 0) - 1)


@contextmanager
def acquire_heavy_task(task: str, *, snapshot: ResourceSnapshot | None = None) -> Iterator[None]:
    allowed, reason = try_begin_heavy_task(task, snapshot=snapshot)
    if not allowed:
        raise ResourceBusyError(reason or "System is busy. Please try again.")
    try:
        yield
    finally:
        end_heavy_task(task)


def format_resource_status(snapshot: ResourceSnapshot | None = None) -> str:
    snap = snapshot or get_resource_snapshot()
    parts = []
    if snap.system_ram_available_mb is not None and snap.system_ram_total_mb is not None:
        parts.append(
            f"RAM free {snap.system_ram_available_mb:.0f}/{snap.system_ram_total_mb:.0f} MB"
        )
    if snap.process_rss_mb is not None:
        parts.append(f"App {snap.process_rss_mb:.0f} MB")
    if snap.cpu_percent is not None:
        parts.append(f"CPU {snap.cpu_percent:.0f}%")
    if snap.gpu_available and snap.gpu_free_vram_mb is not None and snap.gpu_total_vram_mb is not None:
        parts.append(
            f"VRAM free {snap.gpu_free_vram_mb:.0f}/{snap.gpu_total_vram_mb:.0f} MB"
        )
    elif snap.gpu_error:
        parts.append("GPU n/a")
    return " | ".join(parts) if parts else "Resource metrics unavailable"
