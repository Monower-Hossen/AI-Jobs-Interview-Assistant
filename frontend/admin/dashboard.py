"""Admin dashboard page."""

import streamlit as st
from config.settings import settings
from database import (
    Assessment,
    InterviewSession,
    KnowledgeDocument,
    ResumeAnalysis,
    get_db_context,
)
from services.auth_service import (
    AuthError,
    admin_create_user,
    get_user_statistics,
    list_events,
    list_users,
    require_admin,
    set_user_role,
    set_user_status,
)
from services.llm_service import check_provider_health

from frontend.shared.auth_ui import clear_credential_state
from frontend.shared.components import (
    data_table,
    error_state,
    kpi_row,
    page_header,
    section,
)


def show_admin_dashboard() -> None:
    page_header(
        "Admin dashboard",
        "Platform health, usage and account management",
        ":material/admin_panel_settings:",
    )
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        error_state("Administrator access required.")
        st.stop()
    require_admin(account.id)

    account_stats = get_user_statistics(actor_admin_id=account.id)
    with get_db_context() as db:
        interviews = db.query(InterviewSession).count()
        documents = db.query(KnowledgeDocument).count()
        resumes = db.query(ResumeAnalysis).count()
        assessments = db.query(Assessment).count()

    event_rows = [
        {
            "Time": event.created_at.strftime("%Y-%m-%d %H:%M") if event.created_at else "",
            "Type": event.event_type,
            "Level": event.level,
            "Message": event.message,
        }
        for event in list_events(actor_admin_id=account.id)
    ]

    kpi_row(
        [
            ("Total users", account_stats["total"], None),
            ("Active users", account_stats["active"], None),
            ("Inactive users", account_stats["inactive"], None),
            ("Suspended users", account_stats["suspended"], None),
            ("New users (7d)", account_stats["new_last_7_days"], None),
            ("Interviews", interviews, None),
            ("Knowledge docs", documents, None),
            ("Assessments", assessments, None),
        ]
    )

    section("Users", ":material/group:")
    st.page_link(
        "frontend/admin/users.py",
        label="Open user management",
        icon=":material/manage_accounts:",
    )
    users = list_users(actor_admin_id=account.id)
    data_table(
        [
            {
                "ID": user.id,
                "Email": user.email,
                "Username": user.username or "",
                "Name": user.full_name,
                "Role": user.role,
                "Status": user.account_status.title(),
                "Created": user.created_at.strftime("%Y-%m-%d") if user.created_at else "",
                "Last Login": (
                    user.last_login.strftime("%Y-%m-%d %H:%M") if user.last_login else "Never"
                ),
            }
            for user in users
        ],
        "No users found.",
    )
    if users:
        selected = st.selectbox(
            "Manage user",
            [f"{user.email} (@{user.username or 'no-username'}, id {user.id})" for user in users],
            key="dashboard_manage_user",
        )
        selected_id = int(selected.rsplit("id ", 1)[-1].rstrip(")"))
        selected_user = next(user for user in users if user.id == selected_id)
        col1, col2, col3 = st.columns(3)
        with col1:
            new_role = st.selectbox(
                "Role",
                ["user", "admin"],
                index=1 if selected_user.role == "admin" else 0,
                key="dashboard_role",
            )
            if st.button("Update role", width="stretch", key="dashboard_update_role"):
                try:
                    set_user_role(selected_id, new_role, actor_admin_id=account.id)
                    st.toast("Role updated.", icon=":material/check_circle:")
                    st.rerun()
                except AuthError as exc:
                    error_state("Could not update role.", str(exc))
        with col2:
            new_status = st.selectbox(
                "Status",
                ["active", "inactive", "suspended"],
                index=(
                    ["active", "inactive", "suspended"].index(selected_user.account_status)
                    if selected_user.account_status in ["active", "inactive", "suspended"]
                    else 0
                ),
                key="dashboard_status",
            )
            if st.button("Update status", width="stretch", key="dashboard_update_status"):
                try:
                    set_user_status(selected_id, new_status, actor_admin_id=account.id)
                    st.toast("Status updated.", icon=":material/check_circle:")
                    st.rerun()
                except AuthError as exc:
                    error_state("Could not update status.", str(exc))
        with col3:
            st.caption(
                "For full user management (create, edit, reset password, delete), use the Users page."
            )

    section("AI provider status", ":material/cloud:")
    try:
        health = check_provider_health()
        data_table(
            [
                {
                    "Provider": name.title(),
                    "Status": info.get("status", "Not checked"),
                    "Model": info.get("model", ""),
                    "Error": info.get("error") or "",
                }
                for name, info in health.items()
            ],
            "No providers configured.",
        )
    except Exception as exc:
        error_state("Provider health check failed.", str(exc))

    section("System configuration", ":material/tune:")
    st.caption(
        f"Database: {settings.database_path} • Vector store: {settings.vector_store_path} • "
        f"Embeddings: {settings.embedding_model} • Resumes analysed: {resumes}"
    )

    section("Recent activity", ":material/history:")
    data_table(event_rows, "No activity recorded.")

    section("Quick Create User", ":material/person_add:")
    notice = st.session_state.pop("admin_create_user_notice", None)
    if notice:
        st.success(notice, icon=":material/check_circle:")
    with st.form("dashboard_create_user_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            new_full_name = st.text_input("Full Name *", width="stretch")
            new_email = st.text_input(
                "Email Address *", placeholder="user@example.com", width="stretch"
            )
            new_username = st.text_input("Username *", placeholder="username", width="stretch")
        with c2:
            new_password = st.text_input(
                "Password *",
                type="password",
                width="stretch",
                key="admin_create_password",
            )
            confirm_password = st.text_input(
                "Confirm Password *",
                type="password",
                width="stretch",
                key="admin_create_confirm_password",
            )
            new_status = st.selectbox(
                "Account Status", ["active", "inactive", "suspended"], index=0
            )
        phone = st.text_input("Phone Number", width="stretch")
        st.caption(
            f"Password: at least {settings.password_min_length} characters, "
            "including a letter and a number."
        )
        submitted = st.form_submit_button("Create Account", type="primary", width="stretch")
        if submitted:
            clear_credential_state()
            if not (
                new_full_name and new_email and new_username and new_password and confirm_password
            ):
                error_state(
                    "Missing required fields.",
                    "Full name, email, username, password, and confirm password are required.",
                )
            else:
                try:
                    result = admin_create_user(
                        actor_admin_id=account.id,
                        full_name=new_full_name.strip(),
                        email=new_email.strip(),
                        username=new_username.strip(),
                        password=new_password,
                        confirm_password=confirm_password,
                        phone_number=phone.strip() or None,
                        account_status=new_status,
                        created_by_admin_id=account.id,
                    )
                    st.session_state["admin_create_user_notice"] = (
                        f"User account created for {result.email}. Share credentials securely; "
                        "the user must change the password on first login."
                    )
                    st.rerun()
                except AuthError as e:
                    error_state("Failed to create user.", str(e))
                except Exception as e:
                    error_state("Failed to create user.", str(e))


show_admin_dashboard()
