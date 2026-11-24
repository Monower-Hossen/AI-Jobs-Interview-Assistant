from __future__ import annotations

import html

import streamlit as st
from services.auth_service import AuthError, get_account, owned_session_id
from services.career_service import load_latest_resume
from services.preferences_service import get_user_preferences, update_user_preferences
from services.profile_service import (
    get_profile_recommendations,
    get_user_profile,
    update_user_profile,
)

from frontend.shared.components import error_state, kpi_row, page_header, section, success_toast
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
_TABS_KEY = "profile_hub_active_section"
_TAB_OVERVIEW = ":material/grid_view: Overview"
_TAB_PROFESSIONAL = ":material/badge: Professional profile"
_TAB_INTERVIEW = ":material/target: Interview profile"
_TAB_LABELS = (_TAB_OVERVIEW, _TAB_PROFESSIONAL, _TAB_INTERVIEW)
_SECTION_DETAILS = {
    "Profile basics": "Your name, professional headline, and contact number.",
    "Career focus": "Career objective and the roles you want to target.",
    "Skills and interests": "Core skills and the technical areas you want to focus on.",
    "Background": "Education and work experience history.",
    "Profile notes": "Extra context that shapes your interview guidance.",
}
_SECTION_SLUGS = {
    "Profile basics": "basics",
    "Career focus": "focus",
    "Skills and interests": "skills",
    "Background": "background",
    "Profile notes": "notes",
}

_PROFILE_CSS = """
<style>
.profile-hub-hero {
  background: linear-gradient(135deg, rgba(79, 70, 229, 0.18) 0%, rgba(59, 130, 246, 0.07) 100%);
  border: 1px solid rgba(99, 102, 241, 0.28);
  border-radius: 18px;
  padding: 1.25rem 1.4rem;
  box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
}
.profile-hub-hero-top {
  display: flex;
  align-items: center;
  gap: 1rem;
}
.profile-hub-avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 64px;
  height: 64px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: linear-gradient(135deg, #4f46e5, #6366f1);
  color: #ffffff;
  font-size: 1.4rem;
  font-weight: 700;
  letter-spacing: 0.02em;
}
.profile-hub-name {
  font-size: 1.35rem;
  font-weight: 700;
  line-height: 1.2;
  color: inherit;
}
.profile-hub-meta {
  margin-top: 0.2rem;
  font-size: 0.875rem;
  opacity: 0.78;
}
.profile-hub-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-top: 0.5rem;
}
.profile-hub-badge {
  display: inline-block;
  padding: 0.15rem 0.6rem;
  border-radius: 999px;
  background: rgba(99, 102, 241, 0.18);
  color: inherit;
  font-size: 0.75rem;
  font-weight: 600;
}
.profile-hub-headline {
  margin-top: 0.85rem;
  font-size: 0.95rem;
  opacity: 0.9;
}
@media (max-width: 640px) {
  .profile-hub-hero { padding: 1rem; }
  .profile-hub-avatar { width: 52px; height: 52px; font-size: 1.1rem; }
  .profile-hub-name { font-size: 1.15rem; }
}
</style>
"""


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


def _request_tab(label: str) -> None:
    st.session_state["profile_requested_section"] = label
    st.rerun()


def _initials(account) -> str:
    full_name = str(getattr(account, "full_name", "") or "").strip()
    parts = [part for part in full_name.replace("_", " ").split() if part]
    if parts:
        return "".join(part[0] for part in parts[:2]).upper()
    fallback = str(getattr(account, "username", "") or getattr(account, "email", "") or "").strip()
    return fallback[:1].upper() if fallback else "?"


def _render_hero(account, profile: dict, completion: dict) -> None:
    name = html.escape(str(getattr(account, "full_name", "") or "Your name"))
    username = html.escape(str(getattr(account, "username", "") or ""))
    email = html.escape(str(getattr(account, "email", "") or ""))
    role = html.escape(str(getattr(account, "role", "user") or "user").title())
    headline = html.escape(profile["headline"] or "Add your professional headline")
    handle = f"@{username} · {email}" if username else email
    initials = html.escape(_initials(account))

    detail_col, action_col = st.columns([5, 1], vertical_alignment="center")
    with detail_col:
        st.html(f"""
            <div class="profile-hub-hero">
              <div class="profile-hub-hero-top">
                <div class="profile-hub-avatar">{initials}</div>
                <div>
                  <div class="profile-hub-name">{name}</div>
                  <div class="profile-hub-meta">{handle}</div>
                  <div class="profile-hub-badges">
                    <span class="profile-hub-badge">{role}</span>
                    <span class="profile-hub-badge">{completion['percent']}% complete</span>
                  </div>
                </div>
              </div>
              <div class="profile-hub-headline">{headline}</div>
            </div>
            """)
        st.progress(
            completion["percent"] / 100,
            text=f"Profile completion · {completion['percent']}%",
        )
    with action_col:
        if st.button(
            "Edit profile",
            key="profile_hero_edit",
            icon=":material/edit:",
            width="stretch",
        ):
            _request_tab(_TAB_PROFESSIONAL)


