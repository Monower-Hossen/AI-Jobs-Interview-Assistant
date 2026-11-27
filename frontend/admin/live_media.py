from __future__ import annotations

import importlib
from datetime import datetime

import services.live_interview_media_service as live_media_service
import services.live_interview_workflow as live_workflow
import streamlit as st
from services.auth_service import AuthError, require_admin

if not hasattr(live_workflow, "delete_live_media") or not hasattr(
    live_workflow, "run_live_storage_integrity_diagnostics"
):
    importlib.reload(live_media_service)
    importlib.reload(live_workflow)

from services.live_interview_workflow import (
    LiveInterviewError,
    delete_live_media,
    delete_live_recording,
    get_live_recording_details,
    list_admin_live_recordings,
    read_live_media,
    retry_live_media_deletions,
    run_live_storage_integrity_diagnostics,
    verify_live_media,
)

from frontend.shared.components import (
    data_table,
    empty_state,
    error_state,
    page_header,
)

DELETE_PENDING = "_admin_live_media_delete_pending"
DELETE_RECORDING_ID = "_admin_live_media_delete_recording_id"
DELETE_CONFIRMED = "_admin_live_media_delete_confirmed"
MEDIA_DELETE_PENDING = "_admin_live_media_file_delete_pending"
MEDIA_DELETE_ID = "_admin_live_media_file_delete_id"
MEDIA_DELETE_CONFIRMED = "_admin_live_media_file_delete_confirmed"

RECORDING_STATUSES = (
    "all",
    "recording",
    "completed",
    "interrupted",
    "failed",
    "pending_delete",
    "deleted",
    "not_consented",
)


def _format_when(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value or "N/A")


def _render_transcript(report: dict) -> None:
    answers = report.get("answers") or []
    if not answers:
        st.caption("No transcript is available for this interview.")
        return
    with st.expander("Transcript and evaluations", expanded=False):
        for answer in answers:
            st.markdown(f"**Question {answer.get('question_number', '?')}**")
            st.caption(str(answer.get("question", "")))
            st.write(f"Transcript: {answer.get('answer', '')}")
            st.write(f"Score: {float(answer.get('overall_score') or 0):.1f}/10")


@st.dialog("Delete live recording?", width="small")
def _delete_dialog() -> None:
    st.warning(
        "This permanently removes the saved video and audio files for this interview. "
        "The transcript, answers, and final report remain in the user's history."
    )
    keep_col, delete_col = st.columns(2)
    if keep_col.button("Cancel", width="stretch", key="_admin_live_media_delete_cancel"):
        st.session_state[DELETE_PENDING] = False
        st.session_state[DELETE_CONFIRMED] = False
        st.session_state[DELETE_RECORDING_ID] = None
        st.rerun()
    if delete_col.button(
        "Delete recording",
        type="primary",
        width="stretch",
        key="_admin_live_media_delete_confirm",
    ):
        st.session_state[DELETE_CONFIRMED] = True
        st.rerun()


@st.dialog("Delete recording file?", width="small")
def _delete_media_dialog() -> None:
    st.warning("This permanently removes this individual saved media file.")
    keep_col, delete_col = st.columns(2)
    if keep_col.button("Cancel", width="stretch", key="_admin_live_media_file_cancel"):
        st.session_state[MEDIA_DELETE_PENDING] = False
        st.session_state[MEDIA_DELETE_CONFIRMED] = False
        st.session_state[MEDIA_DELETE_ID] = None
        st.rerun()
    if delete_col.button(
        "Delete file",
        type="primary",
        width="stretch",
        key="_admin_live_media_file_confirm",
    ):
        st.session_state[MEDIA_DELETE_CONFIRMED] = True
        st.rerun()


