import streamlit as st
from database import ResumeAnalysis, get_db_context
from services.auth_service import AuthError, get_account, owned_session_id
from services.preferences_service import get_user_preferences, update_user_preferences
from services.profile_service import (
    get_profile_recommendations,
    get_user_profile,
    update_user_profile,
)

from frontend.shared.change_password import render_password_change_form
from frontend.shared.components import (
    empty_state,
    error_state,
    kpi_row,
    page_header,
    section,
    success_toast,
)
from frontend.shared.navigation import go_to

DIFFICULTIES = ("easy", "medium", "hard")
INTERVIEW_TYPES = ("Full Interview", "Technical", "Behavioral", "HR", "Mixed")
_PROFILE_FORM_KEYS = (
    "profile_full_name",
    "profile_phone",
    "profile_headline",
    "profile_objective",
    "profile_notes",
    "profile_skills",
    "profile_technical_interests",
    "profile_education",
    "profile_work_experience",
    "profile_career_interests",
)
_INTERVIEW_FORM_KEYS = (
    "profile_target_role",
    "profile_default_difficulty",
    "profile_default_type",
    "profile_voice_enabled",
    "profile_preferred_categories",
)


def _workspace_id() -> int | None:
    token = st.session_state.get("user_session_id")
    if not token:
        return None
    return owned_session_id(token, st.session_state.get("user_id"))


def _seed_widget(key: str, value: object) -> None:
    if key not in st.session_state:
        st.session_state[key] = value


def _discard_widgets(keys: tuple[str, ...]) -> None:
    for key in keys:
        st.session_state.pop(key, None)


def show_user_profile() -> None:
    account = st.session_state.get("account")
    if not account:
        st.error("No account found. Please sign in.", icon=":material/error:")
        return

    page_header(
        "My profile", "Manage your personal, interview and account details", ":material/person:"
    )
    user_id = int(account.id)
    try:
        profile = get_user_profile(user_id, user_id)
        preferences = get_user_preferences(user_id, user_id)
        recommendations = get_profile_recommendations(user_id, user_id)
    except (AuthError, ValueError) as exc:
        error_state("Profile could not be loaded.", str(exc))
        return

    completion = profile["completion"]
    kpi_row(
        [
            ("Profile completion", f"{completion['percent']}%", None),
            ("Completed sections", f"{completion['completed']}/{completion['total']}", None),
        ]
    )

    overview_tab, professional_tab, interview_tab, security_tab = st.tabs(
        ["Overview", "Professional profile", "Interview profile", "Security"]
    )
    with overview_tab:
        _render_overview(account, profile, completion, recommendations)
    with professional_tab:
        _render_professional_form(user_id, profile)
    with interview_tab:
        _render_interview_profile(user_id, preferences)
    with security_tab:
        _render_security(user_id)


def _render_overview(account, profile: dict, completion: dict, recommendations: list[dict]) -> None:
    with st.container(border=True):
        st.markdown(f"**Name:** {account.full_name}")
        st.markdown(f"**Email:** {account.email}")
        st.markdown(f"**Username:** {getattr(account, 'username', None) or 'not set'}")
        st.markdown(f"**Role:** {account.role.title()}")
        st.markdown(f"**Headline:** {profile['headline'] or 'not set'}")

    section("Profile completion", ":material/fact_check:")
    st.progress(completion["percent"] / 100)
    for label, done in completion["checks"].items():
        st.markdown(f"- {'Done' if done else 'To do'} · {label}")
    if completion["missing"]:
        st.caption("Complete the remaining sections for more tailored guidance.")

    section("Recommended next steps", ":material/lightbulb:")
    if not recommendations:
        empty_state("Your profile is ready.", "Keep it current as your goals change.")
    else:
        for recommendation in recommendations:
            with st.container(border=True):
                st.markdown(f"**{recommendation['title']}**")
                st.caption(recommendation["detail"])
                if st.button(
                    "Open",
                    key=f"recommendation_{recommendation['target_page']}_{recommendation['title']}",
                    icon=":material/arrow_forward:",
                    width="stretch",
                ):
                    go_to(recommendation["target_page"])
    _render_resume_snapshot()


