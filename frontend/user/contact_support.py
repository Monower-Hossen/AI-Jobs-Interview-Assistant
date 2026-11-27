import streamlit as st
from services.support_service import (
    CONTACT_CATEGORIES,
    create_contact_message,
    list_my_contact_messages,
)

from frontend.shared.components import (
    empty_state,
    error_state,
    page_header,
    section,
    success_toast,
)

ATTACHMENT_TYPES = ["pdf", "docx", "txt", "png", "jpg", "jpeg"]


def _stamp(value) -> str:
    return str(value or "-")


def show_contact_support(*, render_header: bool = True) -> None:
    if render_header:
        page_header(
            "Contact support",
            "Send a message to the team and keep track of the replies here",
            ":material/contact_support:",
        )
    actor_user_id = st.session_state.get("user_id")
    if not actor_user_id:
        empty_state("Sign in to contact support.")
        return

    section("New message", ":material/edit_note:")
    with st.form("contact_support_form", border=True):
        subject = st.text_input(
            "Subject",
            max_chars=150,
            placeholder="What do you need help with?",
            key="contact_subject",
        )
        category = st.selectbox("Category", list(CONTACT_CATEGORIES), key="contact_category")
        message = st.text_area(
            "Message",
            height=180,
            placeholder="Tell us as much as you can so we can help quickly",
            key="contact_message",
        )
        uploads = st.file_uploader(
            "Attachments (optional)",
            type=ATTACHMENT_TYPES,
            accept_multiple_files=True,
            key="contact_attachments",
        )
        submitted = st.form_submit_button(
            "Send message", type="primary", icon=":material/send:", width="stretch"
        )

    if submitted:
        if not subject.strip() or not message.strip():
            error_state("Subject and message are required.")
        else:
            try:
                create_contact_message(
                    int(actor_user_id),
                    subject=subject.strip(),
                    category=category,
                    message=message.strip(),
                    attachments=uploads or None,
                )
            except Exception as exc:
                error_state("Message could not be sent.", str(exc))
            else:
                success_toast("Message sent to the support team.")
                st.rerun()

    section("Your messages", ":material/inbox:")
    try:
        rows = list_my_contact_messages(int(actor_user_id))
    except Exception as exc:
        error_state("Contact messages could not be loaded.", str(exc))
        return
    if not rows:
        empty_state("You have not contacted support yet.", "Sent messages and replies appear here.")
        return
    for item in rows:
        with st.container(border=True):
            st.markdown(f"**{item.get('reference_id', '')}** · {item.get('subject', '')}")
            st.caption(
                f"{item.get('category', '')} · {item.get('status', '')} · "
                f"{_stamp(item.get('created_at'))}"
            )
            st.write(item.get("message", ""))
            if item.get("admin_reply"):
                with st.container(border=True):
                    st.markdown("**Reply from the team**")
                    st.write(item["admin_reply"])
                    st.caption(f"Replied {_stamp(item.get('replied_at'))}")
