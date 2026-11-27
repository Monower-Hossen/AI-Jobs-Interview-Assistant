from datetime import UTC, datetime, timedelta

import streamlit as st
from services.support_service import (
    FEATURE_CATEGORIES,
    FEATURE_STATUS_ALIASES,
    FEATURE_STATUSES,
    get_attachment_download,
    get_feature_request,
    list_admin_feature_requests,
    update_feature_request,
)

from frontend.shared.components import (
    data_table,
    empty_state,
    error_state,
    page_header,
    section,
    success_toast,
)

_SELECTED_KEY = "admin_feature_selected_id"


def _stamp(value: object) -> str:
    return str(value or "-")


def _status_index(options: object, value: object) -> int:
    raw = str(value or "").strip()
    normalized = FEATURE_STATUS_ALIASES.get(raw.lower(), raw)
    try:
        return list(options).index(normalized)
    except ValueError:
        return 0


def _date_args(enabled: bool, value: object) -> tuple[object, object]:
    if not enabled:
        return None, None
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return value[0], value[1]
    return value, None


def _render_detail(account, request_id: int) -> None:
    try:
        request = get_feature_request(request_id, actor_admin_id=account.id)
    except Exception as exc:
        error_state(str(exc))
        st.session_state.pop(_SELECTED_KEY, None)
        return

    section(
        f"Request #{request.get('request_id', '')} · {request.get('title', '')}",
        ":material/lightbulb:",
    )
    st.caption(
        f"{request.get('category', '')} · {request.get('status', '')} · "
        f"{request.get('vote_count', 0)} votes · Submitted {_stamp(request.get('created_at'))}"
    )
    with st.container(border=True):
        st.markdown("**Description**")
        st.write(str(request.get("description") or ""))
        st.markdown("**Requested by**")
        st.write(
            f"{request.get('user_name', '')} · @{request.get('user_username', '') or 'unknown'}"
        )

    section("Attachments", ":material/attach_file:")
    attachments = request.get("attachments") or []
    if not attachments:
        empty_state("This request has no attachments.")
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
            key=f"admin_feature_download_{int(item['id'])}",
        )

    section("Review request", ":material/tune:")
    with st.form(f"admin_feature_update_{request_id}", border=True):
        new_status = st.selectbox(
            "Status",
            FEATURE_STATUSES,
            index=_status_index(FEATURE_STATUSES, request.get("status")),
            key=f"admin_feature_status_{request_id}",
        )
        notes = st.text_area(
            "Admin notes",
            value=str(request.get("admin_notes") or ""),
            height=100,
            key=f"admin_feature_notes_{request_id}",
        )
        published = st.checkbox(
            "Include in public roadmap",
            value=bool(request.get("published")),
            key=f"admin_feature_published_{request_id}",
        )
        announced = st.checkbox(
            "Announced to users",
            value=bool(request.get("announced")),
            key=f"admin_feature_announced_{request_id}",
        )
        saved = st.form_submit_button("Save changes", icon=":material/save:", width="stretch")
    if saved:
        try:
            update_feature_request(
                request_id,
                actor_admin_id=account.id,
                status=new_status,
                admin_notes=notes.strip() or None,
                published=published,
                announced=announced,
            )
        except Exception as exc:
            error_state(str(exc))
        else:
            success_toast("Feature request updated.")
            st.rerun()


def show_admin_feature_requests(*, render_header: bool = True) -> None:
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
            "Feature requests",
            "Prioritize requests and publish roadmap updates",
            ":material/lightbulb:",
        )

    search = st.text_input(
        "Search requests",
        type="search",
        placeholder="Title, description, category, or username",
        key="admin_feature_search",
    )
    status_column, category_column = st.columns(2)
    status = status_column.selectbox(
        "Status", ["All", *FEATURE_STATUSES], key="admin_feature_status_filter"
    )
    category = category_column.selectbox(
        "Category", ["All", *FEATURE_CATEGORIES], key="admin_feature_category_filter"
    )
    use_dates = st.checkbox("Filter by request date", key="admin_feature_use_dates")
    date_value = (
        st.date_input(
            "Requested between",
            value=(datetime.now(UTC).date() - timedelta(days=90), datetime.now(UTC).date()),
            key="admin_feature_dates",
        )
        if use_dates
        else None
    )
    date_from, date_to = _date_args(use_dates, date_value)

    try:
        requests = list_admin_feature_requests(
            actor_admin_id=account.id,
            status=status if status != "All" else None,
            category=category if category != "All" else None,
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
                "ID": item.get("request_id", ""),
                "Title": item.get("title", ""),
                "Category": item.get("category", ""),
                "Votes": item.get("vote_count", 0),
                "Status": item.get("status", ""),
                "Roadmap": "Yes" if item.get("published") else "No",
                "Requested by": item.get("user_name", ""),
                "Requested": _stamp(item.get("created_at")),
            }
            for item in requests
        ],
        "No feature requests match the current filters.",
        "Feature requests",
    )
    if not requests:
        return

    options = {
        f"#{item.get('request_id', '')} · {item.get('title', '')} ({item.get('vote_count', 0)} votes)": int(
            item["id"]
        )
        for item in requests
    }
    choice = st.selectbox("Open a request", list(options), key="admin_feature_choice")
    open_column, clear_column = st.columns([2, 1])
    if open_column.button(
        "Open detail", icon=":material/arrow_forward:", width="stretch", key="admin_feature_open"
    ):
        st.session_state[_SELECTED_KEY] = options[choice]
        st.rerun()
    if clear_column.button(
        "Close detail", icon=":material/close:", width="stretch", key="admin_feature_clear"
    ):
        st.session_state.pop(_SELECTED_KEY, None)
        st.rerun()

    selected = st.session_state.get(_SELECTED_KEY)
    if selected:
        _render_detail(account, int(selected))
