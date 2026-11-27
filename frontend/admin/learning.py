import importlib

import streamlit as st

from frontend.shared.components import (
    data_table,
    empty_state,
    error_state,
    kpi_row,
    page_header,
    success_toast,
)
from frontend.shared.navigation import PAGES


def show_admin_learning() -> None:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        st.error("Administrator access required.", icon=":material/error:")
        st.stop()

    from services import learning_service
    from services.auth_service import AuthError, require_admin

    if not hasattr(learning_service, "admin_learning_activity"):
        importlib.reload(learning_service)

    from services.learning_service import (
        ADMIN_NOTIFICATION_TYPES,
        NOTIFICATION_SEVERITIES,
        RESOURCE_CATEGORIES,
        RESOURCE_DIFFICULTIES,
        admin_broadcast_notification,
        admin_create_resource,
        admin_learning_activity,
        admin_learning_analytics,
        admin_learning_progress,
        admin_list_resources,
        admin_notification_broadcasts,
        admin_notification_summary,
        admin_resource_overview,
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
            admin_resource_overview,
            admin_create_resource,
            admin_update_resource,
            admin_set_resource_active,
            RESOURCE_CATEGORIES,
            RESOURCE_DIFFICULTIES,
            AuthError,
        )
    with analytics_tab:
        _render_analytics(
            account.id,
            admin_learning_analytics,
            admin_resource_overview,
            admin_learning_progress,
            admin_learning_activity,
            AuthError,
        )
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


def _option_index(options, value) -> int:
    try:
        return list(options).index(value)
    except (ValueError, TypeError):
        return 0


def _render_resources(
    admin_id,
    list_resources,
    resource_overview,
    create_resource,
    update_resource,
    set_resource_active,
    categories,
    difficulties,
    auth_error,
) -> None:
    st.subheader("Resource library", divider=True)
    try:
        overview = resource_overview(admin_id)
        resources = list_resources(admin_id, include_inactive=True)
    except (ValueError, auth_error) as exc:
        error_state("Learning resources could not be loaded.", str(exc))
        overview = []
        resources = []
    if not overview and resources:
        overview = [
            {
                "Title": row.get("title", ""),
                "Category": row.get("category", ""),
                "Difficulty": row.get("difficulty", ""),
                "Status": "Active" if row.get("is_active") else "Archived",
                "Views": "N/A",
            }
            for row in resources
        ]
    search = st.text_input("Search resources", key="_admin_learning_resource_search")
    category_filter = st.selectbox(
        "Category", ["all", *categories], key="_admin_learning_resource_category_filter"
    )
    status_filter = st.selectbox(
        "Status", ["all", "Active", "Archived"], key="_admin_learning_resource_status_filter"
    )
    filtered = [
        row
        for row in overview
        if (not search or search.lower() in str(row.get("title", "")).lower())
        and (category_filter == "all" or row.get("category") == category_filter)
        and (status_filter == "all" or row.get("status") == status_filter)
    ]
    data_table(
        [
            {
                "Title": row.get("title", ""),
                "Category": row.get("category", ""),
                "Difficulty": row.get("difficulty", ""),
                "Type": row.get("type", "N/A"),
                "Status": row.get("status", "N/A"),
                "Minutes": row.get("estimated_minutes", "N/A"),
                "Started": row.get("not_started", "N/A"),
                "In progress": row.get("in_progress", "N/A"),
                "Completed": row.get("completed", "N/A"),
                "Views": row.get("views", "N/A"),
                "Last viewed": row.get("last_viewed", "N/A"),
            }
            for row in filtered
        ],
        "No learning resources match the current filters.",
    )

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
        category = st.selectbox(
            "Category",
            categories,
            index=_option_index(categories, selected["category"]),
            key="admin_learning_edit_category",
        )
        difficulty = st.selectbox(
            "Difficulty",
            difficulties,
            index=_option_index(difficulties, selected["difficulty"]),
            key="admin_learning_edit_difficulty",
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


def _render_analytics(
    admin_id,
    analytics_fn,
    resource_overview,
    progress_fn,
    activity_fn,
    auth_error,
) -> None:
    try:
        snapshot = analytics_fn(admin_id)
        overview = resource_overview(admin_id)
        progress_rows = progress_fn(admin_id)
        activity_rows = activity_fn(admin_id)
    except (ValueError, auth_error) as exc:
        error_state("Learning analytics could not be loaded.", str(exc))
        return
    kpi_row(
        [
            ("Active resources", snapshot["active_resources"], None, ":material/menu_book:"),
            ("Archived resources", snapshot["archived_resources"], None, ":material/archive:"),
            ("Active learners", snapshot["active_learners"], None, ":material/groups:"),
            ("Progress records", snapshot["progress_records"], None, ":material/trending_up:"),
            ("Completion rate", f"{snapshot['completion_rate']}%", None, ":material/check_circle:"),
            ("Achievements earned", snapshot["achievements"], None, ":material/emoji_events:"),
            ("Unread notices", snapshot["unread_notifications"], None, ":material/notifications:"),
        ]
    )
    st.caption(
        f"Last learning activity: {snapshot['last_activity_at']} • "
        f"Last notification: {snapshot['last_notification_at']}"
    )
    st.subheader("Category and difficulty mix", divider=True)
    mix_columns = st.columns(2)
    with mix_columns[0]:
        data_table(
            [
                {"Category": key, "Active resources": value}
                for key, value in sorted(snapshot["resources_by_category"].items())
            ],
            "No active resource categories yet.",
        )
    with mix_columns[1]:
        data_table(
            [
                {"Difficulty": key, "Active resources": value}
                for key, value in sorted(snapshot["resources_by_difficulty"].items())
            ],
            "No active resource difficulties yet.",
        )
    st.subheader("Resource performance", divider=True)
    data_table(
        [
            {
                "Resource": row.get("title", ""),
                "Category": row.get("category", ""),
                "Status": row.get("status", ""),
                "Started": row.get("not_started", 0),
                "In progress": row.get("in_progress", 0),
                "Completed": row.get("completed", 0),
                "Views": row.get("views", "N/A"),
            }
            for row in overview
        ],
        "No learning resources yet.",
    )
    st.subheader("Progress by status", divider=True)
    data_table(
        [
            {"Status": key, "Records": value}
            for key, value in sorted(snapshot["progress_by_status"].items())
        ],
        "No resource progress recorded yet.",
    )
    st.subheader("Recent resource progress", divider=True)
    data_table(progress_rows, "No resource progress recorded yet.")
    st.subheader("Recent learning activity", divider=True)
    data_table(activity_rows, "No learning activity recorded yet.")


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
        target_page = st.selectbox(
            "Open page", target_pages, index=_option_index(target_pages, "learning_center")
        )
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
    except (ValueError, auth_error) as exc:
        error_state("Notification records could not be loaded.", str(exc))
        return
    kpi_row(
        [
            ("Total notifications", summary["total"], None, ":material/notifications:"),
            (
                "Unread notifications",
                summary.get("by_type", {}).get("admin_announcement", 0),
                None,
                ":material/mark_email_unread:",
            ),
            ("Broadcast campaigns", len(campaigns), None, ":material/campaign:"),
        ]
    )
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
                "Created": row["created_at"],
            }
            for row in campaigns
        ],
        "No broadcasts sent yet.",
    )


show_admin_learning()
