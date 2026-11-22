from datetime import datetime, timedelta
from pathlib import Path

import pytest
from services.answer_evaluator import RUBRIC_VERSION, AnswerEvaluation
from services.auth_service import AuthError
from services.interview_generator import Difficulty, InterviewQuestion, QuestionCategory
from services.report_generator import InterviewReport

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def interview_db(tmp_path):
    import database.db as db_module
    from config.settings import settings
    from database import User, UserSession
    from services import auth_service as auth

    original_database = settings.database_path
    original_vectors = settings.vector_store_path
    settings.database_path = str(tmp_path / "interview.db")
    settings.vector_store_path = str(tmp_path / "vectors")
    db_module.engine = None
    db_module.SessionLocal = None
    db_module.init_database()

    with db_module.get_db_context() as db:
        one = User(
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
        two = User(
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
        db.add_all([one, two])
        db.flush()
        session_one = UserSession(session_id="user-1", user_id=one.id, label="one")
        session_two = UserSession(session_id="user-2", user_id=two.id, label="two")
        db.add_all([session_one, session_two])
        db.flush()
        ids = {
            "user1": one.id,
            "user2": two.id,
            "session1": session_one.id,
            "session2": session_two.id,
        }

    yield ids

    db_module.close_database()
    db_module.SessionLocal = None
    settings.database_path = original_database
    settings.vector_store_path = original_vectors


def _question(number: int, text: str = "Explain caching trade-offs") -> InterviewQuestion:
    return InterviewQuestion(
        question_number=number,
        category=QuestionCategory.TECHNICAL,
        difficulty=Difficulty.MEDIUM,
        question_text=text,
        expected_key_points=["latency", "consistency"],
        follow_up_hints=["Discuss invalidation"],
    )


def _evaluation(score: float) -> AnswerEvaluation:
    return AnswerEvaluation(
        technical_accuracy=score,
        relevance=score,
        communication=score,
        confidence=score,
        completeness=score,
        problem_solving=score,
        structure=score,
        overall_score=score,
        strengths=["Clear explanation"],
        weaknesses=["Add a concrete example"],
        missing_points=["Invalidation strategy"],
        incorrect_points=[],
        suggested_answer="Describe a concrete cache invalidation approach.",
        ideal_answer="Explain trade-offs with an example.",
        communication_feedback="Concise answer.",
        technical_feedback="Cover consistency.",
        rubric_version=RUBRIC_VERSION,
    )


def _report(score: float) -> InterviewReport:
    return InterviewReport(
        overall_score=score,
        technical_score=score,
        communication_score=score,
        hr_score=score,
        problem_solving_score=score,
        strengths=["Clear explanations"],
        weaknesses=["Add examples"],
        frequently_missed_concepts=["Invalidation"],
        recommended_topics=["Caching"],
        recommended_projects=["Build a cache demo"],
        recommended_questions=["Explain cache stamps"],
    )


def _seed_completed(session_id: int, score: float | None, created: datetime) -> int:
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
            started_at=created,
            completed_at=created + timedelta(minutes=30),
            duration_seconds=1800,
            created_at=created,
        )
        db.add(interview)
        db.flush()
        db.add(
            InterviewQuestion(
                interview_session_id=interview.id,
                question_number=1,
                category="technical",
                difficulty="medium",
                question_text="Explain caching trade-offs",
                answer_text="Use a cache",
                answer_mode="text",
                overall_score=score,
                answered_at=created,
                evaluated_at=created,
            )
        )
        return int(interview.id)


class TestInterviewLifecycleService:
    def test_start_answer_progress_and_complete(self, interview_db):
        from services import interview_service

        interview_id, question_ids = interview_service.start_interview(
            session_token="user-1",
            actor_user_id=interview_db["user1"],
            questions=[_question(1)],
            interview_type="Technical Interview",
            difficulty="medium",
            answer_mode="text",
        )
        interview_service.save_answer(
            interview_id=interview_id,
            session_token="user-1",
            actor_user_id=interview_db["user1"],
            question_id=question_ids[0],
            answer="Use a cache with TTL",
            evaluation=_evaluation(8.0),
            answer_mode="text",
            current_question_index=1,
            total_questions=1,
        )
        interview_service.save_progress(
            interview_id=interview_id,
            session_token="user-1",
            actor_user_id=interview_db["user1"],
            current_question_index=1,
            total_questions=1,
        )
        interview_service.complete_interview(
            interview_id=interview_id,
            session_token="user-1",
            actor_user_id=interview_db["user1"],
            report=_report(8.0),
            current_question_index=1,
            total_questions=1,
        )

        detail = interview_service.get_interview_detail(
            interview_id=interview_id,
            session_token="user-1",
            actor_user_id=interview_db["user1"],
        )
        assert detail.session["status"] == "completed"
        assert detail.session["overall_score"] == 8.0
        assert detail.session["duration_seconds"] >= 0
        assert detail.questions[0]["answer"] == "Use a cache with TTL"
        assert detail.questions[0]["evaluation"] is not None
        history = interview_service.get_interview_history("user-1", interview_db["user1"])
        assert len(history) == 1

    def test_start_abandons_previous_active_interview(self, interview_db):
        from database import InterviewSession, get_db_context
        from services import interview_service

        first, _ = interview_service.start_interview(
            session_token="user-1",
            actor_user_id=interview_db["user1"],
            questions=[_question(1)],
            interview_type="Technical Interview",
            difficulty="medium",
        )
        second, _ = interview_service.start_interview(
            session_token="user-1",
            actor_user_id=interview_db["user1"],
            questions=[_question(1)],
            interview_type="Behavioral Interview",
            difficulty="hard",
        )
        active = interview_service.get_active_interview("user-1", interview_db["user1"])
        assert active is not None
        assert active.id == second
        with get_db_context() as db:
            statuses = {
                row.id: row.status
                for row in db.query(InterviewSession).filter(InterviewSession.session_id == interview_db["session1"]).all()
            }
        assert statuses[first] == "abandoned"
        assert statuses[second] == "in_progress"

    def test_follow_up_uses_stable_question_id(self, interview_db):
        from services import interview_service

        interview_id, question_ids = interview_service.start_interview(
            session_token="user-1",
            actor_user_id=interview_db["user1"],
            questions=[_question(1)],
            interview_type="Technical Interview",
            difficulty="medium",
        )
        follow_up_id = interview_service.add_follow_up_question(
            interview_id=interview_id,
            session_token="user-1",
            actor_user_id=interview_db["user1"],
            question=_question(2, "How would you invalidate this cache?"),
            question_number=2,
        )
        interview_service.save_answer(
            interview_id=interview_id,
            session_token="user-1",
            actor_user_id=interview_db["user1"],
            question_id=follow_up_id,
            answer="Use versioned keys",
            evaluation=_evaluation(7.0),
        )
        detail = interview_service.get_interview_detail(
            interview_id=interview_id,
            session_token="user-1",
            actor_user_id=interview_db["user1"],
        )
        assert len(detail.questions) == 2
        assert detail.questions[1]["answer"] == "Use versioned keys"
        assert question_ids[0] != follow_up_id

    def test_history_filters_and_zero_scores(self, interview_db):
        from services import interview_service

        now = datetime.now()
        _seed_completed(interview_db["session1"], 0.0, now)
        _seed_completed(interview_db["session1"], 9.0, now - timedelta(days=3))
        _seed_completed(interview_db["session2"], 10.0, now)

        filtered = interview_service.get_interview_history(
            "user-1", interview_db["user1"], minimum_score=1.0
        )
        assert len(filtered) == 1
        assert filtered[0]["Score"] == 9.0
        typed = interview_service.get_interview_history(
            "user-1", interview_db["user1"],             interview_type="Technical", status="completed"

        )
        assert len(typed) == 2
        foreign = interview_service.get_interview_history("user-2", interview_db["user2"])
        assert len(foreign) == 1

    def test_cross_user_access_is_rejected(self, interview_db):
        from services import interview_service

        interview_id, question_ids = interview_service.start_interview(
            session_token="user-1",
            actor_user_id=interview_db["user1"],
            questions=[_question(1)],
            interview_type="Technical Interview",
            difficulty="medium",
        )
        with pytest.raises(AuthError):
            interview_service.get_interview_detail(
                interview_id=interview_id,
                session_token="user-2",
                actor_user_id=interview_db["user2"],
            )
        with pytest.raises(AuthError):
            interview_service.save_answer(
                interview_id=interview_id,
                session_token="user-2",
                actor_user_id=interview_db["user2"],
                question_id=question_ids[0],
                answer="Foreign answer",
            )
        with pytest.raises(AuthError):
            interview_service.complete_interview(
                interview_id=interview_id,
                session_token="user-2",
                actor_user_id=interview_db["user2"],
                report=_report(9.0),
            )

    def test_foreign_token_cannot_open_workspace(self, interview_db):
        from services import interview_service

        with pytest.raises(AuthError):
            interview_service.get_active_interview("user-2", interview_db["user1"])


class TestInterviewPracticeUI:
    def _app(self, interview_db, monkeypatch):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30)
        app.session_state["db_initialized"] = True
        app.session_state["user_id"] = interview_db["user1"]
        app.session_state["user_session_id"] = "user-1"
        app.session_state["account"] = type(
            "Account",
            (),
            {
                "id": interview_db["user1"],
                "email": "one@example.com",
                "full_name": "User One",
                "role": "user",
                "account_status": "active",
                "is_active": True,
                "must_change_password": False,
            },
        )()
        app.session_state["must_change_password"] = False
        app.run()
        return app

    def test_history_page_renders_filters_and_details(self, interview_db):
        _seed_completed(interview_db["session1"], 7.5, datetime.now())
        app = self._app(interview_db, None)
        app.switch_page("frontend/user/interview_history.py").run()
        assert not app.exception, [str(item.value) for item in app.exception]
        labels = [button.label for button in app.button]
        assert "Open details" in labels

    def test_report_page_handles_missing_evaluation(self, interview_db):
        _seed_completed(interview_db["session1"], None, datetime.now())
        app = self._app(interview_db, None)
        app.switch_page("frontend/user/report_page.py").run()
        assert not app.exception, [str(item.value) for item in app.exception]
        text = "\n".join(str(item.value) for item in app.info)
        assert "No evaluation data for this interview" in text
