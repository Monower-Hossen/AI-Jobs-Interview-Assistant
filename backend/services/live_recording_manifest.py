from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime
from typing import Any

MANIFEST_SCHEMA_VERSION = 1
CAPTURE_MANIFEST_KEY = "capture_manifest"
CLOCK_TYPE = "monotonic"
CLOCK_SOURCE = "server"
SYNC_INDEPENDENT = "independent_streams"
SYNC_ESTIMATED = "estimated_alignment"
SYNC_SYNCHRONIZED = "synchronized"
SYNC_STATUSES = {SYNC_INDEPENDENT, SYNC_ESTIMATED, SYNC_SYNCHRONIZED}
STREAM_STATUSES = {"not_started", "capturing", "stopped", "finalized", "failed", "partial"}


class ManifestValidationError(ValueError):
    pass


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"


def monotonic_now_ns() -> int:
    return time.monotonic_ns()


def _is_json_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _json_safe(value: Any, *, path: str = "manifest") -> Any:
    if _is_json_scalar(value):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise ManifestValidationError(f"{path} must be finite JSON number")
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v, path=f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v, path=f"{path}[{i}]") for i, v in enumerate(value)]
    raise ManifestValidationError(f"{path} contains non-JSON-serializable value: {type(value).__name__}")


def _require_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestValidationError(f"{path} must be an object")
    return value


def _optional_str(value: Any, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestValidationError(f"{path} must be a string or null")
    return value


def _optional_int(value: Any, path: str, *, minimum: int | None = 0) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestValidationError(f"{path} must be an integer or null")
    if minimum is not None and value < minimum:
        raise ManifestValidationError(f"{path} must be >= {minimum}")
    return int(value)


def _required_int(value: Any, path: str, *, minimum: int | None = 0) -> int:
    result = _optional_int(value, path, minimum=minimum)
    if result is None:
        raise ManifestValidationError(f"{path} must be an integer")
    return result


def _optional_float(value: Any, path: str, *, minimum: float | None = 0.0) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestValidationError(f"{path} must be a number or null")
    result = float(value)
    if math.isnan(result) or math.isinf(result):
        raise ManifestValidationError(f"{path} must be finite")
    if minimum is not None and result < minimum:
        raise ManifestValidationError(f"{path} must be >= {minimum}")
    return result


def _validate_timing_pair(start: int | None, end: int | None, path: str) -> None:
    if start is not None and end is not None and end < start:
        raise ManifestValidationError(f"{path} end must be >= start")


def create_capture_manifest(
    *,
    recording_id: int | str,
    interview_session_id: int | str,
    policy_version: str = "live-recording-v1",
    created_at_utc: str | None = None,
    started_monotonic_ns: int | None = None,
    started_utc: str | None = None,
) -> dict[str, Any]:
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "recording_id": str(recording_id),
        "interview_session_id": str(interview_session_id),
        "policy_version": str(policy_version),
        "timing": {
            "clock_type": CLOCK_TYPE,
            "clock_source": CLOCK_SOURCE,
            "recording_started_monotonic_ns": started_monotonic_ns,
            "recording_started_utc": started_utc or created_at_utc or utc_now_iso(),
            "recording_ended_monotonic_ns": None,
            "recording_ended_utc": None,
        },
        "audio": {
            "status": "not_started",
            "stream_present": False,
            "first_received_monotonic_ns": None,
            "last_received_monotonic_ns": None,
            "duration_seconds": None,
            "sample_rate": 16000,
            "channels": 1,
            "sample_format": "s16",
            "dropped_chunks": 0,
            "received_chunks": 0,
            "bytes_written": 0,
            "overflowed": False,
            "capture_error": None,
        },
        "video": {
            "status": "not_started",
            "stream_present": False,
            "first_received_monotonic_ns": None,
            "last_received_monotonic_ns": None,
            "duration_seconds": None,
            "width": None,
            "height": None,
            "target_fps": None,
            "encoded_frames": 0,
            "dropped_frames": 0,
            "queue_overflow_count": 0,
            "timestamp_adjustments": 0,
            "bytes_written": 0,
            "codec": None,
            "pixel_format": None,
            "container": "mp4",
            "overflowed": False,
            "capture_error": None,
        },
        "synchronization": {
            "status": SYNC_INDEPENDENT,
            "guaranteed_sync": False,
            "audio_video_offset_seconds": None,
            "offset_method": None,
            "limitations": [
                "Audio and video are stored separately.",
                "Server receive timestamps do not guarantee hardware capture synchronization.",
            ],
        },
        "diagnostics": {
            "last_updated_utc": utc_now_iso(),
            "persistence_error": None,
        },
    }
    return validate_capture_manifest(manifest)


