from __future__ import annotations

import hashlib
import io

import av


def _validate_mp4_boxes(data: bytes) -> set[bytes]:
    offset = 0
    box_types: set[bytes] = set()
    while offset < len(data):
        if len(data) - offset < 8:
            raise ValueError("The camera recording is not a complete MP4 file.")
        size = int.from_bytes(data[offset : offset + 4], "big")
        box_type = bytes(data[offset + 4 : offset + 8])
        header_size = 8
        if size == 1:
            if len(data) - offset < 16:
                raise ValueError("The camera recording is not a complete MP4 file.")
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header_size = 16
        elif size == 0:
            size = len(data) - offset
        if size < header_size or offset + size > len(data):
            raise ValueError("The camera recording is not a complete MP4 file.")
        box_types.add(box_type)
        offset += size
    if b"ftyp" not in box_types or b"moov" not in box_types or b"mdat" not in box_types:
        raise ValueError("The camera recording is not a complete MP4 file.")
    return box_types


def validate_video_bytes(data: bytes) -> tuple[int, str]:
    if not data:
        raise ValueError("The camera recording is empty.")
    if len(data) < 12 or data[4:8] != b"ftyp":
        raise ValueError("The camera recording is not a valid MP4 file.")
    _validate_mp4_boxes(data)
    try:
        with av.open(io.BytesIO(data), mode="r") as container:
            if not container.streams.video:
                raise ValueError("The camera recording does not contain a video stream.")
            stream = container.streams.video[0]
            codec = getattr(getattr(stream, "codec_context", None), "name", None)
            if not codec:
                raise ValueError("The camera recording has no readable video stream.")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The camera recording is not a complete MP4 file.") from exc
    digest = hashlib.sha256(data).hexdigest()
    return len(data), digest
