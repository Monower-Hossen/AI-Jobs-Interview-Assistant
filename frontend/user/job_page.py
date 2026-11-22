import json

import streamlit as st
from services import (
    JobAnalysisResult,
    ResumeJobMatchResult,
    analyze_job_description,
    match_resume_job,
)
from services.auth_service import AuthError
from services.career_service import (
    CareerValidationError,
    PreparedDocument,
    SkillGapAnalysis,
    build_skill_gap,
    document_fingerprint,
    friendly_error,
    get_career_snapshot,
    list_job_history,
    list_match_history,
    list_resume_history,
    load_job_record,
    load_match_record,
    load_resume_record,
    prepare_document_upload,
    save_job_analysis,
    save_match_analysis,
    validate_job_text,
    workspace_id,
)
from services.knowledge_base import get_knowledge_base
from services.report_generator import render_career_pdf

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


def show_job_page() -> None:
    page_header(
        "Job preparation",
        "Browse published roles, practise job-based interviews, then analyse fit and skill gaps.",
        ":material/work:",
    )
    workspace = _workspace_id()
    if workspace is None:
        error_state("Sign in to explore job opportunities.")
        return
    _render_snapshot(workspace)
    resume_ready = _ensure_resume(workspace)

    browse_tab, analyze_tab, match_tab, jobs_tab, matches_tab = st.tabs(
        [
            "Available jobs",
            "Analyse job",
            "Match and skill gap",
            "Saved jobs",
            "Match history",
        ]
    )
    with browse_tab:
        _render_available_jobs()
    with analyze_tab:
        if resume_ready:
            _render_analysis(workspace)
    with match_tab:
        if resume_ready:
            _render_match(workspace)
    with jobs_tab:
        _render_job_history(workspace)
    with matches_tab:
        _render_match_history(workspace)


def _workspace_id() -> int | None:
    token = st.session_state.get("user_session_id")
    user_id = st.session_state.get("user_id")
    if not token or not user_id:
        return None
    try:
        return workspace_id(str(token), int(user_id))
    except AuthError:
        return None


def _actor_id() -> int | None:
    user_id = st.session_state.get("user_id")
    return int(user_id) if user_id else None


def _render_available_jobs() -> None:
    from services.job_post_service import list_jobs

    filter_col1, filter_col2, filter_col3 = st.columns(3)
    search = filter_col1.text_input("Search roles", key="_job_browse_search")
    skill = filter_col2.text_input("Filter by skill", key="_job_browse_skill")
    difficulty = filter_col3.selectbox(
        "Difficulty",
        options=["all", "easy", "medium", "hard"],
        key="_job_browse_difficulty",
    )
    try:
        jobs = list_jobs(
            search=search,
            skill=skill,
            difficulty="" if difficulty == "all" else difficulty,
        )
    except Exception as exc:
        error_state("Could not load published jobs.", str(exc))
        return
    if not jobs:
        empty_state(
            "No published jobs match your filters.",
            "An administrator can publish jobs from Admin Panel → Job postings.",
        )
        return
    st.caption(f"{len(jobs)} published role{'s' if len(jobs) != 1 else ''} available.")
    for job in jobs:
        with st.container(border=True):
            head_col, action_col = st.columns([4, 1])
            head_col.markdown(f"### {job.title}")
            head_col.caption(f"{job.company} • {str(job.experience_level or 'Any experience').title()}")
            head_col.markdown((job.description or "")[:400])
            skills = ", ".join((job.required_skills or [])[:8]) or "No required skills listed"
            head_col.caption(f"Required skills: {skills}")
            head_col.caption(
                f"Interview: {str(job.interview_difficulty or 'medium').title()} • "
                f"{len(job.question_preferences or [])} question categories"
            )
            if action_col.button(
                "Interview",
                icon=":material/mic:",
                key=f"_job_browse_start_{int(job.id)}",
                width="stretch",
            ):
                st.session_state["job_interview_job_id"] = int(job.id)
                go_to("job_interview")
            if action_col.button(
                "Live",
                icon=":material/videocam:",
                key=f"_job_browse_live_{int(job.id)}",
                width="stretch",
            ):
                go_to("live_webcam")
    if st.button(
        "View my job interviews", icon=":material/history:", width="stretch", key="_job_browse_history"
    ):
        go_to("job_interview")
    if st.button(
        "Open live webcam setup",
        icon=":material/videocam:",
        width="stretch",
        key="_job_browse_live_setup",
    ):
        go_to("live_webcam")


