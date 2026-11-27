from datetime import UTC, datetime, timedelta

import streamlit as st
from services.support_service import (
    SUPPORT_PRIORITIES,
    SUPPORT_TICKET_CATEGORIES,
    SUPPORT_TICKET_STATUSES,
    admin_reply_ticket,
    get_admin_ticket,
    get_attachment_download,
    list_admin_users,
    list_support_tickets,
    reopen_ticket,
    update_ticket_status,
)

from frontend.shared.components import (
    data_table,
    empty_state,
    error_state,
    page_header,
    section,
    success_toast,
)

ATTACHMENT_TYPES = ["pdf", "docx", "txt", "png", "jpg", "jpeg"]
_SELECTED_KEY = "admin_support_selected_ticket_id"


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


def _assignee_options(admins: list[dict], ticket: dict) -> dict[str, int | None]:
    options: dict[str, int | None] = {"Unassigned": None}
    for admin in admins:
        label = str(admin.get("name") or admin.get("id") or "Admin")
        options[label] = int(admin["id"])
    current_id = ticket.get("assigned_admin_id")
    if current_id not in (None, ""):
        try:
            numeric_id = int(current_id)
        except (TypeError, ValueError):
            numeric_id = None
        if numeric_id is not None and numeric_id not in options.values():
            label = f"Current: {ticket.get('assigned_admin_name') or current_id}"
            options[label] = numeric_id
    return options


def _current_assignee_label(options: dict[str, int | None], ticket: dict) -> str:
    current_id = ticket.get("assigned_admin_id")
    if current_id in (None, ""):
        return "Unassigned"
    try:
        numeric_id = int(current_id)
    except (TypeError, ValueError):
        return "Unassigned"
    for label, value in options.items():
        if value == numeric_id:
            return label
    return "Unassigned"


