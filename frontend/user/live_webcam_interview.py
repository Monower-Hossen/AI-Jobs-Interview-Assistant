from __future__ import annotations

import io
import logging
import wave
from typing import Any

import streamlit as st

logger = logging.getLogger(__name__)
from services.job_interview_constants import MAX_ANSWER_SECONDS
from services.live_interview_state import (
    ACTIVE_INTERVIEW_ID,
    ANSWER_STARTED_AT,
    INTERVIEW_CONSENT,
    INTERVIEW_ERROR,
    MEDIA_ERROR,
    MEDIA_STATUS,
    PENDING_MEDIA_IDS,
    RECORDING_ACTIVE,
    RECORDING_CONSENT,
    RECORDING_CONSENT_AT,
    RECORDING_ID,
    RECORDING_POLICY_VERSION,
    RECORDING_QUESTION,
    REPORT_ID,
    SUBMITTED_FINGERPRINT,
    TRANSCRIPT,
    TRANSCRIPT_ERROR,
    TTS_COUNTER,
    add_pending_media_id,
    can_submit_answer,
    clear_transcript,
    get_recorder,
    get_video_recorder,
    mark_answer_submitted,
    prepare_for_question,
    recording_consent_granted,
    remaining_answer_seconds,
    reset_answer_state,
    set_media_status,
    set_recording_consent,
    set_transcript,
    set_transcript_error,
    start_recording,
    stop_recording,
)
from services.live_interview_state import (
    initialize_live_interview_state as initialize_interview_state,
)
from services.live_interview_workflow import (
    LiveInterviewError,
    abandon_live_interview,
    available_jobs,
    capture_audio_bytes,
    complete_live_interview,
    delete_live_media,
    delete_live_recording,
    finish_live_recording,
    get_live_interview_state,
    get_live_recording_for_interview,
    get_live_report,
    list_live_interviews,
    save_live_media,
    start_live_interview,
    start_live_recording,
    submit_live_answer,
    transcribe_audio_bytes,
)
from services.live_webcam_state import (
    CAMERA,
    CONSENT_CAMERA,
    CONSENT_MICROPHONE,
    MICROPHONE,
    consent_granted,
    drain_audio_events,
    get_audio_event_queue,
    initialize_live_webcam_state,
    record_connection_failure,
    set_stream_error,
    set_stream_requested,
    stop_all_streams,
    stream_error,
    stream_requested,
    stream_status,
)
from services.llm_service import get_provider_status
from services.resource_monitor import ResourceBusyError, check_heavy_task_allowed

from frontend.shared.components import (
    empty_state,
    error_state,
    page_header,
    render_score_chart,
    speak_text,
    success_toast,
)
from frontend.shared.navigation import go_to

WebRtcMode: Any = None
webrtc_streamer: Any = None
PreviewVideoProcessor: Any = None
RecordingAudioProcessor: Any = None
RecordingVideoProcessor: Any = None
WEBRTC_IMPORT_ERROR: str | None = None
WEBRTC_IMPORT_ERROR_TYPE: str | None = None
WEBRTC_IMPORT_DIAGNOSTIC: str | None = None
_WEBRTC_VENV_STARTUP_HINT = r".\venv\Scripts\python.exe -m streamlit run app.py"


def _is_missing_streamlit_webrtc(exc: BaseException) -> bool:
    if not isinstance(exc, ModuleNotFoundError):
        return False
    missing_name = getattr(exc, "name", "") or ""
    if missing_name == "streamlit_webrtc" or missing_name.startswith("streamlit_webrtc."):
        return True
    message = str(exc)
    return "No module named 'streamlit_webrtc'" in message or 'No module named "streamlit_webrtc"' in message


def _set_webrtc_import_failure(exc: BaseException) -> None:
    global WEBRTC_IMPORT_ERROR, WEBRTC_IMPORT_ERROR_TYPE, WEBRTC_IMPORT_DIAGNOSTIC

    WEBRTC_IMPORT_ERROR = str(exc)
    WEBRTC_IMPORT_ERROR_TYPE = type(exc).__name__
    logger.exception("Live webcam WebRTC import failed", exc_info=exc)

    if _is_missing_streamlit_webrtc(exc):
        WEBRTC_IMPORT_DIAGNOSTIC = (
            "The streamlit-webrtc package is not available in the Python interpreter running this app. "
            "On Windows, start the app with the project virtual environment: "
            f"{_WEBRTC_VENV_STARTUP_HINT}. "
            "A global Streamlit or Python installation may use a different interpreter."
        )
        return

    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        WEBRTC_IMPORT_DIAGNOSTIC = (
            f"{type(exc).__name__}: {exc}. "
            "Live webcam dependencies could not be loaded in this Python environment. "
            "Use the project virtual environment to run Streamlit: "
            f"{_WEBRTC_VENV_STARTUP_HINT}."
        )
        return

    WEBRTC_IMPORT_DIAGNOSTIC = (
        f"Unexpected {type(exc).__name__} while loading live webcam support. "
        "Check application logs for details, then run the app with the project virtual environment: "
        f"{_WEBRTC_VENV_STARTUP_HINT}."
    )


