import json
from datetime import datetime

import streamlit as st
from config.settings import settings
from services import ResumeAnalysisResult, ResumeScores, analyze_resume
from services.auth_service import AuthError
from services.career_service import (
    CareerValidationError,
    PreparedDocument,
    document_fingerprint,
    friendly_error,
    list_resume_history,
    load_resume_record,
    prepare_document_upload,
    save_resume_analysis,
    workspace_id,
)
from services.knowledge_base import get_knowledge_base
from services.preferences_service import get_user_preferences
from services.report_generator import render_career_pdf
from services.resume_insights import ResumeQuality, analyze_resume_quality
from utils.file_utils import format_file_size

from frontend.shared.components import (
    empty_state,
    error_state,
    kpi_row,
    page_header,
    render_score_chart,
    success_toast,
    warning_toast,
)
from frontend.shared.navigation import go_to


def show_resume_page() -> None:
    page_header(
        "Resume analysis",
        "Extract your resume, review structured findings, and save each unique version privately.",
        ":material/description:",
    )
    workspace = _workspace_id()
    if workspace is None:
        error_state("Sign in to analyse a resume.")
        return

    analyze_tab, history_tab = st.tabs(["New analysis", "Saved analyses"])
    with analyze_tab:
        _render_analysis(workspace)
    with history_tab:
        _render_history(workspace)


def _workspace_id() -> int | None:
    token = st.session_state.get("user_session_id")
    user_id = st.session_state.get("user_id")
    if not token or not user_id:
        return None
    try:
        return workspace_id(str(token), int(user_id))
    except AuthError:
        return None


def _target_role() -> str:
    user_id = st.session_state.get("user_id")
    if not user_id:
        return ""
    preferences = get_user_preferences(int(user_id), int(user_id))
    return str(preferences.get("default_role") or "")


def _format_saved_at(value: datetime | None) -> str:
    return value.strftime("%d %b %Y, %H:%M") if value else "Unknown"


def _clear_current_results() -> None:
    for key in (
        "resume_analysis",
        "resume_scores",
        "resume_text",
        "resume_quality",
        "resume_record_id",
        "match_analysis",
        "skill_gap_analysis",
    ):
        st.session_state.pop(key, None)


def _render_analysis(workspace: int) -> None:
    uploaded_file = st.file_uploader(
        "Upload resume",
        type=["pdf", "docx", "txt"],
        help=f"PDF, DOCX or TXT, up to {settings.max_file_size_mb} MB",
        key="resume_upload",
    )

    if uploaded_file is None:
        st.session_state.pop("resume_prepared", None)
        st.session_state.pop("resume_upload_fingerprint", None)
    else:
        try:
            fingerprint = document_fingerprint(uploaded_file)
        except CareerValidationError as exc:
            error_state(str(exc))
            return
        if st.session_state.get("resume_upload_fingerprint") != fingerprint:
            with st.status("Validating and extracting document", expanded=False) as status:
                status.write("Checking file type, size and content")
                try:
                    prepared = prepare_document_upload(uploaded_file, "data/uploads/resumes")
                except Exception as exc:
                    status.update(label="Document rejected", state="error")
                    error_state(friendly_error(exc, "Upload"))
                    return
                status.write("Text extracted")
                status.update(label="Document ready", state="complete")
            st.session_state.resume_prepared = prepared
            st.session_state.resume_upload_fingerprint = fingerprint
            _clear_current_results()

    prepared = st.session_state.get("resume_prepared")
    if not isinstance(prepared, PreparedDocument):
        empty_state(
            "No resume selected",
            "Upload a PDF, DOCX or TXT file to begin. Your document is saved only after validation.",
        )
        return

    with st.container(border=True):
        st.markdown(f"**{prepared.file_name}**")
        st.caption(
            f"{len(prepared.text):,} characters extracted · SHA-256 {prepared.file_hash[:12]}…"
        )
        with st.expander("Extracted text preview"):
            st.text_area(
                "Extracted text",
                prepared.text[:4000] + ("…" if len(prepared.text) > 4000 else ""),
                height=220,
                label_visibility="collapsed",
                disabled=True,
            )

    if st.button("Analyse resume", type="primary", icon=":material/auto_awesome:", width="stretch"):
        _run_analysis(workspace, prepared)

    if st.session_state.get("resume_analysis") is not None:
        _render_results()


