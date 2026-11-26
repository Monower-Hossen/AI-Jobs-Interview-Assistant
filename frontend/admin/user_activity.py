"""Admin user activity overview page."""

from __future__ import annotations

from datetime import datetime
from math import ceil
from typing import cast

import pandas as pd
import streamlit as st
from services.auth_service import Account
from services.user_activity_service import (
    ACTIVITY_DATE_RANGES,
    ActivityEvent,
    InterviewDetail,
    UserActivityReport,
    UserActivityRow,
    get_user_activity_report,
)

from frontend.shared.components import (
    data_table,
    empty_state,
    error_state,
    kpi_row,
    page_header,
    section,
)

_PAGE_SIZE = 10


def _require_admin() -> Account:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        st.error("Administrator access required.", icon=":material/error:")
        st.stop()
    from services.auth_service import require_admin

    require_admin(getattr(account, "id", None))
    return cast(Account, account)


def _format_datetime(value: datetime | None, missing: str = "N/A") -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else missing


def _format_score(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def _daily_counts(events: list[ActivityEvent]) -> pd.DataFrame:
    rows = [(event.timestamp.date(), 1) for event in events if event.timestamp is not None]
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows, columns=["Date", "Count"])
    return frame.groupby("Date")["Count"].sum().reset_index().set_index("Date")


def _score_trend(interviews: list[InterviewDetail]) -> pd.DataFrame:
    scored = sorted(
        (item for item in interviews if item.score is not None and item.created_at is not None),
        key=lambda item: item.created_at.timestamp() if item.created_at else 0.0,
    )
    if not scored:
        return pd.DataFrame()
    frame = pd.DataFrame(
        {
            "Interview": [f"#{index}" for index in range(1, len(scored) + 1)],
            "Score": [float(item.score or 0.0) for item in scored],
        }
    )
    return frame.set_index("Interview")


def _type_counts(interviews: list[InterviewDetail]) -> pd.DataFrame:
    if not interviews:
        return pd.DataFrame()
    frame = pd.DataFrame({"Type": [item.interview_type for item in interviews]})
    return frame.groupby("Type").size().reset_index(name="Count").set_index("Type")


def _chart_or_empty(frame: pd.DataFrame, message: str, alt: str, chart="bar") -> None:
    if frame.empty:
        empty_state(message)
        return
    if chart == "line":
        st.line_chart(frame, alt=alt)
    elif chart == "area":
        st.area_chart(frame, alt=alt)
    else:
        st.bar_chart(frame, alt=alt)


def _timeline_rows(events: list[ActivityEvent]) -> list[dict]:
    return [
        {
            "User Name": event.user_name,
            "Activity Type": event.activity_type,
            "Description": event.description,
            "Timestamp": _format_datetime(event.timestamp),
        }
        for event in events
    ]


def _render_activity_timeline(events: list[ActivityEvent], empty_message: str) -> None:
    data_table(
        _timeline_rows(events),
        empty_message,
        alt="User activity timeline",
    )


def _platform_metrics(report: UserActivityReport) -> None:
    active_count = sum(1 for row in report.rows if row.activity_status == "Active")
    kpi_row(
        [
            ("Total Users", len(report.rows), None, ":material/group:"),
            ("Active Users", active_count, None, ":material/how_to_reg:"),
            ("Inactive Users", len(report.rows) - active_count, None, ":material/person_off:"),
            ("Total Interviews", report.total_interviews, None, ":material/assignment:"),
            ("Completed Interviews", report.completed_interviews, None, ":material/check_circle:"),
            (
                "Average Interview Score",
                _format_score(report.average_score),
                None,
                ":material/score:",
            ),
        ]
    )
    st.caption(
        "Active means an enabled account with tracked activity in the selected range. "
        "Inactive means no tracked activity in that range, including disabled accounts."
    )


