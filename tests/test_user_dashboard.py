import ast
from datetime import datetime, timedelta
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
        assert "Resume analysis" in labels
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


def _seed_interview_record(
    session_id: int,
    *,
    status: str = "completed",
    score: float | None = None,
    created: datetime | None = None,
    plan_7_day=None,
) -> int:
    from database import InterviewSession, get_db_context

    created_at = created or datetime.now()
    with get_db_context() as db:
        row = InterviewSession(
            session_id=session_id,
            interview_type="Technical",
            difficulty="medium",
            status=status,
            overall_score=score,
            technical_score=score,
            communication_score=score,
            problem_solving_score=score,
            duration_seconds=1800,
            completed_at=created_at if status == "completed" else None,
            created_at=created_at,
            improvement_plan_7_day=plan_7_day,
        )
        db.add(row)
        db.flush()
        return int(row.id)


def _seed_question(interview_id: int, *, confidence: float = 4.0) -> None:
    from database import InterviewQuestion, get_db_context

    with get_db_context() as db:
        db.add(
            InterviewQuestion(
                interview_session_id=interview_id,
                question_number=1,
                question_text="Explain caching trade-offs",
                category="technical",
                overall_score=5.0,
                technical_accuracy=6.0,
                communication=5.0,
                confidence=confidence,
                problem_solving=5.0,
                structure=5.0,
                evaluated_at=datetime.now(),
            )
        )


def _seed_learning(session_id: int, *, completed: int = 1) -> None:
    from database import Assessment, LearningRoadmap, LearningTask, get_db_context

    with get_db_context() as db:
        roadmap = LearningRoadmap(
            session_id=session_id,
            title="Focus plan",
            focus_skills=["Python"],
            created_at=datetime.now(),
        )
        db.add(roadmap)
        db.flush()
        for index in range(2):
            db.add(
                LearningTask(
                    roadmap_id=roadmap.id,
                    order_index=index,
                    title=f"Task {index + 1}",
                    status="completed" if index < completed else "pending",
                )
            )
        db.add(
            Assessment(
                session_id=session_id,
                topic="Technical",
                score=7.0,
                created_at=datetime.now(),
            )
        )


def _seed_notification(session_id: int, user_id: int, event_key: str) -> None:
    from services.learning_service import create_notification

    create_notification(
        session_id=session_id,
        actor_user_id=user_id,
        title="Study task due",
        message="Review your study plan.",
        notification_type="task_reminder",
        severity="warning",
        target_page="learning_center",
        event_key=event_key,
    )


def _seed_match(session_id: int, missing_skills: list[str]) -> None:
    from database import JobDescription, ResumeAnalysis, ResumeJobMatch, get_db_context

    with get_db_context() as db:
        resume = ResumeAnalysis(
            session_id=session_id,
            file_name="resume.txt",
            raw_text="Python engineer",
            overall_resume_score=70.0,
            created_at=datetime.now(),
        )
        job = JobDescription(
            session_id=session_id,
            title="Platform Engineer",
            raw_text="Build reliable platforms",
            created_at=datetime.now(),
        )
        db.add_all([resume, job])
        db.flush()
        db.add(
            ResumeJobMatch(
                resume_analysis_id=resume.id,
                job_description_id=job.id,
                missing_skills=missing_skills,
                skill_gaps=missing_skills,
                overall_match=60.0,
                created_at=datetime.now(),
            )
        )