def _run_analysis(workspace: int, prepared: PreparedDocument) -> None:
    with st.status("Analysing resume with AI", expanded=True) as status:
        try:
            status.write("Analysing content and rubric scores")
            analysis, scores = analyze_resume(prepared.text)
            if _is_empty_result(analysis, scores):
                raise RuntimeError("The AI provider returned no usable analysis.")
            status.write("Running deterministic ATS checks")
            quality = analyze_resume_quality(
                analysis,
                scores,
                prepared.text,
                target_role=_target_role(),
                job=st.session_state.get("job_analysis"),
            )
            status.write("Saving private analysis")
            record_id = save_resume_analysis(workspace, _actor_id(), analysis, scores, prepared)
            status.write("Indexing resume for later questions")
            try:
                _index_resume(prepared)
            except Exception as exc:
                warning_toast(f"Analysis saved, but indexing failed: {exc}")
            status.update(label="Analysis complete", state="complete")
        except Exception as exc:
            status.update(label="Analysis failed", state="error")
            error_state(friendly_error(exc, "Resume analysis"))
            return

    st.session_state.resume_analysis = analysis
    st.session_state.resume_scores = scores
    st.session_state.resume_text = prepared.text
    st.session_state.resume_quality = quality
    st.session_state.resume_record_id = record_id
    st.session_state.resume_file_path = prepared.file_path
    st.session_state.resume_file_hash = prepared.file_hash
    st.session_state.resume_file_name = prepared.file_name
    st.session_state.pop("match_analysis", None)
    st.session_state.pop("skill_gap_analysis", None)
    success_toast("Resume analysis saved.")
    st.rerun()


def _index_resume(prepared: PreparedDocument) -> None:
    token = st.session_state.get("user_session_id")
    user_id = st.session_state.get("user_id")
    if not token or not user_id:
        return
    knowledge_base = get_knowledge_base(str(token), int(user_id))
    knowledge_base.add_text(
        prepared.text,
        display_name=prepared.file_name,
        source_type="resume",
    )


def _actor_id() -> int | None:
    user_id = st.session_state.get("user_id")
    return int(user_id) if user_id else None


def _is_empty_result(analysis: ResumeAnalysisResult, scores: ResumeScores) -> bool:
    return (
        scores.overall == 0
        and scores.technical_skills == 0
        and scores.experience == 0
        and scores.projects == 0
        and scores.education == 0
        and scores.certifications == 0
        and scores.structure == 0
        and not analysis.candidate_name
        and not analysis.email
        and not analysis.skills
    )


def _render_results() -> None:
    analysis: ResumeAnalysisResult = st.session_state.resume_analysis
    scores: ResumeScores = st.session_state.resume_scores
    quality: ResumeQuality | None = st.session_state.get("resume_quality")

    if quality is None:
        quality = analyze_resume_quality(
            analysis,
            scores,
            str(st.session_state.get("resume_text") or ""),
            target_role=_target_role(),
            job=st.session_state.get("job_analysis"),
        )
        st.session_state.resume_quality = quality

    st.divider()
    section_heading("Analysis overview")
    kpi_row(
        [
            ("Overall score", f"{scores.overall:.0f}/100", _score_label(scores.overall)),
            ("ATS readiness", f"{quality.ats_score:.0f}/100", _score_label(quality.ats_score)),
            ("Keyword coverage", f"{quality.keyword_coverage:.0f}%", "target aware"),
            ("Quantified points", quality.quantified_points, "evidence"),
        ]
    )
    st.progress(
        max(0.0, min(1.0, scores.overall / 100)),
        text=f"Overall resume score: {scores.overall:.0f}/100",
    )

    with st.expander("Rubric score breakdown", expanded=False):
        render_score_chart(
            {
                "Technical skills": scores.technical_skills,
                "Experience": scores.experience,
                "Projects": scores.projects,
                "Education": scores.education,
                "Certifications": scores.certifications,
                "Structure": scores.structure,
                "Overall": scores.overall,
            },
            "AI-generated resume rubric scores from the current analysis",
        )

    with st.expander("Profile and contact", expanded=True):
        _render_profile(analysis)

    with st.expander("Skills"):
        _render_skills(analysis)

    with st.expander("Experience and projects"):
        left, right = st.columns(2)
        with left:
            _render_experience(analysis)
        with right:
            _render_projects(analysis)

    with st.expander("Education and certifications"):
        left, right = st.columns(2)
        with left:
            _render_education(analysis)
        with right:
            _render_certifications(analysis)

    with st.expander("Strengths and weaknesses"):
        _render_feedback(analysis)

    with st.expander("ATS checks and suggestions", expanded=True):
        _render_quality(quality)

    st.divider()
    section_heading("Export and continue")
    export = _report_payload()
    left, right = st.columns(2)
    with left:
        st.download_button(
            "Download JSON",
            data=json.dumps(export, indent=2, default=str),
            file_name="resume-analysis.json",
            mime="application/json",
            icon=":material/download:",
            width="stretch",
        )
    with right:
        st.download_button(
            "Download PDF report",
            data=render_career_pdf(export),
            file_name="resume-analysis.pdf",
            mime="application/pdf",
            icon=":material/picture_as_pdf:",
            width="stretch",
        )
    if st.button(
        "Match this resume with a job",
        icon=":material/work:",
        type="primary",
        width="stretch",
    ):
        go_to("job")


