"""Personalized study plan generation, tracking, assessments, and recommendations."""

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any

from database import Assessment, LearningRoadmap, LearningTask, get_db_context
from pydantic import BaseModel, Field, model_validator
from services.auth_service import require_owned_session_id
from services.llm_service import LLMService, ProviderManager, _redact, get_llm_service
from sqlalchemy.orm import selectinload

logger = logging.getLogger(__name__)


class RoadmapTask(BaseModel):
    title: str
    description: str = ""
    topic: str = ""
    week_number: int | None = None
    day_number: int | None = None
    task_type: str = "daily"
    objective: str = ""
    estimated_minutes: int = 30
    priority: str = "medium"
    resources: list[str] = Field(default_factory=list)
    practice_questions: list[str] = Field(default_factory=list)


class RoadmapPlan(BaseModel):
    title: str
    tasks: list[RoadmapTask] = Field(..., min_length=1)


class SkillGap(BaseModel):
    strong_skills: list[str] = Field(default_factory=list)
    focus_skills: list[str] = Field(default_factory=list)
    notes: str = ""


class RoadmapPhase(BaseModel):
    title: str
    week_start: int
    week_end: int
    goals: list[str] = Field(default_factory=list)
    outcomes: list[str] = Field(default_factory=list)


class RoadmapWeek(BaseModel):
    week_number: int
    title: str = ""
    topics: list[str] = Field(default_factory=list)
    milestone: str = ""
    weekly_goal: str = ""
    assignments: list[str] = Field(default_factory=list)


class RoadmapBlueprint(BaseModel):
    title: str
    summary: str = ""
    skill_gap: SkillGap = Field(default_factory=SkillGap)
    phases: list[RoadmapPhase] = Field(..., min_length=1)
    weeks: list[RoadmapWeek] = Field(..., min_length=1)
    recommendations: list[str] = Field(default_factory=list)


class WeeklyTaskPlan(BaseModel):
    week_number: int
    tasks: list[RoadmapTask] = Field(..., min_length=1)


class AssessmentQuestion(BaseModel):
    prompt: str
    options: list[str] = Field(..., min_length=2, max_length=4)
    correct_index: int = Field(..., ge=0)
    explanation: str = ""

    @model_validator(mode="after")
    def _validate_options(self) -> "AssessmentQuestion":
        if self.correct_index >= len(self.options):
            raise ValueError("correct_index must point to one of the provided options")
        return self


class AssessmentBundle(BaseModel):
    title: str
    questions: list[AssessmentQuestion] = Field(..., min_length=1, max_length=6)


class RecommendationPlan(BaseModel):
    recommendations: list[str] = Field(..., min_length=1)


ROADMAP_PROMPT = """Create a personalized, structured study roadmap for an interview candidate.

WEAK SKILLS: {weak_skills}
RECOMMENDED TOPICS: {topics}
TARGET ROLE: {role}
HOURS PER WEEK: {hours}

The plan must prepare the candidate for interviews, not just general study. Include:
- learning phases with start/end weeks, goals, and observable outcomes
- one week entry per requested week with topics, a milestone, a weekly goal, and assignments
- a skill gap summary with strong skills, focus skills, and practical notes
- initial recommendations that are specific to the target role

Use free, widely available documentation or well-known learning topics. Never invent URLs.
Return JSON with exactly these top-level fields:
{{
  "title": "short plan title",
  "summary": "two sentence plan summary",
  "skill_gap": {{
    "strong_skills": ["skill"],
    "focus_skills": ["skill"],
    "notes": "short assessment"
  }},
  "phases": [
    {{
      "title": "phase title",
      "week_start": 1,
      "week_end": 2,
      "goals": ["goal"],
      "outcomes": ["observable outcome"]
    }}
  ],
  "weeks": [
    {{
      "week_number": 1,
      "title": "week title",
      "topics": ["topic"],
      "milestone": "demonstrable milestone",
      "weekly_goal": "measurable weekly outcome",
      "assignments": ["assignment"]
    }}
  ],
  "recommendations": ["specific recommendation"]
}}"""

