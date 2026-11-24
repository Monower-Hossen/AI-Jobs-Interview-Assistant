import hashlib
import os
import tempfile
import zipfile
from pathlib import Path

from config.settings import runtime_path, settings

try:
    import magic

    HAS_MAGIC = True
except ImportError:
    HAS_MAGIC = False


ALLOWED_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}

_SAFE_MIME_EXTENSIONS = frozenset(ALLOWED_MIME_TYPES)
_MIME_ALIASES = {
    ".docx": {"application/zip", "application/x-zip-compressed"},
    ".wav": {"audio/x-wav", "audio/wave", "audio/vnd.wave"},
    ".mp3": {"audio/mp3", "audio/x-mpeg"},
    ".m4a": {"audio/x-m4a"},
    ".ogg": {"audio/vorbis", "application/ogg"},
}


def _normalized_ext(ext: str) -> str:
    value = ext.strip().lower()
    if not value:
        return ""
    return value if value.startswith(".") else f".{value}"


def _mime_allowed(mime_type: str | None, ext: str, expected_mime: str) -> bool:
    if not mime_type:
        return False
    normalized = mime_type.strip().lower().split(";")[0]
    return normalized == expected_mime or normalized in _MIME_ALIASES.get(ext, set())


def _is_docx_container(file_path: str) -> bool:
    try:
        with zipfile.ZipFile(file_path) as archive:
            names = set(archive.namelist())
            return "[Content_Types].xml" in names and "word/document.xml" in names
    except Exception:
        return False


def validate_file_extension(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in (settings.allowed_extensions or [])


def validate_file_size(file_path: str) -> bool:
    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    return size_mb <= settings.max_file_size_mb


def validate_mime_type(file_path: str, expected_ext: str) -> bool:
    ext = _normalized_ext(expected_ext)
    expected_mime = ALLOWED_MIME_TYPES.get(ext)
    if expected_mime is None:
        return False

    if not HAS_MAGIC:
        return False
    try:
        mime_type = magic.from_file(file_path, mime=True)
    except Exception:
        return False
    return _mime_allowed(mime_type, ext, expected_mime) or (
        ext == ".docx" and _is_docx_container(file_path)
    )


def sanitize_filename(filename: str) -> str:
    path = Path(filename)
    name = "".join(c for c in path.stem if c.isalnum() or c in (" ", "-", "_")).strip()
    safe_name = name.replace(" ", "_") or "upload"

    suffix = "".join(c for c in path.suffix if c.isalnum())
    safe_suffix = f".{suffix.lower()}" if suffix else ""
    return f"{safe_name}{safe_suffix}"


def compute_file_hash(file_path: str) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def save_uploaded_file(uploaded_file, upload_dir: str | Path | None = None) -> tuple[str, str]:
    resolved_upload_dir = str(runtime_path(upload_dir or "data/uploads"))
    os.makedirs(resolved_upload_dir, exist_ok=True)

    original_name = getattr(uploaded_file, "name", "uploaded_file")
    safe_name = sanitize_filename(original_name)
    ext = get_file_extension(safe_name)
    if not ext or not validate_file_extension(safe_name):
        raise ValueError(f"Unsupported file extension: {ext or 'none'}")

    if hasattr(uploaded_file, "getbuffer"):
        data = uploaded_file.getbuffer()
    elif hasattr(uploaded_file, "read"):
        data = uploaded_file.read()
    else:
        raise ValueError("Uploaded file must have getbuffer() or read() method")

    max_bytes = settings.max_file_size_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise ValueError(f"File exceeds maximum size of {settings.max_file_size_mb} MB")

    with tempfile.NamedTemporaryFile(dir=resolved_upload_dir, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        if not validate_file_size(tmp_path):
            raise ValueError(f"File exceeds maximum size of {settings.max_file_size_mb} MB")
        if not validate_mime_type(tmp_path, ext):
            raise ValueError(f"Rejected MIME type for file: {safe_name}")

        file_path = os.path.join(resolved_upload_dir, safe_name)
        counter = 1
        base_name = Path(safe_name).stem
        while os.path.exists(file_path):
            file_path = os.path.join(resolved_upload_dir, f"{base_name}_{counter}{ext}")
            counter += 1

        os.replace(tmp_path, file_path)
        return file_path, safe_name
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def get_file_extension(filename: str) -> str:
    return Path(filename).suffix.lower()


def is_audio_file(filename: str) -> bool:
    audio_extensions = {".wav", ".mp3", ".m4a", ".ogg", ".flac"}
    return get_file_extension(filename) in audio_extensions


def is_document_file(filename: str) -> bool:
    doc_extensions = {".pdf", ".docx", ".txt"}
    return get_file_extension(filename) in doc_extensions


def format_file_size(size_bytes: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"
