from collections.abc import Iterable
from typing import Literal
from urllib.parse import urlsplit

import pandas as pd
import streamlit as st
from database import LearningRoadmap
from services.auth_service import owned_session_id
from services.dashboard_service import get_dashboard_snapshot
from services.roadmap_service import (
    describe_roadmap_error,
    generate_assessment,
    generate_roadmap,
    get_study_plan_progress,
    list_assessments,
    list_roadmaps,
    refresh_recommendations,
    regenerate_roadmap,
    set_roadmap_status,
    set_task_status,
    submit_assessment,
    update_roadmap_settings,
)

from frontend.shared.components import (
    empty_state,
    error_state,
    kpi_row,
    page_header,
    section,
    success_toast,
    warning_toast,
)

BadgeColor = Literal["red", "orange", "yellow", "blue", "green", "violet", "gray", "grey", "primary"]

_KNOWN_RESOURCE_DOMAINS = frozenset(
    {
        "developer.mozilla.org",
        "docs.python.org",
        "fastapi.tiangolo.com",
        "flask.palletsprojects.com",
        "geeksforgeeks.org",
        "kaggle.com",
        "leetcode.com",
        "nodejs.org",
        "numpy.org",
        "pandas.pydata.org",
        "react.dev",
        "roadmap.sh",
        "scikit-learn.org",
        "sqlalchemy.org",
    }
)
_PROVIDERS = ("auto", "groq", "gemini", "ollama")
_PROVIDER_LABELS = {
    "auto": "Auto (configured fallback chain)",
    "groq": "Groq",
    "gemini": "Gemini",
    "ollama": "Ollama (local)",
}
_FEATURE_TABS = (
    "Career Roadmap",
    "Daily Tasks",
    "Weekly Goals",
    "Skill Analysis",
    "Resources",
    "Progress Analytics",
    "AI Recommendations",
    "Weekly Assessment",
)


def _workspace_id() -> int | None:
    token = st.session_state.get("user_session_id")
    if not token:
        return None
    return owned_session_id(token, st.session_state.get("user_id"))


def _format_names(values: Iterable[object]) -> str:
    return ", ".join(str(value) for value in values if str(value).strip()) or "Not specified"


def _date_label(value: object) -> str:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else ""


def _safe_resource_url(value: object) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    hostname = parsed.hostname.lower().removeprefix("www.")
    return text if hostname in _KNOWN_RESOURCE_DOMAINS else ""


def _infer_resource_type(url: str, title: str) -> str:
    parsed_hostname = urlsplit(url).hostname if url else None
    hostname = parsed_hostname.lower().removeprefix("www.") if parsed_hostname else ""
    label = f"{title} {hostname}".lower()
    if hostname in {"developer.mozilla.org", "docs.python.org", "nodejs.org", "react.dev"}:
        return "Documentation"
    if hostname in {"leetcode.com", "kaggle.com"} or "practice" in label:
        return "Practice"
    if "video" in label or "youtube" in label:
        return "Video"
    if "tutorial" in label or "course" in label:
        return "Tutorial"
    return "Reference" if not url else "Guide"


def _normalize_resources(values: Iterable[object], context: str = "") -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        if isinstance(value, dict):
            title = str(value.get("title") or value.get("name") or "").strip()
            raw_url = str(value.get("url") or "")
            resource_type = str(value.get("type") or "").strip()
            description = str(value.get("description") or "").strip()
        else:
            title = str(value).strip()
            raw_url = title if _safe_resource_url(title) else ""
            resource_type = ""
            description = ""
        if not title and not raw_url:
            continue
        url = _safe_resource_url(raw_url)
        if not title:
            title = urlsplit(url).hostname or "Learning resource"
        key = (title.lower(), url)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "title": title,
                "url": url,
                "type": resource_type or _infer_resource_type(url, title),
                "description": description or context.strip() or "Saved learning reference for this plan.",
            }
        )
    return result


def _provider_select(key: str, label: str = "AI provider") -> str:
    return str(
        st.selectbox(
            label,
            _PROVIDERS,
            key=key,
            format_func=lambda value: _PROVIDER_LABELS.get(value, value),
            help="Auto uses the configured fallback chain. Ollama requires a local service and model.",
        )
    )


