"""Admin user activity overview page."""

from __future__ import annotations

from datetime import datetime
from typing import cast

import pandas as pd
import streamlit as st
from services.auth_service import Account, AuthError, get_user_activity_for_admin, list_users

from frontend.shared.components import (
    data_table,
    empty_state,
    error_state,
    kpi_row,
    page_header,
    section,
)


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


def _status_for_row(user: Account) -> str:
    return user.account_status


def _select_user(users: list[Account]) -> Account | None:
    options = {
        f"{user.full_name} ({user.email}, @{user.username or 'no-username'}, "
        f"{_status_for_row(user).title()}) [id {user.id}]": user.id
        for user in users
    }
    key = "admin_activity_selected_user"
    if st.session_state.get(key) not in options:
        st.session_state[key] = next(iter(options), None)
    selected_label = st.selectbox(
        "Select user for activity",
        list(options.keys()),
        key=key,
    )
    if not selected_label:
        return None
    selected_id = options[selected_label]
    return next((user for user in users if user.id == selected_id), None)


def _recent_events(user_id: int) -> list:
    from database import EventLog, get_db_context

    with get_db_context() as db:
        return list(
            db.query(EventLog)
            .filter(EventLog.user_id == user_id)
            .order_by(EventLog.created_at.desc())
            .limit(20)
            .all()
        )


def show_admin_user_activity() -> None:
    admin = _require_admin()
    page_header(
        "User Activity Overview",
        "Review interview activity and performance from live database records.",
        ":material/history:",
    )

    try:
        users = list_users(actor_admin_id=admin.id)
    except AuthError as exc:
        error_state("Could not load users.", str(exc))
        return
    if not users:
        empty_state("No users found.", "User activity appears here after accounts are created.")
        return

    selected = _select_user(users)
    if selected is None:
        return

    try:
        snapshot = get_user_activity_for_admin(selected.id, actor_admin_id=admin.id, limit=10)
        events = _recent_events(selected.id)
    except Exception as exc:
        error_state("Could not load user activity.", str(exc))
        return

    section("Activity summary", ":material/query_stats:")
    kpi_row(
        [
            ("Total Interview Count", snapshot.total_interviews, None, ":material/assignment:"),
            (
                "Completed Interviews",
                snapshot.completed_interviews,
                None,
                ":material/check_circle:",
            ),
            (
                "Average Interview Score",
                _format_score(snapshot.average_score),
                None,
                ":material/score:",
            ),
            (
                "Last Login",
                _format_datetime(snapshot.last_login, "Never"),
                None,
                ":material/login:",
            ),
        ]
    )

    section("Recent Interview Performance", ":material/trending_up:")
    if snapshot.recent_performance:
        chart_rows = [
            {
                "Date": (item["created_at"].strftime("%m-%d %H:%M") if item["created_at"] else ""),
                "Score": item["score"],
            }
            for item in snapshot.recent_performance
            if isinstance(item["score"], (int, float))
        ]
        if chart_rows:
            st.line_chart(
                pd.DataFrame(chart_rows).set_index("Date"),
                y=["Score"],
                alt="Recent interview scores for the selected user",
            )
        data_table(
            [
                {
                    "ID": item["id"],
                    "Type": item["interview_type"],
                    "Status": item["status"],
                    "Score": item["score"],
                    "Date": _format_datetime(item["created_at"]),
                }
                for item in snapshot.recent_performance
            ],
            "No interviews found.",
            alt="Recent interviews for the selected user",
        )
    else:
        empty_state(
            "No interview history yet",
            "Recent interview performance appears here after an interview is started.",
        )

    section("User-wise Interview Statistics", ":material/table:")
    rows = []
    try:
        for user in users:
            activity = (
                snapshot
                if user.id == selected.id
                else get_user_activity_for_admin(user.id, actor_admin_id=admin.id, limit=1)
            )
            rows.append(
                {
                    "User": user.full_name,
                    "Email": user.email,
                    "Total interviews": activity.total_interviews,
                    "Completed": activity.completed_interviews,
                    "Average score": _format_score(activity.average_score),
                    "Last login": _format_datetime(activity.last_login, "Never"),
                }
            )
    except Exception as exc:
        error_state("Could not load user-wise statistics.", str(exc))
    data_table(rows, "No users found.", alt="Interview statistics for every user")

    section("Recent activity", ":material/event_note:")
    data_table(
        [
            {
                "Time": _format_datetime(event.created_at),
                "Type": event.event_type,
                "Level": event.level,
                "Message": event.message,
            }
            for event in events
        ],
        "No activity recorded for this user.",
        alt="Recent account activity for the selected user",
    )


show_admin_user_activity()
