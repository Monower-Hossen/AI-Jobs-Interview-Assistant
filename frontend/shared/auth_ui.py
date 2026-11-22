"""Sign-in, registration and account management UI."""

import streamlit as st
from services.auth_service import (
    MAX_FULL_NAME_LENGTH,
    AuthError,
    authenticate,
    bind_session_to_account,
    change_password,
    get_account,
    log_event,
    update_profile,
)

from frontend.shared.components import page_header, section

TRANSIENT_KEYS: tuple[str, ...] = (
    "_ai_provider_health_results",
    "_ai_provider_last_success",
    "_confirm_delete_result",
    "account",
    "admin_create_confirm_password",
    "admin_create_password",
    "admin_reset_confirm_password",
    "admin_reset_password",
    "admin_user_confirm_password",
    "admin_user_password",
    "account_confirm_password",
    "account_current_password",
    "account_default_role",
    "account_difficulty",
    "account_interview_type",
    "account_new_password",
    "account_privacy",
    "account_provider",
    "account_voice_enabled",
    "answer_mode",
    "assessment_answers",
    "assessment_difficulty",
    "assessment_feedback",
    "assessment_questions",
    "assessment_topic",
    "behavioral_answers",
    "behavioral_difficulty",
    "behavioral_evaluations",
    "behavioral_questions",
    "behavioral_topic",
    "confirm_new_password",
    "current_page",
    "current_question_index",
    "current_password",
    "db_initialized",
    "email_verified",
    "first_login_confirm_password",
    "first_login_current_password",
    "first_login_new_password",
    "forgot_password_token",
    "history_selected_id",
    "interview_answered_questions",
    "interview_answers",
    "interview_config",
    "interview_evaluations",
    "interview_questions",
    "interview_question_ids",
    "interview_report",
    "interview_session_id",
    "interview_started",
    "interview_target_role",
    "job_analysis",
    "job_file_path",
    "job_text",
    "job_text_input",
    "job_text_pending",
    "kb_delete_label",
    "kb_delete_target",
    "last_evaluation",
    "last_evaluation_question",
    "learning_note_choice",
    "learning_note_delete",
    "match_analysis",
    "must_change_password",
    "new_password",
    "profile_confirm_password",
    "profile_current_password",
    "profile_new_password",
    "question_start_time",
    "resume_analysis",
    "resume_file_hash",
    "resume_file_path",
    "resume_scores",
    "resume_shown_empty_warning",
    "resume_text",
    "settings_gemini_key",
    "settings_groq_key",
    "show_evaluation",
    "shown_empty_warning",
    "sign_in_email",
    "sign_in_password",
    "user_id",
    "user_preferences",
    "user_session_id",
)
_TRANSIENT_PREFIXES = (
    "account_learning_",
    "account_notification_",
    "account_preferred_",
    "account_security_",
    "answer_",
    "kb_delete_",
    "learning_note_",
    "profile_career_",
    "profile_default_",
    "profile_discard_",
    "profile_education",
    "profile_full_",
    "profile_headline",
    "profile_notes",
    "profile_objective",
    "profile_open_",
    "profile_phone",
    "profile_preferred_",
    "profile_skills",
    "profile_target_",
    "profile_technical_",
    "profile_voice_",
    "profile_work_",
    "qbank_",
    "question_bank_",
    "transcript_",
    "voice_answer_",
    "voice_audio_hash_",
    "voice_audio_path_",
)
_TRANSIENT_SUFFIXES = ("_ack", "_resolved")
_CREDENTIAL_KEYS: tuple[str, ...] = (
    "account_confirm_password",
    "account_current_password",
    "account_new_password",
    "admin_create_confirm_password",
    "admin_create_password",
    "admin_reset_confirm_password",
    "admin_reset_password",
    "admin_user_confirm_password",
    "admin_user_password",
    "confirm_new_password",
    "current_password",
    "first_login_confirm_password",
    "first_login_current_password",
    "first_login_new_password",
    "new_password",
    "profile_confirm_password",
    "profile_current_password",
    "profile_new_password",
    "settings_gemini_key",
    "settings_groq_key",
    "sign_in_password",
)


