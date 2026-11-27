import streamlit as st
from services.support_service import get_support_dashboard_metrics

from frontend.shared.components import (
    data_table,
    empty_state,
    error_state,
    kpi_row,
    page_header,
    section,
)
from frontend.shared.navigation import PAGES, go_to

DASHBOARD_LINKS = (
    (
        "Support tickets",
        "admin_support_tickets",
        "frontend/admin/support_tickets.py",
        ":material/confirmation_number:",
    ),
    (
        "User feedback",
        "admin_user_feedback",
        "frontend/admin/user_feedback.py",
        ":material/feedback:",
    ),
    ("Bug reports", "admin_bug_reports", "frontend/admin/bug_reports.py", ":material/bug_report:"),
    (
        "Feature requests",
        "admin_feature_requests",
        "frontend/admin/feature_requests.py",
        ":material/lightbulb:",
    ),
    (
        "Contact messages",
        "admin_contact_messages",
        "frontend/admin/contact_messages.py",
        ":material/mail:",
    ),
    (
        "FAQ management",
        "admin_faq_management",
        "frontend/admin/faq_management.py",
        ":material/quiz:",
    ),
    (
        "Support analytics",
        "admin_support_analytics",
        "frontend/admin/support_analytics.py",
        ":material/query_stats:",
    ),
)


def _hours(value: object) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.1f} h"
    except (TypeError, ValueError):
        return str(value)


def _rating(value: object) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.1f}/5"
    except (TypeError, ValueError):
        return str(value)


def _count_rows(data: object, label: str) -> list[dict[str, object]]:
    if not isinstance(data, dict):
        return []
    return [
        {label: str(key), "Tickets": value}
        for key, value in sorted(data.items(), key=lambda item: str(item[0]))
    ]


def _navigate(alias: str, path: str) -> None:
    go_to(alias if alias in PAGES else path)


def show_admin_support_dashboard(*, render_header: bool = True) -> None:
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
            "Support dashboard",
            "Monitor tickets, feedback, reports, and help center activity",
            ":material/support_agent:",
        )

    try:
        metrics = get_support_dashboard_metrics(actor_admin_id=account.id)
    except Exception as exc:
        error_state(str(exc))
        st.stop()

    kpi_row(
        [
            (
                "Total tickets",
                metrics.get("total_tickets", 0),
                None,
                ":material/confirmation_number:",
            ),
            ("Open tickets", metrics.get("open_tickets", 0), None, ":material/mail:"),
            (
                "In progress",
                metrics.get("in_progress_tickets", 0),
                None,
                ":material/pending_actions:",
            ),
            (
                "Resolved tickets",
                metrics.get("resolved_tickets", 0),
                None,
                ":material/check_circle:",
            ),
            ("Closed tickets", metrics.get("closed_tickets", 0), None, ":material/archive:"),
            (
                "Unread user messages",
                metrics.get("unread_user_messages", 0),
                None,
                ":material/mark_email_unread:",
            ),
            (
                "Unread contacts",
                metrics.get("unread_contacts", 0),
                None,
                ":material/mark_email_unread:",
            ),
            ("Pending feedback", metrics.get("pending_feedback", 0), None, ":material/feedback:"),
            (
                "Pending bug reports",
                metrics.get("pending_bug_reports", 0),
                None,
                ":material/bug_report:",
            ),
            (
                "Pending requests",
                metrics.get("pending_feature_requests", 0),
                None,
                ":material/lightbulb:",
            ),
            ("Published FAQs", metrics.get("published_faqs", 0), None, ":material/quiz:"),
            (
                "Avg first response",
                _hours(metrics.get("avg_first_response_hours")),
                None,
                ":material/schedule:",
            ),
            (
                "Avg resolution",
                _hours(metrics.get("avg_resolution_hours")),
                None,
                ":material/timer:",
            ),
            (
                "Avg feedback rating",
                _rating(metrics.get("feedback_avg_rating")),
                None,
                ":material/star:",
            ),
        ]
    )

    section("Ticket mix", ":material/pie_chart:")
    status_column, category_column, priority_column = st.columns(3)
    with status_column:
        data_table(
            _count_rows(metrics.get("tickets_by_status"), "Status"),
            "No ticket statuses recorded yet.",
            "Tickets by status",
        )
    with category_column:
        data_table(
            _count_rows(metrics.get("tickets_by_category"), "Category"),
            "No ticket categories recorded yet.",
            "Tickets by category",
        )
    with priority_column:
        data_table(
            _count_rows(metrics.get("tickets_by_priority"), "Priority"),
            "No ticket priorities recorded yet.",
            "Tickets by priority",
        )

    section("Recent support activities", ":material/history:")
    activities = [
        {
            "Activity": item.get("event_type", ""),
            "Details": item.get("message", ""),
            "Actor": item.get("actor", ""),
            "When": str(item.get("created_at") or "-"),
        }
        for item in (metrics.get("recent_activity") or [])
        if isinstance(item, dict)
    ]
    if activities:
        data_table(activities, "No support activity recorded yet.", "Recent support activities")
    else:
        empty_state("No support activity recorded yet.")

    section("Top voted feature requests", ":material/lightbulb:")
    top_requests = [
        {
            "Request": row.get("title") or row.get("request_id") or "Request",
            "Votes": row.get("vote_count", 0),
            "Status": row.get("status", ""),
        }
        for row in (metrics.get("top_feature_requests") or [])
        if isinstance(row, dict)
    ]
    data_table(top_requests, "No feature requests have been voted on yet.", "Top feature requests")

    section("Quick actions", ":material/bolt:")
    with st.container(horizontal=True, wrap=True):
        for label, alias, path, icon in DASHBOARD_LINKS:
            if st.button(
                label,
                icon=icon,
                width="stretch",
                key=f"admin_support_dashboard_link_{alias}",
            ):
                _navigate(alias, path)