def _validate_stream_common(stream: dict[str, Any], path: str) -> None:
    status = _optional_str(stream.get("status"), f"{path}.status") or "not_started"
    if status not in STREAM_STATUSES:
        raise ManifestValidationError(f"{path}.status is invalid")
    stream["status"] = status
    stream["stream_present"] = bool(stream.get("stream_present", False))
    first = _optional_int(stream.get("first_received_monotonic_ns"), f"{path}.first_received_monotonic_ns")
    last = _optional_int(stream.get("last_received_monotonic_ns"), f"{path}.last_received_monotonic_ns")
    _validate_timing_pair(first, last, f"{path}.received_monotonic_ns")
    stream["first_received_monotonic_ns"] = first
    stream["last_received_monotonic_ns"] = last
    stream["duration_seconds"] = _optional_float(stream.get("duration_seconds"), f"{path}.duration_seconds")
    stream["bytes_written"] = _required_int(stream.get("bytes_written", 0), f"{path}.bytes_written")
    stream["overflowed"] = bool(stream.get("overflowed", False))
    stream["capture_error"] = _optional_str(stream.get("capture_error"), f"{path}.capture_error")


def validate_capture_manifest(manifest: Any) -> dict[str, Any]:
    data = _json_safe(_require_dict(manifest, "manifest"))
    data = _require_dict(data, "manifest")
    schema_version = data.get("schema_version")
    if schema_version != MANIFEST_SCHEMA_VERSION:
        raise ManifestValidationError(f"Unsupported capture manifest schema_version: {schema_version!r}")
    data["schema_version"] = MANIFEST_SCHEMA_VERSION
    data["recording_id"] = str(data.get("recording_id", ""))
    data["interview_session_id"] = str(data.get("interview_session_id", ""))
    if not data["recording_id"]:
        raise ManifestValidationError("manifest.recording_id is required")
    if not data["interview_session_id"]:
        raise ManifestValidationError("manifest.interview_session_id is required")
    data["policy_version"] = _optional_str(data.get("policy_version"), "manifest.policy_version") or "live-recording-v1"

    timing = _require_dict(data.get("timing"), "manifest.timing")
    timing["clock_type"] = _optional_str(timing.get("clock_type"), "manifest.timing.clock_type") or CLOCK_TYPE
    timing["clock_source"] = _optional_str(timing.get("clock_source"), "manifest.timing.clock_source") or CLOCK_SOURCE
    if timing["clock_type"] != CLOCK_TYPE:
        raise ManifestValidationError("manifest.timing.clock_type must be 'monotonic'")
    started = _optional_int(timing.get("recording_started_monotonic_ns"), "manifest.timing.recording_started_monotonic_ns")
    ended = _optional_int(timing.get("recording_ended_monotonic_ns"), "manifest.timing.recording_ended_monotonic_ns")
    _validate_timing_pair(started, ended, "manifest.timing.recording_monotonic_ns")
    timing["recording_started_monotonic_ns"] = started
    timing["recording_ended_monotonic_ns"] = ended
    timing["recording_started_utc"] = _optional_str(timing.get("recording_started_utc"), "manifest.timing.recording_started_utc")
    timing["recording_ended_utc"] = _optional_str(timing.get("recording_ended_utc"), "manifest.timing.recording_ended_utc")
    data["timing"] = timing

    audio = _require_dict(data.get("audio"), "manifest.audio")
    _validate_stream_common(audio, "manifest.audio")
    audio["sample_rate"] = _optional_int(audio.get("sample_rate"), "manifest.audio.sample_rate", minimum=1)
    audio["channels"] = _optional_int(audio.get("channels"), "manifest.audio.channels", minimum=1)
    audio["sample_format"] = _optional_str(audio.get("sample_format"), "manifest.audio.sample_format")
    audio["dropped_chunks"] = _required_int(audio.get("dropped_chunks", 0), "manifest.audio.dropped_chunks")
    audio["received_chunks"] = _required_int(audio.get("received_chunks", 0), "manifest.audio.received_chunks")
    data["audio"] = audio

    video = _require_dict(data.get("video"), "manifest.video")
    _validate_stream_common(video, "manifest.video")
    video["width"] = _optional_int(video.get("width"), "manifest.video.width", minimum=1)
    video["height"] = _optional_int(video.get("height"), "manifest.video.height", minimum=1)
    video["target_fps"] = _optional_int(video.get("target_fps"), "manifest.video.target_fps", minimum=1)
    video["encoded_frames"] = _required_int(video.get("encoded_frames", 0), "manifest.video.encoded_frames")
    video["dropped_frames"] = _required_int(video.get("dropped_frames", 0), "manifest.video.dropped_frames")
    video["queue_overflow_count"] = _required_int(video.get("queue_overflow_count", 0), "manifest.video.queue_overflow_count")
    video["timestamp_adjustments"] = _required_int(video.get("timestamp_adjustments", 0), "manifest.video.timestamp_adjustments")
    video["codec"] = _optional_str(video.get("codec"), "manifest.video.codec")
    video["pixel_format"] = _optional_str(video.get("pixel_format"), "manifest.video.pixel_format")
    video["container"] = _optional_str(video.get("container"), "manifest.video.container")
    data["video"] = video

    sync = _require_dict(data.get("synchronization"), "manifest.synchronization")
    status = _optional_str(sync.get("status"), "manifest.synchronization.status") or SYNC_INDEPENDENT
    if status not in SYNC_STATUSES:
        raise ManifestValidationError("manifest.synchronization.status is invalid")
    sync["status"] = status
    sync["guaranteed_sync"] = bool(sync.get("guaranteed_sync", False))
    if sync["guaranteed_sync"] and status != SYNC_SYNCHRONIZED:
        raise ManifestValidationError("manifest.synchronization.guaranteed_sync requires synchronized status")
    if status == SYNC_SYNCHRONIZED:
        raise ManifestValidationError("manifest.synchronization.status 'synchronized' is not supported by current architecture")
    sync["audio_video_offset_seconds"] = _optional_float(
        sync.get("audio_video_offset_seconds"), "manifest.synchronization.audio_video_offset_seconds", minimum=None
    )
    sync["offset_method"] = _optional_str(sync.get("offset_method"), "manifest.synchronization.offset_method")
    limitations = sync.get("limitations")
    if limitations is None:
        limitations = []
    if not isinstance(limitations, list) or not all(isinstance(item, str) for item in limitations):
        raise ManifestValidationError("manifest.synchronization.limitations must be a list of strings")
    sync["limitations"] = limitations
    data["synchronization"] = sync

    diagnostics = data.get("diagnostics")
    if diagnostics is None:
        diagnostics = {}
    diagnostics = _require_dict(diagnostics, "manifest.diagnostics")
    diagnostics["last_updated_utc"] = _optional_str(diagnostics.get("last_updated_utc"), "manifest.diagnostics.last_updated_utc") or utc_now_iso()
    diagnostics["persistence_error"] = _optional_str(diagnostics.get("persistence_error"), "manifest.diagnostics.persistence_error")
    data["diagnostics"] = diagnostics

    return data