def _planning_context(session_id: int, actor_user_id: int | None) -> dict:
    snapshot = get_dashboard_snapshot(session_id, actor_user_id)
    gaps = getattr(snapshot, "skill_gaps", {}) or {}
    plan = getattr(snapshot, "improvement_plan", {}) or {}
    history = getattr(snapshot, "interview_history_summary", {}) or {}
    weak_skills = [
        str(item)
        for item in [
            *(gaps.get("items") or []),
            *(gaps.get("weak_areas") or []),
            *(gaps.get("missing_skills") or []),
        ]
        if str(item).strip()
    ]
    topics = [
        str(item)
        for item in [
            *(gaps.get("missing_skills") or []),
            *(plan.get("seven_day") or []),
            *(plan.get("thirty_day") or []),
        ]
        if str(item).strip()
    ]
    if isinstance(plan.get("seven_day"), str):
        topics.append(plan["seven_day"])
    if isinstance(plan.get("thirty_day"), str):
        topics.append(plan["thirty_day"])
    role = str(
        gaps.get("job_title")
        or history.get("latest_job_title")
        or history.get("job_title")
        or ""
    ).strip()
    return {
        "weak_skills": list(dict.fromkeys(weak_skills))[:10],
        "topics": list(dict.fromkeys(topics))[:10],
        "role": role,
    }


def _render_page_header(embedded: bool) -> None:
    if embedded:
        st.subheader("Study Plan", icon=":material/route:")
        st.caption("Your personalized learning journey.")
        return
    page_header("Study Plan", "Your personalized learning journey.", ":material/route:")


def _status_badge(status: str) -> None:
    styles: dict[str, tuple[BadgeColor, str]] = {
        "active": ("green", ":material/check_circle:"),
        "paused": ("orange", ":material/pause:"),
        "completed": ("blue", ":material/check_circle:"),
        "archived": ("gray", ":material/inventory_2:"),
    }
    color, icon = styles.get(status, ("gray", ":material/insights:"))
    st.badge(status.title() or "Active", icon=icon, color=color)


def _week_numbers(roadmap: LearningRoadmap) -> list[int]:
    weeks = {int(task.week_number or 1) for task in roadmap.tasks}
    duration = max(0, int(getattr(roadmap, "duration_weeks", 0) or 0))
    weeks.update(range(1, duration + 1))
    return sorted(weeks) or [1]


def _phase_for_week(roadmap: LearningRoadmap, week: int) -> dict:
    for phase in roadmap.phases or []:
        if not isinstance(phase, dict):
            continue
        start = int(phase.get("week_start") or 0)
        end = int(phase.get("week_end") or duration_or_zero(roadmap) or start)
        if start <= week <= end:
            return phase
    return {}


def duration_or_zero(roadmap: LearningRoadmap) -> int:
    return max(0, int(getattr(roadmap, "duration_weeks", 0) or 0))


def show_roadmap_page(embedded: bool = False) -> None:
    _render_page_header(embedded)
    session_id = _workspace_id()
    actor_user_id = st.session_state.get("user_id")
    if not session_id:
        empty_state("No workspace found.", "Start an interview to create a workspace.")
        return

    try:
        roadmaps = list_roadmaps(session_id, actor_user_id)
    except Exception as exc:
        error_state("Study plans could not be loaded.", describe_roadmap_error(exc))
        return

    if not roadmaps:
        _render_generation_form(session_id, actor_user_id, has_existing=False)
        return

    options = {
        f"{roadmap.title} · {str(roadmap.status or 'active').title()} · {_date_label(roadmap.created_at)}": roadmap
        for roadmap in roadmaps
    }
    selected_label = st.selectbox(
        "Saved study plan",
        list(options),
        key="study_plan_history_select",
    )
    roadmap = options[selected_label]
    _render_plan(roadmap, session_id, actor_user_id)


