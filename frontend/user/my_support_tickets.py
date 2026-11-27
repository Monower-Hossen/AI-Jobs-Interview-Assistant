import streamlit as st
from services.support_service import (
    SUPPORT_PRIORITIES,
    SUPPORT_TICKET_STATUSES,
    add_ticket_message,
    close_ticket,
    get_attachment_download,
    get_user_ticket,
    list_my_tickets,
)

from frontend.shared.components import (
    data_table,
    empty_state,
    error_state,
    kpi_row,
    page_header,
    section,
    success_toast,
)
from frontend.shared.navigation import go_to

ATTACHMENT_TYPES = ["pdf", "docx", "txt", "png", "jpg", "jpeg"]
_SELECTED_KEY = "support_selected_ticket_id"


def _actor() -> int | None:
    value = st.session_state.get("user_id")
    return int(value) if value else None


def _stamp(value) -> str:
    return str(value or "-")


def show_my_support_tickets(*, render_header: bool = True) -> None:
    if render_header:
        page_header(
            "My support tickets",
            "Follow conversations with the support team and reply from one place",
            ":material/support:",
        )
    actor_user_id = _actor()
    if not actor_user_id:
        empty_state("Sign in to view your support tickets.")
        return
    selected = st.session_state.get(_SELECTED_KEY)
    if selected:
        _render_detail(actor_user_id, int(selected))
        return
    _render_list(actor_user_id)


def _render_list(actor_user_id: int) -> None:
    try:
        tickets = list_my_tickets(actor_user_id)
    except Exception as exc:
        error_state("Support tickets could not be loaded.", str(exc))
        return

    kpi_row(
        [
            ("Total", len(tickets), None, ":material/confirmation_number:"),
            ("Open", sum(item["status"] == "Open" for item in tickets), None, ":material/mail:"),
            (
                "In progress",
                sum(item["status"] == "In Progress" for item in tickets),
                None,
                ":material/pending_actions:",
            ),
            (
                "Resolved",
                sum(item["status"] == "Resolved" for item in tickets),
                None,
                ":material/check_circle:",
            ),
        ]
    )

    if st.button(
        "New ticket",
        type="primary",
        icon=":material/add:",
        width="stretch",
        key="support_new_ticket",
    ):
        go_to("submit_ticket")

    section("Your tickets", ":material/list:")
    status_filter, priority_filter, search_filter = st.columns(3)
    status = status_filter.selectbox(
        "Status", ["All", *SUPPORT_TICKET_STATUSES], key="support_filter_status"
    )
    priority = priority_filter.selectbox(
        "Priority", ["All", *SUPPORT_PRIORITIES], key="support_filter_priority"
    )
    search = search_filter.text_input("Search", type="search", key="support_filter_search")
    needle = search.strip().lower()

    def _matches(item: dict) -> bool:
        if status != "All" and item.get("status") != status:
            return False
        if priority != "All" and item.get("priority") != priority:
            return False
        haystack = " ".join(
            str(item.get(key, "")) for key in ("ticket_id", "subject", "category")
        ).lower()
        return not needle or needle in haystack

    filtered = [item for item in tickets if _matches(item)]
    if not filtered:
        empty_state("No tickets match your filters.", "Submit a new ticket to contact support.")
        return

    rows = [
        {
            "Ticket": item.get("ticket_id", ""),
            "Subject": item.get("subject", ""),
            "Category": item.get("category", ""),
            "Priority": item.get("priority", ""),
            "Status": item.get("status", ""),
            "Updated": _stamp(item.get("updated_at")),
            "New reply": "Yes" if item.get("has_unread_admin_reply") else "No",
        }
        for item in filtered
    ]
    data_table(rows, "No support tickets yet.", "Your support tickets")

    options = {
        f"{item.get('ticket_id', '')} · {item.get('subject', '')}": int(item["id"])
        for item in filtered
    }
    choice = st.selectbox("Open a ticket", list(options), key="support_ticket_choice")
    if st.button(
        "Open detail",
        icon=":material/arrow_forward:",
        width="stretch",
        key="support_open_detail",
    ):
        st.session_state[_SELECTED_KEY] = options[choice]
        st.rerun()


def _render_detail(actor_user_id: int, ticket_id: int) -> None:
    try:
        ticket = get_user_ticket(ticket_id, actor_user_id)
    except Exception as exc:
        error_state("Ticket could not be loaded.", str(exc))
        return

    if st.button(
        "Back to tickets", icon=":material/arrow_back:", width="stretch", key="support_back"
    ):
        st.session_state.pop(_SELECTED_KEY, None)
        st.rerun()

    section(
        f"{ticket.get('ticket_id', '')} · {ticket.get('subject', '')}",
        ":material/confirmation_number:",
    )
    st.caption(
        f"{ticket.get('category', '')} · {ticket.get('priority', '')} · "
        f"{ticket.get('status', '')} · Updated {_stamp(ticket.get('updated_at'))}"
    )

    section("Conversation", ":material/forum:")
    messages = ticket.get("messages") or []
    if not messages:
        empty_state("No messages yet.")
    for message in messages:
        with st.container(border=True):
            st.markdown(
                f"**{message.get('author_name') or 'Support'}** · "
                f"{message.get('author_role', '')}"
            )
            st.caption(_stamp(message.get("created_at")))
            st.write(message.get("message", ""))

    section("Attachments", ":material/attach_file:")
    attachments = ticket.get("attachments") or []
    if not attachments:
        empty_state("This ticket has no attachments.")
    for item in attachments:
        try:
            download = get_attachment_download(int(item["id"]), actor_user_id=actor_user_id)
        except Exception as exc:
            error_state(f"Could not prepare {item.get('file_name', 'attachment')}.", str(exc))
            continue
        st.download_button(
            str(download["file_name"]),
            data=download["content"],
            file_name=str(download["file_name"]),
            mime=str(download["mime_type"]),
            icon=":material/download:",
            width="stretch",
            key=f"support_download_{item['id']}",
        )

    section("Reply", ":material/reply:")
    with st.form("support_reply_form", border=True):
        reply = st.text_area("Your message", height=120, key="support_reply_message")
        uploads = st.file_uploader(
            "Attachments (optional)",
            type=ATTACHMENT_TYPES,
            accept_multiple_files=True,
            key="support_reply_uploads",
        )
        sent = st.form_submit_button(
            "Send reply", type="primary", icon=":material/send:", width="stretch"
        )
    if sent:
        if not reply.strip():
            error_state("Write a message before sending.")
        else:
            try:
                add_ticket_message(
                    ticket_id,
                    actor_user_id,
                    message=reply.strip(),
                    attachments=uploads or None,
                )
            except Exception as exc:
                error_state("Reply could not be sent.", str(exc))
            else:
                success_toast("Reply sent.")
                st.rerun()

    if ticket.get("status") not in ("Resolved", "Closed"):
        if st.button(
            "Close ticket",
            icon=":material/check_circle:",
            width="stretch",
            key=f"support_close_{ticket_id}",
        ):
            try:
                close_ticket(ticket_id, actor_user_id)
            except Exception as exc:
                error_state("Ticket could not be closed.", str(exc))
            else:
                success_toast("Ticket closed.")
                st.rerun()