WEEK_TASKS_PROMPT = """Create daily study tasks for one week of an interview preparation plan.

ROADMAP: {roadmap_title}
WEEK: {week_number} — {week_title}
TOPICS: {topics}
MILESTONE: {milestone}
WEEKLY GOAL: {weekly_goal}
SKILL LEVEL: {skill_level}
HOURS PER DAY: {hours_per_day}
TARGET ROLE: {role}

Create 3 to 5 practical, ordered daily tasks. Each task must include:
- title, description, topic, objective, estimated_minutes, priority
- 1-2 concrete free resources or documentation topics
- 1-2 self-test practice questions

Return JSON with exactly these top-level fields:
{{
  "week_number": {week_number},
  "tasks": [
    {{
      "title": "short task title",
      "description": "specific action",
      "topic": "topic name",
      "day_number": 1,
      "task_type": "daily",
      "objective": "what the learner should be able to do",
      "estimated_minutes": 45,
      "priority": "medium",
      "resources": ["free resource or documentation topic"],
      "practice_questions": ["self-test question"]
    }}
  ]
}}"""

ASSESSMENT_PROMPT = """Create a focused interview-readiness assessment from saved study plan data.

ROADMAP: {title}
TOPICS TO ASSESS: {topics}
PREVIOUS ASSESSMENT TOPICS: {existing_titles}
QUESTION LIMIT: {max_questions}

Generate up to {max_questions} multiple-choice questions. Avoid repeating previous topics.
Each question needs a clear prompt, 2-4 options, a correct_index, and a short explanation.
Return JSON with exactly these top-level fields:
{{
  "title": "assessment title",
  "questions": [
    {{
      "prompt": "question text",
      "options": ["option one", "option two"],
      "correct_index": 0,
      "explanation": "why the answer is correct"
    }}
  ]
}}"""

RECOMMENDATIONS_PROMPT = """Recommend next actions from saved study plan progress. Do not invent progress data.

ROADMAP: {title}
PROGRESS: {progress}
EXISTING RECOMMENDATIONS: {existing}

Return 2-4 concise, actionable recommendations focused on interview readiness.
Return JSON with exactly this top-level field:
{{
  "recommendations": ["recommendation"]
}}"""


def _clean_list(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value).strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        result.append(text)
    return result[:20]


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return min(max(value, minimum), maximum)


def _normalize_level(value: str) -> str:
    level = (value or "").strip().lower()
    if level in {"beginner", "intermediate", "advanced"}:
        return level
    return "intermediate"


def _task_key(task: LearningTask | RoadmapTask | dict) -> str:
    if isinstance(task, dict):
        title = str(task.get("title") or "")
        topic = str(task.get("topic") or "")
        task_type = str(task.get("task_type") or "daily")
    else:
        title = str(task.title or "")
        topic = str(task.topic or "")
        task_type = str(getattr(task, "task_type", "daily") or "daily")
    return re.sub(r"[^a-z0-9]+", " ", f"{task_type} {title} {topic}".lower()).strip()


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _llm_for_provider(provider_name: str | None) -> LLMService:
    requested = (provider_name or "auto").strip().lower()
    if requested in {"", "auto", "default"}:
        return get_llm_service()
    return LLMService(manager=ProviderManager(requested_provider=requested))


