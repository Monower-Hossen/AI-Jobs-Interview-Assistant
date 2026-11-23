from __future__ import annotations

from collections.abc import MutableMapping
from datetime import UTC, datetime

from services.live_session_lifecycle import (
    bump_stream_generation,
    initialize_live_session_state,
)
from services.live_webcam_events import (
    LIVE_AUDIO_QUEUE_MAXSIZE,
    BoundedAudioEventQueue,
)

CONSENT_CAMERA = "live_webcam_consent_camera"
CONSENT_MICROPHONE = "live_webcam_consent_microphone"
CAMERA_REQUESTED = "live_webcam_camera_requested"
MICROPHONE_REQUESTED = "live_webcam_microphone_requested"
AUDIO_EVENT_QUEUE = "live_webcam_audio_event_queue"
ERROR_CAMERA = "live_webcam_error_camera"
ERROR_MICROPHONE = "live_webcam_error_microphone"
CAMERA_REQUESTED_AT = "live_webcam_camera_requested_at"
MICROPHONE_REQUESTED_AT = "live_webcam_microphone_requested_at"

LIVE_WEBCAM_STATE_KEYS = (
    CONSENT_CAMERA,
    CONSENT_MICROPHONE,
    CAMERA_REQUESTED,
    MICROPHONE_REQUESTED,
    AUDIO_EVENT_QUEUE,
    ERROR_CAMERA,
    ERROR_MICROPHONE,
    CAMERA_REQUESTED_AT,
    MICROPHONE_REQUESTED_AT,
)

CAMERA = "camera"
MICROPHONE = "microphone"

_REQUEST_KEYS = {
    CAMERA: CAMERA_REQUESTED,
    MICROPHONE: MICROPHONE_REQUESTED,
}
_CONSENT_KEYS = {
    CAMERA: CONSENT_CAMERA,
    MICROPHONE: CONSENT_MICROPHONE,
}
_ERROR_KEYS = {
    CAMERA: ERROR_CAMERA,
    MICROPHONE: ERROR_MICROPHONE,
}
_REQUEST_AT_KEYS = {
    CAMERA: CAMERA_REQUESTED_AT,
    MICROPHONE: MICROPHONE_REQUESTED_AT,
}


def _validate_kind(kind: str) -> str:
    normalized = kind.strip().lower()
    if normalized not in _REQUEST_KEYS:
        raise ValueError("kind must be 'camera' or 'microphone'")
    return normalized


def initialize_live_webcam_state(state: MutableMapping) -> None:
    initialize_live_session_state(state)
    state.setdefault(CONSENT_CAMERA, False)
    state.setdefault(CONSENT_MICROPHONE, False)
    state.setdefault(CAMERA_REQUESTED, False)
    state.setdefault(MICROPHONE_REQUESTED, False)
    state.setdefault(AUDIO_EVENT_QUEUE, BoundedAudioEventQueue(LIVE_AUDIO_QUEUE_MAXSIZE))
    state.setdefault(ERROR_CAMERA, None)
    state.setdefault(ERROR_MICROPHONE, None)
    state.setdefault(CAMERA_REQUESTED_AT, None)
    state.setdefault(MICROPHONE_REQUESTED_AT, None)


def reset_live_webcam_state(state: MutableMapping) -> None:
    state[CONSENT_CAMERA] = False
    state[CONSENT_MICROPHONE] = False
    state[CAMERA_REQUESTED] = False
    state[MICROPHONE_REQUESTED] = False
    state[AUDIO_EVENT_QUEUE] = BoundedAudioEventQueue(LIVE_AUDIO_QUEUE_MAXSIZE)
    state[ERROR_CAMERA] = None
    state[ERROR_MICROPHONE] = None
    state[CAMERA_REQUESTED_AT] = None
    state[MICROPHONE_REQUESTED_AT] = None


def consent_granted(state: MutableMapping, kind: str) -> bool:
    return bool(state.get(_CONSENT_KEYS[_validate_kind(kind)], False))


def stream_requested(state: MutableMapping, kind: str) -> bool:
    return bool(state.get(_REQUEST_KEYS[_validate_kind(kind)], False))


def can_start_stream(state: MutableMapping, kind: str) -> bool:
    return consent_granted(state, kind) and not stream_requested(state, kind)


