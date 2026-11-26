import logging
from datetime import datetime
from typing import Any

from database import QuestionPracticeAttempt, get_db_context
from pydantic import BaseModel, ConfigDict, Field
from services.auth_service import require_owned_session_id
from services.llm_service import _error_message as redacted_error_message
from services.llm_service import get_llm_service

logger = logging.getLogger(__name__)

PRACTICE_RUBRIC_VERSION = "question-practice-v1"
UNAVAILABLE_NOTICE = (
    "AI feedback is unavailable right now. Your answer was saved; try again later."
)
_MAX_QUESTION_LENGTH = 4000
_MAX_ANSWER_LENGTH = 12000
_MAX_HISTORY_LIMIT = 20


class PracticeEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accuracy: float = Field(ge=0, le=100)
    relevance: float = Field(ge=0, le=100)
    clarity: float = Field(ge=0, le=100)
    completeness: float = Field(ge=0, le=100)
    overall_score: float = Field(ge=0, le=100)
    strengths: list[str] = Field(default_factory=list, max_length=6)
    missing_points: list[str] = Field(default_factory=list, max_length=6)
    improvements: list[str] = Field(default_factory=list, max_length=6)
    improved_answer: str = Field(min_length=1, max_length=6000)
    final_feedback: str = Field(min_length=1, max_length=2000)


PRACTICE_EVALUATION_PROMPT = """You are an expert interview coach. Evaluate a practice answer.

QUESTION CONTEXT:
Category: {category}
Difficulty: {difficulty}
Question: {question}
Expected key points: {key_points}

CANDIDATE'S ANSWER:
{answer}

Score every dimension from 0 to 100:
- accuracy: factual correctness
- relevance: how directly the answer addresses the question
- clarity: communication quality, structure, and articulation
- completeness: coverage of the expected key points
- overall_score: balanced overall quality

Also provide:
- strengths: specific things the answer did well
- missing_points: expected points that were not covered
- improvements: concrete changes that would improve the answer
- improved_answer: a stronger example answer the candidate could adapt
- final_feedback: short, honest, constructive feedback

Rules:
- Do not invent facts about the candidate or the role.
- Treat the scores as advisory, not objective truth.
- For behavioral answers, reward concrete situation, task, action, and result details.
- If the answer is empty or very short, score it accordingly.
- Return ONLY valid JSON matching the schema with no extra fields."""


