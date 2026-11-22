"""First login password change page and reusable password-change form."""

import streamlit as st
from services.auth_service import AuthError, change_password, get_account

from frontend.shared.auth_ui import clear_credential_state
from frontend.shared.components import error_state, page_header, success_toast


def render_password_change_form(
    user_id: int,
    *,
    form_id: str,
    key_prefix: str,
    submit_label: str,
    forced: bool = False,
) -> bool:
    with st.form(form_id, border=False):
        current_password = st.text_input(
            "Current password",
            type="password",
            key=f"{key_prefix}_current_password",
        )
        new_password = st.text_input(
            "New password",
            type="password",
            key=f"{key_prefix}_new_password",
        )
        confirm_password = st.text_input(
            "Confirm new password",
            type="password",
            key=f"{key_prefix}_confirm_password",
        )
        submitted = st.form_submit_button(
            submit_label, type="primary", width="stretch"
        )

    if not submitted:
        return False

    clear_credential_state()
    if not current_password or not new_password or not confirm_password:
        error_state(
            "Please fill in all password fields.",
            "Current, new, and confirm password are required.",
        )
        return False
    if new_password != confirm_password:
        error_state("Passwords do not match.", "Please make sure both password entries match.")
        return False

    try:
        change_password(int(user_id), current_password, new_password, actor_user_id=int(user_id))
    except AuthError as exc:
        error_state("Could not update password.", str(exc))
        return False

    st.session_state.must_change_password = False
    refreshed = get_account(int(user_id))
    if refreshed:
        st.session_state.account = refreshed
    success_toast("Password updated successfully.")
    if forced:
        st.rerun()
    return True


def show_change_password_page() -> None:
    account = st.session_state.get("account")
    user_id = st.session_state.get("user_id")
    if user_id and account is None:
        account = get_account(int(user_id))

    if not account:
        st.error("No account found. Please sign in.", icon=":material/error:")
        return

    page_header(
        "Set new password",
        "For security, you must set a new password before continuing.",
        ":material/security:",
    )
    st.caption("Enter your current or temporary password, then choose a new password.")
    render_password_change_form(
        int(account.id),
        form_id="first_login_password_form",
        key_prefix="first_login",
        submit_label="Set password and continue",
        forced=True,
    )


show_change_password_page()
