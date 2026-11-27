from datetime import UTC, datetime, timedelta

import streamlit as st
from services.support_service import (
    CONTACT_STATUSES,
    delete_contact_message,
    get_attachment_download,
    get_contact_message,
    list_contact_messages,
    reply_contact_message,
    set_contact_read,
    set_contact_status,
)

from frontend.shared.components import (
    confirm_delete,
    data_table,
    empty_state,
    error_state,
    page_header,
    section,
    success_toast,
)

SELECTED_KEY = "admin_contact_selected_id"
DELETE_TARGET_KEY = "_admin_contact_delete_target"
DELETE_LABEL_KEY = "_admin_contact_delete_label"


def _stamp(value: object) -> str:
    return str(value or "-")


def _option_index(options: object, value: object) -> int:
    try:
        return list(options).index(value)
    except (ValueError, TypeError):
        return 0


def _date_args(enabled: bool, value: object) -> tuple[object, object]:
    if not enabled:
        return None, None
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return value[0], value[1]
    return value, None


def _render_detail(account, message_id: int) -> None:
    try:
        message = get_contact_message(message_id, actor_admin_id=account.id)
    except Exception as exc:
        error_state(str(exc))
        st.session_state.pop(SELECTED_KEY, None)
        return

    section(
        f"Message #{message.get('reference_id', '')} · {message.get('subject', '')}",
        ":material/mail:",
    )
    st.caption(
        f"From {message.get('user_name', '')} · {message.get('user_email', '') or 'no email'} · "
        f"{_stamp(message.get('created_at'))} · Status: {message.get('status', '')}"
    )
    with st.container(border=True):
        st.write(str(message.get("message") or ""))

    if message.get("is_read_by_admin"):
        st.caption("Marked as read", icon=":material/done:")
    else:
        st.warning("Unread", icon=":material/mark_email_unread:")

    admin_response = (message.get("admin_response") or "").strip()
    if admin_response:
        with st.container(border=True):
            st.markdown("**Admin response**")
            st.caption(_stamp(message.get("replied_at")))
            st.write(admin_response)

    section("Attachments", ":material/attach_file:")
    attachments = message.get("attachments") or []
    if not attachments:
        empty_state("This message has no attachments.")
    for item in attachments:
        try:
            download = get_attachment_download(int(item["id"]), actor_admin_id=account.id)
        except Exception as exc:
            error_state(str(exc))
            continue
        file_name = str(download.get("file_name") or item.get("file_name") or "attachment")
        st.download_button(
            file_name,
            data=download.get("content") or b"",
            file_name=file_name,
            mime=str(download.get("mime_type") or "application/octet-stream"),
            icon=":material/download:",
            width="stretch",
            key=f"admin_contact_download_{int(item['id'])}",
        )

    section("Manage message", ":material/tune:")
    read_column, status_column = st.columns([1, 2])
    if message.get("is_read_by_admin"):
        if read_column.button(
            "Mark unread",
            icon=":material/mark_email_unread:",
            width="stretch",
            key=f"admin_contact_unread_{message_id}",
        ):
            try:
                set_contact_read(message_id, actor_admin_id=account.id, is_read=False)
            except Exception as exc:
                error_state(str(exc))
            else:
                success_toast("Message marked as unread.")
                st.rerun()
    elif read_column.button(
        "Mark read",
        icon=":material/done:",
        width="stretch",
        key=f"admin_contact_read_{message_id}",
    ):
        try:
            set_contact_read(message_id, actor_admin_id=account.id, is_read=True)
        except Exception as exc:
            error_state(str(exc))
        else:
            success_toast("Message marked as read.")
            st.rerun()

    with status_column.form(f"admin_contact_status_{message_id}", border=True):
        new_status = st.selectbox(
            "Status",
            CONTACT_STATUSES,
            index=_option_index(CONTACT_STATUSES, message.get("status")),
            key=f"admin_contact_status_select_{message_id}",
        )
        status_saved = st.form_submit_button(
            "Update status", icon=":material/save:", width="stretch"
        )
    if status_saved:
        try:
            set_contact_status(message_id, new_status, actor_admin_id=account.id)
        except Exception as exc:
            error_state(str(exc))
        else:
            success_toast("Message status updated.")
            st.rerun()

    with st.form(f"admin_contact_reply_{message_id}", border=True):
        reply = st.text_area(
            "Reply to the user",
            value=admin_response,
            height=120,
            key=f"admin_contact_reply_text_{message_id}",
        )
        reply_sent = st.form_submit_button(
            "Send reply", type="primary", icon=":material/send:", width="stretch"
        )
    if reply_sent:
        if not reply.strip():
            error_state("Write a reply before sending.")
        else:
            try:
                reply_contact_message(
                    message_id,
                    actor_admin_id=account.id,
                    reply=reply.strip(),
                )
            except Exception as exc:
                error_state(str(exc))
            else:
                success_toast("Reply sent.")
                st.rerun()

    if st.button(
        "Delete message",
        icon=":material/delete:",
        width="stretch",
        key=f"admin_contact_delete_start_{message_id}",
    ):
        st.session_state[DELETE_TARGET_KEY] = int(message_id)
        st.session_state[DELETE_LABEL_KEY] = (
            f"#{message.get('reference_id', '')} · {message.get('subject', '')}"
        )
        key = f"_admin_contact_delete_{int(message_id)}"
        st.session_state[key] = False
        st.session_state[f"{key}_ack"] = False
        st.session_state[f"{key}_resolved"] = False