try:
    from services.live_webcam_capture import (
        PreviewVideoProcessor as _PreviewVideoProcessor,
    )
    from services.live_webcam_capture import (
        RecordingAudioProcessor as _RecordingAudioProcessor,
    )
    from services.live_webcam_capture import (
        RecordingVideoProcessor as _RecordingVideoProcessor,
    )
    from streamlit_webrtc import WebRtcMode as _WebRtcMode
    from streamlit_webrtc import webrtc_streamer as _webrtc_streamer

    PreviewVideoProcessor = _PreviewVideoProcessor
    RecordingAudioProcessor = _RecordingAudioProcessor
    RecordingVideoProcessor = _RecordingVideoProcessor
    WebRtcMode = _WebRtcMode
    webrtc_streamer = _webrtc_streamer
except Exception as exc:
    _set_webrtc_import_failure(exc)

CAMERA_PLAYING = "_live_webcam_camera_playing"
MICROPHONE_PLAYING = "_live_webcam_microphone_playing"
ABANDON_PENDING = "_live_webcam_abandon_pending"
ABANDON_DECISION = "_live_webcam_abandon_decision"
DELETE_MEDIA_ON_ABANDON = "_live_webcam_delete_media_on_abandon"


@st.fragment(run_every="2s")
def _render_audio_activity() -> None:
    events = drain_audio_events(st.session_state)
    if not events:
        st.caption("No microphone frames received yet.")
        return
    latest = events[-1]
    st.caption(
        f"Microphone active: {latest.frame_count} frames received; "
        f"latest frame {latest.samples} samples / {latest.channels} channel(s)."
    )


def _identity() -> tuple[int | None, str | None]:
    raw_user_id = st.session_state.get("user_id")
    raw_token = st.session_state.get("user_session_id") or st.session_state.get("session_token")
    try:
        user_id = int(raw_user_id) if raw_user_id is not None else None
    except (TypeError, ValueError):
        user_id = None
    token = str(raw_token) if raw_token else None
    return user_id, token


def _render_stream_status(kind: str, context) -> None:
    playing = bool(context.state.playing)
    signalling = bool(context.state.signalling)
    record_connection_failure(
        st.session_state, kind, playing=playing, signalling=signalling
    )
    status = stream_status(
        kind,
        consent=consent_granted(st.session_state, kind),
        requested=stream_requested(st.session_state, kind),
        playing=playing,
        signalling=signalling,
        error_reason=stream_error(st.session_state, kind),
    )
    message = f"**{status['label']}** — {status['message']}"
    tone = status["tone"]
    if tone == "success":
        st.success(message, icon=":material/check_circle:")
    elif tone == "warning":
        st.warning(message, icon=":material/warning:")
    elif tone == "info":
        st.info(message, icon=":material/info:")
    else:
        st.caption(message)


def _render_controls(kind: str, label: str) -> None:
    consent = consent_granted(st.session_state, kind)
    requested = stream_requested(st.session_state, kind)
    start_col, stop_col = st.columns(2)
    if start_col.button(
        f"Start {label.lower()}",
        key=f"_live_webcam_start_{kind}",
        type="primary",
        width="stretch",
        disabled=not consent or requested,
    ):
        set_stream_requested(st.session_state, kind, True)
        st.rerun()
    if stop_col.button(
        f"Stop {label.lower()}",
        key=f"_live_webcam_stop_{kind}",
        width="stretch",
        disabled=not requested,
    ):
        set_stream_requested(st.session_state, kind, False)
        st.rerun()


def _render_video_stream(requested: bool) -> None:
    video_recorder = get_video_recorder(st.session_state)
    context = webrtc_streamer(
        key="live_webcam_camera_streamer",
        mode=WebRtcMode.SENDRECV,
        media_stream_constraints={"video": True, "audio": False},
        video_processor_factory=lambda: RecordingVideoProcessor(video_recorder),
        desired_playing_state=requested,
        media_toggle_controls=False,
        sendback_video=True,
        sendback_audio=False,
    )
    st.session_state[CAMERA_PLAYING] = bool(context.state.playing)
    _render_stream_status(CAMERA, context)


def _render_audio_stream(requested: bool) -> None:
    event_queue = get_audio_event_queue(st.session_state)
    recorder = get_recorder(st.session_state)
    context = webrtc_streamer(
        key="live_webcam_microphone_streamer",
        mode=WebRtcMode.SENDRECV,
        media_stream_constraints={"video": False, "audio": True},
        audio_processor_factory=lambda: RecordingAudioProcessor(event_queue, recorder),
        desired_playing_state=requested,
        media_toggle_controls=False,
        sendback_video=False,
        sendback_audio=False,
    )
    st.session_state[MICROPHONE_PLAYING] = bool(context.state.playing)
    _render_stream_status(MICROPHONE, context)
    _render_audio_activity()


def _stop_live_streams() -> None:
    stop_recording(st.session_state)
    st.session_state[CAMERA_PLAYING] = False
    st.session_state[MICROPHONE_PLAYING] = False
    stop_all_streams(st.session_state)