def _render_professional_form(user_id: int, profile: dict) -> None:
    section("Professional details", ":material/badge:")
    st.caption("Separate list items with commas or new lines. Changes are private to your account.")

    _seed_widget("profile_full_name", profile["full_name"])
    _seed_widget("profile_phone", profile["phone_number"])
    _seed_widget("profile_headline", profile["headline"])
    _seed_widget("profile_objective", profile["objective"])
    _seed_widget("profile_notes", profile["additional_profile_info"])
    _seed_widget("profile_skills", ", ".join(profile["skills"]))
    _seed_widget("profile_technical_interests", ", ".join(profile["technical_interests"]))
    _seed_widget("profile_education", ", ".join(profile["education"]))
    _seed_widget("profile_work_experience", ", ".join(profile["work_experience"]))
    _seed_widget("profile_career_interests", ", ".join(profile["career_interests"]))

    with st.form("user_profile_details_form", border=True):
        col1, col2 = st.columns(2)
        with col1:
            full_name = st.text_input(
                "Full name", max_chars=200, key="profile_full_name"
            )
            phone = st.text_input("Phone number", max_chars=50, key="profile_phone")
            headline = st.text_input(
                "Professional headline",
                max_chars=200,
                key="profile_headline",
                placeholder="e.g. Backend engineer focused on distributed systems",
            )
            objective = st.text_area(
                "Career objective", height=100, max_chars=2000, key="profile_objective"
            )
        with col2:
            skills = st.text_area("Core skills", height=100, key="profile_skills")
            technical_interests = st.text_area(
                "Technical interests", height=100, key="profile_technical_interests"
            )
            education = st.text_area("Education", height=100, key="profile_education")
            work_experience = st.text_area(
                "Work experience", height=100, key="profile_work_experience"
            )
            career_interests = st.text_area(
                "Career interests", height=100, key="profile_career_interests"
            )
        details = st.text_area(
            "Additional profile information",
            height=100,
            max_chars=2000,
            key="profile_notes",
        )
        submitted = st.form_submit_button(
            "Save profile", type="primary", icon=":material/save:", width="stretch"
        )

    if st.button(
        "Discard unsaved changes",
        key="profile_discard_changes",
        icon=":material/restart_alt:",
        width="stretch",
    ):
        _discard_widgets(_PROFILE_FORM_KEYS)
        st.rerun()

    if submitted:
        try:
            update_user_profile(
                user_id,
                full_name=full_name,
                phone_number=phone,
                additional_profile_info=details,
                headline=headline,
                objective=objective,
                skills=skills,
                technical_interests=technical_interests,
                education=education,
                work_experience=work_experience,
                career_interests=career_interests,
                actor_user_id=user_id,
            )
        except (AuthError, ValueError) as exc:
            error_state("Profile was not saved.", str(exc))
        else:
            st.session_state.account = get_account(user_id)
            success_toast("Profile updated successfully.")
            st.rerun()


def _render_interview_profile(user_id: int, preferences: dict) -> None:
    section("Interview defaults", ":material/target:")
    st.caption("These values pre-fill interview practice unless you change them for a session.")

    _seed_widget("profile_target_role", preferences["default_role"])
    _seed_widget("profile_default_difficulty", preferences["default_difficulty"])
    _seed_widget("profile_default_type", preferences["default_interview_type"])
    _seed_widget("profile_voice_enabled", preferences["voice_enabled"])
    _seed_widget(
        "profile_preferred_categories", preferences.get("preferred_interview_categories", [])
    )

    with st.form("interview_profile_form", border=True):
        col1, col2 = st.columns(2)
        with col1:
            role = st.text_input(
                "Target role",
                max_chars=100,
                key="profile_target_role",
                placeholder="e.g. Machine learning engineer",
            )
            difficulty = st.selectbox(
                "Default difficulty", DIFFICULTIES, key="profile_default_difficulty"
            )
        with col2:
            interview_type = st.selectbox(
                "Default interview type", INTERVIEW_TYPES, key="profile_default_type"
            )
            voice_enabled = st.toggle("Enable voice answers", key="profile_voice_enabled")
        preferred_categories = st.multiselect(
            "Preferred interview categories",
            INTERVIEW_TYPES,
            key="profile_preferred_categories",
            help="These choices are offered first when starting a practice session.",
        )
        submitted = st.form_submit_button(
            "Save interview profile", type="primary", icon=":material/save:", width="stretch"
        )

    if st.button(
        "Discard interview changes",
        key="profile_discard_interview_changes",
        icon=":material/restart_alt:",
        width="stretch",
    ):
        _discard_widgets(_INTERVIEW_FORM_KEYS)
        st.rerun()

    if submitted:
        try:
            updated = update_user_preferences(
                user_id,
                default_role=role,
                default_difficulty=difficulty,
                default_interview_type=interview_type,
                voice_enabled=voice_enabled,
                preferred_interview_categories=preferred_categories,
                actor_user_id=user_id,
            )
        except (AuthError, ValueError) as exc:
            error_state("Interview profile was not saved.", str(exc))
        else:
            st.session_state.user_preferences = updated
            success_toast("Interview profile saved.")


def _render_security(user_id: int) -> None:
    section("Change password", ":material/key:")
    st.caption("Choose a new password at any time. Your current password is required.")
    render_password_change_form(
        user_id,
        form_id="profile_password_form",
        key_prefix="profile",
        submit_label="Update password",
    )

    section("Account security", ":material/shield:")
    st.caption(
        "Each account has an isolated workspace. Passwords are salted and hashed; "
        "they are never displayed or stored in plain text."
    )
    if st.button(
        "Manage notifications and activity",
        icon=":material/settings:",
        width="stretch",
        key="profile_open_account_settings",
    ):
        go_to("account_settings")


def _render_resume_snapshot() -> None:
    section("Resume snapshot", ":material/description:")
    workspace_id = _workspace_id()
    if not workspace_id:
        empty_state("No workspace found.")
        return
    with get_db_context() as db:
        resume = (
            db.query(ResumeAnalysis)
            .filter(ResumeAnalysis.session_id == workspace_id)
            .order_by(ResumeAnalysis.created_at.desc(), ResumeAnalysis.id.desc())
            .first()
        )
        snapshot = None
        if resume:
            snapshot = {
                "File": resume.file_name,
                "Score": (
                    f"{resume.overall_resume_score:.0f}/100"
                    if resume.overall_resume_score is not None
                    else "N/A"
                ),
                "Education": ", ".join(
                    str(item.get("degree", "")) for item in (resume.education or []) if item
                )
                or "Not extracted",
                "Skills": ", ".join(list(resume.skills or [])[:12]) or "Not extracted",
                "Experience": ", ".join(
                    str(item.get("title", "")) for item in (resume.experience or []) if item
                )
                or "Not extracted",
            }
    if snapshot:
        for label, value in snapshot.items():
            st.markdown(f"**{label}:** {value}")
    else:
        empty_state("No resume analysed yet.", "Analyze a resume to populate this section.")
