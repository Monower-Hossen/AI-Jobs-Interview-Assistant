import streamlit as st
from services.support_service import (
    SUPPORT_PRIORITIES,
    SUPPORT_TICKET_CATEGORIES,
    create_ticket,
)

from frontend.shared.components import empty_state, error_state, page_header, section, success_toast
from frontend.shared.navigation import go_to

ATTACHMENT_TYPES = ["pdf", "docx", "txt", "png", "jpg", "jpeg"]


def show_submit_ticket(*, render_header: bool = True) -> None:
    if render_header:
        page_header(
            "Submit a ticket",
            "Tell the support team what happened and follow the conversation in My support tickets",
            ":material/support_agent:",
        )
    actor_user_id = st.session_state.get("user_id")
    if not actor_user_id:
        empty_state("Sign in to submit a support ticket.")
        return

    section("Ticket details", ":material/edit_note:")
    with st.form("support_ticket_form", border=True):
        subject = st.text_input(
            "Subject",
            max_chars=150,
            placeholder="Brief summary of your request",
            key="support_ticket_subject",
        )
        category = st.selectbox(
            "Category", list(SUPPORT_TICKET_CATEGORIES), key="support_ticket_category"
        )
        priority = st.selectbox(
            "Priority", list(SUPPORT_PRIORITIES), index=1, key="support_ticket_priority"
        )
        description = st.text_area(
            "Description",
            height=180,
            placeholder="Include any details that will help us understand the issue",
            key="support_ticket_description",
        )
        uploads = st.file_uploader(
            "Attachments (optional)",
            type=ATTACHMENT_TYPES,
            accept_multiple_files=True,
            key="support_ticket_attachments",
        )
        submitted = st.form_submit_button(
            "Submit ticket", type="primary", icon=":material/send:", width="stretch"
        )

    if not submitted:
        return
    if not subject.strip() or not description.strip():
        error_state("Subject and description are required.", "Add both fields before submitting.")
        return
    try:
        ticket = create_ticket(
            int(actor_user_id),
            subject=subject.strip(),
            category=category,
            description=description.strip(),
            priority=priority,
            attachments=uploads or None,
        )
    except Exception as exc:
        error_state("Ticket could not be submitted.", str(exc))
        return
    success_toast(f"Ticket {ticket.get('ticket_id', '')} submitted.")
    go_to("my_support_tickets")
