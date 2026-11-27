from __future__ import annotations

import importlib

import streamlit as st
from config.settings import settings
from services import backup_service
from services.auth_service import AuthError, require_admin

from frontend.shared.components import (
    data_table,
    empty_state,
    error_state,
    kpi_row,
    page_header,
    success_toast,
)

if not hasattr(backup_service, "restore_backup"):
    importlib.reload(backup_service)

from services.backup_service import (
    PRODUCTION_CONFIRMATION,
    RESTORE_CONFIRMATION,
    BackupServiceError,
    create_manual_backup,
    delete_backup,
    download_backup,
    get_backup_audit_log,
    get_backup_status,
    get_recording_storage_overview,
    get_retention_preview,
    get_retention_settings,
    get_storage_overview,
    inspect_backup,
    list_backups,
    list_restorable_backups,
    restore_backup,
    update_retention_settings,
    validate_retention_settings,
    verify_backup,
)

DELETE_PENDING = "_admin_data_delete_pending"
DELETE_ID = "_admin_data_delete_id"
DELETE_CONFIRMED = "_admin_data_delete_confirmed"
DOWNLOAD_ID = "_admin_data_download_id"
RESTORE_PENDING = "_admin_data_restore_pending"
RESTORE_ID = "_admin_data_restore_id"
RESTORE_CONFIRMED = "_admin_data_restore_confirmed"
RESTORE_RESULT = "_admin_data_restore_result"


