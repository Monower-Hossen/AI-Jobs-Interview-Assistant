from __future__ import annotations

import os
import re
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import database.db as db_module
from config.settings import runtime_path, settings
from database import (
    Base,
    DataBackup,
    DataRetentionSetting,
    LiveInterviewMedia,
    LiveInterviewRecording,
    get_db_context,
    get_readonly_db,
)
from database.db import database_operation_gate
from services.auth_service import _require_admin_actor, log_event
from services.live_interview_media_service import (
    list_admin_recordings,
    run_storage_integrity_diagnostics,
)
from sqlalchemy import func
from utils.file_utils import compute_file_hash

BACKUP_MIME = "application/x-sqlite3"
RESTORE_CONFIRMATION = "RESTORE"
PRODUCTION_CONFIRMATION = "PRODUCTION RESTORE"
BACKUP_TYPES = ("manual", "automatic", "safety")
BACKUP_STATUSES = ("pending", "completed", "failed", "deleted", "restored")
_CORE_TABLES = {"users", "user_sessions", "event_logs"}
_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:\\|/)[^\s\"']+")

RETENTION_DEFINITIONS: dict[str, dict[str, Any]] = {
    "backup_retention_days": {
        "label": "Backup retention period",
        "value": 90,
        "min": 1,
        "max": 3650,
        "unit": "days",
        "destructive": False,
    },
    "backup_max_count": {
        "label": "Backup maximum count",
        "value": 20,
        "min": 1,
        "max": 500,
        "unit": "backups",
        "destructive": False,
    },
    "recording_retention_policy": {
        "label": "Recording retention policy",
        "value": "manual",
        "options": ("manual", "active_only"),
        "unit": "policy",
        "destructive": False,
    },
    "temporary_file_retention_hours": {
        "label": "Temporary file retention",
        "value": 24,
        "min": 1,
        "max": 720,
        "unit": "hours",
        "destructive": False,
    },
    "ai_monitoring_retention_days": {
        "label": "AI monitoring retention",
        "value": 90,
        "min": 1,
        "max": 3650,
        "unit": "days",
        "destructive": False,
    },
}


class BackupServiceError(ValueError):
    """Raised when a backup or restore operation cannot safely proceed."""


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _sanitize_error(exc: object) -> str:
    text = " ".join(str(exc).split())
    text = _PATH_PATTERN.sub("<path>", text)
    return (text or type(exc).__name__)[:300]


def _format_datetime(value: object) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return "N/A"


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return _utcnow()


def _backup_root(*, create: bool = False) -> Path:
    root = Path(settings.backup_dir).expanduser().resolve()
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
    return root


def _resolve_backup_path(filename: str) -> Path:
    if not filename or Path(filename).name != filename or filename.startswith("."):
        raise BackupServiceError("Invalid backup filename.")
    root = _backup_root()
    path = (root / filename).resolve()
    if path.parent != root:
        raise BackupServiceError("Backup path is outside the configured backup directory.")
    return path


def _inspect_sqlite_file(path: Path, *, expected_checksum: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "integrity_status": "failed",
        "integrity_detail": "",
        "foreign_key_violations": 0,
        "table_count": 0,
        "page_count": 0,
        "tables": set(),
        "error": "",
    }
    if not path.is_file():
        result["integrity_status"] = "missing"
        result["integrity_detail"] = "Backup file is missing."
        return result
    try:
        if expected_checksum and compute_file_hash(str(path)) != expected_checksum:
            result["integrity_detail"] = "Backup checksum does not match the catalog."
            return result
        connection = sqlite3.connect(str(path), timeout=30)
        try:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0]).strip()
            violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0] or 0)
        finally:
            connection.close()
        result["table_count"] = len(tables)
        result["page_count"] = page_count
        result["tables"] = tables
        result["foreign_key_violations"] = violations
        result["integrity_detail"] = f"integrity_check={integrity or 'empty'}"
        if integrity.lower() == "ok" and violations == 0:
            result["integrity_status"] = "verified"
    except Exception as exc:
        result["error"] = _sanitize_error(exc)
        result["integrity_detail"] = result["error"]
    return result


