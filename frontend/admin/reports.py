"""Admin platform-wide interview reports page."""

import json
from dataclasses import asdict
from datetime import date, datetime
from typing import cast

import streamlit as st

from frontend.shared.components import data_table, page_header


def show_admin_reports() -> None:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        st.error("Administrator access required.", icon=":material/error:")
        st.stop()

    from services.analytics import get_platform_report, get_platform_report_date_range
    from services.auth_service import AuthError, require_admin

    try:
        require_admin(account.id)
        earliest, latest = get_platform_report_date_range(account.id)
        page_header(
            "Platform Reports",
            "Review interview activity across all user workspaces",
            ":material/summarize:",
        )
        if earliest is None or latest is None:
            st.info("No interview records are available yet.")
            return

        selected_dates = st.date_input(
            "Report date range",
            value=(earliest, latest),
            min_value=earliest,
            max_value=latest,
        )
        if isinstance(selected_dates, (list, tuple)):
            if len(selected_dates) != 2:
                st.info("Select a valid date range.")
                return
            start_date, end_date = cast(tuple[date, date], tuple(selected_dates))
        else:
            start_date = end_date = cast(date, selected_dates)

        snapshot = get_platform_report(account.id, start_date, end_date)
        metric_columns = st.columns(5)
        metrics = [
            ("Interviews", snapshot.total_interviews),
            ("Completed", snapshot.completed_interviews),
            ("Users", snapshot.unique_users),
            ("Avg score", f"{snapshot.average_score:.1f}"),
            ("Reports", snapshot.stored_reports),
        ]
        for column, (label, value) in zip(metric_columns, metrics):
            column.metric(label, value)

        st.caption(
            f"{snapshot.answered_questions} of {snapshot.total_questions} recorded questions "
            f"were answered · completion rate {snapshot.completion_rate:.1f}%"
        )
        data_table(
            [
                {
                    "Date": row.interview_date,
                    "User": row.user_name,
                    "Email": row.user_email,
                    "Workspace": row.workspace_id,
                    "Type": row.interview_type,
                    "Difficulty": row.difficulty,
                    "Status": row.status,
                    "Score": f"{row.overall_score:.1f}" if row.overall_score is not None else "N/A",
                    "Questions": row.total_questions,
                    "Answered": row.answered_questions,
                }
                for row in snapshot.rows
            ],
            "No interviews match the selected date range.",
        )
        st.download_button(
            "Download platform report (JSON)",
            data=json.dumps(asdict(snapshot), default=str, indent=2),
            file_name=f"platform-report-{datetime.now().strftime('%Y%m%d')}.json",
            mime="application/json",
        )
    except AuthError as exc:
        st.error(str(exc), icon=":material/error:")
        st.stop()


show_admin_reports()