def set_stream_requested(state: MutableMapping, kind: str, requested: bool) -> None:
    normalized = _validate_kind(kind)
    state[_REQUEST_KEYS[normalized]] = bool(requested)
    state[_REQUEST_AT_KEYS[normalized]] = (
        datetime.now(UTC).replace(tzinfo=None).isoformat() if requested else None
    )
    if requested:
        bump_stream_generation(state, normalized)
    set_stream_error(state, normalized, None)
    if not requested and normalized == MICROPHONE:
        audio_event_queue(state).clear()


def stop_all_streams(state: MutableMapping) -> None:
    for kind in (CAMERA, MICROPHONE):
        set_stream_requested(state, kind, False)


def record_connection_failure(
    state: MutableMapping,
    kind: str,
    *,
    playing: bool,
    signalling: bool,
    grace_seconds: float = 6.0,
) -> bool:
    normalized = _validate_kind(kind)
    if (
        not stream_requested(state, normalized)
        or playing
        or signalling
        or stream_error(state, normalized)
    ):
        return False
    raw = state.get(_REQUEST_AT_KEYS[normalized])
    try:
        requested_at = datetime.fromisoformat(str(raw)) if raw else None
    except ValueError:
        requested_at = None
    if requested_at is None:
        state[_REQUEST_AT_KEYS[normalized]] = (
            datetime.now(UTC).replace(tzinfo=None).isoformat()
        )
        return False
    if requested_at.tzinfo is not None:
        requested_at = requested_at.astimezone(UTC).replace(tzinfo=None)
    elapsed = (datetime.now(UTC).replace(tzinfo=None) - requested_at).total_seconds()
    if elapsed < grace_seconds:
        return False
    set_stream_error(state, normalized, "permission or device unavailable")
    return True


def stream_error(state: MutableMapping, kind: str) -> str | None:
    reason = state.get(_ERROR_KEYS[_validate_kind(kind)])
    return str(reason) if reason else None


def set_stream_error(state: MutableMapping, kind: str, reason: str | None) -> None:
    state[_ERROR_KEYS[_validate_kind(kind)]] = str(reason) if reason else None


def get_audio_event_queue(state: MutableMapping) -> BoundedAudioEventQueue:
    candidate = state.get(AUDIO_EVENT_QUEUE)
    if isinstance(candidate, BoundedAudioEventQueue):
        return candidate
    replacement = BoundedAudioEventQueue(LIVE_AUDIO_QUEUE_MAXSIZE)
    state[AUDIO_EVENT_QUEUE] = replacement
    return replacement

audio_event_queue = get_audio_event_queue


def drain_audio_events(state: MutableMapping):
    return get_audio_event_queue(state).drain()


def media_error_message(kind: str, reason: str | None = None) -> str:
    label = _validate_kind(kind).title()
    text = (reason or "").lower()
    if any(token in text for token in ("notallowed", "permission", "denied")):
        return f"{label} permission denied. Allow {label.lower()} access in your browser settings, then start again."
    if any(token in text for token in ("notfound", "devicesnotfound", "overconstrained")):
        return f"No {label.lower()} device was found. Connect a device and start again."
    return (
        f"{label} unavailable. Check that the device is connected, not in use, and "
        "that browser permission was granted."
    )


def stream_status(
    kind: str,
    *,
    consent: bool,
    requested: bool,
    playing: bool,
    signalling: bool,
    error_reason: str | None = None,
) -> dict[str, str]:
    label = _validate_kind(kind).title()
    if not consent:
        return {
            "label": "Off",
            "message": f"Grant {label.lower()} consent to enable capture.",
            "tone": "info",
        }
    if not requested:
        return {
            "label": "Off",
            "message": f"{label} capture is stopped.",
            "tone": "caption",
        }
    if playing:
        return {
            "label": "Connected",
            "message": f"{label} capture is connected.",
            "tone": "success",
        }
    if signalling:
        return {
            "label": "Connecting",
            "message": f"Waiting for {label.lower()} permission and WebRTC connection...",
            "tone": "info",
        }
    return {
        "label": "Not connected",
        "message": media_error_message(label, error_reason),
        "tone": "warning",
    }