def _render_generation_form(
    session_id: int, actor_user_id: int | None, *, has_existing: bool
) -> None:
    if has_existing:
        with st.expander("Why can't I create another plan?", icon=":material/info:"):
            st.caption(
                "Use Regenerate to replace the active plan. The previous plan is archived "
                "and completed work with matching topics is preserved."
            )
        return

    section("Create your study plan", ":material/auto_awesome:")
    st.caption("Set your goal and study pace. The plan is saved only after generation succeeds.")
    context = _planning_context(session_id, actor_user_id)
    with st.form("study_plan_generation_form", border=True):
        with st.container(horizontal=True, wrap=True):
            career_target = st.text_input(
                "Career goal",
                value=context["role"],
                placeholder="Backend Engineer",
                help="Used to tailor interview topics and practice tasks.",
            )
            existing_skills = st.text_input(
                "Skills you already have",
                placeholder="Git, REST APIs, SQL",
            )
        focus_skills = st.text_area(
            "Skills and topics to focus on",
            value=", ".join([*context["weak_skills"], *context["topics"]]),
            placeholder="Python, algorithms, system design",
        )
        with st.container(horizontal=True, wrap=True):
            skill_level = st.segmented_control(
                "Current level",
                ["beginner", "intermediate", "advanced"],
                default="intermediate",
            )
            hours_per_day = st.slider(
                "Hours available per day",
                min_value=0.25,
                max_value=3.0,
                value=1.0,
                step=0.25,
            )
            duration_weeks = st.slider(
                "Plan duration",
                min_value=1,
                max_value=12,
                value=4,
                step=1,
            )
            provider = _provider_select("study_plan_generate_provider")
        submitted = st.form_submit_button(
            "Generate roadmap", type="primary", icon=":material/auto_awesome:", width="stretch"
        )

    if not submitted:
        st.caption(
            "Plan context comes from saved interviews, profile details, and job analysis. "
            "You can adjust it before generating."
        )
        return

    focus_items = [item.strip() for item in focus_skills.split(",") if item.strip()]
    existing_items = [item.strip() for item in existing_skills.split(",") if item.strip()]
    if not career_target.strip() and not focus_items:
        error_state("Add a career goal or focus skills before generating.")
        return

    with st.spinner("Creating a phased study plan…"):
        try:
            generate_roadmap(
                session_id,
                focus_items,
                [],
                role=career_target,
                hours_per_week=max(1, round(hours_per_day * 7)),
                source_interview_id=None,
                actor_user_id=actor_user_id,
                career_target=career_target,
                existing_skills=existing_items,
                skill_level=str(skill_level or "intermediate"),
                hours_per_day=float(hours_per_day),
                duration_weeks=int(duration_weeks),
                provider_name=provider,
            )
        except Exception as exc:
            error_state("The study plan could not be generated. Nothing was saved.", describe_roadmap_error(exc))
            return
    success_toast("Study plan generated and saved.")
    st.rerun()


@st.dialog("Edit study plan settings", width="large")
def _edit_plan_dialog(roadmap: LearningRoadmap, session_id: int, actor_user_id: int | None) -> None:
    with st.form("study_plan_edit_form", border=True):
        career_target = st.text_input(
            "Career goal",
            value=str(roadmap.career_target or ""),
        )
        existing_skills = st.text_input(
            "Skills you already have",
            value=", ".join(str(item) for item in (roadmap.existing_skills or [])),
        )
        current_level = str(roadmap.skill_level or "intermediate")
        if current_level not in {"beginner", "intermediate", "advanced"}:
            current_level = "intermediate"
        skill_level = st.segmented_control(
            "Current level",
            ["beginner", "intermediate", "advanced"],
            default=current_level,
        )
        hours_per_day = st.slider(
            "Hours available per day",
            min_value=0.25,
            max_value=3.0,
            value=float(roadmap.hours_per_day or 1.0),
            step=0.25,
        )
        duration_weeks = st.slider(
            "Plan duration",
            min_value=1,
            max_value=12,
            value=int(roadmap.duration_weeks or 4),
            step=1,
        )
        cancel, save = st.columns(2)
        cancelled = cancel.button(
            "Cancel", width="stretch", key="study_plan_edit_cancel_button"
        )
        submitted = save.button(
            "Save settings",
            type="primary",
            width="stretch",
            key="study_plan_edit_save_button",
        )

    if cancelled:
        st.session_state["study_plan_edit_open"] = False
        st.rerun()
    if submitted:
        try:
            update_roadmap_settings(
                int(roadmap.id),
                session_id=session_id,
                actor_user_id=actor_user_id,
                career_target=career_target,
                existing_skills=[
                    item.strip() for item in existing_skills.split(",") if item.strip()
                ],
                skill_level=str(skill_level or "intermediate"),
                hours_per_day=float(hours_per_day),
                duration_weeks=int(duration_weeks),
            )
        except Exception as exc:
            error_state("Settings were not saved.", describe_roadmap_error(exc))
            return
        st.session_state["study_plan_edit_open"] = False
        success_toast("Study plan settings saved.")
        st.rerun()


