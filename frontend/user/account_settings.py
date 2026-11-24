import streamlit as st
from services.auth_service import AuthError, get_account, get_account_activity
from services.llm_service import get_provider_registry
from services.preferences_service import get_user_preferences, update_user_preferences

from frontend.shared.change_password import render_password_change_form
from frontend.shared.components import (
    empty_state,
    error_state,
    page_header,
    section,
    success_toast,
)

DIFFICULTIES = ("easy", "medium", "hard")
INTERVIEW_TYPES = ("Full Interview", "Technical", "Behavioral", "HR", "Mixed")
PROVIDERS = ("auto",) + tuple(get_provider_registry())
LEARNING_FORMATS = ("mixed", "articles", "videos", "practice")
NOTIFICATION_LABELS = {
    "interview_reminders": "Interview reminders",
    "learning_updates": "Learning updates",
    "achievements": "Achievements and badges",
    "admin_announcements": "Admin announcements",
    "system_updates": "System updates",
    "email_digest": "Email digest",
}
_INTERVIEW_KEYS = (
    "account_default_role",
    "account_difficulty",
    "account_interview_type",
    "account_voice_enabled",
    "account_preferred_categories",
)
_NOTIFICATION_KEYS = (
    "account_privacy",
    "account_learning_focus_skills",
    "account_learning_weekly_goal",
    "account_learning_format",
    *(f"account_notification_{key}" for key in NOTIFICATION_LABELS),
)


def _seed_widget(key: str, value: object) -> None:
    if key not in st.session_state:
        st.session_state[key] = value


def _discard_widgets(keys: tuple[str, ...]) -> None:
    for key in keys:
        st.session_state.pop(key, None)


def _format_timestamp(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "strftime") else str(value)


def show_account_settings() -> None:
    page_header(
        "Account settings",
        "Personalize your interview preferences, privacy choices, security and account activity.",
        ":material/tune:",
    )
    user_id = st.session_state.get("user_id")
    if not user_id:
        empty_state("Sign in to manage account settings.")
        return
    if st.session_state.get("account") is None:
        st.session_state.account = get_account(int(user_id))
    if st.session_state.get("account") is None:
        error_state("Account could not be loaded.", "Please sign in again.")
        return

    try:
        preferences = get_user_preferences(int(user_id), int(user_id))
        activity = get_account_activity(int(user_id), int(user_id))
    except (AuthError, ValueError) as exc:
        error_state("Settings could not be loaded.", str(exc))
        return

    preferences_tab, notifications_tab, security_tab = st.tabs(
        ["Interview preferences", "Notifications and privacy", "Security"]
    )
    with preferences_tab:
        _render_interview_preferences(int(user_id), preferences)
    with notifications_tab:
        _render_notification_preferences(int(user_id), preferences)
    with security_tab:
        _render_security(int(user_id), activity)


def _render_interview_preferences(user_id: int, preferences: dict) -> None:
    section("Interview preferences", ":material/forum:")
    st.caption("These defaults are used when starting a new practice session.")

    _seed_widget("account_default_role", preferences["default_role"])
    _seed_widget("account_difficulty", preferences["default_difficulty"])
    _seed_widget("account_interview_type", preferences["default_interview_type"])
    _seed_widget("account_voice_enabled", preferences["voice_enabled"])
    _seed_widget(
        "account_preferred_categories", preferences.get("preferred_interview_categories", [])
    )

    with st.form("account_preferences_form", border=True):
        col1, col2 = st.columns(2)
        with col1:
            default_role = st.text_input(
                "Default target role", max_chars=100, key="account_default_role"
            )
            difficulty = st.selectbox("Default difficulty", DIFFICULTIES, key="account_difficulty")
            interview_type = st.selectbox(
                "Default interview type", INTERVIEW_TYPES, key="account_interview_type"
            )
        with col2:
            st.selectbox(
                "Preferred AI provider",
                PROVIDERS,
                key="account_provider",
                disabled=True,
                help="Provider availability is configured at the platform level.",
            )
            voice_enabled = st.toggle("Enable voice answers", key="account_voice_enabled")
        preferred_categories = st.multiselect(
            "Preferred interview categories",
            INTERVIEW_TYPES,
            key="account_preferred_categories",
            help="Your first preferred category is offered as the default interview type.",
        )
        submitted = st.form_submit_button(
            "Save preferences", type="primary", icon=":material/save:", width="stretch"
        )

    if st.button(
        "Discard preference changes",
        key="account_discard_preferences",
        icon=":material/restart_alt:",
        width="stretch",
    ):
        _discard_widgets(_INTERVIEW_KEYS)
        st.rerun()

    if submitted:
        try:
            updated = update_user_preferences(
                user_id,
                default_role=default_role,
                default_difficulty=difficulty,
                default_interview_type=interview_type,
                voice_enabled=voice_enabled,
                preferred_interview_categories=preferred_categories,
                actor_user_id=user_id,
            )
        except (AuthError, ValueError) as exc:
            error_state("Preferences were not saved.", str(exc))
        else:
            st.session_state.user_preferences = updated
            success_toast("Interview preferences saved.")


