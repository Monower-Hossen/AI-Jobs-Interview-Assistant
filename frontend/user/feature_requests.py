import streamlit as st
from services.support_service import (
    FEATURE_CATEGORIES,
    create_feature_request,
    list_feature_requests,
    toggle_feature_request_vote,
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


def show_feature_requests(*, render_header: bool = True) -> None:
    if render_header:
        page_header(
            "Feature requests",
            "Suggest an improvement and support the ideas you want to see next",
            ":material/lightbulb:",
        )
    actor_user_id = st.session_state.get("user_id")
    if not actor_user_id:
        empty_state("Sign in to suggest or vote on features.")
        return

    section("Suggest a feature", ":material/add_circle:")
    with st.form("feature_request_form", border=True):
        title = st.text_input(
            "Title", max_chars=120, placeholder="Short name for your idea", key="feature_title"
        )
        description = st.text_area(
            "Description",
            height=140,
            placeholder="What problem would this solve?",
            key="feature_description",
        )
        category = st.selectbox("Category", list(FEATURE_CATEGORIES), key="feature_category")
        uploads = st.file_uploader(
            "Attachments (optional)",
            type=ATTACHMENT_TYPES,
            accept_multiple_files=True,
            key="feature_attachments",
        )
        submitted = st.form_submit_button(
            "Submit request", type="primary", icon=":material/send:", width="stretch"
        )

    if submitted:
        if not title.strip() or not description.strip():
            error_state("Title and description are required.")
        else:
            try:
                create_feature_request(
                    int(actor_user_id),
                    title=title.strip(),
                    description=description.strip(),
                    category=category,
                    attachments=uploads or None,
                )
            except Exception as exc:
                error_state("Feature request could not be submitted.", str(exc))
            else:
                success_toast("Feature request submitted.")
                st.rerun()

    section("Requests from the community", ":material/how_to_vote:")
    try:
        rows = list_feature_requests(int(actor_user_id))
    except Exception as exc:
        error_state("Feature requests could not be loaded.", str(exc))
        return
    if not rows:
        empty_state("No feature requests yet.", "Be the first to share an idea for the platform.")
        return

    for item in rows:
        with st.container(border=True):
            st.markdown(f"**{item.get('request_id', '')}** · {item.get('title', '')}")
            st.caption(f"{item.get('category', '')} · {_stamp(item.get('created_at'))}")
            st.write(item.get("description", ""))
            if item.get("status"):
                st.caption(f"Status: {item['status']}")
            if item.get("admin_notes"):
                st.caption(f"Admin notes: {item['admin_notes']}")
            label = "Unvote" if item.get("voted") else "Vote"
            if st.button(
                f"{label} · {item.get('vote_count', 0)}",
                icon=":material/how_to_vote:" if not item.get("voted") else ":material/undo:",
                width="stretch",
                key=f"feature_vote_{item['id']}",
            ):
                try:
                    voted = toggle_feature_request_vote(int(item["id"]), int(actor_user_id))
                except Exception as exc:
                    error_state("Vote could not be updated.", str(exc))
                else:
                    success_toast("Vote added." if voted else "Vote removed.")
                    st.rerun()
