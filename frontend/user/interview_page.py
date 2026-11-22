"""AI mock interview: adaptive questions, text/voice answers, evaluation and reports."""

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import cast

import streamlit as st
from config.settings import runtime_path, settings
from database import get_db_context
from database.models import UserSession, UserSettings
from services import (
    evaluate_answer,
    generate_interview_questions,
    generate_interview_report,
    interview_service,
)
from services.answer_evaluator import AnswerEvaluation
from services.auth_service import AuthError, owned_session_id
from services.interview_generator import InterviewGenerator
from services.job_analyzer import JobAnalysisResult
from services.job_matcher import ResumeJobMatchResult
from services.resume_analyzer import ResumeAnalysisResult
from services.speech_to_text import transcribe_audio
from utils.file_utils import validate_mime_type

from frontend.shared.components import (
    empty_state,
    error_state,
    kpi_row,
    page_header,
    render_score_chart,
    section,
    speak_text,
)
from frontend.shared.navigation import go_to

INTERVIEW_TYPES = [
    "Full Interview",
    "HR Interview",
    "Technical Interview",
    "Behavioral Interview",
    "Machine Learning Interview",
    "Python Interview",
    "Data Science Interview",
    "AI/ML Engineer Interview",
    "Custom Interview",
]

CATEGORY_LABELS = {
    "technical": "Technical",
    "hr": "HR",
    "behavioral": "Behavioral",
    "situational": "Situational",
    "project_based": "Project-based",
    "resume_based": "Resume-based",
    "job_specific": "Job-specific",
}


def _workspace_token() -> str:
    token = st.session_state.get("user_session_id")
    if not token:
        token = str(uuid.uuid4())
        st.session_state.user_session_id = token
    return str(token)


def _render_resume_banner() -> None:
    current_id = st.session_state.get("interview_session_id")
    if current_id and st.session_state.get("interview_started"):
        return
    try:
        active = interview_service.get_active_interview(
            _workspace_token(), st.session_state.get("user_id")
        )
    except AuthError as exc:
        error_state("Workspace access unavailable", str(exc))
        return
    if active is None:
        return
    with st.container(border=True):
        st.markdown("**An interview is already in progress**")
        st.caption(
            f"{active.interview_type} · {active.difficulty.title()} · "
            f"question {active.current_question_index + 1} of {max(active.total_questions, 1)}"
        )
        continue_col, new_col = st.columns(2)
        with continue_col:
            if st.button(
                "Continue practice",
                icon=":material/play_arrow:",
                type="primary",
                width="stretch",
                key="resume_interview_button",
            ):
                _load_active_interview(active)
                st.rerun()
        with new_col:
            if st.button(
                "Start a new interview",
                icon=":material/add:",
                width="stretch",
                key="discard_interview_button",
            ):
                try:
                    interview_service.abandon_active_interviews(
                        _workspace_token(), st.session_state.get("user_id")
                    )
                except AuthError as exc:
                    error_state("Workspace access unavailable", str(exc))
                    return
                reset_interview_state()
                st.toast("Previous practice session was closed.", icon=":material/check:")
                st.rerun()


def _load_active_interview(active) -> None:
    try:
        questions, question_ids, saved_answers = interview_service.load_interview_questions(
            interview_id=active.id,
            session_token=_workspace_token(),
            actor_user_id=st.session_state.get("user_id"),
        )
    except AuthError as exc:
        error_state("This interview is not accessible", str(exc))
        return
    answered = [
        (question, item)
        for question, item in zip(questions, saved_answers)
        if item["answer"]
    ]
    st.session_state.interview_questions = questions
    st.session_state.interview_question_ids = question_ids
    st.session_state.interview_config = {
        "type": active.interview_type,
        "difficulty": active.difficulty,
        "num_questions": len(questions),
        "categories": list(CATEGORY_LABELS),
        "adaptive": False,
        "read_aloud": False,
        "answer_mode": active.answer_mode,
        "target_role": "",
    }
    st.session_state.interview_session_id = active.id
    st.session_state.interview_started = True
    st.session_state.current_question_index = max(0, min(active.current_question_index, len(questions)))
    st.session_state.interview_answers = [item["answer"] for _, item in answered]
    st.session_state.interview_evaluations = [item["evaluation"] for _, item in answered]
    st.session_state.interview_answered_questions = [question for question, _ in answered]
    st.session_state.interview_report = None
    st.session_state.interview_completion_saved = False
    st.session_state.answer_mode = st.session_state.interview_config["answer_mode"]
    st.session_state.show_evaluation = True
    st.session_state.question_start_time = time.time()
    st.toast("Practice session restored.", icon=":material/check:")


