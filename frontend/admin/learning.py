import streamlit as st

from frontend.shared.components import (
    data_table,
    empty_state,
    error_state,
    page_header,
    success_toast,
)
from frontend.shared.navigation import PAGES


def show_admin_learning() -> None:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        st.error("Administrator access required.", icon=":material/error:")
        st.stop()

    from services.auth_service import AuthError, require_admin
    from services.learning_service import (
        ADMIN_NOTIFICATION_TYPES,
        NOTIFICATION_SEVERITIES,
        RESOURCE_CATEGORIES,
        RESOURCE_DIFFICULTIES,
        admin_broadcast_notification,
        admin_create_resource,
        admin_learning_analytics,
        admin_list_resources,
        admin_notification_broadcasts,
        admin_notification_summary,
        admin_set_resource_active,
        admin_update_resource,
    )

    try:
        require_admin(account.id)
    except AuthError as exc:
        st.error(str(exc), icon=":material/error:")
        st.stop()

    page_header(
        "Learning management",
        "Manage curated resources, monitor learning activity, and broadcast notices",
        ":material/school:",
    )
    resources_tab, analytics_tab, notifications_tab = st.tabs(
        ["Resources", "Learning analytics", "Notifications"]
    )
    with resources_tab:
        _render_resources(
            account.id,
            admin_list_resources,
            admin_create_resource,
            admin_update_resource,
            admin_set_resource_active,
            RESOURCE_CATEGORIES,
            RESOURCE_DIFFICULTIES,
            AuthError,
        )
    with analytics_tab:
        _render_analytics(account.id, admin_learning_analytics, AuthError)
    with notifications_tab:
        _render_notifications(
            account.id,
            admin_broadcast_notification,
            admin_notification_broadcasts,
            admin_notification_summary,
            ADMIN_NOTIFICATION_TYPES,
            NOTIFICATION_SEVERITIES,
            sorted(PAGES),
            AuthError,
        )


def _render_resources(
    admin_id,
    list_resources,
    create_resource,
    update_resource,
    set_resource_active,
    categories,
    difficulties,
    auth_error,
) -> None:
    st.subheader("Create resource", divider=True)
    with st.form("admin_learning_create_resource", border=True):
        title = st.text_input("Title", key="admin_learning_new_title")
        description = st.text_area("Description", height=80, key="admin_learning_new_description")
        category = st.selectbox("Category", categories, key="admin_learning_new_category")
        difficulty = st.selectbox("Difficulty", difficulties, key="admin_learning_new_difficulty")
        minutes = st.number_input(
            "Estimated minutes", min_value=5, max_value=600, value=15, step=5
        )
        tags = st.text_input("Skill tags (comma separated)", key="admin_learning_new_tags")
        content = st.text_area("Study notes or summary", height=140, key="admin_learning_new_content")
        url = st.text_input("Optional http(s) URL", key="admin_learning_new_url")
        submitted = st.form_submit_button(
            "Create resource", type="primary", icon=":material/add:", width="stretch"
        )
    if submitted:
        payload = {
            "title": title,
            "description": description,
            "category": category,
            "difficulty": difficulty,
            "estimated_minutes": minutes,
            "skill_tags": tags,
            "content": content,
            "url": url,
        }
        try:
            create_resource(admin_id, payload)
        except (ValueError, auth_error) as exc:
            error_state("Resource was not created.", str(exc))
        else:
            success_toast("Learning resource created and announced.")
            st.rerun()

    resources = list_resources(admin_id, include_inactive=True)
    st.subheader("Existing resources", divider=True)
    if not resources:
        empty_state("No learning resources yet.", "Create the first curated resource above.")
        return
    options = {f"{row['title']} · {row['category']}": row for row in resources}
    choice = st.selectbox("Resource to edit", list(options), key="admin_learning_resource_choice")
    selected = options[choice]
    with st.form("admin_learning_edit_resource", border=True):
        title = st.text_input("Title", value=selected["title"], key="admin_learning_edit_title")
        description = st.text_area(
            "Description",
            value=selected["description"],
            height=80,
            key="admin_learning_edit_description",
        )
        category_index = categories.index(selected["category"])
        category = st.selectbox(
            "Category", categories, index=category_index, key="admin_learning_edit_category"
        )
        difficulty_index = difficulties.index(selected["difficulty"])
        difficulty = st.selectbox(
            "Difficulty", difficulties, index=difficulty_index, key="admin_learning_edit_difficulty"
        )
        minutes = st.number_input(
            "Estimated minutes",
            min_value=5,
            max_value=600,
            value=int(selected["estimated_minutes"] or 15),
            step=5,
            key="admin_learning_edit_minutes",
        )
        tags = st.text_input(
            "Skill tags (comma separated)",
            value=", ".join(selected["skill_tags"]),
            key="admin_learning_edit_tags",
        )
        content = st.text_area(
            "Study notes or summary",
            value=selected["content"],
            height=140,
            key="admin_learning_edit_content",
        )
        url = st.text_input(
            "Optional http(s) URL", value=selected["url"], key="admin_learning_edit_url"
        )
        submitted = st.form_submit_button("Save changes", icon=":material/save:", width="stretch")
    if submitted:
        payload = {
            "title": title,
            "description": description,
            "category": category,
            "difficulty": difficulty,
            "estimated_minutes": minutes,
            "skill_tags": tags,
            "content": content,
            "url": url,
        }
        try:
            update_resource(admin_id, int(selected["id"]), payload)
        except (ValueError, auth_error) as exc:
            error_state("Resource was not updated.", str(exc))
        else:
            success_toast("Learning resource updated.")
            st.rerun()

    toggle_columns = st.columns(2)
    if selected["is_active"]:
        if toggle_columns[0].button("Archive resource", icon=":material/archive:", width="stretch"):
            try:
                set_resource_active(admin_id, int(selected["id"]), is_active=False)
            except (ValueError, auth_error) as exc:
                error_state("Resource was not archived.", str(exc))
            else:
                success_toast("Resource archived.")
                st.rerun()
    elif toggle_columns[1].button("Restore resource", icon=":material/unarchive:", width="stretch"):
        try:
            set_resource_active(admin_id, int(selected["id"]), is_active=True)
        except (ValueError, auth_error) as exc:
            error_state("Resource was not restored.", str(exc))
        else:
            success_toast("Resource restored.")
            st.rerun()


