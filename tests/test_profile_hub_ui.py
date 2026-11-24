from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _account():
    return SimpleNamespace(
        id=7,
        full_name="Tamim Ahmed",
        email="tamim@gmail.com",
        username="tamim11",
        role="user",
    )


def _profile(completed=1, skills=None, interests=None):
    checks = {
        "Profile basics": completed >= 1,
        "Career focus": completed >= 2,
        "Skills and interests": completed >= 3,
        "Background": completed >= 4,
        "Profile notes": completed >= 5,
    }
    return {
        "full_name": "Tamim Ahmed",
        "email": "tamim@gmail.com",
        "phone_number": "555-0100" if completed >= 1 else "",
        "additional_profile_info": "Career notes" if completed >= 5 else "",
        "headline": "Backend engineer",
        "objective": "Grow into a staff engineer" if completed >= 2 else "",
        "skills": skills or [],
        "technical_interests": interests or [],
        "education": ["BSc Computer Science"] if completed >= 4 else [],
        "work_experience": ["Software engineer"] if completed >= 4 else [],
        "career_interests": ["Staff engineer"] if completed >= 2 else [],
        "completion": {
            "percent": round(100 * completed / 5),
            "completed": completed,
            "total": 5,
            "checks": checks,
            "missing": [name for name, done in checks.items() if not done],
        },
    }


def _preferences(categories=None):
    return {
        "default_role": "",
        "default_difficulty": "medium",
        "default_interview_type": "Technical",
        "voice_enabled": False,
        "preferred_interview_categories": categories or [],
        "learning_preferences": {},
    }


def _recommendations():
    return [
        {
            "title": "Add your core skills",
            "detail": "List the skills you want interview questions to probe.",
            "target_page": "profile",
        },
        {
            "title": "Choose technical interests",
            "detail": "Add focus areas so practice and learning stay aligned with your goals.",
            "target_page": "profile",
        },
        {
            "title": "Set preferred interview types",
            "detail": "Choose the interview formats you want to see first.",
            "target_page": "account_settings",
        },
        {
            "title": "Finish your profile",
            "detail": "Complete the remaining profile sections for more tailored guidance.",
            "target_page": "profile",
        },
    ]