def _render_delete_confirmation(account) -> None:
    target_id = st.session_state.get(DELETE_TARGET_KEY)
    if target_id is None:
        return
    key = f"_admin_contact_delete_{int(target_id)}"
    if st.session_state.get(f"{key}_resolved"):
        st.session_state.pop(f"{key}_resolved", None)
        if st.session_state.get(key):
            try:
                delete_contact_message(int(target_id), actor_admin_id=account.id)
            except Exception as exc:
                error_state(str(exc))
                st.session_state.pop(DELETE_TARGET_KEY, None)
                st.session_state.pop(DELETE_LABEL_KEY, None)
                st.session_state.pop(key, None)
                st.session_state.pop(f"{key}_ack", None)
                st.rerun()
            success_toast("Contact message deleted.")
        st.session_state.pop(DELETE_TARGET_KEY, None)
        st.session_state.pop(DELETE_LABEL_KEY, None)
        st.rerun()
    confirm_delete(st.session_state.get(DELETE_LABEL_KEY, str(target_id)), key=key)


def show_admin_contact_messages(*, render_header: bool = True) -> None:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        st.error("Administrator access required.", icon=":material/error:")
        st.stop()

    from services.auth_service import AuthError, require_admin

    try:
        require_admin(account.id)
    except AuthError as exc:
        st.error(str(exc), icon=":material/error:")
        st.stop()

    if render_header:
        page_header(
            "Contact messages",
            "Search, answer, and manage user messages",
            ":material/mail:",
        )

    search = st.text_input(
        "Search messages",
        type="search",
        placeholder="Sender, email, subject, or message",
        key="admin_contact_search",
    )
    read_column, status_column = st.columns(2)
    read_filter = read_column.selectbox(
        "Read state", ["All", "Unread", "Read"], key="admin_contact_read_filter"
    )
    status = status_column.selectbox(
        "Status", ["All", *CONTACT_STATUSES], key="admin_contact_status_filter"
    )
    use_dates = st.checkbox("Filter by message date", key="admin_contact_use_dates")
    date_value = (
        st.date_input(
            "Received between",
            value=(datetime.now(UTC).date() - timedelta(days=90), datetime.now(UTC).date()),
            key="admin_contact_dates",
        )
        if use_dates
        else None
    )
    date_from, date_to = _date_args(use_dates, date_value)

    try:
        messages = list_contact_messages(
            actor_admin_id=account.id,
            search=search,
            read_filter=read_filter.lower() if read_filter != "All" else None,
            status=status if status != "All" else None,
            date_from=date_from,
            date_to=date_to,
        )
    except Exception as exc:
        error_state(str(exc))
        return

    data_table(
        [
            {
                "ID": item.get("reference_id", ""),
                "From": item.get("user_name", ""),
                "Email": item.get("user_email", ""),
                "Subject": item.get("subject", ""),
                "Status": item.get("status", ""),
                "Read": "Yes" if item.get("is_read_by_admin") else "No",
                "Received": _stamp(item.get("created_at")),
            }
            for item in messages
        ],
        "No contact messages match the current filters.",
        "Contact messages",
    )
    if not messages:
        return

    options = {
        f"#{item.get('reference_id', '')} · {item.get('subject', '')}": int(item["id"])
        for item in messages
    }
    choice = st.selectbox("Open a message", list(options), key="admin_contact_choice")
    open_column, clear_column = st.columns([2, 1])
    if open_column.button(
        "Open detail", icon=":material/arrow_forward:", width="stretch", key="admin_contact_open"
    ):
        st.session_state[SELECTED_KEY] = options[choice]
        st.rerun()
    if clear_column.button(
        "Close detail", icon=":material/close:", width="stretch", key="admin_contact_clear"
    ):
        st.session_state.pop(SELECTED_KEY, None)
        st.rerun()

    selected = st.session_state.get(SELECTED_KEY)
    if selected:
        _render_detail(account, int(selected))
    _render_delete_confirmation(account)
