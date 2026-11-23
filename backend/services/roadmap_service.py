"""Personalized learning roadmap generation and completion tracking."""

from datetime import datetime

from database import LearningRoadmap, LearningTask, get_db_context
from pydantic import BaseModel, Field
from services.auth_service import require_owned_session_id
from services.llm_service import get_llm_service


class RoadmapTask(BaseModel):
    title: str
    description: str = ""
    topic: str = ""
    resources: list[str] = Field(default_factory=list)
    practice_questions: list[str] = Field(default_factory=list)


class RoadmapPlan(BaseModel):
    title: str
    tasks: list[RoadmapTask] = Field(default_factory=list)


ROADMAP_PROMPT = """Create a personalized, actionable learning roadmap for an interview candidate.

WEAK SKILLS: {weak_skills}
RECOMMENDED TOPICS: {topics}
TARGET ROLE: {role}
HOURS PER WEEK: {hours}

Produce 5 to 8 ordered tasks. Each task needs:
- title (short), description (specific action), topic
- resources (2-3 concrete free learning resources or documentation topics)
- practice_questions (1-2 questions to self-test)

Prefer free, widely available resources. Do not invent links. Return JSON matching the schema."""


def generate_roadmap(
    session_id: int,
    weak_skills: list[str],
    topics: list[str],
    role: str = "",
    hours_per_week: int = 6,
    source_interview_id: int | None = None,
    actor_user_id: int | None = None,
) -> LearningRoadmap:
    require_owned_session_id(session_id, actor_user_id)
    if not weak_skills and not topics:
        raise ValueError("Complete an interview or provide focus skills first.")
    prompt = ROADMAP_PROMPT.format(
        weak_skills=", ".join(weak_skills) or "Not specified",
        topics=", ".join(topics) or "Not specified",
        role=role or "Not specified",
        hours=hours_per_week,
    )
    plan = get_llm_service().generate_structured(prompt, RoadmapPlan)
    if not plan.tasks:
        raise RuntimeError("The model returned no roadmap tasks.")

    with get_db_context() as db:
        roadmap = LearningRoadmap(
            session_id=session_id,
            title=plan.title or f"Interview readiness plan · {role or 'general'}",
            focus_skills=list(dict.fromkeys([*weak_skills, *topics]))[:15],
            source_interview_id=source_interview_id,
        )
        db.add(roadmap)
        db.flush()
        for index, task in enumerate(plan.tasks):
            db.add(
                LearningTask(
                    roadmap_id=roadmap.id,
                    order_index=index,
                    title=task.title,
                    description=task.description,
                    topic=task.topic,
                    resources=task.resources,
                    practice_questions=task.practice_questions,
                    status="pending",
                )
            )
        roadmap_id = int(roadmap.id)
        roadmap_title = str(roadmap.title)
        focus_skills = list(roadmap.focus_skills or [])
    from services.learning_service import record_roadmap_created

    record_roadmap_created(
        roadmap_id, roadmap_title, focus_skills, session_id, actor_user_id
    )
    return roadmap


def list_roadmaps(session_id: int, actor_user_id: int | None) -> list[LearningRoadmap]:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        return (
            db.query(LearningRoadmap)
            .filter(LearningRoadmap.session_id == session_id)
            .order_by(LearningRoadmap.created_at.desc())
            .all()
        )


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
