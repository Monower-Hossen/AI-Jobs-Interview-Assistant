from datetime import UTC, datetime, timedelta

import streamlit as st
from services.support_service import (
    FEEDBACK_CATEGORIES,
    FEEDBACK_RATING_LABELS,
    FEEDBACK_STATUSES,
    add_feedback_note,
    get_feedback_detail,
    list_feedback,
    respond_feedback,
    set_feedback_status,
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
_SELECTED_KEY = "admin_feedback_selected_id"


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


def _render_detail(account, feedback_id: int) -> None:
    try:
        feedback = get_feedback_detail(feedback_id, actor_admin_id=account.id)
    except Exception as exc:
        error_state(str(exc))
        st.session_state.pop(_SELECTED_KEY, None)
        return

    section(
        f"Feedback #{feedback.get('feedback_id', '')}",
        ":material/feedback:",
    )
    st.caption(
        f"{feedback.get('category', '')} · {FEEDBACK_RATING_LABELS.get(int(feedback.get('rating') or 0), 'Not rated')} · "
        f"{_stamp(feedback.get('created_at'))} · Status: {feedback.get('status', '')}"
    )
    with st.container(border=True):
        st.write(str(feedback.get("details") or ""))
        st.markdown("**Submitted by**")
        st.write(
            f"{feedback.get('user_name', '')} · @{feedback.get('user_username', '') or 'unknown'} · "
            f"{feedback.get('user_email', '') or 'no email'}"
        )

    response = (feedback.get("admin_response") or "").strip()
    if response:
        with st.container(border=True):
            st.markdown("**Admin response**")
            st.caption(_stamp(feedback.get("admin_response_at")))
            st.write(response)

    section("Attachments", ":material/attach_file:")
    attachments = feedback.get("attachments") or []
    if not attachments:
        empty_state("This feedback has no attachments.")
    for item in attachments:
        try:
            from services.support_service import get_attachment_download

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
            key=f"admin_feedback_download_{int(item['id'])}",
        )

    section("Internal notes", ":material/lock:")
    notes = feedback.get("internal_notes") or []
    if not notes:
        empty_state("No internal notes yet.", "Private notes are only visible to administrators.")
    for note in notes:
        with st.container(border=True):
            st.warning("Private note — not visible to the user", icon=":material/lock:")
            st.markdown(f"**{note.get('author_name') or 'Administrator'}**")
            st.caption(_stamp(note.get("created_at")))
            st.write(note.get("note", ""))

    section("Feedback actions", ":material/tune:")
    reviewed_column, status_column = st.columns([1, 2])
    if reviewed_column.button(
        "Mark reviewed",
        icon=":material/check_circle:",
        width="stretch",
        key=f"admin_feedback_mark_reviewed_{feedback_id}",
    ):
        try:
            set_feedback_status(feedback_id, actor_admin_id=account.id, status="Reviewed")
        except Exception as exc:
            error_state(str(exc))
        else:
            success_toast("Feedback marked as reviewed.")
            st.rerun()

    with status_column.form(f"admin_feedback_status_{feedback_id}", border=True):
        new_status = st.selectbox(
            "Status",
            FEEDBACK_STATUSES,
            index=_option_index(FEEDBACK_STATUSES, feedback.get("status")),
            key=f"admin_feedback_status_select_{feedback_id}",
        )
        status_saved = st.form_submit_button(
            "Update status", icon=":material/save:", width="stretch"
        )
    if status_saved:
        try:
            set_feedback_status(feedback_id, actor_admin_id=account.id, status=new_status)
        except Exception as exc:
            error_state(str(exc))
        else:
            success_toast("Feedback status updated.")
            st.rerun()

    with st.form(f"admin_feedback_respond_{feedback_id}", border=True):
        reply = st.text_area(
            "Reply to the user",
            value=response,
            height=120,
            key=f"admin_feedback_reply_{feedback_id}",
        )
        reply_sent = st.form_submit_button(
            "Send reply", type="primary", icon=":material/send:", width="stretch"
        )
    if reply_sent:
        if not reply.strip():
            error_state("Write a reply before sending.")
        else:
            try:
                respond_feedback(
                    feedback_id,
                    actor_admin_id=account.id,
                    response=reply.strip(),
                    status="Responded",
                )
            except Exception as exc:
                error_state(str(exc))
            else:
                success_toast("Reply sent.")
                st.rerun()

    with st.form(f"admin_feedback_note_{feedback_id}", border=True):
        note = st.text_area(
            "Private internal note",
            height=100,
            key=f"admin_feedback_note_text_{feedback_id}",
        )
        note_saved = st.form_submit_button("Save note", icon=":material/lock:", width="stretch")
    if note_saved:
        if not note.strip():
            error_state("Write a note before saving.")
        else:
            try:
                add_feedback_note(feedback_id, actor_admin_id=account.id, note=note.strip())
            except Exception as exc:
                error_state(str(exc))
            else:
                success_toast("Internal note saved.")
                st.rerun()


