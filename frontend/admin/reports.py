from __future__ import annotations

import json
import math
from datetime import date
from typing import Any

import pandas as pd
import streamlit as st
from services.auth_service import AuthError, list_users, require_admin
from services.report_export_service import export_report_bytes, mime_for_format, safe_download_name
from services.reports_center_service import (
    CATEGORIES,
    REPORT_VIEWS,
    ReportError,
    ReportResult,
    build_report,
    build_report_from_history,
    delete_report_history,
    export_report_from_history,
    get_filter_options,
    get_report_date_bounds,
    get_report_overview,
    list_report_history,
    save_generated_report,
)

from frontend.shared.components import empty_state, error_state, kpi_row, page_header, section

_CATEGORY_KEY = "admin_center_category"
_PENDING_CATEGORY_KEY = "admin_center_pending_category"
_RESULT_KEY = "admin_center_result"
_RESULT_CATEGORY_KEY = "admin_center_result_category"
_FILTERS_KEY = "admin_center_filters"
_DATE_KEY = "admin_center_dates"
_FLASH_KEY = "admin_center_flash"
_DELETE_ANSWER_KEY = "admin_center_delete_answer"
_DELETE_ANSWERED_KEY = "admin_center_delete_answered"
_DELETE_TARGET_KEY = "admin_center_delete_target"
_DELETE_CONFIRM_KEY = "admin_center_delete_confirm"
_PAGE_SIZE = 10
_CATEGORY_DESCRIPTIONS = {
    "User Reports": "Accounts, registrations, approvals, and engagement.",
    "Interview Reports": "Attempts, completion, types, scores, and history.",
    "Performance Reports": "Stored evaluation scores and skill dimensions.",
    "Job Reports": "Postings, status, categories, and trends.",
    "Live Interview Reports": "Invitations and recorded session status.",
    "System Activity Reports": "Audit events and admin actions.",
}


@st.cache_data(ttl="60s", max_entries=30)
def _cached_report(
    actor_admin_id: int,
    category: str,
    start_date: date | None,
    end_date: date | None,
    filters_json: str,
) -> ReportResult:
    return build_report(
        actor_admin_id=actor_admin_id,
        category=category,
        start_date=start_date,
        end_date=end_date,
        filters=json.loads(filters_json),
    )


@st.cache_data(ttl="300s", max_entries=10)
def _cached_options(actor_admin_id: int) -> dict[str, list[str]]:
    return get_filter_options(actor_admin_id)


@st.cache_data(ttl="300s", max_entries=10)
def _cached_users(actor_admin_id: int) -> list[Any]:
    return list_users(actor_admin_id=actor_admin_id)


@st.cache_data(ttl="60s", max_entries=10)
def _cached_bounds(actor_admin_id: int) -> tuple[date | None, date | None]:
    return get_report_date_bounds(actor_admin_id)


@st.cache_data(ttl="30s", max_entries=10)
def _cached_overview(actor_admin_id: int) -> dict[str, str]:
    return get_report_overview(actor_admin_id)


def _on_delete_dismiss() -> None:
    st.session_state[_DELETE_ANSWER_KEY] = False
    st.session_state[_DELETE_ANSWERED_KEY] = True


def _consume_category_click() -> None:
    pending = st.session_state.pop(_PENDING_CATEGORY_KEY, None)
    if pending:
        st.session_state[_CATEGORY_KEY] = pending


def _consume_delete_answer(actor_admin_id: int) -> None:
    if not bool(st.session_state.get(_DELETE_ANSWERED_KEY, False)):
        return
    target = st.session_state.pop(_DELETE_TARGET_KEY, None)
    confirmed = bool(st.session_state.pop(_DELETE_ANSWER_KEY, False))
    st.session_state.pop(_DELETE_ANSWERED_KEY, None)
    st.session_state[_DELETE_CONFIRM_KEY] = False
    if confirmed and target is not None:
        deleted = delete_report_history(actor_admin_id, int(target))
        st.session_state[_FLASH_KEY] = (
            "Generated report deleted." if deleted else "Generated report was not found."
        )
    st.rerun()