def _backup_dict(row: DataBackup) -> dict[str, Any]:
    return {
        "backup_id": str(row.backup_id),
        "filename": str(row.filename),
        "created_at": _format_datetime(row.created_at),
        "backup_type": str(row.backup_type),
        "status": str(row.status),
        "integrity_status": str(row.integrity_status),
        "size_bytes": int(row.size_bytes or 0),
        "checksum": str(row.checksum or ""),
        "db_engine": str(row.db_engine or "sqlite"),
        "table_count": int(row.table_count or 0),
        "page_count": int(row.page_count or 0),
        "completed_at": _format_datetime(row.completed_at),
        "error_message": str(row.error_message or ""),
        "metadata": dict(row.metadata_json or {}),
    }


def _create_backup(actor_admin_id: int, *, backup_type: str = "manual") -> dict[str, Any]:
    if backup_type not in BACKUP_TYPES:
        raise BackupServiceError("Select a valid backup type.")
    with get_db_context() as db:
        _require_admin_actor(db, actor_admin_id)

    db_path = Path(settings.database_path).expanduser().resolve()
    if not db_path.is_file():
        raise BackupServiceError("The configured database file does not exist.")

    root = _backup_root(create=True)
    now = _utcnow()
    backup_id = f"bkp_{now:%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}"
    filename = f"{backup_id}.db"
    final_path = root / filename
    temp_path = root / f".{filename}.part"

    try:
        with database_operation_gate():
            source = sqlite3.connect(str(db_path), timeout=30)
            destination = sqlite3.connect(str(temp_path), timeout=30)
            try:
                source.backup(destination, pages=256)
            finally:
                destination.close()
                source.close()

        inspection = _inspect_sqlite_file(temp_path)
        if inspection["integrity_status"] != "verified":
            raise BackupServiceError(
                inspection["integrity_detail"] or "The backup failed integrity verification."
            )
        checksum = compute_file_hash(str(temp_path))
        size_bytes = int(temp_path.stat().st_size)
        os.replace(temp_path, final_path)
        os.chmod(final_path, 0o600)
    except Exception as exc:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        error = _sanitize_error(exc)
        with get_db_context() as db:
            db.add(
                DataBackup(
                    backup_id=backup_id,
                    filename=filename,
                    backup_type=backup_type,
                    status="failed",
                    integrity_status="failed",
                    size_bytes=0,
                    checksum=None,
                    db_engine="sqlite",
                    table_count=0,
                    page_count=0,
                    created_at=now,
                    completed_at=_utcnow(),
                    actor_admin_id=int(actor_admin_id),
                    error_message=error,
                    metadata_json={},
                )
            )
        log_event(
            "admin.backup_failed",
            f"Backup {backup_id} failed: {error}",
            int(actor_admin_id),
            level="error",
        )
        raise BackupServiceError(error) from exc

    with get_db_context() as db:
        row = DataBackup(
            backup_id=backup_id,
            filename=filename,
            backup_type=backup_type,
            status="completed",
            integrity_status="verified",
            size_bytes=size_bytes,
            checksum=checksum,
            db_engine="sqlite",
            table_count=int(inspection["table_count"]),
            page_count=int(inspection["page_count"]),
            created_at=now,
            completed_at=_utcnow(),
            actor_admin_id=int(actor_admin_id),
            error_message="",
            metadata_json={"foreign_key_violations": int(inspection["foreign_key_violations"])},
        )
        db.add(row)
        db.flush()
        result = _backup_dict(row)
    log_event(
        "admin.backup_create",
        f"Backup {backup_id} created ({backup_type}, {size_bytes} bytes)",
        int(actor_admin_id),
    )
    return result


def create_manual_backup(actor_admin_id: int) -> dict[str, Any]:
    return _create_backup(actor_admin_id, backup_type="manual")


def list_backups(
    actor_admin_id: int,
    *,
    search: str = "",
    backup_type: str = "all",
    status: str = "all",
) -> list[dict[str, Any]]:
    with get_readonly_db() as db:
        _require_admin_actor(db, actor_admin_id)
        query = db.query(DataBackup)
        if search:
            term = f"%{search.strip()}%"
            query = query.filter(
                (DataBackup.backup_id.ilike(term)) | (DataBackup.filename.ilike(term))
            )
        if backup_type != "all":
            query = query.filter(DataBackup.backup_type == backup_type)
        if status != "all":
            query = query.filter(DataBackup.status == status)
        rows = query.order_by(DataBackup.created_at.desc(), DataBackup.id.desc()).all()
        return [_backup_dict(row) for row in rows]


