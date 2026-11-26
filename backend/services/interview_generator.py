import logging
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator
from services.job_analyzer import JobAnalysisResult, get_job_input_limit
from services.job_matcher import ResumeJobMatchResult
from services.llm_service import _error_message as redacted_error_message
from services.llm_service import get_llm_service
from services.resume_analyzer import ResumeAnalysisResult, get_resume_input_limit
from utils.helpers import truncate_text

logger = logging.getLogger(__name__)

_MAX_GENERATION_ATTEMPTS = 2
_MAX_QUESTION_TEXT_LENGTH = 4000
_MAX_TOTAL_QUESTIONS = 50
_GENERATION_ERROR_MESSAGE = (
    "Question generation could not produce the requested number of valid, unique questions "
    "after two attempts. Check the AI provider configuration and try again."
)


class QuestionCategory(str, Enum):
    TECHNICAL = "technical"
    HR = "hr"
    BEHAVIORAL = "behavioral"
    SITUATIONAL = "situational"
    PROJECT_BASED = "project_based"
    RESUME_BASED = "resume_based"
    JOB_SPECIFIC = "job_specific"


class Difficulty(str, Enum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class InterviewQuestion(BaseModel):
    question_number: int
    category: QuestionCategory
    difficulty: Difficulty
    question_text: str
    expected_key_points: list[str] = []
    follow_up_hints: list[str] = []


_VALID_CATEGORIES = {c.value for c in QuestionCategory}
_VALID_DIFFICULTIES = {d.value for d in Difficulty}
_VALID_REQUESTED_DIFFICULTIES = _VALID_DIFFICULTIES | {"mixed"}

_DIFFICULTY_ALIASES = {
    "beginner": "easy",
    "beginner_level": "easy",
    "beginner level": "easy",
    "intermediate": "medium",
    "advanced": "hard",
    "mixed": "mixed",
}

_QUESTION_TYPE_CATEGORIES: dict[str, tuple[QuestionCategory, ...]] = {
    "technical": (QuestionCategory.TECHNICAL, QuestionCategory.PROJECT_BASED),
    "hr_and_behavioral": (QuestionCategory.HR, QuestionCategory.BEHAVIORAL),
    "situational": (QuestionCategory.SITUATIONAL, QuestionCategory.BEHAVIORAL),
    "problem_solving": (
        QuestionCategory.SITUATIONAL,
        QuestionCategory.PROJECT_BASED,
        QuestionCategory.TECHNICAL,
    ),
    "conceptual": (QuestionCategory.TECHNICAL, QuestionCategory.JOB_SPECIFIC),
    "practical": (QuestionCategory.PROJECT_BASED, QuestionCategory.TECHNICAL),
    "general_interview": (
        QuestionCategory.HR,
        QuestionCategory.BEHAVIORAL,
        QuestionCategory.SITUATIONAL,
        QuestionCategory.JOB_SPECIFIC,
    ),
    "mixed": tuple(QuestionCategory),
}


def _normalize_label(value: Any, default: str) -> str:
    if value is None:
        return default

    label = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if not label:
        return default
    return label


class QuestionSet(BaseModel):
    questions: list[InterviewQuestion]
    total_count: int = 0
    category_distribution: dict = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def coerce_missing_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        data = dict(data)
        questions = data.get("questions")
        if isinstance(questions, list):
            fixed_questions = []
            for i, q in enumerate(questions):
                if isinstance(q, str):
                    fixed_questions.append(
                        {
                            "question_number": i + 1,
                            "category": "technical",
                            "difficulty": "medium",
                            "question_text": q,
                            "expected_key_points": [],
                            "follow_up_hints": [],
                        }
                    )
                    continue

                if not isinstance(q, dict):
                    if hasattr(q, "model_dump"):
                        q = q.model_dump()
                    else:
                        continue

                item = dict(q)
                item["question_number"] = item.get("question_number") or i + 1

                category = _normalize_label(item.get("category"), "technical")
                item["category"] = category if category in _VALID_CATEGORIES else "technical"

                difficulty = _normalize_label(item.get("difficulty"), "medium")
                item["difficulty"] = difficulty if difficulty in _VALID_DIFFICULTIES else "medium"

                item["question_text"] = item.get("question_text") or item.get("question") or ""
                for key in ("expected_key_points", "follow_up_hints"):
                    value = item.get(key)
                    if value is None:
                        item[key] = []
                    elif isinstance(value, str):
                        item[key] = [value]
                    elif isinstance(value, list):
                        item[key] = [str(v) for v in value]
                    else:
                        item[key] = []
                fixed_questions.append(item)

            data["questions"] = fixed_questions
            data["total_count"] = len(fixed_questions)

        distribution = data.get("category_distribution")
        if isinstance(distribution, dict) and distribution:
            data["category_distribution"] = {
                _normalize_label(k, "technical"): v for k, v in distribution.items()
            }
        elif isinstance(data.get("questions"), list):
            derived: dict[str, int] = {}
            for q in data["questions"]:
                key = q.get("category", "technical") if isinstance(q, dict) else "technical"
                derived[key] = derived.get(key, 0) + 1
            data["category_distribution"] = derived

        return data


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _bounded_context(value: Any, limit: int = 300) -> str:
    return _clean_text(value)[:limit] or "None specified"


def _normalize_requested_difficulty(value: Any) -> str:
    label = _normalize_label(value, "medium")
    return _DIFFICULTY_ALIASES.get(label, label)


def _categories_for_question_type(value: Any) -> list[QuestionCategory]:
    key = _normalize_label(value, "")
    return list(_QUESTION_TYPE_CATEGORIES.get(key, tuple(QuestionCategory)))


def _category_distribution(questions: list[InterviewQuestion]) -> dict[str, int]:
    distribution: dict[str, int] = {}
    for question in questions:
        category = _enum_value(question.category)
        distribution[category] = distribution.get(category, 0) + 1
    return distribution


def _validate_generated_questions(result: QuestionSet, requested_count: int) -> QuestionSet:
    if not isinstance(result, QuestionSet):
        result = QuestionSet.model_validate(result)

    cleaned: list[InterviewQuestion] = []
    seen: set[str] = set()
    for question in result.questions:
        text = _clean_text(question.question_text)
        if not text:
            raise ValueError("Generated question text is empty.")
        if len(text) > _MAX_QUESTION_TEXT_LENGTH:
            raise ValueError("Generated question text is too long.")
        if int(question.question_number) < 1:
            raise ValueError("Generated question number is invalid.")
        category = _normalize_label(_enum_value(question.category), "")
        difficulty = _normalize_label(_enum_value(question.difficulty), "")
        if category not in _VALID_CATEGORIES:
            raise ValueError("Generated question has an invalid category.")
        if difficulty not in _VALID_DIFFICULTIES:
            raise ValueError("Generated question has an invalid difficulty.")
        if not isinstance(question.expected_key_points, list) or not isinstance(
            question.follow_up_hints, list
        ):
            raise TypeError("Generated question is missing required fields.")

        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(
            question.model_copy(
                update={"question_number": len(cleaned) + 1, "question_text": text}
            )
        )

    if len(cleaned) != requested_count:
        raise ValueError(
            f"Expected {requested_count} unique questions, received {len(cleaned)}."
        )

    return QuestionSet(
        questions=cleaned,
        total_count=len(cleaned),
        category_distribution=_category_distribution(cleaned),
    )


QUESTION_GENERATION_PROMPT = """You are an expert interview coach. Generate original interview questions for any industry, profession, education path, or career stage.

UNIVERSAL QUESTION CONFIGURATION:
- Industry or sector: {industry}
- Target job role or profession: {target_role}
- Topic or subject: {topic}
- Question type: {question_type}
- Experience level: {experience_level}
- Difficulty: {difficulty}
- Total questions needed: {total_questions}
- Categories to include: {categories}
- Interview type: {interview_type}
- Focus areas: {focus_areas}

OPTIONAL CANDIDATE CONTEXT (use only when it is populated and relevant; never let it override the configuration above):
CANDIDATE RESUME:
{resume_json}

TARGET JOB:
{job_json}

MATCH ANALYSIS:
{match_json}

KNOWLEDGE BASE CONTEXT (use it when relevant):
{knowledge_context}

Generate exactly {total_questions} highly relevant interview questions with unique, non-empty question text.
Use the universal configuration as the primary source of truth. Do not assume that every profession is technical or AI-related.
When no role is specified, ask role-neutral questions that still match the industry, topic, and experience level.
When difficulty is "mixed", vary the questions across easy, medium, and hard; every question's "difficulty" field must still be easy, medium, or hard.
Test domain-specific competencies appropriate to the profession, such as clinical judgment, classroom management, financial reporting, sales pipeline, design critique, legal reasoning, or technical problem solving.
Distribute questions across these allowed categories: {categories}.
- Technical: Core skills, tools, and applied problem solving
- HR: Motivation, culture fit, career goals
- Behavioral: Past behavior predicting future performance (STAR method)
- Situational: Hypothetical scenarios relevant to the industry and role
- Project-based: Deep dives into relevant work samples or case studies
- Resume-based: Clarification of candidate claims when resume context exists
- Job-specific: Questions tailored to the named role when provided

Each question must have:
1. question_number: Sequential number (1 to {total_questions})
2. category: One of the allowed categories listed above (lowercase: technical, hr, behavioral, situational, project_based, resume_based, job_specific)
3. difficulty: easy, medium, or hard
4. question_text: The actual question, specific to the configured industry, role, topic, question type, and experience level
5. expected_key_points: 3-5 key points a strong answer should cover
6. follow_up_hints: 2-3 hints for follow-up questions based on answer

Avoid generic questions like "Tell me about yourself" unless specifically needed.
DO NOT mention that the resume, job, or knowledge base is empty or missing.

Return ONLY valid JSON matching this exact structure:
{{
  "questions": [
    {{
      "question_number": 1,
      "category": "technical",
      "difficulty": "medium",
      "question_text": "Your specific question here",
      "expected_key_points": ["point 1", "point 2", "point 3"],
      "follow_up_hints": ["hint 1", "hint 2"]
    }}
  ],
  "total_count": {total_questions},
  "category_distribution": {{"technical": 1}}
}}

The category_distribution must use lowercase keys, contain only allowed categories, and sum to total_count ({total_questions})."""


RETRY_QUESTION_PROMPT = """Generate exactly {total_questions} interview questions. Every question must be unique, non-empty, and specific to the configuration below. Return ONLY a single valid JSON object with this EXACT structure:

CONFIGURATION:
- Industry or sector: {industry}
- Target job role or profession: {target_role}
- Topic or subject: {topic}
- Question type: {question_type}
- Experience level: {experience_level}
- Difficulty: {difficulty}
- Categories to include: {categories}
- Interview type: {interview_type}
- Focus areas: {focus_areas}

{{
  "questions": [
    {{"question_number": 1, "category": "technical", "difficulty": "medium", "question_text": "Your question here", "expected_key_points": ["point 1", "point 2"], "follow_up_hints": ["hint 1"]}},
    {{"question_number": 2, "category": "hr", "difficulty": "medium", "question_text": "Your question here", "expected_key_points": ["point 1", "point 2"], "follow_up_hints": ["hint 1"]}}
  ],
  "total_count": {total_questions},
  "category_distribution": {{"technical": 1, "hr": 1}}
}}

CRITICAL RULES:
- "questions" MUST be an array of exactly {total_questions} OBJECTS (not strings)
- Do not repeat or paraphrase a question; every question_text must be unique and non-empty
- Tailor every question to the industry, role, topic, question type, and experience level above; do not assume a technical or AI profession
- When difficulty is "mixed", vary across easy, medium, and hard
- Each question object MUST have: question_number (int), category (lowercase), difficulty (easy/medium/hard), question_text (string), expected_key_points (array of strings), follow_up_hints (array of strings)
- "category_distribution" MUST use lowercase keys and sum to total_count
- NO markdown, NO explanation, NO ```json fences
- Return ONLY the JSON object"""


class InterviewGenerator:
    def __init__(self):
        self.llm = get_llm_service()

    def generate(
        self,
        resume: ResumeAnalysisResult,
        job: JobAnalysisResult,
        match: ResumeJobMatchResult,
        total_questions: int = 5,
        difficulty: str = "medium",
        categories: list[QuestionCategory] | None = None,
        focus_areas: list[str] | None = None,
        knowledge_context: str = "",
        interview_type: str = "Full Interview",
        *,
        industry: str = "",
        target_role: str = "",
        topic: str = "",
        question_type: str = "",
        experience_level: str = "",
    ) -> QuestionSet:

        if not isinstance(total_questions, int) or not 1 <= total_questions <= _MAX_TOTAL_QUESTIONS:
            raise ValueError(f"Total questions must be between 1 and {_MAX_TOTAL_QUESTIONS}.")

        requested_difficulty = _normalize_requested_difficulty(difficulty)
        if requested_difficulty not in _VALID_REQUESTED_DIFFICULTIES:
            raise ValueError("Difficulty must be easy, medium, hard, or mixed.")

        if categories is None:
            categories = _categories_for_question_type(question_type)

        category_names = [c.value for c in categories]
        focus = list(focus_areas or [])

        for missing in match.missing_skills:
            if missing.importance in ["critical", "important"]:
                focus.append(f"Missing skill: {missing.skill}")

        resume_limit = get_resume_input_limit()
        job_limit = get_job_input_limit()
        universality = {
            "industry": _bounded_context(industry),
            "target_role": _bounded_context(target_role),
            "topic": _bounded_context(topic),
            "question_type": _bounded_context(question_type, 120),
            "experience_level": _bounded_context(experience_level, 120),
        }
        common_prompt_values = {
            **universality,
            "total_questions": total_questions,
            "difficulty": requested_difficulty,
            "categories": ", ".join(category_names),
            "focus_areas": ", ".join(focus) if focus else "None specified",
            "interview_type": _bounded_context(interview_type, 120),
        }
        prompt = QUESTION_GENERATION_PROMPT.format(
            **common_prompt_values,
            resume_json=truncate_text(resume.model_dump_json(indent=2), resume_limit),
            job_json=truncate_text(job.model_dump_json(indent=2), job_limit),
            match_json=truncate_text(match.model_dump_json(indent=2), job_limit),
            knowledge_context=knowledge_context[:4000] or "None provided",
        )
        retry_prompt = RETRY_QUESTION_PROMPT.format(**common_prompt_values)
        retry_prompt += (
            "\n\nKNOWLEDGE CONTEXT:\n"
            + knowledge_context[:2000]
            + "\n\nRESUME:\n"
            + truncate_text(resume.model_dump_json(indent=2), resume_limit)
            + "\n\nJOB:\n"
            + truncate_text(job.model_dump_json(indent=2), job_limit)
            + "\n\nMATCH:\n"
            + truncate_text(match.model_dump_json(indent=2), job_limit)
        )

        last_error: Exception | None = None
        for attempt in range(_MAX_GENERATION_ATTEMPTS):
            active_prompt = prompt if attempt == 0 else retry_prompt
            kwargs: dict[str, Any] = {} if attempt == 0 else {"max_tokens": 3000}
            try:
                result = self.llm.generate_structured(active_prompt, QuestionSet, **kwargs)
                return _validate_generated_questions(result, total_questions)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Question generation attempt %d failed: %s",
                    attempt + 1,
                    redacted_error_message(exc),
                )

        raise RuntimeError(_GENERATION_ERROR_MESSAGE) from last_error

    def generate_follow_up(
        self,
        previous_question: InterviewQuestion,
        candidate_answer: str,
        resume: ResumeAnalysisResult,
        job: JobAnalysisResult,
    ) -> InterviewQuestion:

        FOLLOW_UP_PROMPT = """Generate a relevant follow-up question based on the candidate's answer.

PREVIOUS QUESTION:
Category: {category}
Difficulty: {difficulty}
Question: {question}
Expected key points: {key_points}

CANDIDATE'S ANSWER:
{answer}

CANDIDATE CONTEXT:
{resume_summary}

Generate ONE follow-up question that:
1. Probes deeper based on their answer
2. Addresses gaps or vague points in their response
3. Is personalized to their background
4. Maintains appropriate difficulty

Return as JSON matching InterviewQuestion schema."""

        resume_summary = f"""
Name: {resume.candidate_name}
Experience: {len(resume.experience)} roles
Key Skills: {', '.join(resume.skills[:10])}
Projects: {len(resume.projects)}
"""

        prompt = FOLLOW_UP_PROMPT.format(
            category=previous_question.category.value,
            difficulty=previous_question.difficulty.value,
            question=previous_question.question_text,
            key_points=", ".join(previous_question.expected_key_points),
            answer=candidate_answer,
            resume_summary=resume_summary,
        )

        try:
            return self.llm.generate_structured(prompt, InterviewQuestion)
        except Exception as exc:
            logger.warning(
                "Follow-up question generation failed: %s", redacted_error_message(exc)
            )
            raise RuntimeError(
                "Follow-up question generation could not complete. Check the AI provider configuration and try again."
            ) from exc


def generate_interview_questions(
    resume: ResumeAnalysisResult,
    job: JobAnalysisResult,
    match: ResumeJobMatchResult,
    total_questions: int = 5,
    difficulty: str = "medium",
    categories: list[QuestionCategory] | None = None,
    knowledge_context: str = "",
    interview_type: str = "Full Interview",
    *,
    focus_areas: list[str] | None = None,
    industry: str = "",
    target_role: str = "",
    topic: str = "",
    question_type: str = "",
    experience_level: str = "",
) -> QuestionSet:
    generator = InterviewGenerator()
    return generator.generate(
        resume,
        job,
        match,
        total_questions,
        difficulty,
        categories,
        focus_areas,
        knowledge_context=knowledge_context,
        interview_type=interview_type,
        industry=industry,
        target_role=target_role,
        topic=topic,
        question_type=question_type,
        experience_level=experience_level,
    )