def _render_completion_tracker(completion: dict) -> None:
    section("Profile completion", ":material/fact_check:")
    with st.container(border=True):
        st.progress(
            completion["percent"] / 100,
            text=f"{completion['completed']} of {completion['total']} sections complete "
            f"· {completion['percent']}%",
        )
        for label, done in completion["checks"].items():
            status_col, detail_col, action_col = st.columns(
                [1, 5, 1], vertical_alignment="center", gap="small"
            )
            status_col.markdown(
                ":material/check_circle:" if done else ":material/radio_button_unchecked:"
            )
            done_badge = ":green-badge[Done]" if done else ":orange-badge[To do]"
            detail_col.markdown(f"**{label}**  {done_badge}")
            detail_col.caption(_SECTION_DETAILS.get(label, ""))
            slug = _SECTION_SLUGS.get(label, "section")
            if action_col.button(
                "Edit",
                key=f"profile_section_edit_{slug}",
                icon=":material/edit:",
                width="stretch",
            ):
                _request_tab(_TAB_PROFESSIONAL)


def _recommendation_cards(
    profile: dict, preferences: dict, completion: dict, recommendations: list[dict]
) -> list[dict]:
    cards: list[dict] = []
    if not profile["skills"]:
        cards.append(
            {
                "id": "skills",
                "icon": ":material/psychology:",
                "title": "Add your core skills",
                "detail": "List the skills you want interview questions to probe.",
                "action": "Add skills",
                "tab": _TAB_PROFESSIONAL,
            }
        )
    if not profile["technical_interests"]:
        cards.append(
            {
                "id": "interests",
                "icon": ":material/explore:",
                "title": "Choose technical interests",
                "detail": "Add focus areas so practice and learning stay aligned with your goals.",
                "action": "Choose interests",
                "tab": _TAB_PROFESSIONAL,
            }
        )
    categories = list(preferences.get("preferred_interview_categories") or [])
    if not categories:
        cards.append(
            {
                "id": "types",
                "icon": ":material/event:",
                "title": "Set preferred interview types",
                "detail": "Choose the interview formats you want to see first.",
                "action": "Set preferences",
                "tab": _TAB_INTERVIEW,
            }
        )
    else:
        practice = next(
            (item for item in recommendations if item.get("title", "").startswith("Practice ")),
            None,
        )
        if practice:
            cards.append(
                {
                    "id": "practice",
                    "icon": ":material/record_voice_over:",
                    "title": practice["title"],
                    "detail": practice["detail"],
                    "action": "Start practice",
                    "page": "interview",
                }
            )
    if completion["percent"] < 100:
        cards.append(
            {
                "id": "finish",
                "icon": ":material/auto_awesome:",
                "title": "Finish your profile",
                "detail": "Complete the remaining profile sections for more tailored guidance.",
                "action": "Complete profile",
                "tab": _TAB_PROFESSIONAL,
            }
        )
    else:
        study = next(
            (item for item in recommendations if item.get("title", "").startswith("Study ")),
            None,
        )
        if study:
            cards.append(
                {
                    "id": "study",
                    "icon": ":material/school:",
                    "title": study["title"],
                    "detail": study["detail"],
                    "action": "Open learning plan",
                    "page": "learning_roadmap",
                }
            )
    return cards[:4]


def _render_recommendations(
    profile: dict, preferences: dict, completion: dict, recommendations: list[dict]
) -> None:
    cards = _recommendation_cards(profile, preferences, completion, recommendations)
    if not cards:
        with st.container(border=True):
            st.markdown(":material/check_circle: **Your profile is ready.**")
            st.caption("Keep it current as your goals change.")
        return

    columns = st.columns(2, gap="medium")
    for index, card in enumerate(cards):
        with columns[index % 2], st.container(border=True):
            st.markdown(f"**{card['icon']} {card['title']}**")
            st.caption(card["detail"])
            if st.button(
                card["action"],
                key=f"profile_recommendation_{card['id']}",
                icon=":material/arrow_forward:",
                width="stretch",
            ):
                if card.get("tab"):
                    _request_tab(card["tab"])
                else:
                    go_to(card["page"])