def _format_saved_at(value) -> str:
    return value.strftime("%d %b %Y, %H:%M") if value else "Unknown"


def _render_snapshot(workspace: int) -> None:
    try:
        snapshot = get_career_snapshot(workspace, _actor_id())
    except AuthError as exc:
        error_state(str(exc))
        return
    latest = f"{snapshot.latest_score:.0f}%" if snapshot.latest_score is not None else "None"
    kpi_row(
        [
            ("Saved resumes", snapshot.resume_count, "private"),
            ("Saved jobs", snapshot.job_count, "private"),
            ("Match runs", snapshot.match_count, "saved"),
            ("Latest match", latest, snapshot.latest_role),
        ]
    )


def _ensure_resume(workspace: int) -> bool:
    if st.session_state.get("resume_analysis") is not None:
        return True
    latest = load_latest_safely(workspace)
    if latest is None:
        warning_toast("Analyse or load a resume before matching jobs.")
        st.info("A saved resume is required so match scores use your real profile.")
        if st.button(
            "Go to resume analysis",
            icon=":material/description:",
            type="primary",
            width="stretch",
        ):
            go_to("resume")
        return False
    st.session_state.resume_analysis = latest.analysis
    st.session_state.resume_scores = latest.scores
    st.session_state.resume_text = latest.raw_text
    st.session_state.resume_record_id = latest.id
    st.session_state.resume_file_name = latest.file_name
    return True


def load_latest_safely(workspace: int):
    from services.career_service import load_latest_resume

    try:
        return load_latest_resume(workspace, _actor_id())
    except AuthError as exc:
        error_state(str(exc))
        return None


def _sync_job_text_input() -> None:
    st.session_state.job_text = st.session_state.job_text_input


def _set_job_text(value: str) -> None:
    st.session_state.job_text = value
    st.session_state.job_text_pending = value


def _render_analysis(workspace: int) -> None:
    pending = st.session_state.pop("job_text_pending", None)
    if pending is not None:
        st.session_state.job_text_input = pending
    elif "job_text_input" not in st.session_state:
        st.session_state.job_text_input = st.session_state.get("job_text", "")

    title = st.text_input("Job title", key="job_title", placeholder="e.g. Python backend engineer")
    st.text_area(
        "Paste job description",
        height=260,
        key="job_text_input",
        on_change=_sync_job_text_input,
        placeholder="Include responsibilities, required skills, experience and education requirements.",
    )
    st.caption("Or upload a job description document")
    uploaded_file = st.file_uploader(
        "Upload job description",
        type=["pdf", "docx", "txt"],
        key="job_upload",
    )

    if uploaded_file is None:
        st.session_state.pop("job_prepared", None)
        st.session_state.pop("job_upload_fingerprint", None)
    else:
        try:
            fingerprint = document_fingerprint(uploaded_file)
        except CareerValidationError as exc:
            error_state(str(exc))
            return
        if st.session_state.get("job_upload_fingerprint") != fingerprint:
            with st.status("Validating job description", expanded=False) as status:
                status.write("Checking file type, size and content")
                try:
                    prepared = prepare_document_upload(uploaded_file, "data/uploads/jobs")
                except Exception as exc:
                    status.update(label="Document rejected", state="error")
                    error_state(friendly_error(exc, "Upload"))
                    return
                status.write("Text extracted")
                status.update(label="Document ready", state="complete")
            st.session_state.job_prepared = prepared
            st.session_state.job_upload_fingerprint = fingerprint
            _set_job_text(prepared.text)
            st.session_state.job_source = prepared.file_name
            st.session_state.pop("match_analysis", None)
            st.session_state.pop("skill_gap_analysis", None)
            st.rerun()

    prepared = st.session_state.get("job_prepared")
    if isinstance(prepared, PreparedDocument):
        with st.container(border=True):
            st.markdown(f"**{prepared.file_name}**")
            st.caption(f"{len(prepared.text):,} characters extracted")
            with st.expander("Extracted text preview"):
                st.text_area(
                    "Extracted job description",
                    prepared.text[:4000] + ("…" if len(prepared.text) > 4000 else ""),
                    height=220,
                    label_visibility="collapsed",
                    disabled=True,
                )

    if st.button("Analyse job", type="primary", icon=":material/analytics:", width="stretch"):
        _run_job_analysis(workspace, title)

    if st.session_state.get("job_analysis") is not None:
        _render_job_results()