def _wav_duration_seconds(data: bytes) -> float:
    try:
        with wave.open(io.BytesIO(data), "rb") as wav_file:
            rate = wav_file.getframerate()
            return round(wav_file.getnframes() / rate, 3) if rate else 0.0
    except (EOFError, wave.Error):
        return 0.0


def _delete_pending_media(user_id: int, session_token: str) -> None:
    pending = list(st.session_state.get(PENDING_MEDIA_IDS) or [])
    errors: list[str] = []
    for media_id in pending:
        try:
            delete_live_media(int(media_id), user_id=user_id, session_token=session_token)
        except LiveInterviewError as exc:
            errors.append(str(exc))
    st.session_state[PENDING_MEDIA_IDS] = []
    if errors:
        set_media_status(st.session_state, "delete_failed", "; ".join(errors))


def _finalize_answer_media(
    *,
    interview_id: int,
    question_number: int,
    user_id: int,
    session_token: str,
) -> tuple[str | None, str | None]:
    recorder = get_recorder(st.session_state)
    video_recorder = get_video_recorder(st.session_state)
    consent = recording_consent_granted(st.session_state)
    recording_id = st.session_state.get(RECORDING_ID)
    audio_snapshot = recorder.snapshot()
    video_snapshot = video_recorder.snapshot()
    audio_bytes = b""
    transcript_error: str | None = None
    try:
        audio_bytes = capture_audio_bytes(recorder)
    except LiveInterviewError as exc:
        transcript_error = str(exc)

    video_bytes = b""
    try:
        video_bytes = video_recorder.finalize()
    except ResourceBusyError as exc:
        set_media_status(st.session_state, "failed", str(exc))
    except Exception as exc:
        set_media_status(st.session_state, "failed", str(exc))

    if consent and recording_id:
        saved_ids: list[int] = []
        metadata_base = {
            "question_number": int(question_number),
            "consent_policy_version": RECORDING_POLICY_VERSION,
        }
        try:
            if audio_bytes:
                metadata = {
                    **metadata_base,
                    "sample_rate": audio_snapshot.get("sample_rate"),
                    "channels": audio_snapshot.get("channels"),
                    "chunk_count": audio_snapshot.get("chunk_count"),
                    "dropped_chunks": audio_snapshot.get("dropped_chunks"),
                }
                saved_ids.append(
                    save_live_media(
                        recording_id=int(recording_id),
                        interview_id=int(interview_id),
                        user_id=user_id,
                        session_token=session_token,
                        media_type="audio",
                        data=audio_bytes,
                        duration_seconds=_wav_duration_seconds(audio_bytes),
                        metadata=metadata,
                    )
                )
            if video_bytes:
                metadata = {
                    **metadata_base,
                    "codec": "h264",
                    "container": "mp4",
                    "fps": 30,
                    "width": video_snapshot.get("width"),
                    "height": video_snapshot.get("height"),
                    "frames_encoded": video_snapshot.get("frames_encoded"),
                    "dropped_frames": video_snapshot.get("dropped_frames"),
                    "timestamp_adjustments": video_snapshot.get("timestamp_adjustments"),
                    "overflowed": video_snapshot.get("overflowed"),
                }
                saved_ids.append(
                    save_live_media(
                        recording_id=int(recording_id),
                        interview_id=int(interview_id),
                        user_id=user_id,
                        session_token=session_token,
                        media_type="video",
                        data=video_bytes,
                        duration_seconds=float(video_snapshot.get("duration_seconds") or 0.0),
                        metadata=metadata,
                    )
                )
            for media_id in saved_ids:
                add_pending_media_id(st.session_state, media_id)
            if saved_ids:
                set_media_status(
                    st.session_state,
                    "saved",
                    "Video and audio files are separate and are not synchronized." if len(saved_ids) > 1 else None,
                )
            else:
                set_media_status(st.session_state, "failed", "No playable media was captured.")
        except ResourceBusyError as exc:
            set_media_status(st.session_state, "failed", str(exc))
        except LiveInterviewError as exc:
            set_media_status(st.session_state, "failed", str(exc))
    else:
        set_media_status(
            st.session_state,
            "not_saved",
            "Recording storage consent is not active; temporary capture was discarded.",
        )

    recorder.clear()
    transcript: str | None = None
    if audio_bytes:
        try:
            transcript = transcribe_audio_bytes(audio_bytes)
        except LiveInterviewError as exc:
            transcript_error = str(exc)
    return transcript, transcript_error


def _provider_caption() -> None:
    try:
        status = get_provider_status()
        provider = str(status.get("active") or "configured").title()
        fallback = "enabled" if status.get("fallback_enabled") else "disabled"
        st.caption(
            f"Answer text is scored by the configured AI provider ({provider}); "
            f"automatic provider fallback is {fallback}. Transcription is local."
        )
    except Exception:
        st.caption("Answer text is scored by your configured AI provider. Transcription is local.")


def _recording_consent_changed() -> None:
    set_recording_consent(st.session_state, bool(st.session_state.get(RECORDING_CONSENT)))