def _render_media_item(item: dict, *, admin_id: int) -> None:
    media_id = int(item["id"])
    label = "Camera video" if item["media_type"] == "video" else "Microphone audio"
    size_mb = float(item["size_bytes"] or 0) / 1024 / 1024
    with st.container(border=True):
        st.markdown(f"**{label}**")
        st.caption(
            f"Status: {item['media_status']} • {size_mb:.2f} MB • "
            f"{float(item['duration_seconds']):.1f}s • "
            f"Checksum: {str(item.get('checksum') or 'N/A')[:12]}"
        )
        try:
            verification = verify_live_media(media_id, admin_id=admin_id)
            if verification["exists"] and verification["checksum_valid"]:
                st.caption("Integrity verified: file present and checksum matches.")
            elif verification["exists"]:
                st.warning("Integrity check failed: the stored checksum does not match.")
            else:
                st.warning("This media file is missing from local storage.")
        except LiveInterviewError as exc:
            st.warning(str(exc))
        if item.get("available"):
            try:
                data, mime_type, filename = read_live_media(media_id, admin_id=admin_id)
                if item["media_type"] == "video":
                    st.video(data, format="video/mp4")
                else:
                    st.audio(data, format="audio/wav")
                st.download_button(
                    f"Download {label.lower()}",
                    data=data,
                    file_name=filename,
                    mime=mime_type,
                    key=f"_admin_live_media_download_{media_id}",
                    width="stretch",
                )
            except LiveInterviewError as exc:
                error_state("Could not open the recording.", str(exc))
        with st.expander("Media metadata", expanded=False):
            st.json(item.get("metadata") or {})
        if st.button(
            "Delete file",
            icon=":material/delete:",
            key=f"_admin_live_media_delete_file_{media_id}",
        ):
            st.session_state[MEDIA_DELETE_PENDING] = True
            st.session_state[MEDIA_DELETE_ID] = media_id
            st.rerun()


def _render_capture_manifest(manifest: dict | None) -> None:
    if not manifest:
        st.caption("Capture manifest: N/A")
        return
    sync = manifest.get("synchronization") or {}
    st.caption(
        "Capture manifest: "
        f"schema {manifest.get('schema_version', 'N/A')} • "
        f"sync {sync.get('status', 'N/A')} • "
        f"guaranteed sync {sync.get('guaranteed_sync', False)}"
    )


def _render_storage_health(admin_id: int) -> None:
    st.subheader("Storage health", divider=True)
    st.caption("Read-only diagnostics. This page never repairs, creates, or deletes stored files.")
    if not st.button(
        "Run storage diagnostics",
        icon=":material/health_and_safety:",
        key="_admin_live_media_run_diagnostics",
    ):
        return
    try:
        report = run_live_storage_integrity_diagnostics(admin_id=admin_id)
    except LiveInterviewError as exc:
        error_state("Storage diagnostics could not be completed.", str(exc))
        return
    if not report.get("storage_root_exists"):
        st.warning("Live media storage root is not present yet; file checks are unavailable.")
    metrics = {
        "Missing files": len(report.get("missing_files", [])),
        "Checksum mismatches": len(report.get("checksum_mismatches", [])),
        "Size mismatches": len(report.get("size_mismatches", [])),
        "Pending deletions": len(report.get("pending_deletions", [])),
        "Incomplete records": len(report.get("incomplete_records", [])),
        "Legacy without manifest": len(report.get("legacy_without_manifest", [])),
        "Candidate orphans": len(report.get("candidate_orphans", [])),
        "Temporary candidates": len(report.get("temp_candidates", [])),
        "Unverified": len(report.get("unverified", [])),
    }
    columns = st.columns(3)
    for index, (label, value) in enumerate(metrics.items()):
        columns[index % 3].metric(label, value)
    sections = (
        ("Missing files", "missing_files"),
        ("Checksum mismatches", "checksum_mismatches"),
        ("Size mismatches", "size_mismatches"),
        ("Pending deletions", "pending_deletions"),
        ("Incomplete records", "incomplete_records"),
        ("Legacy without manifest", "legacy_without_manifest"),
        ("Candidate orphans", "candidate_orphans"),
        ("Temporary candidates", "temp_candidates"),
        ("Unverified", "unverified"),
    )
    for title, key in sections:
        rows = report.get(key) or []
        with st.expander(f"{title} ({len(rows)})", expanded=False):
            data_table(
                [
                    {name.replace("_", " ").title(): str(value) for name, value in row.items()}
                    for row in rows
                    if isinstance(row, dict)
                ],
                "None reported.",
            )


