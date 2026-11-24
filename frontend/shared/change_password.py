"""First login password change page and reusable password-change form."""

import re
from typing import Literal

import streamlit as st
from config.settings import settings
from services.auth_service import AuthError, change_password, get_account

from frontend.shared.auth_ui import clear_credential_state
from frontend.shared.components import error_state, page_header, success_toast


def _password_strength(password: str, minimum: int) -> tuple[float, str, list[tuple[str, bool]]]:
    checks = [
        (f"At least {minimum} characters", len(password) >= minimum),
        ("One letter", bool(re.search(r"[A-Za-z]", password))),
        ("One number", bool(re.search(r"\d", password))),
    ]
    ratio = sum(1 for _, passed in checks if passed) / len(checks)
    if not password:
        label = "Enter a new password"
    elif ratio == 1:
        label = "Strong"
    elif ratio >= 0.66:
        label = "Fair"
    else:
        label = "Weak"
    return ratio, label, checks


def render_password_change_form(
    user_id: int,
    *,
    form_id: str,
    key_prefix: str,
    submit_label: str,
    forced: bool = False,
) -> bool:
    minimum = int(getattr(settings, "password_min_length", 8))
    with st.container(border=True):
        st.markdown(":material/lock: **Secure password update**")
        st.caption("Use a strong, unique password. It is salted and hashed before storage.")
        show_password = st.toggle(
            "Show passwords",
            key=f"{key_prefix}_show_password",
            help="Reveal the characters you type.",
        )
        input_type: Literal["default", "password"] = "default" if show_password else "password"

        with st.form(form_id, border=False):
            current_password = st.text_input(
                "Current password",
                type=input_type,
                key=f"{key_prefix}_current_password",
                autocomplete="current-password",
            )
            new_password = st.text_input(
                "New password",
                type=input_type,
                key=f"{key_prefix}_new_password",
                autocomplete="new-password",
            )
            confirm_password = st.text_input(
                "Confirm new password",
                type=input_type,
                key=f"{key_prefix}_confirm_password",
                autocomplete="new-password",
            )

            ratio, label, checks = _password_strength(new_password, minimum)
            st.progress(ratio, text=f"Password strength · {label}")
            with st.container(horizontal=True, wrap=True, gap="small"):
                for requirement, passed in checks:
                    icon = (
                        ":material/check_circle:" if passed else ":material/radio_button_unchecked:"
                    )
                    st.markdown(f"{icon} :small[{requirement}]")
            if confirm_password:
                matches = new_password == confirm_password
                icon = ":material/check_circle:" if matches else ":material/error:"
                st.markdown(f"{icon} :small[Passwords match]")

            submitted = st.form_submit_button(
                submit_label, type="primary", icon=":material/lock:", width="stretch"
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
    st.badge("Action required", icon=":material/lock:", color="violet")
    render_password_change_form(
        int(account.id),
        form_id="first_login_password_form",
        key_prefix="first_login",
        submit_label="Set password and continue",
        forced=True,
    )


show_change_password_page()