def _mb(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "N/A"
    return f"{float(value) / 1024 / 1024:.1f} MB"


def _count(value: object) -> str:
    return "N/A" if not isinstance(value, (int, float)) else str(value)


@st.dialog("Delete backup?", width="small")
def _delete_dialog(filename: str) -> None:
    st.warning(
        f"This permanently removes **{filename}** from the backup directory and catalog. "
        "The active database is not changed."
    )
    acknowledged = st.checkbox(
        "I understand this backup cannot be recovered after deletion",
        key="_admin_data_delete_ack",
    )
    keep_col, delete_col = st.columns(2)
    if keep_col.button("Cancel", width="stretch", key="_admin_data_delete_cancel"):
        st.session_state[DELETE_PENDING] = False
        st.session_state[DELETE_ID] = None
        st.session_state[DELETE_CONFIRMED] = False
        st.rerun()
    if delete_col.button(
        "Delete backup",
        type="primary",
        disabled=not acknowledged,
        width="stretch",
        key="_admin_data_delete_confirm",
    ):
        st.session_state[DELETE_CONFIRMED] = True


@st.dialog("Restore database?", width="small")
def _restore_dialog(backup_id: str, filename: str) -> None:
    st.error(
        "Restoring replaces the current database with the selected backup. "
        "Users, interviews, reports, and settings created after the backup will be lost."
    )
    st.caption(f"Selected backup: {backup_id} ({filename})")
    st.caption("A safety backup of the current database is created before the swap.")
    confirm_restore = st.checkbox(
        "I understand the current database will be replaced",
        key="_admin_data_restore_ack",
    )
    confirm_production = st.checkbox(
        "I understand this will affect production data",
        key="_admin_data_production_ack",
    )
    restore_phrase = st.text_input(
        f"Type {RESTORE_CONFIRMATION} to confirm restore",
        key="_admin_data_restore_phrase",
        placeholder=RESTORE_CONFIRMATION,
    )
    production_phrase = st.text_input(
        f"Type {PRODUCTION_CONFIRMATION} to confirm production impact",
        key="_admin_data_production_phrase",
        placeholder=PRODUCTION_CONFIRMATION,
    )
    ready = (
        confirm_restore
        and confirm_production
        and restore_phrase == RESTORE_CONFIRMATION
        and production_phrase == PRODUCTION_CONFIRMATION
    )
    keep_col, restore_col = st.columns(2)
    if keep_col.button("Cancel", width="stretch", key="_admin_data_restore_cancel"):
        st.session_state[RESTORE_PENDING] = False
        st.session_state[RESTORE_ID] = None
        st.session_state[RESTORE_CONFIRMED] = False
        st.rerun()
    if restore_col.button(
        "Restore database",
        type="primary",
        disabled=not ready,
        width="stretch",
        key="_admin_data_restore_confirm",
    ):
        st.session_state[RESTORE_CONFIRMED] = True


def _render_overview(admin_id: int) -> None:
    st.caption(
        "Backup status is read from the backup catalog and files. No automatic deletion is enabled."
    )
    if st.button(
        "Create backup now",
        icon=":material/backup:",
        type="primary",
        width="stretch",
        key="_admin_data_create_now",
    ):
        try:
            result = create_manual_backup(admin_id)
            success_toast(f"Backup {result['backup_id']} created and verified.")
            st.rerun()
        except BackupServiceError as exc:
            error_state("Backup creation failed.", str(exc))
    status = get_backup_status(admin_id)
    kpi_row(
        [
            ("Latest backup", status["latest_backup"], None, ":material/schedule:"),
            ("Last successful", status["last_successful_backup"], None, ":material/check_circle:"),
            ("Successful backups", _count(status["success_count"]), None, ":material/database:"),
            ("Failed backups", _count(status["failure_count"]), None, ":material/error:"),
            ("Integrity", status["integrity_status"], None, ":material/verified:"),
            ("Stored backups", _mb(status["total_storage_bytes"]), None, ":material/storage:"),
        ]
    )
    if status["last_backup_error"] != "N/A":
        st.caption(f"Last backup error: {status['last_backup_error']}")
    st.subheader("Recent backup and restore activity", icon=":material/history:")
    data_table(
        get_backup_audit_log(admin_id, limit=40),
        empty_message="No backup or restore activity has been recorded.",
        alt="Recent backup, restore, and retention audit events",
    )


def _render_backup(admin_id: int) -> None:
    st.caption(
        "Backups use SQLite's online backup API with WAL-safe copying, checksum verification, "
        "and atomic file replacement."
    )
    with st.container(border=True):
        st.markdown("**Backup destination**")
        st.code(str(settings.backup_dir), language="text")
        st.caption("Set BACKUP_DIR to change this location before starting the app.")
        if st.button(
            "Create verified backup",
            icon=":material/backup:",
            type="primary",
            width="stretch",
            key="_admin_data_create_verified",
        ):
            try:
                result = create_manual_backup(admin_id)
                st.success(
                    f"Backup {result['backup_id']} completed: {result['table_count']} tables, "
                    f"{result['page_count']} pages, checksum verified.",
                    icon=":material/check:",
                )
            except BackupServiceError as exc:
                error_state("Backup creation failed.", str(exc))
    status = get_backup_status(admin_id)
    with st.container(border=True):
        st.markdown("**Latest verified backup**")
        st.table(
            {
                "Backup ID": status["latest_backup_id"],
                "Created": status["latest_backup"],
                "Integrity": status["integrity_status"],
                "Verified backups": _count(status["verified_count"]),
                "Stored size": _mb(status["total_storage_bytes"]),
            },
            border="horizontal",
            width="content",
        )


def _render_history(admin_id: int) -> None:
    with st.container(horizontal=True, wrap=True):
        search = st.text_input(
            "Search backups",
            type="search",
            placeholder="Backup ID or filename",
            key="_admin_data_history_search",
        )
        backup_type = st.selectbox(
            "Backup type",
            ["all", "manual", "automatic", "safety"],
            key="_admin_data_history_type",
        )
        status_filter = st.selectbox(
            "Status",
            ["all", "completed", "restored", "failed", "deleted"],
            key="_admin_data_history_status",
        )
    rows = list_backups(
        admin_id,
        search=search,
        backup_type=backup_type,
        status=status_filter,
    )
    if not rows:
        empty_state("No backups match the current filters.")
        return
    table_rows = [
        {
            "Backup ID": row["backup_id"],
            "Created": row["created_at"],
            "Type": row["backup_type"],
            "Status": row["status"],
            "Integrity": row["integrity_status"],
            "Size": _mb(row["size_bytes"]),
            "Tables": _count(row["table_count"]),
            "Pages": _count(row["page_count"]),
            "Checksum": row["checksum"][:12] if row["checksum"] else "N/A",
        }
        for row in rows
    ]
    data_table(table_rows, alt="Database backup history with integrity and size details")
    st.subheader("Backup actions", icon=":material/build:")
    selected_id = st.selectbox(
        "Select a backup",
        [row["backup_id"] for row in rows],
        key="_admin_data_history_selected",
    )
    with st.container(horizontal=True, wrap=True):
        if st.button(
            "Verify",
            icon=":material/verified:",
            width="stretch",
            key="_admin_data_verify",
        ):
            try:
                result = verify_backup(admin_id, selected_id)
                success_toast(f"Backup {selected_id} integrity: {result['integrity_status']}.")
            except BackupServiceError as exc:
                error_state("Verification failed.", str(exc))
        if st.button(
            "Prepare download",
            icon=":material/download:",
            width="stretch",
            key="_admin_data_prepare_download",
        ):
            st.session_state[DOWNLOAD_ID] = selected_id
        if st.button(
            "Delete",
            icon=":material/delete:",
            width="stretch",
            key="_admin_data_delete_request",
        ):
            st.session_state[DELETE_PENDING] = True
            st.session_state[DELETE_ID] = selected_id
            st.session_state[DELETE_CONFIRMED] = False
    if st.session_state.get(DOWNLOAD_ID):
        download_id = str(st.session_state[DOWNLOAD_ID])
        try:
            payload, filename, mime = download_backup(admin_id, download_id)
            st.download_button(
                "Download verified backup",
                data=payload,
                file_name=filename,
                mime=mime,
                icon=":material/download:",
                width="stretch",
                key="_admin_data_download_button",
            )
        except BackupServiceError as exc:
            error_state("Download could not be prepared.", str(exc))
    if st.session_state.get(DELETE_PENDING):
        pending_id = st.session_state.get(DELETE_ID)
        pending = next((row for row in rows if row["backup_id"] == pending_id), None)
        if pending:
            _delete_dialog(pending["filename"])
            if st.session_state.get(DELETE_CONFIRMED):
                try:
                    delete_backup(admin_id, str(pending_id))
                    success_toast(f"Backup {pending_id} deleted.")
                except BackupServiceError as exc:
                    error_state("Backup deletion failed.", str(exc))
                st.session_state[DELETE_PENDING] = False
                st.session_state[DELETE_ID] = None
                st.session_state[DELETE_CONFIRMED] = False
                st.session_state[DOWNLOAD_ID] = None
                st.rerun()


def _render_storage(admin_id: int) -> None:
    overview = get_storage_overview(admin_id)
    rows = [
        {
            "Category": category,
            "Size": _mb(size),
            "Status": "Measured" if isinstance(size, (int, float)) else "N/A",
        }
        for category, size in overview["categories"].items()
    ]
    data_table(rows, alt="Storage usage by category")
    chart_rows = [
        {"Category": category, "Size (MB)": round(int(size) / 1024 / 1024, 2)}
        for category, size in overview["categories"].items()
        if isinstance(size, (int, float))
    ]
    if chart_rows:
        st.bar_chart(
            chart_rows,
            x="Category",
            y="Size (MB)",
            alt="Measured storage usage by category in megabytes",
        )
    recording = overview["recording"]
    if recording.get("error"):
        error_state("Recording storage overview is unavailable.", str(recording["error"]))
    else:
        kpi_row(
            [
                ("Recordings", _count(recording["total_recordings"]), None, ":material/videocam:"),
                ("Audio", _mb(recording["audio_bytes"]), None, ":material/audiotrack:"),
                ("Video", _mb(recording["video_bytes"]), None, ":material/movie:"),
                ("Total media", _mb(recording["total_bytes"]), None, ":material/storage:"),
                (
                    "Missing files",
                    _count(recording["missing_count"]),
                    None,
                    ":material/broken_image:",
                ),
                (
                    "Corrupt files",
                    _count(recording["corrupted_count"]),
                    None,
                    ":material/report:",
                ),
            ]
        )
        st.caption(f"Recording integrity: {recording['integrity_status']}")
        data_table(
            recording["individual_files"],
            empty_message="No active recording files are tracked.",
            alt="Individual recording files with size and availability",
        )
    with st.container(border=True):
        st.markdown("**Data availability**")
        st.caption(
            "Learning resources are stored as metadata in the application database; "
            "a separate resource-file store is not tracked. Unavailable directories show N/A."
        )
        st.caption(f"Database file present: {'Yes' if overview['database_file_exists'] else 'No'}")
        st.caption(
            "Backup directory configured: "
            f"{'Yes' if overview['backup_directory_configured'] else 'No'}"
        )
    with st.expander("Detailed recording storage", expanded=False):
        details = get_recording_storage_overview(admin_id)
        data_table(
            details["individual_files"],
            empty_message="No detailed recording files are available.",
            alt="Detailed list of live interview recording files",
        )


def _render_retention(admin_id: int) -> None:
    st.info(
        "Retention values are validated and stored for policy reporting. "
        "Automatic deletion is disabled and requires an explicit future implementation."
    )
    current = {row["key"]: row["value"] for row in get_retention_settings(admin_id)}
    payload: dict[str, object] = {}
    with st.form("admin_data_retention_form", border=True):
        for row in get_retention_settings(admin_id):
            label = f"{row['label']} ({row['unit']})"
            if row["options"]:
                options = list(row["options"])
                default = str(row["value"]) if str(row["value"]) in options else options[0]
                payload[row["key"]] = st.selectbox(
                    label,
                    options,
                    index=options.index(default),
                    key=f"_admin_data_retention_{row['key']}",
                )
            else:
                payload[row["key"]] = st.number_input(
                    label,
                    min_value=int(row["min"] or 1),
                    max_value=int(row["max"] or 100000),
                    value=int(row["value"]),
                    key=f"_admin_data_retention_{row['key']}",
                )
            st.caption(f"Source: {row['source']}. Destructive operation: {'Yes' if row['destructive'] else 'No'}")
        submitted = st.form_submit_button(
            "Save retention settings",
            type="primary",
            icon=":material/save:",
            width="stretch",
        )
    if submitted:
        try:
            validate_retention_settings(payload)
            update_retention_settings(admin_id, payload)
            success_toast("Retention settings saved and validated.")
            st.rerun()
        except BackupServiceError as exc:
            error_state("Retention settings were not saved.", str(exc))
    preview = get_retention_preview(admin_id)
    st.subheader("Retention preview", icon=":material/visibility:")
    st.table(
        {
            "Backups above maximum": _count(preview["backups_beyond_count"]),
            "Active recordings": _count(preview["active_recordings"]),
            "Active media files": _count(preview["active_media_files"]),
            "Temporary file candidates": _count(preview["temporary_file_candidates"]),
            "Automatic deletion": "Enabled" if preview["automatic_deletion_enabled"] else "Disabled",
        },
        border="horizontal",
        width="content",
    )
    st.caption(f"Current values loaded: {len(current)}")


def _render_restore(admin_id: int) -> None:
    st.error(
        "Restore is a destructive operation. Select a verified, compatible backup and "
        "complete both confirmations. A safety backup is created before any swap."
    )
    restorable = list_restorable_backups(admin_id)
    if not restorable:
        empty_state(
            "No restorable backups are available.",
            "Create and verify a backup before attempting a restore.",
        )
        return
    options = [
        f"{row['backup_id']} | {row['created_at']} | {row['integrity_status']}"
        for row in restorable
    ]
    choice = st.selectbox("Available backups", options, key="_admin_data_restore_choice")
    selected_id = choice.split(" | ", 1)[0]
    details = inspect_backup(admin_id, selected_id)
    with st.container(border=True):
        st.markdown(f"**{details['backup_id']}**")
        st.caption(
            f"Created: {details['created_at']} • Type: {details['backup_type']} • "
            f"Integrity: {details['integrity_status']} • Size: {_mb(details['size_bytes'])}"
        )
        st.caption(
            f"Tables: {_count(details['table_count_actual'])} • "
            f"Pages: {_count(details['page_count_actual'])} • "
            f"Foreign-key violations: {_count(details['foreign_key_violations'])}"
        )
        if details["compatible"]:
            st.success("Backup is compatible with this application version.", icon=":material/check:")
        else:
            error_state("Backup is not compatible.", "; ".join(details["reasons"]))
        if details["migratable_tables"]:
            st.caption(
                "Tables absent from this backup will be created by schema initialization: "
                + ", ".join(details["migratable_tables"])
            )
    if st.button(
        "Restore selected backup",
        icon=":material/restart_alt:",
        type="primary",
        disabled=not details["can_restore"],
        width="stretch",
        key="_admin_data_restore_request",
    ):
        st.session_state[RESTORE_PENDING] = True
        st.session_state[RESTORE_ID] = selected_id
        st.session_state[RESTORE_CONFIRMED] = False
    if st.session_state.get(RESTORE_PENDING):
        pending_id = st.session_state.get(RESTORE_ID)
        pending = next((row for row in restorable if row["backup_id"] == pending_id), None)
        if pending:
            _restore_dialog(str(pending_id), pending["filename"])
            if st.session_state.get(RESTORE_CONFIRMED):
                try:
                    result = restore_backup(
                        admin_id,
                        str(pending_id),
                        confirm_restore=True,
                        confirm_production=True,
                        restore_phrase=st.session_state.get(
                            "_admin_data_restore_phrase", ""
                        ),
                        production_phrase=st.session_state.get(
                            "_admin_data_production_phrase", ""
                        ),
                    )
                    st.session_state[RESTORE_RESULT] = result
                    success_toast(
                        f"Database restored from {result['restored_backup_id']}. "
                        f"Safety backup: {result['safety_backup_id']}."
                    )
                except BackupServiceError as exc:
                    error_state("Restore failed.", str(exc))
                st.session_state[RESTORE_PENDING] = False
                st.session_state[RESTORE_ID] = None
                st.session_state[RESTORE_CONFIRMED] = False
                st.rerun()
    result = st.session_state.get(RESTORE_RESULT)
    if result:
        st.caption(
            f"Last restore completed at {result['completed_at']} using safety backup "
            f"{result['safety_backup_id']}."
        )


def show_admin_data_management() -> None:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        error_state("Administrator access required.")
        st.stop()
    admin_id = int(account.id)
    try:
        require_admin(admin_id)
    except AuthError as exc:
        error_state("Administrator access required.", str(exc))
        st.stop()
    page_header(
        "Backup and data management",
        "Create verified SQLite backups, review storage, manage retention policy, and restore safely.",
        icon=":material/database:",
    )
    st.session_state.setdefault(DELETE_PENDING, False)
    st.session_state.setdefault(DELETE_ID, None)
    st.session_state.setdefault(DELETE_CONFIRMED, False)
    st.session_state.setdefault(DOWNLOAD_ID, None)
    st.session_state.setdefault(RESTORE_PENDING, False)
    st.session_state.setdefault(RESTORE_ID, None)
    st.session_state.setdefault(RESTORE_CONFIRMED, False)
    st.session_state.setdefault(RESTORE_RESULT, None)

    overview_tab, backup_tab, history_tab, storage_tab, retention_tab, restore_tab = st.tabs(
        [
            "Overview",
            "Database backup",
            "Backup history",
            "Storage overview",
            "Retention settings",
            "Restore backup",
        ]
    )
    with overview_tab:
        _render_overview(admin_id)
    with backup_tab:
        _render_backup(admin_id)
    with history_tab:
        _render_history(admin_id)
    with storage_tab:
        _render_storage(admin_id)
    with retention_tab:
        _render_retention(admin_id)
    with restore_tab:
        _render_restore(admin_id)


show_admin_data_management()
