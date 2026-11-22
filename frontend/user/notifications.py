import streamlit as st
from services.auth_service import owned_session_id
from services.learning_service import (
    delete_notification,
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
    refresh_task_reminders,
)

from frontend.shared.components import empty_state, error_state, page_header, success_toast
from frontend.shared.navigation import go_to


def _workspace_id() -> int | None:
    token = st.session_state.get("user_session_id")
    if not token:
        return None
    return owned_session_id(token, st.session_state.get("user_id"))


def show_notifications() -> None:
    page_header(
        "Notifications",
        "Task reminders, achievements, new resources, and account announcements",
        ":material/notifications:",
    )
    session_id = _workspace_id()
    if not session_id:
        empty_state("No workspace found.")
        return

    try:
        refresh_task_reminders(session_id, st.session_state.get("user_id"))
    except Exception as exc:
        error_state("Reminders could not be refreshed.", str(exc))

    actor_user_id = st.session_state.get("user_id")
    unread_tab, all_tab = st.tabs(["Unread", "All"])
    _render_notifications(unread_tab, session_id, actor_user_id, unread_only=True)
    _render_notifications(all_tab, session_id, actor_user_id, unread_only=False)


def _render_notifications(tab, session_id: int, actor_user_id, *, unread_only: bool) -> None:
    scope = "unread" if unread_only else "all"
    with tab:
        try:
            rows = list_notifications(
                session_id, actor_user_id, unread_only=unread_only, limit=50
            )
        except Exception as exc:
            error_state("Notifications could not be loaded.", str(exc))
            return
        action_columns = st.columns([1, 3])
        if action_columns[0].button(
            "Mark all read",
            icon=":material/done_all:",
            width="stretch",
            key=f"notifications_mark_all_{scope}",
            disabled=not rows,
        ):
            try:
                count = mark_all_notifications_read(session_id, actor_user_id)
            except Exception as exc:
                error_state("Notifications were not updated.", str(exc))
            else:
                success_toast(f"Marked {count} notification(s) as read.")
                st.rerun()
        if action_columns[1].button(
            "Open learning center",
            icon=":material/school:",
            width="stretch",
            key=f"notifications_learning_{scope}",
        ):
            go_to("learning_center")

        if not rows:
            empty_state(
                "No unread notifications." if unread_only else "No notifications yet.",
                "Reminders and achievements appear here as you learn.",
            )
            return

        for row in rows:
            with st.container(border=True):
                status = "NEW" if not row["is_read"] else "READ"
                st.caption(f"{row['severity'].upper()} · {row['notification_type']} · {status}")
                st.markdown(f"**{row['title']}**")
                st.write(row["message"])
                st.caption(str(row["created_at"]))
                button_columns = st.columns(3)
                if (
                    not row["is_read"]
                    and button_columns[0].button(
                        "Mark read",
                        key=f"notification_{scope}_read_{row['id']}",
                        width="stretch",
                    )
                ):
                    try:
                        mark_notification_read(int(row["id"]), session_id, actor_user_id)
                    except Exception as exc:
                        error_state("Notification was not updated.", str(exc))
                    else:
                        st.rerun()
                target = str(row["target_page"] or "")
                if target and button_columns[1].button(
                    "Open",
                    key=f"notification_{scope}_open_{row['id']}",
                    width="stretch",
                ):
                    go_to(target)
                if button_columns[2].button(
                    "Dismiss",
                    icon=":material/close:",
                    key=f"notification_{scope}_delete_{row['id']}",
                    width="stretch",
                ):
                    try:
                        delete_notification(int(row["id"]), session_id, actor_user_id)
                    except Exception as exc:
                        error_state("Notification was not removed.", str(exc))
                    else:
                        success_toast("Notification dismissed.")
                        st.rerun()


show_notifications()