def _render_notification_preferences(user_id: int, preferences: dict) -> None:
    section("Notifications", ":material/notifications:")
    notifications = preferences.get("notification_preferences", {})
    for key, label in NOTIFICATION_LABELS.items():
        _seed_widget(f"account_notification_{key}", bool(notifications.get(key, False)))

    with st.form("account_notification_form", border=True):
        for key, label in NOTIFICATION_LABELS.items():
            st.toggle(label, key=f"account_notification_{key}")
        submitted = st.form_submit_button(
            "Save notification choices", type="primary", icon=":material/save:", width="stretch"
        )

    if submitted:
        updated_notifications = {
            key: bool(st.session_state.get(f"account_notification_{key}", False))
            for key in NOTIFICATION_LABELS
        }
        try:
            updated = update_user_preferences(
                user_id,
                notification_preferences=updated_notifications,
                actor_user_id=user_id,
            )
        except (AuthError, ValueError) as exc:
            error_state("Notification choices were not saved.", str(exc))
        else:
            st.session_state.user_preferences = updated
            success_toast("Notification choices saved.")

    section("Learning preferences", ":material/school:")
    learning = preferences.get("learning_preferences", {})
    _seed_widget("account_learning_focus_skills", ", ".join(learning.get("focus_skills", [])))
    _seed_widget("account_learning_weekly_goal", int(learning.get("weekly_goal", 5)))
    _seed_widget("account_learning_format", learning.get("preferred_format", "mixed"))

    with st.form("account_learning_preferences_form", border=True):
        col1, col2 = st.columns(2)
        with col1:
            focus_skills = st.text_input(
                "Focus skills",
                key="account_learning_focus_skills",
                placeholder="e.g. Python, system design",
            )
            weekly_goal = st.number_input(
                "Weekly learning goal",
                min_value=1,
                max_value=40,
                key="account_learning_weekly_goal",
            )
        with col2:
            preferred_format = st.selectbox(
                "Preferred learning format", LEARNING_FORMATS, key="account_learning_format"
            )
        learning_submitted = st.form_submit_button(
            "Save learning preferences", type="primary", icon=":material/save:", width="stretch"
        )

    if learning_submitted:
        try:
            updated = update_user_preferences(
                user_id,
                learning_preferences={
                    "focus_skills": focus_skills,
                    "weekly_goal": weekly_goal,
                    "preferred_format": preferred_format,
                },
                actor_user_id=user_id,
            )
        except (AuthError, ValueError) as exc:
            error_state("Learning preferences were not saved.", str(exc))
        else:
            st.session_state.user_preferences = updated
            success_toast("Learning preferences saved.")

    section("Privacy", ":material/privacy:")
    _seed_widget("account_privacy", bool(preferences["privacy_share_analytics"]))
    with st.form("account_privacy_form", border=True):
        privacy = st.toggle(
            "Share anonymous usage analytics",
            key="account_privacy",
            help="Aggregated counts only; no answers, credentials or personal profile data.",
        )
        privacy_submitted = st.form_submit_button(
            "Save privacy choice", type="primary", icon=":material/save:", width="stretch"
        )
    if privacy_submitted:
        try:
            updated = update_user_preferences(
                user_id,
                privacy_share_analytics=privacy,
                actor_user_id=user_id,
            )
        except (AuthError, ValueError) as exc:
            error_state("Privacy choice was not saved.", str(exc))
        else:
            st.session_state.user_preferences = updated
            success_toast("Privacy choice saved.")

    if st.button(
        "Discard notification and privacy changes",
        key="account_discard_notifications",
        icon=":material/restart_alt:",
        width="stretch",
    ):
        _discard_widgets(_NOTIFICATION_KEYS)
        st.rerun()


def _render_security(user_id: int, activity: list[dict]) -> None:
    section("Change password", ":material/key:")
    st.caption(
        "Your current password is required. Passwords are never shown or stored in plain text."
    )
    render_password_change_form(
        user_id,
        form_id="account_password_form",
        key_prefix="account",
        submit_label="Update password",
    )

    section("Account activity", ":material/history:")
    st.caption(
        "Recent account events for this account. Passwords and credentials are never logged."
    )
    if not activity:
        empty_state("No account activity yet.", "Sign-ins and profile changes will appear here.")
        return
    for item in activity:
        with st.container(border=True):
            st.markdown(f"**{item['label']}**")
            st.caption(f"{_format_timestamp(item['created_at'])} · {item['message']}")


show_account_settings()
