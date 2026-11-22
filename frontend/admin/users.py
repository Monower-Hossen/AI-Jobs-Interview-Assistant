"""Admin user management page."""

from __future__ import annotations

from typing import Literal, cast

import streamlit as st
from config.settings import settings
from services.auth_service import (
    Account,
    AuthError,
    admin_create_user,
    admin_reset_password,
    delete_user,
    list_users,
    require_admin,
    set_user_status,
    update_user_account,
)

from frontend.shared.auth_ui import clear_credential_state
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
    "active": "green",
    "inactive": "gray",
    "suspended": "red",
}


def _require_admin() -> Account:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        st.error("Administrator access required.", icon=":material/error:")
        st.stop()
    require_admin(getattr(account, "id", None))
    return cast(Account, account)


def _status_for_row(user) -> str:
    return getattr(
        user, "account_status", "active" if getattr(user, "is_active", False) else "inactive"
    )


def _status_badge(status: str) -> None:
    st.badge(
        status.title(),
        color=_STATUS_COLORS.get(status, "gray"),
        icon=":material/check_circle:" if status == "active" else ":material/pause_circle:",
    )


@st.dialog("Delete user account", width="small")
def _delete_user_dialog(user, actor_admin_id: int) -> None:
    st.warning(
        f"Permanently delete **{user.full_name}** ({user.email})? This cannot be undone.",
        icon=":material/warning:",
    )
    st.caption("The account, its workspace sessions, and its activity log will be removed.")
    confirmed = st.checkbox(
        "I understand this action is permanent", key=f"confirm_delete_ack_{user.id}"
    )
    cancel_col, confirm_col = st.columns(2)
    if cancel_col.button("Cancel", width="stretch"):
        st.rerun()
    if confirm_col.button(
        "Delete account", type="primary", disabled=not confirmed, width="stretch"
    ):
        try:
            delete_user(user.id, actor_admin_id=actor_admin_id)
        except AuthError as exc:
            error_state("Could not delete user.", str(exc))
            return
        success_toast("User account deleted.")
        st.rerun()


def show_admin_users() -> None:
    admin = _require_admin()
    page_header("User Management", "Create, update, and manage user accounts", ":material/group:")

    users = list_users(actor_admin_id=admin.id)
    _render_create_form(admin)
    _render_users_table(users)

    if not users:
        st.info("No users found.")
        return

    selected = _select_user(users)
    if selected is None:
        return
    _render_user_actions(selected, admin)


def _render_create_form(admin) -> None:
    section("Create User", ":material/person_add:")
    notice = st.session_state.pop("admin_create_user_notice", None)
    if notice:
        st.success(notice, icon=":material/check_circle:")
    with st.container(border=True):
        with st.form("admin_create_user_form", clear_on_submit=True):
            col1, col2 = st.columns(2)
            with col1:
                full_name = st.text_input("Full Name *", placeholder="Jane Doe")
                email = st.text_input("Email Address *", placeholder="jane@example.com")
                username = st.text_input("Username *", placeholder="jane.doe")
                phone_number = st.text_input("Phone Number", placeholder="Optional")
            with col2:
                password = st.text_input(
                    "Password *", type="password", key="admin_user_password"
                )
                confirm_password = st.text_input(
                    "Confirm Password *", type="password", key="admin_user_confirm_password"
                )
                status = st.selectbox(
                    "Account Status", ["Active", "Inactive", "Suspended"], index=0
                )
                additional_profile_info = st.text_area(
                    "Additional Profile Information", placeholder="Optional notes"
                )
            st.caption(
                f"Password: at least {settings.password_min_length} characters, "
                "including a letter and a number."
            )
            submitted = st.form_submit_button(
                "Create User Account", type="primary", width="stretch"
            )

        if submitted:
            clear_credential_state()
            if not (full_name and email and username and password and confirm_password):
                error_state(
                    "Missing required fields.",
                    "Full name, email, username, password, and confirm password are required.",
                )
                return
            try:
                user = admin_create_user(
                    actor_admin_id=admin.id,
                    full_name=full_name,
                    email=email,
                    username=username,
                    password=password,
                    confirm_password=confirm_password,
                    phone_number=phone_number,
                    account_status=status.lower(),
                    additional_profile_info=additional_profile_info,
                    created_by_admin_id=getattr(admin, "id", None),
                )
                st.session_state["admin_create_user_notice"] = (
                    f"User account created for {user.email}. Share credentials securely; "
                    "the user must change the password on first login."
                )
                st.rerun()
            except AuthError as exc:
                error_state("Could not create user.", str(exc))
            except Exception as exc:
                error_state("Could not create user.", str(exc))