def describe_roadmap_error(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if "rate limit" in lowered or "429" in lowered or "quota" in lowered:
        return "The AI provider is rate-limited. Wait a moment and try again."
    if "authentication failed" in lowered or "api key" in lowered or "401" in lowered or "403" in lowered:
        return "The AI provider could not authenticate. Check the provider settings and try again."
    if any(
        token in lowered
        for token in (
            "timeout",
            "timed out",
            "deadline exceeded",
            "deadline_exceeded",
            "504",
            "connection",
            "service unavailable",
        )
    ):
        return "The AI provider is temporarily unavailable. Try again shortly."
    if "no llm providers configured" in lowered or "all providers failed" in lowered:
        return "No AI provider is available. Configure a provider and try again."
    if "invalid json" in lowered:
        return "The AI response could not be parsed. Nothing was saved; try again."
    if "empty response" in lowered:
        return "The AI provider returned an empty response. Nothing was saved; try again."
    if any(
        token in lowered
        for token in (
            "no roadmap tasks",
            "no roadmap weeks",
            "no daily tasks",
            "validation failed",
            "empty structured response",
        )
    ):
        return "The AI response did not match the study plan format. Nothing was saved; try again."
    if "database" in lowered or "sqlite" in lowered or "commit" in lowered:
        return "The study plan could not be saved. Existing plans were left unchanged."
    return _redact(text) or "An unexpected error occurred."


def _normalize_blueprint(blueprint: RoadmapBlueprint, duration_weeks: int, topics: list[str]) -> RoadmapBlueprint:
    phases: list[RoadmapPhase] = []
    for phase in blueprint.phases:
        start = int(_clamp(int(phase.week_start or 1), 1, duration_weeks))
        end = int(_clamp(int(phase.week_end or start), start, duration_weeks))
        phases.append(
            RoadmapPhase(
                title=phase.title.strip() or f"Phase {len(phases) + 1}",
                week_start=start,
                week_end=end,
                goals=_clean_list(phase.goals)[:6],
                outcomes=_clean_list(phase.outcomes)[:6],
            )
        )
    if not phases:
        phases.append(
            RoadmapPhase(
                title="Interview readiness",
                week_start=1,
                week_end=duration_weeks,
                goals=_clean_list(topics)[:6],
                outcomes=[blueprint.title],
            )
        )

    weeks_by_number: dict[int, RoadmapWeek] = {}
    for index, week in enumerate(sorted(blueprint.weeks, key=lambda item: item.week_number)):
        number = int(_clamp(int(week.week_number or index + 1), 1, duration_weeks))
        if number in weeks_by_number:
            continue
        weeks_by_number[number] = RoadmapWeek(
            week_number=number,
            title=(week.title or f"Week {number}").strip(),
            topics=_clean_list(week.topics)[:8],
            milestone=(week.milestone or "").strip(),
            weekly_goal=(week.weekly_goal or "").strip(),
            assignments=_clean_list(week.assignments)[:6],
        )
    for number in range(1, duration_weeks + 1):
        if number not in weeks_by_number:
            phase = next(
                (item for item in phases if item.week_start <= number <= item.week_end),
                phases[0],
            )
            weeks_by_number[number] = RoadmapWeek(
                week_number=number,
                title=f"Week {number}: {phase.title}",
                topics=_clean_list(topics)[:5],
                milestone=f"Complete the {phase.title.lower()} practice goal.",
                weekly_goal="Finish the week's daily tasks and self-tests.",
                assignments=[],
            )

    return RoadmapBlueprint(
        title=blueprint.title.strip() or "Interview preparation roadmap",
        summary=(blueprint.summary or "").strip(),
        skill_gap=blueprint.skill_gap,
        phases=phases,
        weeks=[weeks_by_number[number] for number in range(1, duration_weeks + 1)],
        recommendations=_clean_list(blueprint.recommendations)[:6],
    )


def _generate_week_tasks(
    llm: LLMService,
    week: RoadmapWeek,
    *,
    roadmap_title: str,
    role: str,
    skill_level: str,
    hours_per_day: float,
) -> list[RoadmapTask]:
    prompt = WEEK_TASKS_PROMPT.format(
        roadmap_title=roadmap_title,
        week_number=week.week_number,
        week_title=week.title,
        topics=", ".join(week.topics) or "Not specified",
        milestone=week.milestone or "Not specified",
        weekly_goal=week.weekly_goal or "Not specified",
        skill_level=skill_level,
        hours_per_day=hours_per_day,
        role=role or "Not specified",
    )
    plan = llm.generate_structured(prompt, WeeklyTaskPlan, max_tokens=2600)
    tasks = [task for task in plan.tasks if task.title.strip()]
    if not tasks:
        raise RuntimeError(f"The model returned no daily tasks for week {week.week_number}.")

    normalized: list[RoadmapTask] = []
    for index, task in enumerate(tasks[:5], start=1):
        normalized.append(
            RoadmapTask(
                title=task.title.strip()[:255],
                description=(task.description or task.objective or "").strip(),
                topic=(task.topic or (week.topics[0] if week.topics else "")).strip()[:120],
                week_number=week.week_number,
                day_number=int(_clamp(int(task.day_number or index), 1, 7)),
                task_type="daily",
                objective=(task.objective or task.description or "").strip()[:500],
                estimated_minutes=int(_clamp(int(task.estimated_minutes or 30), 10, 240)),
                priority=(task.priority or "medium").strip().lower()[:20],
                resources=_clean_list(task.resources)[:3],
                practice_questions=_clean_list(task.practice_questions)[:3],
            )
        )
    return normalized


def generate_roadmap(
    session_id: int,
    weak_skills: list[str],
    topics: list[str],
    role: str = "",
    hours_per_week: int = 6,
    source_interview_id: int | None = None,
    actor_user_id: int | None = None,
    *,
    career_target: str = "",
    existing_skills: list[str] | None = None,
    skill_level: str = "intermediate",
    hours_per_day: float | None = None,
    duration_weeks: int | None = None,
    provider_name: str = "auto",
    replace_roadmap_id: int | None = None,
    preserve_progress: bool = True,
) -> LearningRoadmap:
    stage = "validation"
    try:
        require_owned_session_id(session_id, actor_user_id)
        weak_skills = _clean_list(weak_skills)[:10]
        topics = _clean_list(topics)[:10]
        existing_skills = _clean_list(existing_skills)[:15]
        target = (career_target or role or "interview readiness").strip()
        if not weak_skills and not topics and not target:
            raise ValueError("Complete an interview or provide focus skills first.")

        level = _normalize_level(skill_level)
        weeks_count = int(_clamp(int(duration_weeks or max(2, round(hours_per_week / 2))), 1, 12))
        day_hours = float(_clamp(float(hours_per_day or hours_per_week / 7), 0.25, 4.0))
        fingerprint = _fingerprint(
            {
                "session_id": session_id,
                "weak_skills": weak_skills,
                "topics": topics,
                "career_target": target,
                "existing_skills": existing_skills,
                "skill_level": level,
                "hours_per_day": day_hours,
                "duration_weeks": weeks_count,
            }
        )

        if replace_roadmap_id is None:
            with get_db_context() as db:
                existing = (
                    db.query(LearningRoadmap)
                    .options(selectinload(LearningRoadmap.tasks))
                    .filter(
                        LearningRoadmap.session_id == session_id,
                        LearningRoadmap.status == "active",
                        LearningRoadmap.generation_fingerprint == fingerprint,
                    )
                    .order_by(LearningRoadmap.created_at.desc())
                    .first()
                )
                if existing is not None:
                    return existing

        preserved: dict[str, tuple[str, datetime | None]] = {}
        if replace_roadmap_id is not None:
            with get_db_context() as db:
                old = (
                    db.query(LearningRoadmap)
                    .options(selectinload(LearningRoadmap.tasks))
                    .filter(
                        LearningRoadmap.id == replace_roadmap_id,
                        LearningRoadmap.session_id == session_id,
                    )
                    .first()
                )
                if old is None:
                    raise ValueError("The study plan to replace was not found.")
                if preserve_progress:
                    for task in old.tasks:
                        if task.status == "completed":
                            preserved[_task_key(task)] = (str(task.status), task.completed_at)

        prompt = ROADMAP_PROMPT.format(
            weak_skills=", ".join(weak_skills) or "Not specified",
            topics=", ".join(topics) or "Not specified",
            role=target,
            hours=max(1, round(day_hours * 7)),
        )
        stage = "generation"
        llm = _llm_for_provider(provider_name)
        blueprint = llm.generate_structured(prompt, RoadmapBlueprint, max_tokens=4000)
        blueprint = _normalize_blueprint(blueprint, weeks_count, topics or weak_skills)
        week_tasks: dict[int, list[RoadmapTask]] = {}
        for week in blueprint.weeks:
            week_tasks[week.week_number] = _generate_week_tasks(
                llm,
                week,
                roadmap_title=blueprint.title,
                role=target,
                skill_level=level,
                hours_per_day=day_hours,
            )

        stage = "database"
        with get_db_context() as db:
            if replace_roadmap_id is not None:
                old = (
                    db.query(LearningRoadmap)
                    .filter(
                        LearningRoadmap.id == replace_roadmap_id,
                        LearningRoadmap.session_id == session_id,
                    )
                    .first()
                )
                if old is not None:
                    old.status = "archived"
                    old.completed_at = datetime.now()

            roadmap = LearningRoadmap(
                session_id=session_id,
                title=blueprint.title[:255],
                summary=blueprint.summary,
                focus_skills=_clean_list([*weak_skills, *topics, *existing_skills])[:15],
                source_interview_id=source_interview_id,
                career_target=target[:255],
                existing_skills=existing_skills,
                skill_level=level,
                hours_per_day=day_hours,
                duration_weeks=weeks_count,
                status="active",
                phases=[phase.model_dump() for phase in blueprint.phases],
                skill_gap=blueprint.skill_gap.model_dump(),
                recommendations=blueprint.recommendations,
                topic_mastery={"mastered": [], "needs_review": []},
                generation_fingerprint=fingerprint,
            )
            db.add(roadmap)
            db.flush()

            order_index = 0
            for week in blueprint.weeks:
                week_goal_key = _task_key(
                    {
                        "title": week.weekly_goal or week.title,
                        "topic": week.title,
                        "task_type": "weekly_goal",
                    }
                )
                week_status, week_completed_at = preserved.get(week_goal_key, ("pending", None))
                db.add(
                    LearningTask(
                        roadmap_id=roadmap.id,
                        order_index=order_index,
                        title=(week.weekly_goal or week.title)[:255],
                        description=week.milestone or week.title,
                        topic=week.title[:120],
                        task_type="weekly_goal",
                        week_number=week.week_number,
                        objective=" · ".join(week.topics)[:500],
                        estimated_minutes=int(day_hours * 60),
                        priority="high",
                        resources=week.assignments,
                        practice_questions=[],
                        status=week_status,
                        completed_at=week_completed_at,
                    )
                )
                order_index += 1
                for task in week_tasks[week.week_number]:
                    task_status, task_completed_at = preserved.get(_task_key(task), ("pending", None))
                    db.add(
                        LearningTask(
                            roadmap_id=roadmap.id,
                            order_index=order_index,
                            title=task.title,
                            description=task.description,
                            topic=task.topic,
                            task_type="daily",
                            week_number=task.week_number,
                            day_number=task.day_number,
                            objective=task.objective,
                            estimated_minutes=task.estimated_minutes,
                            priority=task.priority,
                            resources=task.resources,
                            practice_questions=task.practice_questions,
                            status=task_status,
                            completed_at=task_completed_at,
                        )
                    )
                    order_index += 1

            roadmap_id = int(roadmap.id)
            roadmap_title = str(roadmap.title)
            focus_skills = list(roadmap.focus_skills or [])

        stage = "activity"
        from services.learning_service import record_roadmap_created

        try:
            record_roadmap_created(
                roadmap_id, roadmap_title, focus_skills, session_id, actor_user_id
            )
        except Exception as exc:
            logger.warning(
                "roadmap.activity_failed",
                extra={
                    "event": "roadmap.activity_failed",
                    "stage": "activity",
                    "session_id": session_id,
                    "error_type": type(exc).__name__,
                    "error": _redact(str(exc)),
                },
            )
        with get_db_context() as db:
            return (
                db.query(LearningRoadmap)
                .options(selectinload(LearningRoadmap.tasks))
                .filter(LearningRoadmap.id == roadmap_id)
                .one()
            )
    except Exception as exc:
        logger.warning(
            "roadmap.generation_failed",
            extra={
                "event": "roadmap.generation_failed",
                "stage": stage,
                "session_id": session_id,
                "error_type": type(exc).__name__,
                "error": _redact(str(exc)),
            },
        )
        raise


def regenerate_roadmap(
    roadmap_id: int,
    *,
    session_id: int,
    actor_user_id: int | None,
    career_target: str = "",
    existing_skills: list[str] | None = None,
    skill_level: str = "intermediate",
    hours_per_day: float | None = None,
    duration_weeks: int | None = None,
    provider_name: str = "auto",
) -> LearningRoadmap:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        old = (
            db.query(LearningRoadmap)
            .options(selectinload(LearningRoadmap.tasks))
            .filter(
                LearningRoadmap.id == roadmap_id,
                LearningRoadmap.session_id == session_id,
            )
            .first()
        )
        if old is None:
            raise ValueError("The study plan to regenerate was not found.")
        weak_skills = list(old.focus_skills or [])
        topics = _clean_list(
            [task.topic for task in old.tasks if task.topic and task.task_type == "daily"]
        )
        role = str(old.career_target or "")
        source_interview_id = old.source_interview_id
        day_hours = float(old.hours_per_day or hours_per_day or 1.0)
        weeks_count = int(old.duration_weeks or duration_weeks or 4)
        level = str(old.skill_level or skill_level)

    return generate_roadmap(
        session_id,
        weak_skills,
        topics,
        role=role,
        hours_per_week=max(1, round(day_hours * 7)),
        source_interview_id=source_interview_id,
        actor_user_id=actor_user_id,
        career_target=career_target or role,
        existing_skills=existing_skills,
        skill_level=level,
        hours_per_day=day_hours,
        duration_weeks=weeks_count,
        provider_name=provider_name,
        replace_roadmap_id=roadmap_id,
    )


def list_roadmaps(session_id: int, actor_user_id: int | None) -> list[LearningRoadmap]:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        return (
            db.query(LearningRoadmap)
            .options(selectinload(LearningRoadmap.tasks))
            .filter(LearningRoadmap.session_id == session_id)
            .order_by(LearningRoadmap.created_at.desc())
            .all()
        )


def get_roadmap(
    roadmap_id: int, session_id: int, actor_user_id: int | None
) -> LearningRoadmap:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        roadmap = (
            db.query(LearningRoadmap)
            .options(selectinload(LearningRoadmap.tasks))
            .filter(
                LearningRoadmap.id == roadmap_id,
                LearningRoadmap.session_id == session_id,
            )
            .first()
        )
    if roadmap is None:
        raise ValueError("Study plan not found.")
    return roadmap


def set_roadmap_status(
    roadmap_id: int, status: str, session_id: int, actor_user_id: int | None
) -> LearningRoadmap:
    normalized = (status or "").strip().lower()
    if normalized not in {"active", "paused", "completed", "archived"}:
        raise ValueError("Unsupported study plan status.")
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        roadmap = (
            db.query(LearningRoadmap)
            .filter(
                LearningRoadmap.id == roadmap_id,
                LearningRoadmap.session_id == session_id,
            )
            .first()
        )
        if roadmap is None:
            raise ValueError("Study plan not found.")
        if roadmap.status == "archived" and normalized != "archived":
            raise ValueError("Archived study plans cannot be resumed.")
        roadmap.status = normalized
        roadmap_id_value = int(roadmap.id)
    return get_roadmap(roadmap_id_value, session_id, actor_user_id)


def update_roadmap_settings(
    roadmap_id: int,
    *,
    session_id: int,
    actor_user_id: int | None,
    career_target: str,
    existing_skills: list[str],
    skill_level: str,
    hours_per_day: float,
    duration_weeks: int,
) -> LearningRoadmap:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        roadmap = (
            db.query(LearningRoadmap)
            .filter(
                LearningRoadmap.id == roadmap_id,
                LearningRoadmap.session_id == session_id,
                LearningRoadmap.status != "archived",
            )
            .first()
        )
        if roadmap is None:
            raise ValueError("Study plan not found.")
        roadmap.career_target = (career_target or "").strip()[:255]
        roadmap.existing_skills = _clean_list(existing_skills)
        roadmap.skill_level = _normalize_level(skill_level)
        roadmap.hours_per_day = float(_clamp(float(hours_per_day or 1.0), 0.25, 4.0))
        roadmap.duration_weeks = int(_clamp(int(duration_weeks or 4), 1, 12))
        roadmap_id_value = int(roadmap.id)
    return get_roadmap(roadmap_id_value, session_id, actor_user_id)


def set_task_status(
    task_id: int, completed: bool, session_id: int, actor_user_id: int | None
) -> None:
    require_owned_session_id(session_id, actor_user_id)
    roadmap_id: int | None = None
    title = ""
    with get_db_context() as db:
        task = (
            db.query(LearningTask)
            .join(LearningRoadmap, LearningTask.roadmap_id == LearningRoadmap.id)
            .filter(LearningTask.id == task_id, LearningRoadmap.session_id == session_id)
            .first()
        )
        if not task:
            return
        task.status = "completed" if completed else "pending"
        task.completed_at = datetime.now() if completed else None
        roadmap_id = int(task.roadmap_id)
        title = str(task.title)
    if completed and roadmap_id is not None:
        from services.learning_service import record_task_completion

        record_task_completion(task_id, roadmap_id, title, session_id, actor_user_id)


def get_study_plan_progress(
    roadmap_id: int, session_id: int, actor_user_id: int | None
) -> dict:
    roadmap = get_roadmap(roadmap_id, session_id, actor_user_id)
    tasks = list(roadmap.tasks)
    daily_tasks = [task for task in tasks if task.task_type == "daily"]
    weekly_goals = [task for task in tasks if task.task_type == "weekly_goal"]
    completed_daily = sum(1 for task in daily_tasks if task.status == "completed")
    completed_goals = sum(1 for task in weekly_goals if task.status == "completed")
    total_tasks = len(tasks)
    completed_tasks = completed_daily + completed_goals
    completed_minutes = sum(
        int(task.estimated_minutes or 0)
        for task in tasks
        if task.status == "completed"
    )
    completion_dates = {
        task.completed_at.date().isoformat()
        for task in tasks
        if task.status == "completed" and task.completed_at
    }
    created_at = roadmap.created_at or datetime.now()
    elapsed_days = max(1, (datetime.now() - created_at).days + 1)
    consistency = min(100, round(len(completion_dates) / min(elapsed_days, 30) * 100))
    current_week = int(
        _clamp(((datetime.now() - created_at).days // 7) + 1, 1, roadmap.duration_weeks or 1)
    )
    phases = roadmap.phases or []
    current_phase = next(
        (
            phase
            for phase in phases
            if int(phase.get("week_start", 0)) <= current_week <= int(phase.get("week_end", 0))
        ),
        phases[0] if phases else None,
    )

    with get_db_context() as db:
        assessments = (
            db.query(Assessment)
            .filter(
                Assessment.roadmap_id == roadmap_id,
                Assessment.session_id == session_id,
                Assessment.status == "completed",
            )
            .all()
        )
    scored = [item for item in assessments if item.max_score]
    average_score = (
        round(sum(float(item.score) / float(item.max_score) for item in scored) / len(scored) * 100)
        if scored
        else None
    )

    weekly_breakdown = []
    for number in range(1, int(roadmap.duration_weeks or 1) + 1):
        week_tasks = [task for task in tasks if task.week_number == number]
        done = sum(1 for task in week_tasks if task.status == "completed")
        weekly_breakdown.append(
            {
                "week_number": number,
                "total": len(week_tasks),
                "completed": done,
                "percent": round(done / len(week_tasks) * 100) if week_tasks else 0,
            }
        )

    return {
        "roadmap_id": int(roadmap.id),
        "title": str(roadmap.title),
        "status": str(roadmap.status or "active"),
        "career_target": str(roadmap.career_target or ""),
        "duration_weeks": int(roadmap.duration_weeks or 1),
        "current_week": current_week,
        "current_phase": current_phase,
        "total_tasks": total_tasks,
        "completed_tasks": completed_tasks,
        "daily_tasks": len(daily_tasks),
        "completed_daily": completed_daily,
        "weekly_goals": len(weekly_goals),
        "completed_goals": completed_goals,
        "completion_percent": round(completed_tasks / total_tasks * 100) if total_tasks else 0,
        "study_hours": round(completed_minutes / 60, 1),
        "consistency_percent": consistency,
        "assessment_count": len(assessments),
        "average_assessment_percent": average_score,
        "mastery": roadmap.topic_mastery or {},
        "weekly_breakdown": weekly_breakdown,
    }


def generate_assessment(
    roadmap_id: int,
    *,
    session_id: int,
    actor_user_id: int | None,
    week_number: int | None = None,
    provider_name: str = "auto",
) -> Assessment:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        roadmap = (
            db.query(LearningRoadmap)
            .options(selectinload(LearningRoadmap.tasks))
            .filter(
                LearningRoadmap.id == roadmap_id,
                LearningRoadmap.session_id == session_id,
            )
            .first()
        )
        if roadmap is None:
            raise ValueError("Study plan not found.")
        candidates = [
            task
            for task in roadmap.tasks
            if task.task_type == "daily" and task.topic
            and (week_number is None or task.week_number == week_number)
        ]
        completed_topics = _clean_list(
            [task.topic for task in candidates if task.status == "completed"]
        )
        topics = completed_topics or _clean_list([task.topic for task in candidates])
        topics = topics or _clean_list(roadmap.focus_skills)
        if not topics:
            raise ValueError("Complete a study plan task before generating an assessment.")
        existing_titles = _clean_list(
            [
                item.topic
                for item in db.query(Assessment)
                .filter(
                    Assessment.roadmap_id == roadmap_id,
                    Assessment.session_id == session_id,
                )
                .all()
            ]
        )
        title = str(roadmap.title)

    prompt = ASSESSMENT_PROMPT.format(
        title=title,
        topics=", ".join(topics[:8]),
        existing_titles=", ".join(existing_titles[-8:]) or "None",
        max_questions=5,
    )
    llm = _llm_for_provider(provider_name)
    bundle = llm.generate_structured(prompt, AssessmentBundle, max_tokens=3000)
    questions = [question.model_dump() for question in bundle.questions[:5]]
    if not questions:
        raise RuntimeError("The model returned no assessment questions.")

    with get_db_context() as db:
        assessment = Assessment(
            session_id=session_id,
            roadmap_id=roadmap_id,
            week_number=week_number,
            topic=bundle.title.strip()[:120] or f"Week {week_number or 1} review",
            difficulty="medium",
            questions=questions,
            answers=[],
            feedback=[],
            score=0.0,
            max_score=float(len(questions)),
            status="in_progress",
            question_count=len(questions),
        )
        db.add(assessment)
        db.flush()
        assessment_id = int(assessment.id)
    return get_assessment(assessment_id, session_id, actor_user_id)


def get_assessment(
    assessment_id: int, session_id: int, actor_user_id: int | None
) -> Assessment:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        assessment = (
            db.query(Assessment)
            .filter(
                Assessment.id == assessment_id,
                Assessment.session_id == session_id,
            )
            .first()
        )
        if assessment is None:
            raise ValueError("Assessment not found.")
        db.refresh(assessment)
        return assessment


def list_assessments(
    roadmap_id: int, session_id: int, actor_user_id: int | None
) -> list[Assessment]:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        return (
            db.query(Assessment)
            .filter(
                Assessment.roadmap_id == roadmap_id,
                Assessment.session_id == session_id,
            )
            .order_by(Assessment.created_at.desc())
            .all()
        )


def submit_assessment(
    assessment_id: int,
    answers: list[int],
    session_id: int,
    actor_user_id: int | None,
) -> dict:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        assessment = (
            db.query(Assessment)
            .filter(
                Assessment.id == assessment_id,
                Assessment.session_id == session_id,
            )
            .first()
        )
        if assessment is None:
            raise ValueError("Assessment not found.")
        if assessment.status == "completed":
            return {
                "score": float(assessment.score or 0),
                "max_score": float(assessment.max_score or 0),
                "feedback": list(assessment.feedback or []),
                "mastered": bool(
                    (assessment.score or 0) / max(float(assessment.max_score or 1), 1) >= 0.7
                ),
            }

        questions = list(assessment.questions or [])
        recorded_answers = [int(answer) if isinstance(answer, int) else -1 for answer in answers]
        feedback = []
        correct = 0
        for index, question in enumerate(questions):
            chosen = recorded_answers[index] if index < len(recorded_answers) else -1
            expected = int(question.get("correct_index", -1))
            is_correct = chosen == expected
            correct += int(is_correct)
            feedback.append(
                {
                    "prompt": str(question.get("prompt") or ""),
                    "correct": is_correct,
                    "correct_answer": str(question.get("options", [])[expected])
                    if 0 <= expected < len(question.get("options", []))
                    else "",
                    "explanation": str(question.get("explanation") or ""),
                }
            )

        assessment.answers = recorded_answers[: len(questions)]
        assessment.feedback = feedback
        assessment.score = float(correct)
        assessment.max_score = float(len(questions))
        assessment.status = "completed"
        assessment.completed_at = datetime.now()

        mastered: bool
        if assessment.roadmap_id:
            roadmap = (
                db.query(LearningRoadmap)
                .filter(
                    LearningRoadmap.id == assessment.roadmap_id,
                    LearningRoadmap.session_id == session_id,
                )
                .first()
            )
            if roadmap is not None:
                mastery = dict(roadmap.topic_mastery or {})
                mastered_topics = set(mastery.get("mastered") or [])
                needs_review = set(mastery.get("needs_review") or [])
                topic = str(assessment.topic or "General review")
                percent = correct / max(len(questions), 1)
                mastered = percent >= 0.7
                if mastered:
                    mastered_topics.add(topic)
                    needs_review.discard(topic)
                else:
                    needs_review.add(topic)
                    mastered_topics.discard(topic)
                roadmap.topic_mastery = {
                    "mastered": sorted(mastered_topics),
                    "needs_review": sorted(needs_review),
                }
        else:
            mastered = correct / max(len(questions), 1) >= 0.7

        return {
            "score": float(correct),
            "max_score": float(len(questions)),
            "feedback": feedback,
            "mastered": mastered,
        }


def refresh_recommendations(
    roadmap_id: int,
    *,
    session_id: int,
    actor_user_id: int | None,
    provider_name: str = "auto",
) -> list[str]:
    progress = get_study_plan_progress(roadmap_id, session_id, actor_user_id)
    roadmap = get_roadmap(roadmap_id, session_id, actor_user_id)
    existing = _clean_list(roadmap.recommendations)
    prompt = RECOMMENDATIONS_PROMPT.format(
        title=roadmap.title,
        progress=json.dumps(progress, default=str),
        existing=", ".join(existing[-8:]) or "None",
    )
    llm = _llm_for_provider(provider_name)
    result = llm.generate_structured(prompt, RecommendationPlan, max_tokens=1200)
    seen = {item.lower() for item in existing}
    merged = list(existing)
    for item in _clean_list(result.recommendations):
        if item.lower() not in seen:
            merged.append(item)
            seen.add(item.lower())
    merged = merged[-8:]
    with get_db_context() as db:
        stored = (
            db.query(LearningRoadmap)
            .filter(
                LearningRoadmap.id == roadmap_id,
                LearningRoadmap.session_id == session_id,
            )
            .first()
        )
        if stored is None:
            raise ValueError("Study plan not found.")
        stored.recommendations = merged
    return merged
