from __future__ import annotations

import json
from datetime import UTC, datetime

import streamlit as st
from services.ai_avatar_interview_service import (
    AvatarInterviewError,
    begin_avatar_listening,
    cancel_avatar_interview,
    clear_avatar_session,
    complete_avatar_interview,
    get_avatar_interview_state,
    mark_avatar_question_spoken,
    pause_avatar_interview,
    repeat_avatar_question,
    resume_avatar_interview,
    resume_avatar_interview_state,
    retry_avatar_step,
    start_avatar_interview,
    submit_avatar_answer,
    transcribe_avatar_answer,
)
from services.ai_avatar_session import (
    CANCELLED,
    COMPLETED,
    ERROR,
    IDLE,
    LISTENING,
    PAUSED,
    PROCESSING,
    SETUP,
    SPEAKING,
    WAITING,
    clear_audio,
    initialize_ai_avatar_state,
    mark_completed,
    mark_ready_to_complete,
    mark_resumed,
)
from services.job_interview_service import get_job_interview_report, list_job_interviews
from services.job_post_service import get_job_post, list_jobs

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

REPORT_KEY = "_ai_avatar_report_id"


def _identity() -> tuple[int, str] | None:
    user_id = st.session_state.get("user_id")
    token = st.session_state.get("user_session_id")
    if not user_id or not token:
        return None
    return int(user_id), str(token)


def show_ai_avatar_interview() -> None:
    identity = _identity()
    if identity is None:
        error_state("Sign in to use the AI avatar interviewer.")
        return
    user_id, session_token = identity
    page_header(
        "AI avatar interview",
        "Speak with a job-focused AI avatar, review your transcript, and receive adaptive feedback.",
        ":material/smart_toy:",
    )
    record = initialize_ai_avatar_state(st.session_state, user_id, session_token)
    report_id = st.session_state.get(REPORT_KEY)
    if report_id:
        _render_report(int(report_id), user_id, session_token)
        return
    status = str(record.get("status") or IDLE)
    if status == COMPLETED and record.get("interview_id"):
        st.session_state[REPORT_KEY] = int(record["interview_id"])
        st.rerun()
        return
    if status == CANCELLED:
        st.info("The AI avatar session was stopped. You can start a new interview below.")
    if not record.get("interview_id") or status in {IDLE, SETUP, CANCELLED}:
        _render_setup(user_id, session_token)
        return
    _render_active(record, user_id, session_token)


