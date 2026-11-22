from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def learning_db(tmp_path):
    import database.db as db_module
    from config.settings import settings
    from database import User, UserSession
    from services import auth_service as auth

    original_database = settings.database_path
    original_vectors = settings.vector_store_path
    settings.database_path = str(tmp_path / "learning.db")
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
        session_one = UserSession(session_id="learn-1", user_id=user_one.id, label="one")
        session_two = UserSession(session_id="learn-2", user_id=user_two.id, label="two")
        db.add_all([session_one, session_two])
        db.flush()
        ids = {
            "admin": int(admin.id),
            "user1": int(user_one.id),
            "user2": int(user_two.id),
            "session1": int(session_one.id),
            "session2": int(session_two.id),
        }

    yield ids

    db_module.close_database()
    db_module.SessionLocal = None
    settings.database_path = original_database
    settings.vector_store_path = original_vectors


def _resource_payload(title: str = "Python fundamentals") -> dict:
    return {
        "title": title,
        "description": "A focused refresher.",
        "category": "technical",
        "difficulty": "beginner",
        "estimated_minutes": 20,
        "skill_tags": "Python, debugging",
        "content": "Practice functions and exceptions.",
        "url": "https://example.com/learn",
    }


def _seed_roadmap(learning_db, *, created_at=None) -> tuple[int, int]:
    from database import LearningRoadmap, LearningTask, get_db_context

    with get_db_context() as db:
        roadmap = LearningRoadmap(
            session_id=learning_db["session1"],
            title="Focus plan",
            focus_skills=["Python", "debugging"],
            created_at=created_at or datetime.now(UTC).replace(tzinfo=None),
        )
        db.add(roadmap)
        db.flush()
        first = LearningTask(
            roadmap_id=roadmap.id,
            order_index=0,
            title="Review functions",
            status="pending",
        )
        second = LearningTask(
            roadmap_id=roadmap.id,
            order_index=1,
            title="Practice debugging",
            status="pending",
        )
        db.add_all([first, second])
        db.flush()
        return int(first.id), int(second.id)


def test_learning_notification_tables_exist(learning_db):
    from database.db import engine
    from sqlalchemy import inspect

    table_names = set(inspect(engine).get_table_names())
    assert {
        "learning_resources",
        "learning_resource_progress",
        "learning_activities",
        "user_achievements",
        "notifications",
    }.issubset(table_names)


def test_admin_resource_management_and_broadcast(learning_db):
    from services.learning_service import (
        admin_broadcast_notification,
        admin_create_resource,
        admin_list_resources,
        admin_set_resource_active,
        admin_update_resource,
        list_notifications,
        list_resources_for_user,
    )

    admin_id = learning_db["admin"]
    resource_id = admin_create_resource(admin_id, _resource_payload())
    resources = list_resources_for_user(learning_db["session1"], learning_db["user1"])
    assert any(row["id"] == resource_id for row in resources)
    first_notifications = list_notifications(learning_db["session1"], learning_db["user1"])
    assert any(row["notification_type"] == "new_resource" for row in first_notifications)

    admin_update_resource(admin_id, resource_id, {"title": "Updated resource"})
    rows = admin_list_resources(admin_id, include_inactive=True)
    assert next(row for row in rows if row["id"] == resource_id)["title"] == "Updated resource"

    recipients = admin_broadcast_notification(
        admin_id,
        title="Study reminder",
        message="Review your plan today.",
        notification_type="reminder",
    )
    assert recipients == 2
    admin_set_resource_active(admin_id, resource_id, is_active=False)
    active_ids = [
        row["id"] for row in list_resources_for_user(learning_db["session1"], learning_db["user1"])
    ]
    assert resource_id not in active_ids


def test_non_admin_learning_management_is_denied(learning_db):
    from services.auth_service import AuthError
    from services.learning_service import (
        admin_broadcast_notification,
        admin_create_resource,
        admin_learning_analytics,
        admin_list_resources,
    )

    user_id = learning_db["user1"]
    with pytest.raises(AuthError):
        admin_list_resources(user_id)
    with pytest.raises(AuthError):
        admin_create_resource(user_id, _resource_payload())
    with pytest.raises(AuthError):
        admin_broadcast_notification(user_id, title="Notice", message="Denied")
    with pytest.raises(AuthError):
        admin_learning_analytics(user_id)


def test_notifications_are_owner_scoped(learning_db):
    from services.auth_service import AuthError
    from services.learning_service import (
        admin_create_resource,
        list_notifications,
        mark_notification_read,
    )

    admin_create_resource(learning_db["admin"], _resource_payload())
    first = list_notifications(learning_db["session1"], learning_db["user1"])
    second = list_notifications(learning_db["session2"], learning_db["user2"])
    assert first and second
    assert not any(row["id"] == first[0]["id"] for row in second)

    with pytest.raises(AuthError):
        list_notifications(learning_db["session1"], learning_db["user2"])
    with pytest.raises(AuthError):
        mark_notification_read(int(first[0]["id"]), learning_db["session1"], learning_db["user2"])