def _render_consent_and_streams(locked: bool) -> None:
    with st.container(border=True):
        st.subheader("Consent and privacy", icon=":material/shield:")
        st.write(
            "Camera frames are previewed locally and microphone audio is normalized to 16 kHz mono "
            "for local Whisper transcription. If you explicitly opt in below, the camera video and "
            "original microphone audio are also saved on the application's local server so you "
            "and authorized administrators can review them. Recordings remain stored until you "
            "delete them; deleting media keeps your transcript and report. You can request deletion "
            "according to the application's authorization rules. No facial, emotion, or personality "
            "analysis is performed."
        )
        st.checkbox("I consent to camera access.", key=CONSENT_CAMERA, disabled=locked)
        st.checkbox("I consent to microphone access.", key=CONSENT_MICROPHONE, disabled=locked)
        st.checkbox(
            "I consent to AI transcription and scoring of my live spoken answers.",
            key=INTERVIEW_CONSENT,
            disabled=locked,
        )
        st.checkbox(
            "Separate consent: record and save camera video and microphone audio for later playback.",
            key=RECORDING_CONSENT,
            disabled=locked,
            on_change=_recording_consent_changed,
        )
        st.caption(
            "Recording storage is off by default. Interview consent alone never enables recording "
            "storage. Video and audio are saved as separate files and are not muxed into one "
            "synchronized recording."
        )
        _provider_caption()
        if locked:
            st.caption(
                "Consent is locked while an interview is active. Exit the interview to withdraw it."
            )
        if not all(consent_granted(st.session_state, kind) for kind in (CAMERA, MICROPHONE)):
            st.info("Grant camera and microphone consent before starting a stream.", icon=":material/info:")

    camera_requested = stream_requested(st.session_state, CAMERA)
    microphone_requested = stream_requested(st.session_state, MICROPHONE)

    with st.container(border=True):
        st.subheader("Camera", icon=":material/videocam:")
        _render_controls(CAMERA, "Camera")
        try:
            _render_video_stream(camera_requested)
        except Exception as exc:
            st.session_state[CAMERA_PLAYING] = False
            set_stream_error(st.session_state, CAMERA, str(exc))
            set_stream_requested(st.session_state, CAMERA, False)
            error_state("The camera stream could not be started.", str(exc))

    with st.container(border=True):
        st.subheader("Microphone", icon=":material/mic:")
        _render_controls(MICROPHONE, "Microphone")
        try:
            _render_audio_stream(microphone_requested)
        except Exception as exc:
            st.session_state[MICROPHONE_PLAYING] = False
            set_stream_error(st.session_state, MICROPHONE, str(exc))
            set_stream_requested(st.session_state, MICROPHONE, False)
            error_state("The microphone stream could not be started.", str(exc))


def _reset_live_workflow() -> None:
    reset_answer_state(st.session_state)
    clear_transcript(st.session_state)
    st.session_state[SUBMITTED_FINGERPRINT] = None
    st.session_state[INTERVIEW_ERROR] = None
    st.session_state[TTS_COUNTER] = 0


def _render_resume(user_id: int, session_token: str) -> None:
    try:
        interviews = list_live_interviews(user_id=user_id, session_token=session_token)
    except LiveInterviewError as exc:
        error_state("Could not load unfinished interviews.", str(exc))
        return
    if not interviews:
        return
    st.markdown("**Unfinished interviews**")
    for item in interviews:
        interview_id = int(item["id"])
        label_col, resume_col = st.columns([4, 1])
        label_col.markdown(f"**{item.get('job_title', 'Interview')}** — {item.get('company', '')}")
        label_col.caption(f"Status: {item.get('status', 'in_progress')}")
        if resume_col.button(
            "Resume",
            key=f"_live_webcam_resume_{interview_id}",
            icon=":material/play_arrow:",
            width="stretch",
        ):
            _reset_live_workflow()
            st.session_state[ACTIVE_INTERVIEW_ID] = interview_id
            st.session_state[REPORT_ID] = None
            st.session_state[RECORDING_ID] = None
            st.session_state[RECORDING_CONSENT] = False
            st.session_state[RECORDING_CONSENT_AT] = None
            try:
                details = get_live_recording_for_interview(
                    interview_id=interview_id, user_id=user_id, session_token=session_token
                )
                if details and details.get("consent_given"):
                    st.session_state[RECORDING_ID] = int(details["id"])
                    st.session_state[RECORDING_CONSENT] = True
                    st.session_state[RECORDING_CONSENT_AT] = str(details.get("consent_at") or "")
            except LiveInterviewError as exc:
                st.session_state[MEDIA_ERROR] = str(exc)
            st.rerun()


