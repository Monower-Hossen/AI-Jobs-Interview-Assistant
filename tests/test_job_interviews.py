from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ScriptedLLM:
    def __init__(
        self,
        *,
        score: float = 8.0,
        follow_up: bool = False,
        questions: list[str] | None = None,
        focus_skill: str = "Python",
        focus_skills: list[str] | None = None,
        fail_models: set[str] | None = None,
        blueprint_payload: dict | None = None,
    ):
        self.score = score
        self.follow_up = follow_up
        self.focus_skill = focus_skill
        self.focus_skills = focus_skills
        self.fail_models = fail_models or set()
        self.blueprint_payload = blueprint_payload
        self.questions = questions or [
            "How do you use Python and FastAPI to design a REST API?",
            "How do you secure Python FastAPI endpoints with dependency injection?",
            "How do you test Python FastAPI services in production?",
            "How do you monitor Python FastAPI latency and errors?",
        ]
        self.index = 0

    def generate_structured(self, prompt, response_model, **kwargs):
        name = response_model.__name__
        if name in self.fail_models:
            raise RuntimeError(f"Simulated provider failure for {name}")
        if name == "InterviewBlueprint":
            if self.blueprint_payload is not None:
                return response_model.model_validate(self.blueprint_payload)
            return response_model(
                target_role="Python Backend Engineer",
                company_context="Acme",
                job_summary="Build Python APIs",
                technical_topics=["Python", "FastAPI"],
                soft_skills=["Communication"],
                responsibility_themes=["Design APIs"],
                competency_matrix=[
                    {
                        "name": "Python",
                        "category": "Technical",
                        "importance": "high",
                        "evidence_to_look_for": "API design example",
                    }
                ],
                suggested_question_plan=[
                    {"category": "Technical", "focus": "Python", "difficulty": "medium"}
                ],
                evaluation_focus=["Python API design"],
            )
        if name == "AdaptiveQuestion":
            index = min(self.index, len(self.questions) - 1)
            text = self.questions[index]
            focus = (
                self.focus_skills[min(self.index, len(self.focus_skills) - 1)]
                if self.focus_skills
                else self.focus_skill
            )
            self.index += 1
            return response_model(
                question_number=1,
                category="Technical",
                difficulty="medium",
                question_text=text,
                expected_key_points=["routing", "validation"],
                focus_skill=focus,
            )
        if name == "AdaptiveEvaluation":
            return response_model(
                technical_accuracy=self.score,
                relevance=self.score,
                communication=self.score,
                confidence=self.score,
                completeness=self.score,
                problem_solving=self.score,
                structure=self.score,
                overall_score=self.score,
                strengths=["Clear API design explanation"],
                weaknesses=[],
                missing_points=[],
                incorrect_points=[],
                suggested_answer="Describe a concrete Python FastAPI design with validation.",
                ideal_answer="Explain routes, models, and tests.",
                communication_feedback="Well structured.",
                technical_feedback="Covered Python API concepts.",
                follow_up_needed=self.follow_up,
                follow_up_focus="Python FastAPI testing",
                knowledge_gaps=[],
                demonstrated_strengths=["Practical Python experience"],
                job_requirement_relevance=self.score,
            )
        if name == "ReportNarrative":
            return response_model(
                executive_summary="The candidate gave job-relevant Python answers.",
                readiness_level="job-ready",
                strengths=["Practical Python examples"],
                areas_to_improve=[],
                recommended_topics=["Python"],
                skill_coverage={},
                learning_plan=["Practice FastAPI testing"],
            )
        raise AssertionError(f"Unexpected response model: {name}")


LONG_ANSWER = (
    "In my last project I built a Python FastAPI service with pydantic request models, "
    "dependency injection for database sessions, and explicit status codes. I validated all "
    "inputs, wrote pytest tests for each route, and used structured logging to trace failures. "
    "I also documented the API and reviewed query performance with the team before release."
)