def _persist_progress(total_questions: int) -> None:
    interview_id = st.session_state.get("interview_session_id")
    if not interview_id:
        return
    try:
        interview_service.save_progress(
            interview_id=int(interview_id),
            session_token=_workspace_token(),
            actor_user_id=st.session_state.get("user_id"),
            current_question_index=int(st.session_state.get("current_question_index", 0)),
            total_questions=total_questions,
        )
    except AuthError as exc:
        error_state("Progress could not be saved", str(exc))


def _answer_hints(answer: str, question) -> tuple[int, int, int]:
    words = len(re.findall(r"\b\w+\b", answer or ""))
    points = [
        str(point).strip()
        for point in (getattr(question, "expected_key_points", None) or [])
        if str(point).strip()
    ]
    lowered = (answer or "").lower()
    covered = sum(1 for point in points if point.lower() in lowered)
    return words, covered, len(points)


def _analysis_context() -> tuple[ResumeAnalysisResult, JobAnalysisResult, ResumeJobMatchResult]:
    resume = st.session_state.get("resume_analysis") or ResumeAnalysisResult()
    job = st.session_state.get("job_analysis") or JobAnalysisResult()
    match = st.session_state.get("match_analysis") or ResumeJobMatchResult(
        technical_skills_match=0.0,
        experience_match=0.0,
        projects_match=0.0,
        education_match=0.0,
        overall_match=0.0,
    )
    return resume, job, match


def _preferred_interview_type(categories: object) -> str | None:
    values = categories
    if isinstance(values, str):
        try:
            parsed = json.loads(values)
        except (TypeError, ValueError):
            parsed = []
        values = parsed if isinstance(parsed, (list, tuple)) else []
    if not isinstance(values, (list, tuple, set)):
        return None
    for value in values:
        text = str(value).strip().lower()
        if not text:
            continue
        for option in INTERVIEW_TYPES:
            if text == option.lower() or text in option.lower():
                return option
    return None


def _interview_defaults() -> dict:
    defaults = {
        "interview_type": "Full Interview",
        "difficulty": "medium",
        "voice_enabled": True,
        "default_role": "",
    }
    cached = st.session_state.get("user_preferences") or {}
    if cached:
        defaults["interview_type"] = cached.get(
            "default_interview_type", defaults["interview_type"]
        )
        defaults["difficulty"] = cached.get("default_difficulty", defaults["difficulty"])
        defaults["voice_enabled"] = cached.get("voice_enabled", defaults["voice_enabled"])
        defaults["default_role"] = cached.get("default_role", defaults["default_role"])
        preferred = _preferred_interview_type(cached.get("preferred_interview_categories"))
        if preferred:
            defaults["interview_type"] = preferred
        return defaults
    user_id = st.session_state.get("user_id")
    if not user_id:
        return defaults
    with get_db_context() as db:
        row = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
        if row:
            defaults["interview_type"] = row.default_interview_type or defaults["interview_type"]
            defaults["difficulty"] = row.default_difficulty or defaults["difficulty"]
            defaults["voice_enabled"] = bool(row.voice_enabled)
            defaults["default_role"] = row.default_role or ""
            preferred = _preferred_interview_type(
                getattr(row, "preferred_interview_categories", [])
            )
            if preferred:
                defaults["interview_type"] = preferred
    return defaults