def _render_setup(user_id: int, session_token: str) -> None:
    _render_resume(user_id, session_token)
    try:
        jobs = available_jobs()
    except Exception as exc:
        error_state("Could not load published jobs.", str(exc))
        return
    if not jobs:
        empty_state(
            "No published job postings are available yet.",
            "Ask an administrator to create and publish a job from the Admin Panel.",
        )
        return

    st.subheader("Start a live interview", icon=":material/work:")
    options = [int(job.id) for job in jobs]
    selected = st.selectbox(
        "Job posting",
        options=options,
        format_func=lambda value: next(
            f"{job.title} — {job.company}" for job in jobs if int(job.id) == value
        ),
        key="_live_webcam_job_select",
    )
    job = next(item for item in jobs if int(item.id) == selected)
    with st.expander("Job details", expanded=False):
        st.markdown(f"### {job.title}")
        st.caption(f"{job.company} • {getattr(job, 'experience_level', None) or 'Any experience'}")
        st.write(job.description)
        if getattr(job, "responsibilities", None):
            st.markdown("**Responsibilities**")
            for item in job.responsibilities:
                st.write(f"- {item}")
        st.markdown("**Required skills:** " + (", ".join(job.required_skills or []) or "Not specified"))

    config_col1, config_col2 = st.columns(2)
    difficulty = config_col1.selectbox(
        "Difficulty",
        options=("easy", "medium", "hard"),
        index=1,
        key="_live_webcam_difficulty",
    )
    duration = config_col2.slider(
        "Duration (minutes)",
        min_value=10,
        max_value=45,
        value=20,
        step=5,
        key="_live_webcam_duration",
    )
    streams_ready = all(
        bool(st.session_state.get(key)) for key in (CAMERA_PLAYING, MICROPHONE_PLAYING)
    )
    consents_ready = all(
        consent_granted(st.session_state, kind) for kind in (CAMERA, MICROPHONE)
    ) and bool(st.session_state.get(INTERVIEW_CONSENT))
    if not streams_ready:
        st.caption("Connect both streams above before beginning the interview.")
    if st.button(
        "Start live interview",
        type="primary",
        icon=":material/play_arrow:",
        width="stretch",
        key="_live_webcam_start_interview",
        disabled=not (streams_ready and consents_ready),
    ):
        try:
            state = start_live_interview(
                user_id=user_id,
                session_token=session_token,
                job_id=selected,
                difficulty=difficulty,
                duration_minutes=int(duration),
                camera_consent=consent_granted(st.session_state, CAMERA),
                microphone_consent=consent_granted(st.session_state, MICROPHONE),
                interview_consent=bool(st.session_state.get(INTERVIEW_CONSENT)),
            )
        except LiveInterviewError as exc:
            error_state("Could not start the live interview.", str(exc))
            return
        _reset_live_workflow()
        st.session_state[ACTIVE_INTERVIEW_ID] = int(state.interview_id)
        st.session_state[REPORT_ID] = None
        st.session_state[RECORDING_ID] = None
        if st.session_state.get(RECORDING_CONSENT) and not st.session_state.get(RECORDING_CONSENT_AT):
            set_recording_consent(st.session_state, True)
        try:
            recording_id = start_live_recording(
                interview_id=int(state.interview_id),
                user_id=user_id,
                session_token=session_token,
                consent_given=recording_consent_granted(st.session_state),
                consent_at=st.session_state.get(RECORDING_CONSENT_AT),
                policy_version=RECORDING_POLICY_VERSION,
            )
            st.session_state[RECORDING_ID] = recording_id
            set_media_status(st.session_state, "recording" if recording_consent_granted(st.session_state) else "not_consented")
        except LiveInterviewError as exc:
            set_media_status(st.session_state, "failed", str(exc))
        st.rerun()


def _render_report(user_id: int, session_token: str) -> None:
    interview_id = int(st.session_state[REPORT_ID])
    try:
        report = get_live_report(
            interview_id=interview_id, user_id=user_id, session_token=session_token
        )
    except LiveInterviewError as exc:
        error_state("Could not load the final report.", str(exc))
        st.session_state[REPORT_ID] = None
        return

    st.subheader("Interview report", icon=":material/assessment:")
    st.caption(f"{report.get('job_title', 'Job interview')} • {report.get('company', '')}")
    st.metric("Overall score", f"{float(report.get('overall_score') or 0):.1f}/10", border=True)
    if report.get("readiness_level"):
        st.write(f"**Readiness:** {report['readiness_level']}")
    if report.get("executive_summary"):
        st.write(report["executive_summary"])
    if report.get("category_scores"):
        render_score_chart(report["category_scores"], "AI-generated rubric scores")
    for heading, key in (
        ("Strengths", "strengths"),
        ("Areas to improve", "areas_to_improve"),
        ("Recommended topics", "recommended_topics"),
        ("Learning plan", "learning_plan"),
    ):
        items = report.get(key) or []
        if items:
            st.markdown(f"**{heading}**")
            for item in items:
                st.write(f"- {item}")
    reviews = report.get("answers") or []
    if reviews:
        with st.expander("Answer reviews", expanded=False):
            for review in reviews:
                st.markdown(f"**Question {review.get('question_number', '?')}**")
                st.caption(str(review.get("question", "")))
                st.write(f"Score: {float(review.get('overall_score') or 0):.1f}/10")
                st.write(str(review.get("feedback", "")))
    if st.button(
        "Done",
        type="primary",
        icon=":material/check:",
        width="stretch",
        key="_live_webcam_report_done",
    ):
        _stop_live_streams()
        _reset_live_workflow()
        st.session_state[RECORDING_CONSENT] = False
        st.session_state[RECORDING_CONSENT_AT] = None
        st.session_state[RECORDING_ID] = None
        st.session_state[DELETE_MEDIA_ON_ABANDON] = False
        st.session_state[REPORT_ID] = None
        st.rerun()


