from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, field_validator
from services.answer_evaluator import RUBRIC_VERSION, AnswerEvaluation
from services.job_blueprint import InterviewBlueprint
from services.job_interview_constants import (
    INTERVIEW_CATEGORIES,
    INTERVIEW_MODES,
    MAX_TARGET_QUESTIONS,
)
from services.llm_service import get_llm_service

logger = logging.getLogger(__name__)


class QuestionGenerationError(RuntimeError):
    """Raised when a job-grounded question cannot be generated."""


class AdaptiveQuestion(BaseModel):
    question_number: int
    category: str
    difficulty: str
    question_text: str
    expected_key_points: list[str] = Field(default_factory=list)
    focus_skill: str = ""
    follow_up_of: int | None = None

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        normalized = str(value or "").strip()
        lookup = {item.lower(): item for item in INTERVIEW_CATEGORIES}
        return lookup.get(normalized.lower(), "Job-Specific")

    @field_validator("difficulty")
    @classmethod
    def _difficulty(cls, value: str) -> str:
        normalized = str(value or "medium").strip().lower()
        return normalized if normalized in {"easy", "medium", "hard"} else "medium"

    @field_validator("question_text")
    @classmethod
    def _question(cls, value: str) -> str:
        text = str(value or "").strip()
        if len(text) < 12:
            raise ValueError("Question text is too short.")
        return text


class AdaptiveEvaluation(AnswerEvaluation):
    knowledge_gaps: list[str] = Field(default_factory=list)
    demonstrated_strengths: list[str] = Field(default_factory=list)
    job_requirement_relevance: float = Field(default=5.0, ge=0, le=10)
    next_difficulty: str = "medium"
    follow_up_needed: bool = False
    follow_up_focus: str = ""
    private_reasoning: str = ""

    @field_validator("next_difficulty")
    @classmethod
    def _next_difficulty(cls, value: str) -> str:
        normalized = str(value or "medium").strip().lower()
        return normalized if normalized in {"easy", "medium", "hard"} else "medium"

    def as_base(self) -> AnswerEvaluation:
        return AnswerEvaluation.model_validate(self.model_dump())


class AnswerReview(BaseModel):
    question_number: int
    category: str
    difficulty: str
    question: str
    answer: str
    score: float
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    knowledge_gaps: list[str] = Field(default_factory=list)
    suggested_answer: str = ""
    feedback: str = ""
    media_id: int | None = None


class JobInterviewReport(BaseModel):
    job_title: str
    company: str
    interview_mode: str
    duration_minutes: int
    questions_answered: int
    overall_score: float
    readiness_level: str
    executive_summary: str
    category_scores: dict[str, float] = Field(default_factory=dict)
    skill_coverage: dict[str, str] = Field(default_factory=dict)
    strengths: list[str] = Field(default_factory=list)
    areas_to_improve: list[str] = Field(default_factory=list)
    recommended_topics: list[str] = Field(default_factory=list)
    learning_plan: list[str] = Field(default_factory=list)
    answers: list[AnswerReview] = Field(default_factory=list)
    rubric_version: str = RUBRIC_VERSION

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@dataclass
class InterviewConfig:
    mode: str = "text"
    difficulty: str = "medium"
    duration_minutes: int = 20
    target_questions: int = 5
    question_preferences: list[str] = field(default_factory=list)
    follow_up_budget: int = 2

    def __post_init__(self) -> None:
        if self.mode not in INTERVIEW_MODES:
            raise ValueError("Unsupported interview mode.")
        if self.difficulty not in {"easy", "medium", "hard"}:
            raise ValueError("Unsupported difficulty.")
        self.duration_minutes = max(5, min(90, int(self.duration_minutes)))
        self.target_questions = max(3, min(MAX_TARGET_QUESTIONS, int(self.target_questions)))
        self.follow_up_budget = max(0, min(4, int(self.follow_up_budget)))