def _render_user_activity_table(rows: list[UserActivityRow]) -> UserActivityRow | None:
    search = st.text_input(
        "Search users",
        placeholder="Search by name or email",
        key="admin_activity_search",
    )
    status_filter = st.selectbox(
        "Activity status",
        ["All", "Active", "Inactive"],
        key="admin_activity_status_filter",
    )
    sort_by = st.selectbox(
        "Sort by",
        ["Last Active", "Interview Count", "Average Score"],
        key="admin_activity_sort_by",
    )
    descending = st.toggle("Descending", value=True, key="admin_activity_sort_desc")

    query = (search or "").strip().lower()
    filtered = [
        row
        for row in rows
        if (status_filter == "All" or row.activity_status == status_filter)
        and (not query or query in f"{row.full_name} {row.email} {row.username}".lower())
    ]

    def sort_key(row: UserActivityRow) -> float:
        if sort_by == "Interview Count":
            return float(row.total_interviews)
        if sort_by == "Average Score":
            return row.average_score if row.average_score is not None else -1.0
        return row.last_active.timestamp() if row.last_active else 0.0

    filtered.sort(key=sort_key, reverse=descending)
    pages = max(1, ceil(len(filtered) / _PAGE_SIZE))
    requested_page = st.number_input(
        "Page",
        min_value=1,
        max_value=pages,
        value=1,
        key="admin_activity_page",
    )
    page = min(int(requested_page), pages)
    if page != requested_page:
        st.session_state["admin_activity_page"] = page
    visible = filtered[(page - 1) * _PAGE_SIZE : page * _PAGE_SIZE]

    data_table(
        [
            {
                "User": row.full_name,
                "Email": row.email,
                "Status": row.activity_status,
                "Account Status": row.account_status.title(),
                "Interview Count": row.total_interviews,
                "Average Score": _format_score(row.average_score),
                "Last Active": _format_datetime(row.last_active, "N/A"),
                "Logged Events": row.logged_events,
            }
            for row in visible
        ],
        "No users match the current filters.",
        alt="Activity statistics for every registered user",
    )
    st.caption(f"Showing {len(visible)} of {len(filtered)} users (page {page} of {pages}).")

    if not filtered:
        return None
    options = {f"{row.full_name} ({row.email}) [id {row.user_id}]": row for row in filtered}
    selected_label = st.selectbox(
        "Open user activity",
        list(options),
        index=None,
        placeholder="Choose a user to inspect…",
        key="admin_activity_table_user",
    )
    if not selected_label:
        return None
    st.button(
        "Open individual activity",
        icon=":material/open_in_new:",
        width="stretch",
        on_click=_open_individual_mode,
        args=(options[selected_label].user_id,),
    )
    return options[selected_label]


def _open_individual_mode(user_id: int) -> None:
    st.session_state["admin_activity_selected_user_id"] = user_id
    st.session_state["admin_activity_mode"] = "Individual User"


def _render_all_users(report: UserActivityReport) -> None:
    section("Platform activity snapshot", ":material/query_stats:")
    _platform_metrics(report)

    with st.container(border=True):
        section("Activity trend", ":material/show_chart:")
        _chart_or_empty(
            _daily_counts(report.timeline),
            "No tracked activity in the selected range.",
            "Daily activity counts for the selected date range",
            "area",
        )

    left, right = st.columns(2)
    with left:
        section("Login activity", ":material/login:")
        _chart_or_empty(
            _daily_counts(report.login_history),
            "No login events recorded in the selected range.",
            "Daily login event counts",
        )
    with right:
        section("Interview mix", ":material/bar_chart:")
        _chart_or_empty(
            _type_counts(report.interviews),
            "No interviews recorded in the selected range.",
            "Interview counts by interview type",
        )

    section("User activity", ":material/table:")
    _render_user_activity_table(report.rows)


def _select_individual_user(rows: list[UserActivityRow]) -> UserActivityRow | None:
    search = st.text_input(
        "Search by name or email",
        placeholder="Start typing a name or email address",
        key="admin_activity_user_search",
    )
    query = (search or "").strip().lower()
    filtered = [row for row in rows if not query or query in f"{row.full_name} {row.email}".lower()]
    if not filtered:
        empty_state(
            "No users match the current search.",
            "Clear the search to choose another account.",
        )
        return None

    options = {
        f"{row.full_name} ({row.email}, @{row.username or 'no-username'}) [id {row.user_id}]": row
        for row in filtered
    }
    id_key = "admin_activity_selected_user_id"
    valid_ids = {row.user_id for row in filtered}
    desired_id = st.session_state.get(id_key)
    if desired_id not in valid_ids:
        desired_id = filtered[0].user_id
    ordered_rows = [options[label] for label in options]
    selected_label = st.selectbox(
        "Select user for activity",
        list(options),
        index=[row.user_id for row in ordered_rows].index(desired_id),
        key="admin_activity_selected_user",
    )
    if not selected_label:
        return None
    selected = options[selected_label]
    st.session_state[id_key] = selected.user_id
    return selected


def _profile(row: UserActivityRow) -> None:
    section("Profile", ":material/person:")
    with st.container(border=True):
        left, right = st.columns(2)
        with left:
            st.markdown(f"### {row.full_name}")
            st.caption(f"Email: {row.email}")
            st.caption(f"Username: {row.username or 'not set'}")
        with right:
            st.caption(f"User ID: {row.user_id}")
            st.caption(f"Role: {row.role.title()}")
            st.caption(f"Account status: {row.account_status.title()}")
            st.caption(f"Activity status: {row.activity_status}")
            st.caption(f"Registered: {_format_datetime(row.created_at)}")


def _individual_metrics(row: UserActivityRow) -> None:
    section("Activity summary", ":material/query_stats:")
    kpi_row(
        [
            ("Total Interviews", row.total_interviews, None, ":material/assignment:"),
            ("Completed Interviews", row.completed_interviews, None, ":material/check_circle:"),
            (
                "Average Interview Score",
                _format_score(row.average_score),
                None,
                ":material/score:",
            ),
            ("Last Login", _format_datetime(row.last_login, "Never"), None, ":material/login:"),
            ("Last Active", _format_datetime(row.last_active, "N/A"), None, ":material/bolt:"),
            ("Event Count", row.logged_events, None, ":material/event_note:"),
        ]
    )