def _run_job_analysis(workspace: int, title_hint: str) -> None:
    raw_text = str(st.session_state.get("job_text") or "")
    source = str(st.session_state.get("job_source") or "pasted")
    prepared = st.session_state.get("job_prepared")
    if isinstance(prepared, PreparedDocument):
        raw_text = prepared.text
        source = prepared.file_name
    try:
        cleaned = validate_job_text(raw_text, title_hint)
    except CareerValidationError as exc:
        error_state(str(exc))
        return

    prompt_text = f"Job title: {title_hint.strip()}\n\n{cleaned}" if title_hint.strip() else cleaned
    with st.status("Analysing job description with AI", expanded=True) as status:
        try:
            status.write("Extracting structured requirements")
            analysis: JobAnalysisResult = analyze_job_description(prompt_text)
            if title_hint.strip() and not analysis.job_title:
                analysis.job_title = title_hint.strip()
            if _is_empty_job(analysis):
                raise RuntimeError("The AI provider returned no usable job analysis.")
            status.write("Saving private job record")
            record_id = save_job_analysis(workspace, _actor_id(), analysis, cleaned, source)
            status.write("Indexing job description for later questions")
            try:
                _index_job(cleaned, source)
            except Exception as exc:
                warning_toast(f"Analysis saved, but indexing failed: {exc}")
            status.update(label="Job analysis complete", state="complete")
        except Exception as exc:
            status.update(label="Job analysis failed", state="error")
            error_state(friendly_error(exc, "Job analysis"))
            return

    st.session_state.job_analysis = analysis
    _set_job_text(cleaned)
    st.session_state.job_source = source
    st.session_state.job_record_id = record_id
    st.session_state.pop("match_analysis", None)
    st.session_state.pop("skill_gap_analysis", None)
    success_toast("Job analysis saved.")
    st.rerun()


def _index_job(text: str, source: str) -> None:
    token = st.session_state.get("user_session_id")
    user_id = st.session_state.get("user_id")
    if not token or not user_id:
        return
    knowledge_base = get_knowledge_base(str(token), int(user_id))
    knowledge_base.add_text(text, display_name=source, source_type="job_description")


def _is_empty_job(analysis: JobAnalysisResult) -> bool:
    fields = (
        analysis.required_skills,
        analysis.preferred_skills,
        analysis.programming_languages,
        analysis.frameworks,
        analysis.databases,
        analysis.tools,
        analysis.experience_requirements,
        analysis.responsibilities,
        analysis.keywords,
    )
    return not analysis.job_title and not any(fields)