@st.dialog("Replace study plan?", width="small")
def _regenerate_plan_dialog(roadmap: LearningRoadmap) -> None:
    st.warning(
        f"This archives **{roadmap.title}** and generates a replacement. "
        "Completed work with matching topics is preserved."
    )
    provider = _provider_select("study_plan_regenerate_provider", "Provider for regeneration")
    confirmed = st.checkbox("I understand the current plan will be archived", key="study_plan_regen_ack")
    cancel, replace = st.columns(2)
    if cancel.button(
        "Cancel", width="stretch", key="study_plan_regenerate_cancel_button"
    ):
        st.session_state["study_plan_regenerate_open"] = False
        st.session_state["study_plan_regenerate_confirmed"] = False
        st.rerun()
    if replace.button(
        "Regenerate",
        type="primary",
        disabled=not confirmed,
        width="stretch",
        key="study_plan_regenerate_confirm_button",
    ):
        st.session_state["study_plan_regenerate_provider"] = provider
        st.session_state["study_plan_regenerate_confirmed"] = True
        st.rerun()


def _render_plan_actions(
    roadmap: LearningRoadmap, session_id: int, actor_user_id: int | None
) -> None:
    status = str(roadmap.status or "active")
    status_column, action_column = st.columns([1, 3], vertical_alignment="center")
    with status_column:
        _status_badge(status)
    with action_column, st.container(horizontal=True, horizontal_alignment="right"):
        if st.button(
            "Regenerate",
            icon=":material/refresh:",
            type="primary",
            key="study_plan_regenerate_button",
        ):
            st.session_state["study_plan_regenerate_open"] = True
            st.session_state["study_plan_regenerate_confirmed"] = False
            st.rerun()
        with st.popover("More actions", icon=":material/tune:"):
            if st.button(
                "Edit settings",
                icon=":material/edit:",
                width="stretch",
                key="study_plan_edit_button",
            ):
                st.session_state["study_plan_edit_open"] = True
                st.rerun()
            if status == "paused":
                if st.button(
                    "Resume plan",
                    icon=":material/play_arrow:",
                    width="stretch",
                    key="study_plan_resume_button",
                ):
                    _set_status(int(roadmap.id), "active", session_id, actor_user_id)
            elif status == "active":
                if st.button(
                    "Pause plan",
                    icon=":material/pause:",
                    width="stretch",
                    key="study_plan_pause_button",
                ):
                    _set_status(int(roadmap.id), "paused", session_id, actor_user_id)
            else:
                st.button(
                    "Pause plan",
                    icon=":material/pause:",
                    disabled=True,
                    width="stretch",
                    key="study_plan_pause_disabled",
                )
            if status == "active":
                if st.button(
                    "Mark complete",
                    icon=":material/check_circle:",
                    width="stretch",
                    key="study_plan_complete_button",
                ):
                    _set_status(int(roadmap.id), "completed", session_id, actor_user_id)
            else:
                st.button(
                    "Mark complete",
                    icon=":material/check_circle:",
                    disabled=True,
                    width="stretch",
                    key="study_plan_complete_disabled",
                )

    if st.session_state.get("study_plan_edit_open"):
        _edit_plan_dialog(roadmap, session_id, actor_user_id)

    if st.session_state.get("study_plan_regenerate_open"):
        _regenerate_plan_dialog(roadmap)

    if st.session_state.pop("study_plan_regenerate_confirmed", False):
        st.session_state["study_plan_regenerate_open"] = False
        provider = st.session_state.get("study_plan_regenerate_provider", "auto")
        with st.spinner("Regenerating study plan…"):
            try:
                regenerate_roadmap(
                    int(roadmap.id),
                    session_id=session_id,
                    actor_user_id=actor_user_id,
                    provider_name=str(provider),
                )
            except Exception as exc:
                error_state("The study plan could not be regenerated. Nothing was saved.", describe_roadmap_error(exc))
                return
        success_toast("Study plan regenerated and saved.")
        st.rerun()


