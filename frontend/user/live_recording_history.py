from __future__ import annotations

from datetime import datetime

import streamlit as st
from services.live_interview_workflow import (
    LiveInterviewError,
    delete_live_recording,
    get_live_recording_details,
    list_live_recordings,
    read_live_media,
    retry_live_media_deletions,
    verify_live_media,
)

from frontend.shared.components import empty_state, error_state, page_header

DELETE_PENDING = "_live_media_delete_pending"
DELETE_RECORDING_ID = "_live_media_delete_recording_id"
DELETE_CONFIRMED = "_live_media_delete_confirmed"


def _identity() -> tuple[int | None, str | None]:
    raw_user_id = st.session_state.get("user_id")
    raw_token = st.session_state.get("user_session_id") or st.session_state.get("session_token")
    try:
        user_id = int(raw_user_id) if raw_user_id is not None else None
    except (TypeError, ValueError):
        user_id = None
    token = str(raw_token) if raw_token else None
    return user_id, token


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
        "This permanently removes the saved video and audio files. Your transcript, answers, "
        "and final report stay in your interview history."
    )
    keep_col, delete_col = st.columns(2)
    if keep_col.button("Cancel", width="stretch", key="_live_media_delete_cancel"):
        st.session_state[DELETE_PENDING] = False
        st.session_state[DELETE_CONFIRMED] = False
        st.session_state[DELETE_RECORDING_ID] = None
        st.rerun()
    if delete_col.button(
        "Delete recording",
        type="primary",
        width="stretch",
        key="_live_media_delete_confirm",
    ):
        st.session_state[DELETE_CONFIRMED] = True
        st.rerun()


def _render_media_item(
    item: dict, *, recording: dict, user_id: int, session_token: str
) -> None:
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
            verification = verify_live_media(
                media_id, user_id=user_id, session_token=session_token
            )
            if not verification["checksum_valid"]:
                st.warning("This media file failed its integrity check.")
        except LiveInterviewError as exc:
            st.warning(str(exc))
        if item.get("available"):
            try:
                data, mime_type, filename = read_live_media(
                    media_id, user_id=user_id, session_token=session_token
                )
                if item["media_type"] == "video":
                    st.video(data, format="video/mp4")
                else:
                    st.audio(data, format="audio/wav")
                st.download_button(
                    f"Download {label.lower()}",
                    data=data,
                    file_name=filename,
                    mime=mime_type,
                    key=f"_live_media_download_{media_id}",
                    width="stretch",
                )
            except LiveInterviewError as exc:
                error_state("Could not open the recording.", str(exc))
        with st.expander("Media metadata", expanded=False):
            st.json(item.get("metadata") or {})


def show_live_recording_history(show_header: bool = True) -> None:
    if show_header:
        page_header(
            "Live recording history",
            "Review, play, download, and delete recordings from live webcam interviews.",
            icon=":material/video_file:",
        )
    st.session_state.setdefault(DELETE_PENDING, False)
    st.session_state.setdefault(DELETE_RECORDING_ID, None)
    st.session_state.setdefault(DELETE_CONFIRMED, False)
    user_id, token = _identity()
    if not user_id or not token:
        error_state("Please sign in to view your recordings.")
        return
    if st.button(
        "Retry pending deletions",
        icon=":material/restart_alt:",
        key="_live_media_retry_pending",
    ):
        try:
            report = retry_live_media_deletions(user_id=user_id, session_token=token)
            items = report.get("recordings", []) + report.get("media", [])
            pending = [item for item in items if item.get("status") == "pending_delete"]
            if pending:
                st.warning(f"{len(pending)} deletion(s) are still pending. Please try again later.")
            else:
                st.success("No pending deletions remain.")
        except LiveInterviewError as exc:
            error_state("Could not retry pending deletions.", str(exc))
    try:
        recordings = list_live_recordings(user_id=user_id, session_token=token)
    except LiveInterviewError as exc:
        error_state("Could not load live interview recordings.", str(exc))
        return
    if not recordings:
        empty_state(
            "No saved live recordings yet.",
            "Recordings appear here only after you grant separate recording consent during a live interview.",
        )
        return

    for recording in recordings:
        recording_id = int(recording["id"])
        interview_id = int(recording["interview_id"])
        duration = sum(float(item.get("duration_seconds") or 0) for item in recording["media"])
        with st.container(border=True):
            st.subheader(f"{recording.get('job_title') or 'Job interview'}")
            st.caption(
                f"{recording.get('company') or ''} • Interview #{interview_id} • "
                f"Status: {recording.get('recording_status', '')} • "
                f"Recorded: {_format_when(recording.get('created_at'))} • {duration:.1f}s"
            )
            if not recording.get("consent_given"):
                st.warning("Recording consent was not given for this interview.")
            if recording.get("recording_status") == "interrupted":
                st.warning("This interview ended before all answers were finalized.")
            for item in recording["media"]:
                _render_media_item(item, recording=recording, user_id=user_id, session_token=token)
            try:
                details = get_live_recording_details(
                    interview_id=interview_id, user_id=user_id, session_token=token
                )
                _render_transcript(details.get("report") or {})
            except LiveInterviewError as exc:
                st.caption(f"Transcript unavailable: {exc}")
            if st.button(
                "Delete recording",
                icon=":material/delete:",
                width="stretch",
                key=f"_live_media_delete_request_{recording_id}",
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
                    delete_live_recording(
                        int(pending_id), user_id=user_id, session_token=token
                    )
                    st.success("The recording and its saved files were deleted.", icon=":material/check:")
                except LiveInterviewError as exc:
                    error_state("The recording could not be deleted.", str(exc))
                st.session_state[DELETE_PENDING] = False
                st.session_state[DELETE_CONFIRMED] = False
                st.session_state[DELETE_RECORDING_ID] = None
                st.rerun()


if __name__ == "__main__":
    show_live_recording_history()
