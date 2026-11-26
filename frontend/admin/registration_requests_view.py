"""Shared UI for reviewing admin registration requests."""

from __future__ import annotations

from typing import Literal, cast

import streamlit as st
from services.auth_service import Account, AuthError, require_admin
from services.registration_service import (
    RegistrationRequestView,
    approve_registration_request,
    get_registration_request_counts,
    list_registration_requests,
    reject_registration_request,
)

from frontend.shared.components import (
    data_table,
    error_state,
    page_header,
    section,
    success_toast,
)

_BadgeColor = Literal[
    "red", "orange", "yellow", "blue", "green", "violet", "gray", "grey", "primary"
]
_STATUS_COLORS: dict[str, _BadgeColor] = {
    "pending": "orange",
    "approved": "green",
    "rejected": "red",
}


def _require_admin() -> Account:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        st.error("Administrator access required.", icon=":material/error:")
        st.stop()
    require_admin(getattr(account, "id", None))
    return cast(Account, account)


def _status_badge(status: str) -> None:
    st.badge(
        status.title(),
        color=_STATUS_COLORS.get(status, "gray"),
        icon=":material/schedule:" if status == "pending" else ":material/fact_check:",
    )


def _format_timestamp(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "N/A"


@st.dialog("Approve registration request", width="small")
def _approve_dialog(request: RegistrationRequestView, actor_admin_id: int) -> None:
    st.warning(
        f"Approve **{request.full_name}** ({request.email})? This creates a new user account "
        "with the submitted password and access to the workspace.",
        icon=":material/warning:",
    )
    st.caption(
        "The account will be active immediately and will not be forced to change its password."
    )
    confirmed = st.checkbox("I have verified this request", key="approve_registration_ack")
    cancel_col, confirm_col = st.columns(2)
    if cancel_col.button("Cancel", width="stretch"):
        st.rerun()
    if confirm_col.button(
        "Approve request", type="primary", disabled=not confirmed, width="stretch"
    ):
        try:
            _, email = approve_registration_request(
                request_id=request.id, actor_admin_id=actor_admin_id
            )
        except AuthError as exc:
            error_state("Could not approve registration request.", str(exc))
            return
        success_toast(f"Account created for {email}.")
        st.rerun()


@st.dialog("Reject registration request", width="small")
def _reject_dialog(request: RegistrationRequestView, actor_admin_id: int) -> None:
    st.warning(
        f"Reject the request from **{request.full_name}** ({request.email})? "
        "No account will be created.",
        icon=":material/warning:",
    )
    confirmed = st.checkbox("I have verified this request", key="reject_registration_ack")
    cancel_col, confirm_col = st.columns(2)
    if cancel_col.button("Cancel", width="stretch"):
        st.rerun()
    if confirm_col.button(
        "Reject request", type="primary", disabled=not confirmed, width="stretch"
    ):
        try:
            reject_registration_request(request_id=request.id, actor_admin_id=actor_admin_id)
        except AuthError as exc:
            error_state("Could not reject registration request.", str(exc))
            return
        success_toast("Registration request rejected.")
        st.rerun()


def _select_request(requests: list[RegistrationRequestView]) -> RegistrationRequestView | None:
    options = {
        f"{request.full_name} ({request.email}, {request.status}) [id {request.id}]": request.id
        for request in requests
    }
    selected_label = st.selectbox(
        "Select request to review", list(options.keys()), key="admin_selected_registration"
    )
    selected_id = options[selected_label]
    return next((request for request in requests if request.id == selected_id), None)


def _render_request_actions(request: RegistrationRequestView, admin: Account) -> None:
    section("Selected Request", ":material/fact_check:")
    _status_badge(request.status)
    st.caption(
        f"ID: {request.id} | Submitted: {_format_timestamp(request.submitted_at)} | "
        f"Reviewed: {_format_timestamp(request.reviewed_at)} | "
        f"Reviewer admin ID: {request.reviewed_by_admin_id or 'N/A'} | "
        f"Created user ID: {request.created_user_id or 'N/A'}"
    )
    st.markdown(f"**Name:** {request.full_name}")
    st.markdown(f"**Email:** {request.email}")

    if request.status != "pending":
        st.info("This request has already been reviewed. Its decision is shown for audit purposes.")
        return

    approve_col, reject_col = st.columns(2)
    if approve_col.button("Approve", icon=":material/check:", type="primary", width="stretch"):
        _approve_dialog(request, admin.id)
    if reject_col.button("Reject", icon=":material/close:", width="stretch"):
        _reject_dialog(request, admin.id)


def render_registration_requests_section(admin: Account) -> None:
    counts = get_registration_request_counts(actor_admin_id=admin.id)
    pending_col, approved_col, rejected_col = st.columns(3)
    pending_col.metric("Pending", counts.get("pending", 0))
    approved_col.metric("Approved", counts.get("approved", 0))
    rejected_col.metric("Rejected", counts.get("rejected", 0))

    status_filter = st.segmented_control(
        "Status",
        ["pending", "approved", "rejected"],
        format_func=str.title,
        default="pending",
        key="registration_request_status_filter",
    )
    selected_status = status_filter or "pending"
    requests = list_registration_requests(actor_admin_id=admin.id, status=selected_status)

    section("Requests", ":material/table:")
    data_table(
        [
            {
                "ID": request.id,
                "Full Name": request.full_name,
                "Email": request.email,
                "Status": request.status.title(),
                "Submitted": _format_timestamp(request.submitted_at),
                "Reviewed": _format_timestamp(request.reviewed_at),
                "Created User ID": request.created_user_id or "",
            }
            for request in requests
        ],
        "No registration requests match the current filter.",
    )

    if not requests:
        return

    selected = _select_request(requests)
    if selected is not None:
        _render_request_actions(selected, admin)


def show_admin_registration_requests() -> None:
    admin = _require_admin()
    page_header(
        "Registration Requests",
        "Review access requests and create approved accounts",
        ":material/how_to_reg:",
    )
    render_registration_requests_section(admin)