def _set_status(
    roadmap_id: int, status: str, session_id: int, actor_user_id: int | None
) -> None:
    try:
        set_roadmap_status(roadmap_id, status, session_id, actor_user_id)
    except Exception as exc:
        error_state("The study plan status was not changed.", describe_roadmap_error(exc))
        return
    success_toast(f"Study plan marked {status}.")
    st.rerun()


def _render_compact_overview(roadmap: LearningRoadmap, progress: dict) -> None:
    with st.container(horizontal=True, wrap=True):
        st.metric(
            "Career goal",
            str(progress["career_target"] or roadmap.career_target or "Not specified"),
            border=True,
            icon=":material/flag:",
        )
        st.metric(
            "Roadmap duration",
            f"{progress['duration_weeks']} weeks",
            border=True,
            icon=":material/calendar_month:",
        )
        st.metric(
            "Overall progress",
            f"{progress['completion_percent']}%",
            border=True,
            icon=":material/trending_up:",
        )
        st.metric(
            "Daily study target",
            f"{float(roadmap.hours_per_day or 0):g} h/day",
            border=True,
            icon=":material/schedule:",
        )


def _render_plan(
    roadmap: LearningRoadmap, session_id: int, actor_user_id: int | None
) -> None:
    try:
        progress = get_study_plan_progress(int(roadmap.id), session_id, actor_user_id)
    except Exception as exc:
        error_state("Study plan progress could not be loaded.", describe_roadmap_error(exc))
        return

    _render_compact_overview(roadmap, progress)
    _render_plan_actions(roadmap, session_id, actor_user_id)
    _render_generation_form(session_id, actor_user_id, has_existing=True)

    tabs = st.tabs(_FEATURE_TABS, key="study_plan_sections", on_change="rerun")
    if tabs[0].open:
        with tabs[0]:
            _render_career_roadmap(roadmap, progress)
    if tabs[1].open:
        with tabs[1]:
            _render_daily_tasks(roadmap, session_id, actor_user_id)
    if tabs[2].open:
        with tabs[2]:
            _render_weekly_goals(roadmap, session_id, actor_user_id)
    if tabs[3].open:
        with tabs[3]:
            _render_skill_gap(roadmap, progress)
    if tabs[4].open:
        with tabs[4]:
            _render_resources_tab(roadmap)
    if tabs[5].open:
        with tabs[5]:
            _render_progress_tab(progress)
    if tabs[6].open:
        with tabs[6]:
            _render_recommendations_tab(roadmap, session_id, actor_user_id, progress)
    if tabs[7].open:
        with tabs[7]:
            _render_assessment_tab(roadmap, session_id, actor_user_id)


def _render_career_roadmap(roadmap: LearningRoadmap, progress: dict) -> None:
    if roadmap.summary:
        st.caption(str(roadmap.summary))
    st.progress(
        progress["completion_percent"] / 100,
        text=f"{progress['completed_tasks']} of {progress['total_tasks']} plan items complete",
    )

    weeks = _week_numbers(roadmap)
    if not weeks:
        empty_state("No weekly roadmap was saved for this plan.")
        return

    section("Weekly timeline", ":material/route:")
    current_week = int(progress.get("current_week") or weeks[0])
    for week in weeks:
        daily_tasks = [
            task for task in roadmap.tasks
            if int(task.week_number or 1) == week and task.task_type == "daily"
        ]
        goals = [
            task for task in roadmap.tasks
            if int(task.week_number or 1) == week and task.task_type == "weekly_goal"
        ]
        phase = _phase_for_week(roadmap, week)
        objectives = [
            str(item)
            for item in [
                *list(phase.get("goals") or []),
                *[task.objective for task in daily_tasks if task.objective],
                *[goal.objective for goal in goals if goal.objective],
            ]
            if str(item).strip()
        ]
        topics = [
            str(item)
            for item in [task.topic for task in daily_tasks if task.topic]
            if str(item).strip()
        ]
        milestones = [
            str(item)
            for item in [
                *list(phase.get("outcomes") or []),
                *[goal.title for goal in goals if goal.title],
            ]
            if str(item).strip()
        ]
        week_tasks = [*daily_tasks, *goals]
        completed = sum(1 for task in week_tasks if task.status == "completed")
        estimated_minutes = sum(int(task.estimated_minutes or 0) for task in daily_tasks)
        week_title = str(goals[0].title if goals else (daily_tasks[0].topic if daily_tasks else f"Week {week}"))

        with st.expander(f"Week {week} · {week_title}", expanded=week == current_week):
            detail_left, detail_right = st.columns(2)
            with detail_left:
                st.markdown("**Learning objectives**")
                st.caption(_format_names(objectives[:6]))
                st.markdown("**Topics**")
                st.caption(_format_names(dict.fromkeys(topics)))
            with detail_right:
                st.markdown("**Milestones**")
                st.caption(_format_names(dict.fromkeys(milestones[:4])))
                st.markdown("**Daily tasks**")
                st.caption(_format_names([task.title for task in daily_tasks[:5]]))
            with st.container(horizontal=True, wrap=True):
                st.badge(f"{completed}/{len(week_tasks)} complete", icon=":material/check_circle:")
                st.badge(f"{estimated_minutes} min estimated", icon=":material/schedule:", color="blue")