def get_backup(actor_admin_id: int, backup_id: str) -> dict[str, Any]:
    with get_readonly_db() as db:
        _require_admin_actor(db, actor_admin_id)
        row = db.query(DataBackup).filter(DataBackup.backup_id == backup_id).first()
        if row is None:
            raise BackupServiceError("Backup not found.")
        return _backup_dict(row)


def verify_backup(actor_admin_id: int, backup_id: str) -> dict[str, Any]:
    with get_db_context() as db:
        _require_admin_actor(db, actor_admin_id)
        row = db.query(DataBackup).filter(DataBackup.backup_id == backup_id).first()
        if row is None or str(row.status) == "deleted":
            raise BackupServiceError("Backup not found.")
        path = _resolve_backup_path(str(row.filename))
        inspection = _inspect_sqlite_file(path, expected_checksum=str(row.checksum or "") or None)
        status = str(inspection["integrity_status"])
        row.integrity_status = status
        row.table_count = int(inspection["table_count"])
        row.page_count = int(inspection["page_count"])
        if status == "verified":
            row.error_message = ""
        result = _backup_dict(row)
    log_event(
        "admin.backup_verify",
        f"Backup {backup_id} verification: {status}",
        int(actor_admin_id),
    )
    return result


def download_backup(actor_admin_id: int, backup_id: str) -> tuple[bytes, str, str]:
    with get_readonly_db() as db:
        _require_admin_actor(db, actor_admin_id)
        row = db.query(DataBackup).filter(DataBackup.backup_id == backup_id).first()
        if row is None or str(row.status) == "deleted":
            raise BackupServiceError("Backup not found.")
        filename = str(row.filename)
        checksum = str(row.checksum or "")
    path = _resolve_backup_path(filename)
    inspection = _inspect_sqlite_file(path, expected_checksum=checksum or None)
    if inspection["integrity_status"] != "verified":
        raise BackupServiceError("Only verified backups can be downloaded.")
    try:
        return path.read_bytes(), filename, BACKUP_MIME
    except OSError as exc:
        raise BackupServiceError("The backup could not be read.") from exc


def delete_backup(actor_admin_id: int, backup_id: str) -> None:
    with get_db_context() as db:
        _require_admin_actor(db, actor_admin_id)
        row = db.query(DataBackup).filter(DataBackup.backup_id == backup_id).first()
        if row is None or str(row.status) == "deleted":
            raise BackupServiceError("Backup not found.")
        valid_count = int(
            db.query(func.count(DataBackup.id))
            .filter(
                DataBackup.id != row.id,
                DataBackup.status.in_(["completed", "restored"]),
                DataBackup.integrity_status == "verified",
            )
            .scalar()
            or 0
        )
        is_valid = str(row.status) in {"completed", "restored"} and str(row.integrity_status) == "verified"
        if is_valid and valid_count == 0:
            raise BackupServiceError("At least one valid backup must remain available.")
        path = _resolve_backup_path(str(row.filename))
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            raise BackupServiceError("The backup file could not be removed.") from exc
        row.status = "deleted"
        row.integrity_status = "deleted"
        row.error_message = ""
    log_event(
        "admin.backup_delete",
        f"Backup {backup_id} deleted",
        int(actor_admin_id),
        level="warning",
    )