def _finish_interview(interview_id: int, user_id: int, session_token: str) -> None:
    if st.session_state.get(RECORDING_ACTIVE):
        stop_recording(st.session_state)
        question = st.session_state.get(RECORDING_QUESTION)
        if question is not None:
            text, error = _finalize_answer_media(
                interview_id=interview_id,
                question_number=int(question),
                user_id=user_id,
                session_token=session_token,
            )
            if text:
                set_transcript(st.session_state, int(question), text)
            if error:
                set_transcript_error(st.session_state, error)
    recording_id = st.session_state.get(RECORDING_ID)
    if recording_id:
        try:
            finish_live_recording(
                recording_id=int(recording_id),
                user_id=user_id,
                session_token=session_token,
                status="completed",
            )
        except LiveInterviewError as exc:
            set_media_status(st.session_state, "finalize_failed", str(exc))
    try:
        complete_live_interview(
            interview_id=interview_id,
            user_id=user_id,
            session_token=session_token,
            consent_given=bool(st.session_state.get(INTERVIEW_CONSENT)),
        )
    except LiveInterviewError as exc:
        st.session_state[INTERVIEW_ERROR] = str(exc)
        return
    _stop_live_streams()
    st.session_state[ACTIVE_INTERVIEW_ID] = None
    st.session_state[REPORT_ID] = interview_id
    st.rerun()


@st.dialog("Abandon live interview?", width="small")
def _abandon_dialog() -> None:
    st.warning(
        "Saved answers stay in your interview history. The current transcript and unsaved answer "
        "buffer will be discarded, and camera/microphone capture will stop. Any recording already "
        "saved for completed answers remains stored until you delete it."
    )
    st.checkbox(
        "Also delete this interview's saved video and audio recordings now",
        key=DELETE_MEDIA_ON_ABANDON,
    )
    keep_col, abandon_col = st.columns(2)
    if keep_col.button("Keep interview", width="stretch", key="_live_webcam_abandon_keep"):
        st.session_state[ABANDON_DECISION] = "keep"
        st.rerun()
    if abandon_col.button(
        "Abandon interview",
        type="primary",
        width="stretch",
        key="_live_webcam_abandon_confirm",
    ):
        st.session_state[ABANDON_DECISION] = "abandon"
        st.rerun()


def _perform_abandon(interview_id: int, user_id: int, session_token: str) -> None:
    failed = False
    if st.session_state.get(RECORDING_ACTIVE):
        stop_recording(st.session_state)
        question = st.session_state.get(RECORDING_QUESTION)
        if question is not None:
            _finalize_answer_media(
                interview_id=interview_id,
                question_number=int(question),
                user_id=user_id,
                session_token=session_token,
            )
    recording_id = st.session_state.get(RECORDING_ID)
    if recording_id:
        try:
            if st.session_state.get(DELETE_MEDIA_ON_ABANDON):
                delete_live_recording(
                    int(recording_id), user_id=user_id, session_token=session_token
                )
                set_media_status(st.session_state, "deleted", "Saved recordings were deleted.")
            else:
                finish_live_recording(
                    recording_id=int(recording_id),
                    user_id=user_id,
                    session_token=session_token,
                    status="interrupted",
                )
                set_media_status(st.session_state, "interrupted", "Saved recordings were kept.")
        except LiveInterviewError as exc:
            set_media_status(st.session_state, "finalize_failed", str(exc))
    try:
        abandon_live_interview(
            interview_id=interview_id, user_id=user_id, session_token=session_token
        )
    except LiveInterviewError as exc:
        st.session_state[INTERVIEW_ERROR] = str(exc)
        failed = True
    finally:
        stop_recording(st.session_state)
        clear_transcript(st.session_state)
    reset_answer_state(st.session_state)
    st.session_state[SUBMITTED_FINGERPRINT] = None
    if not failed:
        _stop_live_streams()
        st.session_state[ACTIVE_INTERVIEW_ID] = None
        st.session_state[REPORT_ID] = None
        st.session_state[RECORDING_CONSENT] = False
        st.session_state[RECORDING_CONSENT_AT] = None
        st.session_state[RECORDING_ID] = None
        st.session_state[DELETE_MEDIA_ON_ABANDON] = False
        go_to("job")
        return
    st.rerun()