def show_interview_setup_page() -> None:
    page_header(
        "AI mock interview",
        "Configure a personalized, adaptive practice session",
        ":material/forum:",
    )

    resume_ready = bool(st.session_state.get("resume_analysis"))
    job_ready = bool(st.session_state.get("job_analysis"))
    _render_resume_banner()

    defaults = _interview_defaults()
    st.session_state.setdefault("interview_target_role", defaults["default_role"])
    with st.container(border=True):
        st.markdown("**Session configuration**")
        col1, col2 = st.columns(2)
        with col1:
            interview_type = st.selectbox(
                "Interview type",
                INTERVIEW_TYPES,
                index=(
                    INTERVIEW_TYPES.index(defaults["interview_type"])
                    if defaults["interview_type"] in INTERVIEW_TYPES
                    else 0
                ),
            )
            target_role = st.text_input(
                "Target role",
                key="interview_target_role",
                max_chars=100,
                placeholder="e.g. Machine learning engineer",
            )
            difficulty = st.segmented_control(
                "Difficulty", ["easy", "medium", "hard"], default=defaults["difficulty"]
            )
            answer_mode = st.segmented_control(
                "Answer mode",
                ["text", "voice"],
                default="voice" if defaults["voice_enabled"] else "text",
            )
        with col2:
            num_questions = st.slider("Number of questions", 5, 15, 5, step=1)
            adaptive = st.toggle("Generate adaptive follow-up questions", value=True)
            read_aloud = st.toggle("AI interviewer reads questions aloud", value=False)

    with st.container(border=True):
        st.markdown("**Prerequisites**")
        kpi_row(
            [
                ("Resume analysis", "Ready" if resume_ready else "Missing", None),
                ("Job description", "Ready" if job_ready else "Not analyzed", None),
                ("Knowledge base", f"{_kb_chunk_count()} chunks", None),
            ]
        )
        if not resume_ready:
            st.warning("Analyze a resume first for personalized questions.", icon=":material/info:")
            if st.button("Go to resume analyzer", width="stretch"):
                go_to("resume")
        elif not job_ready:
            st.caption(
                "A job description is optional; adding one makes questions more role-specific."
            )

    if st.button(
        "Generate interview questions",
        type="primary",
        icon=":material/auto_awesome:",
        disabled=not resume_ready,
        width="stretch",
    ):
        with st.spinner("Generating personalized questions…"):
            try:
                from services.knowledge_base import get_knowledge_base

                context = get_knowledge_base(
                    st.session_state.user_session_id, st.session_state.get("user_id")
                ).build_context(
                    f"{interview_type} interview preparation for {target_role or 'the selected role'}"
                )
                resume, job, match = _analysis_context()
                questions = generate_interview_questions(
                    resume=resume,
                    job=job,
                    match=match,
                    total_questions=num_questions,
                    difficulty=difficulty or "medium",
                    knowledge_context=context,
                    interview_type=interview_type,
                )
                if not questions.questions:
                    raise RuntimeError("The model returned no questions.")
            except Exception as exc:
                error_state(
                    "Question generation failed. No placeholder questions were created.", str(exc)
                )
                st.stop()

            reset_interview_state()
            st.session_state.interview_questions = questions.questions
            st.session_state.interview_question_ids = []
            st.session_state.interview_config = {
                "type": interview_type,
                "difficulty": difficulty,
                "num_questions": num_questions,
                "categories": list(CATEGORY_LABELS),
                "adaptive": adaptive,
                "read_aloud": read_aloud,
                "answer_mode": answer_mode,
                "target_role": target_role,
            }
            st.session_state.interview_started = False
            st.session_state.current_question_index = 0
            st.session_state.interview_answers = []
            st.session_state.interview_evaluations = []
            st.session_state.interview_answered_questions = []
            st.session_state.interview_report = None
            st.session_state.interview_session_id = None
            st.session_state.interview_completion_saved = False
            st.session_state.answer_mode = answer_mode or "text"
            st.session_state.show_evaluation = True
            success = st.toast
            success("Questions ready. Review them below, then start the interview.")
            st.rerun()

    if "interview_questions" in st.session_state and st.session_state.interview_questions:
        st.markdown("#### Question preview")
        for index, question in enumerate(st.session_state.interview_questions):
            with st.expander(f"Q{index + 1} · {question.question_text[:70]}"):
                st.write(question.question_text)
                st.caption("Key points: " + ", ".join(question.expected_key_points))
        if st.button(
            "Start interview", type="primary", icon=":material/play_arrow:", width="stretch"
        ):
            st.session_state.interview_started = True
            st.session_state.current_question_index = 0
            st.session_state.interview_answers = []
            st.session_state.interview_evaluations = []
            st.session_state.interview_answered_questions = []
            st.session_state.interview_report = None
            st.session_state.interview_session_id = None
            st.session_state.interview_completion_saved = False
            st.session_state.question_start_time = time.time()
            create_interview_session(st.session_state.interview_questions)
            st.rerun()


