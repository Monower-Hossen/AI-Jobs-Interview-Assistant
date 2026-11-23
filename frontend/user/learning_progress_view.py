"""Shared learning progress rendering for Learning Center and the Learning Progress page."""

import streamlit as st
from database import Assessment, get_db_context
from services.analytics import get_analytics
from services.roadmap_service import list_roadmaps

from frontend.shared.components import empty_state, section


def render_achievements(achievements: list[dict]) -> None:
    section("Achievements and badges", ":material/emoji_events:")
    if not achievements:
        empty_state("No badges yet.", "Complete tasks and resources to earn achievements.")
        return
    for badge in achievements:
        st.markdown(f"- **{badge['title']}** · {badge['points']} pts · {badge['description']}")


def render_learning_progress(session_id: int, snapshot: dict) -> None:
    section("Learning progress", ":material/analytics:")
    snapshot_analytics = get_analytics(session_id, st.session_state.get("user_id"))
    roadmaps = list_roadmaps(session_id, st.session_state.get("user_id"))
    total_tasks = sum(len(roadmap.tasks) for roadmap in roadmaps)
    done_tasks = sum(
        1 for roadmap in roadmaps for task in roadmap.tasks if task.status == "completed"
    )
    with get_db_context() as db:
        assessment_count = (
            db.query(Assessment)
            .filter(Assessment.session_id == session_id, Assessment.status == "completed")
            .count()
        )
    st.metric("Interview readiness", f"{snapshot_analytics.readiness_score:.1f}/10", border=True)
    st.metric("Study plan tasks", f"{done_tasks}/{total_tasks}", border=True)
    st.metric("Completed assessments", assessment_count, border=True)
    st.metric("Questions answered", snapshot_analytics.total_answered, border=True)
    st.metric("Activity streak", f"{snapshot['streak']} days", border=True)
    st.metric("Learning points", snapshot["points"], border=True)
    if total_tasks:
        st.progress(done_tasks / total_tasks, text=f"{done_tasks} of {total_tasks} tasks complete")
    else:
        empty_state("Create a study plan to track task progress.")
    render_achievements(snapshot["achievements"])