def _render_analytics(admin_id, analytics_fn, auth_error) -> None:
    try:
        snapshot = analytics_fn(admin_id)
    except auth_error as exc:
        error_state("Learning analytics could not be loaded.", str(exc))
        return
    st.metric("Active resources", snapshot["active_resources"])
    st.metric("Archived resources", snapshot["archived_resources"])
    st.metric("Active learners", snapshot["active_learners"])
    st.metric("Achievements earned", snapshot["achievements"])
    st.metric("Notifications", snapshot["notifications"])
    data_table(
        [
            {"Category": key, "Resources": value}
            for key, value in sorted(snapshot["resources_by_category"].items())
        ],
        "No active resource categories yet.",
    )
    data_table(
        [
            {"Status": key, "Resources": value}
            for key, value in sorted(snapshot["progress_by_status"].items())
        ],
        "No resource progress recorded yet.",
    )


def _render_notifications(
    admin_id,
    broadcast_fn,
    broadcasts_fn,
    summary_fn,
    notification_types,
    severities,
    target_pages,
    auth_error,
) -> None:
    st.subheader("Broadcast notice", divider=True)
    with st.form("admin_learning_broadcast", border=True):
        title = st.text_input("Notice title", key="admin_learning_broadcast_title")
        message = st.text_area("Message", height=120, key="admin_learning_broadcast_message")
        notice_type = st.selectbox("Type", notification_types, key="admin_learning_broadcast_type")
        severity = st.selectbox("Severity", severities, key="admin_learning_broadcast_severity")
        target_page = st.selectbox("Open page", target_pages, index=target_pages.index("learning_center"))
        submitted = st.form_submit_button(
            "Send to active users", type="primary", icon=":material/campaign:", width="stretch"
        )
    if submitted:
        try:
            recipients = broadcast_fn(
                admin_id,
                title=title,
                message=message,
                notification_type=notice_type,
                severity=severity,
                target_page=target_page,
            )
        except (ValueError, auth_error) as exc:
            error_state("Notice was not sent.", str(exc))
        else:
            success_toast(f"Notice sent to {recipients} workspace(s).")

    try:
        summary = summary_fn(admin_id)
        campaigns = broadcasts_fn(admin_id)
    except auth_error as exc:
        error_state("Notification records could not be loaded.", str(exc))
        return
    st.metric("Total notifications", summary["total"])
    data_table(
        [{"Type": key, "Count": value} for key, value in sorted(summary["by_type"].items())],
        "No notifications recorded yet.",
    )
    data_table(
        [
            {
                "Campaign": row["campaign"],
                "Title": row["title"],
                "Type": row["type"],
                "Recipients": row["recipients"],
                "Created": str(row["created_at"]),
            }
            for row in campaigns
        ],
        "No broadcasts sent yet.",
    )


show_admin_learning()