@pytest.fixture
def job_db(tmp_path):
    import database.db as db_module
    from config.settings import settings
    from database import User, UserSession
    from services import auth_service as auth

    original_database = settings.database_path
    original_vectors = settings.vector_store_path
    settings.database_path = str(tmp_path / "jobs.db")
    settings.vector_store_path = str(tmp_path / "vectors")
    db_module.engine = None
    db_module.SessionLocal = None
    db_module.init_database()

    with db_module.get_db_context() as db:
        admin = User(
            email="admin@jobs.com",
            username="admin",
            full_name="Job Admin",
            password_hash=auth.hash_password("Pass1234"),
            role="admin",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        user = User(
            email="user@jobs.com",
            username="user",
            full_name="Job User",
            password_hash=auth.hash_password("Pass1234"),
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        other = User(
            email="other@jobs.com",
            username="other",
            full_name="Other User",
            password_hash=auth.hash_password("Pass1234"),
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        db.add_all([admin, user, other])
        db.flush()
        sessions = [
            UserSession(session_id="job-token", user_id=user.id, label="job"),
            UserSession(session_id="other-token", user_id=other.id, label="other"),
        ]
        db.add_all(sessions)
        db.flush()
        ids = {
            "admin": int(admin.id),
            "user": int(user.id),
            "other": int(other.id),
            "token": "job-token",
            "other_token": "other-token",
        }

    yield ids

    db_module.close_database()
    db_module.SessionLocal = None
    settings.database_path = original_database
    settings.vector_store_path = original_vectors


def _job(admin_id: int, *, published: bool = True):
    from services.job_post_service import create_job_post

    return create_job_post(
        actor_admin_id=admin_id,
        title="Python Backend Engineer",
        company="Acme",
        description="Build and maintain Python APIs with FastAPI.",
        responsibilities=["Design APIs", "Write tests"],
        required_skills=["Python", "FastAPI"],
        preferred_skills=["PostgreSQL"],
        experience_level="Mid-level",
        interview_difficulty="medium",
        question_preferences=["Technical", "Behavioral"],
        is_published=published,
    )


def _start(job_db, job, *, llm=None, mode="text"):
    from services.adaptive_interview_engine import AdaptiveInterviewEngine
    from services.job_interview_service import start_job_interview

    ai = AdaptiveInterviewEngine(llm=llm or ScriptedLLM())
    state = start_job_interview(
        session_token=job_db["token"],
        actor_user_id=job_db["user"],
        job_id=int(job.id),
        mode=mode,
        difficulty="medium",
        duration_minutes=20,
        consent_given=True,
        engine=ai,
    )
    return state, ai


class TestJobPostManagement:
    def test_admin_can_create_update_publish_and_list(self, job_db):
        from services.job_post_service import (
            get_job_post,
            list_jobs,
            set_job_published,
            update_job_post,
        )

        job = _job(job_db["admin"], published=False)
        assert [item.id for item in list_jobs()] == []
        set_job_published(actor_admin_id=job_db["admin"], job_id=job.id, is_published=True)
        assert [item.id for item in list_jobs()] == [job.id]
        assert [item.id for item in list_jobs(skill="python")] == [job.id]
        assert [item.id for item in list_jobs(difficulty="medium")] == [job.id]
        updated = update_job_post(
            actor_admin_id=job_db["admin"],
            job_id=job.id,
            title="Senior Python Engineer",
            company="Acme",
            description="Lead Python API development.",
            required_skills=["Python"],
            is_published=True,
        )
        assert updated.title == "Senior Python Engineer"
        assert get_job_post(job.id).title == "Senior Python Engineer"

    def test_non_admin_cannot_manage_jobs(self, job_db):
        from services.auth_service import AuthError
        from services.job_post_service import create_job_post

        with pytest.raises(AuthError):
            create_job_post(
                actor_admin_id=job_db["user"],
                title="Hack",
                company="Hack",
                description="Hack",
            )

    def test_unpublished_jobs_are_hidden_from_users(self, job_db):
        from services.job_post_service import JobPostError, get_job_post

        job = _job(job_db["admin"], published=False)
        with pytest.raises(JobPostError):
            get_job_post(job.id, require_published=True)


class TestJobBlueprint:
    def test_partial_provider_payload_is_coerced(self):
        from services.job_blueprint import analyze_job_blueprint

        llm = ScriptedLLM(
            blueprint_payload={
                "title": "ML Engineer",
                "categories": ["HR", "Technical"],
                "required_skills": "Python, PyTorch",
                "responsibilities": ["Train models"],
            }
        )
        blueprint = analyze_job_blueprint({}, llm=llm)
        assert blueprint.target_role == "ML Engineer"
        assert blueprint.technical_topics == ["Python", "PyTorch"]
        assert [item.category for item in blueprint.suggested_question_plan] == [
            "HR",
            "Technical",
        ]

    def test_provider_failure_uses_job_grounded_blueprint(self):
        from services.job_blueprint import analyze_job_blueprint

        llm = ScriptedLLM(fail_models={"InterviewBlueprint"})
        blueprint = analyze_job_blueprint(
            {
                "title": "Data Engineer",
                "company": "Globex",
                "required_skills": ["SQL", "Airflow"],
                "responsibilities": ["Build pipelines"],
            },
            llm=llm,
        )
        assert blueprint.target_role == "Data Engineer"
        assert blueprint.company_context == "Globex"
        assert "SQL" in blueprint.technical_topics
        assert blueprint.suggested_question_plan
        assert all(item.focus for item in blueprint.suggested_question_plan)


class TestAdaptiveEngine:
    def test_question_is_grounded_and_retries_once(self):
        from services.adaptive_interview_engine import AdaptiveInterviewEngine, InterviewConfig
        from services.job_blueprint import InterviewBlueprint

        blueprint = InterviewBlueprint(
            target_role="Python Backend Engineer",
            technical_topics=["Python", "FastAPI"],
            responsibility_themes=["Design APIs"],
        )
        llm = ScriptedLLM(questions=["Tell me about your favourite film"], focus_skill="Cinema")
        engine = AdaptiveInterviewEngine(llm=llm)
        question = engine.next_question(blueprint, InterviewConfig(), [])
        assert "Python" in question.question_text
        assert llm.index == 2

        llm = ScriptedLLM(
            questions=["Tell me about your favourite film", "Describe Python FastAPI routing."],
            focus_skills=["Cinema", "Python"],
        )
        engine = AdaptiveInterviewEngine(llm=llm)
        question = engine.next_question(blueprint, InterviewConfig(), [])
        assert "Python" in question.question_text
        assert llm.index == 2

    def test_provider_failure_uses_deterministic_question_and_evaluation(self):
        from services.adaptive_interview_engine import AdaptiveInterviewEngine, InterviewConfig
        from services.job_blueprint import InterviewBlueprint

        blueprint = InterviewBlueprint(
            target_role="Python Backend Engineer",
            technical_topics=["Python"],
            responsibility_themes=["Design APIs"],
        )
        llm = ScriptedLLM(fail_models={"AdaptiveQuestion", "AdaptiveEvaluation"})
        engine = AdaptiveInterviewEngine(llm=llm)
        question = engine.next_question(blueprint, InterviewConfig(), [])
        assert "Python" in question.question_text
        evaluation = engine.evaluate_answer(question, LONG_ANSWER, blueprint, InterviewConfig())
        assert evaluation.overall_score > 0
        assert "Deterministic evidence evaluation" in evaluation.private_reasoning
        assert evaluation.rubric_version == "v2"

    def test_evaluation_adapts_difficulty_and_follow_up(self):
        from services.adaptive_interview_engine import AdaptiveInterviewEngine, InterviewConfig
        from services.job_blueprint import InterviewBlueprint

        blueprint = InterviewBlueprint(target_role="Python Engineer", technical_topics=["Python"])
        llm = ScriptedLLM(score=3.0, follow_up=True)
        engine = AdaptiveInterviewEngine(llm=llm)
        question = engine.next_question(blueprint, InterviewConfig(), [])
        evaluation = engine.evaluate_answer(question, LONG_ANSWER, blueprint, InterviewConfig())
        assert evaluation.next_difficulty == "easy"
        assert evaluation.follow_up_needed is True


class TestJobInterviewFlow:
    def test_start_survives_total_provider_failure(self, job_db):
        job = _job(job_db["admin"])
        llm = ScriptedLLM(
            fail_models={
                "InterviewBlueprint",
                "AdaptiveQuestion",
                "AdaptiveEvaluation",
                "ReportNarrative",
            }
        )
        state, _ = _start(job_db, job, llm=llm)
        assert state.status == "in_progress"
        assert state.current_question is not None
        assert "Python" in state.current_question.question_text
        assert state.blueprint.target_role == job.title

    def test_text_interview_persists_answers_and_report(self, job_db):
        from database import InterviewAnswer, InterviewEvaluation, InterviewSession
        from database.db import get_db_context
        from services.job_interview_service import (
            complete_job_interview,
            get_job_interview_report,
            submit_job_answer,
        )

        job = _job(job_db["admin"])
        state, ai = _start(job_db, job)
        assert state.current_question is not None
        assert "Python" in state.current_question.question_text
        number = state.current_question.question_number
        state = submit_job_answer(
            interview_id=state.interview_id,
            question_id=number,
            answer=LONG_ANSWER,
            media_id=None,
            actor_user_id=job_db["user"],
            session_token=job_db["token"],
            engine=ai,
        )
        assert state.answered_count == 1
        with get_db_context() as db:
            session = (
                db.query(InterviewSession).filter(InterviewSession.id == state.interview_id).one()
            )
            assert session.is_job_interview is True
            assert session.interview_mode == "text"
            assert session.blueprint_json is not None
            assert db.query(InterviewAnswer).filter_by(interview_session_id=session.id).count() == 1
            assert (
                db.query(InterviewEvaluation).filter_by(interview_session_id=session.id).count()
                == 1
            )
        report = complete_job_interview(
            interview_id=state.interview_id,
            actor_user_id=job_db["user"],
            session_token=job_db["token"],
            engine=ai,
        )
        assert report["questions_answered"] == 1
        assert report["overall_score"] > 0
        assert (
            get_job_interview_report(
                state.interview_id, actor_user_id=job_db["user"], session_token=job_db["token"]
            )["job_title"]
            == "Python Backend Engineer"
        )

    def test_answer_submission_is_atomic_when_next_question_persistence_fails(self, job_db, monkeypatch):
        import services.job_interview_service as service
        from database import InterviewAnswer, InterviewEvaluation, InterviewQuestion
        from database.db import get_db_context

        job = _job(job_db["admin"])
        state, ai = _start(job_db, job)
        number = int(state.current_question.question_number)

        def _fail_question_row(question, interview_id):
            raise RuntimeError("next question persistence failed")

        monkeypatch.setattr(service, "_question_to_row", _fail_question_row)
        with pytest.raises(RuntimeError, match="next question persistence failed"):
            service.submit_job_answer(
                interview_id=state.interview_id,
                question_id=number,
                answer=LONG_ANSWER,
                media_id=None,
                actor_user_id=job_db["user"],
                session_token=job_db["token"],
                engine=ai,
            )

        with get_db_context() as db:
            row = (
                db.query(InterviewQuestion)
                .filter_by(interview_session_id=state.interview_id, question_number=number)
                .one()
            )
            assert row.answer_text is None
            assert (
                db.query(InterviewAnswer)
                .filter_by(interview_session_id=state.interview_id)
                .count()
                == 0
            )
            assert (
                db.query(InterviewEvaluation)
                .filter_by(interview_session_id=state.interview_id)
                .count()
                == 0
            )

    def test_stale_question_and_malformed_answer_time_are_rejected(self, job_db):
        from services.job_interview_service import JobInterviewError, submit_job_answer

        job = _job(job_db["admin"])
        state, ai = _start(job_db, job)
        number = int(state.current_question.question_number)
        updated = submit_job_answer(
            interview_id=state.interview_id,
            question_id=number,
            answer=LONG_ANSWER,
            media_id=None,
            actor_user_id=job_db["user"],
            session_token=job_db["token"],
            engine=ai,
        )
        assert updated.answered_count == 1

        with pytest.raises(JobInterviewError, match="not the current question"):
            submit_job_answer(
                interview_id=state.interview_id,
                question_id=number,
                answer=LONG_ANSWER,
                media_id=None,
                actor_user_id=job_db["user"],
                session_token=job_db["token"],
                engine=ai,
            )
        with pytest.raises(JobInterviewError, match="Invalid answer start time"):
            submit_job_answer(
                interview_id=state.interview_id,
                question_id=int(updated.current_question.question_number),
                answer=LONG_ANSWER,
                media_id=None,
                actor_user_id=job_db["user"],
                session_token=job_db["token"],
                answer_started_at="not-a-date",
                engine=ai,
            )

    def test_completed_interview_is_idempotent_and_owner_scoped(self, job_db):
        from services.job_interview_service import (
            JobInterviewError,
            complete_job_interview,
            submit_job_answer,
        )

        job = _job(job_db["admin"])
        state, ai = _start(job_db, job)
        state = submit_job_answer(
            interview_id=state.interview_id,
            question_id=int(state.current_question.question_number),
            answer=LONG_ANSWER,
            media_id=None,
            actor_user_id=job_db["user"],
            session_token=job_db["token"],
            engine=ai,
        )
        first = complete_job_interview(
            interview_id=state.interview_id,
            actor_user_id=job_db["user"],
            session_token=job_db["token"],
            engine=ai,
        )
        second = complete_job_interview(
            interview_id=state.interview_id,
            actor_user_id=job_db["user"],
            session_token=job_db["token"],
            engine=ai,
        )
        assert second == first
        with pytest.raises(JobInterviewError, match="does not belong"):
            complete_job_interview(
                interview_id=state.interview_id,
                actor_user_id=job_db["other"],
                session_token=job_db["other_token"],
                engine=ai,
            )

    def test_follow_up_is_generated_after_weak_answer(self, job_db):
        from services.job_interview_service import submit_job_answer

        job = _job(job_db["admin"])
        state, ai = _start(job_db, job, llm=ScriptedLLM(score=4.0, follow_up=True))
        number = state.current_question.question_number
        state = submit_job_answer(
            interview_id=state.interview_id,
            question_id=number,
            answer=LONG_ANSWER,
            media_id=None,
            actor_user_id=job_db["user"],
            session_token=job_db["token"],
            engine=ai,
        )
        follow_ups = [turn for turn in state.turns if turn.question.follow_up_of == number]
        assert len(follow_ups) == 1

    def test_evaluation_fields_are_persisted_on_question(self, job_db, monkeypatch):
        from database import InterviewQuestion
        from database.db import get_db_context
        from services.job_interview_service import submit_job_answer

        job = _job(job_db["admin"])
        state, ai = _start(job_db, job)
        number = int(state.current_question.question_number)
        original_evaluate = ai.evaluate_answer

        def evaluate_with_gaps(question, answer, blueprint, config):
            result = original_evaluate(question, answer, blueprint, config)
            result.knowledge_gaps = ["Missing latency metrics"]
            result.communication_feedback = "Structure the answer with a concrete example."
            return result

        monkeypatch.setattr(ai, "evaluate_answer", evaluate_with_gaps)
        submit_job_answer(
            interview_id=state.interview_id,
            question_id=number,
            answer=LONG_ANSWER,
            media_id=None,
            actor_user_id=job_db["user"],
            session_token=job_db["token"],
            engine=ai,
        )
        with get_db_context() as db:
            row = (
                db.query(InterviewQuestion)
                .filter_by(interview_session_id=state.interview_id, question_number=number)
                .first()
            )
            assert row is not None
            assert row.missing_points == ["Missing latency metrics"]
            assert row.communication_feedback == "Structure the answer with a concrete example."

    def test_target_question_count_is_stable_and_final_report_is_reachable(self, job_db):
        from database import InterviewSession
        from database.db import get_db_context
        from services.job_interview_service import complete_job_interview, submit_job_answer

        job = _job(job_db["admin"])
        state, ai = _start(job_db, job)
        target = int(state.config.target_questions)
        assert target >= 3
        for _ in range(target):
            assert state.current_question is not None
            state = submit_job_answer(
                interview_id=state.interview_id,
                question_id=int(state.current_question.question_number),
                answer=LONG_ANSWER,
                media_id=None,
                actor_user_id=job_db["user"],
                session_token=job_db["token"],
                engine=ai,
            )
        assert state.answered_count == target
        assert state.current_question is None
        with get_db_context() as db:
            row = (
                db.query(InterviewSession)
                .filter(InterviewSession.id == state.interview_id)
                .one()
            )
            assert int(row.total_questions) == target
        report = complete_job_interview(
            interview_id=state.interview_id,
            actor_user_id=job_db["user"],
            session_token=job_db["token"],
            engine=ai,
        )
        assert report["questions_answered"] == target

    def test_answer_time_limit_is_enforced(self, job_db):
        from services.job_interview_constants import MAX_ANSWER_SECONDS
        from services.job_interview_service import JobInterviewError, submit_job_answer

        job = _job(job_db["admin"])
        state, ai = _start(job_db, job)
        stale_start = datetime.now(UTC) - timedelta(seconds=MAX_ANSWER_SECONDS + 30)
        with pytest.raises(JobInterviewError, match="Answer time limit"):
            submit_job_answer(
                interview_id=state.interview_id,
                question_id=int(state.current_question.question_number),
                answer=LONG_ANSWER,
                media_id=None,
                actor_user_id=job_db["user"],
                session_token=job_db["token"],
                answer_started_at=stale_start.isoformat(),
                engine=ai,
            )
        state = submit_job_answer(
            interview_id=state.interview_id,
            question_id=int(state.current_question.question_number),
            answer=LONG_ANSWER,
            media_id=None,
            actor_user_id=job_db["user"],
            session_token=job_db["token"],
            answer_started_at=datetime.now(UTC).isoformat(),
            engine=ai,
        )
        assert state.answered_count == 1

    def test_other_user_cannot_access_interview(self, job_db):
        from services.job_interview_service import JobInterviewError, get_job_interview_state

        job = _job(job_db["admin"])
        state, _ = _start(job_db, job)
        with pytest.raises(JobInterviewError):
            get_job_interview_state(state.interview_id, job_db["other"], job_db["other_token"])

    def test_sqlite_schema_is_preserved_on_reinitialization(self, job_db):
        import database.db as db_module
        from database import InterviewSession

        job = _job(job_db["admin"])
        state, _ = _start(job_db, job)
        db_module.init_database()
        with db_module.get_db_context() as db:
            row = db.query(InterviewSession).filter(InterviewSession.id == state.interview_id).one()
            assert row.is_job_interview is True
            assert row.blueprint_json is not None


class TestInterviewMedia:
    def test_consent_and_owner_checks(self, job_db, monkeypatch):
        from services.interview_media import MediaError, read_media, save_interview_media

        monkeypatch.setattr(
            "services.interview_media.magic.from_buffer", lambda *args, **kwargs: "audio/wav"
        )
        job = _job(job_db["admin"])
        state, _ = _start(job_db, job)
        with pytest.raises(MediaError):
            save_interview_media(
                data=b"audio",
                filename="answer.wav",
                kind="audio",
                interview_id=state.interview_id,
                actor_user_id=job_db["user"],
                session_token=job_db["token"],
                consent_given=False,
            )
        record = save_interview_media(
            data=b"audio",
            filename="answer.wav",
            kind="audio",
            interview_id=state.interview_id,
            actor_user_id=job_db["user"],
            session_token=job_db["token"],
            consent_given=True,
        )
        payload, mime, kind = read_media(
            int(record.id), actor_user_id=job_db["user"], session_token=job_db["token"]
        )
        assert payload == b"audio" and kind == "audio" and mime == "audio/wav"
        with pytest.raises(MediaError):
            read_media(
                int(record.id), actor_user_id=job_db["other"], session_token=job_db["other_token"]
            )

    def test_video_audio_is_transcribed_and_attached(self, job_db, monkeypatch):
        from database import InterviewMedia
        from database.db import get_db_context
        from services.interview_media import save_interview_media, transcribe_media
        from services.job_interview_service import submit_job_answer

        monkeypatch.setattr(
            "services.interview_media.magic.from_buffer", lambda *args, **kwargs: "video/mp4"
        )

        class FakeAudio:
            def export(self, path, format):
                Path(path).write_bytes(b"wav")

        monkeypatch.setattr(
            "services.interview_media.AudioSegment.from_file", lambda *args, **kwargs: FakeAudio()
        )
        monkeypatch.setattr(
            "services.interview_media.transcribe_audio",
            lambda *args, **kwargs: SimpleNamespace(text=LONG_ANSWER),
        )
        job = _job(job_db["admin"])
        state, ai = _start(job_db, job, mode="video")
        record = save_interview_media(
            data=b"video",
            filename="answer.mp4",
            kind="video",
            interview_id=state.interview_id,
            actor_user_id=job_db["user"],
            session_token=job_db["token"],
            consent_given=True,
        )
        assert LONG_ANSWER in transcribe_media(
            int(record.id), actor_user_id=job_db["user"], session_token=job_db["token"]
        )
        state = submit_job_answer(
            interview_id=state.interview_id,
            question_id=int(state.current_question.question_number),
            answer=LONG_ANSWER,
            media_id=int(record.id),
            actor_user_id=job_db["user"],
            session_token=job_db["token"],
            engine=ai,
        )
        with get_db_context() as db:
            media = db.query(InterviewMedia).filter(InterviewMedia.id == record.id).one()
            assert media.answer_id is not None
            assert media.media_type == "video"
        assert state.answered_count == 1

    def test_voice_answer_uses_transcript(self, job_db, monkeypatch):
        from services.interview_media import save_interview_media, transcribe_media
        from services.job_interview_service import submit_job_answer

        monkeypatch.setattr(
            "services.interview_media.magic.from_buffer", lambda *args, **kwargs: "audio/wav"
        )
        monkeypatch.setattr(
            "services.interview_media.transcribe_audio",
            lambda *args, **kwargs: SimpleNamespace(text=LONG_ANSWER),
        )
        job = _job(job_db["admin"])
        state, ai = _start(job_db, job, mode="voice")
        record = save_interview_media(
            data=b"audio",
            filename="answer.wav",
            kind="audio",
            interview_id=state.interview_id,
            actor_user_id=job_db["user"],
            session_token=job_db["token"],
            consent_given=True,
        )
        transcript = transcribe_media(
            int(record.id), actor_user_id=job_db["user"], session_token=job_db["token"]
        )
        state = submit_job_answer(
            interview_id=state.interview_id,
            question_id=int(state.current_question.question_number),
            answer=transcript,
            media_id=int(record.id),
            actor_user_id=job_db["user"],
            session_token=job_db["token"],
            engine=ai,
        )
        assert state.turns[0].answer == LONG_ANSWER
        assert state.turns[0].media_id == int(record.id)


class TestPhaseOneRegression:
    def test_upload_fingerprint_tracks_file_identity(self):
        from frontend.user.job_interview import _upload_fingerprint

        class Upload:
            def __init__(self, *, file_id=None, data=b"video", name="answer.mp4"):
                self.file_id = file_id
                self.data = data
                self.name = name

            def getvalue(self):
                return self.data

        assert _upload_fingerprint(None) is None
        assert _upload_fingerprint(Upload(file_id="file-one")) == "file-one"
        assert _upload_fingerprint(Upload(file_id="file-two")) == "file-two"
        first = _upload_fingerprint(Upload(data=b"video-a"))
        second = _upload_fingerprint(Upload(data=b"video-b"))
        assert first and second and first != second

    def test_stale_upload_state_is_cleared(self):
        from frontend.user.job_interview import _sync_upload_state

        state = {"_media": 9, "_answer": "old transcript", "_fingerprint": "old-file"}
        changed = _sync_upload_state(
            state,
            media_key="_media",
            answer_key="_answer",
            fingerprint_key="_fingerprint",
            fingerprint="new-file",
        )
        assert changed is True
        assert "_media" not in state
        assert state["_answer"] == ""
        assert "_fingerprint" not in state

        unchanged = _sync_upload_state(
            state,
            media_key="_media",
            answer_key="_answer",
            fingerprint_key="_fingerprint",
            fingerprint=None,
        )
        assert unchanged is False
        assert "_media" not in state

        state = {"_fingerprint": "same-file"}
        assert (
            _sync_upload_state(
                state,
                media_key="_media",
                answer_key="_answer",
                fingerprint_key="_fingerprint",
                fingerprint="same-file",
            )
            is False
        )

    def test_media_upload_requires_session_consent(self, monkeypatch):
        from services.interview_media import MediaError

        import frontend.user.job_interview as job_ui

        calls = []

        def fake_save(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(id=42)

        monkeypatch.setattr(job_ui, "save_interview_media", fake_save)
        monkeypatch.setattr(
            job_ui,
            "transcribe_media",
            lambda *args, **kwargs: "Recorded answer.",
        )
        with pytest.raises(MediaError, match="consent"):
            job_ui._process_media_upload(
                data=b"video",
                filename="answer.mp4",
                kind="video",
                interview_id=1,
                user_id=2,
                session_token="token",
                consent_given=False,
            )
        assert calls == []
        media_id, text = job_ui._process_media_upload(
            data=b"video",
            filename="answer.mp4",
            kind="video",
            interview_id=1,
            user_id=2,
            session_token="token",
            consent_given=True,
        )
        assert media_id == 42 and text == "Recorded answer."
        assert calls[0]["consent_given"] is True

    def test_answer_time_remaining_is_clamped_at_zero(self):
        from services.job_interview_constants import MAX_ANSWER_SECONDS

        from frontend.user.job_interview import _answer_time_remaining

        stale = datetime.now(UTC) - timedelta(seconds=MAX_ANSWER_SECONDS + 30)
        assert _answer_time_remaining(stale) == 0
        assert 0 < _answer_time_remaining(datetime.now(UTC)) <= MAX_ANSWER_SECONDS


class TestNewPages:
    @pytest.mark.parametrize(
        ("page", "role"),
        [
            ("frontend/admin/jobs.py", "admin"),
            ("frontend/user/job_interview.py", "user"),
        ],
    )
    def test_new_pages_render(self, job_db, monkeypatch, page, role):
        from services import llm_service
        from services.auth_service import get_account
        from streamlit.testing.v1 import AppTest

        monkeypatch.setattr(llm_service, "check_provider_health", dict)
        monkeypatch.setattr(
            llm_service,
            "get_provider_status",
            lambda: {"active": "none", "providers": {}, "fallback_triggered": False},
        )
        _job(job_db["admin"])
        account = get_account(job_db[role])
        assert account is not None
        app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30)
        app.session_state["db_initialized"] = True
        app.session_state["user_id"] = job_db[role]
        app.session_state["user_session_id"] = "job-token" if role == "user" else "admin-token"
        app.session_state["account"] = account
        app.session_state["must_change_password"] = False
        app.run()
        app.switch_page(page).run()
        assert not app.exception, [str(error.value) for error in app.exception]