def _render_job_results() -> None:
    job: JobAnalysisResult = st.session_state.job_analysis
    st.divider()
    st.subheader(job.job_title or "Job analysis")
    if job.company:
        st.caption(job.company)
    kpi_row(
        [
            ("Required skills", len(job.required_skills), "explicit"),
            ("Preferred skills", len(job.preferred_skills), "explicit"),
            ("Tech stack", _stack_count(job), "extracted"),
            ("Experience rules", len(job.experience_requirements), "extracted"),
        ]
    )
    left, right = st.columns(2)
    with left:
        st.markdown("**Required skills**")
        st.write(", ".join(job.required_skills) if job.required_skills else "None specified")
        st.markdown("**Preferred skills**")
        st.write(", ".join(job.preferred_skills) if job.preferred_skills else "None specified")
    with right:
        st.markdown("**Experience requirements**")
        for item in job.experience_requirements or ["None specified"]:
            st.markdown(f"- {item}")
        st.markdown("**Education requirements**")
        for item in job.education_requirements or ["None specified"]:
            st.markdown(f"- {item}")

    with st.expander("Tech stack, responsibilities and keywords"):
        stack_rows = [
            {"Category": label, "Values": ", ".join(values) if values else "None"}
            for label, values in (
                ("Languages", job.programming_languages),
                ("Frameworks", job.frameworks),
                ("Databases", job.databases),
                ("Tools", job.tools),
            )
        ]
        st.dataframe(stack_rows, hide_index=True, width="stretch", alt="Extracted job tech stack")
        st.markdown("**Responsibilities**")
        for item in job.responsibilities or ["None specified"]:
            st.markdown(f"- {item}")
        st.markdown("**Keywords**")
        st.write(", ".join(job.keywords) if job.keywords else "None extracted")


def _stack_count(job: JobAnalysisResult) -> int:
    return sum(
        len(values)
        for values in (
            job.programming_languages,
            job.frameworks,
            job.databases,
            job.tools,
        )
    )


def _render_match(workspace: int) -> None:
    resume_records = list_resume_history(workspace, _actor_id())
    job_records = list_job_history(workspace, _actor_id())
    if not job_records:
        empty_state("No job analysis yet", "Analyse a job before running a match.")
        return

    active_resume_id = st.session_state.get("resume_record_id")
    active_job_id = st.session_state.get("job_record_id")
    resume_index = _index_for(resume_records, active_resume_id)
    job_index = _index_for(job_records, active_job_id)

    left, right = st.columns(2)
    with left:
        selected_resume = st.selectbox(
            "Resume version",
            options=resume_records,
            index=resume_index,
            format_func=lambda item: f"{_format_saved_at(item.created_at)} · {item.label}",
        )
    with right:
        selected_job = st.selectbox(
            "Job version",
            options=job_records,
            index=job_index,
            format_func=lambda item: f"{_format_saved_at(item.created_at)} · {item.label}",
        )

    if st.button(
        "Use selected pair",
        icon=":material/swap_horiz:",
        width="stretch",
    ):
        _load_pair(workspace, selected_resume.id, selected_job.id)

    extra_skills = st.text_area(
        "Additional skills you have",
        height=90,
        key="extra_skills",
        placeholder="Only add skills you can evidence, separated by commas or new lines.",
        help="These are included in the deterministic skill gap, alongside extracted resume skills.",
    )

    build_col, match_col = st.columns(2)
    with build_col:
        if st.button(
            "Build skill gap",
            icon=":material/route:",
            width="stretch",
        ):
            _run_gap(workspace, selected_resume.id, selected_job.id, extra_skills)
    with match_col:
        if st.button(
            "Run AI match",
            icon=":material/compare_arrows:",
            type="primary",
            width="stretch",
        ):
            _run_match(workspace, selected_resume.id, selected_job.id, extra_skills)

    gap = st.session_state.get("skill_gap_analysis")
    if isinstance(gap, SkillGapAnalysis):
        _render_skill_gap(gap)
    match = st.session_state.get("match_analysis")
    if isinstance(match, ResumeJobMatchResult):
        _render_match_results(match, gap if isinstance(gap, SkillGapAnalysis) else None)


def _index_for(records: list, active_id) -> int:
    if active_id is None:
        return 0
    for index, record in enumerate(records):
        if record.id == active_id:
            return index
    return 0


def _load_pair(workspace: int, resume_id: int, job_id: int) -> None:
    try:
        resume = load_resume_record(resume_id, workspace, _actor_id())
        job = load_job_record(job_id, workspace, _actor_id())
    except AuthError as exc:
        error_state(str(exc))
        return
    _apply_pair(resume, job)
    st.session_state.pop("match_analysis", None)
    st.session_state.pop("skill_gap_analysis", None)
    success_toast("Selected pair loaded.")
    st.rerun()