def _kb_chunk_count() -> int:
    try:
        from services.knowledge_base import get_knowledge_base

        return get_knowledge_base(
            st.session_state.user_session_id, st.session_state.get("user_id")
        ).stats()["chunks"]
    except Exception:
        return 0


def show_interview_page() -> None:
    page_header(
        "Mock interview", "Answer questions and receive structured AI feedback", ":material/mic:"
    )
    if not st.session_state.get("interview_questions"):
        empty_state(
            "No interview session is prepared",
            "Choose your settings and generate a question set to begin.",
        )
        if st.button("Set up an interview", type="primary", width="stretch"):
            go_to("interview_setup")
        return

    session_id = st.session_state.get("interview_session_id")
    if session_id:
        try:
            detail = interview_service.get_interview_detail(
                interview_id=int(session_id),
                session_token=_workspace_token(),
                actor_user_id=st.session_state.get("user_id"),
            )
        except AuthError as exc:
            error_state("This interview is not accessible", str(exc))
            reset_interview_state()
            return
        if detail.session["status"] == "completed":
            st.toast("This interview is already complete.", icon=":material/check:")
            st.session_state.current_question_index = len(st.session_state.interview_questions)
        else:
            st.session_state.current_question_index = max(
                0,
                min(
                    int(st.session_state.get("current_question_index", 0)),
                    len(st.session_state.interview_questions),
                ),
            )

    config = st.session_state.get("interview_config", {})
    st.session_state.answer_mode = config.get("answer_mode", "text")
    st.session_state.show_evaluation = True
    if "question_start_time" not in st.session_state:
        st.session_state.question_start_time = time.time()
    show_interview_session(st.session_state.interview_questions)


def show_interview_session(questions: list) -> None:
    idx = st.session_state.current_question_index
    if idx >= len(questions):
        show_interview_complete(questions)
        return

    question = questions[idx]
    config = st.session_state.get("interview_config", {})
    if "question_start_time" not in st.session_state:
        st.session_state.question_start_time = time.time()

    st.progress((idx + 1) / len(questions), text=f"Question {idx + 1} of {len(questions)}")
    elapsed = int(time.time() - st.session_state.question_start_time)
    st.caption(f"Time on this question: {elapsed // 60:02d}:{elapsed % 60:02d}")

    with st.container(border=True):
        st.markdown(
            f"**Q{idx + 1} · {CATEGORY_LABELS.get(question.category.value, question.category.value)} "
            f"· {question.difficulty.value.title()}**"
        )
        st.markdown(f"### {question.question_text}")
        if config.get("read_aloud"):
            speak_text(question.question_text)
        if question.expected_key_points:
            st.caption("Expected key points: " + ", ".join(question.expected_key_points))

    if f"voice_audio_path_{idx}" not in st.session_state:
        st.session_state[f"voice_audio_path_{idx}"] = None
    if st.session_state.answer_mode == "voice":
        st.caption(
            "Record your answer with microphone permission, or type it below if unavailable."
        )
        transcript_key = f"transcript_{idx}"
        hash_key = f"voice_audio_hash_{idx}"
        if transcript_key not in st.session_state:
            st.session_state[transcript_key] = ""
        audio_file = st.audio_input("Record your answer", key=f"voice_answer_{idx}")
        if audio_file:
            try:
                payload = audio_file.getvalue()
                current_hash = hashlib.sha256(payload).hexdigest()
                existing_path = st.session_state.get(f"voice_audio_path_{idx}")
                needs_transcription = (
                    st.session_state.get(hash_key) != current_hash
                    or not existing_path
                    or not Path(existing_path).exists()
                )
                if needs_transcription:
                    audio_path = _save_audio(
                        payload, idx, getattr(audio_file, "type", "audio/wav")
                    )
                    st.session_state[f"voice_audio_path_{idx}"] = str(audio_path)
                    with st.spinner("Transcribing your answer…"):
                        st.session_state[transcript_key] = transcribe_audio(str(audio_path)).text
                    st.session_state[hash_key] = current_hash
            except Exception as exc:
                error_state("Could not process the recording. Type your answer below.", str(exc))
                st.session_state[f"voice_audio_path_{idx}"] = None
        answer = st.text_area(
            "Transcript (editable)",
            key=transcript_key,
            height=160,
            placeholder="Speech transcription appears here; you can type your answer instead…",
        )
        st.caption(
            f"Speech recognition: faster-whisper ({settings.whisper_model}). Chrome and Edge are supported."
        )
    else:
        answer = st.text_area(
            "Your answer", height=180, key=f"answer_{idx}", placeholder="Type your answer…"
        )

    answer_key = f"transcript_{idx}" if st.session_state.answer_mode == "voice" else f"answer_{idx}"
    words, covered, total_points = _answer_hints(st.session_state.get(answer_key, ""), question)
    st.caption(f"Live check: {words} words · Expected points covered: {covered}/{total_points}")

    action_col, skip_col = st.columns([3, 1])
    with action_col:
        if st.button(
            "Submit answer",
            type="primary",
            icon=":material/send:",
            disabled=not answer.strip(),
            width="stretch",
        ):
            _submit_answer(idx, question, answer)
    with skip_col:
        if st.button("Skip", icon=":material/skip_next:", width="stretch"):
            st.session_state.interview_answers.append("[SKIPPED]")
            st.session_state.interview_evaluations.append(None)
            st.session_state.interview_answered_questions.append(question)
            save_question_answer(idx, question, "[SKIPPED]", None, "text")
            st.session_state.current_question_index += 1
            st.session_state.question_start_time = time.time()
            _persist_progress(len(questions))
            st.rerun()

    if st.button("End interview early", icon=":material/stop_circle:"):
        st.session_state.current_question_index = len(questions)
        _persist_progress(len(questions))
        st.toast("Interview saved. You can continue it from history.", icon=":material/check:")
        st.rerun()