def _add_dashboard_admin() -> dict:
    from database import User, UserSession, get_db_context
    from services import auth_service as auth

    with get_db_context() as db:
        admin = User(
            email="dash-admin@example.com",
            username="dash-admin",
            full_name="Dashboard Admin",
            password_hash=auth.hash_password("Pass1234"),
            role="admin",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        db.add(admin)
        db.flush()
        workspace = UserSession(session_id="admin-dash-token", user_id=admin.id, label="admin")
        db.add(workspace)
        db.flush()
        return {
            "user_id": int(admin.id),
            "session_id": int(workspace.id),
            "token": "admin-dash-token",
        }


class TestStage3DashboardEnhancements:
    def test_sections_are_data_backed_and_owner_scoped(self, dashboard_db):
        from services.dashboard_service import get_dashboard_snapshot

        base = datetime.now()
        _seed_interview_record(
            dashboard_db["session1"], score=7.0, created=base - timedelta(days=2)
        )
        high_id = _seed_interview_record(
            dashboard_db["session1"],
            score=8.0,
            created=base - timedelta(days=1),
            plan_7_day=["Practice system design"],
        )
        _seed_question(high_id)
        _seed_interview_record(dashboard_db["session1"], status="in_progress", created=base)
        _seed_interview_record(dashboard_db["session2"], score=3.0, created=base)
        _seed_learning(dashboard_db["session1"], completed=1)
        _seed_notification(dashboard_db["session1"], dashboard_db["user1"], "dash-notice-1")
        _seed_match(dashboard_db["session1"], ["Kubernetes"])

        first = get_dashboard_snapshot(
            dashboard_db["session1"],
            dashboard_db["user1"],
            session_token="user-1",
            actor_role="user",
        )
        second = get_dashboard_snapshot(
            dashboard_db["session2"],
            dashboard_db["user2"],
            session_token="user-2",
            actor_role="user",
        )

        assert first.interview_history_summary["total_sessions"] == 3
        assert first.interview_history_summary["completed_sessions"] == 2
        assert first.interview_history_summary["in_progress_sessions"] == 1
        assert first.interview_history_summary["latest_score"] is None
        assert len(first.interview_history_summary["recent_completed"]) == 2
        assert first.performance_comparison["available"] is True
        assert first.performance_comparison["latest_score"] == 8.0
        assert first.performance_comparison["previous_score"] == 7.0
        assert first.performance_comparison["difference"] == 1.0
        assert first.performance_comparison["direction"] == "improved"
        assert first.learning_progress == {
            "completed_tasks": 1,
            "total_tasks": 2,
            "completion_percent": 50.0,
            "assessment_count": 1,
            "summary": "1 of 2 study tasks complete.",
        }
        assert "Kubernetes" in first.skill_gaps["missing_skills"]
        assert "confidence" in first.skill_gaps["weak_areas"]
        assert first.improvement_plan["available"] is True
        assert "Practice system design" in first.improvement_plan["seven_day"]
        assert first.notification_unread_count == 1
        assert first.recent_notifications[0]["Title"] == "Study task due"

        assert second.interview_history_summary["total_sessions"] == 1
        assert second.performance_comparison["available"] is False
        assert second.learning_progress["total_tasks"] == 0
        assert second.learning_progress["completion_percent"] is None
        assert second.skill_gaps["has_data"] is False
        assert second.improvement_plan["available"] is False
        assert second.recent_notifications == []
        assert second.notification_unread_count == 0
        assert second.upcoming_invitations == []

    def test_comparison_handles_zero_previous_and_missing_scores(self, dashboard_db):
        from services.dashboard_service import get_dashboard_snapshot

        base = datetime.now()
        _seed_interview_record(
            dashboard_db["session1"], score=0.0, created=base - timedelta(days=2)
        )
        _seed_interview_record(
            dashboard_db["session1"], score=6.0, created=base - timedelta(days=1)
        )
        first = get_dashboard_snapshot(
            dashboard_db["session1"], dashboard_db["user1"], session_token="user-1"
        )
        comparison = first.performance_comparison
        assert comparison["available"] is True
        assert comparison["previous_score"] == 0.0
        assert comparison["percent_change"] is None
        assert comparison["difference"] == 6.0

        _seed_interview_record(dashboard_db["session2"], status="in_progress", created=base)
        second = get_dashboard_snapshot(
            dashboard_db["session2"], dashboard_db["user2"], session_token="user-2"
        )
        assert second.performance_comparison["available"] is False
        assert second.interview_history_summary["latest_score"] is None

    def test_learning_and_skill_sections_have_honest_empty_states(self, dashboard_db):
        from services.dashboard_service import get_dashboard_snapshot

        snapshot = get_dashboard_snapshot(
            dashboard_db["session1"], dashboard_db["user1"], session_token="user-1"
        )
        assert snapshot.learning_progress["total_tasks"] == 0
        assert snapshot.learning_progress["completion_percent"] is None
        assert snapshot.learning_progress["summary"] == "No study plan tasks saved yet."
        assert snapshot.skill_gaps["has_data"] is False
        assert snapshot.skill_gaps["items"] == []
        assert snapshot.improvement_plan["available"] is False
        assert snapshot.recent_notifications == []

    def test_pending_invitations_are_candidate_only_and_admin_hidden(self, dashboard_db):
        from services import live_interview_coordination_service as coordination
        from services.dashboard_service import get_dashboard_snapshot

        admin = _add_dashboard_admin()
        room = coordination.create_invitation(
            admin_user_id=admin["user_id"],
            session_token=admin["token"],
            candidate_user_id=dashboard_db["user1"],
            title="Pending backend interview",
        )
        assert room["status"] == "invited"

        candidate = get_dashboard_snapshot(
            dashboard_db["session1"],
            dashboard_db["user1"],
            session_token="user-1",
            actor_role="user",
        )
        other = get_dashboard_snapshot(
            dashboard_db["session2"],
            dashboard_db["user2"],
            session_token="user-2",
            actor_role="user",
        )
        admin_snapshot = get_dashboard_snapshot(
            admin["session_id"],
            admin["user_id"],
            session_token=admin["token"],
            actor_role="admin",
        )

        assert len(candidate.upcoming_invitations) == 1
        assert candidate.upcoming_invitations[0]["Title"] == "Pending backend interview"
        assert candidate.upcoming_invitations[0]["Status"] == "invited"
        assert other.upcoming_invitations == []
        assert admin_snapshot.upcoming_invitations == []

    def test_terminal_and_expired_invitations_are_excluded(self, dashboard_db):
        from database import InterviewCallRoom, get_db_context
        from services import live_interview_coordination_service as coordination
        from services.dashboard_service import get_dashboard_snapshot

        admin = _add_dashboard_admin()
        declined = coordination.create_invitation(
            admin_user_id=admin["user_id"],
            session_token=admin["token"],
            candidate_user_id=dashboard_db["user1"],
            title="Declined interview",
        )
        coordination.decline_invitation(
            room_id=declined["room_id"],
            user_id=dashboard_db["user1"],
            session_token="user-1",
        )
        expired = coordination.create_invitation(
            admin_user_id=admin["user_id"],
            session_token=admin["token"],
            candidate_user_id=dashboard_db["user1"],
            title="Expired interview",
        )
        with get_db_context() as db:
            row = db.query(InterviewCallRoom).filter(
                InterviewCallRoom.room_id == expired["room_id"]
            ).first()
            row.expires_at = datetime.now() - timedelta(days=1)

        snapshot = get_dashboard_snapshot(
            dashboard_db["session1"],
            dashboard_db["user1"],
            session_token="user-1",
            actor_role="user",
        )
        assert snapshot.upcoming_invitations == []

    def test_dashboard_sections_render_and_quick_actions_remain(self, dashboard_db, monkeypatch):
        from services import knowledge_base
        from services.auth_service import get_account
        from streamlit.testing.v1 import AppTest

        _seed_interview_record(dashboard_db["session1"], score=8.0)
        _seed_notification(dashboard_db["session1"], dashboard_db["user1"], "dash-ui-notice")
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

        app.switch_page("frontend/user/dashboard.py").run()
        assert not app.exception, [str(error.value) for error in app.exception]
        headings = [node.value for node in app.subheader]
        for title in (
            "Performance overview",
            "Continue preparation",
            "Recent activity",
            "AI recommendations",
            "Upcoming activities",
        ):
            assert title in headings, (title, headings)
        labels = [button.label for button in app.button]
        for label in (
            "Start interview",
            "Continue mock interview",
            "Resume analysis",
            "Job preparation",
            "Continue learning",
            "Open history",
            "Open invitations",
            "Open notifications",
        ):
            assert label in labels, (label, labels)