def _render_daily_tasks(
    roadmap: LearningRoadmap, session_id: int, actor_user_id: int | None
) -> None:
    tasks = [task for task in roadmap.tasks if task.task_type == "daily"]
    if not tasks:
        empty_state("No daily tasks in this plan.", "Regenerate the plan to create daily tasks.")
        return
    weeks = sorted({int(task.week_number or 1) for task in tasks})
    for week in weeks:
        with st.expander(f"Week {week}", expanded=week == weeks[0]):
            for task in [item for item in tasks if int(item.week_number or 1) == week]:
                checked = st.checkbox(
                    task.title,
                    value=task.status == "completed",
                    key=f"study_plan_task_{task.id}",
                )
                st.caption(
                    f"Day {task.day_number or '-'} · {task.topic or 'General'} · "
                    f"{task.estimated_minutes or 30} min · {task.priority or 'medium'}"
                )
                if task.objective:
                    st.caption(task.objective)
                if task.description and task.description != task.objective:
                    st.write(task.description)
                if task.practice_questions:
                    st.caption("Self-tests: " + _format_names(task.practice_questions))
                if checked != (task.status == "completed"):
                    try:
                        set_task_status(int(task.id), checked, session_id, actor_user_id)
                    except Exception as exc:
                        error_state("Task status was not saved.", describe_roadmap_error(exc))
                        return
                    st.rerun()


def _render_weekly_goals(
    roadmap: LearningRoadmap, session_id: int, actor_user_id: int | None
) -> None:
    goals = [task for task in roadmap.tasks if task.task_type == "weekly_goal"]
    if not goals:
        empty_state("No weekly goals in this plan.")
        return
    weeks = sorted({int(goal.week_number or 1) for goal in goals})
    for week in weeks:
        with st.expander(f"Week {week}", expanded=week == weeks[0]):
            for goal in [item for item in goals if int(item.week_number or 1) == week]:
                checked = st.checkbox(
                    goal.title,
                    value=goal.status == "completed",
                    key=f"study_plan_goal_{goal.id}",
                )
                if goal.objective:
                    st.caption(f"Topics: {goal.objective}")
                if goal.description:
                    st.write(goal.description)
                if goal.resources:
                    st.caption("Assignments: " + _format_names(goal.resources))
                if checked != (goal.status == "completed"):
                    try:
                        set_task_status(int(goal.id), checked, session_id, actor_user_id)
                    except Exception as exc:
                        error_state("Goal status was not saved.", describe_roadmap_error(exc))
                        return
                    st.rerun()