def _clean_text(value: Any, limit: int, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required.")
    if len(text) > limit:
        raise ValueError(f"{label} is too long.")
    return text


def _clean_points(points: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for point in points or []:
        text = str(point or "").strip()
        if text:
            cleaned.append(text[:300])
        if len(cleaned) == 10:
            break
    return cleaned


def _validate_evaluation(result: PracticeEvaluation) -> PracticeEvaluation:
    if not isinstance(result, PracticeEvaluation):
        raise TypeError("Unexpected evaluation response")
    for name in ("accuracy", "relevance", "clarity", "completeness", "overall_score"):
        value = float(getattr(result, name))
        if not 0.0 <= value <= 100.0:
            raise ValueError("Evaluation score is outside the allowed range")
    if not result.improved_answer.strip() or not result.final_feedback.strip():
        raise ValueError("Evaluation response is incomplete")
    return result


def _request_evaluation(
    *,
    question: str,
    answer: str,
    category: str,
    difficulty: str,
    expected_key_points: list[str],
) -> PracticeEvaluation:
    prompt = PRACTICE_EVALUATION_PROMPT.format(
        category=category,
        difficulty=difficulty,
        question=question,
        key_points=", ".join(expected_key_points) or "Not specified",
        answer=answer,
    )
    result = get_llm_service().generate_structured(
        prompt,
        PracticeEvaluation,
        max_tokens=1800,
        temperature=0.1,
    )
    return _validate_evaluation(result)


def evaluate_practice_answer(
    *,
    session_id: int,
    actor_user_id: int | None,
    question_text: str,
    answer_text: str,
    category: str = "technical",
    difficulty: str = "medium",
    expected_key_points: list[str] | None = None,
    source: str = "generated",
    source_id: int | None = None,
) -> dict:
    require_owned_session_id(session_id, actor_user_id)
    question = _clean_text(question_text, _MAX_QUESTION_LENGTH, "Question")
    answer = _clean_text(answer_text, _MAX_ANSWER_LENGTH, "Answer")
    points = _clean_points(expected_key_points)

    evaluation: PracticeEvaluation | None = None
    try:
        evaluation = _request_evaluation(
            question=question,
            answer=answer,
            category=str(category or "technical")[:50] or "technical",
            difficulty=str(difficulty or "medium")[:20] or "medium",
            expected_key_points=points,
        )
    except Exception as exc:
        logger.warning("Question practice evaluation failed: %s", redacted_error_message(exc))

    submitted_at = datetime.now()
    with get_db_context() as db:
        row = QuestionPracticeAttempt(
            session_id=session_id,
            source=str(source or "generated")[:30] or "generated",
            source_id=source_id,
            question_text=question,
            category=str(category or "technical")[:50] or "technical",
            difficulty=str(difficulty or "medium")[:20] or "medium",
            expected_key_points=points,
            answer_text=answer,
            status="evaluated" if evaluation is not None else "unavailable",
            submitted_at=submitted_at,
        )
        if evaluation is not None:
            row.accuracy = evaluation.accuracy
            row.relevance = evaluation.relevance
            row.clarity = evaluation.clarity
            row.completeness = evaluation.completeness
            row.overall_score = evaluation.overall_score
            row.strengths = evaluation.strengths
            row.missing_points = evaluation.missing_points
            row.improvements = evaluation.improvements
            row.improved_answer = evaluation.improved_answer
            row.final_feedback = evaluation.final_feedback
            row.rubric_version = PRACTICE_RUBRIC_VERSION
            row.evaluated_at = submitted_at
        db.add(row)
        db.flush()
        attempt_id = int(row.id)

    return {
        "attempt_id": attempt_id,
        "status": "evaluated" if evaluation is not None else "unavailable",
        "notice": "" if evaluation is not None else UNAVAILABLE_NOTICE,
        "evaluation": evaluation.model_dump() if evaluation is not None else None,
        "submitted_at": submitted_at,
    }


def list_practice_attempts(
    session_id: int, actor_user_id: int | None, limit: int = 8
) -> list[dict]:
    require_owned_session_id(session_id, actor_user_id)
    safe_limit = min(_MAX_HISTORY_LIMIT, max(1, int(limit)))
    with get_db_context() as db:
        rows = (
            db.query(QuestionPracticeAttempt)
            .filter(QuestionPracticeAttempt.session_id == session_id)
            .order_by(QuestionPracticeAttempt.submitted_at.desc(), QuestionPracticeAttempt.id.desc())
            .limit(safe_limit)
            .all()
        )
        return [
            {
                "id": int(row.id),
                "source": str(row.source or "generated"),
                "source_id": row.source_id,
                "question_text": str(row.question_text or ""),
                "answer_text": str(row.answer_text or ""),
                "category": str(row.category or "technical"),
                "difficulty": str(row.difficulty or "medium"),
                "status": str(row.status or "unavailable"),
                "overall_score": row.overall_score,
                "accuracy": row.accuracy,
                "relevance": row.relevance,
                "clarity": row.clarity,
                "completeness": row.completeness,
                "strengths": list(row.strengths or []),
                "missing_points": list(row.missing_points or []),
                "improvements": list(row.improvements or []),
                "improved_answer": str(row.improved_answer or ""),
                "final_feedback": str(row.final_feedback or ""),
                "submitted_at": row.submitted_at,
            }
            for row in rows
        ]