def _render_active(user_id: int, session_token: str) -> None:
    interview_id = int(st.session_state[ACTIVE_INTERVIEW_ID])
    try:
        state = get_live_interview_state(
            interview_id, user_id=user_id, session_token=session_token
        )
    except LiveInterviewError as exc:
        _stop_live_streams()
        st.session_state[ACTIVE_INTERVIEW_ID] = None
        st.session_state[INTERVIEW_ERROR] = str(exc)
        st.rerun()
        return
    if state.is_complete:
        _stop_live_streams()
        st.session_state[ACTIVE_INTERVIEW_ID] = None
        st.session_state[REPORT_ID] = interview_id
        st.rerun()
        return

    question = state.current_question
    st.caption(f"{state.job_title} • {state.company} • {state.config.mode.title()} mode")
    st.progress(
        state.answered_count / max(1, state.config.target_questions),
        text=f"Answer {state.answered_count} of {state.config.target_questions} planned questions",
    )
    if question is None:
        st.warning(
            "No pending question was found in this session. You can generate the final report "
            "from the answers that were saved."
        )
        if st.button(
            "Generate final report",
            type="primary",
            icon=":material/assessment:",
            width="stretch",
            key="_live_webcam_finish",
        ):
            with st.spinner("Analysing your answers…"):
                _finish_interview(interview_id, user_id, session_token)
        return

    prepare_for_question(st.session_state, question.question_number)
    remaining = remaining_answer_seconds(st.session_state)
    recorder = get_recorder(st.session_state)
    snapshot = recorder.snapshot()
    recording = bool(st.session_state.get(RECORDING_ACTIVE))
    microphone_ready = bool(st.session_state.get(MICROPHONE_PLAYING))
    consent = bool(st.session_state.get(INTERVIEW_CONSENT))

    if recording and (
        not microphone_ready
        or not consent
        or remaining <= 0
        or float(snapshot["duration_seconds"]) >= MAX_ANSWER_SECONDS
    ):
        stop_recording(st.session_state)
        text, error = _finalize_answer_media(
            interview_id=interview_id,
            question_number=question.question_number,
            user_id=user_id,
            session_token=session_token,
        )
        if text:
            set_transcript(st.session_state, question.question_number, text)
        if error:
            set_transcript_error(st.session_state, error)
        st.rerun()
        return

    with st.container(border=True):
        st.markdown(f"### Question {question.question_number}")
        st.caption(f"{question.category} • {question.difficulty.title()}")
        st.markdown(f"**{question.question_text}**")
        counter = int(st.session_state.get(TTS_COUNTER, 0))
        speak_text(
            question.question_text,
            key=f"live_q_{interview_id}_{question.question_number}_{counter}",
        )
        if st.button(
            "Replay question",
            icon=":material/volume_up:",
            key="_live_webcam_replay_question",
        ):
            st.session_state[TTS_COUNTER] = counter + 1
            st.rerun()

    if remaining:
        minutes, seconds = divmod(remaining, 60)
        st.caption(f"Time remaining for this answer: {minutes}:{seconds:02d}")
    else:
        st.warning(f"The {MAX_ANSWER_SECONDS // 60}-minute answer limit has passed. Submit is disabled.")

    st.caption(
        f"Audio buffer: {snapshot['duration_seconds']}s captured at "
        f"{snapshot['sample_rate'] or '—'} Hz, {snapshot['channels'] or '—'} channel(s), "
        f"{snapshot['chunk_count']} chunk(s)."
    )
    video_snapshot = get_video_recorder(st.session_state).snapshot()
    media_status = str(st.session_state.get(MEDIA_STATUS) or "not_started")
    st.caption(
        f"Persistent recording: {media_status}. Camera frames: "
        f"{video_snapshot['frames_encoded']} encoded, {video_snapshot['dropped_frames']} dropped."
    )
    if st.session_state.get(MEDIA_ERROR):
        error_state("Recording problem", str(st.session_state[MEDIA_ERROR]))
    if video_snapshot.get("error"):
        error_state("Camera recording problem", str(video_snapshot["error"]))
    if recording:
        st.info("Recording in progress. Speak now, then choose Stop & transcribe.", icon=":material/mic:")
    if snapshot.get("capture_error"):
        error_state("Live audio capture failed.", str(snapshot["capture_error"]))
    if snapshot.get("overflowed"):
        st.warning(
            "Audio capture reached its limit. Stop the answer to transcribe what was captured."
        )
    if int(snapshot.get("dropped_chunks") or 0) > 0:
        st.warning(
            f"{snapshot['dropped_chunks']} audio chunk(s) were dropped because the device format "
            "changed or the buffer limit was reached. Only captured audio will be transcribed."
        )
    if not microphone_ready:
        st.warning(
            "Microphone is disconnected. Recording is unavailable; an existing transcript can "
            "still be reviewed and submitted."
        )
    if not consent:
        error_state("Consent is not active. Exit the interview before changing consent.")

    can_start, start_reason = check_heavy_task_allowed("whisper_transcription")
    if not can_start and not recording:
        st.warning(start_reason or "Speech transcription is temporarily unavailable.")
    start_col, stop_col, discard_col = st.columns([1, 1, 1])
    if start_col.button(
        "Start answer",
        type="primary",
        icon=":material/mic:",
        width="stretch",
        key="_live_webcam_start_answer",
        disabled=recording or remaining <= 0 or not microphone_ready or not consent or not can_start,
    ):
        start_recording(
            st.session_state,
            question.question_number,
            record_video=recording_consent_granted(st.session_state),
        )
        set_media_status(st.session_state, "recording" if recording_consent_granted(st.session_state) else "not_consented")
        st.rerun()
    if stop_col.button(
        "Stop & transcribe",
        icon=":material/stop:",
        width="stretch",
        key="_live_webcam_stop_answer",
        disabled=not recording,
    ):
        stop_recording(st.session_state)
        with st.spinner("Finalizing media and transcribing your answer with the local Whisper model…"):
            text, error = _finalize_answer_media(
                interview_id=interview_id,
                question_number=question.question_number,
                user_id=user_id,
                session_token=session_token,
            )
            if text:
                set_transcript(st.session_state, question.question_number, text)
            if error:
                set_transcript_error(st.session_state, error)
        st.rerun()
    if discard_col.button(
        "Discard answer",
        icon=":material/delete:",
        width="stretch",
        key="_live_webcam_discard_answer",
    ):
        _delete_pending_media(user_id, session_token)
        reset_answer_state(st.session_state)
        clear_transcript(st.session_state)
        st.rerun()

    if st.session_state.get(TRANSCRIPT_ERROR):
        error_state("Transcription problem", str(st.session_state[TRANSCRIPT_ERROR]))
    elif st.session_state.get(TRANSCRIPT):
        st.caption("Transcript ready for review. You may edit it before submitting.")

    transcript = str(st.session_state.get(TRANSCRIPT, ""))
    st.text_area(
        "Transcribed answer — review and edit before submitting",
        key=TRANSCRIPT,
        height=180,
        help="This text is evaluated. Editing it does not change the captured audio.",
        placeholder="Start your answer, speak, then stop and transcribe.",
    )

    answer_ready = can_submit_answer(
        st.session_state, interview_id, question.question_number, transcript
    )
    if st.button(
        "Submit answer",
        type="primary",
        icon=":material/send:",
        width="stretch",
        key="_live_webcam_submit_answer",
        disabled=not answer_ready or remaining <= 0 or not consent,
    ):
        started_at = st.session_state.get(ANSWER_STARTED_AT)
        with st.spinner("Evaluating your answer and choosing the next question…"):
            try:
                submit_live_answer(
                    interview_id=interview_id,
                    question_number=question.question_number,
                    transcript=transcript,
                    user_id=user_id,
                    session_token=session_token,
                    answer_started_at=started_at,
                    consent_given=consent,
                )
            except LiveInterviewError as exc:
                st.session_state[INTERVIEW_ERROR] = str(exc)
                st.rerun()
                return
        mark_answer_submitted(
            st.session_state, interview_id, question.question_number, transcript
        )
        success_toast("Answer saved. Here is your next question.")
        st.rerun()

    if st.button(
        "Exit interview",
        icon=":material/logout:",
        width="stretch",
        key="_live_webcam_exit_interview",
    ):
        st.session_state[ABANDON_PENDING] = True
        st.session_state[ABANDON_DECISION] = None
        st.rerun()


