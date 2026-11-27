import streamlit as st
from services.support_service import (
    FEEDBACK_CATEGORIES,
    FEEDBACK_RATING_LABELS,
    create_feedback,
    list_my_feedback,
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


def show_my_feedback(*, render_header: bool = True) -> None:
    if render_header:
        page_header(
            "My feedback",
            "Share what is working and what could improve, then follow the team's response",
            ":material/feedback:",
        )
    actor_user_id = st.session_state.get("user_id")
    if not actor_user_id:
        empty_state("Sign in to share feedback.")
        return

    section("Send feedback", ":material/edit_note:")
    with st.form("feedback_form", border=True):
        category = st.selectbox("Category", list(FEEDBACK_CATEGORIES), key="feedback_category")
        rating = st.selectbox(
            "Rating",
            list(FEEDBACK_RATING_LABELS),
            format_func=lambda value: FEEDBACK_RATING_LABELS[value],
            key="feedback_rating",
        )
        details = st.text_area(
            "Details",
            height=150,
            placeholder="What should we keep, change or fix?",
            key="feedback_details",
        )
        uploads = st.file_uploader(
            "Attachments (optional)",
            type=ATTACHMENT_TYPES,
            accept_multiple_files=True,
            key="feedback_attachments",
        )
        submitted = st.form_submit_button(
            "Send feedback", type="primary", icon=":material/send:", width="stretch"
        )

    if submitted:
        if not details.strip():
            error_state("Add a few details before sending.")
        else:
            try:
                create_feedback(
                    int(actor_user_id),
                    category=category,
                    rating=int(rating),
                    details=details.strip(),
                    attachments=uploads or None,
                )
            except Exception as exc:
                error_state("Feedback could not be sent.", str(exc))
            else:
                success_toast("Thanks for sharing your feedback.")
                st.rerun()

    section("Previous feedback", ":material/history:")
    try:
        rows = list_my_feedback(int(actor_user_id))
    except Exception as exc:
        error_state("Feedback could not be loaded.", str(exc))
        return
    if not rows:
        empty_state("You have not shared feedback yet.", "Your notes help us improve the platform.")
        return
    for item in rows:
        with st.container(border=True):
            st.markdown(
                f"**{item.get('feedback_id', '')}** · {item.get('category', '')} · "
                f"{item.get('rating_label', '')}"
            )
            st.caption(f"{item.get('status', '')} · {_stamp(item.get('created_at'))}")
            st.write(item.get("details", ""))
            if item.get("admin_response"):
                with st.container(border=True):
                    st.markdown("**Response from the team**")
                    st.write(item["admin_response"])
                    st.caption(_stamp(item.get("admin_response_at")))