@st.dialog("Delete generated report", width="small", on_dismiss=_on_delete_dismiss)
def _history_delete_dialog(report_id: int, report_name: str) -> None:
    st.warning("Delete this generated report?", icon=":material/warning:")
    st.caption(f"{report_name} (report #{report_id}). Original application records are not affected.")
    confirmed = st.checkbox(
        "I understand this removes only the generated report history and file.",
        key=_DELETE_CONFIRM_KEY,
    )
    with st.container(horizontal=True):
        if st.button("Cancel", key="admin_center_delete_cancel"):
            st.session_state[_DELETE_ANSWER_KEY] = False
            st.session_state[_DELETE_ANSWERED_KEY] = True
            st.rerun()
        if st.button(
            "Delete",
            type="primary",
            disabled=not confirmed,
            key="admin_center_delete_confirm_button",
        ):
            st.session_state[_DELETE_TARGET_KEY] = report_id
            st.session_state[_DELETE_ANSWER_KEY] = True
            st.session_state[_DELETE_ANSWERED_KEY] = True
            st.rerun()


def _normalize_range(value: Any) -> tuple[date | None, date | None]:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return value[0], value[1]
    if isinstance(value, date):
        return value, value
    return None, None


def _user_label(account: Any) -> str:
    name = getattr(account, "full_name", "") or getattr(account, "username", "")
    email = getattr(account, "email", "")
    return f"{name} ({email})" if name and email else name or email or str(account.id)


def _render_category_cards() -> None:
    columns = st.columns(3)
    for index, category in enumerate(CATEGORIES):
        with columns[index % 3].container(border=True):
            st.markdown(f"**{category}**")
            st.caption(_CATEGORY_DESCRIPTIONS[category])
            if st.button(
                "Use category",
                key=f"admin_center_pick_{index}",
                icon=":material/arrow_forward:",
            ):
                st.session_state[_PENDING_CATEGORY_KEY] = category
                st.rerun()


def _render_filters(actor_admin_id: int, category: str) -> None:
    options = _cached_options(actor_admin_id)
    users = _cached_users(actor_admin_id)
    earliest, latest = _cached_bounds(actor_admin_id)
    user_options: list[int | None] = [None, *[int(user.id) for user in users]]
    views = REPORT_VIEWS[category]

    with st.form("admin_center_filter_form", border=True):
        st.markdown("**Filters**")
        selected_dates = st.date_input(
            "Date range",
            value=(earliest, latest) if earliest and latest else (None, None),
            key="admin_center_date_range",
            format="YYYY-MM-DD",
        )
        view = st.selectbox("Report view", views, key="admin_center_view")
        user_id = st.selectbox(
            "User",
            user_options,
            format_func=lambda value: "All users" if value is None else _user_label(
                next((user for user in users if int(user.id) == int(value)), value)
            ),
            key="admin_center_user",
        )
        status = "all"
        interview_type = "all"
        job_status = "all"
        job_category = "all"
        invitation_status = "all"
        activity_type = "all"
        score_range = (0.0, 10.0)

        left, right = st.columns(2)
        if category == "User Reports":
            status = left.selectbox("User status", options["user_status"], key="admin_center_status")
        elif category in {"Interview Reports", "Performance Reports"}:
            status = left.selectbox(
                "Interview status", options["interview_status"], key="admin_center_status"
            )
            interview_type = right.selectbox(
                "Interview type", options["interview_type"], key="admin_center_interview_type"
            )
        elif category == "Job Reports":
            job_status = left.selectbox("Job status", options["job_status"], key="admin_center_job_status")
            job_category = right.selectbox(
                "Job category", options["job_category"], key="admin_center_job_category"
            )
        elif category == "Live Interview Reports":
            invitation_status = left.selectbox(
                "Invitation status",
                options["invitation_status"],
                key="admin_center_invitation_status",
            )
        elif category == "System Activity Reports":
            activity_type = left.selectbox(
                "Activity type", options["activity_type"], key="admin_center_activity_type"
            )

        if category == "Performance Reports":
            score_range = st.slider(
                "Score range",
                min_value=0.0,
                max_value=10.0,
                value=(0.0, 10.0),
                step=0.5,
                key="admin_center_score_range",
            )

        submitted = st.form_submit_button(
            "Generate report", type="primary", icon=":material/analytics:", key="admin_center_generate"
        )
        if not submitted:
            return
        start_date, end_date = _normalize_range(selected_dates)
        filters = {
            "view": view,
            "user_id": user_id,
            "status": status,
            "interview_type": interview_type,
            "job_status": job_status,
            "job_category": job_category,
            "invitation_status": invitation_status,
            "activity_type": activity_type,
            "min_score": score_range[0],
            "max_score": score_range[1],
        }
        try:
            result = _cached_report(
                actor_admin_id,
                category,
                start_date,
                end_date,
                json.dumps(filters, sort_keys=True, default=str),
            )
            st.session_state[_RESULT_KEY] = result
            st.session_state[_RESULT_CATEGORY_KEY] = category
            st.session_state[_FILTERS_KEY] = filters
            st.session_state[_DATE_KEY] = (start_date, end_date)
            st.rerun()
        except (ReportError, AuthError) as exc:
            st.error(str(exc), icon=":material/error:")
        except Exception as exc:
            error_state("Report could not be generated.", str(exc)[:300])


