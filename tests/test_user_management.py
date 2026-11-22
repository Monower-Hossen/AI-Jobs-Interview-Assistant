"""Account management workflows: admin creation, login, status, and access control."""

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def auth_db(tmp_path):
    import database.db as db_module
    import services.auth_service as auth
    from config.settings import settings
    from database import User

    original_path = settings.database_path
    original_vector_path = settings.vector_store_path
    settings.database_path = str(tmp_path / "auth.db")
    settings.vector_store_path = str(tmp_path / "vectors")
    db_module.engine = None
    db_module.SessionLocal = None
    with auth._login_lock:
        auth._failed_logins.clear()

    db_module.init_database()
    with db_module.get_db_context() as db:
        admin = User(
            email="admin@example.com",
            username="admin",
            full_name="Platform Admin",
            password_hash=auth.hash_password("Admin1234"),
            role="admin",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        db.add(admin)
        db.flush()
        admin_id = admin.id

    yield {"admin_id": admin_id, "db_module": db_module}

    with auth._login_lock:
        auth._failed_logins.clear()
    db_module.close_database()
    db_module.SessionLocal = None
    settings.database_path = original_path
    settings.vector_store_path = original_vector_path


def _create_user(admin_id, **overrides):
    from services.auth_service import admin_create_user

    values = {
        "full_name": "Jane Doe",
        "email": "jane@example.com",
        "username": "jane.doe",
        "password": "TempPass123",
        "confirm_password": "TempPass123",
    }
    values.update(overrides)
    return admin_create_user(actor_admin_id=admin_id, **values)


class TestAdminUserManagementWorkflows:
    def test_01_admin_logs_in(self, auth_db):
        from services.auth_service import authenticate, list_users

        account = authenticate("admin@example.com", "Admin1234")
        assert account is not None
        assert account.is_admin
        assert account.account_status == "active"
        assert account.last_login is not None
        assert [user.email for user in list_users(actor_admin_id=auth_db["admin_id"])] == ["admin@example.com"]

    def test_02_admin_opens_user_management(self, auth_db):
        from services.auth_service import get_user_statistics, require_admin

        require_admin(auth_db["admin_id"])
        stats = get_user_statistics(actor_admin_id=auth_db["admin_id"])
        assert stats["total"] == 1
        assert stats["active"] == 1
        assert stats["inactive"] == 0
        assert stats["suspended"] == 0

    def test_03_admin_creates_user_with_hashed_password(self, auth_db):
        from database import User, get_db_context
        from services.auth_service import verify_password

        account = _create_user(auth_db["admin_id"])
        assert account.role == "user"
        assert account.account_status == "active"
        assert account.must_change_password is True

        with get_db_context() as db:
            row = db.query(User).filter(User.email == "jane@example.com").one()
            assert row.password_hash != "TempPass123"
            assert "TempPass123" not in row.password_hash
            assert verify_password("TempPass123", row.password_hash)
            assert row.created_by_admin_id == auth_db["admin_id"]

    def test_04_duplicate_email_rejected(self, auth_db):
        from services.auth_service import AuthError, list_users

        _create_user(auth_db["admin_id"])
        with pytest.raises(AuthError, match="email"):
            _create_user(auth_db["admin_id"], username="other.user", email="JANE@example.com")
        assert len(list_users(actor_admin_id=auth_db["admin_id"])) == 2

    def test_05_duplicate_username_rejected(self, auth_db):
        from services.auth_service import AuthError, list_users

        _create_user(auth_db["admin_id"])
        with pytest.raises(AuthError, match="username"):
            _create_user(auth_db["admin_id"], email="other@example.com", username="JANE.DOE")
        assert len(list_users(actor_admin_id=auth_db["admin_id"])) == 2

    def test_06_invalid_password_rejected(self, auth_db):
        from services.auth_service import AuthError, list_users

        with pytest.raises(AuthError):
            _create_user(auth_db["admin_id"], password="short", confirm_password="short")
        with pytest.raises(AuthError):
            _create_user(
                auth_db["admin_id"], password="lettersOnly", confirm_password="lettersOnly"
            )
        with pytest.raises(AuthError, match="do not match"):
            _create_user(auth_db["admin_id"], confirm_password="Different123")
        assert len(list_users(actor_admin_id=auth_db["admin_id"])) == 1

    def test_07_new_user_logs_in_with_assigned_credentials(self, auth_db):
        from services.auth_service import authenticate

        _create_user(auth_db["admin_id"])
        account = authenticate("jane@example.com", "TempPass123")
        assert account is not None
        assert not account.is_admin
        assert account.must_change_password is True

    def test_08_new_user_reaches_user_dashboard_scope(self, auth_db):
        from services.auth_service import authenticate, get_account, is_admin_role, is_user_role

        _create_user(auth_db["admin_id"])
        authenticated = authenticate("jane@example.com", "TempPass123")
        dashboard_account = get_account(authenticated.id)
        assert dashboard_account is not None
        assert is_user_role(dashboard_account)
        assert not is_admin_role(dashboard_account)

    def test_09_admin_deactivates_user(self, auth_db):
        from services.auth_service import get_account_for_admin, set_user_status

        user = _create_user(auth_db["admin_id"])
        set_user_status(user.id, "inactive", actor_admin_id=auth_db["admin_id"])
        account = get_account_for_admin(user.id, actor_admin_id=auth_db["admin_id"])
        assert account is not None
        assert account.account_status == "inactive"
        assert account.is_active is False

    def test_10_deactivated_user_cannot_log_in(self, auth_db):
        from services.auth_service import authenticate, get_account, set_user_status

        user = _create_user(auth_db["admin_id"])
        set_user_status(user.id, "inactive", actor_admin_id=auth_db["admin_id"])
        assert authenticate("jane@example.com", "TempPass123") is None
        assert get_account(user.id) is None

    def test_11_admin_reactivates_user(self, auth_db):
        from services.auth_service import authenticate, set_user_status

        user = _create_user(auth_db["admin_id"])
        set_user_status(user.id, "suspended", actor_admin_id=auth_db["admin_id"])
        assert authenticate("jane@example.com", "TempPass123") is None
        set_user_status(user.id, "active", actor_admin_id=auth_db["admin_id"])
        assert authenticate("jane@example.com", "TempPass123") is not None

    def test_12_user_can_log_in_again_after_reactivation(self, auth_db):
        from services.auth_service import authenticate, set_user_status

        user = _create_user(auth_db["admin_id"])
        set_user_status(user.id, "inactive", actor_admin_id=auth_db["admin_id"])
        assert authenticate("jane@example.com", "TempPass123") is None
        set_user_status(user.id, "active", actor_admin_id=auth_db["admin_id"])
        assert authenticate("jane@example.com", "TempPass123") is not None

    def test_13_non_admin_cannot_access_admin_operations(self, auth_db):
        from services.auth_service import (
            AuthError,
            admin_create_user,
            admin_reset_password,
            delete_user,
            list_users,
            require_admin,
            set_user_role,
            set_user_status,
            update_user_account,
        )

        user = _create_user(auth_db["admin_id"])
        with pytest.raises(AuthError):
            require_admin(user.id)
        with pytest.raises(AuthError):
            require_admin(None)
        with pytest.raises(AuthError):
            admin_create_user(
                actor_admin_id=user.id,
                full_name="Evil",
                email="evil@example.com",
                username="evil",
                password="EvilPass123",
                confirm_password="EvilPass123",
            )
        with pytest.raises(AuthError):
            set_user_status(user.id, "active", actor_admin_id=user.id)
        with pytest.raises(AuthError):
            set_user_role(user.id, "admin", actor_admin_id=user.id)
        with pytest.raises(AuthError):
            admin_reset_password(user.id, "Another123", actor_admin_id=user.id)
        with pytest.raises(AuthError):
            update_user_account(
                user.id,
                actor_admin_id=user.id,
                full_name="Evil",
                email="jane@example.com",
                username="jane.doe",
            )
        with pytest.raises(AuthError):
            delete_user(user.id, actor_admin_id=user.id)
        assert [item.email for item in list_users(actor_admin_id=auth_db["admin_id"])] == ["admin@example.com", "jane@example.com"]

    def test_14_existing_accounts_keep_working(self, auth_db):
        from services.auth_service import authenticate, get_user_statistics, list_users

        _create_user(auth_db["admin_id"], email="one@example.com", username="one")
        _create_user(auth_db["admin_id"], email="two@example.com", username="two")
        assert authenticate("admin@example.com", "Admin1234") is not None
        assert authenticate("one@example.com", "TempPass123") is not None
        assert authenticate("two@example.com", "TempPass123") is not None
        assert len(list_users(actor_admin_id=auth_db["admin_id"])) == 3
        assert get_user_statistics(actor_admin_id=auth_db["admin_id"])["total"] == 3

    def test_15_created_user_cannot_self_assign_admin(self, auth_db):
        from database import User, get_db_context
        from services.auth_service import AuthError, set_user_role

        user = _create_user(auth_db["admin_id"])
        with get_db_context() as db:
            assert db.query(User).filter(User.id == user.id).one().role == "user"
        with pytest.raises(AuthError):
            set_user_role(user.id, "admin", actor_admin_id=user.id)
        with get_db_context() as db:
            assert db.query(User).filter(User.id == user.id).one().role == "user"

    def test_16_user_changes_password_after_login(self, auth_db):
        from services.auth_service import authenticate, change_password, get_account

        user = _create_user(auth_db["admin_id"])
        change_password(user.id, "TempPass123", "Changed123")
        refreshed = get_account(user.id)
        assert refreshed is not None
        assert refreshed.must_change_password is False
        assert authenticate("jane@example.com", "Changed123") is not None

    def test_17_admin_password_reset_forces_change(self, auth_db):
        from services.auth_service import admin_reset_password, authenticate, get_account

        user = _create_user(auth_db["admin_id"])
        admin_reset_password(user.id, "ResetPass123", actor_admin_id=auth_db["admin_id"])
        assert get_account(user.id).must_change_password is True
        assert authenticate("jane@example.com", "ResetPass123") is not None

    def test_18_admin_can_activate_and_suspend(self, auth_db):
        from services.auth_service import authenticate, set_user_status

        user = _create_user(auth_db["admin_id"], account_status="inactive")
        assert authenticate("jane@example.com", "TempPass123") is None
        set_user_status(user.id, "active", actor_admin_id=auth_db["admin_id"])
        assert authenticate("jane@example.com", "TempPass123") is not None
        set_user_status(user.id, "suspended", actor_admin_id=auth_db["admin_id"])
        assert authenticate("jane@example.com", "TempPass123") is None

    def test_19_dashboard_statistics_track_all_statuses(self, auth_db):
        from services.auth_service import get_user_statistics, set_user_status

        active = _create_user(auth_db["admin_id"], email="active@example.com", username="active")
        inactive = _create_user(
            auth_db["admin_id"],
            email="inactive@example.com",
            username="inactive",
            account_status="inactive",
        )
        suspended = _create_user(
            auth_db["admin_id"],
            email="suspended@example.com",
            username="suspended",
            account_status="suspended",
        )
        assert inactive.account_status == "inactive"
        assert suspended.account_status == "suspended"
        set_user_status(active.id, "active", actor_admin_id=auth_db["admin_id"])
        stats = get_user_statistics(actor_admin_id=auth_db["admin_id"])
        assert stats == {
            "total": 4,
            "active": 2,
            "inactive": 1,
            "suspended": 1,
            "new_last_7_days": 4,
        }

    def test_20_delete_user_cleans_dependencies(self, auth_db):
        from database import (
            DocumentChunk,
            EventLog,
            InterviewProgress,
            User,
            UserSession,
            UserSettings,
            get_db_context,
        )
        from services.auth_service import bind_session_to_account, delete_user
        from sqlalchemy import inspect

        user = _create_user(auth_db["admin_id"])
        token = bind_session_to_account(user, None)
        with get_db_context() as db:
            inspector = inspect(db.bind)

            def ondelete(table: str, column: str) -> str | None:
                for fk in inspector.get_foreign_keys(table):
                    if fk["constrained_columns"] == [column]:
                        return fk["options"].get("ondelete")
                return None

            assert ondelete("learning_tasks", "roadmap_id") == "CASCADE"
            assert ondelete("generated_reports", "interview_session_id") == "SET NULL"
            assert ondelete("resume_job_matches", "resume_analysis_id") == "CASCADE"
            assert ondelete("event_logs", "user_id") == "SET NULL"
            assert any(
                index["name"] == "ix_users_workspace_token_unique" and index["unique"]
                for index in inspector.get_indexes("users")
            )

            session_row = db.query(UserSession).filter(UserSession.session_id == token).one()
            db.add(
                DocumentChunk(
                    session_id=session_row.id,
                    source_type="resume",
                    chunk_text="sample",
                    chunk_index=0,
                )
            )
            db.add(InterviewProgress(session_id=session_row.id, total_interviews=1))
            db.add(UserSettings(user_id=user.id))
            db.add(EventLog(user_id=user.id, event_type="test.delete_user"))

        delete_user(user.id, actor_admin_id=auth_db["admin_id"])
        with get_db_context() as db:
            assert db.query(User).filter(User.id == user.id).first() is None
            assert db.query(UserSession).filter(UserSession.user_id == user.id).count() == 0
            assert db.query(UserSettings).filter(UserSettings.user_id == user.id).count() == 0
            assert db.query(User).filter(User.id == auth_db["admin_id"]).count() == 1
            event = db.query(EventLog).filter(EventLog.event_type == "test.delete_user").one()
            assert event.user_id is None

    def test_21_failed_login_is_logged_without_credentials(self, auth_db):
        from database import EventLog, get_db_context
        from services.auth_service import authenticate

        assert authenticate("admin@example.com", "definitely-not-the-password") is None

        with get_db_context() as db:
            event = db.query(EventLog).filter(EventLog.event_type == "auth.login_failed").one()

        assert event.user_id == auth_db["admin_id"]
        assert "definitely-not-the-password" not in event.message
        assert "admin@example.com" not in event.message


class TestAdminPageWiring:
    @pytest.mark.parametrize(
        "page_path,function_name",
        [
            ("frontend/admin/dashboard.py", "show_admin_dashboard"),
            ("frontend/admin/users.py", "show_admin_users"),
            ("frontend/admin/interviews.py", "show_admin_interviews"),
            ("frontend/admin/settings.py", "show_admin_settings"),
            ("frontend/shared/change_password.py", "show_change_password_page"),
        ],
    )
    def test_page_script_renders_its_view(self, page_path, function_name):
        import ast

        tree = ast.parse((PROJECT_ROOT / page_path).read_text(encoding="utf-8"))
        called = any(
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == function_name
            for node in tree.body
        )
        assert called, f"{page_path} must call {function_name}()"

    def test_admin_users_page_denies_non_admin(self, auth_db):
        from services.auth_service import Account
        from streamlit.testing.v1 import AppTest

        non_admin = Account(
            id=999,
            email="intruder@example.com",
            username="intruder",
            full_name="Intruder",
            role="user",
            account_status="active",
            is_active=True,
            must_change_password=False,
        )
        app = AppTest.from_file(str(PROJECT_ROOT / "frontend/admin/users.py"), default_timeout=20)
        app.session_state["account"] = non_admin
        app.run()
        assert any("Administrator access required" in error.value for error in app.error)

    def test_admin_users_page_renders_for_admin(self, auth_db):
        from services.auth_service import get_account
        from streamlit.testing.v1 import AppTest

        admin = get_account(auth_db["admin_id"])
        assert admin is not None
        _create_user(auth_db["admin_id"])
        app = AppTest.from_file(str(PROJECT_ROOT / "frontend/admin/users.py"), default_timeout=20)
        app.session_state["account"] = admin
        app.run()
        assert any("User Management" in title.value for title in app.title)
        assert app.exception == []


class TestAdminCreateUserForms:
    @staticmethod
    def _open_page(page_path, admin_id, monkeypatch):
        from services import llm_service
        from services.auth_service import get_account
        from streamlit.testing.v1 import AppTest

        monkeypatch.setattr(llm_service, "check_provider_health", dict)
        admin = get_account(admin_id)
        assert admin is not None
        app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30)
        app.session_state["db_initialized"] = True
        app.session_state["user_id"] = admin_id
        app.session_state["account"] = admin
        app.session_state["must_change_password"] = False
        app.run()
        app.switch_page(page_path).run()
        assert app.exception == []
        return app

    @staticmethod
    def _fill_form(app, password="FormPass123"):
        values = {
            "Full Name *": "Form User",
            "Email Address *": "form.user@example.com",
            "Username *": "form.user",
            "Password *": password,
            "Confirm Password *": password,
        }
        for index, widget in enumerate(app.text_input):
            label = str(widget.label).replace("\u200b", "")
            if label in values:
                app.text_input[index].set_value(values.pop(label))
        assert not values

    @staticmethod
    def _submit(app, button_label):
        button = next(button for button in app.button if button.label == button_label)
        button.click().run()

    @pytest.mark.parametrize(
        "page_path,button_label",
        [
            ("frontend/admin/dashboard.py", "Create Account"),
            ("frontend/admin/users.py", "Create User Account"),
        ],
    )
    def test_create_user_shows_persistent_confirmation(
        self, auth_db, monkeypatch, page_path, button_label
    ):
        from services.auth_service import list_users

        app = self._open_page(page_path, auth_db["admin_id"], monkeypatch)
        self._fill_form(app)
        self._submit(app, button_label)
        assert app.exception == []
        assert app.error == []
        assert any(
            "User account created for form.user@example.com" in success.value
            for success in app.success
        )
        assert "form.user@example.com" in [user.email for user in list_users(actor_admin_id=auth_db["admin_id"])]

    @pytest.mark.parametrize(
        "page_path,button_label",
        [
            ("frontend/admin/dashboard.py", "Create Account"),
            ("frontend/admin/users.py", "Create User Account"),
        ],
    )
    def test_create_user_surfaces_validation_detail(
        self, auth_db, monkeypatch, page_path, button_label
    ):
        from services.auth_service import list_users

        app = self._open_page(page_path, auth_db["admin_id"], monkeypatch)
        self._fill_form(app, password="abcdefgh")
        self._submit(app, button_label)
        assert app.exception == []
        assert any("letter and one number" in caption.value for caption in app.caption)
        assert "form.user@example.com" not in [user.email for user in list_users(actor_admin_id=auth_db["admin_id"])]


