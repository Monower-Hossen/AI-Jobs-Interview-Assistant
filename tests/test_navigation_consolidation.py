"""Phase 2 navigation consolidation guards and wrapper-page regression tests."""

import ast
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
IMPROVEMENT_PLANS_PAGE = "frontend/user/improvement_plans.py"
LEARNING_PROGRESS_VIEW = "frontend/user/learning_progress_view.py"

ORIGINAL_ALIASES = {
    "dashboard": "frontend/user/dashboard.py",
    "overview": "frontend/user/dashboard.py",
    "resume": "frontend/user/resume_analyzer.py",
    "job": "frontend/user/job_page.py",
    "job_description": "frontend/user/job_page.py",
    "job_matcher": "frontend/user/job_page.py",
    "interview_setup": "frontend/user/job_interview.py",
    "interview": "frontend/user/job_interview.py",
    "job_interview": "frontend/user/job_interview.py",
    "live_webcam": "frontend/user/live_webcam_interview.py",
    "live_webcam_interview": "frontend/user/live_webcam_interview.py",
    "live_recording_history": "frontend/user/live_recording_history.py",
    "live_interview_invitations": "frontend/user/live_interview_invitations.py",
    "report": "frontend/user/report_page.py",
    "reports": "frontend/user/report_page.py",
    "progress": "frontend/user/analytics.py",
    "analytics": "frontend/user/analytics.py",
    "history": "frontend/user/interview_history.py",
    "settings": "frontend/user/account_settings.py",
    "account_settings": "frontend/user/account_settings.py",
    "coach": "frontend/user/coach.py",
    "behavioral": "frontend/user/coach.py",
    "evaluation": "frontend/user/coach.py",
    "technical": "frontend/user/learning_center.py",
    "technical_assessment": "frontend/user/learning_center.py",
    "knowledge": "frontend/user/learning_center.py",
    "knowledge_base": "frontend/user/learning_center.py",
    "question": "frontend/user/question_bank.py",
    "question_bank": "frontend/user/question_bank.py",
    "roadmap": "frontend/user/learning_resources.py",
    "learning_roadmap": "frontend/user/learning_resources.py",
    "learning_center": "frontend/user/learning_center.py",
    "notifications": "frontend/user/notifications.py",
    "profile": "frontend/user/profile.py",
    "admin": "frontend/admin/dashboard.py",
    "admin_jobs": "frontend/admin/jobs.py",
    "admin_live_media": "frontend/admin/live_media.py",
    "admin_live_interviews": "frontend/admin/live_interview_invitations.py",
    "admin_learning": "frontend/admin/learning.py",
}


def _navigation_pages() -> dict[str, str]:
    tree = ast.parse(
        (PROJECT_ROOT / "frontend" / "shared" / "navigation.py").read_text(encoding="utf-8")
    )
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "PAGES"
            and isinstance(node.value, ast.Dict)
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("PAGES mapping was not found")


def _user_page_groups() -> dict[str, list[dict[str, object]]]:
    tree = ast.parse((PROJECT_ROOT / "app.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_user_pages"
    )
    return_node = next(node for node in function.body if isinstance(node, ast.Return))
    assert isinstance(return_node.value, ast.Dict)
    groups: dict[str, list[dict[str, object]]] = {}
    for key_node, value_node in zip(return_node.value.keys, return_node.value.values):
        assert isinstance(key_node, ast.Constant)
        entries: list[dict[str, object]] = []
        for node in ast.walk(value_node):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Attribute)
                or node.func.attr != "Page"
                or not node.args
                or not isinstance(node.args[0], ast.Constant)
            ):
                continue
            entry: dict[str, object] = {"path": str(node.args[0].value)}
            for keyword in node.keywords:
                if keyword.arg and isinstance(keyword.value, ast.Constant):
                    entry[keyword.arg] = keyword.value.value
            entries.append(entry)
        groups[str(key_node.value)] = entries
    return groups


def test_all_original_aliases_are_preserved() -> None:
    pages = _navigation_pages()
    for alias, target in ORIGINAL_ALIASES.items():
        assert pages[alias] == target, alias
        assert (PROJECT_ROOT / target).exists(), target


def test_improvement_plans_alias_resolves() -> None:
    pages = _navigation_pages()
    assert pages["improvement_plans"] == IMPROVEMENT_PLANS_PAGE
    assert (PROJECT_ROOT / IMPROVEMENT_PLANS_PAGE).exists()


def test_learning_progress_route_is_removed() -> None:
    pages = _navigation_pages()
    assert "learning_progress" not in pages
    assert not (PROJECT_ROOT / "frontend/user/learning_progress.py").exists()


def test_visible_sidebar_has_no_duplicate_targets() -> None:
    groups = _user_page_groups()
    visible_paths = [
        str(entry["path"])
        for entries in groups.values()
        for entry in entries
        if entry.get("visibility", "visible") == "visible"
    ]
    assert len(visible_paths) == len(set(visible_paths))


def test_mock_interview_routes_are_removed() -> None:
    studio = _user_page_groups()["Interview Studio"]
    assert not [
        entry
        for entry in studio
        if "mock" in str(entry.get("title", "")).lower()
        or "mock" in str(entry.get("url_path", "")).lower()
    ]
    pages = _navigation_pages()
    for alias in ("mock_interview", "ai_avatar", "ai_avatar_interview", "legacy_mock_interview"):
        assert alias not in pages


