"""Job-based multimodal interview page shared by text, voice, and video modes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import streamlit as st
from services.ai_avatar_interview_service import clear_avatar_session
from services.ai_avatar_session import (
    CANCELLED,
    COMPLETED,
    ERROR,
    IDLE,
    LISTENING,
    PROCESSING,
    SPEAKING,
    WAITING,
    AvatarSessionError,
    begin_listening,
    begin_processing,
    current_ai_avatar_state,
    initialize_ai_avatar_state,
    mark_question_spoken,
    mark_ready_to_complete,
    mark_resumed,
    mark_submitted,
    repeat_question,
    reset_ai_avatar_state,
    set_error,
    set_transcript,
)
from services.interview_media import MediaError, save_interview_media, transcribe_media
from services.job_interview_constants import MAX_ANSWER_SECONDS

from frontend.shared.avatar import avatar_status_label, render_ai_avatar
from frontend.shared.components import (
    empty_state,
    error_state,
    page_header,
    render_score_chart,
    speak_text,
    stop_speech,
    success_toast,
)
from frontend.shared.navigation import go_to

_AVATAR_SPEECH_KEY = "ai_avatar"


def _identity() -> tuple[int, str] | None:
    user_id = st.session_state.get("user_id")
    token = st.session_state.get("user_session_id")
    if not user_id or not token:
        return None
    return int(user_id), str(token)


def _upload_fingerprint(upload: Any) -> str | None:
    if upload is None:
        return None
    file_id = getattr(upload, "file_id", None)
    if file_id:
        return str(file_id)
    data = upload.getvalue()
    name = str(getattr(upload, "name", "answer"))
    return f"{name}:{hashlib.sha256(data).hexdigest()}"


def _sync_upload_state(
    state: Any,
    *,
    media_key: str,
    answer_key: str,
    fingerprint_key: str,
    fingerprint: str | None,
) -> bool:
    if state.get(fingerprint_key) == fingerprint:
        return False
    state.pop(media_key, None)
    state[answer_key] = ""
    state.pop(fingerprint_key, None)
    return True


def _process_media_upload(
    *,
    data: bytes,
    filename: str,
    kind: str,
    interview_id: int,
    user_id: int,
    session_token: str,
    consent_given: bool,
) -> tuple[int, str]:
    if not consent_given:
        raise MediaError("Give recording consent before uploading interview media.")
    record = save_interview_media(
        data=data,
        filename=filename,
        kind=kind,
        interview_id=interview_id,
        actor_user_id=user_id,
        session_token=session_token,
        consent_given=consent_given,
    )
    text = transcribe_media(int(record.id), actor_user_id=user_id, session_token=session_token)
    return int(record.id), str(text)


def _answer_time_remaining(started_at: datetime) -> int:
    elapsed = (datetime.now(UTC) - started_at).total_seconds()
    return max(0, MAX_ANSWER_SECONDS - int(elapsed))


def _clear_answer_timers(state: Any, interview_id: int) -> None:
    prefix = f"_job_answer_start_{interview_id}_"
    for key in list(state):
        if isinstance(key, str) and key.startswith(prefix):
            state.pop(key, None)


def _sync_voice_avatar_state(
    state: Any,
    *,
    user_id: int,
    session_token: str,
    interview_id: int,
    question: Any | None,
    job_title: str,
    company: str,
    consent_given: bool,
) -> dict[str, Any]:
    record = initialize_ai_avatar_state(state, user_id, session_token)
    question_number = int(question.question_number) if question is not None else 0
    status = str(record.get("status") or IDLE)
    stale = (
        int(record.get("interview_id") or 0) != int(interview_id)
        or status in {COMPLETED, CANCELLED}
        or (question is not None and int(record.get("question_id") or 0) != question_number)
        or (question is None and not record.get("can_complete"))
    )
    if stale:
        reset_ai_avatar_state(state, user_id, session_token)
        record = current_ai_avatar_state(state)
        if question is None:
            mark_ready_to_complete(
                record,
                interview_id=interview_id,
                job_title=job_title,
                company=company,
                consent_given=consent_given,
            )
        else:
            mark_resumed(
                record,
                interview_id=interview_id,
                job_title=job_title,
                company=company,
                question_id=question_number,
                question_number=question_number,
                question_text=str(question.question_text),
                question_category=str(getattr(question, "category", "") or ""),
                question_difficulty=str(getattr(question, "difficulty", "") or ""),
                consent_given=consent_given,
            )
    return record


def _voice_avatar_speech_key(record: dict[str, Any]) -> str:
    return (
        f"job_avatar_{int(record.get('interview_id') or 0)}_"
        f"{int(record.get('question_number') or 0)}_"
        f"{int(record.get('tts_key') or 0)}"
    )


def _render_voice_avatar(record: dict[str, Any], question_text: str) -> None:
    status = str(record.get("status") or IDLE)
    render_ai_avatar(
        status,
        question_text,
        f"job_{int(record.get('interview_id') or 0)}_{_voice_avatar_speech_key(record)}",
    )
    st.caption(f"AI avatar: {avatar_status_label(status)}")


def _ensure_voice_avatar_listening(record: dict[str, Any]) -> None:
    status = str(record.get("status") or IDLE)
    if status == SPEAKING:
        stop_speech(_AVATAR_SPEECH_KEY)
        mark_question_spoken(record)
    if str(record.get("status") or IDLE) in {WAITING, ERROR}:
        begin_listening(record)


def show_job_interview_page() -> None:
    identity = _identity()
    if identity is None:
        error_state("Sign in to take a job interview.")
        return
    user_id, session_token = identity
    page_header(
        "Job interview",
        "Answer job-grounded adaptive questions in text, voice, or video, then review your report.",
        ":material/videocam:",
    )
    report_id = st.session_state.get("_job_interview_report_id")
    active_id = st.session_state.get("_job_interview_active_id")
    if report_id:
        _render_report(int(report_id), user_id, session_token)
        return
    if active_id:
        _render_active(int(active_id), user_id, session_token)
        return
    _render_setup(user_id, session_token)


def _render_setup(user_id: int, session_token: str) -> None:
    from services.job_interview_service import list_job_interviews
    from services.job_post_service import get_job_post, list_jobs

    st.subheader("Choose a published job", icon=":material/work:")
    active = list_job_interviews(
        actor_user_id=user_id, session_token=session_token, completed=False
    )
    completed = list_job_interviews(
        actor_user_id=user_id, session_token=session_token, completed=True
    )
    if active:
        st.info("You have unfinished interviews. Resume one or start a new interview below.")
        for item in active:
            with st.container(border=True):
                label_col, button_col = st.columns([4, 1])
                label_col.markdown(f"**{item['job_title']}** — {item['company']}")
                label_col.caption(
                    f"{str(item['mode']).title()} mode • "
                    f"{int(item['overall_score'] or 0)}% average so far"
                )
                if button_col.button(
                    "Resume",
                    key=f"_job_resume_{item['id']}",
                    icon=":material/play_arrow:",
                    width="stretch",
                ):
                    st.session_state["_job_interview_active_id"] = int(item["id"])
                    st.rerun()
    if completed:
        with st.expander("Completed interviews", expanded=False):
            for item in completed:
                cols = st.columns([4, 2, 1])
                cols[0].markdown(f"**{item['job_title']}** — {item['company']}")
                cols[1].caption(
                    f"{str(item['mode']).title()} • {float(item['overall_score'] or 0):.1f}/10"
                )
                if cols[2].button(
                    "Report",
                    key=f"_job_report_{item['id']}",
                    icon=":material/assessment:",
                    width="stretch",
                ):
                    st.session_state["_job_interview_report_id"] = int(item["id"])
                    st.rerun()

    try:
        jobs = list_jobs()
    except Exception as exc:
        error_state("Could not load published jobs.", str(exc))
        return
    if not jobs:
        empty_state(
            "No published job postings are available yet.",
            "Ask an administrator to create and publish a job from the Admin Panel.",
        )
        return
    options = [int(job.id) for job in jobs]
    selected = st.selectbox(
        "Job posting",
        options=options,
        format_func=lambda value: next(
            f"{job.title} — {job.company}" for job in jobs if int(job.id) == value
        ),
        key="_job_interview_job_select",
    )
    job = get_job_post(int(selected), require_published=True)
    with st.expander("Job description", expanded=True):
        st.markdown(f"### {job.title}")
        st.caption(f"{job.company} • {job.experience_level or 'Any experience'}")
        st.write(job.description)
        if job.responsibilities:
            st.markdown("**Responsibilities**")
            for item in job.responsibilities:
                st.write(f"- {item}")
        st.markdown(
            "**Required skills:** " + (", ".join(job.required_skills or []) or "Not specified")
        )
        st.markdown(
            "**Preferred skills:** " + (", ".join(job.preferred_skills or []) or "Not specified")
        )

    config_col1, config_col2, config_col3 = st.columns(3)
    mode_label = config_col1.segmented_control(
        "Interview mode",
        options=("Text", "Voice", "Video"),
        default="Text",
        key="_job_interview_mode",
    )
    difficulty = config_col2.selectbox(
        "Difficulty",
        options=("easy", "medium", "hard"),
        index=1,
        key="_job_interview_difficulty",
    )
    duration = config_col3.slider(
        "Duration (minutes)",
        min_value=10,
        max_value=45,
        value=20,
        step=5,
        key="_job_interview_duration",
    )
    mode = str(mode_label or "Text").lower()
    if mode == "voice":
        render_ai_avatar(IDLE, "Your AI interviewer is ready to begin.", "job_voice_setup")
        st.caption(
            "Voice mode pairs you with an AI avatar, reads each question aloud, and transcribes "
            "your microphone answer for evaluation."
        )
    elif mode == "video":
        st.caption(
            "Video mode uses a recorded video upload and transcribes its audio. "
            "Streamlit cannot continuously record a live webcam without custom JavaScript."
        )
    else:
        st.caption("Text mode evaluates your written answers only.")

    consent = st.checkbox(
        "I consent to the interview being scored by AI and, for voice/video modes, to my recording "
        "being stored privately in this workspace for playback and transcription.",
        key="_job_interview_consent",
    )
    if st.button(
        "Start interview",
        type="primary",
        icon=":material/play_arrow:",
        width="stretch",
        disabled=not consent,
        key="_job_interview_start",
    ):
        from services.job_interview_service import start_job_interview

        with st.spinner("Preparing your job-specific interview…"):
            state = start_job_interview(
                session_token=session_token,
                actor_user_id=user_id,
                job_id=int(selected),
                mode=mode,
                difficulty=difficulty,
                duration_minutes=int(duration),
                consent_given=bool(consent),
            )
        st.session_state["_job_interview_active_id"] = int(state.interview_id)
        st.rerun()


def _render_active(interview_id: int, user_id: int, session_token: str) -> None:
    from services.job_interview_service import (
        JobInterviewError,
        abandon_job_interview,
        complete_job_interview,
        get_job_interview_state,
        submit_job_answer,
    )

    try:
        state = get_job_interview_state(interview_id, user_id, session_token)
    except Exception as exc:
        st.session_state.pop("_job_interview_active_id", None)
        error_state("This interview is no longer available.", str(exc))
        return
    if state.is_complete:
        st.session_state.pop("_job_interview_active_id", None)
        st.session_state["_job_interview_report_id"] = interview_id
        st.rerun()
        return

    question = state.current_question
    voice_record: dict[str, Any] | None = None
    if state.config.mode == "voice":
        voice_record = _sync_voice_avatar_state(
            st.session_state,
            user_id=user_id,
            session_token=session_token,
            interview_id=interview_id,
            question=question,
            job_title=state.job_title,
            company=state.company,
            consent_given=state.consent_given,
        )
    st.caption(f"{state.job_title} • {state.company} • {state.config.mode.title()} mode")
    st.progress(
        state.answered_count / max(1, state.config.target_questions),
        text=f"Answer {state.answered_count} of {state.config.target_questions} planned questions",
    )
    if question is None:
        if voice_record is not None:
            render_ai_avatar(COMPLETED, "All planned questions are answered.", "job_voice_complete")
        st.success("All planned questions are answered.")
        if st.button(
            "Generate final report",
            type="primary",
            icon=":material/assessment:",
            width="stretch",
            key="_job_interview_finish",
        ):
            if voice_record is not None:
                stop_speech(_AVATAR_SPEECH_KEY)
            with st.spinner("Analysing your answers…"):
                complete_job_interview(
                    interview_id=interview_id,
                    actor_user_id=user_id,
                    session_token=session_token,
                )
            if voice_record is not None:
                clear_avatar_session(
                    st.session_state, user_id=user_id, session_token=session_token
                )
            st.session_state.pop("_job_interview_active_id", None)
            st.session_state["_job_interview_report_id"] = interview_id
            st.rerun()
        return

    with st.container(border=True):
        st.markdown(f"### Question {question.question_number}")
        st.caption(f"{question.category} • {question.difficulty.title()}")
        st.markdown(f"**{question.question_text}**")
        if state.config.mode == "voice" and voice_record is not None:
            question_text = str(voice_record.get("question_text") or question.question_text)
            _render_voice_avatar(voice_record, question_text)
            status = str(voice_record.get("status") or IDLE)
            if status == SPEAKING:
                speak_text(question_text, key=_voice_avatar_speech_key(voice_record))
                if st.button(
                    "I heard the question",
                    type="primary",
                    icon=":material/check:",
                    width="stretch",
                    key=f"_job_avatar_heard_{interview_id}_{question.question_number}",
                ):
                    stop_speech(_AVATAR_SPEECH_KEY)
                    mark_question_spoken(voice_record)
                    st.rerun()
            elif status == WAITING:
                st.info("The AI avatar is waiting for your answer.")
                if st.button(
                    "Repeat question",
                    icon=":material/volume_up:",
                    key=f"_job_avatar_repeat_{interview_id}_{question.question_number}",
                ):
                    stop_speech(_AVATAR_SPEECH_KEY)
                    repeat_question(voice_record)
                    st.rerun()
            elif status == PROCESSING:
                st.caption("The AI avatar is transcribing your answer.")
        elif state.config.mode == "video":
            replay_key = f"_job_replay_{interview_id}_{question.question_number}"
            count = int(st.session_state.get(replay_key, 0))
            speak_text(
                question.question_text,
                key=f"job_q_{interview_id}_{question.question_number}_{count}",
            )
            if st.button(
                "Replay question", icon=":material/volume_up:", key=f"_job_replay_btn_{replay_key}"
            ):
                st.session_state[replay_key] = count + 1
                st.rerun()

    answer_key = f"_job_answer_{interview_id}_{question.question_number}"
    media_key = f"_job_media_{interview_id}_{question.question_number}"
    fingerprint_key = f"{media_key}_fingerprint"
    answer_start_key = f"_job_answer_start_{interview_id}_{question.question_number}"
    if answer_start_key not in st.session_state:
        st.session_state[answer_start_key] = datetime.now(UTC).isoformat()
    try:
        answer_started_at = datetime.fromisoformat(str(st.session_state[answer_start_key]))
    except ValueError:
        answer_started_at = datetime.now(UTC)
        st.session_state[answer_start_key] = answer_started_at.isoformat()
    remaining_seconds = _answer_time_remaining(answer_started_at)
    if remaining_seconds:
        minutes, seconds = divmod(remaining_seconds, 60)
        st.caption(f"Time remaining for this answer: {minutes}:{seconds:02d}")
    else:
        st.warning(
            f"The {MAX_ANSWER_SECONDS // 60}-minute answer limit has passed. "
            "Submit is disabled."
        )

    transcript = str(st.session_state.get(answer_key, ""))
    media_id = st.session_state.get(media_key)
    source_caption = "Type your answer."

    if state.config.mode == "voice":
        if voice_record is not None and str(voice_record.get("status") or IDLE) == WAITING:
            if st.button(
                "Answer with microphone",
                type="primary",
                icon=":material/mic:",
                width="stretch",
                key=f"_job_avatar_listen_{interview_id}_{question.question_number}",
            ):
                begin_listening(voice_record)
                st.rerun()
        recording = st.audio_input(
            "Record your answer", key=f"_job_audio_input_{interview_id}_{question.question_number}"
        )
        if voice_record is not None:
            avatar_status = str(voice_record.get("status") or IDLE)
            if avatar_status == LISTENING:
                st.caption(
                    "Microphone is ready. Allow browser access if prompted."
                    if recording is None
                    else "Recording captured. Review the transcript before submitting."
                )
            elif avatar_status == SPEAKING:
                st.caption("Listen to the avatar's question, then start your answer.")
        fingerprint = _upload_fingerprint(recording)
        if _sync_upload_state(
            st.session_state,
            media_key=media_key,
            answer_key=answer_key,
            fingerprint_key=fingerprint_key,
            fingerprint=fingerprint,
        ):
            media_id = None
        else:
            media_id = st.session_state.get(media_key)
        if recording is not None and media_id is None:
            source_caption = "Transcribed from your voice recording. Review before submitting."
            if not state.consent_given:
                if voice_record is not None:
                    set_error(voice_record, "Give recording consent before transcribing audio.")
                error_state("Give recording consent before saving an answer recording.")
            else:
                try:
                    if voice_record is not None:
                        _ensure_voice_avatar_listening(voice_record)
                        begin_processing(voice_record)
                    with st.spinner("Transcribing your recording…"):
                        saved_media_id, text = _process_media_upload(
                            data=recording.getvalue(),
                            filename=str(getattr(recording, "name", "answer.wav")),
                            kind="audio",
                            interview_id=interview_id,
                            user_id=user_id,
                            session_token=session_token,
                            consent_given=state.consent_given,
                        )
                except (MediaError, AvatarSessionError) as exc:
                    if voice_record is not None:
                        set_error(voice_record, str(exc))
                    error_state(str(exc))
                else:
                    if voice_record is not None:
                        set_transcript(voice_record, text)
                    st.session_state[media_key] = saved_media_id
                    st.session_state[fingerprint_key] = fingerprint
                    st.session_state[answer_key] = text
                    st.rerun()
    elif state.config.mode == "video":
        st.info(
            "Record a short video answer with your device, then upload it here. "
            "Its audio is transcribed; video frames are not analysed."
        )
        upload = st.file_uploader(
            "Upload a recorded video answer (MP4, WEBM, MOV)",
            type=["mp4", "webm", "mov", "m4v"],
            key=f"_job_video_upload_{interview_id}_{question.question_number}",
        )
        fingerprint = _upload_fingerprint(upload)
        if _sync_upload_state(
            st.session_state,
            media_key=media_key,
            answer_key=answer_key,
            fingerprint_key=fingerprint_key,
            fingerprint=fingerprint,
        ):
            media_id = None
        else:
            media_id = st.session_state.get(media_key)
        if upload is not None and media_id is None:
            source_caption = "Transcribed from your video. Review before submitting."
            if not state.consent_given:
                error_state("Give recording consent before saving an answer recording.")
            else:
                try:
                    with st.spinner("Transcribing video audio…"):
                        saved_media_id, text = _process_media_upload(
                            data=upload.getvalue(),
                            filename=str(getattr(upload, "name", "answer.mp4")),
                            kind="video",
                            interview_id=interview_id,
                            user_id=user_id,
                            session_token=session_token,
                            consent_given=state.consent_given,
                        )
                    st.session_state[media_key] = saved_media_id
                    st.session_state[fingerprint_key] = fingerprint
                    st.session_state[answer_key] = text
                    st.rerun()
                except MediaError as exc:
                    error_state(str(exc))

    answer = st.text_area(
        "Your answer",
        value=transcript,
        height=180,
        help=source_caption,
        key=f"{answer_key}_input",
    )
    if voice_record is not None and str(voice_record.get("status") or IDLE) == ERROR:
        error_state(str(voice_record.get("last_error") or "The AI avatar could not continue."))
    submit_col, abandon_col = st.columns([4, 1])
    if submit_col.button(
        "Submit answer",
        type="primary",
        icon=":material/send:",
        width="stretch",
        disabled=not answer.strip() or remaining_seconds <= 0,
        key=f"_job_submit_{interview_id}_{question.question_number}",
    ):
        try:
            submitted = submit_job_answer(
                interview_id=interview_id,
                question_id=int(question.question_number),
                answer=answer,
                media_id=int(media_id) if media_id else None,
                actor_user_id=user_id,
                session_token=session_token,
                answer_started_at=st.session_state[answer_start_key],
            )
            if voice_record is not None:
                stop_speech(_AVATAR_SPEECH_KEY)
                mark_submitted(voice_record, submitted.current_question)
        except (JobInterviewError, AvatarSessionError) as exc:
            if voice_record is not None:
                set_error(voice_record, str(exc))
            error_state(str(exc))
        else:
            st.session_state.pop(answer_key, None)
            st.session_state.pop(f"{answer_key}_input", None)
            st.session_state.pop(media_key, None)
            st.session_state.pop(fingerprint_key, None)
            st.session_state.pop(answer_start_key, None)
            st.rerun()
    if abandon_col.button(
        "Exit", icon=":material/logout:", width="stretch", key="_job_interview_exit"
    ):
        abandon_job_interview(
            interview_id=interview_id, actor_user_id=user_id, session_token=session_token
        )
        if voice_record is not None:
            stop_speech(_AVATAR_SPEECH_KEY)
            clear_avatar_session(st.session_state, user_id=user_id, session_token=session_token)
        st.session_state.pop("_job_interview_active_id", None)
        _clear_answer_timers(st.session_state, interview_id)
        success_toast("Interview saved as unfinished. You can resume it later.")
        st.rerun()
    st.caption("Scores and suggested answers are withheld until the interview is complete.")


def _render_report(interview_id: int, user_id: int, session_token: str) -> None:
    from services.interview_media import MediaError, read_media
    from services.job_interview_service import JobInterviewError, get_job_interview_report

    try:
        report = get_job_interview_report(
            interview_id, actor_user_id=user_id, session_token=session_token
        )
    except JobInterviewError as exc:
        st.session_state.pop("_job_interview_report_id", None)
        error_state(str(exc))
        return
    st.subheader("Final evaluation", icon=":material/assessment:")
    metric_col, summary_col = st.columns([1, 3])
    metric_col.metric("Overall score", f"{float(report['overall_score']):.1f}/10")
    metric_col.caption(f"Readiness: {str(report['readiness_level']).title()}")
    summary_col.write(report["executive_summary"])
    st.caption(
        f"{report['job_title']} at {report['company']} • "
        f"{report['interview_mode'].title()} mode • {report['questions_answered']} answers evaluated"
    )
    if report.get("category_scores"):
        render_score_chart(report["category_scores"], "Category scores from the adaptive rubric")
    if report.get("skill_coverage"):
        st.markdown("**Job skill coverage**")
        st.dataframe(
            [{"Skill": key, "Evidence": value} for key, value in report["skill_coverage"].items()],
            hide_index=True,
            width="stretch",
            alt="Evidence for each job skill",
        )
    feedback_col1, feedback_col2 = st.columns(2)
    feedback_col1.markdown("**Strengths**")
    for item in report.get("strengths", []):
        feedback_col1.write(f"- {item}")
    feedback_col2.markdown("**Areas to improve**")
    for item in report.get("areas_to_improve", []):
        feedback_col2.write(f"- {item}")
    st.markdown("**Recommended topics**")
    st.write(", ".join(report.get("recommended_topics", [])) or "None")
    st.markdown("**Learning plan**")
    for item in report.get("learning_plan", []):
        st.write(f"- {item}")

    st.subheader("Answer-by-answer review", icon=":material/chat:")
    for item in report.get("answers", []):
        with st.expander(
            f"Answer {item['question_number']} — {item['category']} ({float(item['score']):.1f}/10)",
            expanded=False,
        ):
            st.markdown(f"**Question:** {item['question']}")
            st.markdown("**Your answer**")
            st.write(item["answer"])
            st.markdown("**Stronger answer**")
            st.write(item["suggested_answer"] or "No suggestion recorded.")
            if item.get("feedback"):
                st.caption(item["feedback"])
            if item.get("media_id"):
                try:
                    payload, mime, kind = read_media(
                        int(item["media_id"]), actor_user_id=user_id, session_token=session_token
                    )
                    if kind == "audio":
                        st.audio(payload, format=mime)
                    elif kind == "video":
                        st.video(payload, format=mime)
                except MediaError as exc:
                    st.caption(f"Recording unavailable: {exc}")
    st.download_button(
        "Download report (JSON)",
        data=json.dumps(report, indent=2, ensure_ascii=False),
        file_name=f"job_interview_{interview_id}_report.json",
        mime="application/json",
        icon=":material/download:",
        width="stretch",
        key="_job_report_download",
    )
    if st.button(
        "Back to job preparation",
        icon=":material/arrow_back:",
        width="stretch",
        key="_job_report_back",
    ):
        st.session_state.pop("_job_interview_report_id", None)
        go_to("job")


show_job_interview_page()