def show_admin_user_feedback(*, render_header: bool = True) -> None:
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
            "User feedback",
            "Review feedback, add notes, and respond to users",
            ":material/feedback:",
        )

    search = st.text_input(
        "Search feedback",
        type="search",
        placeholder="Name, email, or feedback text",
        key="admin_feedback_search",
    )
    status_column, category_column, rating_column = st.columns(3)
    status = status_column.selectbox(
        "Status", ["All", *FEEDBACK_STATUSES], key="admin_feedback_status_filter"
    )
    category = category_column.selectbox(
        "Category", ["All", *FEEDBACK_CATEGORIES], key="admin_feedback_category_filter"
    )
    rating_options = [None, *FEEDBACK_RATING_LABELS]
    rating = rating_column.selectbox(
        "Rating",
        rating_options,
        format_func=lambda value: (
            "All" if value is None else FEEDBACK_RATING_LABELS.get(value, str(value))
        ),
        key="admin_feedback_rating_filter",
    )
    use_dates = st.checkbox("Filter by submission date", key="admin_feedback_use_dates")
    date_value = (
        st.date_input(
            "Submitted between",
            value=(datetime.now(UTC).date() - timedelta(days=90), datetime.now(UTC).date()),
            key="admin_feedback_dates",
        )
        if use_dates
        else None
    )
    date_from, date_to = _date_args(use_dates, date_value)

    try:
        feedback_rows = list_feedback(
            actor_admin_id=account.id,
            status=status if status != "All" else None,
            category=category if category != "All" else None,
            rating=rating,
            search=search,
            date_from=date_from,
            date_to=date_to,
        )
    except Exception as exc:
        error_state(str(exc))
        return

    data_table(
        [
            {
                "ID": item.get("feedback_id", ""),
                "User": item.get("user_name", ""),
                "Category": item.get("category", ""),
                "Rating": FEEDBACK_RATING_LABELS.get(int(item.get("rating") or 0), "Not rated"),
                "Status": item.get("status", ""),
                "Feedback": item.get("details", ""),
                "Submitted": _stamp(item.get("created_at")),
            }
            for item in feedback_rows
        ],
        "No feedback matches the current filters.",
        "User feedback",
    )
    if not feedback_rows:
        return

    options = {
        f"#{item.get('feedback_id', '')} · {item.get('user_name', '') or 'User'} · {FEEDBACK_RATING_LABELS.get(int(item.get('rating') or 0), 'Not rated')}": int(
            item["id"]
        )
        for item in feedback_rows
    }
    choice = st.selectbox("Open feedback", list(options), key="admin_feedback_choice")
    open_column, clear_column = st.columns([2, 1])
    if open_column.button(
        "Open detail", icon=":material/arrow_forward:", width="stretch", key="admin_feedback_open"
    ):
        st.session_state[_SELECTED_KEY] = options[choice]
        st.rerun()
    if clear_column.button(
        "Close detail", icon=":material/close:", width="stretch", key="admin_feedback_clear"
    ):
        st.session_state.pop(_SELECTED_KEY, None)
        st.rerun()

    selected = st.session_state.get(_SELECTED_KEY)
    if selected:
        _render_detail(account, int(selected))