def _save_audio(data: bytes, index: int, mime_type: str = "audio/webm") -> Path:
    token = str(st.session_state.get("user_session_id", "anon"))
    safe_token = "".join(char for char in token if char.isalnum() or char in "-_")[:80] or "anon"
    directory = runtime_path("data/uploads/audio") / safe_token
    directory.mkdir(parents=True, exist_ok=True)
    suffix = {
        "audio/webm": ".webm",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/mp4": ".m4a",
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
    }.get(mime_type.split(";")[0].strip().lower(), ".webm")
    path = directory / f"q{index + 1}_{uuid.uuid4().hex[:8]}{suffix}"
    path.write_bytes(data)
    if not validate_mime_type(str(path), suffix):
        path.unlink(missing_ok=True)
        raise ValueError(f"Rejected audio MIME type: {suffix}")
    return path


def _answer_modes(question) -> tuple[str, str]:
    evaluation_mode = (
        "behavioral" if getattr(question.category, "value", "") == "behavioral" else "standard"
    )
    answer_mode = "voice" if st.session_state.get("answer_mode") == "voice" else "text"
    return evaluation_mode, answer_mode


def _submit_answer(idx: int, question, answer: str) -> None:
    evaluation_mode, answer_mode = _answer_modes(question)
    resume, job, _ = _analysis_context()
    try:
        with st.spinner("Evaluating your answer…"):
            evaluation = evaluate_answer(
                question=question,
                answer=answer,
                resume=resume,
                job=job,
                mode=evaluation_mode,
            )
    except Exception as exc:
        error_state("Unable to evaluate this answer right now. Please try again.", str(exc))
        return

    st.session_state.interview_answers.append(answer)
    st.session_state.interview_evaluations.append(evaluation)
    st.session_state.interview_answered_questions.append(question)
    save_question_answer(idx, question, answer, evaluation, answer_mode)
    st.toast("Answer saved.", icon=":material/check:")

    if st.session_state.get("show_evaluation", True):
        show_evaluation_result(evaluation)
        if st.button("Continue to next question", type="primary", width="stretch"):
            _advance(idx, question)
    else:
        _advance(idx, question)