def _apply_pair(resume, job) -> None:
    st.session_state.resume_analysis = resume.analysis
    st.session_state.resume_scores = resume.scores
    st.session_state.resume_text = resume.raw_text
    st.session_state.resume_record_id = resume.id
    st.session_state.resume_file_name = resume.file_name
    st.session_state.job_analysis = job.analysis
    _set_job_text(job.raw_text)
    st.session_state.job_source = job.source
    st.session_state.job_record_id = job.id


def _gap_inputs(workspace: int, resume_id: int, job_id: int, extra_skills: str):
    try:
        resume = load_resume_record(resume_id, workspace, _actor_id())
        job = load_job_record(job_id, workspace, _actor_id())
    except AuthError as exc:
        error_state(str(exc))
        return None
    _apply_pair(resume, job)
    return resume.analysis, job.analysis


def _run_gap(workspace: int, resume_id: int, job_id: int, extra_skills: str) -> None:
    inputs = _gap_inputs(workspace, resume_id, job_id, extra_skills)
    if inputs is None:
        return
    resume, job = inputs
    gap = build_skill_gap(resume, job, extra_skills)
    st.session_state.skill_gap_analysis = gap
    success_toast("Skill gap updated from extracted resume and user-provided skills.")
    st.rerun()


def _run_match(workspace: int, resume_id: int, job_id: int, extra_skills: str) -> None:
    inputs = _gap_inputs(workspace, resume_id, job_id, extra_skills)
    if inputs is None:
        return
    resume, job = inputs
    gap = build_skill_gap(resume, job, extra_skills)
    with st.status("Matching resume with job", expanded=True) as status:
        try:
            status.write("Comparing extracted profiles")
            match: ResumeJobMatchResult = match_resume_job(resume, job)
            if _is_empty_match(match):
                raise RuntimeError("The AI provider returned no usable match result.")
            status.write("Saving match and learning plan")
            record_id = save_match_analysis(
                workspace,
                _actor_id(),
                resume_id,
                job_id,
                match,
                gap,
            )
            status.update(label="Match complete", state="complete")
        except Exception as exc:
            status.update(label="Match failed", state="error")
            error_state(friendly_error(exc, "Resume-job matching"))
            return
    st.session_state.match_analysis = match
    st.session_state.skill_gap_analysis = gap
    st.session_state.match_record_id = record_id
    success_toast("Match saved.")
    st.rerun()


def _is_empty_match(match: ResumeJobMatchResult) -> bool:
    return (
        match.overall_match == 0
        and match.technical_skills_match == 0
        and match.experience_match == 0
        and not match.matching_skills
        and not match.missing_skills
        and not match.recommendations
    )


def _render_skill_gap(gap: SkillGapAnalysis) -> None:
    st.divider()
    st.subheader("Skill gap analysis")
    st.caption(gap.summary)
    kpi_row(
        [
            ("Evidence coverage", f"{gap.coverage_score:.0f}%", "deterministic"),
            ("Technical coverage", f"{gap.technical_coverage:.0f}%", "detected requirements"),
            ("Soft coverage", f"{gap.soft_coverage:.0f}%", "detected requirements"),
            ("Gaps", len(gap.gaps), "prioritised"),
        ]
    )
    st.progress(
        max(0.0, min(1.0, gap.coverage_score / 100)),
        text=f"Matched evidence: {gap.coverage_score:.0f}% of detected requirements",
    )

    if not gap.gaps:
        empty_state("No gaps detected", "Every detected requirement has evidence in the selected profile.")
    else:
        rows = [
            {
                "Skill": item.skill,
                "Category": item.category.title(),
                "Priority": item.priority.title(),
                "Requirement": item.requirement,
                "Learning topics": ", ".join(item.learning_topics),
                "Next action": item.action_steps[0] if item.action_steps else "",
            }
            for item in gap.gaps
        ]
        st.dataframe(rows, hide_index=True, width="stretch", alt="Prioritised skill gaps")

    with st.expander("Current versus required skills"):
        left, right = st.columns(2)
        with left:
            st.markdown("**Evidenced skills**")
            st.write(", ".join(gap.matched_skills) if gap.matched_skills else "None matched")
            st.caption("Sources: extracted resume fields and user-provided skills only.")
        with right:
            st.markdown("**Detected requirements**")
            st.write(", ".join(gap.required_skills) if gap.required_skills else "None detected")
            st.caption("Sources: selected job analysis fields only.")

    if gap.action_plan:
        with st.expander("Action plan"):
            for index, item in enumerate(gap.action_plan, start=1):
                st.markdown(f"{index}. {item}")