def test_learning_center_reuses_shared_renderer() -> None:
    center_source = (PROJECT_ROOT / "frontend/user/learning_center.py").read_text(encoding="utf-8")
    assert "from frontend.user.learning_progress_view import" in center_source
    assert "render_learning_progress(session_id, snapshot)" in center_source
    assert "def _render_progress" not in center_source

    view_tree = ast.parse((PROJECT_ROOT / LEARNING_PROGRESS_VIEW).read_text(encoding="utf-8"))
    module_level_calls = [
        node
        for node in view_tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]
    assert not module_level_calls


def test_improvement_plans_reuses_report_renderer_and_owner_scope() -> None:
    page_source = (PROJECT_ROOT / IMPROVEMENT_PLANS_PAGE).read_text(encoding="utf-8")
    assert "show_improvement_plan" in page_source
    assert "from frontend.shared.components import" in page_source
    assert "InterviewSession.session_id == session_id" in page_source
    assert "owned_session_id" in page_source

    report_source = (PROJECT_ROOT / "frontend/user/report_page.py").read_text(encoding="utf-8")
    assert "def show_improvement_plan" not in report_source
    assert "show_improvement_plan" in report_source

    components_source = (PROJECT_ROOT / "frontend/shared/components.py").read_text(encoding="utf-8")
    assert "def show_improvement_plan" in components_source


@pytest.fixture
def wrapper_db(tmp_path):
    import database.db as db_module
    from config.settings import settings
    from database import User, UserSession
    from services import auth_service as auth

    original_database = settings.database_path
    original_vectors = settings.vector_store_path
    settings.database_path = str(tmp_path / "wrappers.db")
    settings.vector_store_path = str(tmp_path / "vectors")
    db_module.engine = None
    db_module.SessionLocal = None
    db_module.init_database()

    with db_module.get_db_context() as db:
        users = []
        for index in (1, 2):
            user = User(
                email=f"user{index}@example.com",
                username=f"user{index}",
                full_name=f"User {index}",
                password_hash=auth.hash_password("Pass1234"),
                role="user",
                account_status="active",
                is_active=True,
                is_verified=True,
                must_change_password=False,
            )
            db.add(user)
            users.append(user)
        db.flush()
        session_one = UserSession(session_id="user-1", user_id=users[0].id, label="one")
        session_two = UserSession(session_id="user-2", user_id=users[1].id, label="two")
        db.add_all([session_one, session_two])
        db.flush()
        ids = {
            "user1": users[0].id,
            "user2": users[1].id,
            "session1": session_one.id,
            "session2": session_two.id,
        }

    yield ids

    db_module.close_database()
    db_module.SessionLocal = None
    settings.database_path = original_database
    settings.vector_store_path = original_vectors


def _seed_plan_interview(session_id: int) -> None:
    from database import InterviewSession, get_db_context

    with get_db_context() as db:
        db.add(
            InterviewSession(
                session_id=session_id,
                interview_type="Technical",
                difficulty="medium",
                status="completed",
                overall_score=8.0,
                technical_score=8.0,
                communication_score=8.0,
                problem_solving_score=8.0,
                improvement_plan_7_day=[
                    {
                        "day": 1,
                        "focus": "System design",
                        "activity": "Practice caching questions",
                        "estimated_hours": 2,
                        "resources": ["Designing Data-Intensive Applications"],
                    }
                ],
                improvement_plan_30_day=[
                    {
                        "day": 1,
                        "focus": "Communication",
                        "activity": "Record structured answers",
                        "estimated_hours": 1,
                        "resources": [],
                    }
                ],
                completed_at=datetime.now(),
                created_at=datetime.now(),
            )
        )


def _run_app(user_key: str, token: str, wrapper_db, monkeypatch, page_path: str):
    from services import knowledge_base
    from services.auth_service import get_account
    from streamlit.testing.v1 import AppTest

    class _StubKnowledgeBase:
        def stats(self):
            return {"chunks": 0, "documents": 0, "indexed": 0}

        def build_context(self, query):
            return ""

        def list_documents(self):
            return []

    monkeypatch.setattr(
        knowledge_base, "get_knowledge_base", lambda *args, **kwargs: _StubKnowledgeBase()
    )
    account = get_account(wrapper_db[user_key])
    assert account is not None
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30)
    app.session_state["db_initialized"] = True
    app.session_state["user_id"] = wrapper_db[user_key]
    app.session_state["user_session_id"] = token
    app.session_state["account"] = account
    app.session_state["must_change_password"] = False
    app.run()
    assert not app.exception, [str(error.value) for error in app.exception]
    app.switch_page(page_path).run()
    assert not app.exception, [str(error.value) for error in app.exception]
    return app


def test_improvement_plans_page_renders_saved_plans(wrapper_db, monkeypatch):
    _seed_plan_interview(wrapper_db["session1"])
    app = _run_app("user1", "user-1", wrapper_db, monkeypatch, IMPROVEMENT_PLANS_PAGE)
    markdown_text = " ".join(str(item.value) for item in app.markdown)
    assert "Practice caching questions" in markdown_text
    assert "Record structured answers" in markdown_text
    assert app.selectbox


def test_improvement_plans_page_is_owner_scoped(wrapper_db, monkeypatch):
    _seed_plan_interview(wrapper_db["session1"])
    app = _run_app("user2", "user-2", wrapper_db, monkeypatch, IMPROVEMENT_PLANS_PAGE)
    info_text = " ".join(str(item.value) for item in app.info)
    assert "No improvement plans yet" in info_text
    markdown_text = " ".join(str(item.value) for item in app.markdown)
    assert "Practice caching questions" not in markdown_text