def _render_skill_gap(roadmap: LearningRoadmap, progress: dict) -> None:
    gap = roadmap.skill_gap or {}
    strong_skills = list(gap.get("strong_skills") or [])
    focus_skills = list(gap.get("focus_skills") or roadmap.focus_skills or [])
    mastered = list((progress["mastery"] or {}).get("mastered") or [])
    needs_review = list((progress["mastery"] or {}).get("needs_review") or [])
    coverage = (
        sum(1 for skill in focus_skills if skill in mastered) / len(focus_skills)
        if focus_skills
        else 0.0
    )

    current_column, target_column = st.columns(2, border=True)
    with current_column:
        st.markdown("**Current strengths**")
        st.caption(_format_names(strong_skills))
        st.markdown("**Verified mastery**")
        st.caption(_format_names(mastered))
    with target_column:
        st.markdown("**Target skills**")
        st.caption(_format_names(focus_skills))
        st.markdown("**Needs review**")
        st.caption(_format_names(needs_review))
    st.progress(coverage, text=f"Assessment-verified coverage of target skills: {coverage:.0%}")
    st.caption("Task completion alone never marks a topic as mastered.")
    if gap.get("notes"):
        st.caption(f"Assessment: {gap['notes']}")


def _render_resources_tab(roadmap: LearningRoadmap) -> None:
    resources: list[dict[str, str]] = []
    for task in roadmap.tasks:
        resources.extend(_normalize_resources(task.resources or [], context=str(task.topic or task.title or "")))
    if not resources:
        empty_state("No learning resources were saved for this plan.")
        return
    for index, item in enumerate(resources):
        with st.container(border=True):
            title_column, type_column = st.columns([3, 1])
            with title_column:
                st.markdown(f"**{item['title']}**")
                st.caption(item["description"])
            with type_column:
                st.badge(item["type"], icon=":material/menu_book:")
            if item["url"]:
                st.link_button(
                    "Open resource",
                    item["url"],
                    width="stretch",
                    key=f"study_plan_resource_{index}",
                )
            else:
                st.caption("Known topic or documentation search term; no unverified link was generated.")


def _render_progress_tab(progress: dict) -> None:
    kpi_row(
        [
            ("Daily tasks done", f"{progress['completed_daily']}/{progress['daily_tasks']}", None, ":material/checklist:"),
            ("Weekly goals done", f"{progress['completed_goals']}/{progress['weekly_goals']}", None, ":material/flag:"),
            ("Study time logged", f"{progress['study_hours']} h", None, ":material/schedule:"),
            ("Study consistency", f"{progress['consistency_percent']}%", None, ":material/insights:"),
        ]
    )
    weekly_breakdown = list(progress["weekly_breakdown"] or [])
    if not weekly_breakdown:
        empty_state("No weekly completion data yet.")
        return
    chart_data = pd.DataFrame(
        [
            {
                "Week": f"Week {item['week_number']}",
                "Complete (%)": item["percent"],
            }
            for item in weekly_breakdown
        ]
    )
    section("Weekly completion", ":material/bar_chart:")
    st.bar_chart(
        chart_data,
        x="Week",
        y="Complete (%)",
        alt="Completion percentage by study plan week",
    )
    st.caption("All values come from saved task, goal, and assessment data.")


def _render_recommendations_tab(
    roadmap: LearningRoadmap, session_id: int, actor_user_id: int | None, progress: dict
) -> None:
    recommendations = [str(item).strip() for item in (roadmap.recommendations or []) if str(item).strip()]
    if recommendations:
        with st.container(border=True):
            for index, item in enumerate(recommendations, start=1):
                if len(item) > 220:
                    st.markdown(f"**{index}. {item[:217]}…**")
                    with st.expander("Show full recommendation"):
                        st.write(item)
                else:
                    st.markdown(f"**{index}. {item}**")
    else:
        empty_state("No recommendations saved yet.")

    with st.expander("Update recommendations", icon=":material/tune:"):
        provider = _provider_select("study_plan_recommendation_provider")
        if st.button(
            "Refresh recommendations",
            icon=":material/auto_awesome:",
            width="stretch",
            key="study_plan_refresh_recommendations",
        ):
            with st.spinner("Reviewing saved progress…"):
                try:
                    refresh_recommendations(
                        int(roadmap.id),
                        session_id=session_id,
                        actor_user_id=actor_user_id,
                        provider_name=provider,
                    )
                except Exception as exc:
                    error_state("Recommendations could not be refreshed.", describe_roadmap_error(exc))
                    return
            success_toast("Recommendations refreshed from saved progress.")
            st.rerun()
    st.caption(
        f"Based on {progress['completed_tasks']} completed plan items and "
        f"{progress['assessment_count']} completed assessments."
    )