def test_resource_progress_records_achievement_and_is_idempotent(learning_db):
    from services.learning_service import (
        admin_create_resource,
        get_learning_snapshot,
        list_notifications,
        list_resource_progress,
        record_resource_progress,
    )

    resource_id = admin_create_resource(learning_db["admin"], _resource_payload())
    result = record_resource_progress(
        resource_id=resource_id,
        session_id=learning_db["session1"],
        actor_user_id=learning_db["user1"],
        progress_percent=100,
        status="completed",
    )
    assert result["completed"] is True
    progress = list_resource_progress(learning_db["session1"], learning_db["user1"])
    assert progress[0]["status"] == "completed"
    notifications = list_notifications(learning_db["session1"], learning_db["user1"])
    assert any(row["notification_type"] == "achievement" for row in notifications)
    snapshot = get_learning_snapshot(learning_db["session1"], learning_db["user1"])
    assert snapshot["completed_resources"] == 1
    assert snapshot["points"] >= 15

    repeated = record_resource_progress(
        resource_id=resource_id,
        session_id=learning_db["session1"],
        actor_user_id=learning_db["user1"],
        progress_percent=100,
        status="completed",
    )
    assert repeated["completed"] is False


def test_task_completion_notifies_and_tracks_progress(learning_db):
    from services.learning_service import get_learning_snapshot, list_notifications
    from services.roadmap_service import set_task_status

    first_task, second_task = _seed_roadmap(learning_db)
    set_task_status(first_task, True, learning_db["session1"], learning_db["user1"])
    snapshot = get_learning_snapshot(learning_db["session1"], learning_db["user1"])
    assert snapshot["done_tasks"] == 1
    assert snapshot["points"] >= 10

    set_task_status(second_task, True, learning_db["session1"], learning_db["user1"])
    notifications = list_notifications(learning_db["session1"], learning_db["user1"])
    assert any(row["title"] == "Study plan complete" for row in notifications)


def test_task_reminders_are_created_once(learning_db):
    from services.learning_service import list_notifications, refresh_task_reminders

    _seed_roadmap(
        learning_db, created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=5)
    )
    created = refresh_task_reminders(learning_db["session1"], learning_db["user1"])
    assert created >= 1
    assert refresh_task_reminders(learning_db["session1"], learning_db["user1"]) == 0
    notifications = list_notifications(learning_db["session1"], learning_db["user1"])
    assert any(row["notification_type"] == "task_reminder" for row in notifications)


def test_learning_pages_are_wired(learning_db):
    import ast

    tree = ast.parse((PROJECT_ROOT / "app.py").read_text(encoding="utf-8"))
    paths: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Page"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            paths.append(str(node.args[0].value))
    assert "frontend/user/notifications.py" in paths
    assert "frontend/admin/learning.py" in paths

    navigation = ast.parse(
        (PROJECT_ROOT / "frontend/shared/navigation.py").read_text(encoding="utf-8")
    )
    pages_node = next(
        node
        for node in navigation.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "PAGES"
    )
    navigation_pages = ast.literal_eval(pages_node.value)
    assert "frontend/user/notifications.py" in navigation_pages.values()
    assert "frontend/admin/learning.py" in navigation_pages.values()

    for page_path, function_name in (
        ("frontend/user/notifications.py", "show_notifications"),
        ("frontend/admin/learning.py", "show_admin_learning"),
    ):
        source = (PROJECT_ROOT / page_path).read_text(encoding="utf-8")
        assert f"\n{function_name}()\n" in source


def test_notifications_page_renders_for_owner(learning_db):
    from services.auth_service import get_account
    from streamlit.testing.v1 import AppTest

    account = get_account(learning_db["user1"])
    assert account is not None
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30)
    app.session_state["db_initialized"] = True
    app.session_state["user_id"] = learning_db["user1"]
    app.session_state["user_session_id"] = "learn-1"
    app.session_state["account"] = account
    app.session_state["must_change_password"] = False
    app.run()
    app.switch_page("frontend/user/notifications.py").run()
    assert not app.exception, [str(error.value) for error in app.exception]


def test_admin_learning_page_denies_non_admin(learning_db):
    from services.auth_service import get_account
    from streamlit.testing.v1 import AppTest

    account = get_account(learning_db["user1"])
    assert account is not None
    app = AppTest.from_file(str(PROJECT_ROOT / "frontend/admin/learning.py"), default_timeout=20)
    app.session_state["db_initialized"] = True
    app.session_state["user_id"] = learning_db["user1"]
    app.session_state["user_session_id"] = "learn-1"
    app.session_state["account"] = account
    app.run()
    assert any("Administrator access required" in error.value for error in app.error)


def test_admin_learning_page_renders_for_admin(learning_db):
    from services.auth_service import get_account
    from streamlit.testing.v1 import AppTest

    account = get_account(learning_db["admin"])
    assert account is not None
    app = AppTest.from_file(str(PROJECT_ROOT / "frontend/admin/learning.py"), default_timeout=20)
    app.session_state["db_initialized"] = True
    app.session_state["user_id"] = learning_db["admin"]
    app.session_state["user_session_id"] = "admin-workspace"
    app.session_state["account"] = account
    app.run()
    assert not app.exception, [str(error.value) for error in app.exception]
    assert not any("Administrator access required" in error.value for error in app.error)
