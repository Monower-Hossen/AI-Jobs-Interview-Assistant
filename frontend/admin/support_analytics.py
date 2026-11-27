from datetime import UTC, date, datetime, timedelta

import pandas as pd
import streamlit as st
from services.support_service import get_support_analytics

from frontend.shared.components import data_table, empty_state, error_state, page_header, section


def _stamp(value: object) -> str:
    return str(value or "-")


def _date_range(value: object) -> tuple[date | None, date | None]:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return value[0], value[1]
    if isinstance(value, date):
        return value, value
    return None, None


def _chart_frame(rows: object, value_label: str) -> pd.DataFrame | None:
    if not rows:
        return None
    frame = pd.DataFrame(rows)
    if "date" not in frame.columns or value_label not in frame.columns:
        return None
    return frame.set_index("date")[[value_label]]


def _breakdown_rows(data: object, label: str) -> list[dict[str, object]]:
    if not isinstance(data, dict):
        return []
    return [
        {label: str(key), "Count": value}
        for key, value in sorted(data.items(), key=lambda item: str(item[0]))
    ]


def show_admin_support_analytics(*, render_header: bool = True) -> None:
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
            "Support analytics",
            "Explore support volume, resolution times, and feedback trends",
            ":material/query_stats:",
        )

    with st.container(border=True):
        st.markdown("**Date range**")
        selected_range = st.date_input(
            "Analytics period",
            value=(datetime.now(UTC).date() - timedelta(days=29), datetime.now(UTC).date()),
            key="admin_analytics_range",
        )
        start, end = _date_range(selected_range)
        if start and end and start > end:
            error_state("Start date must be before end date.")
            st.stop()

    if not start or not end:
        empty_state("Select a start and end date to view analytics.")
        st.stop()

    st.caption(f"Showing activity from {start.isoformat()} to {end.isoformat()}.")

    try:
        analytics = get_support_analytics(account.id, date_from=start, date_to=end)
    except Exception as exc:
        error_state(str(exc))
        st.stop()

    section("Key metrics", ":material/insights:")
    st.metric("Tickets created", analytics.get("tickets_created", 0), border=True)
    kpi_column, feedback_column = st.columns(2)
    with kpi_column.container(horizontal=True, wrap=True):
        st.metric("Total tickets", analytics.get("total_tickets", 0), border=True)
        st.metric("Resolved", analytics.get("resolved_tickets", 0), border=True)
        st.metric("Closed", analytics.get("closed_tickets", 0), border=True)
        st.metric(
            "Avg resolution",
            f"{float(analytics.get('avg_resolution_hours') or 0):.1f} h",
            border=True,
        )
        st.metric("Messages sent", analytics.get("messages_sent", 0), border=True)
    with feedback_column.container(horizontal=True, wrap=True):
        st.metric("Feedback submitted", analytics.get("feedback_submitted", 0), border=True)
        st.metric("Bugs submitted", analytics.get("bugs_submitted", 0), border=True)
        st.metric("Feature requests", analytics.get("feature_requests_submitted", 0), border=True)
        st.metric("Contact messages", analytics.get("contact_messages_received", 0), border=True)
        st.metric(
            "Avg rating", f"{float(analytics.get('feedback_avg_rating') or 0):.1f}/5", border=True
        )

    section("Ticket breakdowns", ":material/pie_chart:")
    status_column, priority_column, category_column = st.columns(3)
    with status_column:
        data_table(
            _breakdown_rows(analytics.get("status_breakdown"), "Status"),
            "No ticket statuses in this range.",
            "Ticket statuses",
        )
    with priority_column:
        data_table(
            _breakdown_rows(analytics.get("priority_breakdown"), "Priority"),
            "No ticket priorities in this range.",
            "Ticket priorities",
        )
    with category_column:
        data_table(
            _breakdown_rows(analytics.get("category_breakdown"), "Category"),
            "No ticket categories in this range.",
            "Ticket categories",
        )

    section("Volume trends", ":material/trending_up:")
    ticket_column, message_column = st.columns(2)
    cumulative = _chart_frame(analytics.get("cumulative_tickets"), "count")
    with ticket_column:
        if cumulative is not None:
            st.line_chart(
                cumulative.rename(columns={"count": "Tickets"}),
                y="Tickets",
                alt="Cumulative support tickets over time",
            )
        else:
            empty_state("No ticket trend data in this range.")
        daily = _chart_frame(analytics.get("daily_tickets"), "count")
        if daily is not None:
            st.bar_chart(daily.rename(columns={"count": "New tickets"}), y="New tickets")
    with message_column:
        activity = _chart_frame(analytics.get("daily_activity"), "count")
        if activity is not None:
            st.line_chart(
                activity.rename(columns={"count": "Activities"}),
                y="Activities",
                alt="Support activities over time",
            )
        else:
            empty_state("No activity data in this range.")
        messages = _chart_frame(analytics.get("daily_messages"), "count")
        if messages is not None:
            st.bar_chart(messages.rename(columns={"count": "Messages"}), y="Messages")

    section("Feedback, reports, and contacts", ":material/bar_chart:")
    feedback_column, bug_column, contact_column = st.columns(3)
    with feedback_column:
        data_table(
            _breakdown_rows(analytics.get("feedback_rating_breakdown"), "Rating"),
            "No feedback in this range.",
            "Feedback ratings",
        )
        feedback_trend = _chart_frame(analytics.get("daily_feedback"), "count")
        if feedback_trend is not None:
            st.line_chart(feedback_trend.rename(columns={"count": "Feedback"}), y="Feedback")
    with bug_column:
        data_table(
            _breakdown_rows(analytics.get("bug_status_breakdown"), "Status"),
            "No bug reports in this range.",
            "Bug statuses",
        )
        data_table(
            _breakdown_rows(analytics.get("bug_severity_breakdown"), "Severity"),
            "No bug reports in this range.",
            "Bug severities",
        )
        bug_trend = _chart_frame(analytics.get("daily_bugs"), "count")
        if bug_trend is not None:
            st.line_chart(bug_trend.rename(columns={"count": "Bugs"}), y="Bugs")
    with contact_column:
        data_table(
            _breakdown_rows(analytics.get("contact_status_breakdown"), "Status"),
            "No contact messages in this range.",
            "Contact statuses",
        )
        contact_trend = _chart_frame(analytics.get("daily_contacts"), "count")
        if contact_trend is not None:
            st.line_chart(contact_trend.rename(columns={"count": "Contacts"}), y="Contacts")

    section("Feature requests", ":material/lightbulb:")
    request_column, top_column = st.columns([1, 2])
    with request_column:
        data_table(
            _breakdown_rows(analytics.get("feature_status_breakdown"), "Status"),
            "No feature requests in this range.",
            "Feature request statuses",
        )
        request_trend = _chart_frame(analytics.get("daily_feature_requests"), "count")
        if request_trend is not None:
            st.line_chart(request_trend.rename(columns={"count": "Requests"}), y="Requests")
    with top_column:
        data_table(
            [
                {
                    "Request": row.get("title", ""),
                    "Votes": row.get("vote_count", 0),
                    "Status": row.get("status", ""),
                }
                for row in (analytics.get("top_voted_requests") or [])
                if isinstance(row, dict)
            ],
            "No voted requests in this range.",
            "Top voted feature requests",
        )

    section("Recent support activity", ":material/history:")
    activities = [
        {
            "Activity": row.get("event_type", ""),
            "Details": row.get("message", ""),
            "Actor": row.get("actor", ""),
            "When": _stamp(row.get("created_at")),
        }
        for row in (analytics.get("recent_activity") or [])
        if isinstance(row, dict)
    ]
    data_table(activities, "No support activity recorded in this range.", "Support activity")
