"""Admin page for creating and managing job posts used by job interviews."""

from __future__ import annotations

import streamlit as st
from database.models import JobPost
from services.job_interview_constants import DIFFICULTIES, INTERVIEW_CATEGORIES

from frontend.shared.components import (
    confirm_delete,
    data_table,
    empty_state,
    page_header,
    success_toast,
)

_FORM_KEYS = [
    "title",
    "company",
    "description",
    "responsibilities",
    "required_skills",
    "preferred_skills",
    "experience_level",
    "education",
    "interview_difficulty",
    "question_preferences",
    "is_published",
]
_DEFAULTS_KEY = "_admin_job_form_defaults"
_EDITING_KEY = "_admin_job_editing_id"


def _reset_form_keys() -> None:
    for suffix in _FORM_KEYS:
        st.session_state.pop(f"_admin_job_{suffix}", None)


def _load_job_choices(jobs: list[JobPost]) -> dict[int, JobPost]:
    return {int(job.id): job for job in jobs}


def show_admin_jobs() -> None:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        st.error("Administrator access required.", icon=":material/error:")
        st.stop()

    from services.auth_service import require_admin
    from services.job_blueprint import BlueprintError, build_job_blueprint
    from services.job_post_service import (
        JobPostError,
        create_job_post,
        delete_job_post,
        get_job_skills,
        list_jobs,
        set_job_published,
        update_job_post,
    )

    require_admin(account.id)
    page_header(
        "Job Postings",
        "Create and publish jobs that power job-based AI interviews.",
        ":material/work:",
    )

    jobs = list_jobs(actor_admin_id=account.id, include_unpublished=True)
    choices = _load_job_choices(jobs)
    create_tab, manage_tab, analysis_tab = st.tabs(
        ["Create / Edit", "Manage postings", "JD analysis"]
    )

    with create_tab:
        editing_id = st.session_state.get(_EDITING_KEY)
        st.caption(
            f"Editing job #{editing_id}. Clear the selection to create a new posting."
            if editing_id
            else "Create a complete job posting so interview questions stay grounded in the role."
        )
        if choices:
            pick_col, load_col, clear_col = st.columns([3, 1, 1])
            selected = pick_col.selectbox(
                "Load an existing job for editing",
                options=[0, *list(choices.keys())],
                format_func=lambda value: (
                    "Select…" if value == 0 else f"#{value} — {choices[value].title}"
                ),
                key="_admin_job_pick",
            )
            if load_col.button("Load", width="stretch", key="_admin_job_load"):
                if selected:
                    job = choices[int(selected or 0)]
                    st.session_state[_DEFAULTS_KEY] = {
                        "title": job.title,
                        "company": job.company,
                        "description": job.description,
                        "responsibilities": job.responsibilities or [],
                        "required_skills": job.required_skills or [],
                        "preferred_skills": job.preferred_skills or [],
                        "experience_level": job.experience_level or "",
                        "education": job.education or "",
                        "interview_difficulty": job.interview_difficulty or "medium",
                        "question_preferences": job.question_preferences or [],
                        "is_published": bool(job.is_published),
                    }
                    st.session_state[_EDITING_KEY] = int(selected)
                else:
                    st.session_state[_DEFAULTS_KEY] = {}
                    st.session_state[_EDITING_KEY] = None
                _reset_form_keys()
                st.rerun()
            if clear_col.button("New job", width="stretch", key="_admin_job_new"):
                st.session_state[_DEFAULTS_KEY] = {}
                st.session_state[_EDITING_KEY] = None
                _reset_form_keys()
                st.rerun()

        defaults: dict = st.session_state.get(_DEFAULTS_KEY, {})
        with st.form("_admin_job_form", clear_on_submit=False):
            col1, col2 = st.columns(2)
            title = col1.text_input(
                "Job title *", value=defaults.get("title", ""), key="_admin_job_title"
            )
            company = col2.text_input(
                "Company *", value=defaults.get("company", ""), key="_admin_job_company"
            )
            description = st.text_area(
                "Full job description *",
                value=defaults.get("description", ""),
                height=180,
                key="_admin_job_description",
            )
            responsibilities = st.text_area(
                "Key responsibilities (one per line)",
                value="\n".join(defaults.get("responsibilities", [])),
                height=110,
                key="_admin_job_responsibilities",
            )
            skill_col1, skill_col2 = st.columns(2)
            required_skills = skill_col1.text_area(
                "Required skills (comma or newline separated)",
                value=", ".join(defaults.get("required_skills", [])),
                height=100,
                key="_admin_job_required_skills",
            )
            preferred_skills = skill_col2.text_area(
                "Preferred skills",
                value=", ".join(defaults.get("preferred_skills", [])),
                height=100,
                key="_admin_job_preferred_skills",
            )
            detail_col1, detail_col2 = st.columns(2)
            experience = detail_col1.text_input(
                "Experience level",
                value=defaults.get("experience_level", ""),
                key="_admin_job_experience_level",
            )
            education = detail_col2.text_input(
                "Education", value=defaults.get("education", ""), key="_admin_job_education"
            )
            config_col1, config_col2 = st.columns(2)
            difficulty = config_col1.selectbox(
                "Interview difficulty",
                options=list(DIFFICULTIES),
                index=(
                    list(DIFFICULTIES).index(defaults.get("interview_difficulty", "medium"))
                    if defaults.get("interview_difficulty", "medium") in DIFFICULTIES
                    else 1
                ),
                key="_admin_job_interview_difficulty",
            )
            categories = config_col2.multiselect(
                "Question categories",
                options=list(INTERVIEW_CATEGORIES),
                default=defaults.get("question_preferences", list(INTERVIEW_CATEGORIES)),
                key="_admin_job_question_preferences",
            )
            published = st.checkbox(
                "Publish immediately (users can see and interview for this job)",
                value=bool(defaults.get("is_published", False)),
                key="_admin_job_is_published",
            )
            save_col, cancel_col = st.columns([1, 3])
            save = save_col.form_submit_button(
                "Save job", type="primary", icon=":material/save:", width="stretch"
            )
            if cancel_col.form_submit_button("Clear form", width="stretch"):
                st.session_state[_DEFAULTS_KEY] = {}
                st.session_state[_EDITING_KEY] = None
                _reset_form_keys()
                st.rerun()

        if save:
            payload = {
                "title": title,
                "company": company,
                "description": description,
                "responsibilities": responsibilities,
                "required_skills": required_skills,
                "preferred_skills": preferred_skills,
                "experience_level": experience,
                "education": education,
                "interview_difficulty": difficulty,
                "question_preferences": categories,
                "is_published": published,
            }
            try:
                if st.session_state.get(_EDITING_KEY):
                    update_job_post(
                        actor_admin_id=account.id,
                        job_id=int(st.session_state[_EDITING_KEY]),
                        **payload,
                    )
                    message = "Job posting updated."
                else:
                    create_job_post(actor_admin_id=account.id, **payload)
                    message = "Job posting created."
                st.session_state[_DEFAULTS_KEY] = {}
                st.session_state[_EDITING_KEY] = None
                _reset_form_keys()
                success_toast(message)
                st.rerun()
            except JobPostError as exc:
                st.error(str(exc), icon=":material/error:")

    with manage_tab:
        data_table(
            [
                {
                    "ID": int(job.id),
                    "Title": job.title,
                    "Company": job.company,
                    "Difficulty": str(job.interview_difficulty).title(),
                    "Published": "Yes" if job.is_published else "No",
                    "Skills": len(job.required_skills or []) + len(job.preferred_skills or []),
                }
                for job in jobs
            ],
            "No job postings yet. Create one in the first tab.",
        )
        if not choices:
            return
        selected_id = st.selectbox(
            "Select job",
            options=list(choices.keys()),
            format_func=lambda value: f"#{value} — {choices[value].title}",
            key="_admin_job_manage_select",
        )
        job = choices[int(selected_id or 0)]
        st.markdown(f"**{job.company} — {job.title}**")
        st.caption((job.description or "")[:500])
        skills = get_job_skills(int(job.id))
        st.markdown(
            "**Skills:** "
            + (", ".join(f"{skill.name} ({skill.skill_type})" for skill in skills) or "None")
        )
        action_col1, action_col2, action_col3 = st.columns(3)
        if action_col1.button(
            "Unpublish" if job.is_published else "Publish",
            icon=":material/public:" if not job.is_published else ":material/public_off:",
            width="stretch",
            key="_admin_job_toggle_publish",
        ):
            set_job_published(
                actor_admin_id=account.id,
                job_id=int(job.id),
                is_published=not bool(job.is_published),
            )
            success_toast("Job visibility updated.")
            st.rerun()
        if action_col2.button(
            "Edit", icon=":material/edit:", width="stretch", key="_admin_job_edit"
        ):
            st.session_state[_EDITING_KEY] = int(job.id)
            st.session_state[_DEFAULTS_KEY] = {
                "title": job.title,
                "company": job.company,
                "description": job.description,
                "responsibilities": job.responsibilities or [],
                "required_skills": job.required_skills or [],
                "preferred_skills": job.preferred_skills or [],
                "experience_level": job.experience_level or "",
                "education": job.education or "",
                "interview_difficulty": job.interview_difficulty or "medium",
                "question_preferences": job.question_preferences or [],
                "is_published": bool(job.is_published),
            }
            _reset_form_keys()
            st.rerun()
        if action_col3.button(
            "Delete", icon=":material/delete:", width="stretch", key="_admin_job_delete_start"
        ):
            st.session_state["_admin_job_delete_target"] = int(job.id)
            st.session_state["_admin_job_delete_label"] = f"{job.company} — {job.title}"
            key = f"_admin_job_delete_{int(job.id)}"
            st.session_state[key] = False
            st.session_state[f"{key}_ack"] = False
            st.session_state[f"{key}_resolved"] = False

        target_id = st.session_state.get("_admin_job_delete_target")
        if target_id is not None:
            key = f"_admin_job_delete_{int(target_id)}"
            if st.session_state.get(f"{key}_resolved"):
                st.session_state.pop(f"{key}_resolved", None)
                if st.session_state.get(key):
                    delete_job_post(actor_admin_id=account.id, job_id=int(target_id))
                    success_toast("Job posting deleted.")
                st.session_state.pop("_admin_job_delete_target", None)
                st.session_state.pop("_admin_job_delete_label", None)
                st.rerun()
            confirm_delete(st.session_state.get("_admin_job_delete_label", str(target_id)), key=key)

    with analysis_tab:
        if not choices:
            empty_state("Create a job posting before analyzing a description.")
            return
        selected_id = st.selectbox(
            "Job to analyze",
            options=list(choices.keys()),
            format_func=lambda value: f"#{value} — {choices[value].title}",
            key="_admin_job_analysis_select",
        )
        job = choices[int(selected_id or 0)]
        if st.button(
            "Analyze description with AI",
            type="primary",
            icon=":material/auto_awesome:",
            width="stretch",
            key="_admin_job_analyze",
        ):
            with st.spinner("Building interview blueprint…"):
                try:
                    blueprint = build_job_blueprint(job, refresh=True)
                except BlueprintError as exc:
                    st.error(str(exc), icon=":material/error:")
                else:
                    st.session_state["_admin_job_blueprint"] = blueprint.to_dict()
                    success_toast("Blueprint saved for interviews.")
        payload = st.session_state.get("_admin_job_blueprint") or {}
        if isinstance(payload, dict) and payload:
            st.subheader("Generated blueprint")
            st.json(payload)
        elif job.blueprint_json:
            st.subheader("Saved blueprint")
            st.json(job.blueprint_json)


show_admin_jobs()