def _render_assessment_tab(
    roadmap: LearningRoadmap, session_id: int, actor_user_id: int | None
) -> None:
    weeks = _week_numbers(roadmap)
    week = st.selectbox("Assessment week", weeks, key="study_plan_assessment_week")
    try:
        assessments = list_assessments(int(roadmap.id), session_id, actor_user_id)
    except Exception as exc:
        error_state("Assessments could not be loaded.", describe_roadmap_error(exc))
        return

    with st.expander("Assessment options", icon=":material/tune:"):
        provider = _provider_select("study_plan_assessment_provider")
    if st.button(
        "Generate weekly assessment",
        icon=":material/school:",
        type="primary",
        width="stretch",
        key="study_plan_generate_assessment",
    ):
        active = [item for item in assessments if item.status == "in_progress" and item.week_number == week]
        if active:
            warning_toast("An assessment is already open for this week.")
        else:
            with st.spinner("Building an assessment from completed topics…"):
                try:
                    generate_assessment(
                        int(roadmap.id),
                        session_id=session_id,
                        actor_user_id=actor_user_id,
                        week_number=int(week),
                        provider_name=provider,
                    )
                except Exception as exc:
                    error_state("Assessment could not be generated.", describe_roadmap_error(exc))
                    return
            success_toast("Assessment generated and saved.")
            st.rerun()

    if not assessments:
        empty_state(
            "No assessments yet.",
            "Complete daily tasks, then generate a quiz or practical check.",
        )
        return

    attempts = [item for item in assessments if item.week_number == week]
    if not attempts:
        empty_state(f"No assessment attempts for week {week} yet.")
        return

    section(f"Week {week} attempts", ":material/history:")
    history_rows = []
    for item in attempts:
        score = (
            f"{int(item.score or 0)}/{int(item.max_score or 0)}"
            if item.max_score
            else "-"
        )
        history_rows.append(
            {
                "Topic": item.topic,
                "Status": str(item.status or "pending").title(),
                "Score": score,
                "Created": _date_label(item.created_at),
            }
        )
    st.dataframe(
        pd.DataFrame(history_rows),
        hide_index=True,
        alt=f"Assessment attempts for week {week} of the study plan",
    )

    options = {
        f"{item.topic} · {str(item.status).title()} · {_date_label(item.created_at)}": item
        for item in attempts
    }
    selected = options[st.selectbox("Saved assessment", list(options), key="study_plan_assessment_select")]
    questions = list(selected.questions or [])
    if not questions:
        empty_state("This assessment has no saved questions.")
        return

    if selected.status == "completed":
        percent = (
            round(float(selected.score or 0) / float(selected.max_score or 1) * 100)
            if selected.max_score
            else 0
        )
        st.progress(percent / 100, text=f"Score: {int(selected.score or 0)}/{int(selected.max_score or 0)}")
        for item in selected.feedback or []:
            with st.container(border=True):
                st.markdown(f"**{item.get('prompt', '')}**")
                st.markdown(
                    "Correct" if item.get("correct") else f"Review: {item.get('correct_answer', '')}"
                )
                if item.get("explanation"):
                    st.caption(item["explanation"])
        return

    with st.form(f"study_plan_assessment_form_{selected.id}", border=True):
        answers: list[int] = []
        for index, question in enumerate(questions):
            choices = list(question.get("options") or [])
            choice = st.radio(
                str(question.get("prompt") or f"Question {index + 1}"),
                choices,
                index=None,
                key=f"study_plan_answer_{selected.id}_{index}",
            )
            answers.append(choices.index(choice) if choice in choices else -1)
        submitted = st.form_submit_button(
            "Submit assessment", type="primary", width="stretch"
        )
    if submitted:
        try:
            result = submit_assessment(
                int(selected.id), answers, session_id, actor_user_id
            )
        except Exception as exc:
            error_state("Assessment could not be submitted.", describe_roadmap_error(exc))
            return
        success_toast(
            f"Score: {int(result['score'])}/{int(result['max_score'])}. "
            + ("Topic marked mastered." if result["mastered"] else "Topic needs review.")
        )
        st.rerun()