def _advance(idx: int, question) -> None:
    if st.session_state.get("interview_config", {}).get("adaptive") and question.follow_up_hints:
        resume, job, _ = _analysis_context()
        try:
            with st.spinner("Preparing a follow-up…"):
                follow_up = InterviewGenerator().generate_follow_up(
                    previous_question=question,
                    candidate_answer=st.session_state.interview_answers[-1],
                    resume=resume,
                    job=job,
                )
                follow_up.question_number = len(st.session_state.interview_questions) + 1
                question_id = interview_service.add_follow_up_question(
                    interview_id=int(st.session_state.interview_session_id),
                    session_token=_workspace_token(),
                    actor_user_id=st.session_state.get("user_id"),
                    question=follow_up,
                    question_number=follow_up.question_number,
                )
                st.session_state.interview_questions.insert(idx + 1, follow_up)
                st.session_state.interview_question_ids.insert(idx + 1, question_id)
                st.toast("A follow-up question was added.", icon=":material/question_answer:")
        except Exception as exc:
            st.toast(f"Follow-up unavailable: {exc}", icon=":material/info:")
    st.session_state.current_question_index += 1
    st.session_state.question_start_time = time.time()
    _persist_progress(len(st.session_state.interview_questions))
    st.rerun()


def show_evaluation_result(evaluation) -> None:
    section("Answer evaluation", ":material/analytics:")
    st.caption(
        "AI-generated advisory scores based on the configured rubric — not objective ground truth."
    )
    render_score_chart(evaluation.dimension_scores(), "AI-generated answer rubric scores")
    st.metric("Overall score", f"{evaluation.overall_score:.1f}/10", border=True)

    tabs = st.tabs(
        ["Strengths", "Improvements", "Missing concepts", "Ideal answer", "Communication"]
    )
    with tabs[0]:
        for item in evaluation.strengths:
            st.markdown(f"- {item}")
    with tabs[1]:
        for item in evaluation.weaknesses + evaluation.incorrect_points:
            st.markdown(f"- {item}")
    with tabs[2]:
        for item in evaluation.missing_points:
            st.markdown(f"- {item}")
    with tabs[3]:
        st.markdown("**Improved answer**")
        st.write(evaluation.suggested_answer)
        if evaluation.ideal_answer:
            st.markdown("**Ideal answer**")
            st.write(evaluation.ideal_answer)
    with tabs[4]:
        st.write(evaluation.communication_feedback)
        st.write(evaluation.technical_feedback)


def show_interview_complete(questions: list) -> None:
    page_header("Interview complete", "Your answers have been saved", ":material/emoji_events:")
    answered_questions = st.session_state.get("interview_answered_questions", [])
    answers = st.session_state.get("interview_answers", [])
    evaluations = st.session_state.get("interview_evaluations", [])
    records = list(zip(answered_questions, answers, evaluations))
    scored = [evaluation for _, _, evaluation in records if evaluation is not None]
    pairs = [
        (question, evaluation)
        for question, _, evaluation in records
        if evaluation is not None
    ]

    kpi_row(
        [
            ("Answered", len(answers), None),
            ("Skipped", len(records) - len(scored), None),
            (
                "Average score",
                (
                    f"{sum(e.overall_score for e in scored) / len(scored):.1f}/10"
                    if scored
                    else "—"
                ),
                None,
            ),
        ]
    )

    report = st.session_state.get("interview_report")
    if report is None and pairs:
        resume, job, _ = _analysis_context()
        with st.spinner("Generating your final report…"):
            try:
                report = generate_interview_report(
                    questions=[question for question, _ in pairs],
                    evaluations=[evaluation for _, evaluation in pairs],
                    resume=resume,
                    job=job,
                )
                st.session_state.interview_report = report
            except Exception as exc:
                error_state(
                    "The full report could not be generated; individual evaluations are saved.",
                    str(exc),
                )

    save_interview_completion(report, questions, evaluations)
    st.success("Interview results saved to your private workspace.", icon=":material/check:")
    if report:
        section("Report summary", ":material/summarize:")
        st.metric("Overall", f"{report.overall_score:.1f}/10", border=True)
        st.write("**Strengths:** " + ", ".join(report.strengths[:5]))
        st.write("**Focus areas:** " + ", ".join(report.weaknesses[:5]))
    else:
        empty_state(
            "No overall evaluation yet",
            "Individual answers are saved. Review them in Interview history.",
        )

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("View full report", icon=":material/assessment:", width="stretch"):
            go_to("report")
    with col2:
        if st.button("Build learning roadmap", icon=":material/route:", width="stretch"):
            go_to("roadmap")
    with col3:
        if st.button("Practice again", icon=":material/replay:", width="stretch"):
            reset_interview_state()
            go_to("interview_setup")


