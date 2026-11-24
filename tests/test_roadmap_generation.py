import logging
from types import SimpleNamespace

import pytest
from database import LearningRoadmap, get_db_context
from pydantic import ValidationError
from services import llm_service, roadmap_service
from services.auth_service import hash_password
from services.llm_service import GroqProvider, LLMResponse, _generate_structured, is_retryable_error
from services.roadmap_service import (
    ROADMAP_PROMPT,
    AssessmentBundle,
    RoadmapBlueprint,
    RoadmapPhase,
    RoadmapPlan,
    RoadmapTask,
    RoadmapWeek,
    WeeklyTaskPlan,
    describe_roadmap_error,
    generate_roadmap,
    get_study_plan_progress,
    list_roadmaps,
    refresh_recommendations,
    regenerate_roadmap,
    set_task_status,
    submit_assessment,
)
from utils.helpers import extract_json_from_text

_TRUNCATED_RESPONSE = (
    '{"tasks":[{"title":"Python Basics","description":"Review fundamentals.",'
    '"topic":"Python","resources":[],"practice_questions":[]}],"title":"Study Plan"'
)
_VALID_RESPONSE = (
    '{"title":"Study Plan","tasks":[{"title":"Python Basics",'
    '"description":"Review fundamentals.","topic":"Python","resources":[],'
    '"practice_questions":[]}]}'
)