_QUESTION_PROMPT = """You are an adaptive interviewer for a specific job posting.

Generate exactly ONE question for the next interview turn. The question MUST be
grounded in the target job's skills, responsibilities, or stated competencies.
Never ask about unrelated technologies, employers, or topics. Never claim to
assess emotions, personality, honesty, or facial expressions.

JOB BLUEPRINT:
{blueprint_json}

INTERVIEW CONFIG:
{config_json}

PREVIOUS TURNS (question, answer, private evaluation notes):
{history_json}

NEXT CATEGORY: {category}
TARGET DIFFICULTY: {difficulty}
{follow_up_block}

Return strict JSON with: question_number, category, difficulty, question_text,
expected_key_points (3-5), focus_skill, follow_up_of.
Use one of these categories only: {categories}.
Make the question specific enough that a strong answer would demonstrate the target job's requirements.
"""

_FOLLOW_UP_BLOCK = """This is a FOLLOW-UP to question {parent_number}.
Previous question: {parent_question}
Previous answer: {parent_answer}
Probe deeper into: {focus}. Do not repeat the previous question.
"""

_EVALUATION_PROMPT = """Evaluate one answer in a job-based adaptive interview. Use the shared rubric
version {rubric_version}. Scores are 0-10 and advisory, not objective truth.

JOB BLUEPRINT:
{blueprint_json}

QUESTION:
{question_json}

CANDIDATE ANSWER:
{answer}

INTERVIEW MODE: {mode}

Return strict JSON with the base rubric fields: technical_accuracy, relevance,
communication, confidence, completeness, problem_solving, structure,
overall_score, strengths, weaknesses, missing_points, incorrect_points,
suggested_answer, ideal_answer, communication_feedback, technical_feedback,
rubric_version. Also return: knowledge_gaps, demonstrated_strengths,
job_requirement_relevance (0-10), follow_up_needed (bool), follow_up_focus,
private_reasoning. Base every observation on the answer text only; do not infer
emotions, facial expressions, or personality traits.
"""

_REPORT_PROMPT = """Create a final, evidence-based interview report from the recorded questions,
answers, and evaluations. Use only the supplied evidence. Mention uncertainty
where evidence is thin. Do not include fabricated facts or emotion claims.

JOB: {job_title} at {company}
MODE: {mode}
DURATION: {duration_minutes} minutes

BLUEPRINT:
{blueprint_json}

TURNS:
{turns_json}

RETURN strict JSON with: executive_summary (2-3 sentences), readiness_level
(beginner/developing/job-ready/strong), strengths, areas_to_improve,
recommended_topics, skill_coverage (object mapping each technical topic to
"strong", "partial", or "not demonstrated"), learning_plan (3-5 concrete steps).
"""


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9+#.]{3,}", str(text).lower())
        if token not in {"the", "and", "for", "with", "your", "you", "how", "what", "this", "that"}
    }