def _render_users_table(users) -> None:
    section("All Users", ":material/table:")
    search = st.text_input(
        "Search users", placeholder="Search by name, email, or username", key="admin_user_search"
    )
    status_filter = st.selectbox(
        "Filter by status",
        ["All", "Active", "Inactive", "Suspended"],
        key="admin_user_status_filter",
    )

    filtered = []
    query = (search or "").strip().lower()
    for user in users:
        status = _status_for_row(user).title()
        if status_filter != "All" and status != status_filter:
            continue
        haystack = " ".join(
            [
                str(getattr(user, "full_name", "") or ""),
                str(getattr(user, "email", "") or ""),
                str(getattr(user, "username", "") or ""),
            ]
        ).lower()
        if query and query not in haystack:
            continue
        filtered.append(user)

    data_table(
        [
            {
                "ID": user.id,
                "Full Name": user.full_name,
                "Email": user.email,
                "Username": user.username or "",
                "Status": _status_for_row(user).title(),
                "Role": user.role,
                "Created": user.created_at.strftime("%Y-%m-%d %H:%M") if user.created_at else "",
                "Last Login": (
                    user.last_login.strftime("%Y-%m-%d %H:%M") if user.last_login else "Never"
                ),
                "Phone": user.phone_number or "",
            }
            for user in filtered
        ],
        "No users match the current filters.",
    )


def _select_user(users):
    options = {
        f"{u.full_name} ({u.email}, @{u.username or 'no-username'}, { _status_for_row(u).title() }) [id {u.id}]": u.id
        for u in users
    }
    selected_label = st.selectbox(
        "Select user to manage", list(options.keys()), key="admin_selected_user"
    )
    selected_id = options[selected_label]
    return next((u for u in users if u.id == selected_id), None)