def _render_charts(result: ReportResult) -> None:
    if not result.charts:
        empty_state("No charts are available for this report selection.")
        return
    for chart in result.charts[:4]:
        frame = pd.DataFrame(chart.data)
        if frame.empty:
            continue
        with st.container(border=True):
            st.markdown(f"**{chart.title}**")
            series = frame.set_index(chart.x)
            if chart.kind == "line":
                st.line_chart(series, y=chart.y, alt=chart.alt)
            else:
                st.bar_chart(series, y=chart.y, alt=chart.alt)


def _render_preview(actor_admin_id: int, result: ReportResult) -> None:
    start_date, end_date = st.session_state.get(_DATE_KEY, (None, None))
    applied_filters = st.session_state.get(_FILTERS_KEY, result.filters)
    header = {
        "Application": "AI Jobs Interview Assistant",
        "Report title": result.title,
        "Report category": result.category,
        "Generated": result.generated_at.strftime("%Y-%m-%d %H:%M:%S"),
        "Date range": f"{start_date or 'All'} to {end_date or 'All'}",
        "Applied filters": json.dumps(applied_filters, ensure_ascii=False, default=str),
    }
    with st.container(border=True):
        st.markdown("**Report header**")
        st.table(header)

    kpi_row(
        [
            (label, value, None, ":material/bar_chart:")
            for label, value in result.summary[:4]
        ]
    )

    with st.container(border=True):
        st.markdown("**Charts**")
        _render_charts(result)

    with st.container(border=True):
        st.markdown("**Detailed data**")
        if not result.rows:
            empty_state("No records match the selected filters.", "Adjust filters and generate again.")
            return
        frame = pd.DataFrame(result.rows)
        search = st.text_input(
            "Search report rows",
            type="search",
            placeholder="Search values in the current report",
            key="admin_center_search",
        )
        if search.strip():
            mask = frame.astype(str).apply(
                lambda column: column.str.contains(search.strip(), case=False, na=False)
            ).any(axis=1)
            frame = frame.loc[mask]
        sort_column = st.selectbox("Sort by", result.columns, key="admin_center_sort")
        descending = st.toggle("Descending", key="admin_center_descending")
        frame = frame.sort_values(by=sort_column, ascending=not descending)
        pages = max(1, math.ceil(len(frame) / _PAGE_SIZE))
        page = st.number_input("Page", min_value=1, max_value=pages, value=1, key="admin_center_page")
        start = (int(page) - 1) * _PAGE_SIZE
        page_frame = frame.iloc[start : start + _PAGE_SIZE]
        st.dataframe(
            page_frame,
            hide_index=True,
            width="stretch",
            alt=f"Filtered records for {result.title}",
        )
        st.caption(f"Showing {len(page_frame)} of {len(frame)} records (page {page} of {pages}).")

    if result.notes:
        st.caption(" · ".join(result.notes))

    with st.container(horizontal=True, wrap=True):
        for export_format in ("pdf", "csv", "xlsx", "json"):
            if st.button(
                f"Export {export_format.upper()}",
                icon=":material/download:",
                key=f"admin_center_export_{export_format}",
            ):
                try:
                    data = export_report_bytes(result, export_format)
                    save_generated_report(
                        actor_admin_id=actor_admin_id,
                        user_session_id=None,
                        result=result,
                        export_format=export_format,
                        data=data,
                    )
                    st.success(f"{export_format.upper()} report generated and saved to history.")
                    st.download_button(
                        f"Download {export_format.upper()}",
                        data=data,
                        file_name=safe_download_name(result, export_format),
                        mime=mime_for_format(export_format),
                        key=f"admin_center_download_{export_format}",
                    )
                except Exception as exc:
                    error_state(f"{export_format.upper()} export failed.", str(exc)[:300])