def safe_load_capture_manifest(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = json.loads(value)
        return validate_capture_manifest(value)
    except Exception:
        return None


def manifest_to_json(manifest: dict[str, Any]) -> str:
    validated = validate_capture_manifest(manifest)
    return json.dumps(validated, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _touch(manifest: dict[str, Any]) -> dict[str, Any]:
    manifest.setdefault("diagnostics", {})
    manifest["diagnostics"]["last_updated_utc"] = utc_now_iso()
    return manifest


def mark_manifest_started(
    manifest: dict[str, Any], *, started_monotonic_ns: int | None = None, started_utc: str | None = None
) -> dict[str, Any]:
    m = validate_capture_manifest(manifest)
    timing = m["timing"]
    if timing.get("recording_started_monotonic_ns") is None:
        timing["recording_started_monotonic_ns"] = _optional_int(started_monotonic_ns, "started_monotonic_ns")
    if not timing.get("recording_started_utc"):
        timing["recording_started_utc"] = started_utc or utc_now_iso()
    return validate_capture_manifest(_touch(m))


def mark_manifest_finalized(
    manifest: dict[str, Any], *, ended_monotonic_ns: int | None = None, ended_utc: str | None = None
) -> dict[str, Any]:
    m = validate_capture_manifest(manifest)
    timing = m["timing"]
    if timing.get("recording_ended_monotonic_ns") is None:
        timing["recording_ended_monotonic_ns"] = _optional_int(ended_monotonic_ns, "ended_monotonic_ns")
    if not timing.get("recording_ended_utc"):
        timing["recording_ended_utc"] = ended_utc or utc_now_iso()
    return validate_capture_manifest(_touch(m))


def _compute_estimated_offset_seconds(manifest: dict[str, Any]) -> tuple[float | None, str | None]:
    audio = manifest.get("audio", {})
    video = manifest.get("video", {})
    a_first = audio.get("first_received_monotonic_ns")
    v_first = video.get("first_received_monotonic_ns")
    if not isinstance(a_first, int) or not isinstance(v_first, int):
        return None, None
    offset = (a_first - v_first) / 1_000_000_000
    return float(offset), "server_receive_monotonic_first_frame_delta"


def update_manifest_synchronization(manifest: dict[str, Any]) -> dict[str, Any]:
    m = validate_capture_manifest(manifest)
    offset, method = _compute_estimated_offset_seconds(m)
    sync = m["synchronization"]
    if offset is None:
        sync["status"] = SYNC_INDEPENDENT
        sync["guaranteed_sync"] = False
        sync["audio_video_offset_seconds"] = None
        sync["offset_method"] = None
    else:
        sync["status"] = SYNC_ESTIMATED
        sync["guaranteed_sync"] = False
        sync["audio_video_offset_seconds"] = round(offset, 6)
        sync["offset_method"] = method
    return validate_capture_manifest(_touch(m))


def update_audio_manifest(manifest: dict[str, Any], snapshot: dict[str, Any], *, status: str | None = None) -> dict[str, Any]:
    m = validate_capture_manifest(manifest)
    audio = m["audio"]
    if status:
        if status not in STREAM_STATUSES:
            raise ManifestValidationError("audio status is invalid")
        audio["status"] = status
    received_chunks = _required_int(snapshot.get("chunk_count", audio["received_chunks"]), "audio.chunk_count")
    dropped_chunks = _required_int(snapshot.get("dropped_chunks", audio["dropped_chunks"]), "audio.dropped_chunks")
    duration = _optional_float(snapshot.get("duration_seconds"), "audio.duration_seconds")
    audio["received_chunks"] = received_chunks
    audio["dropped_chunks"] = dropped_chunks
    audio["duration_seconds"] = duration
    audio["sample_rate"] = _optional_int(snapshot.get("sample_rate"), "audio.sample_rate", minimum=1) or audio.get("sample_rate")
    audio["channels"] = _optional_int(snapshot.get("channels"), "audio.channels", minimum=1) or audio.get("channels")
    audio["sample_format"] = _optional_str(snapshot.get("sample_format"), "audio.sample_format") or audio.get("sample_format")
    audio["bytes_written"] = _required_int(snapshot.get("byte_count", audio.get("bytes_written", 0)), "audio.byte_count")
    audio["overflowed"] = bool(snapshot.get("overflowed", audio.get("overflowed", False)))
    audio["capture_error"] = _optional_str(snapshot.get("capture_error"), "audio.capture_error")
    for key in ("first_received_monotonic_ns", "last_received_monotonic_ns"):
        if snapshot.get(key) is not None:
            audio[key] = _optional_int(snapshot.get(key), f"audio.{key}")
    _validate_timing_pair(audio["first_received_monotonic_ns"], audio["last_received_monotonic_ns"], "manifest.audio.received_monotonic_ns")
    has_data = any([received_chunks > 0, audio["bytes_written"] > 0, duration not in (None, 0), audio["first_received_monotonic_ns"] is not None])
    audio["stream_present"] = bool(audio.get("stream_present") or has_data)
    if status is None:
        if audio.get("capture_error") and not has_data:
            audio["status"] = "failed"
        elif has_data and audio["status"] == "not_started":
            audio["status"] = "capturing"
    return validate_capture_manifest(update_manifest_synchronization(_touch(m)))


def update_video_manifest(manifest: dict[str, Any], snapshot: dict[str, Any], *, status: str | None = None) -> dict[str, Any]:
    m = validate_capture_manifest(manifest)
    video = m["video"]
    if status:
        if status not in STREAM_STATUSES:
            raise ManifestValidationError("video status is invalid")
        video["status"] = status
    video["encoded_frames"] = _required_int(snapshot.get("frames_encoded", video["encoded_frames"]), "video.frames_encoded")
    video["dropped_frames"] = _required_int(snapshot.get("dropped_frames", video["dropped_frames"]), "video.dropped_frames")
    video["queue_overflow_count"] = _required_int(snapshot.get("queue_overflow_count", video.get("queue_overflow_count", 0)), "video.queue_overflow_count")
    video["timestamp_adjustments"] = _required_int(snapshot.get("timestamp_adjustments", video["timestamp_adjustments"]), "video.timestamp_adjustments")
    video["duration_seconds"] = _optional_float(snapshot.get("duration_seconds"), "video.duration_seconds")
    width = snapshot.get("width")
    height = snapshot.get("height")
    video["width"] = _optional_int(width, "video.width", minimum=1) if isinstance(width, int) and width > 0 else video.get("width")
    video["height"] = _optional_int(height, "video.height", minimum=1) if isinstance(height, int) and height > 0 else video.get("height")
    video["target_fps"] = _optional_int(snapshot.get("fps", snapshot.get("target_fps")), "video.target_fps", minimum=1)
    video["bytes_written"] = _required_int(snapshot.get("bytes_written", video.get("bytes_written", 0)), "video.bytes_written")
    video["codec"] = _optional_str(snapshot.get("codec"), "video.codec") or video.get("codec")
    video["pixel_format"] = _optional_str(snapshot.get("pixel_format"), "video.pixel_format") or video.get("pixel_format")
    video["container"] = _optional_str(snapshot.get("container"), "video.container") or video.get("container")
    video["overflowed"] = bool(snapshot.get("overflowed", video.get("overflowed", False)))
    video["capture_error"] = _optional_str(snapshot.get("error"), "video.error")
    for key in ("first_received_monotonic_ns", "last_received_monotonic_ns"):
        if snapshot.get(key) is not None:
            video[key] = _optional_int(snapshot.get(key), f"video.{key}")
    _validate_timing_pair(video["first_received_monotonic_ns"], video["last_received_monotonic_ns"], "manifest.video.received_monotonic_ns")
    has_data = any([video["encoded_frames"] > 0, video["bytes_written"] > 0, (video["duration_seconds"] or 0) > 0, video["first_received_monotonic_ns"] is not None])
    video["stream_present"] = bool(video.get("stream_present") or has_data)
    if status is None:
        if video.get("capture_error") and not has_data:
            video["status"] = "failed"
        elif has_data and video["status"] == "not_started":
            video["status"] = "capturing"
    return validate_capture_manifest(update_manifest_synchronization(_touch(m)))


def merge_manifest_metadata(metadata: dict[str, Any] | None, manifest: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(metadata or {})
    if manifest is not None:
        payload[CAPTURE_MANIFEST_KEY] = validate_capture_manifest(manifest)
    return _json_safe(payload)


def extract_capture_manifest(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get(CAPTURE_MANIFEST_KEY)
    manifest = safe_load_capture_manifest(raw)
    if manifest is not None:
        return manifest
    if raw is None:
        return None
    return {
        "schema_version": raw.get("schema_version") if isinstance(raw, dict) else "unknown",
        "invalid": True,
        "error": "Unsupported or invalid capture manifest",
    }