@pytest.fixture
def study_db(tmp_path):
    import database.db as db_module
    from config.settings import settings
    from database import User, UserSession

    original_database = settings.database_path
    original_vectors = settings.vector_store_path
    settings.database_path = str(tmp_path / "study.db")
    settings.vector_store_path = str(tmp_path / "vectors")
    db_module.engine = None
    db_module.SessionLocal = None
    db_module.init_database()

    with db_module.get_db_context() as db:
        user = User(
            email="study@example.com",
            username="study",
            full_name="Study User",
            password_hash=hash_password("Pass1234"),
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        db.add(user)
        db.flush()
        session = UserSession(session_id="study-1", user_id=user.id, label="study")
        db.add(session)
        db.flush()
        ids = {"user": int(user.id), "session": int(session.id)}

    yield ids

    db_module.close_database()
    db_module.SessionLocal = None
    settings.database_path = original_database
    settings.vector_store_path = original_vectors


class ScriptedLLM:
    def __init__(self, weeks: int = 2) -> None:
        self.weeks = weeks
        self.week_index = 1
        self.calls: list[dict] = []

    def generate_structured(self, prompt: str, response_model: type, **kwargs) -> object:
        self.calls.append(kwargs)
        if response_model is RoadmapBlueprint:
            return _blueprint(self.weeks)
        if response_model is WeeklyTaskPlan:
            number = min(self.week_index, self.weeks)
            self.week_index += 1
            return WeeklyTaskPlan(
                week_number=number,
                tasks=[
                    RoadmapTask(
                        title="Review Python basics",
                        description="Review functions and exceptions.",
                        topic="Python",
                        day_number=1,
                        objective="Write small Python utilities.",
                        estimated_minutes=45,
                        priority="medium",
                        resources=["Python documentation"],
                        practice_questions=["How do exceptions propagate?"],
                    ),
                    RoadmapTask(
                        title="Practice algorithms",
                        description="Solve two beginner problems.",
                        topic="Algorithms",
                        day_number=2,
                        objective="Explain your solution complexity.",
                        estimated_minutes=40,
                        priority="medium",
                        resources=["Algorithm practice problems"],
                        practice_questions=["What is the complexity of binary search?"],
                    ),
                ],
            )
        if response_model is AssessmentBundle:
            return AssessmentBundle(
                title="Python readiness check",
                questions=[
                    {
                        "prompt": "Which keyword defines a function?",
                        "options": ["def", "function", "lambda x", "class"],
                        "correct_index": 0,
                        "explanation": "Python uses def for named functions.",
                    },
                    {
                        "prompt": "Which structure handles runtime errors?",
                        "options": ["try/except", "for/else", "while", "import"],
                        "correct_index": 0,
                        "explanation": "try/except catches runtime errors.",
                    },
                ],
            )
        if response_model.__name__ == "RecommendationPlan":
            return SimpleNamespace(
                recommendations=["Practice system design", "Mock interview"]
            )
        raise AssertionError(f"Unexpected response model: {response_model}")


def _blueprint(weeks: int = 2) -> RoadmapBlueprint:
    return RoadmapBlueprint(
        title="Backend interview plan",
        summary="A focused plan for interview readiness.",
        skill_gap={
            "strong_skills": ["Python"],
            "focus_skills": ["Algorithms"],
            "notes": "Strengthen problem explanation.",
        },
        phases=[
            RoadmapPhase(
                title="Fundamentals",
                week_start=1,
                week_end=max(1, weeks),
                goals=["Review Python", "Practice algorithms"],
                outcomes=["Solve interview problems"],
            )
        ],
        weeks=[
            RoadmapWeek(
                week_number=number,
                title=f"Week {number}",
                topics=["Python", "Algorithms"],
                milestone="Complete timed practice.",
                weekly_goal=f"Finish week {number} practice",
                assignments=["Write two solutions"],
            )
            for number in range(1, weeks + 1)
        ],
        recommendations=["Practice system design"],
    )


def _patch_llm(monkeypatch: pytest.MonkeyPatch, llm: object) -> None:
    monkeypatch.setattr(roadmap_service, "get_llm_service", lambda: llm)


def test_roadmap_prompt_specifies_structured_fields() -> None:
    prompt = ROADMAP_PROMPT.format(
        weak_skills="Python", topics="Algorithms", role="Engineer", hours=6
    )

    for field in ('"title"', '"phases"', '"weeks"', '"skill_gap"', '"recommendations"'):
        assert field in prompt


def test_roadmap_plan_requires_tasks() -> None:
    with pytest.raises(ValidationError):
        RoadmapPlan.model_validate({"title": "Study Plan"})


def test_extract_json_rejects_nested_object_from_truncated_payload() -> None:
    assert extract_json_from_text(_TRUNCATED_RESPONSE) is None


def test_generate_structured_retries_after_truncated_payload() -> None:
    class Provider:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def generate(self, prompt: str, **kwargs) -> LLMResponse:
            self.calls.append(kwargs)
            content = _TRUNCATED_RESPONSE if len(self.calls) == 1 else _VALID_RESPONSE
            return LLMResponse(content=content, model="test", provider="test")

    provider = Provider()
    plan = _generate_structured(provider, "prompt", RoadmapPlan)

    assert [task.title for task in plan.tasks] == ["Python Basics"]
    assert len(provider.calls) == 2


def test_generate_roadmap_uses_blueprint_token_budget_and_logs_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, study_db
) -> None:
    captured: dict = {}

    class FailingLLM:
        def generate_structured(self, prompt: str, response_model: type, **kwargs) -> object:
            captured.update(kwargs)
            raise RuntimeError("Validation failed: incomplete plan")

    _patch_llm(monkeypatch, FailingLLM())

    with (
        caplog.at_level(logging.WARNING, logger="services.roadmap_service"),
        pytest.raises(RuntimeError),
    ):
        generate_roadmap(
            study_db["session"],
            ["Python"],
            [],
            actor_user_id=study_db["user"],
            duration_weeks=1,
        )

    assert captured["max_tokens"] == 4000
    assert "roadmap.generation_failed" in caplog.text
    assert vars(caplog.records[0])["stage"] == "generation"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Groq request failed: HTTP 429: quota exceeded", "rate-limited"),
        ("Gemini authentication failed", "could not authenticate"),
        ("All providers failed: timeout", "temporarily unavailable"),
        ("Gemini 504 DEADLINE_EXCEEDED", "temporarily unavailable"),
        ("LLM returned invalid JSON (attempt 1).", "could not be parsed"),
        ("Groq empty response", "empty response"),
        ("The model returned no roadmap tasks.", "did not match the study plan format"),
        ("Validation failed: missing weeks", "did not match the study plan format"),
        ("SQLite database error", "could not be saved"),
    ],
)
def test_describe_roadmap_error_gives_actionable_message(message: str, expected: str) -> None:
    assert expected in describe_roadmap_error(RuntimeError(message))


