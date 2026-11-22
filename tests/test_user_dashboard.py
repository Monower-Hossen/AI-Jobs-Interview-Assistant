import ast
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _registered_user_pages() -> list[str]:
    tree = ast.parse((PROJECT_ROOT / "app.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_user_pages"
    )
    paths: list[str] = []
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Page"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            paths.append(str(node.args[0].value))
    return paths


def _assert_page_is_wired(page_path: str) -> None:
    tree = ast.parse((PROJECT_ROOT / page_path).read_text(encoding="utf-8"))
    called = any(
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        for node in ast.walk(tree)
    )
    assert called, f"{page_path} must call its render function"


@pytest.fixture
def dashboard_db(tmp_path):
    import database.db as db_module
    from config.settings import settings
    from database import User, UserSession
    from services import auth_service as auth

    original_database = settings.database_path
    original_vectors = settings.vector_store_path
    settings.database_path = str(tmp_path / "dashboard.db")
    settings.vector_store_path = str(tmp_path / "vectors")
    db_module.engine = None
    db_module.SessionLocal = None
    db_module.init_database()

    with db_module.get_db_context() as db:
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
        db.add_all([user_one, user_two])
        db.flush()
        session_one = UserSession(session_id="user-1", user_id=user_one.id, label="one")
        session_two = UserSession(session_id="user-2", user_id=user_two.id, label="two")
        db.add_all([session_one, session_two])
        db.flush()
        ids = {
            "user1": user_one.id,
            "user2": user_two.id,
            "session1": session_one.id,
            "session2": session_two.id,
        }

    yield ids

    db_module.close_database()
    db_module.SessionLocal = None
    settings.database_path = original_database
    settings.vector_store_path = original_vectors


class _StubKnowledgeBase:
    def stats(self) -> dict[str, int]:
        return {"chunks": 0, "documents": 0, "indexed": 0}

    def build_context(self, query: str) -> str:
        return ""

    def list_documents(self) -> list:
        return []

    def add_document(self, *args, **kwargs) -> int:
        return 0

    def rebuild_index(self) -> int:
        return 0

    def answer_query(self, query: str) -> dict:
        return {"answer": "", "sources": [], "grounded": False}

    def search(self, query: str) -> list:
        return []

    def generate_questions(self, topic: str, count: int) -> list:
        return []

    def delete_document(self, document_id: int) -> bool:
        return False


def _seed_interview(session_id: int, score: float, duration_seconds: int = 1800) -> None:
    from database import InterviewSession, get_db_context

    with get_db_context() as db:
        db.add(
            InterviewSession(
                session_id=session_id,
                interview_type="Technical",
                difficulty="medium",
                status="completed",
                overall_score=score,
                technical_score=score,
                communication_score=score,
                problem_solving_score=score,
                duration_seconds=duration_seconds,
                completed_at=datetime.now(),
                created_at=datetime.now(),
            )
        )


def _seed_feedback(session_id: int, score: float) -> None:
    from database import InterviewQuestion, InterviewSession, get_db_context

    with get_db_context() as db:
        interview = InterviewSession(
            session_id=session_id,
            interview_type="Technical",
            difficulty="medium",
            status="completed",
            overall_score=score,
            technical_score=score,
            communication_score=score,
            problem_solving_score=score,
            completed_at=datetime.now(),
            created_at=datetime.now(),
        )
        db.add(interview)
        db.flush()
        db.add(
            InterviewQuestion(
                interview_session_id=interview.id,
                question_number=1,
                question_text="Explain caching trade-offs",
                category="Technical",
                overall_score=score,
                weaknesses=["Add a concrete example"],
                evaluated_at=datetime.now(),
            )
        )


class TestUserDashboardWiring:
    def test_registered_user_pages_render_their_views(self):
        pages = _registered_user_pages()
        assert len(pages) >= 10
        for page_path in pages:
            assert (PROJECT_ROOT / page_path).exists(), page_path
            _assert_page_is_wired(page_path)

    def test_navigation_targets_exist(self):
        tree = ast.parse((PROJECT_ROOT / "frontend/shared/navigation.py").read_text("utf-8"))
        pages = {}
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "PAGES":
                    pages = ast.literal_eval(node.value)
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "PAGES" for target in node.targets
            ):
                pages = ast.literal_eval(node.value)
        assert "report" in pages
        assert "coach" in pages
        assert "question_bank" in pages
        assert "learning_center" in pages
        for target in pages.values():
            assert (PROJECT_ROOT / target).exists(), target

    @pytest.mark.parametrize("page_path", _registered_user_pages())
    def test_user_pages_render_without_exceptions(self, dashboard_db, monkeypatch, page_path):
        from services import knowledge_base
        from services.auth_service import get_account
        from streamlit.testing.v1 import AppTest

        monkeypatch.setattr(
            knowledge_base, "get_knowledge_base", lambda *args, **kwargs: _StubKnowledgeBase()
        )
        account = get_account(dashboard_db["user1"])
        assert account is not None
        app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30)
        app.session_state["db_initialized"] = True
        app.session_state["user_id"] = dashboard_db["user1"]
        app.session_state["user_session_id"] = "user-1"
        app.session_state["account"] = account
        app.session_state["must_change_password"] = False
        app.run()
        assert not app.exception, [str(error.value) for error in app.exception]

        app.switch_page(page_path).run()
        assert not app.exception, (page_path, [str(error.value) for error in app.exception])

    def test_empty_dashboard_keeps_quick_actions(self, dashboard_db, monkeypatch):
        from services import knowledge_base
        from services.auth_service import get_account
        from streamlit.testing.v1 import AppTest

        monkeypatch.setattr(
            knowledge_base, "get_knowledge_base", lambda *args, **kwargs: _StubKnowledgeBase()
        )
        account = get_account(dashboard_db["user1"])
        assert account is not None
        app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30)
        app.session_state["db_initialized"] = True
        app.session_state["user_id"] = dashboard_db["user1"]
        app.session_state["user_session_id"] = "user-1"
        app.session_state["account"] = account
        app.session_state["must_change_password"] = False
        app.run()

        assert not app.exception, [str(error.value) for error in app.exception]
        labels = [button.label for button in app.button]
        assert "Analyze resume" in labels
        assert "Start interview" in labels