def _render_detail(account, ticket_id: int, admins: list[dict]) -> None:
    try:
        ticket = get_admin_ticket(ticket_id, actor_admin_id=account.id)
    except Exception as exc:
        error_state(str(exc))
        st.session_state.pop(_SELECTED_KEY, None)
        return

    section(
        f"{ticket.get('ticket_id', '')} · {ticket.get('subject', '')}",
        ":material/confirmation_number:",
    )
    st.caption(
        f"{ticket.get('category', '')} · {ticket.get('priority', '')} · "
        f"{ticket.get('status', '')} · Updated {_stamp(ticket.get('updated_at'))}"
    )
    with st.container(border=True):
        st.markdown("**Submitted by**")
        st.write(
            f"{ticket.get('user_name', '')} · @{ticket.get('user_username', '') or 'unknown'} · "
            f"{ticket.get('user_email', '') or 'no email'}"
        )
        st.caption(
            f"Created {_stamp(ticket.get('created_at'))} · "
            f"Resolved {_stamp(ticket.get('resolved_at'))} · "
            f"Assigned to {ticket.get('assigned_admin_name') or 'Unassigned'}"
        )
        st.write(str(ticket.get("description") or ""))

    history = ticket.get("resolution_history") or []
    if history:
        with st.expander("Status history", expanded=False):
            data_table(
                [
                    {"Change": item.get("message", ""), "When": _stamp(item.get("created_at"))}
                    for item in history
                ],
                "No status changes recorded.",
                "Ticket status history",
            )

    section("Conversation", ":material/forum:")
    messages = ticket.get("messages") or []
    if not messages:
        empty_state("No messages yet.")
    for message in messages:
        with st.container(border=True):
            st.markdown(
                f"**{message.get('author_name') or 'User'}** · {message.get('author_role', '')}"
            )
            st.caption(_stamp(message.get("created_at")))
            st.write(message.get("message", ""))

    section("Internal notes", ":material/lock:")
    notes = ticket.get("internal_notes") or []
    if not notes:
        empty_state("No internal notes yet.", "Private notes are only visible to administrators.")
    for note in notes:
        with st.container(border=True):
            st.warning("Private internal note — not visible to the user", icon=":material/lock:")
            st.markdown(f"**{note.get('author_name') or 'Administrator'}**")
            st.caption(_stamp(note.get("created_at")))
            st.write(note.get("note", ""))

    section("Attachments", ":material/attach_file:")
    attachments = ticket.get("attachments") or []
    if not attachments:
        empty_state("This ticket has no attachments.")
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
            key=f"admin_support_download_{int(item['id'])}",
        )

    section("Reply or private note", ":material/reply:")
    with st.form(f"admin_support_reply_{ticket_id}", border=True):
        message = st.text_area(
            "Message", height=120, key=f"admin_support_reply_message_{ticket_id}"
        )
        is_internal = st.checkbox(
            "Save as private internal note", key=f"admin_support_reply_internal_{ticket_id}"
        )
        uploads = st.file_uploader(
            "Attachments (optional)",
            type=ATTACHMENT_TYPES,
            accept_multiple_files=True,
            key=f"admin_support_reply_uploads_{ticket_id}",
        )
        sent = st.form_submit_button(
            "Save private note" if is_internal else "Send reply",
            type="primary",
            icon=":material/lock:" if is_internal else ":material/send:",
            width="stretch",
        )
    if sent:
        if not message.strip():
            error_state("Write a message before saving.")
        else:
            try:
                admin_reply_ticket(
                    ticket_id,
                    actor_admin_id=account.id,
                    message=message.strip(),
                    is_internal=is_internal,
                    attachments=None if is_internal else uploads or None,
                )
            except Exception as exc:
                error_state(str(exc))
            else:
                success_toast("Private note saved." if is_internal else "Reply sent.")
                st.rerun()

    section("Update ticket", ":material/tune:")
    assignee_options = _assignee_options(admins, ticket)
    assignee_labels = list(assignee_options)
    with st.form(f"admin_support_update_{ticket_id}", border=True):
        new_status = st.selectbox(
            "Status",
            SUPPORT_TICKET_STATUSES,
            index=_option_index(SUPPORT_TICKET_STATUSES, ticket.get("status")),
            key=f"admin_support_update_status_{ticket_id}",
        )
        new_priority = st.selectbox(
            "Priority",
            SUPPORT_PRIORITIES,
            index=_option_index(SUPPORT_PRIORITIES, ticket.get("priority")),
            key=f"admin_support_update_priority_{ticket_id}",
        )
        selected_assignee = st.selectbox(
            "Assigned admin",
            assignee_labels,
            index=_option_index(assignee_labels, _current_assignee_label(assignee_options, ticket)),
            key=f"admin_support_update_assignee_{ticket_id}",
        )
        updated = st.form_submit_button("Save changes", icon=":material/save:", width="stretch")
    if updated:
        try:
            update_ticket_status(
                ticket_id,
                actor_admin_id=account.id,
                status=new_status,
                priority=new_priority,
                assigned_admin_id=assignee_options[selected_assignee],
                clear_assignee=assignee_options[selected_assignee] is None,
            )
        except Exception as exc:
            error_state(str(exc))
        else:
            success_toast("Ticket updated.")
            st.rerun()

    close_column, reopen_column = st.columns(2)
    if close_column.button(
        "Close ticket",
        icon=":material/archive:",
        width="stretch",
        key=f"admin_support_close_{ticket_id}",
    ):
        try:
            update_ticket_status(ticket_id, actor_admin_id=account.id, status="Closed")
        except Exception as exc:
            error_state(str(exc))
        else:
            success_toast("Ticket closed.")
            st.rerun()
    if reopen_column.button(
        "Reopen ticket",
        icon=":material/restart_alt:",
        width="stretch",
        key=f"admin_support_reopen_{ticket_id}",
    ):
        try:
            reopen_ticket(ticket_id, actor_admin_id=account.id)
        except Exception as exc:
            error_state(str(exc))
        else:
            success_toast("Ticket reopened.")
            st.rerun()