def _report_payload() -> dict:
    analysis: ResumeAnalysisResult = st.session_state.resume_analysis
    scores: ResumeScores = st.session_state.resume_scores
    quality: ResumeQuality = st.session_state.resume_quality
    return {
        "title": "Resume analysis report",
        "subtitle": f"{analysis.candidate_name or 'Candidate'} · {_target_role() or 'General target'}",
        "scores": {
            "Overall": f"{scores.overall:.0f}/100",
            "ATS readiness": f"{quality.ats_score:.0f}/100",
            "Keyword coverage": f"{quality.keyword_coverage:.0f}%",
            "Technical skills": f"{scores.technical_skills:.0f}/100",
            "Experience": f"{scores.experience:.0f}/100",
            "Projects": f"{scores.projects:.0f}/100",
            "Education": f"{scores.education:.0f}/100",
            "Structure": f"{scores.structure:.0f}/100",
        },
        "sections": [
            {
                "heading": "Extracted skills",
                "items": analysis.skills
                + analysis.programming_languages
                + analysis.frameworks
                + analysis.databases
                + analysis.tools,
            },
            {
                "heading": "Strengths",
                "items": analysis.strengths,
            },
            {
                "heading": "Weaknesses and missing information",
                "items": analysis.weaknesses + quality.suggestions,
            },
            {
                "heading": "Experience",
                "items": [
                    {
                        "title": item.title,
                        "detail": ", ".join(
                            part for part in (item.company, item.duration, item.description) if part
                        ),
                    }
                    for item in analysis.experience
                ],
            },
        ],
    }


def _render_history(workspace: int) -> None:
    section_heading("Saved resume versions")
    records = list_resume_history(workspace, _actor_id())
    if not records:
        empty_state("No saved analyses", "Analysed resumes will appear here with their scores.")
        return

    selected = st.selectbox(
        "Saved resume",
        options=records,
        format_func=lambda item: f"{_format_saved_at(item.created_at)} · {item.label}",
        key="resume_history_select",
    )
    details = load_resume_record(selected.id, workspace, _actor_id())
    kpi_row(
        [
            ("File", details.file_name, "saved version"),
            ("Overall score", f"{details.scores.overall:.0f}/100", _score_label(details.scores.overall)),
            ("Saved", _format_saved_at(details.created_at), "local workspace"),
            ("Roles", len(details.analysis.experience), "extracted"),
        ]
    )
    with st.expander("Saved analysis details"):
        _render_profile(details.analysis)
        _render_skills(details.analysis)
        _render_experience(details.analysis)

    if st.button(
        "Load into workspace",
        icon=":material/upload_file:",
        type="primary",
        width="stretch",
    ):
        quality = analyze_resume_quality(
            details.analysis,
            details.scores,
            details.raw_text,
            target_role=_target_role(),
            job=st.session_state.get("job_analysis"),
        )
        st.session_state.resume_analysis = details.analysis
        st.session_state.resume_scores = details.scores
        st.session_state.resume_text = details.raw_text
        st.session_state.resume_quality = quality
        st.session_state.resume_record_id = details.id
        st.session_state.resume_file_name = details.file_name
        st.session_state.pop("match_analysis", None)
        st.session_state.pop("skill_gap_analysis", None)
        success_toast("Saved analysis loaded.")
        st.rerun()