class TestUserDashboardServices:
    def test_dashboard_snapshot_is_owner_scoped(self, dashboard_db):
        from database import ResumeAnalysis, get_db_context
        from services.dashboard_service import get_dashboard_snapshot

        _seed_interview(dashboard_db["session1"], 8.0)
        _seed_interview(dashboard_db["session1"], 7.0, duration_seconds=1800)
        _seed_interview(dashboard_db["session2"], 3.0)
        with get_db_context() as db:
            db.add(
                ResumeAnalysis(
                    session_id=dashboard_db["session1"],
                    file_name="resume.txt",
                    raw_text="Python engineer",
                    overall_resume_score=72.0,
                )
            )

        first = get_dashboard_snapshot(dashboard_db["session1"], dashboard_db["user1"])
        second = get_dashboard_snapshot(dashboard_db["session2"], dashboard_db["user2"])

        assert first.analytics.total_interviews == 2
        assert first.analytics.completed_interviews == 2
        assert first.practice_hours == 1.0
        assert first.resume_score == 72.0
        assert second.analytics.total_interviews == 1
        assert second.resume_score is None
        assert len(first.weekly_activity) == 7
        assert first.recent_activity

    def test_recent_feedback_is_owner_scoped(self, dashboard_db):
        from services.dashboard_service import get_dashboard_snapshot

        _seed_feedback(dashboard_db["session1"], 8.0)
        first = get_dashboard_snapshot(dashboard_db["session1"], dashboard_db["user1"])
        second = get_dashboard_snapshot(dashboard_db["session2"], dashboard_db["user2"])

        assert first.analytics.scored_interviews == 1
        assert len(first.recent_feedback) == 1
        assert first.recent_feedback[0]["Question"] == "Explain caching trade-offs"
        assert first.recent_feedback[0]["Focus"] == "Add a concrete example"
        assert second.recent_feedback == []

    def test_report_filters_by_date_and_score(self):
        from datetime import datetime, timedelta

        from frontend.user.report_page import _filter_interviews

        class _Interview:
            def __init__(self, days_ago: int, score: float):
                self.created_at = datetime.now() - timedelta(days=days_ago)
                self.overall_score = score

        interviews = [_Interview(1, 8.0), _Interview(10, 6.0), _Interview(20, 9.0)]
        today = datetime.now().date()
        filtered = _filter_interviews(interviews, today - timedelta(days=14), today, 7.0)
        assert len(filtered) == 1
        assert filtered[0].overall_score == 8.0
        assert _filter_interviews(interviews, None, None, 0.0) == interviews

    def test_user_content_crud_is_owner_scoped(self, dashboard_db):
        from services.user_content_service import (
            add_bookmark,
            delete_bookmark,
            delete_note,
            list_bookmarks,
            list_notes,
            save_note,
        )

        bookmark_id = add_bookmark(
            dashboard_db["session1"],
            "Explain vector databases",
            category="machine_learning",
            expected_key_points=["indexing", "recall"],
            actor_user_id=dashboard_db["user1"],
        )
        note_id = save_note(
            dashboard_db["session1"],
            "SQL notes",
            "Use window functions",
            "SQL",
            actor_user_id=dashboard_db["user1"],
        )
        assert len(list_bookmarks(dashboard_db["session1"], dashboard_db["user1"])) == 1
        assert len(list_notes(dashboard_db["session1"], dashboard_db["user1"])) == 1
        assert list_bookmarks(dashboard_db["session2"], dashboard_db["user2"]) == []
        assert list_notes(dashboard_db["session2"], dashboard_db["user2"]) == []
        assert not delete_bookmark(bookmark_id, dashboard_db["session2"], dashboard_db["user2"])
        assert not delete_note(note_id, dashboard_db["session2"], dashboard_db["user2"])

        save_note(
            dashboard_db["session1"],
            "SQL notes v2",
            "Practice joins",
            "SQL",
            note_id,
            actor_user_id=dashboard_db["user1"],
        )
        updated = list_notes(dashboard_db["session1"], dashboard_db["user1"])[0]
        assert updated["Title"] == "SQL notes v2"
        assert delete_bookmark(bookmark_id, dashboard_db["session1"], dashboard_db["user1"])
        assert list_bookmarks(dashboard_db["session1"], dashboard_db["user1"]) == []

    def test_preferences_are_validated_and_persisted(self, dashboard_db):
        from services.preferences_service import (
            get_user_preferences,
            update_account_details,
            update_user_preferences,
        )

        preferences = update_user_preferences(
            dashboard_db["user1"],
            default_role="ML Engineer",
            default_difficulty="hard",
            default_interview_type="Technical",
            voice_enabled=False,
            actor_user_id=dashboard_db["user1"],
        )
        assert preferences["default_role"] == "ML Engineer"
        assert (
            get_user_preferences(dashboard_db["user1"], dashboard_db["user1"])[
                "default_difficulty"
            ]
            == "hard"
        )
        with pytest.raises(ValueError, match="difficulty"):
            update_user_preferences(
                dashboard_db["user1"],
                default_difficulty="extreme",
                actor_user_id=dashboard_db["user1"],
            )
        update_account_details(
            dashboard_db["user1"],
            phone_number="555-0100",
            additional_profile_info="Career switcher",
            actor_user_id=dashboard_db["user1"],
        )

    def test_resume_quality_checks_sections_and_keywords(self):
        from services.job_analyzer import JobAnalysisResult
        from services.resume_analyzer import ResumeAnalysisResult, ResumeScores
        from services.resume_insights import analyze_resume_quality

        analysis = ResumeAnalysisResult(email="candidate@example.com", skills=["Python", "SQL"])
        scores = ResumeScores(structure=80, overall=75)
        text = """
        Email: candidate@example.com
        SUMMARY
        Data Engineer focused on data platforms.
        EXPERIENCE
        - Built pipelines that reduced latency by 35%.
        - Led a team of 4 engineers.
        EDUCATION
        BSc Computer Science
        PROJECTS
        SQL reporting dashboard
        SKILLS
        Python, SQL
        """
        job = JobAnalysisResult(job_title="Data Engineer", required_skills=["Python", "SQL"])
        quality = analyze_resume_quality(analysis, scores, text, "Data Engineer", job)
        assert quality.ats_score >= 60
        assert quality.keyword_coverage == 100.0
        assert quality.missing_keywords == []
        assert all(quality.section_checks.values())

    def test_knowledge_base_rejects_foreign_owner(self, dashboard_db):
        from services.knowledge_base import get_knowledge_base

        with pytest.raises(ValueError, match="signed-in account"):
            get_knowledge_base("user-1", dashboard_db["user2"])
        with pytest.raises(ValueError, match="Sign in"):
            get_knowledge_base("user-1", None)

    def test_knowledge_base_add_text_is_persistent_and_deduplicated(self, dashboard_db, monkeypatch):
        from rag import embeddings

        class FakeEmbedding:
            dimension = 3
            model_name = "fake-embedding"

            def encode(self, texts, **kwargs):
                return [[0.1, 0.2, 0.3] for _ in texts]

        monkeypatch.setattr(embeddings, "get_embedding_model", lambda: FakeEmbedding())
        from services.knowledge_base import get_knowledge_base, reset_knowledge_base

        reset_knowledge_base()
        try:
            knowledge_base = get_knowledge_base("user-1", dashboard_db["user1"])
            text = "Build Python data pipelines with SQL reporting dashboards."
            first = knowledge_base.add_text(text, "resume.txt", "resume")

            assert first["duplicate"] is False
            assert first["chunk_count"] > 0
            assert any(
                document["file_name"] == "resume.txt"
                for document in knowledge_base.list_documents()
            )

            duplicate = knowledge_base.add_text(text, "resume.txt", "resume")
            assert duplicate["duplicate"] is True
            assert duplicate["id"] == first["id"]
        finally:
            reset_knowledge_base()

    def test_user_services_reject_missing_actor(self, dashboard_db):
        from services.dashboard_service import get_dashboard_snapshot
        from services.preferences_service import get_user_preferences

        with pytest.raises(ValueError, match="signed-in user"):
            get_user_preferences(dashboard_db["user1"], None)
        with pytest.raises(ValueError, match="Sign in"):
            get_dashboard_snapshot(dashboard_db["session1"], None)

    def test_owner_helpers_reject_foreign_workspace(self, dashboard_db):
        from services.auth_service import require_owned_session_id, require_owned_user_id

        with pytest.raises(ValueError, match="signed-in user"):
            require_owned_user_id(dashboard_db["user2"], dashboard_db["user1"])
        with pytest.raises(ValueError, match="workspace"):
            require_owned_session_id(dashboard_db["session2"], dashboard_db["user1"])
