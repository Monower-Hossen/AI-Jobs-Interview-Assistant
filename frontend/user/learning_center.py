import streamlit as st
from services.auth_service import owned_session_id
from services.learning_service import (
    get_learning_snapshot,
    get_recommended_resources,
    list_resources_for_user,
    record_resource_progress,
    refresh_task_reminders,
)
from services.user_content_service import delete_note, list_notes, save_note

from frontend.shared.components import (
    empty_state,
    error_state,
    kpi_row,
    page_header,
    section,
    success_toast,
)
from frontend.shared.navigation import go_to
from frontend.user.knowledge_page import show_knowledge_page
from frontend.user.learning_progress_view import render_achievements, render_learning_progress
from frontend.user.roadmap_page import show_roadmap_page
from frontend.user.technical_page import show_technical_page


def _workspace_id() -> int | None:
    token = st.session_state.get("user_session_id")
    if not token:
        return None
    return owned_session_id(token, st.session_state.get("user_id"))


def show_learning_center() -> None:
    page_header(
        "Learning center",
        "Personalized study plans, curated resources, progress, achievements, and reminders",
        ":material/school:",
    )
    session_id = _workspace_id()
    if not session_id:
        empty_state("No workspace found.")
        return
    actor_user_id = st.session_state.get("user_id")
    try:
        refresh_task_reminders(session_id, actor_user_id)
        snapshot = get_learning_snapshot(session_id, actor_user_id)
    except Exception as exc:
        error_state("Learning progress could not be loaded.", str(exc))
        snapshot = {
            "total_tasks": 0,
            "done_tasks": 0,
            "completed_resources": 0,
            "points": 0,
            "activities": 0,
            "unread_notifications": 0,
            "streak": 0,
            "achievements": [],
        }

    (
        overview_tab,
        roadmap_tab,
        resources_tab,
        library_tab,
        exercises_tab,
        notes_tab,
        progress_tab,
    ) = st.tabs(
        [
            "Overview",
            "Study plan",
            "Resources",
            "Knowledge library",
            "Exercises",
            "Study notes",
            "Progress",
        ]
    )

    with overview_tab:
        _render_overview(session_id, actor_user_id, snapshot)
    with roadmap_tab:
        show_roadmap_page(embedded=True)
    with resources_tab:
        _render_resources(session_id, actor_user_id)
    with library_tab:
        show_knowledge_page(embedded=True)
    with exercises_tab:
        show_technical_page(embedded=True)
    with notes_tab:
        _render_notes(session_id)
    with progress_tab:
        render_learning_progress(session_id, snapshot)


def _render_overview(session_id: int, actor_user_id, snapshot: dict) -> None:
    section("Your learning at a glance", ":material/insights:")
    kpi_row(
        [
            ("Activity streak", f"{snapshot['streak']} days", None),
            ("Learning points", snapshot["points"], None),
            ("Tasks complete", f"{snapshot['done_tasks']}/{snapshot['total_tasks']}", None),
            ("Resources complete", snapshot["completed_resources"], None),
            ("Unread notices", snapshot["unread_notifications"], None),
        ]
    )
    if snapshot["unread_notifications"]:
        st.info(
            f"You have {snapshot['unread_notifications']} unread notification(s).",
            icon=":material/notifications:",
        )
        if st.button("Open notifications", icon=":material/notifications:", width="stretch"):
            go_to("notifications")

    section("Recommended for your skill gaps", ":material/recommend:")
    try:
        recommendations = get_recommended_resources(session_id, actor_user_id)
    except Exception as exc:
        error_state("Recommendations could not be loaded.", str(exc))
        return
    if not recommendations:
        empty_state(
            "No curated resources yet.",
            "An administrator can add resources from Learning management.",
        )
    for item in recommendations:
        with st.container(border=True):
            st.markdown(f"**{item['title']}**")
            st.caption(
                f"{item['category']} · {item['difficulty']} · "
                f"{item['estimated_minutes']} min · {item['match_reason']}"
            )
            st.write(item["description"])
    render_achievements(snapshot["achievements"])