def get_backup_status(actor_admin_id: int) -> dict[str, Any]:
    with get_readonly_db() as db:
        _require_admin_actor(db, actor_admin_id)
        rows = db.query(DataBackup).order_by(DataBackup.created_at.desc()).all()
    active = [row for row in rows if str(row.status) != "deleted"]
    successful = [
        row for row in active if str(row.status) in {"completed", "restored"} and str(row.integrity_status) == "verified"
    ]
    failed = [row for row in active if str(row.status) == "failed"]
    verified = [row for row in active if str(row.integrity_status) == "verified"]
    last_error = next((str(row.error_message) for row in failed if row.error_message), "")
    integrity = (
        "All verified"
        if active and len(verified) == len(active)
        else "Issues detected"
        if any(str(row.integrity_status) in {"failed", "missing", "unknown"} for row in active)
        else "N/A"
    )
    return {
        "latest_backup": _format_datetime(active[0].created_at) if active else "N/A",
        "latest_backup_id": str(active[0].backup_id) if active else "N/A",
        "last_successful_backup": _format_datetime(successful[0].created_at) if successful else "N/A",
        "last_successful_backup_id": str(successful[0].backup_id) if successful else "N/A",
        "success_count": len(successful),
        "failure_count": len(failed),
        "total_backups": len(active),
        "total_storage_bytes": sum(int(row.size_bytes or 0) for row in active),
        "integrity_status": integrity,
        "verified_count": len(verified),
        "last_backup_error": last_error or "N/A",
    }


def _directory_size(path: Path) -> int | None:
    if not path.exists():
        return None
    if path.is_file():
        try:
            return int(path.stat().st_size)
        except OSError:
            return None
    total = 0
    try:
        for root, _, files in os.walk(path):
            for name in files:
                try:
                    total += int((Path(root) / name).stat().st_size)
                except OSError:
                    continue
    except OSError:
        return None
    return total


def _recording_storage_overview(actor_admin_id: int) -> dict[str, Any]:
    try:
        recordings = list_admin_recordings(actor_admin_id=actor_admin_id, limit=10000)
        diagnostics = run_storage_integrity_diagnostics(actor_admin_id=actor_admin_id)
    except Exception as exc:
        return {
            "total_recordings": "N/A",
            "audio_bytes": "N/A",
            "video_bytes": "N/A",
            "total_bytes": "N/A",
            "individual_files": [],
            "missing_count": "N/A",
            "corrupted_count": "N/A",
            "integrity_status": "N/A",
            "error": _sanitize_error(exc),
        }
    active_recordings = [row for row in recordings if str(row.get("deletion_status")) == "active"]
    media_items = [
        item
        for row in active_recordings
        for item in row.get("media", [])
        if str(item.get("deletion_status")) == "active"
    ]
    audio = sum(int(item.get("size_bytes") or 0) for item in media_items if item.get("media_type") == "audio")
    video = sum(int(item.get("size_bytes") or 0) for item in media_items if item.get("media_type") == "video")
    missing = len(diagnostics.get("missing_files", []))
    corrupted = len(diagnostics.get("checksum_mismatches", [])) + len(
        diagnostics.get("size_mismatches", [])
    )
    files = [
        {
            "Filename": str(item.get("filename") or "N/A"),
            "Type": str(item.get("media_type") or "N/A"),
            "Size (MB)": round(int(item.get("size_bytes") or 0) / 1024 / 1024, 2),
            "Duration (s)": round(float(item.get("duration_seconds") or 0), 1),
            "Available": "Yes" if item.get("available") else "No",
        }
        for item in media_items[:200]
    ]
    if not media_items:
        integrity = "N/A"
    elif missing or corrupted:
        integrity = "Issues detected"
    else:
        integrity = "Verified"
    return {
        "total_recordings": len(active_recordings),
        "audio_bytes": audio,
        "video_bytes": video,
        "total_bytes": audio + video,
        "individual_files": files,
        "missing_count": missing,
        "corrupted_count": corrupted,
        "integrity_status": integrity,
        "error": "",
    }