def _resume():
    from services.career_service import ResumeDetails
    from services.resume_analyzer import ResumeAnalysisResult, ResumeScores

    return ResumeDetails(
        id=3,
        analysis=ResumeAnalysisResult(
            education=[
                {
                    "degree": "B.Sc. in Computer Science & Engineering",
                    "institution": "Example University",
                }
            ],
            experience=[],
            skills=["Python", "SQL", "REST APIs"],
        ),
        scores=ResumeScores(
            technical_skills=80,
            experience=70,
            projects=70,
            education=90,
            certifications=60,
            structure=80,
            overall=78,
        ),
        raw_text="",
        file_name="resume_snapshot.pdf",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


def _run_profile_app(monkeypatch, profile, preferences, resume, recommendations=None):
    import frontend.user.User_profile as hub

    monkeypatch.setattr(hub, "get_user_profile", lambda *a, **k: profile)
    monkeypatch.setattr(hub, "get_user_preferences", lambda *a, **k: preferences)
    monkeypatch.setattr(
        hub,
        "get_profile_recommendations",
        lambda *a, **k: recommendations if recommendations is not None else _recommendations(),
    )
    monkeypatch.setattr(hub, "owned_session_id", lambda *a, **k: 1)
    monkeypatch.setattr(hub, "load_latest_resume", lambda *a, **k: resume)

    app = AppTest.from_file(str(PROJECT_ROOT / "frontend/user/profile.py"), default_timeout=30)
    app.session_state["account"] = _account()
    app.session_state["user_id"] = 7
    app.session_state["user_session_id"] = "workspace-token"
    app.run()
    return app, hub


def test_profile_hub_uses_authenticated_data_and_saved_resume(monkeypatch):
    app, _ = _run_profile_app(monkeypatch, _profile(), _preferences(), _resume())
    assert app.exception == [], [str(error.value) for error in app.exception]
    assert any("My profile" in title.value for title in app.title)

    values = [metric.value for metric in app.metric]
    assert "20%" in values
    assert "1/5" in values
    assert "78/100" in values
    assert "User" in values

    html_bodies = " ".join(str(getattr(node, "value", "")) for node in app.main)
    assert "Tamim Ahmed" in html_bodies
    assert "@tamim11" in html_bodies
    assert "tamim@gmail.com" in html_bodies
    assert "Backend engineer" in html_bodies
    assert "resume_snapshot.pdf" in html_bodies

    captions = [caption.value for caption in app.caption]
    assert "B.Sc. in Computer Science & Engineering · Example University" in captions
    assert "Not extracted" in captions


def test_profile_hub_empty_resume_offers_upload(monkeypatch):
    app, _ = _run_profile_app(monkeypatch, _profile(), _preferences(), None)
    assert app.exception == [], [str(error.value) for error in app.exception]
    assert app.get_by_key("profile_resume_upload") is not None


def test_profile_hub_exposes_only_three_sections(monkeypatch):
    app, hub = _run_profile_app(monkeypatch, _profile(), _preferences(), None)
    assert app.exception == [], [str(error.value) for error in app.exception]
    assert [tab.label for tab in app.tabs] == list(hub._TAB_LABELS)
    assert all("security" not in label.lower() for label in hub._TAB_LABELS)
    with pytest.raises(KeyError):
        app.get_by_key("profile_open_account_settings")
    with pytest.raises(KeyError):
        app.get_by_key("profile_current_password")
    text = " ".join(str(getattr(node, "value", "")) for node in app.main)
    assert "Account security" not in text


def test_profile_hub_resets_stale_security_tab(monkeypatch):
    app, hub = _run_profile_app(monkeypatch, _profile(), _preferences(), None)
    app.session_state[hub._TABS_KEY] = ":material/shield: Security"
    app.run()
    assert app.exception == [], [str(error.value) for error in app.exception]
    assert app.session_state[hub._TABS_KEY] == hub._TAB_OVERVIEW


def test_recommendation_action_switches_to_interview_tab(monkeypatch):
    app, hub = _run_profile_app(monkeypatch, _profile(), _preferences(), None)
    assert app.exception == [], [str(error.value) for error in app.exception]
    app.get_by_key("profile_recommendation_types").click().run()
    assert app.exception == [], [str(error.value) for error in app.exception]
    assert app.session_state[hub._TABS_KEY] == hub._TAB_INTERVIEW


def test_completed_recommendations_are_hidden_or_updated():
    import frontend.user.User_profile as hub

    profile = _profile(completed=5, skills=["Python"], interests=["APIs"])
    preferences = _preferences(categories=["Technical"])
    recommendations = [
        {
            "title": "Practice Technical interviews",
            "detail": "Your preferred interview type is ready to use in interview practice.",
            "target_page": "interview",
        },
        {
            "title": "Study Python",
            "detail": "Build a focused learning plan from your saved learning preferences.",
            "target_page": "learning_roadmap",
        },
    ]
    cards = hub._recommendation_cards(profile, preferences, profile["completion"], recommendations)
    assert [card["id"] for card in cards] == ["practice", "study"]


def test_forced_password_screen_renders_secure_form():
    app = AppTest.from_file(
        str(PROJECT_ROOT / "frontend/shared/change_password.py"), default_timeout=30
    )
    app.session_state["account"] = _account()
    app.session_state["user_id"] = 7
    app.run()
    assert app.exception == [], [str(error.value) for error in app.exception]
    assert any("Set new password" in title.value for title in app.title)
    assert app.get_by_key("first_login_current_password") is not None
    assert app.get_by_key("first_login_show_password") is not None


def test_password_strength_checks_follow_configured_minimum(monkeypatch):
    import frontend.shared.change_password as pw

    monkeypatch.setattr(pw.settings, "password_min_length", 8)
    ratio, label, checks = pw._password_strength("Strong123", 8)
    assert ratio == 1
    assert label == "Strong"
    assert [passed for _, passed in checks] == [True, True, True]
    assert pw._password_strength("abc", 8)[1] == "Weak"


def _password_app():
    app = AppTest.from_string("""
import streamlit as st
from frontend.shared.change_password import render_password_change_form
render_password_change_form(
    7, form_id="hub_test_form", key_prefix="profile", submit_label="Update password", forced=True
)
""")
    app.session_state["must_change_password"] = True
    app.run()
    assert app.exception == [], [str(error.value) for error in app.exception]
    return app


def test_password_form_rejects_mismatched_confirmation(monkeypatch):
    import frontend.shared.change_password as pw

    calls = []
    monkeypatch.setattr(pw, "change_password", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(pw, "get_account", lambda user_id: SimpleNamespace(id=user_id, role="user"))

    app = _password_app()
    app.get_by_key("profile_current_password").set_value("Old12345")
    app.get_by_key("profile_new_password").set_value("New12345")
    app.get_by_key("profile_confirm_password").set_value("Wrong12345")
    app.get_by_key("FormSubmitter:hub_test_form-Update password").click().run()
    assert calls == []
    assert any("Passwords do not match" in error.value for error in app.error)
    assert app.session_state["must_change_password"] is True


def test_password_form_uses_secure_service_and_clears_credentials(monkeypatch):
    import frontend.shared.change_password as pw

    calls = []
    monkeypatch.setattr(pw, "change_password", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(pw, "get_account", lambda user_id: SimpleNamespace(id=user_id, role="user"))

    app = _password_app()
    app.get_by_key("profile_current_password").set_value("Old12345")
    app.get_by_key("profile_new_password").set_value("New12345")
    app.get_by_key("profile_confirm_password").set_value("New12345")
    app.get_by_key("FormSubmitter:hub_test_form-Update password").click().run()
    assert app.exception == [], [str(error.value) for error in app.exception]
    assert calls == [((7, "Old12345", "New12345"), {"actor_user_id": 7})]
    assert app.session_state["must_change_password"] is False
    assert app.session_state["profile_current_password"] == ""
    assert app.session_state["profile_new_password"] == ""
    assert app.session_state["profile_confirm_password"] == ""
