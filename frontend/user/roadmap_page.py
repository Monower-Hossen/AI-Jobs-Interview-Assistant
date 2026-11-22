"""Personalized learning roadmap UI with completion tracking."""

import streamlit as st
from services.roadmap_service import generate_roadmap, list_roadmaps, set_task_status

from frontend.shared.components import empty_state, error_state, page_header, section, success_toast
from frontend.shared.navigation import go_to
from frontend.user.interview_page import _ensure_user_session


def show_roadmap_page(embedded: bool = False) -> None:
    if not embedded:
        page_header(
            "Learning roadmap",
            "Turn interview feedback into a measurable study plan",
            ":material/route:",
        )
    session_id = _ensure_user_session()

    with st.container(border=True):
        st.markdown("**Build a plan**")
        use_report = st.toggle("Use my latest interview report", value=True)
        manual_skills = ""
        job_analysis = st.session_state.get("job_analysis")
        role = str(getattr(job_analysis, "job_title", "") or "") if job_analysis else ""
        if not use_report:
            manual_skills = st.text_area("Weak skills or topics (comma separated)", height=80)
            role = st.text_input("Target role", value=role or "")
        hours = st.slider("Hours available per week", 2, 20, 6)
        if st.button(
            "Generate roadmap", type="primary", icon=":material/auto_awesome:", width="stretch"
        ):
            last = _latest_completed_interview(session_id)
            if use_report and not last:
                st.warning(
                    "No completed interview report found. Complete a mock interview first, "
                    "or disable 'Use my latest interview report' and enter focus skills."
                )
                return
            weak = (
                list(last.get("weaknesses") or []) + list(last.get("recommended_topics") or [])
                if last
                else []
            )
            topics = [item.strip() for item in manual_skills.split(",") if item.strip()]
            try:
                with st.spinner("Creating your study plan…"):
                    generate_roadmap(
                        session_id=session_id,
                        weak_skills=weak,
                        topics=topics,
                        role=role,
                        hours_per_week=hours,
                        source_interview_id=last.get("id") if last else None,
                        actor_user_id=st.session_state.get("user_id"),
                    )
            except Exception as exc:
                error_state("Roadmap generation failed.", str(exc))
            else:
                success_toast("Roadmap created.")
                st.rerun()

    roadmaps = list_roadmaps(session_id, st.session_state.get("user_id"))
    if not roadmaps:
        empty_state("No roadmap yet.", "Complete a mock interview or enter focus skills above.")
        if st.button("Go to mock interview", width="stretch"):
            go_to("interview_setup")
        return

    options = {
        (
            f"{roadmap.title!s} · {roadmap.created_at:%Y-%m-%d}"
            if roadmap.created_at
            else str(roadmap.title)
        ): roadmap
        for roadmap in roadmaps
    }
    choice = st.selectbox("Roadmap to view", list(options), index=0, width="stretch")
    roadmap = options[choice]

    section(str(roadmap.title), ":material/map:")
    tasks = list(roadmap.tasks)
    completed = sum(1 for task in tasks if task.status == "completed")
    st.progress(
        completed / len(tasks) if tasks else 0, text=f"{completed}/{len(tasks)} tasks complete"
    )
    st.caption("Focus: " + ", ".join(roadmap.focus_skills or []))

    for task in tasks:
        with st.container(border=True):
            done = st.checkbox(
                f"**{task.title}**",
                value=task.status == "completed",
                key=f"roadmap_task_{task.id}",
            )
            if done != (task.status == "completed"):
                set_task_status(task.id, done, session_id, st.session_state.get("user_id"))
                st.rerun()
            if task.description:
                st.write(task.description)
            if task.resources:
                st.caption("Resources: " + ", ".join(task.resources))
            if task.practice_questions:
                with st.expander("Practice questions"):
                    for question in task.practice_questions:
                        st.markdown(f"- {question}")


def _latest_completed_interview(session_id: int) -> dict:
    from database import InterviewSession, get_db_context

    with get_db_context() as db:
        row = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.session_id == session_id, InterviewSession.status == "completed"
            )
            .order_by(InterviewSession.completed_at.desc())
            .first()
        )
        if not row:
            return {}
        return {
            "id": row.id,
            "weaknesses": row.weaknesses or [],
            "recommended_topics": row.recommended_topics or [],
        }