def get_storage_overview(actor_admin_id: int) -> dict[str, Any]:
    with get_readonly_db() as db:
        _require_admin_actor(db, actor_admin_id)

    db_path = Path(settings.database_path).expanduser().resolve()
    database_bytes = 0
    for candidate in (
        db_path,
        Path(str(db_path) + "-wal"),
        Path(str(db_path) + "-shm"),
    ):
        size = _directory_size(candidate)
        if size is not None:
            database_bytes += size

    backup_bytes = int(get_backup_status(actor_admin_id)["total_storage_bytes"])
    recording = _recording_storage_overview(actor_admin_id)
    categories = {
        "Database storage": database_bytes,
        "Backup storage": backup_bytes,
        "Recording storage": recording["total_bytes"],
        "User uploaded files": _directory_size(runtime_path("data/uploads")),
        "Knowledge vector store": _directory_size(Path(settings.vector_store_path)),
        "Generated reports": _directory_size(runtime_path("data/reports")),
        "Temporary files": _directory_size(Path(settings.temp_dir)),
        "Other tracked storage": _directory_size(runtime_path("data/secure")),
        "Learning resources storage": None,
    }
    total = sum(int(value) for value in categories.values() if value is not None)
    return {
        "categories": categories,
        "total_bytes": total,
        "database_file_exists": db_path.is_file(),
        "backup_directory_configured": bool(settings.backup_dir),
        "recording": recording,
    }


def get_recording_storage_overview(actor_admin_id: int) -> dict[str, Any]:
    with get_readonly_db() as db:
        _require_admin_actor(db, actor_admin_id)
    return _recording_storage_overview(actor_admin_id)


def get_retention_settings(actor_admin_id: int) -> list[dict[str, Any]]:
    with get_readonly_db() as db:
        _require_admin_actor(db, actor_admin_id)
        stored = {str(row.key): dict(row.value_json or {}) for row in db.query(DataRetentionSetting).all()}
    result: list[dict[str, Any]] = []
    for key, definition in RETENTION_DEFINITIONS.items():
        value = stored.get(key, {}).get("value", definition["value"])
        try:
            validate_retention_settings({key: value})
        except ValueError:
            value = definition["value"]
        result.append(
            {
                "key": key,
                "label": str(definition["label"]),
                "value": value,
                "unit": str(definition["unit"]),
                "options": list(definition.get("options", [])),
                "min": definition.get("min"),
                "max": definition.get("max"),
                "destructive": bool(definition["destructive"]),
                "source": "configured" if key in stored else "default",
                "description": str(stored.get(key, {}).get("description") or ""),
            }
        )
    return result