def show_admin_live_media() -> None:
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
        "Live recording management",
        "Review, verify, and delete saved video and audio from live webcam interviews.",
        icon=":material/video_file:",
    )
    st.session_state.setdefault(DELETE_PENDING, False)
    st.session_state.setdefault(DELETE_RECORDING_ID, None)
    st.session_state.setdefault(DELETE_CONFIRMED, False)
    st.session_state.setdefault(MEDIA_DELETE_PENDING, False)
    st.session_state.setdefault(MEDIA_DELETE_ID, None)
    st.session_state.setdefault(MEDIA_DELETE_CONFIRMED, False)

    recordings_tab, health_tab = st.tabs(["Recordings", "Storage health"])
    with recordings_tab:
        if st.button(
            "Retry pending deletions",
            icon=":material/restart_alt:",
            key="_admin_live_media_retry_pending",
        ):
            try:
                report = retry_live_media_deletions(admin_id=admin_id)
                items = report.get("recordings", []) + report.get("media", [])
                pending = [item for item in items if item.get("status") == "pending_delete"]
                if pending:
                    st.warning(f"{len(pending)} deletion(s) are still pending. Please try again later.")
                else:
                    st.success("No pending deletions remain.")
            except LiveInterviewError as exc:
                error_state("Could not retry pending deletions.", str(exc))

        search_col, status_col = st.columns([3, 2])
        search = search_col.text_input(
            "Search by user email, username, interview ID, job title, or company",
            key="_admin_live_media_search",
        )
        recording_status = status_col.selectbox(
            "Recording status",
            options=RECORDING_STATUSES,
            key="_admin_live_media_status_filter",
        )
        try:
            recordings = list_admin_live_recordings(
                admin_id=admin_id,
                search=search,
                status=None if recording_status == "all" else recording_status,
                limit=200,
            )
        except LiveInterviewError as exc:
            error_state("Could not load live interview recordings.", str(exc))
            return
        st.caption(f"{len(recordings)} recording(s) found.")
        if not recordings:
            empty_state("No live recordings match the current filters.")
            return

        for recording in recordings:
            recording_id = int(recording["id"])
            interview_id = int(recording["interview_id"])
            duration = sum(float(item.get("duration_seconds") or 0) for item in recording["media"])
            available = sum(1 for item in recording["media"] if item.get("available"))
            with st.container(border=True):
                st.subheader(f"{recording.get('job_title') or 'Job interview'}")
                st.caption(
                    f"Interview #{interview_id} • {recording.get('company') or 'No company'} • "
                    f"{recording.get('owner_email') or 'Unknown user'} • "
                    f"Status: {recording.get('recording_status', '')} • "
                    f"Deletion: {recording.get('deletion_status', '')} • "
                    f"Created: {_format_when(recording.get('created_at'))} • "
                    f"Finalized: {_format_when(recording.get('finalized_at'))} • "
                    f"Media: {available}/{len(recording['media'])} available • {duration:.1f}s"
                )
                if recording.get("consent_given"):
                    st.caption(
                        f"Recording consent captured under {recording.get('consent_policy_version') or 'N/A'} "
                        f"at {_format_when(recording.get('consent_at'))}."
                    )
                else:
                    st.warning("No recording consent was captured for this interview.")
                _render_capture_manifest(recording.get("capture_manifest"))
                try:
                    details = get_live_recording_details(
                        interview_id=interview_id, admin_id=admin_id
                    )
                    _render_transcript(details.get("report") or {})
                except LiveInterviewError as exc:
                    st.caption(f"Transcript unavailable: {exc}")
                for item in recording["media"]:
                    _render_media_item(item, admin_id=admin_id)
                if st.button(
                    "Delete recording",
                    icon=":material/delete:",
                    width="stretch",
                    key=f"_admin_live_media_delete_request_{recording_id}",
                ):
                    st.session_state[DELETE_PENDING] = True
                    st.session_state[DELETE_RECORDING_ID] = recording_id
                    st.rerun()

        if st.session_state.get(DELETE_PENDING):
            pending_id = st.session_state.get(DELETE_RECORDING_ID)
            if pending_id:
                _delete_dialog()
                if st.session_state.get(DELETE_CONFIRMED):
                    try:
                        delete_live_recording(int(pending_id), admin_id=admin_id)
                        st.success("The recording and its saved files were deleted.", icon=":material/check:")
                    except LiveInterviewError as exc:
                        error_state("The recording could not be deleted.", str(exc))
                    st.session_state[DELETE_PENDING] = False
                    st.session_state[DELETE_CONFIRMED] = False
                    st.session_state[DELETE_RECORDING_ID] = None
                    st.rerun()
        if st.session_state.get(MEDIA_DELETE_PENDING):
            pending_media_id = st.session_state.get(MEDIA_DELETE_ID)
            if pending_media_id:
                _delete_media_dialog()
                if st.session_state.get(MEDIA_DELETE_CONFIRMED):
                    try:
                        delete_live_media(int(pending_media_id), admin_id=admin_id)
                        st.success("The recording file was deleted.", icon=":material/check:")
                    except LiveInterviewError as exc:
                        error_state("The recording file could not be deleted.", str(exc))
                    st.session_state[MEDIA_DELETE_PENDING] = False
                    st.session_state[MEDIA_DELETE_CONFIRMED] = False
                    st.session_state[MEDIA_DELETE_ID] = None
                    st.rerun()
    with health_tab:
        _render_storage_health(admin_id)


show_admin_live_media()
