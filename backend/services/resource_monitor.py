"""Lightweight Windows-compatible resource monitoring and heavy-task guardrails.

Phase 1: baseline profiling and guardrails only. No sensitive user data is collected.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    psutil = None  # type: ignore[assignment]


class ResourceBusyError(RuntimeError):
    """Raised when a heavy operation cannot start due to guardrails."""


class MemoryPressureError(RuntimeError):
    """Raised when available memory is too low for a heavy operation."""


class AdmissionRejectionReason(str, Enum):
    UNKNOWN_TASK = "unknown_task"
    INVALID_OWNER = "invalid_owner"
    TASK_BUSY = "task_busy"
    TASK_EXCLUDED = "task_excluded"
    WORKLOAD_LIMIT = "workload_limit"
    MEMORY_LOW = "memory_low"
    MEMORY_UNKNOWN = "memory_unknown"
    CPU_HIGH = "cpu_high"
    LEASE_NOT_FOUND = "lease_not_found"
    NOT_OWNER = "not_owner"
    WORKER_ACTIVE = "worker_active"
    WORKER_UNKNOWN = "worker_unknown"


class LeaseStatus(str, Enum):
    REQUESTED = "REQUESTED"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    RELEASED = "RELEASED"


class LeaseReleaseStatus(str, Enum):
    PENDING = "pending"
    RELEASED = "released"
    ROLLED_BACK = "rolled_back"
    RECOVERED = "recovered"


@dataclass(frozen=True)
class AdmissionRejection:
    code: AdmissionRejectionReason
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "reason": self.message}


def _redact_owner(owner_id: str) -> str:
    return hashlib.sha256(str(owner_id).encode("utf-8", "replace")).hexdigest()[:12]


@dataclass
class ResourceLease:
    lease_id: str
    task: str
    owner_id: str
    created_ns: int
    last_heartbeat_ns: int
    expires_ns: int
    heartbeat_timeout_seconds: float
    reservation: dict[str, Any] = field(default_factory=dict)
    status: LeaseStatus = LeaseStatus.ACTIVE
    release_status: LeaseReleaseStatus = LeaseReleaseStatus.PENDING
    worker_ref: Any | None = None
    worker_probe: str | None = None

    @property
    def owner_key(self) -> str:
        return _redact_owner(self.owner_id)

    def is_expired(self, *, now_ns: int | None = None) -> bool:
        current = time.monotonic_ns() if now_ns is None else now_ns
        return current >= self.expires_ns

    def to_dict(self, *, now_ns: int | None = None) -> dict[str, Any]:
        current = time.monotonic_ns() if now_ns is None else now_ns
        return {
            "lease_id": self.lease_id,
            "task": self.task,
            "owner_key": self.owner_key,
            "created_monotonic_ns": self.created_ns,
            "last_heartbeat_monotonic_ns": self.last_heartbeat_ns,
            "expires_monotonic_ns": self.expires_ns,
            "heartbeat_age_seconds": max(0.0, (current - self.last_heartbeat_ns) / 1_000_000_000),
            "expired": self.is_expired(now_ns=current),
            "reservation": dict(self.reservation),
            "status": self.status.value,
            "release_status": self.release_status.value,
        }


class _ResourceLeaseManager:
    _HEAVY_TASKS = frozenset(
        {
            "whisper_transcription",
            "video_processing",
            "embedding_generation",
            "live_recording",
            "video_analysis",
        }
    )
    _DEFAULT_BUDGETS: ClassVar[dict[str, int]] = {
        "whisper_transcription": 1,
        "video_processing": 1,
        "embedding_generation": 1,
        "live_recording": 1,
        "llm_inference": 4,
        "video_analysis": 1,
        "audio_analysis": 2,
    }
    _EXCLUSIONS: ClassVar[dict[str, tuple[str, ...]]] = {
        "whisper_transcription": ("video_processing",),
        "video_processing": ("whisper_transcription", "embedding_generation"),
        "embedding_generation": ("video_processing",),
        "audio_analysis": (),
        "video_analysis": ("whisper_transcription", "embedding_generation"),
        "live_recording": (),
        "llm_inference": (),
    }

    def __init__(self) -> None:
        self._leases: dict[str, ResourceLease] = {}
        self._legacy_by_thread: dict[tuple[int, str], list[str]] = {}
        self._rejected: dict[str, int] = {}
        self._recovered = 0

    @staticmethod
    def _settings() -> Any:
        try:
            from config.settings import settings

            return settings
        except Exception:
            return None

    @staticmethod
    def _setting(settings: Any, name: str, default: Any) -> Any:
        try:
            return getattr(settings, name, default)
        except Exception:
            return default

    @staticmethod
    def _bounded_number(value: Any, *, default: float, minimum: float, maximum: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = float(default)
        if not -1e12 < number < 1e12:
            number = float(default)
        return max(minimum, min(maximum, number))

    def _budget_for(self, task: str, settings: Any) -> int:
        default = int(self._DEFAULT_BUDGETS.get(task, 1))
        raw = self._setting(settings, f"resource_task_{task}_limit", default)
        budget = int(self._bounded_number(raw, default=default, minimum=1, maximum=8))
        low_memory = bool(self._setting(settings, "resource_low_memory_mode", False))
        allow_concurrent = bool(self._setting(settings, "resource_allow_concurrent_heavy_ml", False))
        if task in self._HEAVY_TASKS and (low_memory or not allow_concurrent):
            return 1
        return budget

    def _counts_locked(self) -> dict[str, int]:
        counts = {task: 0 for task in _ACTIVE}
        for lease in self._leases.values():
            if lease.status in (LeaseStatus.ACTIVE, LeaseStatus.EXPIRED):
                counts[lease.task] = counts.get(lease.task, 0) + 1
        return counts

    def _record_rejection(self, code: AdmissionRejectionReason) -> None:
        with _STATE_LOCK:
            self._rejected[code.value] = self._rejected.get(code.value, 0) + 1

    def _evaluate_locked(
        self,
        task: str,
        *,
        snapshot: Any,
        settings: Any,
        now_ns: int,
    ) -> AdmissionRejection | None:
        if task not in _ACTIVE:
            return AdmissionRejection(
                AdmissionRejectionReason.UNKNOWN_TASK,
                "This resource task is not recognized. Please try again.",
            )

        counts = self._counts_locked()
        budget = self._budget_for(task, settings)
        if counts.get(task, 0) >= budget:
            return AdmissionRejection(
                AdmissionRejectionReason.TASK_BUSY,
                f"{_task_label(task)} is already running. Please wait for it to finish.",
            )

        low_memory = bool(self._setting(settings, "resource_low_memory_mode", False))
        allow_concurrent = bool(self._setting(settings, "resource_allow_concurrent_heavy_ml", False))
        if not allow_concurrent or low_memory:
            for other in self._EXCLUSIONS.get(task, ()):
                if counts.get(other, 0) > 0:
                    return AdmissionRejection(
                        AdmissionRejectionReason.TASK_EXCLUDED,
                        f"{_task_label(other)} is running. Please wait before starting {_task_label(task).lower()}.",
                    )

        max_active = int(
            self._bounded_number(
                self._setting(settings, "resource_max_active_leases", 2 if low_memory else 4),
                default=4,
                minimum=1,
                maximum=8,
            )
        )
        if sum(counts.values()) >= max_active:
            return AdmissionRejection(
                AdmissionRejectionReason.WORKLOAD_LIMIT,
                "The application is already running other heavy tasks. Please try again shortly.",
            )

        available = getattr(snapshot, "system_ram_available_mb", None)
        if available is None:
            return AdmissionRejection(
                AdmissionRejectionReason.MEMORY_UNKNOWN,
                "Resource monitoring is unavailable. Please retry with system metrics available.",
            )

        guardrails_enabled = bool(self._setting(settings, "resource_guardrails_enabled", True))
        if guardrails_enabled:
            critical_mb = self._bounded_number(
                self._setting(settings, "resource_critical_available_mb", 900),
                default=900,
                minimum=128,
                maximum=32768,
            )
            low_mb = self._bounded_number(
                self._setting(settings, "resource_low_available_mb", 1600),
                default=1600,
                minimum=256,
                maximum=32768,
            )
            required_mb = low_mb if low_memory and task in self._HEAVY_TASKS else critical_mb
            if float(available) <= required_mb:
                return AdmissionRejection(
                    AdmissionRejectionReason.MEMORY_LOW,
                    "Available system memory is critically low. Close other apps or wait for other heavy work to finish, then retry.",
                )

            cpu_percent = getattr(snapshot, "cpu_percent", None)
            if cpu_percent is not None:
                cpu_default = 70.0 if low_memory else 85.0
                cpu_limit = self._bounded_number(
                    self._setting(settings, "resource_cpu_admission_threshold_percent", cpu_default),
                    default=cpu_default,
                    minimum=10,
                    maximum=100,
                )
                if float(cpu_percent) >= cpu_limit:
                    return AdmissionRejection(
                        AdmissionRejectionReason.CPU_HIGH,
                        "The processor is currently busy. Please wait for existing work to finish.",
                    )
        return None

    def evaluate(
        self,
        task: str,
        *,
        snapshot: Any,
        settings: Any | None = None,
        now_ns: int | None = None,
    ) -> AdmissionRejection | None:
        active_settings = settings if settings is not None else self._settings()
        current = time.monotonic_ns() if now_ns is None else now_ns
        with _STATE_LOCK:
            return self._evaluate_locked(
                task,
                snapshot=snapshot,
                settings=active_settings,
                now_ns=current,
            )

    def acquire(
        self,
        task: str,
        *,
        owner_id: str,
        snapshot: Any,
        worker: Any | None = None,
        worker_probe: str | None = None,
        reservation: dict[str, Any] | None = None,
    ) -> tuple[ResourceLease | None, AdmissionRejection | None]:
        if not str(owner_id or "").strip():
            invalid_owner = AdmissionRejection(
                AdmissionRejectionReason.INVALID_OWNER,
                "A resource lease owner is required.",
            )
            self._record_rejection(invalid_owner.code)
            return None, invalid_owner

        settings = self._settings()
        now = time.monotonic_ns()
        heartbeat_seconds = self._bounded_number(
            self._setting(settings, "resource_lease_heartbeat_seconds", 300),
            default=300,
            minimum=30,
            maximum=3600,
        )
        with _STATE_LOCK:
            rejection = self._evaluate_locked(
                task,
                snapshot=snapshot,
                settings=settings,
                now_ns=now,
            )
            if rejection is not None:
                self._rejected[rejection.code.value] = self._rejected.get(rejection.code.value, 0) + 1
                return None, rejection
            lease = ResourceLease(
                lease_id=uuid.uuid4().hex,
                task=task,
                owner_id=str(owner_id),
                created_ns=now,
                last_heartbeat_ns=now,
                expires_ns=now + int(heartbeat_seconds * 1_000_000_000),
                heartbeat_timeout_seconds=heartbeat_seconds,
                reservation=dict(reservation or {"slots": 1}),
                status=LeaseStatus.ACTIVE,
                release_status=LeaseReleaseStatus.PENDING,
                worker_ref=self._worker_ref(worker),
                worker_probe=worker_probe,
            )
            self._leases[lease.lease_id] = lease
            if str(owner_id).startswith("legacy:"):
                key = (threading.get_ident(), task)
                self._legacy_by_thread.setdefault(key, []).append(lease.lease_id)
            _ACTIVE[task] = _ACTIVE.get(task, 0) + 1
        logger.info(
            "resource.admission_granted",
            extra={
                "event": "resource.admission_granted",
                "task": task,
                "lease_id": lease.lease_id,
                "owner_key": lease.owner_key,
            },
        )
        return lease, None

    @staticmethod
    def _worker_ref(worker: Any | None) -> Any | None:
        if worker is None:
            return None
        try:
            return weakref.ref(worker)
        except TypeError:
            return worker

    @staticmethod
    def _resolve_worker(worker_ref: Any | None) -> Any | None:
        if worker_ref is None:
            return None
        if isinstance(worker_ref, weakref.ReferenceType):
            return worker_ref()
        return worker_ref

    def _worker_status(self, lease: ResourceLease) -> str:
        worker = self._resolve_worker(lease.worker_ref)
        if worker is None:
            return "none" if lease.worker_ref is None else "unknown"
        probe_name = lease.worker_probe or "is_alive"
        probe = getattr(worker, probe_name, None)
        if probe is None and callable(worker):
            probe = worker
        if not callable(probe):
            return "unknown"
        try:
            return "alive" if bool(probe()) else "stopped"
        except Exception:
            return "unknown"

    def heartbeat(
        self,
        lease_id: str,
        *,
        owner_id: str | None = None,
        now_ns: int | None = None,
    ) -> bool:
        current = time.monotonic_ns() if now_ns is None else now_ns
        with _STATE_LOCK:
            lease = self._leases.get(lease_id)
            if lease is None or lease.status is LeaseStatus.RELEASED:
                return False
            if owner_id is not None and owner_id != lease.owner_id:
                return False
            lease.last_heartbeat_ns = current
            lease.expires_ns = current + int(lease.heartbeat_timeout_seconds * 1_000_000_000)
            if lease.status is LeaseStatus.EXPIRED:
                lease.status = LeaseStatus.ACTIVE
            return True

    def release(
        self,
        lease_id: str,
        *,
        owner_id: str | None = None,
        worker_stopped: bool | None = True,
        release_status: LeaseReleaseStatus = LeaseReleaseStatus.RELEASED,
    ) -> bool:
        with _STATE_LOCK:
            lease = self._leases.get(lease_id)
            if lease is None:
                return True
            if lease.status is LeaseStatus.RELEASED:
                return True
            if owner_id is not None and owner_id != lease.owner_id:
                return False

        if worker_stopped is not True:
            if worker_stopped is None:
                status = self._worker_status(lease)
                if status != "stopped":
                    return False
            elif worker_stopped is False:
                return False

        with _STATE_LOCK:
            lease = self._leases.get(lease_id)
            if lease is None or lease.status is LeaseStatus.RELEASED:
                return True
            if owner_id is not None and owner_id != lease.owner_id:
                return False
            lease.status = LeaseStatus.RELEASED
            lease.release_status = release_status
            _ACTIVE[lease.task] = max(0, _ACTIVE.get(lease.task, 0) - 1)
            key = (threading.get_ident(), lease.task)
            pending = self._legacy_by_thread.get(key)
            if pending is not None:
                self._legacy_by_thread[key] = [item for item in pending if item != lease_id]
        logger.info(
            "resource.lease_released",
            extra={
                "event": "resource.lease_released",
                "task": lease.task,
                "lease_id": lease.lease_id,
                "owner_key": lease.owner_key,
                "release_status": release_status.value,
            },
        )
        return True

    def release_legacy(self, task: str) -> bool:
        key = (threading.get_ident(), task)
        with _STATE_LOCK:
            lease_ids = list(self._legacy_by_thread.get(key, ()))
        while lease_ids:
            lease_id = lease_ids.pop()
            if self.release(lease_id, worker_stopped=True):
                return True
        return False

    def inspect_stale(
        self,
        *,
        now_ns: int | None = None,
        dry_run: bool = True,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        current = time.monotonic_ns() if now_ns is None else now_ns
        with _STATE_LOCK:
            leases = [
                lease
                for lease in self._leases.values()
                if lease.status is not LeaseStatus.RELEASED
            ]
        expired: list[ResourceLease] = []
        for lease in leases:
            limit = lease.heartbeat_timeout_seconds if timeout_seconds is None else timeout_seconds
            expires = lease.expires_ns if timeout_seconds is None else lease.last_heartbeat_ns + int(
                limit * 1_000_000_000
            )
            if current >= expires:
                expired.append(lease)

        recoverable: list[dict[str, Any]] = []
        preserved: list[dict[str, Any]] = []
        for lease in expired:
            worker_status = self._worker_status(lease)
            item = {
                "lease_id": lease.lease_id,
                "task": lease.task,
                "owner_key": lease.owner_key,
                "worker_status": worker_status,
            }
            if worker_status in ("stopped", "none"):
                recoverable.append(item)
            else:
                preserved.append(item)

        recovered = 0
        if not dry_run:
            for item in recoverable:
                if self.release(
                    item["lease_id"],
                    worker_stopped=True,
                    release_status=LeaseReleaseStatus.RECOVERED,
                ):
                    recovered += 1
            with _STATE_LOCK:
                self._recovered += recovered

        return {
            "dry_run": bool(dry_run),
            "expired_count": len(expired),
            "recoverable_count": len(recoverable),
            "preserved_count": len(preserved),
            "recovered_count": recovered,
            "recoverable": recoverable,
            "preserved": preserved,
        }

    def diagnostics(self, *, snapshot: Any) -> dict[str, Any]:
        current = time.monotonic_ns()
        with _STATE_LOCK:
            counts = self._counts_locked()
            leases = [
                lease.to_dict(now_ns=current)
                for lease in self._leases.values()
                if lease.status is not LeaseStatus.RELEASED
            ]
            expired = sum(1 for lease in leases if lease["expired"])
            rejected = dict(self._rejected)
            recovered = self._recovered
        return {
            "active_lease_count": sum(counts.values()),
            "active_leases_by_task": counts,
            "rejected_admissions": rejected,
            "expired_lease_count": expired,
            "recovered_lease_count": recovered,
            "monitoring_available": getattr(snapshot, "system_ram_available_mb", None) is not None,
            "resource_pressure": getattr(snapshot, "memory_pressure", "unknown"),
            "monitor_errors": list(getattr(snapshot, "monitor_errors", ())),
            "resource_snapshot": snapshot.to_dict(),
            "leases": leases,
        }

    def reset(self) -> None:
        with _STATE_LOCK:
            self._leases.clear()
            self._legacy_by_thread.clear()
            self._rejected.clear()
            self._recovered = 0
            for key in list(_ACTIVE):
                _ACTIVE[key] = 0


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
    "live_recording": "Live recording",
    "llm_inference": "Language model inference",
    "video_analysis": "Video analysis",
    "audio_analysis": "Audio analysis",
}

_STATE_LOCK = threading.RLock()
_ACTIVE: dict[str, int] = {task: 0 for task in _HEAVY_TASK_LABELS}
_LEASE_MANAGER = _ResourceLeaseManager()
_CACHE_LOCK = threading.RLock()
_CACHED_SNAPSHOT: ResourceSnapshot | None = None
_CACHED_AT: float = 0.0


def reset_resource_monitor_state() -> None:
    """Reset cache, leases, and active counters. Intended for tests only."""
    global _CACHED_SNAPSHOT, _CACHED_AT
    _LEASE_MANAGER.reset()
    with _STATE_LOCK:
        for key in list(_ACTIVE):
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


def _safe_resource_snapshot(snapshot: ResourceSnapshot | None) -> ResourceSnapshot:
    if snapshot is not None:
        return snapshot
    try:
        return get_resource_snapshot()
    except Exception as exc:
        logger.debug("resource monitor read failed: %s", type(exc).__name__)
        return ResourceSnapshot(
            timestamp=time.time(),
            monitor_errors=("Resource monitoring unavailable",),
        )


def check_heavy_task_allowed(
    task: str, *, snapshot: ResourceSnapshot | None = None
) -> tuple[bool, str | None]:
    snap = _safe_resource_snapshot(snapshot)
    rejection = _LEASE_MANAGER.evaluate(task, snapshot=snap)
    if rejection is None:
        return True, None
    return False, rejection.message


def acquire_resource_lease(
    task: str,
    *,
    owner_id: str,
    snapshot: ResourceSnapshot | None = None,
    worker: Any | None = None,
    worker_probe: str | None = None,
    reservation: dict[str, Any] | None = None,
) -> tuple[ResourceLease | None, AdmissionRejection | None]:
    snap = _safe_resource_snapshot(snapshot)
    return _LEASE_MANAGER.acquire(
        task,
        owner_id=owner_id,
        snapshot=snap,
        worker=worker,
        worker_probe=worker_probe,
        reservation=reservation,
    )


def release_resource_lease(
    lease_id: str,
    *,
    owner_id: str | None = None,
    worker_stopped: bool | None = True,
    release_status: LeaseReleaseStatus = LeaseReleaseStatus.RELEASED,
) -> bool:
    return _LEASE_MANAGER.release(
        lease_id,
        owner_id=owner_id,
        worker_stopped=worker_stopped,
        release_status=release_status,
    )


def heartbeat_resource_lease(
    lease_id: str,
    *,
    owner_id: str | None = None,
    now_ns: int | None = None,
) -> bool:
    return _LEASE_MANAGER.heartbeat(lease_id, owner_id=owner_id, now_ns=now_ns)


def inspect_stale_leases(
    *,
    now_ns: int | None = None,
    dry_run: bool = True,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    return _LEASE_MANAGER.inspect_stale(now_ns=now_ns, dry_run=dry_run, timeout_seconds=timeout_seconds)


def recover_stale_leases(
    *,
    now_ns: int | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    return _LEASE_MANAGER.inspect_stale(now_ns=now_ns, dry_run=False, timeout_seconds=timeout_seconds)


def get_resource_admission_diagnostics() -> dict[str, Any]:
    return _LEASE_MANAGER.diagnostics(snapshot=_safe_resource_snapshot(None))


def try_begin_heavy_task(
    task: str,
    *,
    snapshot: ResourceSnapshot | None = None,
    owner_id: str | None = None,
    worker: Any | None = None,
) -> tuple[bool, str | None]:
    """Backward-compatible non-context-manager lease API."""
    owner = owner_id or f"legacy:{threading.get_ident()}:{uuid.uuid4().hex}"
    _lease, rejection = acquire_resource_lease(
        task,
        owner_id=owner,
        snapshot=snapshot,
        worker=worker,
    )
    if rejection is not None:
        return False, rejection.message
    return True, None


def end_heavy_task(
    task: str,
    *,
    lease_id: str | None = None,
    owner_id: str | None = None,
    worker_stopped: bool | None = True,
) -> None:
    if lease_id is not None:
        release_resource_lease(
            lease_id,
            owner_id=owner_id,
            worker_stopped=worker_stopped,
        )
        return
    _LEASE_MANAGER.release_legacy(task)


@contextmanager
def resource_lease(
    task: str,
    *,
    owner_id: str,
    snapshot: ResourceSnapshot | None = None,
    worker: Any | None = None,
    worker_probe: str | None = None,
    rollback_on_error: bool = True,
) -> Iterator[ResourceLease]:
    lease, rejection = acquire_resource_lease(
        task,
        owner_id=owner_id,
        snapshot=snapshot,
        worker=worker,
        worker_probe=worker_probe,
    )
    if lease is None:
        raise ResourceBusyError(
            rejection.message if rejection is not None else "System is busy. Please try again."
        )
    try:
        yield lease
    except Exception:
        if rollback_on_error:
            release_resource_lease(
                lease.lease_id,
                owner_id=owner_id,
                worker_stopped=True,
                release_status=LeaseReleaseStatus.ROLLED_BACK,
            )
        raise
    else:
        release_resource_lease(
            lease.lease_id,
            owner_id=owner_id,
            worker_stopped=None if worker is not None else True,
        )


@contextmanager
def acquire_heavy_task(
    task: str,
    *,
    snapshot: ResourceSnapshot | None = None,
    owner_id: str | None = None,
) -> Iterator[None]:
    owner = owner_id or f"legacy:{threading.get_ident()}:{uuid.uuid4().hex}"
    lease, rejection = acquire_resource_lease(task, owner_id=owner, snapshot=snapshot)
    if lease is None:
        raise ResourceBusyError(
            rejection.message if rejection is not None else "System is busy. Please try again."
        )
    try:
        yield
    finally:
        release_resource_lease(lease.lease_id, owner_id=owner, worker_stopped=True)


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