def _ensure_user_session() -> int:
    token = st.session_state.get("user_session_id")
    if not token:
        token = str(uuid.uuid4())
        st.session_state.user_session_id = token
    user_id = st.session_state.get("user_id")
    if user_id is not None:
        owned = owned_session_id(token, user_id)
        if owned is not None:
            return owned
    with get_db_context() as db:
        session = db.query(UserSession).filter(UserSession.session_id == token).first()
        if not session:
            session = UserSession(session_id=token, user_id=user_id)
            db.add(session)
            db.flush()
        elif user_id is not None and session.user_id != user_id:
            raise PermissionError("Workspace does not belong to the current account.")
        return cast(int, session.id)


def create_interview_session(questions: list) -> None:
    _ensure_user_session()
    config = st.session_state.get("interview_config", {})
    started = time.time()
    try:
        interview_id, question_ids = interview_service.start_interview(
            session_token=_workspace_token(),
            actor_user_id=st.session_state.get("user_id"),
            questions=questions,
            interview_type=str(config.get("type", "Full Interview")),
            difficulty=str(config.get("difficulty", "medium")),
            answer_mode=str(config.get("answer_mode", "text")),
            total_questions=int(config.get("num_questions", len(questions))),
        )
    except (AuthError, ValueError) as exc:
        error_state("The interview could not be started", str(exc))
        st.stop()
    except Exception as exc:
        error_state("The interview could not be started", str(exc))
        st.stop()
    st.session_state.interview_session_id = interview_id
    st.session_state.interview_question_ids = question_ids
    st.session_state.question_start_time = started


def save_question_answer(
    idx: int,
    question,
    answer: str,
    evaluation: AnswerEvaluation | None,
    answer_mode: str = "text",
) -> None:
    interview_id = st.session_state.get("interview_session_id")
    if not interview_id:
        return
    question_ids = list(st.session_state.get("interview_question_ids", []))
    try:
        if idx >= len(question_ids):
            question_id = interview_service.add_follow_up_question(
                interview_id=int(interview_id),
                session_token=_workspace_token(),
                actor_user_id=st.session_state.get("user_id"),
                question=question,
                question_number=idx + 1,
            )
            question_ids.insert(idx, question_id)
            st.session_state.interview_question_ids = question_ids
        else:
            question_id = question_ids[idx]
        audio_path = st.session_state.get(f"voice_audio_path_{idx}")
        if answer_mode != "voice" or not audio_path or not Path(str(audio_path)).exists():
            audio_path = None
        interview_service.save_answer(
            interview_id=int(interview_id),
            session_token=_workspace_token(),
            actor_user_id=st.session_state.get("user_id"),
            question_id=question_id,
            answer=answer,
            evaluation=evaluation,
            answer_mode=answer_mode,
            audio_path=str(audio_path) if audio_path else None,
            current_question_index=idx,
            total_questions=len(st.session_state.get("interview_questions", [])),
        )
    except AuthError as exc:
        error_state("Answer could not be saved", str(exc))


def save_interview_completion(report, questions: list, evaluations: list) -> None:
    interview_id = st.session_state.get("interview_session_id")
    if not interview_id:
        return
    try:
        interview_service.complete_interview(
            interview_id=int(interview_id),
            session_token=_workspace_token(),
            actor_user_id=st.session_state.get("user_id"),
            report=report,
            current_question_index=int(st.session_state.get("current_question_index", 0)),
            total_questions=len(st.session_state.get("interview_questions", questions)),
        )
    except AuthError as exc:
        error_state("Interview results could not be saved", str(exc))
        return
    st.session_state.interview_completion_saved = True


def reset_interview_state() -> None:
    for key in (
        "interview_started",
        "current_question_index",
        "interview_answers",
        "interview_evaluations",
        "interview_answered_questions",
        "interview_session_id",
        "interview_question_ids",
        "interview_questions",
        "interview_config",
        "interview_report",
        "question_start_time",
        "interview_completion_saved",
        "answer_mode",
        "show_evaluation",
    ):
        st.session_state.pop(key, None)
    prefixes = (
        "answer_",
        "transcript_",
        "voice_answer_",
        "voice_audio_hash_",
        "voice_audio_path_",
    )
    for key in [key for key in st.session_state if key.startswith(prefixes)]:
        st.session_state.pop(key, None)