class TestPhaseTwoSecurityRegressions:
    def test_self_service_password_flows_are_disabled(self, auth_db):
        from services.auth_service import (
            activate_user,
            forgot_password,
            resend_activation,
            reset_password,
        )

        with pytest.raises(ValueError, match="disabled"):
            forgot_password("admin@example.com")
        with pytest.raises(ValueError, match="disabled"):
            reset_password("token", "Another123")
        with pytest.raises(ValueError, match="disabled"):
            activate_user("token")
        with pytest.raises(ValueError, match="disabled"):
            resend_activation(auth_db["admin_id"])

    def test_verify_password_rejects_unbounded_iterations(self):
        from services.auth_service import verify_password

        assert verify_password("Pass1234", "pbkdf2_sha256$0$00$00") is False
        assert verify_password("Pass1234", "pbkdf2_sha256$2000000$00$00") is False

    def test_admin_service_functions_reject_unauthenticated_actor(self, auth_db):
        from services.auth_service import (
            get_account_for_admin,
            get_user_statistics,
            list_events,
            list_users,
        )

        with pytest.raises(ValueError, match="Administrator access"):
            list_users(actor_admin_id=None)
        with pytest.raises(ValueError, match="Administrator access"):
            get_user_statistics(actor_admin_id=None)
        with pytest.raises(ValueError, match="Administrator access"):
            get_account_for_admin(auth_db["admin_id"], actor_admin_id=None)
        with pytest.raises(ValueError, match="Administrator access"):
            list_events(actor_admin_id=None)

    def test_bind_session_generates_private_workspace_token(self, auth_db):
        from database import User, UserSession, get_db_context
        from services.auth_service import bind_session_to_account

        account = _create_user(auth_db["admin_id"])
        token = bind_session_to_account(account, f"user-{account.id}")

        assert token and not token.startswith("user-")
        with get_db_context() as db:
            user = db.query(User).filter(User.id == account.id).one()
            assert user.workspace_token == token
            row = db.query(UserSession).filter(UserSession.session_id == token).one()
            assert row.user_id == account.id

    def test_sign_out_clears_credential_widget_state(self):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_string(
            """
import streamlit as st
from frontend.shared.auth_ui import sign_out

if "guard" not in st.session_state:
    st.session_state.guard = True
    st.session_state.sign_in_password = "secret"
    st.session_state.settings_groq_key = "secret"
    sign_out()
st.write("done")
"""
        )
        app.run()

        assert app.exception == []
        assert "sign_in_password" not in app.session_state
        assert "settings_groq_key" not in app.session_state

    def test_reset_token_reuse_and_expiry_are_impossible(self):
        from services.auth_service import reset_password

        with pytest.raises(ValueError, match="disabled"):
            reset_password("single-use-token", "Another123")
        with pytest.raises(ValueError, match="disabled"):
            reset_password("single-use-token", "Another123")
        with pytest.raises(ValueError, match="disabled"):
            reset_password("expired-token", "Another123")

    def test_provider_settings_apply_requires_admin(self):
        from services.auth_service import AuthError

        from frontend.user.settings_page import _apply

        with pytest.raises(AuthError, match="Administrator access"):
            _apply({"AI_PROVIDER_MODE": "manual"})