def _render_history(actor_admin_id: int) -> None:
    status = st.selectbox("Report status", ["all", "generated"], key="admin_center_history_status")
    rows = list_report_history(actor_admin_id, status=status)
    if not rows:
        empty_state("No generated report history yet.", "Export a report to create a history entry.")
        return
    visible_rows = [
        {key: value for key, value in row.items() if key not in {"filter_json", "file_name"}}
        for row in rows
    ]
    st.dataframe(
        pd.DataFrame(visible_rows),
        hide_index=True,
        width="stretch",
        alt="Generated report history",
    )
    selected_id = st.selectbox(
        "Select generated report",
        [None, *[int(row["id"]) for row in rows]],
        format_func=lambda value: "Choose a report"
        if value is None
        else next(
            (
                f"{row['report_name']} · {row['generated_at']} · {row['export_format']}"
                for row in rows
                if int(row["id"]) == int(value)
            ),
            str(value),
        ),
        key="admin_center_history_select",
    )
    if selected_id is None:
        return
    selected = next(row for row in rows if int(row["id"]) == int(selected_id))
    with st.container(border=True):
        st.markdown("**Report metadata**")
        st.table(
            {
                "Report": selected["report_name"],
                "Category": selected["category"],
                "Generated by": selected["generated_by"] or "Unknown",
                "Generated at": selected["generated_at"],
                "Date range": selected["date_range"],
                "Format": selected["export_format"],
                "Status": selected["status"],
            }
        )
    with st.container(horizontal=True, wrap=True):
        if st.button("View report", icon=":material/visibility:", key="admin_center_history_view"):
            try:
                report = build_report_from_history(actor_admin_id, int(selected_id))
                st.session_state[_RESULT_KEY] = report
                st.session_state[_RESULT_CATEGORY_KEY] = report.category
                st.session_state[_FILTERS_KEY] = report.filters
                st.session_state[_DATE_KEY] = (report.start_date, report.end_date)
                st.rerun()
            except (ReportError, AuthError) as exc:
                st.error(str(exc), icon=":material/error:")
            except Exception as exc:
                error_state("Report preview could not be restored.", str(exc)[:300])
        if st.button(
            "Download report", icon=":material/download:", key="admin_center_history_download"
        ):
            try:
                data, file_name, mime = export_report_from_history(actor_admin_id, int(selected_id))
                st.download_button(
                    "Save report file",
                    data=data,
                    file_name=file_name,
                    mime=mime,
                    key="admin_center_history_download_ready",
                )
            except (ReportError, AuthError) as exc:
                st.error(str(exc), icon=":material/error:")
            except Exception as exc:
                error_state("Report file could not be prepared.", str(exc)[:300])
        if st.button("Delete report", icon=":material/delete:", key="admin_center_history_delete"):
            _history_delete_dialog(int(selected_id), str(selected["report_name"]))


def show_admin_reports() -> None:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        st.error("Administrator access required.", icon=":material/error:")
        st.stop()
    actor_admin_id = int(account.id)
    try:
        require_admin(actor_admin_id)
    except AuthError as exc:
        st.error(str(exc), icon=":material/error:")
        st.stop()

    _consume_category_click()
    _consume_delete_answer(actor_admin_id)
    flash = st.session_state.pop(_FLASH_KEY, None)
    if flash:
        st.success(flash, icon=":material/check_circle:")

    page_header(
        "Reports center",
        "Generate and export reports from real application records",
        ":material/summarize:",
    )
    overview = _cached_overview(actor_admin_id)
    kpi_row(
        [
            ("Total reports generated", overview["total"], None, ":material/description:"),
            ("User reports", overview["user_reports"], None, ":material/group:"),
            ("Interview reports", overview["interview_reports"], None, ":material/person:"),
            ("Last generated", overview["last_generated"], None, ":material/schedule:"),
        ]
    )

    section("Report categories", ":material/grid_view:")
    _render_category_cards()

    section("Report generator & filters", ":material/tune:")
    category = st.selectbox("Report type", CATEGORIES, key=_CATEGORY_KEY)
    if st.session_state.get(_RESULT_CATEGORY_KEY) != category:
        st.session_state.pop(_RESULT_KEY, None)
    _render_filters(actor_admin_id, category)

    result = st.session_state.get(_RESULT_KEY)
    if isinstance(result, ReportResult):
        section("Generated report preview", ":material/preview:")
        _render_preview(actor_admin_id, result)

    section("Report history", ":material/history:")
    _render_history(actor_admin_id)


show_admin_reports()