def _render_match_results(match: ResumeJobMatchResult, gap: SkillGapAnalysis | None) -> None:
    st.divider()
    st.subheader("AI compatibility assessment")
    kpi_row(
        [
            ("Overall match", f"{match.overall_match:.0f}%", "AI generated"),
            ("Technical", f"{match.technical_skills_match:.0f}%", "AI generated"),
            ("Experience", f"{match.experience_match:.0f}%", "AI generated"),
            ("Education", f"{match.education_match:.0f}%", "AI generated"),
        ]
    )
    render_score_chart(
        {
            "Technical": match.technical_skills_match,
            "Experience": match.experience_match,
            "Projects": match.projects_match,
            "Education": match.education_match,
            "Overall": match.overall_match,
        },
        "AI-generated match percentages from the selected resume and job",
    )

    with st.expander("Matched skills"):
        if match.matching_skills:
            rows = [
                {
                    "Skill": item.skill,
                    "Strength": item.match_strength,
                    "Resume evidence": item.resume_evidence,
                    "Job requirement": item.job_requirement,
                }
                for item in match.matching_skills
            ]
            st.dataframe(rows, hide_index=True, width="stretch", alt="Matched skills")
        else:
            empty_state("No matched skills returned")

    with st.expander("Missing skills and recommendations"):
        if match.missing_skills:
            rows = [
                {
                    "Skill": item.skill,
                    "Importance": item.importance,
                    "Job requirement": item.job_requirement,
                    "Learning resources": ", ".join(item.learning_resources),
                }
                for item in match.missing_skills
            ]
            st.dataframe(rows, hide_index=True, width="stretch", alt="Missing skills")
        else:
            empty_state("No missing skills returned")
        for item in match.recommendations:
            st.markdown(f"- {item}")

    with st.expander("Experience comparison"):
        if match.experience_gaps:
            rows = [
                {
                    "Area": item.area,
                    "Job requirement": item.job_requirement,
                    "Your status": item.resume_status,
                    "Gap": item.gap_description,
                }
                for item in match.experience_gaps
            ]
            st.dataframe(rows, hide_index=True, width="stretch", alt="Experience gaps")
        else:
            empty_state("No experience gaps returned")

    export = _match_report(match, gap)
    export_col, pdf_col, interview_col = st.columns(3)
    with export_col:
        st.download_button(
            "Download JSON",
            data=json.dumps(export, indent=2),
            file_name="job-match.json",
            mime="application/json",
            icon=":material/download:",
            width="stretch",
        )
    with pdf_col:
        st.download_button(
            "Download PDF report",
            data=render_career_pdf(export),
            file_name="job-match.pdf",
            mime="application/pdf",
            icon=":material/picture_as_pdf:",
            width="stretch",
        )
    with interview_col:
        if st.button(
            "Set up interview",
            icon=":material/forum:",
            type="primary",
            width="stretch",
        ):
            go_to("interview_setup")


