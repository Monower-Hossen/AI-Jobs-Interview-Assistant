from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from services.job_interview_constants import INTERVIEW_CATEGORIES
from services.job_post_service import store_blueprint
from services.llm_service import get_llm_service

logger = logging.getLogger(__name__)


class BlueprintError(RuntimeError):
    """Raised when the job blueprint cannot be created."""


class BlueprintCompetency(BaseModel):
    name: str
    category: str = "Technical"
    importance: str = "high"
    evidence_to_look_for: str = ""


class QuestionPlan(BaseModel):
    category: str
    focus: str = ""
    difficulty: str = "medium"

    @field_validator("focus")
    @classmethod
    def _default_focus(cls, value: str, info: Any) -> str:
        text = str(value or "").strip()
        return text or str(info.data.get("category") or "job requirements").strip()

    @field_validator("category")
    @classmethod
    def _validate_category(cls, value: str) -> str:
        normalized = str(value).strip()
        lookup = {item.lower(): item for item in INTERVIEW_CATEGORIES}
        if normalized.lower() not in lookup:
            return "Technical"
        return lookup[normalized.lower()]

    @field_validator("difficulty")
    @classmethod
    def _validate_difficulty(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        return normalized if normalized in {"easy", "medium", "hard"} else "medium"


class InterviewBlueprint(BaseModel):
    target_role: str = ""
    company_context: str = ""
    job_summary: str = ""
    seniority: str = "mid-level"
    technical_topics: list[str] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)
    responsibility_themes: list[str] = Field(default_factory=list)
    competency_matrix: list[BlueprintCompetency] = Field(default_factory=list)
    suggested_question_plan: list[QuestionPlan] = Field(default_factory=list)
    evaluation_focus: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_provider_output(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        aliases = {
            "target_role": ("title", "role", "job_title"),
            "company_context": ("company", "company_name"),
            "job_summary": ("description", "summary"),
            "technical_topics": ("required_skills", "skills", "technical_skills"),
            "responsibility_themes": ("responsibilities", "key_responsibilities"),
            "evaluation_focus": ("evaluation_criteria", "focus_areas"),
            "suggested_question_plan": ("question_plan", "categories"),
        }
        for field, names in aliases.items():
            if values.get(field) in (None, "", []):
                for name in names:
                    if values.get(name) not in (None, "", []):
                        values[field] = values[name]
                        break
        for field in (
            "technical_topics",
            "soft_skills",
            "responsibility_themes",
            "evaluation_focus",
            "red_flags",
        ):
            value = values.get(field)
            if isinstance(value, str):
                values[field] = [part.strip() for part in re.split(r"[,\n]", value) if part.strip()]
        plan = values.get("suggested_question_plan")
        if isinstance(plan, list):
            fixed_plan: list[Any] = []
            for item in plan:
                if isinstance(item, str):
                    fixed_plan.append({"category": item, "focus": item})
                elif isinstance(item, dict):
                    entry = dict(item)
                    if not str(entry.get("focus") or "").strip():
                        entry["focus"] = str(entry.get("category") or "job requirements")
                    fixed_plan.append(entry)
                else:
                    fixed_plan.append(item)
            values["suggested_question_plan"] = fixed_plan
        competencies = values.get("competency_matrix")
        if isinstance(competencies, list):
            fixed_competencies: list[Any] = []
            for item in competencies:
                if isinstance(item, str):
                    fixed_competencies.append({"name": item})
                elif isinstance(item, dict):
                    entry = dict(item)
                    if not str(entry.get("name") or "").strip():
                        entry["name"] = str(entry.get("category") or "job requirement")
                    fixed_competencies.append(entry)
                else:
                    fixed_competencies.append(item)
            values["competency_matrix"] = fixed_competencies
        return values

    @field_validator("technical_topics", "soft_skills", "responsibility_themes", "evaluation_focus")
    @classmethod
    def _clean_strings(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                cleaned.append(text)
        return cleaned[:20]

    @field_validator("suggested_question_plan")
    @classmethod
    def _limit_plan(cls, value: list[QuestionPlan]) -> list[QuestionPlan]:
        return value[:12]

    @property
    def all_focus_terms(self) -> list[str]:
        terms = [
            *self.technical_topics,
            *self.soft_skills,
            *self.responsibility_themes,
            *[item.name for item in self.competency_matrix],
            self.target_role,
            self.seniority,
        ]
        seen: set[str] = set()
        result: list[str] = []
        for term in terms:
            key = term.lower().strip()
            if key and key not in seen:
                seen.add(key)
                result.append(term.strip())
        return result

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


BLUEPRINT_PROMPT = """You are a hiring manager and interview designer.

Build a structured interview blueprint for ONE job posting. Use only information
in the job posting and reasonable professional inference from it. Never invent
unrelated technologies, credentials, or responsibilities.

Rules:
- technical_topics and responsibility_themes must come from the job posting.
- soft_skills may include professional competencies implied by the role.
- suggested_question_plan must contain only these categories: {categories}.
- Focus question areas on the job's actual skills, seniority, and responsibilities.
- evaluation_focus must describe observable evidence, never personality or emotion claims.
- Return strict JSON matching the requested schema.

Job posting:
{job_json}
"""


def _job_to_dict(job: Any) -> dict[str, Any]:
    if isinstance(job, dict):
        return job
    return {
        "title": getattr(job, "title", ""),
        "company": getattr(job, "company", ""),
        "description": getattr(job, "description", ""),
        "responsibilities": getattr(job, "responsibilities", None) or [],
        "required_skills": getattr(job, "required_skills", None) or [],
        "preferred_skills": getattr(job, "preferred_skills", None) or [],
        "experience_level": getattr(job, "experience_level", ""),
        "education": getattr(job, "education", ""),
        "interview_difficulty": getattr(job, "interview_difficulty", "medium"),
        "question_preferences": getattr(job, "question_preferences", None) or [],
    }


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,\n]", value)
    elif isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        return []
    result: list[str] = []
    for part in parts:
        text = " ".join(str(part).split())
        if text and text.lower() not in {item.lower() for item in result}:
            result.append(text)
    return result


def build_deterministic_blueprint(job_dict: dict[str, Any]) -> InterviewBlueprint:
    """Build a job-grounded blueprint without any LLM provider."""
    title = str(job_dict.get("title") or "Target role").strip()
    company = str(job_dict.get("company") or "").strip()
    description = str(job_dict.get("description") or "").strip()
    topics = _string_list(job_dict.get("required_skills")) + _string_list(
        job_dict.get("preferred_skills")
    )
    topics = list(dict.fromkeys(topics)) or _string_list(title)
    if not topics and description:
        topics = [description[:120]]
    responsibilities = _string_list(job_dict.get("responsibilities"))
    if not responsibilities and description:
        responsibilities = [
            part.strip() for part in re.split(r"[.\n]", description) if len(part.strip()) > 12
        ][:4]
    responsibilities = responsibilities or [f"Deliver the core work of {title}"]
    preferences = [
        item
        for item in _string_list(job_dict.get("question_preferences"))
        if item in INTERVIEW_CATEGORIES
    ]
    categories = preferences or list(INTERVIEW_CATEGORIES)
    difficulty = str(job_dict.get("interview_difficulty") or "medium").strip().lower()
    if difficulty not in {"easy", "medium", "hard"}:
        difficulty = "medium"
    plan = [
        QuestionPlan(
            category=category,
            focus=topics[index % len(topics)],
            difficulty=difficulty,
        )
        for index, category in enumerate(categories[:8])
    ]
    competencies = [
        BlueprintCompetency(
            name=topic,
            category="Technical",
            importance="high" if index < 4 else "medium",
            evidence_to_look_for=f"Concrete example involving {topic}",
        )
        for index, topic in enumerate(topics[:8])
    ]
    return InterviewBlueprint(
        target_role=title,
        company_context=company,
        job_summary=description[:1000],
        seniority=str(job_dict.get("experience_level") or "mid-level").strip(),
        technical_topics=topics[:12],
        soft_skills=["Communication", "Collaboration", "Problem solving"],
        responsibility_themes=responsibilities[:8],
        competency_matrix=competencies,
        suggested_question_plan=plan,
        evaluation_focus=[f"Evidence of {topic}" for topic in topics[:6]] or [f"Fit for {title}"],
        red_flags=[],
    )


def analyze_job_blueprint(job: Any, *, llm: Any = None) -> InterviewBlueprint:
    client = llm or get_llm_service()
    job_dict = _job_to_dict(job)
    deterministic = build_deterministic_blueprint(job_dict)
    prompt = BLUEPRINT_PROMPT.format(
        categories=", ".join(INTERVIEW_CATEGORIES),
        job_json=job_dict,
    )
    try:
        blueprint = client.generate_structured(prompt, response_model=InterviewBlueprint)
    except Exception as exc:
        logger.warning("Blueprint generation failed; using job-grounded fallback: %s", exc)
        return deterministic
    if not blueprint.target_role.strip():
        blueprint.target_role = deterministic.target_role
    if not blueprint.company_context:
        blueprint.company_context = deterministic.company_context
    if not blueprint.job_summary:
        blueprint.job_summary = deterministic.job_summary
    if not blueprint.technical_topics:
        blueprint.technical_topics = deterministic.technical_topics
    if not blueprint.responsibility_themes:
        blueprint.responsibility_themes = deterministic.responsibility_themes
    if not blueprint.competency_matrix:
        blueprint.competency_matrix = deterministic.competency_matrix
    if not blueprint.suggested_question_plan:
        blueprint.suggested_question_plan = deterministic.suggested_question_plan
    if not blueprint.evaluation_focus:
        blueprint.evaluation_focus = deterministic.evaluation_focus
    return blueprint


def build_job_blueprint(job: Any, *, llm: Any = None, refresh: bool = False) -> InterviewBlueprint:
    cached = getattr(job, "blueprint_json", None)
    if not refresh and isinstance(cached, dict) and cached:
        try:
            return InterviewBlueprint.model_validate(cached)
        except Exception:
            logger.debug("Ignoring invalid cached blueprint for job %s", getattr(job, "id", None))
    blueprint = analyze_job_blueprint(job, llm=llm)
    job_id = getattr(job, "id", None)
    if job_id is not None:
        store_blueprint(int(job_id), blueprint.to_dict())
    return blueprint