class TestAdminPlatformReports:
    def test_admin_pages_are_registered_and_wired(self):
        import ast
        from pathlib import Path

        project_root = Path(__file__).resolve().parents[1]
        tree = ast.parse((project_root / "app.py").read_text(encoding="utf-8"))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_admin_pages"
        )
        page_paths: list[str] = []
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Page"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                page_paths.append(str(node.args[0].value))

        assert "frontend/admin/reports.py" in page_paths
        for page_path in page_paths:
            assert (project_root / page_path).exists(), page_path
            page_tree = ast.parse((project_root / page_path).read_text(encoding="utf-8"))
            assert any(
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                for node in ast.walk(page_tree)
            ), page_path

    def test_admin_reports_page_renders_for_admin(self, auth_db):
        from services.auth_service import get_account
        from streamlit.testing.v1 import AppTest

        account = get_account(auth_db["admin_id"])
        assert account is not None
        app = AppTest.from_file(
            str(PROJECT_ROOT / "frontend/admin/reports.py"), default_timeout=30
        )
        app.session_state["account"] = account
        app.run()

        assert not app.exception, [str(error.value) for error in app.exception]

    def test_admin_report_includes_all_workspaces(self, auth_db):
        from database import InterviewQuestion, InterviewSession, User, UserSession, get_db_context
        from services.analytics import get_platform_report, get_platform_report_date_range
        from services.auth_service import AuthError

        admin_id = auth_db["admin_id"]
        member = _create_user(admin_id)

        with get_db_context() as db:
            admin = db.query(User).filter(User.id == admin_id).one()
            member_row = db.query(User).filter(User.id == member.id).one()
            admin_workspace = UserSession(session_id="admin-report-admin", user_id=admin.id)
            member_workspace = UserSession(session_id="admin-report-member", user_id=member_row.id)
            db.add_all([admin_workspace, member_workspace])
            db.flush()

            completed = InterviewSession(
                session_id=admin_workspace.id,
                interview_type="Technical",
                difficulty="hard",
                status="completed",
                overall_score=8.0,
                technical_score=8.0,
                communication_score=7.0,
                problem_solving_score=9.0,
            )
            created = InterviewSession(
                session_id=member_workspace.id,
                interview_type="HR",
                difficulty="medium",
                status="created",
            )
            db.add_all([completed, created])
            db.flush()
            db.add(
                InterviewQuestion(
                    interview_session_id=completed.id,
                    question_number=1,
                    question_text="Explain a trade-off.",
                    overall_score=7.0,
                )
            )

        snapshot = get_platform_report(admin_id)

        assert snapshot.total_interviews == 2
        assert snapshot.completed_interviews == 1
        assert snapshot.unique_users == 2
        assert snapshot.average_score == 8.0
        assert snapshot.total_questions == 1
        assert snapshot.answered_questions == 1
        assert {row.user_email for row in snapshot.rows} == {
            "admin@example.com",
            "jane@example.com",
        }
        assert all(row.answered_questions <= row.total_questions for row in snapshot.rows)

        earliest, latest = get_platform_report_date_range(admin_id)
        assert earliest is not None
        assert latest is not None
        assert get_platform_report(admin_id, latest, latest).total_interviews == 2

        with pytest.raises(AuthError, match="Administrator access"):
            get_platform_report(member.id)
        with pytest.raises(AuthError, match="Administrator access"):
            get_platform_report_date_range(member.id)
