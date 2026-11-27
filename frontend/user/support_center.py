from __future__ import annotations

import streamlit as st

from frontend.shared.components import empty_state, page_header
from frontend.user.contact_support import show_contact_support
from frontend.user.feature_requests import show_feature_requests
from frontend.user.help_center import show_help_center
from frontend.user.my_feedback import show_my_feedback
from frontend.user.my_support_tickets import show_my_support_tickets
from frontend.user.report_bug import show_report_bug
from frontend.user.submit_ticket import show_submit_ticket

TAB_LABELS = (
    "My Support Tickets",
    "Submit a Ticket",
    "My Feedback",
    "Report a Bug",
    "Feature Requests",
    "Help Center / FAQs",
    "Contact Support",
)
TAB_KEY = "user_support_center_active_tab"
TAB_ALIASES = {
    "support_center": "My Support Tickets",
    "my_support_tickets": "My Support Tickets",
    "frontend/user/my_support_tickets.py": "My Support Tickets",
    "submit_ticket": "Submit a Ticket",
    "frontend/user/submit_ticket.py": "Submit a Ticket",
    "my_feedback": "My Feedback",
    "frontend/user/my_feedback.py": "My Feedback",
    "report_bug": "Report a Bug",
    "frontend/user/report_bug.py": "Report a Bug",
    "feature_requests": "Feature Requests",
    "frontend/user/feature_requests.py": "Feature Requests",
    "help_center": "Help Center / FAQs",
    "frontend/user/help_center.py": "Help Center / FAQs",
    "contact_support": "Contact Support",
    "frontend/user/contact_support.py": "Contact Support",
}


def _requested_tab() -> str | None:
    requested = st.session_state.pop("current_page", None)
    if not requested:
        return None
    return TAB_ALIASES.get(str(requested))


def show_support_center() -> None:
    if not st.session_state.get("user_id"):
        empty_state("Sign in to access the support center.")
        st.stop()

    page_header(
        "Support Center",
        "Share feedback, report issues, request features, and get help from one centralized hub.",
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
            show_my_support_tickets(render_header=False)
    if tabs[1].open:
        with tabs[1]:
            show_submit_ticket(render_header=False)
    if tabs[2].open:
        with tabs[2]:
            show_my_feedback(render_header=False)
    if tabs[3].open:
        with tabs[3]:
            show_report_bug(render_header=False)
    if tabs[4].open:
        with tabs[4]:
            show_feature_requests(render_header=False)
    if tabs[5].open:
        with tabs[5]:
            show_help_center(render_header=False)
    if tabs[6].open:
        with tabs[6]:
            show_contact_support(render_header=False)


show_support_center()