def _resume_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _render_resume_snapshot(resume) -> None:
    section("Resume snapshot", ":material/description:")
    if resume is None:
        with st.container(border=True):
            st.markdown(":material/upload_file: **No resume analysed yet**")
            st.caption("Upload and analyze a resume to see your score, skills, and education here.")
            if st.button(
                "Upload resume",
                key="profile_resume_upload",
                icon=":material/upload:",
                type="primary",
                width="stretch",
            ):
                go_to("resume")
        return

    analysis = resume.analysis
    score = max(0.0, min(float(resume.scores.overall or 0), 100.0))
    education = [
        text
        for text in (
            " · ".join(
                part
                for part in (
                    _resume_text(getattr(item, "degree", "")),
                    _resume_text(getattr(item, "institution", "")),
                )
                if part
            )
            for item in analysis.education
        )
        if text
    ]
    experience = [
        text
        for text in (
            " · ".join(
                part
                for part in (
                    _resume_text(getattr(item, "title", "")),
                    _resume_text(getattr(item, "company", "")),
                )
                if part
            )
            for item in analysis.experience
        )
        if text
    ]
    skills = [skill for skill in (_resume_text(item) for item in analysis.skills) if skill]

    with st.container(border=True):
        file_col, score_col = st.columns([3, 2], vertical_alignment="center")
        with file_col:
            st.markdown(f":material/picture_as_pdf: **{resume.file_name}**")
            saved = resume.created_at.strftime("%d %b %Y") if resume.created_at else "Saved record"
            st.caption(f"Latest analysis · {saved}")
        with score_col:
            st.progress(score / 100, text=f"Resume score · {score:.0f}/100")

        st.markdown("**Education**")
        st.caption(" · ".join(education) if education else "Not extracted")
        st.markdown("**Experience**")
        st.caption(" · ".join(experience) if experience else "Not extracted")
        st.markdown("**Skills**")
        if skills:
            with st.container(horizontal=True, wrap=True, gap="small"):
                for skill in skills[:16]:
                    st.badge(skill, color="blue")
            if len(skills) > 16:
                st.caption(f"+{len(skills) - 16} more skills")
        else:
            st.caption("Not extracted")

        if st.button(
            "Open resume analyzer",
            key="profile_resume_open",
            icon=":material/open_in_new:",
            width="stretch",
        ):
            go_to("resume")


def _render_overview(
    account,
    profile: dict,
    preferences: dict,
    completion: dict,
    recommendations: list[dict],
    resume,
) -> None:
    _render_completion_tracker(completion)
    section("Recommended next steps", ":material/lightbulb:")
    _render_recommendations(profile, preferences, completion, recommendations)
    _render_resume_snapshot(resume)


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
            full_name = st.text_input("Full name", max_chars=200, key="profile_full_name")
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


def show_user_profile() -> None:
    account = st.session_state.get("account")
    if not account:
        st.error("No account found. Please sign in.", icon=":material/error:")
        return

    page_header(
        "My profile",
        "Your career hub for profile progress and interview preferences",
        ":material/person:",
    )
    st.html(_PROFILE_CSS)
    user_id = int(account.id)
    try:
        profile = get_user_profile(user_id, user_id)
        preferences = get_user_preferences(user_id, user_id)
        recommendations = get_profile_recommendations(user_id, user_id)
    except (AuthError, ValueError) as exc:
        error_state("Profile could not be loaded.", str(exc))
        return

    workspace_id = _workspace_id()
    resume = None
    try:
        if workspace_id:
            resume = load_latest_resume(workspace_id, user_id)
    except (AuthError, ValueError) as exc:
        error_state("Resume snapshot could not be loaded.", str(exc))

    completion = profile["completion"]
    requested_tab = st.session_state.pop("profile_requested_section", None)
    if requested_tab in _TAB_LABELS:
        st.session_state[_TABS_KEY] = requested_tab
    if st.session_state.get(_TABS_KEY) not in _TAB_LABELS:
        st.session_state[_TABS_KEY] = _TAB_OVERVIEW

    _render_hero(account, profile, completion)
    score = f"{resume.scores.overall:.0f}/100" if resume is not None else "—"
    kpi_row(
        [
            ("Profile completion", f"{completion['percent']}%", None, ":material/fact_check:"),
            (
                "Completed sections",
                f"{completion['completed']}/{completion['total']}",
                None,
                ":material/checklist:",
            ),
            ("Resume score", score, None, ":material/score:"),
            ("Account role", str(account.role or "user").title(), None, ":material/shield:"),
        ]
    )

    overview_tab, professional_tab, interview_tab = st.tabs(
        list(_TAB_LABELS), key=_TABS_KEY, on_change="rerun"
    )
    with overview_tab:
        _render_overview(account, profile, preferences, completion, recommendations, resume)
    with professional_tab:
        _render_professional_form(user_id, profile)
    with interview_tab:
        _render_interview_profile(user_id, preferences)
