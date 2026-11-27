from datetime import UTC, datetime, timedelta

import streamlit as st
from services.support_service import (
    BUG_SEVERITIES,
    BUG_STATUS_ALIASES,
    BUG_STATUSES,
    get_attachment_download,
    get_bug_report,
    list_bug_reports,
    update_bug_report,
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
_SELECTED_KEY = "admin_bug_selected_id"


def _stamp(value: object) -> str:
    return str(value or "-")


def _status_index(options: object, value: object) -> int:
    raw = str(value or "").strip()
    normalized = BUG_STATUS_ALIASES.get(raw.lower(), raw)
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


def _render_detail(account, report_id: int) -> None:
    try:
        bug = get_bug_report(report_id, actor_admin_id=account.id)
    except Exception as exc:
        error_state(str(exc))
        st.session_state.pop(_SELECTED_KEY, None)
        return

    section(f"Bug #{bug.get('report_id', '')} · {bug.get('title', '')}", ":material/bug_report:")
    st.caption(
        f"{bug.get('feature', '')} · {bug.get('severity', '')} · "
        f"{bug.get('status', '')} · Reported {_stamp(bug.get('created_at'))} · "
        f"Resolved {_stamp(bug.get('resolved_at'))}"
    )
    with st.container(border=True):
        st.markdown("**Description**")
        st.write(str(bug.get("description") or ""))
        st.markdown("**Steps to reproduce**")
        st.write(str(bug.get("steps_to_reproduce") or "—"))
        st.markdown("**Expected result**")
        st.write(str(bug.get("expected_result") or "—"))
        st.markdown("**Actual result**")
        st.write(str(bug.get("actual_result") or "—"))
        st.markdown("**Reported by**")
        st.write(f"{bug.get('user_name', '')} · @{bug.get('user_username', '') or 'unknown'}")

    history = bug.get("resolution_history") or []
    if history:
        with st.expander("Status history", expanded=False):
            data_table(
                [
                    {"Change": item.get("message", ""), "When": _stamp(item.get("created_at"))}
                    for item in history
                ],
                "No status changes recorded.",
                "Bug status history",
            )

    section("Attachments", ":material/attach_file:")
    attachments = bug.get("attachments") or []
    if not attachments:
        empty_state("This report has no attachments.")
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
            key=f"admin_bug_download_{int(item['id'])}",
        )

    section("Update report", ":material/tune:")
    with st.form(f"admin_bug_update_{report_id}", border=True):
        new_status = st.selectbox(
            "Status",
            BUG_STATUSES,
            index=_status_index(BUG_STATUSES, bug.get("status")),
            key=f"admin_bug_status_{report_id}",
        )
        notes = st.text_area(
            "Admin notes",
            value=str(bug.get("admin_notes") or ""),
            height=100,
            key=f"admin_bug_notes_{report_id}",
        )
        resolution_details = st.text_area(
            "Resolution details",
            value=str(bug.get("resolution_details") or ""),
            height=100,
            key=f"admin_bug_resolution_{report_id}",
        )
        saved = st.form_submit_button("Save changes", icon=":material/save:", width="stretch")
    if saved:
        try:
            update_bug_report(
                report_id,
                actor_admin_id=account.id,
                status=new_status,
                admin_notes=notes.strip() or None,
                resolution_details=resolution_details.strip() or None,
            )
        except Exception as exc:
            error_state(str(exc))
        else:
            success_toast("Bug report updated.")
            st.rerun()


def show_admin_bug_reports(*, render_header: bool = True) -> None:
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
            "Bug reports",
            "Triage submitted bugs and track their resolution",
            ":material/bug_report:",
        )

    search = st.text_input(
        "Search reports",
        type="search",
        placeholder="Title, feature, description, or username",
        key="admin_bug_search",
    )
    status_column, severity_column, feature_column = st.columns(3)
    status = status_column.selectbox(
        "Status", ["All", *BUG_STATUSES], key="admin_bug_status_filter"
    )
    severity = severity_column.selectbox(
        "Severity", ["All", *BUG_SEVERITIES], key="admin_bug_severity_filter"
    )
    feature = feature_column.text_input(
        "Feature", placeholder="e.g. Interviews", key="admin_bug_feature_filter"
    )
    use_dates = st.checkbox("Filter by report date", key="admin_bug_use_dates")
    date_value = (
        st.date_input(
            "Reported between",
            value=(datetime.now(UTC).date() - timedelta(days=90), datetime.now(UTC).date()),
            key="admin_bug_dates",
        )
        if use_dates
        else None
    )
    date_from, date_to = _date_args(use_dates, date_value)

    try:
        bugs = list_bug_reports(
            actor_admin_id=account.id,
            status=status if status != "All" else None,
            severity=severity if severity != "All" else None,
            feature=feature.strip() or None,
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
                "ID": item.get("report_id", ""),
                "Title": item.get("title", ""),
                "Feature": item.get("feature", ""),
                "Severity": item.get("severity", ""),
                "Status": item.get("status", ""),
                "Reported by": item.get("user_name", ""),
                "Reported": _stamp(item.get("created_at")),
            }
            for item in bugs
        ],
        "No bug reports match the current filters.",
        "Bug reports",
    )
    if not bugs:
        return

    options = {
        f"#{item.get('report_id', '')} · {item.get('title', '')}": int(item["id"]) for item in bugs
    }
    choice = st.selectbox("Open a report", list(options), key="admin_bug_choice")
    open_column, clear_column = st.columns([2, 1])
    if open_column.button(
        "Open detail", icon=":material/arrow_forward:", width="stretch", key="admin_bug_open"
    ):
        st.session_state[_SELECTED_KEY] = options[choice]
        st.rerun()
    if clear_column.button(
        "Close detail", icon=":material/close:", width="stretch", key="admin_bug_clear"
    ):
        st.session_state.pop(_SELECTED_KEY, None)
        st.rerun()

    selected = st.session_state.get(_SELECTED_KEY)
    if selected:
        _render_detail(account, int(selected))