def test_generate_roadmap_persists_phases_goals_tasks_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, study_db
) -> None:
    _patch_llm(monkeypatch, ScriptedLLM(weeks=2))

    first = generate_roadmap(
        study_db["session"],
        ["Python"],
        ["Algorithms"],
        role="Backend Engineer",
        actor_user_id=study_db["user"],
        career_target="Backend Engineer",
        existing_skills=["Git"],
        skill_level="intermediate",
        hours_per_day=1.0,
        duration_weeks=2,
    )
    second = generate_roadmap(
        study_db["session"],
        ["Python"],
        ["Algorithms"],
        role="Backend Engineer",
        actor_user_id=study_db["user"],
        career_target="Backend Engineer",
        existing_skills=["Git"],
        skill_level="intermediate",
        hours_per_day=1.0,
        duration_weeks=2,
    )

    assert second.id == first.id
    assert first.status == "active"
    assert first.phases and first.phases[0]["title"] == "Fundamentals"
    assert first.skill_gap["focus_skills"] == ["Algorithms"]
    assert first.recommendations == ["Practice system design"]
    assert sum(1 for task in first.tasks if task.task_type == "weekly_goal") == 2
    assert sum(1 for task in first.tasks if task.task_type == "daily") == 4
    assert all(task.week_number in {1, 2} for task in first.tasks)

    progress = get_study_plan_progress(
        int(first.id), study_db["session"], study_db["user"]
    )
    assert progress["total_tasks"] == 6
    assert progress["weekly_goals"] == 2
    assert progress["daily_tasks"] == 4
    assert progress["completion_percent"] == 0


def test_generate_roadmap_keeps_saved_plan_when_activity_logging_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, study_db
) -> None:
    from services import learning_service

    _patch_llm(monkeypatch, ScriptedLLM(weeks=1))
    monkeypatch.setattr(
        learning_service,
        "record_roadmap_created",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("activity log failed")),
    )

    with caplog.at_level(logging.WARNING, logger="services.roadmap_service"):
        roadmap = generate_roadmap(
            study_db["session"],
            ["Python"],
            [],
            actor_user_id=study_db["user"],
            duration_weeks=1,
        )

    assert roadmap.id is not None
    assert "roadmap.activity_failed" in caplog.text
    assert list_roadmaps(study_db["session"], study_db["user"])


def test_regenerate_archives_old_plan_and_preserves_completed_tasks(
    monkeypatch: pytest.MonkeyPatch, study_db
) -> None:
    _patch_llm(monkeypatch, ScriptedLLM(weeks=1))
    original = generate_roadmap(
        study_db["session"],
        ["Python"],
        ["Algorithms"],
        actor_user_id=study_db["user"],
        career_target="Backend Engineer",
        duration_weeks=1,
    )
    first_daily = next(task for task in original.tasks if task.task_type == "daily")
    set_task_status(
        int(first_daily.id), True, study_db["session"], study_db["user"]
    )

    replacement = regenerate_roadmap(
        int(original.id),
        session_id=study_db["session"],
        actor_user_id=study_db["user"],
    )

    with get_db_context() as db:
        old = db.query(LearningRoadmap).filter(LearningRoadmap.id == original.id).one()
        assert old.status == "archived"

    assert replacement.id != original.id
    assert replacement.status == "active"
    assert sum(1 for task in replacement.tasks if task.status == "completed") >= 1


def test_assessment_uses_saved_topics_and_updates_mastery(
    monkeypatch: pytest.MonkeyPatch, study_db
) -> None:
    from services.roadmap_service import generate_assessment

    _patch_llm(monkeypatch, ScriptedLLM(weeks=1))
    roadmap = generate_roadmap(
        study_db["session"],
        ["Python"],
        ["Algorithms"],
        actor_user_id=study_db["user"],
        duration_weeks=1,
    )
    for task in roadmap.tasks:
        if task.task_type == "daily":
            set_task_status(int(task.id), True, study_db["session"], study_db["user"])

    assessment = generate_assessment(
        int(roadmap.id),
        session_id=study_db["session"],
        actor_user_id=study_db["user"],
        week_number=1,
    )
    result = submit_assessment(
        int(assessment.id), [0, 0], study_db["session"], study_db["user"]
    )

    assert result["score"] == 2
    assert result["max_score"] == 2
    assert result["mastered"] is True
    with get_db_context() as db:
        stored = db.query(LearningRoadmap).filter(LearningRoadmap.id == roadmap.id).one()
        assert str(assessment.topic) in stored.topic_mastery["mastered"]


def test_refresh_recommendations_deduplicates_existing_items(
    monkeypatch: pytest.MonkeyPatch, study_db
) -> None:
    _patch_llm(monkeypatch, ScriptedLLM(weeks=1))
    roadmap = generate_roadmap(
        study_db["session"],
        ["Python"],
        ["Algorithms"],
        actor_user_id=study_db["user"],
        duration_weeks=1,
    )
    recommendations = refresh_recommendations(
        int(roadmap.id), session_id=study_db["session"], actor_user_id=study_db["user"]
    )

    assert recommendations.count("Practice system design") == 1
    assert "Mock interview" in recommendations


def test_deadline_exceeded_is_retryable() -> None:
    assert is_retryable_error(RuntimeError("Gemini 504 DEADLINE_EXCEEDED"))


