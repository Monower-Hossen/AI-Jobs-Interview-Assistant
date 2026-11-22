from __future__ import annotations

from datetime import datetime

import streamlit as st
from services.auth_service import require_admin
from services.live_interview_workflow import (
    LiveInterviewError,
    delete_live_recording,
    get_live_recording_details,
    list_admin_live_recordings,
    read_live_media,
    verify_live_media,
)

from frontend.shared.components import empty_state, error_state, page_header

DELETE_PENDING = "_admin_live_media_delete_pending"
DELETE_RECORDING_ID = "_admin_live_media_delete_recording_id"
DELETE_CONFIRMED = "_admin_live_media_delete_confirmed"


def _format_when(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value or "—")


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


def _render_media_item(item: dict, *, admin_id: int) -> None:
    media_id = int(item["id"])
    label = "Camera video" if item["media_type"] == "video" else "Microphone audio"
    with st.container(border=True):
        st.markdown(f"**{label}**")
        st.caption(
            f"Status: {item['media_status']} • {int(item['size_bytes']) / 1024 / 1024:.2f} MB • "
            f"{float(item['duration_seconds']):.1f}s"
        )
        if not item.get("available"):
            st.warning("This media file is missing from local storage.")
        try:
            verification = verify_live_media(media_id, admin_id=admin_id)
            if not verification["checksum_valid"]:
                st.warning("This media file failed its integrity check.")
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


def show_admin_live_media() -> None:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        error_state("Administrator access required.")
        st.stop()
    admin_id = int(account.id)
    require_admin(admin_id)
    page_header(
        "Live recording management",
        "Review, verify, and delete saved video and audio from live webcam interviews.",
        icon=":material/video_file:",
    )
    st.session_state.setdefault(DELETE_PENDING, False)
    st.session_state.setdefault(DELETE_RECORDING_ID, None)
    st.session_state.setdefault(DELETE_CONFIRMED, False)

    search_col, status_col = st.columns([3, 1])
    search = search_col.text_input(
        "Search by user email, interview ID, or job title",
        key="_admin_live_media_search",
    )
    recording_status = status_col.selectbox(
        "Recording status",
        options=["all", "recording", "completed", "interrupted", "failed", "deleted", "not_consented"],
        key="_admin_live_media_status_filter",
    )
    try:
        recordings = list_admin_live_recordings(
            admin_id=admin_id,
            search=search,
            status=recording_status,
            limit=200,
        )
    except LiveInterviewError as exc:
        error_state("Could not load live interview recordings.", str(exc))
        return
    if not recordings:
        empty_state("No live recordings match the current filters.")
        return

    for recording in recordings:
        recording_id = int(recording["id"])
        interview_id = int(recording["interview_id"])
        duration = sum(float(item.get("duration_seconds") or 0) for item in recording["media"])
        with st.container(border=True):
            st.subheader(f"{recording.get('job_title') or 'Job interview'}")
            st.caption(
                f"Interview #{interview_id} • {recording.get('user_email') or 'Unknown user'} • "
                f"Status: {recording.get('recording_status', '')} • "
                f"Created: {_format_when(recording.get('created_at'))} • {duration:.1f}s"
            )
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


show_admin_live_media()