def clear_credential_state() -> None:
    """Remove password/API-key widget values from Streamlit session state."""
    for key in _CREDENTIAL_KEYS:
        st.session_state.pop(key, None)


def show_auth_page() -> None:
    page_header("Welcome", "Sign in to your private interview workspace", ":material/login:")
    st.caption("Each account has an isolated workspace. Existing local sessions are preserved.")
    _render_sign_in()


def _render_sign_in() -> None:
    """Render the sign-in form."""
    with st.form("sign_in_form", border=False):
        email = st.text_input("Email", placeholder="Enter your email", key="sign_in_email")
        password = st.text_input(
            "Password", type="password", placeholder="Enter your password", key="sign_in_password"
        )
        submitted = st.form_submit_button("Sign in", type="primary", width="stretch")

    if submitted:
        clear_credential_state()
        if not email or not password:
            st.error("Email and password are required.", icon=":material/error:")
            return
        try:
            account = authenticate(email, password)
            if account is None:
                st.error(
                    "Invalid email or password, or your account is not active.",
                    icon=":material/error:",
                )
                return
            _activate_account(account)
        except AuthError as exc:
            st.error(str(exc), icon=":material/error:")


def _activate_account(account) -> None:
    legacy = st.session_state.get("user_session_id")
    token = bind_session_to_account(account, legacy)
    st.session_state.user_id = account.id
    st.session_state.account = get_account(account.id) or account
    st.session_state.user_session_id = token
    st.session_state.db_initialized = True
    # Force password change on first login when required
    st.session_state.must_change_password = bool(
        getattr(st.session_state.account, "must_change_password", False)
    )
    log_event("auth.session_start", user_id=account.id)
    if st.session_state.must_change_password:
        st.info("Please set a new password to continue.", icon=":material/security:")
    else:
        st.toast(f"Welcome, {st.session_state.account.full_name}!", icon=":material/check_circle:")
    st.rerun()


def sign_out() -> None:
    user_id = st.session_state.get("user_id")
    if user_id:
        log_event("auth.logout", user_id=user_id)
    keys = set(TRANSIENT_KEYS)
    for key in list(st.session_state.keys()):
        if isinstance(key, str) and (
            key.startswith(_TRANSIENT_PREFIXES) or key.endswith(_TRANSIENT_SUFFIXES)
        ):
            keys.add(key)
    for key in keys:
        st.session_state.pop(key, None)
    clear_credential_state()
    st.rerun()


def show_account_section() -> None:
    account = st.session_state.get("account")
    if not account:
        return
    section("Account", ":material/person:")
    with st.container(border=True):
        st.markdown(f"**{account.full_name}**")
        st.caption(f"{account.email} • {account.role.title()} account")
        st.text_input("Email", value=account.email, disabled=True)
        if getattr(account, "username", None):
            st.text_input("Username", value=account.username, disabled=True)
        with st.form("profile_form", border=False):
            st.markdown("**Edit profile**")
            full_name = st.text_input(
                "Full name",
                value=account.full_name,
                key="profile_full_name",
                max_chars=MAX_FULL_NAME_LENGTH,
            )
            if st.form_submit_button("Save profile", width="stretch"):
                try:
                    update_profile(account.id, full_name)
                except AuthError as exc:
                    st.error(str(exc), icon=":material/error:")
                else:
                    st.session_state.account = get_account(account.id)
                    st.success("Profile updated.", icon=":material/check_circle:")
        with st.form("change_password_form", border=False):
            st.markdown("**Change password**")
            current = st.text_input("Current password", type="password", key="current_password")
            new_password = st.text_input("New password", type="password", key="new_password")
            confirm_password = st.text_input(
                "Confirm new password", type="password", key="confirm_new_password"
            )
            if st.form_submit_button("Update password", width="stretch"):
                clear_credential_state()
                if new_password != confirm_password:
                    st.error("Passwords do not match.", icon=":material/error:")
                else:
                    try:
                        change_password(account.id, current, new_password)
                    except AuthError as exc:
                        st.error(str(exc), icon=":material/error:")
                    else:
                        st.session_state.account = get_account(account.id)
                        st.success("Password updated.", icon=":material/check_circle:")
        if st.button("Sign out", icon=":material/logout:", width="stretch"):
            sign_out()