def _render_resources(session_id: int, actor_user_id) -> None:
    section("Curated learning resources", ":material/menu_book:")
    try:
        resources = list_resources_for_user(session_id, actor_user_id)
    except Exception as exc:
        error_state("Resources could not be loaded.", str(exc))
        return
    if not resources:
        empty_state(
            "No curated resources yet.",
            "An administrator can add resources from Learning management.",
        )
        return
    options = {f"{row['title']} · {row['category']}": row for row in resources}
    choice = st.selectbox("Resource", list(options), key="learning_resource_choice")
    selected = options[choice]
    with st.container(border=True):
        st.markdown(f"**{selected['title']}**")
        st.caption(
            f"{selected['category']} · {selected['difficulty']} · "
            f"{selected['estimated_minutes']} min"
        )
        st.write(selected["description"])
        if selected["skill_tags"]:
            st.caption("Skills: " + ", ".join(selected["skill_tags"]))
        if selected["content"]:
            with st.expander("Study notes"):
                st.write(selected["content"])
        if selected["url"]:
            st.link_button("Open external resource", selected["url"])
        st.progress(
            selected["progress_percent"] / 100,
            text=f"{selected['progress_percent']}% complete",
        )
        percent = st.slider(
            "Progress percentage",
            min_value=0,
            max_value=100,
            value=int(selected["progress_percent"]),
            step=5,
            key=f"learning_resource_progress_{selected['id']}",
        )
        action_columns = st.columns(2)
        if action_columns[0].button(
            "Save progress",
            icon=":material/save:",
            width="stretch",
            key=f"learning_resource_save_{selected['id']}",
        ):
            _save_resource_progress(
                session_id, actor_user_id, int(selected["id"]), percent, "in_progress"
            )
        if action_columns[1].button(
            "Mark complete",
            icon=":material/check_circle:",
            width="stretch",
            key=f"learning_resource_complete_{selected['id']}",
        ):
            _save_resource_progress(
                session_id, actor_user_id, int(selected["id"]), 100, "completed"
            )


def _save_resource_progress(
    session_id: int, actor_user_id, resource_id: int, percent: int, status: str
) -> None:
    try:
        result = record_resource_progress(
            resource_id=resource_id,
            session_id=session_id,
            actor_user_id=actor_user_id,
            progress_percent=percent,
            status=status,
        )
    except ValueError as exc:
        error_state("Progress was not saved.", str(exc))
    else:
        success_toast(
            "Resource completed." if result["completed"] else "Progress saved."
        )
        st.rerun()


def _render_notes(session_id: int) -> None:
    section("Study notes", ":material/note:")
    notes = list_notes(session_id, st.session_state.get("user_id"))
    options = {row["Title"]: row for row in notes}
    selected = None
    if options:
        choice = st.selectbox("Note to review", list(options), key="learning_note_choice")
        selected = options[choice]
        st.write(selected["Notes"])
        if st.button("Delete note", icon=":material/delete:", key="learning_note_delete"):
            delete_note(int(selected["id"]), session_id, st.session_state.get("user_id"))
            success_toast("Note deleted.")
            st.rerun()

    with st.form("learning_note_form", border=True):
        note_id = int(selected["id"]) if selected else None
        title = st.text_input(
            "Title",
            value=selected["Title"] if selected else "",
            key=f"learning_note_title_{note_id}",
        )
        topic = st.text_input(
            "Topic",
            value=selected["Topic"] if selected and selected["Topic"] != "General" else "",
            key=f"learning_note_topic_{note_id}",
        )
        content = st.text_area(
            "Notes",
            value=selected["Notes"] if selected else "",
            height=180,
            key=f"learning_note_content_{note_id}",
        )
        submitted = st.form_submit_button(
            "Save note", type="primary", icon=":material/save:", width="stretch"
        )
    if submitted:
        try:
            save_note(
                session_id,
                title,
                content,
                topic,
                note_id,
                actor_user_id=st.session_state.get("user_id"),
            )
        except ValueError as exc:
            error_state("Note was not saved.", str(exc))
        else:
            success_toast("Note saved.")
            st.rerun()


show_learning_center()
