"""Admin Interview Management page."""

import streamlit as st

from frontend.shared.components import data_table, page_header


def show_admin_interviews() -> None:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        st.error("Administrator access required.", icon=":material/error:")
        st.stop()

    from services.auth_service import list_events, require_admin

    require_admin(account.id)
    page_header("Interview Management", "Manage interview sessions", ":material/forum:")

    interview_events = [
        event
        for event in list_events(limit=200, actor_admin_id=account.id)
        if event.event_type.startswith(("auth.login", "interview"))
    ]

    data_table(
        [
            {
                "Date": event.created_at.strftime("%Y-%m-%d %H:%M") if event.created_at else "",
                "Type": event.event_type,
                "Message": (event.message or "")[:120],
            }
            for event in interview_events[:20]
        ],
        "No interview events recorded.",
    )


show_admin_interviews()