def _render_individual_user(admin: Account, report: UserActivityReport) -> None:
    selected_row = _select_individual_user(report.rows)
    if selected_row is None:
        return

    try:
        user_report = get_user_activity_report(
            admin.id, report.window.label, user_id=selected_row.user_id
        )
    except Exception as exc:
        error_state("Could not load user activity.", str(exc))
        return

    _profile(selected_row)
    _individual_metrics(selected_row)

    tabs = st.tabs(
        [
            "Interview History",
            "Login & Sessions",
            "Learning & Practice",
            "Files & Live",
            "Timeline",
        ]
    )

    with tabs[0]:
        section("Interview performance", ":material/trending_up:")
        _chart_or_empty(
            _score_trend(user_report.interviews),
            "No scored interviews in the selected range.",
            "Interview score trend for the selected user",
            "line",
        )
        data_table(
            [
                {
                    "ID": item.interview_id,
                    "Type": item.interview_type,
                    "Status": item.status,
                    "Score": _format_score(item.score),
                    "Date": _format_datetime(item.created_at),
                }
                for item in user_report.interviews
            ],
            "No interviews recorded for this user in the selected range.",
            alt="Interview history for the selected user",
        )

    with tabs[1]:
        section("Login history", ":material/login:")
        data_table(
            [
                {
                    "Time": _format_datetime(event.timestamp),
                    "Event": event.activity_type,
                    "Message": event.description,
                }
                for event in user_report.login_history
            ],
            "No login events recorded for this user in the selected range.",
            alt="Login history for the selected user",
        )
        section("Session details", ":material/devices:")
        data_table(
            [
                {
                    "ID": session.session_id,
                    "Label": session.label,
                    "Created": _format_datetime(session.created_at),
                    "Last Active": _format_datetime(session.last_active),
                    "Revoked": _format_datetime(session.revoked_at, "No"),
                    "Interviews": session.interview_count,
                }
                for session in user_report.sessions
            ],
            "No workspace sessions recorded for this user in the selected range.",
            alt="Workspace sessions for the selected user",
        )

    with tabs[2]:
        section("Activity breakdown", ":material/pie_chart:")
        breakdown = [
            {
                "Activity": label,
                "Count": sum(
                    1 for event in user_report.timeline if event.activity_type == activity_type
                ),
            }
            for label, activity_type in [
                ("Resume uploads", "Resume upload"),
                ("Learning activities", "Learning activity"),
                ("Question practice", "Question practice"),
                ("Interviews", "Interview"),
                ("Reports", "Report generated"),
                ("Live interviews", "Live interview"),
            ]
        ]
        data_table(breakdown, "No tracked activity yet.", alt="Activity counts by type")
        section("Resume uploads", ":material/upload_file:")
        _render_activity_timeline(
            [event for event in user_report.timeline if event.activity_type == "Resume upload"],
            "No resume uploads recorded.",
        )
        section("Learning activity", ":material/school:")
        _render_activity_timeline(
            [event for event in user_report.timeline if event.activity_type == "Learning activity"],
            "No learning activity recorded.",
        )
        section("Question practice", ":material/quiz:")
        _render_activity_timeline(
            [event for event in user_report.timeline if event.activity_type == "Question practice"],
            "No question practice attempts recorded.",
        )

    with tabs[3]:
        section("Generated reports", ":material/description:")
        _render_activity_timeline(
            [event for event in user_report.timeline if event.activity_type == "Report generated"],
            "No reports generated in the selected range.",
        )
        section("Live interviews", ":material/videocam:")
        _render_activity_timeline(
            [event for event in user_report.timeline if event.activity_type == "Live interview"],
            "No live interviews recorded in the selected range.",
        )

    with tabs[4]:
        section("Recent activity timeline", ":material/event_note:")
        _render_activity_timeline(
            user_report.timeline,
            "No tracked activity recorded for this user in the selected range.",
        )


def show_admin_user_activity() -> None:
    admin = _require_admin()
    page_header(
        "User Activity Overview",
        "Review platform-wide engagement and individual user history from live database records.",
        ":material/history:",
    )

    mode = st.segmented_control(
        "Activity view",
        ["All Users", "Individual User"],
        default="All Users",
        key="admin_activity_mode",
        bind="query-params",
    )
    date_range = st.segmented_control(
        "Date range",
        list(ACTIVITY_DATE_RANGES),
        default="Last 30 Days",
        key="admin_activity_date_range",
        bind="query-params",
    )

    try:
        report = get_user_activity_report(admin.id, date_range or "Last 30 Days")
    except Exception as exc:
        error_state("Could not load user activity.", str(exc))
        return

    if mode == "Individual User":
        _render_individual_user(admin, report)
    else:
        _render_all_users(report)


show_admin_user_activity()