def _match_report(match: ResumeJobMatchResult, gap: SkillGapAnalysis | None) -> dict:
    sections = [
        {
            "heading": "Matched skills",
            "items": [item.skill for item in match.matching_skills],
        },
        {
            "heading": "Missing skills",
            "items": [item.skill for item in match.missing_skills],
        },
        {
            "heading": "Experience gaps",
            "items": [
                {
                    "title": item.area,
                    "detail": f"{item.job_requirement} · {item.resume_status} · {item.gap_description}",
                }
                for item in match.experience_gaps
            ],
        },
        {
            "heading": "Recommendations",
            "items": match.recommendations,
        },
    ]
    if gap:
        sections.extend(
            [
                {
                    "heading": "Skill gaps",
                    "items": [
                        {
                            "skill": item.skill,
                            "evidence": f"{item.category} · {item.priority} · {item.requirement}",
                        }
                        for item in gap.gaps
                    ],
                },
                {"heading": "Action plan", "items": gap.action_plan},
            ]
        )
    return {
        "title": "Job match report",
        "subtitle": gap.target_role if gap else "Resume and job match",
        "scores": {
            "Overall": f"{match.overall_match:.0f}%",
            "Technical": f"{match.technical_skills_match:.0f}%",
            "Experience": f"{match.experience_match:.0f}%",
            "Projects": f"{match.projects_match:.0f}%",
            "Education": f"{match.education_match:.0f}%",
            "Evidence coverage": f"{gap.coverage_score:.0f}%" if gap else "Not computed",
        },
        "sections": sections,
    }


def _render_job_history(workspace: int) -> None:
    st.subheader("Saved job analyses")
    records = list_job_history(workspace, _actor_id())
    if not records:
        empty_state("No saved jobs", "Analysed job descriptions will appear here.")
        return
    selected = st.selectbox(
        "Saved job",
        options=records,
        format_func=lambda item: f"{_format_saved_at(item.created_at)} · {item.label}",
        key="job_history_select",
    )
    details = load_job_record(selected.id, workspace, _actor_id())
    kpi_row(
        [
            ("Job title", details.analysis.job_title or "Not specified", details.source),
            ("Required skills", len(details.analysis.required_skills), "explicit"),
            ("Tech stack", _stack_count(details.analysis), "extracted"),
            ("Saved", _format_saved_at(details.created_at), "local workspace"),
        ]
    )
    if st.button(
        "Load into workspace",
        icon=":material/upload_file:",
        type="primary",
        width="stretch",
    ):
        st.session_state.job_analysis = details.analysis
        _set_job_text(details.raw_text)
        st.session_state.job_source = details.source
        st.session_state.job_record_id = details.id
        st.session_state.pop("match_analysis", None)
        st.session_state.pop("skill_gap_analysis", None)
        success_toast("Saved job loaded.")
        st.rerun()


def _render_match_history(workspace: int) -> None:
    st.subheader("Saved match runs")
    records = list_match_history(workspace, _actor_id())
    if not records:
        empty_state("No saved matches", "AI match runs will appear here with their scores.")
        return
    selected = st.selectbox(
        "Saved match",
        options=records,
        format_func=lambda item: (
            f"{_format_saved_at(item.created_at)} · {item.label} · "
            f"{item.score:.0f}%" if item.score is not None else f"{_format_saved_at(item.created_at)} · {item.label}"
        ),
        key="match_history_select",
    )
    details = load_match_record(selected.id, workspace, _actor_id())
    kpi_row(
        [
            ("Overall", f"{details.match.overall_match:.0f}%", "AI generated"),
            ("Technical", f"{details.match.technical_skills_match:.0f}%", "AI generated"),
            ("Experience", f"{details.match.experience_match:.0f}%", "AI generated"),
            ("Saved", _format_saved_at(details.created_at), "local workspace"),
        ]
    )
    if st.button(
        "Load match details",
        icon=":material/history:",
        type="primary",
        width="stretch",
    ):
        resume = load_resume_record(details.resume_id, workspace, _actor_id())
        job = load_job_record(details.job_id, workspace, _actor_id())
        gap = build_skill_gap(resume.analysis, job.analysis)
        _apply_pair(resume, job)
        st.session_state.match_analysis = details.match
        st.session_state.skill_gap_analysis = gap
        st.session_state.match_record_id = details.id
        success_toast("Saved match loaded.")
        st.rerun()


show_job_page()