def _render_user_actions(user, admin) -> None:
    section("Manage Selected User", ":material/manage_accounts:")
    status = _status_for_row(user)
    is_self = user.id == getattr(admin, "id", None)
    _status_badge(status)
    st.caption(
        f"ID: {user.id} | Role: {user.role} | "
        f"Created: {user.created_at.strftime('%Y-%m-%d %H:%M') if user.created_at else 'N/A'} | "
        f"Last login: {user.last_login.strftime('%Y-%m-%d %H:%M') if user.last_login else 'Never'} | "
        f"Phone: {user.phone_number or 'not provided'}"
    )
    if user.additional_profile_info:
        st.caption(f"Profile notes: {user.additional_profile_info}")

    with st.container(border=True):
        st.markdown("#### Edit User")
        with st.form(f"edit_user_{user.id}"):
            col1, col2 = st.columns(2)
            with col1:
                full_name = st.text_input("Full Name", value=user.full_name or "")
                email = st.text_input("Email Address", value=user.email or "")
                username = st.text_input("Username", value=user.username or "")
            with col2:
                phone_number = st.text_input("Phone Number", value=user.phone_number or "")
                status_value = st.selectbox(
                    "Account Status",
                    ["Active", "Inactive", "Suspended"],
                    index=(
                        ["active", "inactive", "suspended"].index(status)
                        if status in ["active", "inactive", "suspended"]
                        else 0
                    ),
                )
                additional_profile_info = st.text_area(
                    "Additional Profile Information", value=user.additional_profile_info or ""
                )
            if st.form_submit_button("Save Changes", type="primary", width="stretch"):
                try:
                    update_user_account(
                        user.id,
                        actor_admin_id=admin.id,
                        full_name=full_name,
                        email=email,
                        username=username,
                        phone_number=phone_number,
                        account_status=status_value.lower(),
                        additional_profile_info=additional_profile_info,
                    )
                    success_toast("User updated.")
                    st.rerun()
                except AuthError as exc:
                    error_state("Could not update user.", str(exc))

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        st.markdown("#### Account status")
        new_status = st.selectbox(
            "Set status",
            ["active", "inactive", "suspended"],
            index=["active", "inactive", "suspended"].index(status),
            key=f"status_{user.id}",
        )
        if st.button(
            "Update status",
            key=f"update_status_{user.id}",
            width="stretch",
            disabled=is_self,
        ):
            try:
                set_user_status(user.id, new_status, actor_admin_id=admin.id)
                success_toast(f"Status set to {new_status}.")
                st.rerun()
            except AuthError as exc:
                error_state("Could not update status.", str(exc))

        with st.container(horizontal=True):
            for label, target in (
                ("Activate", "active"),
                ("Deactivate", "inactive"),
                ("Suspend", "suspended"),
            ):
                if st.button(
                    label,
                    key=f"quick_{label.lower()}_{user.id}",
                    disabled=status == target or is_self,
                    width="content",
                ):
                    try:
                        set_user_status(user.id, target, actor_admin_id=admin.id)
                        success_toast(f"Account {target}.")
                        st.rerun()
                    except AuthError as exc:
                        error_state("Could not update status.", str(exc))
        if is_self:
            st.caption("You cannot change the status of your own account.")

    with col_b:
        st.markdown("#### Password reset")
        with st.form(f"reset_pw_{user.id}"):
            new_password = st.text_input(
                "New temporary password", type="password", key="admin_reset_password"
            )
            confirm_password = st.text_input(
                "Confirm temporary password",
                type="password",
                key="admin_reset_confirm_password",
            )
            if st.form_submit_button("Reset password", width="stretch"):
                clear_credential_state()
                if not new_password or not confirm_password:
                    error_state(
                        "Password required.", "Enter and confirm the new temporary password."
                    )
                elif new_password != confirm_password:
                    error_state("Passwords do not match.", "Please confirm the same password.")
                else:
                    try:
                        admin_reset_password(user.id, new_password, actor_admin_id=admin.id)
                        success_toast("Password reset. User must change it on next login.")
                        st.info(
                            "Share the new temporary password securely with the user.",
                            icon=":material/key:",
                        )
                    except AuthError as exc:
                        error_state("Could not reset password.", str(exc))

    with col_c:
        st.markdown("#### Delete user")
        st.caption("Deletion requires confirmation in a dialog and cannot be undone.")
        if st.button(
            "Delete user",
            key=f"delete_{user.id}",
            type="secondary",
            width="stretch",
            disabled=(user.role == "admin" or is_self),
        ):
            _delete_user_dialog(user, admin.id)
        if user.role == "admin":
            st.caption("Admin accounts cannot be deleted here.")
        elif is_self:
            st.caption("You cannot delete your own account.")

    _render_user_activity(user)


def _render_user_activity(user) -> None:
    section("User Activity", ":material/history:")
    try:
        from database import EventLog, InterviewSession, UserSession, get_db_context

        with get_db_context() as db:
            events = (
                db.query(EventLog)
                .filter(EventLog.user_id == user.id)
                .order_by(EventLog.created_at.desc())
                .limit(20)
                .all()
            )
            session_ids = [
                row.session_id
                for row in db.query(UserSession.session_id)
                .filter(UserSession.user_id == user.id)
                .all()
            ]
            interview_count = 0
            recent_interviews = []
            if session_ids:
                interview_count = (
                    db.query(InterviewSession)
                    .filter(InterviewSession.session_id.in_(session_ids))
                    .count()
                )
                recent_interviews = (
                    db.query(InterviewSession)
                    .filter(InterviewSession.session_id.in_(session_ids))
                    .order_by(InterviewSession.created_at.desc())
                    .limit(10)
                    .all()
                )

        st.caption(f"Total interview sessions: {interview_count}")
        data_table(
            [
                {
                    "Time": e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else "",
                    "Type": e.event_type,
                    "Level": e.level,
                    "Message": e.message,
                }
                for e in events
            ],
            "No activity recorded for this user.",
        )
        if recent_interviews:
            st.markdown("**Recent Interviews**")
            data_table(
                [
                    {
                        "ID": i.id,
                        "Type": i.interview_type or "",
                        "Status": i.status or "",
                        "Score": round(i.overall_score, 1) if i.overall_score is not None else "",
                        "Date": i.created_at.strftime("%Y-%m-%d %H:%M") if i.created_at else "",
                    }
                    for i in recent_interviews
                ],
                "No interviews found.",
            )
    except Exception as exc:
        st.warning(f"Could not load user activity: {exc}")


show_admin_users()