def _render_setup(user_id: int, session_token: str) -> None:
    try:
        interviews = [
            item
            for item in list_job_interviews(
                actor_user_id=user_id, session_token=session_token
            )
            if str(item.get("mode") or "") == "avatar"
        ]
    except Exception as exc:
        error_state("Could not load AI avatar interview history.", str(exc))
        interviews = []
    active = [item for item in interviews if item.get("status") in {"created", "in_progress"}]
    completed = [item for item in interviews if item.get("status") == "completed"]
    if active:
        st.subheader("Resume an interview", icon=":material/play_arrow:")
        for item in active:
            with st.container(border=True):
                label_col, button_col = st.columns([4, 1])
                label_col.markdown(f"**{item['job_title']}** — {item['company']}")
                label_col.caption("AI avatar interview in progress")
                if button_col.button(
                    "Resume",
                    key=f"_avatar_resume_{item['id']}",
                    icon=":material/play_arrow:",
                    width="stretch",
                ):
                    try:
                        resume_avatar_interview(
                            st.session_state,
                            interview_id=int(item["id"]),
                            user_id=user_id,
                            session_token=session_token,
                        )
                    except AvatarInterviewError as exc:
                        error_state(str(exc))
                    else:
                        st.session_state.pop(REPORT_KEY, None)
                        st.rerun()
    if completed:
        with st.expander("Completed AI avatar interviews", expanded=False):
            for item in completed:
                label_col, button_col = st.columns([4, 1])
                label_col.markdown(f"**{item['job_title']}** — {item['company']}")
                label_col.caption(f"Score: {float(item.get('overall_score') or 0):.1f}/10")
                if button_col.button(
                    "Report",
                    key=f"_avatar_report_{item['id']}",
                    icon=":material/assessment:",
                    width="stretch",
                ):
                    st.session_state[REPORT_KEY] = int(item["id"])
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
    st.subheader("Start a new AI avatar interview", icon=":material/work:")
    options = [int(job.id) for job in jobs]
    selected = st.selectbox(
        "Job posting",
        options=options,
        format_func=lambda value: next(
            f"{job.title} — {job.company}" for job in jobs if int(job.id) == value
        ),
        key="_avatar_job_select",
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
    config_col1, config_col2 = st.columns(2)
    difficulty = config_col1.selectbox(
        "Difficulty",
        options=("easy", "medium", "hard"),
        index=1,
        key="_avatar_difficulty",
    )
    duration = config_col2.slider(
        "Duration (minutes)",
        min_value=10,
        max_value=45,
        value=20,
        step=5,
        key="_avatar_duration",
    )
    st.caption(
        "The avatar speaks each question. Your microphone audio is transcribed into reviewable "
        "text and is not stored as a recording. You can correct the transcript before submitting."
    )
    consent = st.checkbox(
        "I consent to AI-generated questions and feedback, and to local microphone transcription "
        "for this practice interview.",
        key="_avatar_consent",
    )
    if st.button(
        "Start AI avatar interview",
        type="primary",
        icon=":material/play_arrow:",
        width="stretch",
        disabled=not consent,
        key="_avatar_start",
    ):
        try:
            with st.spinner("Preparing your AI avatar interview…"):
                start_avatar_interview(
                    st.session_state,
                    user_id=user_id,
                    session_token=session_token,
                    job_id=int(selected),
                    difficulty=str(difficulty),
                    duration_minutes=int(duration),
                    consent_given=bool(consent),
                )
        except AvatarInterviewError as exc:
            error_state(str(exc))
        else:
            st.session_state.pop(REPORT_KEY, None)
            st.rerun()


def _render_active(
    record: dict, user_id: int, session_token: str
) -> None:
    interview_id = int(record["interview_id"])
    try:
        job_state = get_avatar_interview_state(
            st.session_state, user_id=user_id, session_token=session_token
        )
    except AvatarInterviewError as exc:
        clear_avatar_session(st.session_state, user_id=user_id, session_token=session_token)
        error_state("This AI avatar interview is no longer available.", str(exc))
        return
    if job_state.is_complete:
        mark_completed(record)
        st.session_state[REPORT_KEY] = interview_id
        st.rerun()
        return
    question = job_state.current_question
    if question is None:
        if not record.get("can_complete"):
            mark_ready_to_complete(
                record,
                interview_id=interview_id,
                job_title=job_state.job_title,
                company=job_state.company,
                consent_given=job_state.consent_given,
            )
        _render_completion(record, user_id, session_token)
        return
    if int(record.get("question_id") or 0) != int(question.question_number):
        mark_resumed(
            record,
            interview_id=interview_id,
            job_title=job_state.job_title,
            company=job_state.company,
            question_id=int(question.question_number),
            question_number=int(question.question_number),
            question_text=str(question.question_text),
            question_category=str(question.category),
            question_difficulty=str(question.difficulty),
            consent_given=job_state.consent_given,
        )
    if not record.get("question_started_at"):
        record["question_started_at"] = datetime.now(UTC).isoformat()
    status = str(record.get("status") or IDLE)
    question_text = str(record.get("question_text") or question.question_text)
    render_ai_avatar(
        status,
        question_text,
        f"{interview_id}_{int(record.get('question_number') or 0)}_{int(record.get('tts_key') or 0)}",
    )
    st.caption(
        f"{avatar_status_label(status)} • {job_state.job_title} at {job_state.company} • "
        f"{job_state.config.mode.title()} mode"
    )
    st.progress(
        job_state.answered_count / max(1, job_state.config.target_questions),
        text=(
            f"Answer {job_state.answered_count} of "
            f"{job_state.config.target_questions} planned questions"
        ),
    )
    _render_question(record, user_id, session_token, status, question_text)
    _render_controls(record, user_id, session_token, status)
    _render_answer_editor(record, user_id, session_token, status)


def _render_question(
    record: dict, user_id: int, session_token: str, status: str, question_text: str
) -> None:
    with st.container(border=True):
        st.markdown(f"### Question {int(record.get('question_number') or 0)}")
        st.caption(
            f"{record.get('question_category') or 'Job-specific'!s} • "
            f"{str(record.get('question_difficulty') or 'medium').title()}"
        )
        st.markdown(f"**{question_text}**")
        if status == SPEAKING:
            speak_text(
                question_text,
                key=(
                    f"avatar_{int(record.get('interview_id') or 0)}_"
                    f"{int(record.get('question_number') or 0)}_"
                    f"{int(record.get('tts_key') or 0)}"
                ),
            )
            if st.button(
                "I heard the question",
                type="primary",
                icon=":material/check:",
                width="stretch",
                key="_avatar_question_heard",
            ):
                stop_speech("ai_avatar")
                mark_avatar_question_spoken(
                    st.session_state, user_id=user_id, session_token=session_token
                )
                st.rerun()
        elif status == WAITING:
            st.info("Waiting for your response. Start the microphone when you are ready.")
            if st.button(
                "Answer with microphone",
                type="primary",
                icon=":material/mic:",
                width="stretch",
                key="_avatar_start_listening",
            ):
                begin_avatar_listening(
                    st.session_state, user_id=user_id, session_token=session_token
                )
                st.rerun()


def _render_microphone(record: dict, user_id: int, session_token: str) -> None:
    audio_key = int(record.get("audio_key") or 0)
    widget_key = f"_avatar_audio_{audio_key}"
    recording = st.audio_input("Record your answer", key=widget_key)
    if recording is None:
        st.caption("Microphone is ready. Allow browser access if prompted.")
    else:
        st.audio(recording.getvalue())
        if st.button(
            "Transcribe answer",
            type="primary",
            icon=":material/graphic_eq:",
            width="stretch",
            key=f"{widget_key}_transcribe",
        ):
            st.session_state.pop(widget_key, None)
            try:
                with st.spinner("Transcribing your microphone answer…"):
                    transcribe_avatar_answer(
                        st.session_state,
                        audio_bytes=recording.getvalue(),
                        user_id=user_id,
                        session_token=session_token,
                    )
            except AvatarInterviewError as exc:
                error_state(str(exc))
            else:
                success_toast("Transcript ready. Review and correct it before submitting.")
                st.rerun()
    if st.button(
        "Retry recording",
        icon=":material/restart_alt:",
        width="stretch",
        key=f"{widget_key}_retry",
    ):
        st.session_state.pop(widget_key, None)
        clear_audio(record)
        st.rerun()


def _render_controls(
    record: dict, user_id: int, session_token: str, status: str
) -> None:
    repeat_col, pause_col, stop_col = st.columns(3)
    if repeat_col.button(
        "Repeat question",
        icon=":material/volume_up:",
        width="stretch",
        key="_avatar_repeat",
    ):
        stop_speech("ai_avatar")
        repeat_avatar_question(st.session_state, user_id=user_id, session_token=session_token)
        st.rerun()
    if status == PAUSED:
        if pause_col.button(
            "Resume",
            icon=":material/play_arrow:",
            width="stretch",
            key="_avatar_resume_state",
        ):
            resume_avatar_interview_state(
                st.session_state, user_id=user_id, session_token=session_token
            )
            st.rerun()
    elif pause_col.button(
        "Pause",
        icon=":material/pause:",
        width="stretch",
        key="_avatar_pause",
    ):
        stop_speech("ai_avatar")
        pause_avatar_interview(st.session_state, user_id=user_id, session_token=session_token)
        st.rerun()
    if stop_col.button(
        "End interview",
        icon=":material/stop:",
        width="stretch",
        key="_avatar_end",
    ):
        stop_speech("ai_avatar")
        cancel_avatar_interview(st.session_state, user_id=user_id, session_token=session_token)
        success_toast("AI avatar interview saved as unfinished.")
        st.rerun()


def _render_answer_editor(
    record: dict, user_id: int, session_token: str, status: str
) -> None:
    if status == PROCESSING:
        with st.spinner("Thinking about your answer…"):
            st.write("Transcription is running locally. This usually takes a few seconds.")
    if status == PAUSED:
        st.warning("The interview is paused. Resume to continue.")
    if status == LISTENING:
        _render_microphone(record, user_id, session_token)
    if status == ERROR:
        error_state(str(record.get("last_error") or "The AI avatar could not continue."))
        if st.button(
            "Retry current step",
            icon=":material/restart_alt:",
            width="stretch",
            key="_avatar_retry_step",
        ):
            retry_avatar_step(st.session_state, user_id=user_id, session_token=session_token)
            st.rerun()
    if status not in {WAITING, ERROR}:
        return
    transcript_key = f"_avatar_transcript_{int(record.get('transcript_key') or 0)}"
    if transcript_key not in st.session_state:
        st.session_state[transcript_key] = str(record.get("transcript") or "")
    answer = st.text_area(
        "Review or correct your transcript",
        height=170,
        help="Edit the text before submitting; only this text is used for evaluation.",
        key=transcript_key,
    )
    if st.button(
        "Submit answer",
        type="primary",
        icon=":material/send:",
        width="stretch",
        disabled=not str(answer or "").strip(),
        key="_avatar_submit_answer",
    ):
        try:
            with st.spinner("The AI avatar is evaluating your answer…"):
                submit_avatar_answer(
                    st.session_state,
                    answer=str(answer),
                    user_id=user_id,
                    session_token=session_token,
                )
        except AvatarInterviewError as exc:
            error_state(str(exc))
        else:
            st.rerun()


def _render_completion(
    record: dict, user_id: int, session_token: str
) -> None:
    render_ai_avatar(COMPLETED, "All planned questions are answered.", "avatar_complete")
    st.success("All planned questions are answered. Generate your final report when ready.")
    if st.button(
        "Generate final report",
        type="primary",
        icon=":material/assessment:",
        width="stretch",
        key="_avatar_finish",
    ):
        stop_speech("ai_avatar")
        try:
            with st.spinner("Preparing your final report…"):
                complete_avatar_interview(
                    st.session_state, user_id=user_id, session_token=session_token
                )
        except AvatarInterviewError as exc:
            error_state(str(exc))
        else:
            st.session_state[REPORT_KEY] = int(record["interview_id"])
            st.rerun()


def _render_report(interview_id: int, user_id: int, session_token: str) -> None:
    try:
        report = get_job_interview_report(
            interview_id, actor_user_id=user_id, session_token=session_token
        )
    except Exception as exc:
        st.session_state.pop(REPORT_KEY, None)
        error_state("This AI avatar report is not available.", str(exc))
        return
    st.subheader("AI avatar interview report", icon=":material/assessment:")
    metric_col, summary_col = st.columns([1, 3])
    metric_col.metric("Overall score", f"{float(report['overall_score']):.1f}/10")
    metric_col.caption(f"Readiness: {str(report['readiness_level']).title()}")
    summary_col.write(report["executive_summary"])
    st.caption(
        f"{report['job_title']} at {report['company']} • "
        f"{report['questions_answered']} answers evaluated"
    )
    if report.get("category_scores"):
        render_score_chart(report["category_scores"], "Adaptive interview scorecard")
    if report.get("skill_coverage"):
        st.markdown("**Job skill coverage**")
        st.dataframe(
            [{"Skill": key, "Evidence": value} for key, value in report["skill_coverage"].items()],
            hide_index=True,
            width="stretch",
            alt="Evidence for each job skill",
        )
    strengths_col, improve_col = st.columns(2)
    strengths_col.markdown("**Strengths**")
    for item in report.get("strengths", []):
        strengths_col.write(f"- {item}")
    improve_col.markdown("**Areas to improve**")
    for item in report.get("areas_to_improve", []):
        improve_col.write(f"- {item}")
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
    st.download_button(
        "Download report (JSON)",
        data=json.dumps(report, indent=2, ensure_ascii=False),
        file_name=f"ai_avatar_interview_{interview_id}_report.json",
        mime="application/json",
        icon=":material/download:",
        width="stretch",
        key="_avatar_report_download",
    )
    if st.button(
        "Start another AI avatar interview",
        icon=":material/add:",
        width="stretch",
        key="_avatar_report_new",
    ):
        st.session_state.pop(REPORT_KEY, None)
        clear_avatar_session(st.session_state, user_id=user_id, session_token=session_token)
        st.rerun()
    if st.button(
        "Back to job preparation",
        icon=":material/arrow_back:",
        width="stretch",
        key="_avatar_report_back",
    ):
        st.session_state.pop(REPORT_KEY, None)
        go_to("job")


show_ai_avatar_interview()