def show_admin_support_tickets(*, render_header: bool = True) -> None:
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
            "Support tickets",
            "Search, reply to, and manage support tickets",
            ":material/support_agent:",
        )

    search = st.text_input(
        "Search tickets",
        type="search",
        placeholder="Ticket ID, subject, or username",
        key="admin_support_ticket_search",
    )
    status_column, priority_column, category_column = st.columns(3)
    status = status_column.selectbox(
        "Status", ["All", *SUPPORT_TICKET_STATUSES], key="admin_support_ticket_status_filter"
    )
    priority = priority_column.selectbox(
        "Priority", ["All", *SUPPORT_PRIORITIES], key="admin_support_ticket_priority_filter"
    )
    category = category_column.selectbox(
        "Category", ["All", *SUPPORT_TICKET_CATEGORIES], key="admin_support_ticket_category_filter"
    )
    date_column, assignee_column = st.columns(2)
    use_dates = date_column.checkbox(
        "Filter by creation date", key="admin_support_ticket_use_dates"
    )
    date_value = (
        date_column.date_input(
            "Created between",
            value=(datetime.now(UTC).date() - timedelta(days=90), datetime.now(UTC).date()),
            key="admin_support_ticket_dates",
        )
        if use_dates
        else None
    )
    date_from, date_to = _date_args(use_dates, date_value)

    try:
        admins = list_admin_users(actor_admin_id=account.id)
    except Exception as exc:
        error_state(str(exc))
        admins = []
    admin_options: dict[str, int | None] = {"All assignees": None}
    for admin in admins:
        label = str(admin.get("name") or admin.get("id") or "Admin")
        admin_options[label] = int(admin["id"])
    selected_assignee = assignee_column.selectbox(
        "Assignee", list(admin_options), key="admin_support_ticket_assignee_filter"
    )

    try:
        tickets = list_support_tickets(
            actor_admin_id=account.id,
            status=status if status != "All" else None,
            priority=priority if priority != "All" else None,
            assignee_user_id=admin_options[selected_assignee],
            search=search,
            category=category if category != "All" else None,
            date_from=date_from,
            date_to=date_to,
        )
    except Exception as exc:
        error_state(str(exc))
        return

    data_table(
        [
            {
                "Ticket": item.get("ticket_id", ""),
                "Subject": item.get("subject", ""),
                "Category": item.get("category", ""),
                "Priority": item.get("priority", ""),
                "Status": item.get("status", ""),
                "User": item.get("user_name", ""),
                "Assigned": item.get("assigned_admin_name") or "Unassigned",
                "Messages": item.get("message_count", 0),
                "New user reply": "Yes" if item.get("has_unread_user_reply") else "No",
                "Created": _stamp(item.get("created_at")),
                "Resolved": _stamp(item.get("resolved_at")),
                "Updated": _stamp(item.get("updated_at")),
            }
            for item in tickets
        ],
        "No tickets match the current filters.",
        "Support tickets",
    )
    if not tickets:
        return

    options = {
        f"{item.get('ticket_id', '')} · {item.get('subject', '')}": int(item["id"])
        for item in tickets
    }
    choice = st.selectbox("Open a ticket", list(options), key="admin_support_ticket_choice")
    open_column, clear_column = st.columns([2, 1])
    if open_column.button(
        "Open detail",
        icon=":material/arrow_forward:",
        width="stretch",
        key="admin_support_open_ticket",
    ):
        st.session_state[_SELECTED_KEY] = options[choice]
        st.rerun()
    if clear_column.button(
        "Close detail", icon=":material/close:", width="stretch", key="admin_support_clear_ticket"
    ):
        st.session_state.pop(_SELECTED_KEY, None)
        st.rerun()

    selected = st.session_state.get(_SELECTED_KEY)
    if selected:
        _render_detail(account, int(selected), admins)
