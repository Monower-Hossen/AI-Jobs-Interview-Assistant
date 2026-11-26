"""Public registration request page."""

import streamlit as st
from config.settings import settings
from services.auth_service import MAX_FULL_NAME_LENGTH, AuthError
from services.registration_service import submit_registration_request

from frontend.shared.auth_ui import clear_credential_state
from frontend.shared.components import error_state

REGISTRATION_SUCCESS_MESSAGE = (
    "Your registration request has been submitted successfully. "
    "Please wait for administrator approval before logging in."
)


def show_registration_page() -> None:
    st.title("Create account", icon=":material/person_add:", text_alignment="center")
    st.caption(
        "Request access to your interview workspace. An administrator will review your request.",
        text_alignment="center",
    )

    notice = st.session_state.pop("registration_success_notice", None)
    if notice:
        st.success(notice, icon=":material/check_circle:")

    with (
        st.container(width="stretch", horizontal_alignment="center"),
        st.container(border=True, width=440),
    ):
        with st.form("registration_request_form", border=False, clear_on_submit=True):
            full_name = st.text_input(
                "Full Name *",
                placeholder="Jane Doe",
                max_chars=MAX_FULL_NAME_LENGTH,
                required=True,
                autocomplete="name",
            )
            email = st.text_input(
                "Email Address *",
                type="email",
                placeholder="jane@example.com",
                required=True,
                autocomplete="username",
            )
            password = st.text_input(
                "Password *",
                type="password",
                key="registration_password",
                placeholder="Enter a password",
                required=True,
                autocomplete="new-password",
            )
            confirm_password = st.text_input(
                "Confirm Password *",
                type="password",
                key="registration_confirm_password",
                placeholder="Re-enter your password",
                required=True,
                autocomplete="new-password",
            )
            st.caption(
                f"Password: at least {settings.password_min_length} characters, "
                "including a letter and a number."
            )
            submitted = st.form_submit_button(
                "Submit registration request",
                type="primary",
                icon=":material/send:",
                width="stretch",
            )

        if submitted:
            clear_credential_state()
            if not (full_name and email and password and confirm_password):
                error_state(
                    "All fields are required.",
                    "Enter your full name, email, password, and password confirmation.",
                )
                return
            try:
                submit_registration_request(
                    full_name=full_name,
                    email=email,
                    password=password,
                    confirm_password=confirm_password,
                )
            except AuthError as exc:
                error_state("Could not submit registration request.", str(exc))
            else:
                st.session_state["registration_success_notice"] = REGISTRATION_SUCCESS_MESSAGE
                st.rerun()


show_registration_page()