def validate_retention_settings(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        raise BackupServiceError("No retention settings were supplied.")
    validated: dict[str, Any] = {}
    for key, value in payload.items():
        definition = RETENTION_DEFINITIONS.get(str(key))
        if definition is None:
            raise BackupServiceError(f"Unknown retention setting: {key}")
        if "options" in definition:
            if value not in definition["options"]:
                raise BackupServiceError(f"Select a valid value for {definition['label']}.")
            validated[str(key)] = value
            continue
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise BackupServiceError(f"{definition['label']} must be a whole number.") from exc
        minimum = int(definition["min"])
        maximum = int(definition["max"])
        if not minimum <= number <= maximum:
            raise BackupServiceError(
                f"{definition['label']} must be between {minimum} and {maximum}."
            )
        validated[str(key)] = number
    return validated


def update_retention_settings(actor_admin_id: int, payload: dict[str, Any]) -> list[dict[str, Any]]:
    values = validate_retention_settings(payload)
    with get_db_context() as db:
        _require_admin_actor(db, actor_admin_id)
        for key, value in values.items():
            definition = RETENTION_DEFINITIONS[key]
            row = db.query(DataRetentionSetting).filter(DataRetentionSetting.key == key).first()
            if row is None:
                row = DataRetentionSetting(key=key)
                db.add(row)
            row.value_json = {
                "value": value,
                "description": f"{definition['label']} set by an administrator.",
            }
            row.description = str(definition["label"])
            row.updated_by_admin_id = int(actor_admin_id)
            row.updated_at = _utcnow()
    keys = ", ".join(sorted(values))
    log_event(
        "admin.retention_update",
        f"Retention settings updated: {keys}",
        int(actor_admin_id),
        level="warning",
    )
    return get_retention_settings(actor_admin_id)


def get_retention_preview(actor_admin_id: int) -> dict[str, Any]:
    settings_current = {row["key"]: row["value"] for row in get_retention_settings(actor_admin_id)}
    backups = list_backups(actor_admin_id)
    temp_candidates = []
    try:
        diagnostics = run_storage_integrity_diagnostics(actor_admin_id=actor_admin_id)
        temp_candidates = diagnostics.get("temp_candidates", [])
    except Exception:
        pass
    with get_readonly_db() as db:
        _require_admin_actor(db, actor_admin_id)
        recording_count = int(
            db.query(func.count(LiveInterviewRecording.id))
            .filter(LiveInterviewRecording.deletion_status == "active")
            .scalar()
            or 0
        )
        media_count = int(
            db.query(func.count(LiveInterviewMedia.id))
            .filter(LiveInterviewMedia.deletion_status == "active")
            .scalar()
            or 0
        )
    return {
        "backups_beyond_count": max(
            0,
            len([row for row in backups if row["status"] != "deleted"])
            - int(settings_current["backup_max_count"]),
        ),
        "active_recordings": recording_count,
        "active_media_files": media_count,
        "temporary_file_candidates": len(temp_candidates),
        "automatic_deletion_enabled": False,
    }


def inspect_backup(actor_admin_id: int, backup_id: str) -> dict[str, Any]:
    details = get_backup(actor_admin_id, backup_id)
    if details["status"] == "deleted":
        raise BackupServiceError("Deleted backups cannot be restored.")
    path = _resolve_backup_path(details["filename"])
    inspection = _inspect_sqlite_file(path, expected_checksum=details["checksum"] or None)
    expected_tables = set(Base.metadata.tables.keys())
    backup_tables = inspection["tables"]
    missing_core = sorted(_CORE_TABLES - backup_tables)
    unknown_tables = sorted(backup_tables - expected_tables)
    optional_missing = sorted(expected_tables - backup_tables)
    reasons: list[str] = []
    if inspection["integrity_status"] != "verified":
        reasons.append(inspection["integrity_detail"] or "Backup integrity verification failed.")
    if missing_core:
        reasons.append("Required tables are missing: " + ", ".join(missing_core))
    if unknown_tables:
        reasons.append("Backup contains tables unknown to this application version.")
    if details["db_engine"] != "sqlite":
        reasons.append("Only SQLite backups are supported.")
    return {
        **details,
        "integrity_detail": inspection["integrity_detail"],
        "table_count_actual": int(inspection["table_count"]),
        "page_count_actual": int(inspection["page_count"]),
        "foreign_key_violations": int(inspection["foreign_key_violations"]),
        "missing_core_tables": missing_core,
        "unknown_tables": unknown_tables,
        "migratable_tables": optional_missing,
        "compatible": not reasons,
        "can_restore": not reasons,
        "reasons": reasons,
    }


def list_restorable_backups(actor_admin_id: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in list_backups(actor_admin_id):
        if row["status"] == "deleted":
            continue
        try:
            inspected = inspect_backup(actor_admin_id, row["backup_id"])
        except BackupServiceError:
            continue
        result.append(
            {
                "backup_id": row["backup_id"],
                "filename": row["filename"],
                "created_at": row["created_at"],
                "backup_type": row["backup_type"],
                "integrity_status": row["integrity_status"],
                "compatible": inspected["compatible"],
                "can_restore": inspected["can_restore"],
            }
        )
    return result


def _checkpoint_database(db_path: Path) -> None:
    connection = sqlite3.connect(str(db_path), timeout=30)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def _replace_database_file(source: Path, db_path: Path) -> None:
    temp_path = db_path.parent / f".restore_{uuid.uuid4().hex}.db.part"
    try:
        shutil.copyfile(source, temp_path)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, db_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _verify_current_database() -> bool:
    db_path = Path(settings.database_path).expanduser().resolve()
    inspection = _inspect_sqlite_file(db_path)
    return inspection["integrity_status"] == "verified"


def restore_backup(
    actor_admin_id: int,
    backup_id: str,
    *,
    confirm_restore: bool,
    confirm_production: bool,
    restore_phrase: str,
    production_phrase: str,
) -> dict[str, Any]:
    with get_db_context() as db:
        _require_admin_actor(db, actor_admin_id)
    if not confirm_restore or restore_phrase != RESTORE_CONFIRMATION:
        raise BackupServiceError("Restore confirmation is required.")
    if not confirm_production or production_phrase != PRODUCTION_CONFIRMATION:
        raise BackupServiceError("Production restore confirmation is required.")

    with database_operation_gate():
        inspection = inspect_backup(actor_admin_id, backup_id)
        if not inspection["can_restore"]:
            raise BackupServiceError(
                "Backup cannot be restored: " + "; ".join(inspection["reasons"])
            )
        safety = _create_backup(actor_admin_id, backup_type="safety")
        backup_path = _resolve_backup_path(inspection["filename"])
        safety_path = _resolve_backup_path(safety["filename"])
        db_path = Path(settings.database_path).expanduser().resolve()
        restored = False
        try:
            if db_module.engine is not None:
                db_module.engine.dispose()
            db_module.SessionLocal = None
            _checkpoint_database(db_path)
            pre_swap = _inspect_sqlite_file(backup_path, expected_checksum=inspection["checksum"] or None)
            if pre_swap["integrity_status"] != "verified":
                raise BackupServiceError("Backup changed after inspection and cannot be restored.")
            _replace_database_file(backup_path, db_path)
            restored = True
            db_module.init_database()
            if not _verify_current_database():
                raise BackupServiceError("Restored database failed integrity verification.")
        except Exception as exc:
            error = _sanitize_error(exc)
            try:
                if db_module.engine is not None:
                    db_module.engine.dispose()
                db_module.SessionLocal = None
                if restored:
                    _replace_database_file(safety_path, db_path)
                db_module.init_database()
            except Exception as rollback_exc:
                log_event(
                    "admin.restore_rollback_failed",
                    f"Restore rollback failed: {_sanitize_error(rollback_exc)}",
                    int(actor_admin_id),
                    level="error",
                )
            log_event(
                "admin.restore_failed",
                f"Restore of {backup_id} failed: {error}",
                int(actor_admin_id),
                level="error",
            )
            raise BackupServiceError(error) from exc

        with get_db_context() as db:
            row = db.query(DataBackup).filter(DataBackup.backup_id == backup_id).first()
            if row is None:
                row = DataBackup(
                    backup_id=backup_id,
                    filename=inspection["filename"],
                    backup_type=inspection["backup_type"],
                    integrity_status="verified",
                    db_engine="sqlite",
                    actor_admin_id=int(actor_admin_id),
                    created_at=_parse_datetime(inspection["created_at"]),
                )
                db.add(row)
            row.status = "restored"
            row.integrity_status = "verified"
            row.size_bytes = int(inspection["size_bytes"])
            row.checksum = str(inspection["checksum"] or "")
            row.table_count = int(inspection["table_count_actual"])
            row.page_count = int(inspection["page_count_actual"])
            row.error_message = ""
            metadata = dict(row.metadata_json or {})
            metadata["restored_with_safety_backup"] = safety["backup_id"]
            metadata["restored_at"] = _utcnow().isoformat()
            row.metadata_json = metadata
        log_event(
            "admin.restore_database",
            f"Database restored from {backup_id} with safety backup {safety['backup_id']}",
            int(actor_admin_id),
            level="warning",
        )
        return {
            "status": "completed",
            "restored_backup_id": backup_id,
            "safety_backup_id": safety["backup_id"],
            "integrity_status": "verified",
            "completed_at": _utcnow().isoformat(),
        }


def get_backup_audit_log(actor_admin_id: int, limit: int = 100) -> list[dict[str, Any]]:
    from database import EventLog

    with get_readonly_db() as db:
        _require_admin_actor(db, actor_admin_id)
        rows = (
            db.query(EventLog)
            .filter(
                EventLog.event_type.in_(
                    [
                        "admin.backup_create",
                        "admin.backup_failed",
                        "admin.backup_verify",
                        "admin.backup_delete",
                        "admin.restore_database",
                        "admin.restore_failed",
                        "admin.restore_rollback_failed",
                        "admin.retention_update",
                    ]
                )
            )
            .order_by(EventLog.created_at.desc(), EventLog.id.desc())
            .limit(max(1, min(int(limit), 200)))
            .all()
        )
        return [
            {
                "When": _format_datetime(row.created_at),
                "Event": str(row.event_type),
                "Message": str(row.message or ""),
                "Level": str(row.level or "info"),
            }
            for row in rows
        ]
