from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings_db(tmp_path):
    import database.db as db_module
    from config.settings import settings
    from database import User, UserSession
    from services import auth_service as auth

    original_database = settings.database_path
    original_vectors = settings.vector_store_path
    settings.database_path = str(tmp_path / "settings.db")
    settings.vector_store_path = str(tmp_path / "vectors")
    db_module.engine = None
    db_module.SessionLocal = None
    db_module.init_database()

    with db_module.get_db_context() as db:
        admin = User(
            email="admin@example.com",
            username="admin",
            full_name="Admin",
            password_hash=auth.hash_password("Pass1234"),
            role="admin",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        user_one = User(
            email="one@example.com",
            username="one",
            full_name="User One",
            password_hash=auth.hash_password("Pass1234"),
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        user_two = User(
            email="two@example.com",
            username="two",
            full_name="User Two",
            password_hash=auth.hash_password("Pass1234"),
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        db.add_all([admin, user_one, user_two])
        db.flush()
        workspace_one = UserSession(session_id="settings-1", user_id=user_one.id, label="one")
        db.add(workspace_one)
        db.flush()
        ids = {
            "admin": int(admin.id),
            "user1": int(user_one.id),
            "user2": int(user_two.id),
            "session1": int(workspace_one.id),
        }

    yield ids

    db_module.close_database()
    db_module.engine = None
    db_module.SessionLocal = None
    settings.database_path = original_database
    settings.vector_store_path = original_vectors


def test_profile_tables_and_preference_columns_exist(settings_db):
    from database.db import engine
    from sqlalchemy import inspect

    inspector = inspect(engine)
    assert "user_profiles" in set(inspector.get_table_names())
    settings_columns = {column["name"] for column in inspector.get_columns("user_settings")}
    assert {
        "preferred_interview_categories",
        "learning_preferences",
        "notification_preferences",
    }.issubset(settings_columns)


def test_profile_update_validation_completion_and_owner_scope(settings_db):
    from services.auth_service import AuthError
    from services.profile_service import (
        get_profile_completion,
        get_user_profile,
        update_user_profile,
    )

    user_id = settings_db["user1"]
    other_id = settings_db["user2"]
    empty = get_user_profile(user_id, user_id)
    assert empty["completion"]["percent"] < 100

    updated = update_user_profile(
        user_id,
        full_name="User One Updated",
        phone_number="555-0100",
        additional_profile_info="Focused on backend systems",
        headline="Backend engineer",
        objective="Grow into a staff engineer role",
        skills="Python, SQL, FastAPI",
        technical_interests="Distributed systems, databases",
        education="BSc Computer Science",
        work_experience="Software engineer",
        career_interests="Staff engineer",
        actor_user_id=user_id,
    )
    assert updated["full_name"] == "User One Updated"
    assert updated["skills"] == ["Python", "SQL", "FastAPI"]
    assert updated["completion"]["percent"] == 100
    assert get_profile_completion(user_id, user_id)["percent"] == 100

    assert get_user_profile(user_id, user_id)["technical_interests"] == [
        "Distributed systems",
        "databases",
    ]
    with pytest.raises(AuthError):
        get_user_profile(user_id, other_id)
    with pytest.raises(AuthError):
        update_user_profile(user_id, headline="Stolen", actor_user_id=other_id)
    with pytest.raises(AuthError):
        update_user_profile(user_id, full_name="", actor_user_id=user_id)


def test_profile_recommendations_use_profile_and_preferences(settings_db):
    from services.auth_service import AuthError
    from services.preferences_service import update_user_preferences
    from services.profile_service import get_profile_recommendations

    user_id = settings_db["user1"]
    update_user_preferences(
        user_id,
        preferred_interview_categories=["Technical"],
        learning_preferences={"focus_skills": "Python"},
        actor_user_id=user_id,
    )
    recommendations = get_profile_recommendations(user_id, user_id)
    titles = [item["title"] for item in recommendations]
    assert "Add your core skills" in titles
    assert "Practice Technical interviews" in titles
    assert "Study Python" in titles
    with pytest.raises(AuthError):
        get_profile_recommendations(user_id, settings_db["user2"])


def test_preference_partial_update_preserves_existing_values(settings_db):
    from services.auth_service import AuthError
    from services.preferences_service import get_user_preferences, update_user_preferences

    user_id = settings_db["user1"]
    update_user_preferences(
        user_id,
        default_role="ML Engineer",
        default_difficulty="hard",
        llm_provider="groq",
        llm_model="llama-3",
        preferred_interview_categories=["Technical"],
        learning_preferences={"focus_skills": "Python", "weekly_goal": 8},
        notification_preferences={"achievements": False},
        actor_user_id=user_id,
    )
    update_user_preferences(user_id, voice_enabled=False, actor_user_id=user_id)
    preferences = get_user_preferences(user_id, user_id)

    assert preferences["default_role"] == "ML Engineer"
    assert preferences["default_difficulty"] == "hard"
    assert preferences["llm_provider"] == "groq"
    assert preferences["llm_model"] == "llama-3"
    assert preferences["voice_enabled"] is False
    assert preferences["preferred_interview_categories"] == ["Technical"]
    assert preferences["learning_preferences"]["weekly_goal"] == 8
    assert preferences["notification_preferences"]["achievements"] is False
    with pytest.raises(AuthError):
        update_user_preferences(user_id, default_role="Intruder", actor_user_id=settings_db["user2"])


def test_notification_preferences_are_enforced(settings_db):
    from database import Notification, get_db_context
    from services.learning_service import create_notification
    from services.preferences_service import update_user_preferences

    user_id = settings_db["user1"]
    update_user_preferences(
        user_id,
        notification_preferences={"achievements": False},
        actor_user_id=user_id,
    )
    suppressed = create_notification(
        session_id=settings_db["session1"],
        actor_user_id=user_id,
        title="Suppressed achievement",
        message="This should be suppressed.",
        notification_type="achievement",
        event_key="step6-suppressed",
    )
    assert suppressed is False

    update_user_preferences(
        user_id,
        notification_preferences={"achievements": True},
        actor_user_id=user_id,
    )
    delivered = create_notification(
        session_id=settings_db["session1"],
        actor_user_id=user_id,
        title="Enabled achievement",
        message="This should be delivered.",
        notification_type="achievement",
        event_key="step6-enabled",
    )
    assert delivered is True
    with get_db_context() as db:
        events = db.query(Notification).filter(Notification.event_key.like("step6-%")).all()
    assert [event.event_key for event in events] == ["step6-enabled"]


def test_account_activity_is_owner_scoped_and_credential_safe(settings_db):
    from services.auth_service import AuthError, change_password, get_account_activity

    user_id = settings_db["user1"]
    change_password(user_id, "Pass1234", "Changed123", actor_user_id=user_id)
    activity = get_account_activity(user_id, user_id)
    assert any(item["label"] == "Password changed" for item in activity)
    assert all("Pass1234" not in item["message"] for item in activity)
    assert all("Changed123" not in item["message"] for item in activity)
    with pytest.raises(AuthError):
        get_account_activity(user_id, settings_db["user2"])
    with pytest.raises(AuthError):
        change_password(
            user_id, "Changed123", "Another123", actor_user_id=settings_db["user2"]
        )


def test_interview_defaults_use_preferred_categories():
    from frontend.user.interview_page import _preferred_interview_type

    assert _preferred_interview_type(["Technical"]) == "Technical Interview"
    assert _preferred_interview_type(["Full Interview"]) == "Full Interview"
    assert _preferred_interview_type(["Unknown"]) is None


def test_account_pages_render_and_are_registered(settings_db, monkeypatch):
    import ast

    from services import knowledge_base, llm_service
    from services.auth_service import get_account
    from streamlit.testing.v1 import AppTest

    tree = ast.parse((PROJECT_ROOT / "app.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_user_pages"
    )
    page_paths = [
        str(node.args[0].value)
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Page"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    assert "frontend/user/profile.py" in page_paths
    assert "frontend/user/account_settings.py" in page_paths

    monkeypatch.setattr(
        llm_service, "get_provider_status", lambda: {"providers": {}, "active": "none"}
    )

    class _Stats:
        def stats(self):
            return {"documents": 0, "chunks": 0}

    monkeypatch.setattr(knowledge_base, "get_knowledge_base", lambda *args, **kwargs: _Stats())
    account = get_account(settings_db["user1"])
    assert account is not None

    profile_app = AppTest.from_file(
        str(PROJECT_ROOT / "frontend/user/profile.py"), default_timeout=30
    )
    profile_app.session_state["account"] = account
    profile_app.session_state["user_id"] = settings_db["user1"]
    profile_app.session_state["user_session_id"] = "settings-1"
    profile_app.run()
    assert profile_app.exception == []
    assert any("My profile" in title.value for title in profile_app.title)

    settings_app = AppTest.from_file(
        str(PROJECT_ROOT / "frontend/user/account_settings.py"), default_timeout=30
    )
    settings_app.session_state["account"] = account
    settings_app.session_state["user_id"] = settings_db["user1"]
    settings_app.session_state["user_session_id"] = "settings-1"
    settings_app.run()
    assert settings_app.exception == []
    assert any("Account settings" in title.value for title in settings_app.title)


def test_credential_cleanup_includes_account_password_widgets():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
import streamlit as st
from frontend.shared.auth_ui import clear_credential_state

if "guard" not in st.session_state:
    st.session_state.guard = True
    st.session_state.account_current_password = "secret"
    st.session_state.account_new_password = "secret"
    st.session_state.account_confirm_password = "secret"
    st.session_state.profile_current_password = "secret"
    clear_credential_state()
st.write("done")
"""
    )
    app.run()
    assert app.exception == []
    for key in (
        "account_current_password",
        "account_new_password",
        "account_confirm_password",
        "profile_current_password",
    ):
        assert key not in app.session_state