def section_heading(text: str) -> None:
    st.subheader(text)


def _score_label(score: float) -> str:
    if score >= 85:
        return "Excellent"
    if score >= 70:
        return "Strong"
    if score >= 50:
        return "Developing"
    return "Needs work"


def _render_profile(analysis: ResumeAnalysisResult) -> None:
    values = {
        "Name": analysis.candidate_name,
        "Email": analysis.email,
        "Phone": analysis.phone,
        "LinkedIn": analysis.linkedin,
        "GitHub": analysis.github,
        "Portfolio": analysis.portfolio,
    }
    for start in range(0, len(values), 3):
        columns = st.columns(3)
        for column, (label, value) in zip(columns, list(values.items())[start : start + 3]):
            with column:
                st.markdown(f"**{label}**")
                st.write(value or "Not found")


def _render_skills(analysis: ResumeAnalysisResult) -> None:
    rows = [
        {"Category": label, "Skills": ", ".join(values) if values else "None found"}
        for label, values in (
            ("All skills", analysis.skills),
            ("Programming languages", analysis.programming_languages),
            ("Frameworks", analysis.frameworks),
            ("Databases", analysis.databases),
            ("Tools", analysis.tools),
        )
    ]
    st.dataframe(rows, hide_index=True, width="stretch", alt="Extracted resume skills by category")


def _render_experience(analysis: ResumeAnalysisResult) -> None:
    if not analysis.experience:
        empty_state("No work experience found")
        return
    for item in analysis.experience:
        with st.container(border=True):
            st.markdown(f"**{item.title or 'Role'}**")
            st.caption(", ".join(part for part in (item.company, item.duration) if part) or "No details")
            if item.description:
                st.write(item.description)
            if item.technologies:
                st.caption("Technologies: " + ", ".join(item.technologies))


def _render_projects(analysis: ResumeAnalysisResult) -> None:
    if not analysis.projects:
        empty_state("No projects found")
        return
    for item in analysis.projects:
        with st.container(border=True):
            st.markdown(f"**{item.name or 'Project'}**")
            if item.description:
                st.write(item.description)
            if item.technologies:
                st.caption("Technologies: " + ", ".join(item.technologies))


def _render_education(analysis: ResumeAnalysisResult) -> None:
    if not analysis.education:
        empty_state("No education found")
        return
    for item in analysis.education:
        with st.container(border=True):
            st.markdown(f"**{item.degree or 'Qualification'}**")
            st.caption(", ".join(part for part in (item.institution, item.year, item.grade) if part))
            if item.details:
                st.write(item.details)


def _render_certifications(analysis: ResumeAnalysisResult) -> None:
    if analysis.certifications:
        for value in analysis.certifications:
            st.markdown(f"- {value}")
    else:
        empty_state("No certifications found")


def _render_feedback(analysis: ResumeAnalysisResult) -> None:
    left, right = st.columns(2)
    with left:
        st.markdown("**Strengths**")
        if analysis.strengths:
            for value in analysis.strengths:
                st.markdown(f"- {value}")
        else:
            empty_state("No strengths extracted")
    with right:
        st.markdown("**Weaknesses**")
        if analysis.weaknesses:
            for value in analysis.weaknesses:
                st.markdown(f"- {value}")
        else:
            empty_state("No weaknesses extracted")


def _render_quality(quality: ResumeQuality) -> None:
    rows = [
        {"Section": name, "Status": "Found" if present else "Missing"}
        for name, present in quality.section_checks.items()
    ]
    st.dataframe(rows, hide_index=True, width="stretch", alt="Resume section checks")
    if quality.missing_keywords:
        st.markdown("**Missing target keywords**")
        st.write(", ".join(quality.missing_keywords[:15]))
    if quality.suggestions:
        st.markdown("**Actionable suggestions**")
        for value in quality.suggestions:
            st.markdown(f"- {value}")
    st.caption(f"{quality.word_count:,} words analysed. Deterministic checks are advisory only.")


def _format_file_size(value: float) -> str:
    return format_file_size(value)