def show_live_webcam_interview_page() -> None:
    initialize_live_webcam_state(st.session_state)
    initialize_interview_state(st.session_state)
    st.session_state.setdefault(CAMERA_PLAYING, False)
    st.session_state.setdefault(MICROPHONE_PLAYING, False)
    st.session_state.setdefault(ABANDON_PENDING, False)
    st.session_state.setdefault(ABANDON_DECISION, None)
    st.session_state.setdefault(DELETE_MEDIA_ON_ABANDON, False)
    user_id, token = _identity()
    page_header(
        "AI live webcam interview",
        "Take a job-grounded adaptive interview with a live camera preview, spoken answers, and optional local recording storage.",
        icon=":material/videocam:",
    )
    if not user_id or not token:
        error_state("Please sign in to use the live webcam interview.")
        st.stop()
    if WEBRTC_IMPORT_ERROR:
        detail = WEBRTC_IMPORT_DIAGNOSTIC or (
            f"Live webcam import failed: {WEBRTC_IMPORT_ERROR_TYPE or 'ImportError'}: {WEBRTC_IMPORT_ERROR}"
        )
        error_state("Live webcam support is unavailable.", detail)
        st.stop()

    if st.session_state.get(ABANDON_PENDING) and st.session_state.get(ACTIVE_INTERVIEW_ID):
        _abandon_dialog()
        decision = st.session_state.get(ABANDON_DECISION)
        if decision == "keep":
            st.session_state[ABANDON_PENDING] = False
            st.session_state[ABANDON_DECISION] = None
            st.rerun()
        if decision == "abandon":
            interview_id = int(st.session_state[ACTIVE_INTERVIEW_ID])
            st.session_state[ABANDON_PENDING] = False
            st.session_state[ABANDON_DECISION] = None
            _perform_abandon(interview_id, user_id, token)
        st.stop()

    if st.session_state.get(INTERVIEW_ERROR):
        error_state("Live interview problem", str(st.session_state[INTERVIEW_ERROR]))
        st.session_state[INTERVIEW_ERROR] = None

    if st.session_state.get(REPORT_ID):
        _render_report(user_id, token)
        return

    _render_consent_and_streams(bool(st.session_state.get(ACTIVE_INTERVIEW_ID)))
    if st.session_state.get(ACTIVE_INTERVIEW_ID):
        _render_active(user_id, token)
    else:
        _render_setup(user_id, token)


show_live_webcam_interview_page()