class AdaptiveInterviewEngine:
    """One AI engine shared by text, voice, and video interview modes."""

    def __init__(self, llm: Any = None):
        self.llm = llm or get_llm_service()

    def _grounding_terms(self, blueprint: InterviewBlueprint) -> set[str]:
        terms: set[str] = set()
        for value in blueprint.all_focus_terms:
            terms.update(_tokens(value))
        return terms

    def _is_grounded(self, question: AdaptiveQuestion, blueprint: InterviewBlueprint) -> bool:
        question_tokens = _tokens(question.question_text)
        if question_tokens & self._grounding_terms(blueprint):
            return True
        focus_tokens = _tokens(question.focus_skill)
        return bool(focus_tokens & self._grounding_terms(blueprint))

    def _select_category(
        self, blueprint: InterviewBlueprint, config: InterviewConfig, history: list[dict[str, Any]]
    ) -> str:
        preferred = [item for item in config.question_preferences if item in INTERVIEW_CATEGORIES]
        planned = [
            item.category
            for item in blueprint.suggested_question_plan
            if item.category in INTERVIEW_CATEGORIES
        ]
        options = preferred or planned or list(INTERVIEW_CATEGORIES)
        return options[len(history) % len(options)]

    def _select_difficulty(
        self, config: InterviewConfig, history: list[dict[str, Any]], follow_up: bool = False
    ) -> str:
        if not history:
            return config.difficulty
        last = history[-1].get("evaluation") or {}
        nxt = str(last.get("next_difficulty") or config.difficulty).lower()
        if nxt not in {"easy", "medium", "hard"}:
            nxt = config.difficulty
        if follow_up and nxt == "easy":
            return "medium"
        return nxt

    def next_question(
        self,
        blueprint: InterviewBlueprint,
        config: InterviewConfig,
        history: list[dict[str, Any]],
        *,
        follow_up_of: int | None = None,
        follow_up_focus: str = "",
        previous_answer: str = "",
    ) -> AdaptiveQuestion:
        category = self._select_category(blueprint, config, history)
        if follow_up_of is not None:
            category = (
                "Job-Specific"
                if not config.question_preferences
                else config.question_preferences[len(history) % len(config.question_preferences)]
            )
        difficulty = self._select_difficulty(config, history, follow_up=follow_up_of is not None)
        parent = next((item for item in history if item.get("number") == follow_up_of), None)
        follow_up_block = ""
        if follow_up_of is not None and parent:
            follow_up_block = _FOLLOW_UP_BLOCK.format(
                parent_number=follow_up_of,
                parent_question=parent.get("question", ""),
                parent_answer=str(previous_answer)[:1200],
                focus=follow_up_focus
                or (parent.get("evaluation") or {}).get("follow_up_focus", ""),
            )
        prompt = _QUESTION_PROMPT.format(
            blueprint_json=blueprint.to_dict(),
            config_json=config.__dict__,
            history_json=[
                {
                    "question": item.get("question", ""),
                    "answer": str(item.get("answer", ""))[:1200],
                    "private_notes": {
                        "gaps": (item.get("evaluation") or {}).get("knowledge_gaps", []),
                        "focus": (item.get("evaluation") or {}).get("follow_up_focus", ""),
                    },
                }
                for item in history
            ],
            category=category,
            difficulty=difficulty,
            follow_up_block=follow_up_block,
            categories=", ".join(INTERVIEW_CATEGORIES),
        )
        try:
            question = self._generate_question_once(prompt, len(history) + 1, follow_up_of)
            if not self._is_grounded(question, blueprint):
                strict_prompt = prompt + (
                    "\nThe previous question was NOT related to the job. Rewrite it so it "
                    "explicitly mentions and tests one of these job terms: "
                    + ", ".join(blueprint.all_focus_terms[:12])
                )
                question = self._generate_question_once(
                    strict_prompt, len(history) + 1, follow_up_of
                )
            if self._is_grounded(question, blueprint):
                return question
            logger.warning("Generated question was not job-grounded; using deterministic question.")
        except QuestionGenerationError as exc:
            logger.warning("Question generation failed; using deterministic question: %s", exc)
        return self._deterministic_question(
            blueprint,
            config,
            history,
            category=category,
            difficulty=difficulty,
            follow_up_of=follow_up_of,
            follow_up_focus=follow_up_focus,
        )

    def _generate_question_once(
        self, prompt: str, number: int, follow_up_of: int | None
    ) -> AdaptiveQuestion:
        try:
            question = self.llm.generate_structured(prompt, response_model=AdaptiveQuestion)
        except Exception as exc:
            logger.warning("Question generation failed: %s", exc)
            raise QuestionGenerationError(
                "Could not generate the next interview question."
            ) from exc
        question.question_number = number
        question.follow_up_of = follow_up_of
        return question

    def _deterministic_question(
        self,
        blueprint: InterviewBlueprint,
        config: InterviewConfig,
        history: list[dict[str, Any]],
        *,
        category: str,
        difficulty: str,
        follow_up_of: int | None,
        follow_up_focus: str,
    ) -> AdaptiveQuestion:
        terms = (
            blueprint.technical_topics
            or [item.name for item in blueprint.competency_matrix]
            or blueprint.all_focus_terms
            or [blueprint.target_role]
        )
        responsibilities = blueprint.responsibility_themes or [blueprint.target_role]
        index = len(history)
        topic = terms[index % len(terms)]
        responsibility = responsibilities[index % len(responsibilities)]
        company = blueprint.company_context or "this company"

        def make_question(focus: str, duty: str) -> str:
            templates = {
                "HR": (
                    f"Why does the {blueprint.target_role} role at {company} fit your experience "
                    f"with {focus}?"
                ),
                "Technical": (
                    f"Describe how you would apply {focus} to {duty} in this "
                    f"{blueprint.target_role} role."
                ),
                "Behavioral": (
                    f"Tell me about a time you used {focus} while working on {duty}. "
                    "What did you do and what was the outcome?"
                ),
                "Scenario-Based": (
                    f"A project requires {focus} to deliver {duty}. How would you approach it?"
                ),
                "Problem-Solving": (
                    f"How would you troubleshoot a {focus} problem affecting {duty}?"
                ),
                "Job-Specific": (
                    f"For this {blueprint.target_role} position, explain how you would use {focus} "
                    f"to meet this responsibility: {duty}."
                ),
            }
            return templates.get(category, templates["Job-Specific"])

        question_text = make_question(topic, responsibility)
        existing = {str(item.get("question", "")).strip().lower() for item in history}
        if question_text.lower() in existing:
            topic = terms[(index + 1) % len(terms)]
            responsibility = responsibilities[(index + 1) % len(responsibilities)]
            question_text = make_question(topic, responsibility)
        if follow_up_of is not None:
            probe = follow_up_focus or topic
            question_text = (
                f"You mentioned {probe}. Give a concrete example of how you applied {topic} "
                f"to {responsibility}, including the outcome."
            )
        suffix = {
            "easy": "Start with the basic concepts.",
            "medium": "Include the main trade-offs.",
            "hard": "Include trade-offs, constraints, and measurable outcomes.",
        }.get(difficulty, "")
        if suffix and suffix not in question_text:
            question_text = f"{question_text} {suffix}"
        return AdaptiveQuestion(
            question_number=len(history) + 1,
            category=category,
            difficulty=difficulty,
            question_text=question_text,
            expected_key_points=[topic, responsibility],
            focus_skill=topic,
            follow_up_of=follow_up_of,
        )

    def evaluate_answer(
        self,
        question: AdaptiveQuestion,
        answer: str,
        blueprint: InterviewBlueprint,
        config: InterviewConfig,
    ) -> AdaptiveEvaluation:
        prompt = _EVALUATION_PROMPT.format(
            rubric_version=RUBRIC_VERSION,
            blueprint_json=blueprint.to_dict(),
            question_json=question.model_dump(mode="json"),
            answer=answer[:6000],
            mode=config.mode,
        )
        try:
            evaluation = self.llm.generate_structured(prompt, response_model=AdaptiveEvaluation)
        except Exception as exc:
            logger.warning("Adaptive evaluation failed; using deterministic evaluation: %s", exc)
            evaluation = self._deterministic_evaluation(question, answer, blueprint)
        return self._finalize_evaluation(evaluation, question, answer, config)

    def _deterministic_evaluation(
        self,
        question: AdaptiveQuestion,
        answer: str,
        blueprint: InterviewBlueprint,
    ) -> AdaptiveEvaluation:
        answer_tokens = _tokens(answer)
        focus = [question.focus_skill] if question.focus_skill else []
        expected_points = (
            question.expected_key_points
            or focus
            or blueprint.technical_topics[:3]
            or [blueprint.target_role]
        )
        matched: list[str] = []
        missing: list[str] = []
        for point in expected_points:
            if _tokens(point) & answer_tokens:
                matched.append(point)
            else:
                missing.append(point)
        coverage = len(matched) / len(expected_points)
        words = len(answer.split())
        length_score = min(10.0, words / 10.0)
        structure_score = min(
            10.0,
            5.0
            + (2.0 if ". " in answer or "\n" in answer else 0.0)
            + (1.0 if len(answer) > 300 else 0.0)
            + (
                2.0
                if any(
                    marker in answer.lower()
                    for marker in ("first", "then", "finally", "because", "for example")
                )
                else 0.0
            ),
        )
        relevance = round(2.0 + 8.0 * coverage, 1)
        technical = (
            round(3.0 + 7.0 * coverage, 1)
            if question.category == "Technical"
            else round(5.0 + 5.0 * coverage, 1)
        )
        completeness = round(2.0 + 8.0 * coverage, 1)
        communication = round((length_score + structure_score) / 2.0, 1)
        problem = round(3.0 + 7.0 * coverage, 1)
        overall = round(
            0.25 * relevance
            + 0.20 * technical
            + 0.15 * completeness
            + 0.15 * communication
            + 0.15 * problem
            + 0.10 * structure_score,
            1,
        )
        suggested = (
            f"A strong answer would cover {', '.join(expected_points[:3])} with a concrete "
            f"example related to {blueprint.target_role}."
        )
        return AdaptiveEvaluation(
            technical_accuracy=technical,
            relevance=relevance,
            communication=communication,
            confidence=5.0,
            completeness=completeness,
            problem_solving=problem,
            structure=round(structure_score, 1),
            overall_score=overall,
            strengths=matched or (["Answer used terms related to the role"] if coverage else []),
            weaknesses=missing or ["Add concrete evidence with measurable details"],
            missing_points=missing,
            incorrect_points=[],
            suggested_answer=suggested,
            ideal_answer=suggested,
            communication_feedback=(
                f"Answer length: {words} words. "
                + (
                    "Include more specific evidence and a clearer sequence."
                    if words < 100
                    else "The answer included sufficient detail; keep the structure explicit."
                )
            ),
            technical_feedback=(
                f"Evidence covered {len(matched)} of {len(expected_points)} expected points."
            ),
            knowledge_gaps=missing,
            demonstrated_strengths=matched,
            job_requirement_relevance=relevance,
            follow_up_needed=coverage < 0.6 or words < 100 or bool(missing),
            follow_up_focus=(
                missing[0] if missing else question.focus_skill or blueprint.target_role
            ),
            private_reasoning="Deterministic evidence evaluation; no AI provider response.",
        )

    def _finalize_evaluation(
        self,
        evaluation: AdaptiveEvaluation,
        question: AdaptiveQuestion,
        answer: str,
        config: InterviewConfig,
    ) -> AdaptiveEvaluation:
        evaluation.rubric_version = RUBRIC_VERSION
        evaluation.next_difficulty = self._derive_difficulty(evaluation, config.difficulty)
        if not evaluation.follow_up_focus:
            evaluation.follow_up_focus = (
                evaluation.knowledge_gaps[0] if evaluation.knowledge_gaps else question.focus_skill
            )
        if len(answer.strip()) < 100 and evaluation.overall_score >= 7:
            evaluation.overall_score = min(evaluation.overall_score, 6.5)
            evaluation.follow_up_needed = True
            evaluation.follow_up_focus = evaluation.follow_up_focus or "Provide specific evidence"
        evaluation.job_requirement_relevance = max(
            min(evaluation.job_requirement_relevance, evaluation.relevance),
            0.0,
        )
        return evaluation

    @staticmethod
    def _derive_difficulty(evaluation: AdaptiveEvaluation, fallback: str) -> str:
        score = evaluation.overall_score
        if score >= 7.5:
            return "hard"
        if score <= 4.5:
            return "easy"
        return fallback

    def generate_report(
        self,
        blueprint: InterviewBlueprint,
        config: InterviewConfig,
        turns: list[dict[str, Any]],
    ) -> JobInterviewReport:
        base = self._deterministic_report(blueprint, config, turns)
        prompt = _REPORT_PROMPT.format(
            job_title=blueprint.target_role,
            company=blueprint.company_context or "the target company",
            mode=config.mode,
            duration_minutes=config.duration_minutes,
            blueprint_json=blueprint.to_dict(),
            turns_json=[
                {
                    "question": item.get("question", ""),
                    "answer": str(item.get("answer", ""))[:1200],
                    "score": (item.get("evaluation") or {}).get("overall_score"),
                }
                for item in turns
            ],
        )
        try:
            narrative = self.llm.generate_structured(prompt, response_model=ReportNarrative)
            base.executive_summary = narrative.executive_summary.strip() or base.executive_summary
            base.readiness_level = narrative.readiness_level or base.readiness_level
            base.strengths = narrative.strengths or base.strengths
            base.areas_to_improve = narrative.areas_to_improve or base.areas_to_improve
            base.recommended_topics = narrative.recommended_topics or base.recommended_topics
            base.learning_plan = narrative.learning_plan or base.learning_plan
            base.skill_coverage = narrative.skill_coverage or base.skill_coverage
        except Exception as exc:
            logger.warning("Report narrative generation failed; using evidence summary: %s", exc)
        return base

    def _deterministic_report(
        self,
        blueprint: InterviewBlueprint,
        config: InterviewConfig,
        turns: list[dict[str, Any]],
    ) -> JobInterviewReport:
        reviews: list[AnswerReview] = []
        category_totals: dict[str, list[float]] = {}
        gaps: list[str] = []
        strengths: list[str] = []
        all_scores: list[float] = []
        for item in turns:
            evaluation = dict(item.get("evaluation") or {})
            score = float(evaluation.get("overall_score", 0))
            all_scores.append(score)
            question = dict(item.get("question_data") or {})
            category = str(question.get("category") or "Job-Specific")
            category_totals.setdefault(category, []).append(score)
            gaps.extend(str(x) for x in evaluation.get("knowledge_gaps", []) if x)
            strengths.extend(str(x) for x in evaluation.get("strengths", []) if x)
            reviews.append(
                AnswerReview(
                    question_number=int(item.get("number", len(reviews) + 1)),
                    category=category,
                    difficulty=str(question.get("difficulty") or "medium"),
                    question=str(item.get("question", "")),
                    answer=str(item.get("answer", "")),
                    score=score,
                    strengths=[str(x) for x in evaluation.get("strengths", [])],
                    weaknesses=[str(x) for x in evaluation.get("weaknesses", [])],
                    knowledge_gaps=[str(x) for x in evaluation.get("knowledge_gaps", [])],
                    suggested_answer=str(evaluation.get("suggested_answer", "")),
                    feedback=str(evaluation.get("technical_feedback", "")),
                    media_id=item.get("media_id"),
                )
            )
        overall = round(sum(all_scores) / len(all_scores), 2) if all_scores else 0.0
        readiness = (
            "strong"
            if overall >= 8
            else "job-ready" if overall >= 6.5 else "developing" if overall >= 4 else "beginner"
        )
        coverage: dict[str, str] = {}
        for topic in blueprint.technical_topics[:10]:
            topic_tokens = _tokens(topic)
            evidence = [str(item.get("answer", "")).lower() for item in turns]
            hits = sum(1 for text in evidence if topic_tokens & _tokens(text))
            coverage[topic] = (
                "strong" if hits >= 2 else "partial" if hits == 1 else "not demonstrated"
            )
        unique_gaps = list(dict.fromkeys(gaps))[:6]
        unique_strengths = list(dict.fromkeys(strengths))[:6]
        return JobInterviewReport(
            job_title=blueprint.target_role,
            company=blueprint.company_context or "Target company",
            interview_mode=config.mode,
            duration_minutes=config.duration_minutes,
            questions_answered=len(reviews),
            overall_score=overall,
            readiness_level=readiness,
            executive_summary=(
                f"The candidate answered {len(reviews)} job-grounded questions for "
                f"{blueprint.target_role} in {config.mode} mode with an average rubric score of "
                f"{overall:.1f}/10. Evidence is based on recorded answers, not on voice or video "
                "emotion analysis."
            ),
            category_scores={
                category: round(sum(values) / len(values), 2)
                for category, values in category_totals.items()
            },
            skill_coverage=coverage,
            strengths=unique_strengths,
            areas_to_improve=unique_gaps,
            recommended_topics=list(dict.fromkeys(unique_gaps + blueprint.technical_topics))[:8],
            learning_plan=[
                f"Review job requirement: {topic}" for topic in blueprint.technical_topics[:3]
            ]
            + [f"Prepare a concrete example covering: {topic}" for topic in unique_gaps[:2]],
            answers=reviews,
        )


class ReportNarrative(BaseModel):
    executive_summary: str = ""
    readiness_level: str = "developing"
    strengths: list[str] = Field(default_factory=list)
    areas_to_improve: list[str] = Field(default_factory=list)
    recommended_topics: list[str] = Field(default_factory=list)
    skill_coverage: dict[str, str] = Field(default_factory=dict)
    learning_plan: list[str] = Field(default_factory=list)
