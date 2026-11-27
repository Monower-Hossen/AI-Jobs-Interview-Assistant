from __future__ import annotations

import streamlit as st
from services.auth_service import AuthError, require_admin

from frontend.admin.bug_reports import show_admin_bug_reports
from frontend.admin.contact_messages import show_admin_contact_messages
from frontend.admin.faq_management import show_admin_faq_management
from frontend.admin.feature_requests import show_admin_feature_requests
from frontend.admin.support_analytics import show_admin_support_analytics
from frontend.admin.support_dashboard import show_admin_support_dashboard
from frontend.admin.support_tickets import show_admin_support_tickets
from frontend.admin.user_feedback import show_admin_user_feedback
from frontend.shared.components import error_state, page_header

TAB_LABELS = (
    "Support Dashboard",
    "Support Tickets",
    "User Feedback",
    "Bug Reports",
    "Feature Requests",
    "Contact Messages",
    "FAQ Management",
    "Support Analytics",
)
TAB_KEY = "admin_support_management_active_tab"
TAB_ALIASES = {
    "admin_support_management": "Support Dashboard",
    "support_dashboard": "Support Dashboard",
    "admin_support_tickets": "Support Tickets",
    "frontend/admin/support_tickets.py": "Support Tickets",
    "admin_user_feedback": "User Feedback",
    "frontend/admin/user_feedback.py": "User Feedback",
    "admin_bug_reports": "Bug Reports",
    "frontend/admin/bug_reports.py": "Bug Reports",
    "admin_feature_requests": "Feature Requests",
    "frontend/admin/feature_requests.py": "Feature Requests",
    "admin_contact_messages": "Contact Messages",
    "frontend/admin/contact_messages.py": "Contact Messages",
    "admin_faq_management": "FAQ Management",
    "frontend/admin/faq_management.py": "FAQ Management",
    "admin_support_analytics": "Support Analytics",
    "frontend/admin/support_analytics.py": "Support Analytics",
}


def _requested_tab() -> str | None:
    requested = st.session_state.pop("current_page", None)
    if not requested:
        return None
    return TAB_ALIASES.get(str(requested))


def show_admin_support_management() -> None:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        error_state("Administrator access required.")
        st.stop()
    admin_id = int(account.id)
    try:
        require_admin(admin_id)
    except AuthError as exc:
        error_state("Administrator access required.", str(exc))
        st.stop()

    page_header(
        "Support management",
        "Monitor, triage, and resolve every support workflow from one unified dashboard.",
        icon=":material/support_agent:",
    )
    st.caption("Scroll the tab row horizontally on narrow screens.")

    default_tab = _requested_tab()
    tabs = st.tabs(
        TAB_LABELS,
        default=default_tab,
        key=TAB_KEY,
        on_change="rerun",
    )

    if tabs[0].open:
        with tabs[0]:
            show_admin_support_dashboard(render_header=False)
    if tabs[1].open:
        with tabs[1]:
            show_admin_support_tickets(render_header=False)
    if tabs[2].open:
        with tabs[2]:
            show_admin_user_feedback(render_header=False)
    if tabs[3].open:
        with tabs[3]:
            show_admin_bug_reports(render_header=False)
    if tabs[4].open:
        with tabs[4]:
            show_admin_feature_requests(render_header=False)
    if tabs[5].open:
        with tabs[5]:
            show_admin_contact_messages(render_header=False)
    if tabs[6].open:
        with tabs[6]:
            show_admin_faq_management(render_header=False)
    if tabs[7].open:
        with tabs[7]:
            show_admin_support_analytics(render_header=False)


show_admin_support_management()