def test_http_provider_retries_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = GroqProvider("test-key", "test-model")

    def _response(content: str) -> SimpleNamespace:
        return SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {
                "choices": [{"message": {"content": content}}],
                "usage": None,
            },
        )

    responses = [_response(""), _response('{"ok": true}')]
    calls: list[object] = []

    def _fake_post(path: str, payload: dict) -> SimpleNamespace:
        calls.append(path)
        return responses.pop(0)

    provider._post = _fake_post
    monkeypatch.setattr(llm_service.time, "sleep", lambda seconds: None)

    result = provider.generate("prompt")

    assert result.content == '{"ok": true}'
    assert len(calls) == 2


def test_study_plan_page_renders_generation_form_without_saved_plan() -> None:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
from types import SimpleNamespace
from unittest.mock import patch
import streamlit as st
import frontend.user.roadmap_page as page

snapshot = SimpleNamespace(
    skill_gaps={"items": ["Python"], "weak_areas": [], "missing_skills": [], "job_title": "Engineer"},
    improvement_plan={"seven_day": "", "thirty_day": ""},
    interview_history_summary={},
)
st.session_state["user_session_id"] = "token"
st.session_state["user_id"] = 1
with (
    patch.object(page, "owned_session_id", lambda *args: 1),
    patch.object(page, "list_roadmaps", lambda *args: []),
    patch.object(page, "get_dashboard_snapshot", lambda *args: snapshot),
):
    page.show_roadmap_page(embedded=True)
"""
    ).run()

    assert not app.exception
    assert any("Create your study plan" in element.value for element in app.subheader)
    assert any("Generate roadmap" in element.label for element in app.button)


def test_study_plan_page_renders_all_saved_plan_sections() -> None:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
from types import SimpleNamespace
from unittest.mock import patch
import streamlit as st
import frontend.user.roadmap_page as page

task = SimpleNamespace(
    id=11,
    title="Review Python basics",
    description="Review functions.",
    topic="Python",
    task_type="daily",
    week_number=1,
    day_number=1,
    objective="Write small utilities.",
    estimated_minutes=45,
    priority="medium",
    status="pending",
    resources=["Python documentation"],
    practice_questions=["How do exceptions propagate?"],
)
roadmap = SimpleNamespace(
    id=7,
    title="Backend interview plan",
    summary="A focused plan.",
    status="active",
    created_at=None,
    phases=[{"title": "Fundamentals", "week_start": 1, "week_end": 1, "goals": ["Python"], "outcomes": ["Practice"]}],
    skill_gap={"strong_skills": ["Python"], "focus_skills": ["Algorithms"], "notes": "Practice explanations."},
    recommendations=["Practice system design"],
    focus_skills=["Python", "Algorithms"],
    tasks=[task],
    career_target="Backend Engineer",
    duration_weeks=1,
    skill_level="intermediate",
    existing_skills=["Git"],
    hours_per_day=1.0,
)
progress = {
    "roadmap_id": 7,
    "title": "Backend interview plan",
    "status": "active",
    "career_target": "Backend Engineer",
    "duration_weeks": 1,
    "current_week": 1,
    "current_phase": {"title": "Fundamentals"},
    "total_tasks": 1,
    "completed_tasks": 0,
    "daily_tasks": 1,
    "completed_daily": 0,
    "weekly_goals": 0,
    "completed_goals": 0,
    "completion_percent": 0,
    "study_hours": 0.0,
    "consistency_percent": 0,
    "assessment_count": 0,
    "average_assessment_percent": None,
    "mastery": {"mastered": [], "needs_review": []},
    "weekly_breakdown": [{"week_number": 1, "total": 1, "completed": 0, "percent": 0}],
}
st.session_state["user_session_id"] = "token"
st.session_state["user_id"] = 1
with (
    patch.object(page, "owned_session_id", lambda *args: 1),
    patch.object(page, "list_roadmaps", lambda *args: [roadmap]),
    patch.object(page, "get_study_plan_progress", lambda *args: progress),
    patch.object(page, "list_assessments", lambda *args: []),
    patch.object(page, "set_task_status", lambda *args: None),
):
    page.show_roadmap_page(embedded=True)
"""
    ).run()

    assert not app.exception
    tab_labels = [element.label for element in app.tabs]
    for expected in (
        "Career Roadmap",
        "Daily Tasks",
        "Weekly Goals",
        "Skill Analysis",
        "Resources",
        "Progress Analytics",
        "AI Recommendations",
        "Weekly Assessment",
    ):
        assert expected in tab_labels
