from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from database.db import get_db_context
from database.models import (
    InterviewAnswer,
    InterviewEvaluation,
    InterviewMedia,
    InterviewQuestion,
    InterviewSession,
    UserSession,
)
from pydantic import ValidationError
from services.adaptive_interview_engine import (
    AdaptiveEvaluation,
    AdaptiveInterviewEngine,
    AdaptiveQuestion,
    InterviewConfig,
)
from services.auth_service import log_event, require_owned_session_token
from services.job_blueprint import InterviewBlueprint, build_job_blueprint
from services.job_interview_constants import INTERVIEW_MODES, MAX_ANSWER_SECONDS
from services.job_post_service import get_job_post
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)


def _coerce_answer_start(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise JobInterviewError("Invalid answer start time.") from exc
    else:
        parsed = value
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed

_ACTIVE_STATUSES = {"created", "in_progress"}
_FOLLOWUP_CATEGORY = "Job-Specific"


class JobInterviewError(ValueError):
    """Raised when a job interview session cannot advance."""


@dataclass
class InterviewTurn:
    number: int
    question: AdaptiveQuestion
    answer: str = ""
    evaluation: dict[str, Any] | None = None
    media_id: int | None = None

    @property
    def is_answered(self) -> bool:
        return bool(self.answer.strip())


@dataclass
class JobInterviewState:
    interview_id: int
    job_id: int | None
    job_title: str
    company: str
    status: str
    consent_given: bool
    blueprint: InterviewBlueprint
    config: InterviewConfig
    turns: list[InterviewTurn] = field(default_factory=list)
    report: dict[str, Any] | None = None

    @property
    def current_question(self) -> AdaptiveQuestion | None:
        for turn in self.turns:
            if not turn.is_answered:
                return turn.question
        return None

    @property
    def current_turn(self) -> InterviewTurn | None:
        for turn in self.turns:
            if not turn.is_answered:
                return turn
        return None

    @property
    def answered_count(self) -> int:
        return sum(1 for turn in self.turns if turn.is_answered)

    @property
    def follow_up_count(self) -> int:
        return sum(1 for turn in self.turns if turn.question.follow_up_of is not None)

    @property
    def is_complete(self) -> bool:
        return self.status == "completed"


def _new_engine(engine: AdaptiveInterviewEngine | None) -> AdaptiveInterviewEngine:
    return engine or AdaptiveInterviewEngine()


def _target_for_duration(duration_minutes: int) -> int:
    return max(3, min(8, int(duration_minutes) // 4))


def _config_from_session(session: InterviewSession) -> InterviewConfig:
    blueprint = None
    if isinstance(session.blueprint_json, dict):
        blueprint = session.blueprint_json
    return InterviewConfig(
        mode=str(session.interview_mode or "text"),
        difficulty=str(session.difficulty or "medium"),
        duration_minutes=int(session.duration_minutes or 20),
        target_questions=int(session.total_questions or 5),
        question_preferences=[
            str(item.get("category"))
            for item in (blueprint or {}).get("suggested_question_plan", [])
            if isinstance(item, dict) and item.get("category")
        ][:8],
    )


def _question_to_row(question: AdaptiveQuestion, interview_id: int) -> InterviewQuestion:
    return InterviewQuestion(
        interview_session_id=int(interview_id),
        question_number=int(question.question_number),
        question_text=question.question_text,
        category=question.category.lower().replace("-", "_").replace(" ", "_"),
        difficulty=question.difficulty,
        expected_key_points=question.expected_key_points,
        is_follow_up=question.follow_up_of is not None,
        parent_question_id=question.follow_up_of,
        focus_skill=question.focus_skill[:120] or None,
        difficulty_adjustment=question.difficulty,
    )


def _row_to_question(row: InterviewQuestion) -> AdaptiveQuestion:
    raw = str(row.category or "job_specific").replace("_", "-").replace(" ", "-").title()
    raw = raw.replace("Hr", "HR")
    return AdaptiveQuestion(
        question_number=int(row.question_number),
        category=raw,
        difficulty=str(row.difficulty or "medium"),
        question_text=str(row.question_text),
        expected_key_points=list(row.expected_key_points or []),
        focus_skill=str(row.focus_skill or ""),
        follow_up_of=row.parent_question_id,
    )


def _history_from_state(state: JobInterviewState) -> list[dict[str, Any]]:
    return [
        {
            "number": turn.number,
            "question": turn.question.question_text,
            "answer": turn.answer,
            "question_data": turn.question.model_dump(mode="json"),
            "evaluation": turn.evaluation or {},
            "media_id": turn.media_id,
        }
        for turn in state.turns
        if turn.is_answered
    ]


def _verify_owned_interview(
    db: Any, interview_id: int, actor_user_id: int, session_token: str
) -> InterviewSession:
    workspace_id = require_owned_session_token(session_token, actor_user_id)
    row = (
        db.query(InterviewSession)
        .join(UserSession, UserSession.id == InterviewSession.session_id)
        .filter(
            InterviewSession.id == int(interview_id),
            UserSession.id == workspace_id,
            UserSession.user_id == int(actor_user_id),
        )
        .first()
    )
    if row is None:
        raise JobInterviewError("This interview does not belong to your workspace.")
    return row


def start_job_interview(
    *,
    session_token: str,
    actor_user_id: int,
    job_id: int,
    mode: str,
    difficulty: str,
    duration_minutes: int,
    consent_given: bool,
    engine: AdaptiveInterviewEngine | None = None,
) -> JobInterviewState:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in INTERVIEW_MODES:
        raise JobInterviewError("Unsupported interview mode.")
    if not consent_given:
        raise JobInterviewError("Give consent before starting a recorded interview.")
    workspace_id = require_owned_session_token(session_token, actor_user_id)
    ai = _new_engine(engine)
    job = get_job_post(int(job_id), require_published=True)
    blueprint = build_job_blueprint(job, llm=ai.llm)
    config = InterviewConfig(
        mode=normalized_mode,
        difficulty=str(difficulty or "medium").lower(),
        duration_minutes=int(duration_minutes),
        target_questions=_target_for_duration(int(duration_minutes)),
        question_preferences=[item.category for item in blueprint.suggested_question_plan[:8]],
    )
    question = ai.next_question(blueprint, config, [])
    now = datetime.now(UTC).replace(tzinfo=None)
    with get_db_context() as db:
        active = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.session_id == workspace_id,
                InterviewSession.is_job_interview.is_(True),
                InterviewSession.status.in_(list(_ACTIVE_STATUSES)),
            )
            .all()
        )
        for stale in active:
            stale.status = "abandoned"
            stale.completed_at = now
        session = InterviewSession(
            session_id=workspace_id,
            job_post_id=int(job_id),
            interview_type="job",
            difficulty=config.difficulty,
            total_questions=config.target_questions,
            current_question_index=0,
            status="in_progress",
            started_at=now,
            interview_mode=config.mode,
            duration_minutes=config.duration_minutes,
            consent_given=True,
            is_job_interview=True,
            blueprint_json=blueprint.to_dict(),
        )
        db.add(session)
        db.flush()
        db.add(_question_to_row(question, session.id))
        db.flush()
        interview_id = int(session.id)
    log_event("interview.job_started", f"Job interview started for {job.title}", actor_user_id)
    return get_job_interview_state(interview_id, actor_user_id, session_token, engine=ai)


def get_job_interview_state(
    interview_id: int,
    actor_user_id: int,
    session_token: str,
    *,
    engine: AdaptiveInterviewEngine | None = None,
) -> JobInterviewState:
    with get_db_context() as db:
        session = _verify_owned_interview(db, interview_id, actor_user_id, session_token)
        question_rows = (
            db.query(InterviewQuestion)
            .filter(InterviewQuestion.interview_session_id == session.id)
            .order_by(InterviewQuestion.question_number, InterviewQuestion.id)
            .all()
        )
        answer_rows = (
            db.query(InterviewAnswer)
            .filter(InterviewAnswer.interview_session_id == session.id)
            .all()
        )
        evaluation_rows = (
            db.query(InterviewEvaluation)
            .filter(InterviewEvaluation.interview_session_id == session.id)
            .all()
        )
        media_rows = (
            db.query(InterviewMedia)
            .filter(
                InterviewMedia.interview_session_id == session.id,
                InterviewMedia.answer_id.is_not(None),
            )
            .all()
        )
        answers = {row.question_id: row for row in answer_rows}
        evaluations = {row.answer_id: row for row in evaluation_rows}
        media_by_answer = {row.answer_id: row for row in media_rows}
        turns: list[InterviewTurn] = []
        for row in question_rows:
            question = _row_to_question(row)
            answer = answers.get(row.id)
            evaluation = evaluations.get(answer.id) if answer else None
            media = media_by_answer.get(answer.id) if answer else None
            eval_payload: dict[str, Any] | None = None
            if evaluation is not None:
                eval_payload = {
                    "overall_score": evaluation.scores.get("overall_score", 0),
                    "next_difficulty": evaluation.next_difficulty,
                    "follow_up_needed": bool(evaluation.follow_up_needed),
                    "follow_up_focus": evaluation.follow_up_focus,
                    "knowledge_gaps": evaluation.knowledge_gaps or [],
                    "suggested_answer": evaluation.suggested_answer or "",
                    "technical_feedback": (evaluation.feedback or {}).get("technical_feedback", ""),
                }
            turns.append(
                InterviewTurn(
                    number=int(row.question_number),
                    question=question,
                    answer=str(answer.answer_text) if answer else "",
                    evaluation=eval_payload,
                    media_id=int(media.id) if media else None,
                )
            )
        raw_blueprint = session.blueprint_json
        try:
            blueprint = InterviewBlueprint.model_validate(raw_blueprint or {})
        except ValidationError as exc:
            raise JobInterviewError("Interview blueprint is unavailable.") from exc
        config = _config_from_session(session)
        report = session.report_json if isinstance(session.report_json, dict) else None
        return JobInterviewState(
            interview_id=int(session.id),
            job_id=session.job_post_id,
            job_title=str((raw_blueprint or {}).get("target_role") or "Job interview"),
            company=str((raw_blueprint or {}).get("company_context") or "Target company"),
            status=str(session.status),
            consent_given=bool(session.consent_given),
            blueprint=blueprint,
            config=config,
            turns=turns,
            report=report,
        )


def submit_job_answer(
    *,
    interview_id: int,
    question_id: int,
    answer: str,
    media_id: int | None,
    actor_user_id: int,
    session_token: str,
    answer_started_at: datetime | str | None = None,
    engine: AdaptiveInterviewEngine | None = None,
) -> JobInterviewState:
    text = str(answer or "").strip()
    if not text:
        raise JobInterviewError("Please provide an answer before submitting.")
    ai = _new_engine(engine)
    state = get_job_interview_state(interview_id, actor_user_id, session_token, engine=ai)
    if state.is_complete:
        raise JobInterviewError("This interview is already completed.")
    turn = state.current_turn
    if turn is None or turn.question.question_number != int(question_id):
        raise JobInterviewError("That question is not the current question.")
    evaluation = ai.evaluate_answer(turn.question, text, state.blueprint, state.config)
    now = datetime.now(UTC).replace(tzinfo=None)
    saved_evaluation = AdaptiveEvaluation.model_validate(evaluation.model_dump())
    history = _history_from_state(state)
    history.append(
        {
            "number": int(question_id),
            "question": turn.question.question_text,
            "answer": text,
            "question_data": turn.question.model_dump(mode="json"),
            "evaluation": saved_evaluation.model_dump(mode="json"),
            "media_id": None,
        }
    )
    answered_count = state.answered_count + 1
    follow_up_count = state.follow_up_count + (
        1 if turn.question.follow_up_of is not None else 0
    )
    next_question: AdaptiveQuestion | None = None
    should_follow_up = (
        bool(evaluation.follow_up_needed)
        and follow_up_count < state.config.follow_up_budget
        and answered_count < state.config.target_questions + state.config.follow_up_budget
    )
    if should_follow_up:
        next_question = ai.next_question(
            state.blueprint,
            state.config,
            history,
            follow_up_of=int(question_id),
            follow_up_focus=evaluation.follow_up_focus,
            previous_answer=text,
        )
    elif answered_count < state.config.target_questions:
        next_question = ai.next_question(state.blueprint, state.config, history)
    session_id: int
    try:
        with get_db_context() as db:
            session = _verify_owned_interview(db, interview_id, actor_user_id, session_token)
            question_row = (
                db.query(InterviewQuestion)
                .filter(
                    InterviewQuestion.question_number == int(question_id),
                    InterviewQuestion.interview_session_id == session.id,
                )
                .first()
            )
            if question_row is None or question_row.answer_text:
                raise JobInterviewError("This question has already been answered.")
            started_at = (
                _coerce_answer_start(answer_started_at)
                if answer_started_at is not None
                else question_row.asked_at
            )
            if started_at is not None:
                elapsed_seconds = (
                    datetime.now(UTC).replace(tzinfo=None) - started_at
                ).total_seconds()
                if elapsed_seconds > MAX_ANSWER_SECONDS:
                    raise JobInterviewError(
                        f"Answer time limit of {MAX_ANSWER_SECONDS // 60} minutes exceeded."
                    )
            if media_id is not None:
                media_row = (
                    db.query(InterviewMedia)
                    .filter(
                        InterviewMedia.id == int(media_id),
                        InterviewMedia.owner_user_id == int(actor_user_id),
                        InterviewMedia.interview_session_id == session.id,
                    )
                    .first()
                )
                if media_row is None:
                    raise JobInterviewError("Recording not found for this interview.")
            answer_row = InterviewAnswer(
                interview_session_id=session.id,
                question_id=question_row.id,
                answer_text=text,
                transcription_source=(
                    "voice"
                    if state.config.mode == "voice"
                    else "video" if state.config.mode == "video" else "text"
                ),
            )
            db.add(answer_row)
            db.flush()
            if media_id is not None:
                media_row = (
                    db.query(InterviewMedia).filter(InterviewMedia.id == int(media_id)).first()
                )
                if media_row:
                    media_row.answer_id = answer_row.id
            base = evaluation.as_base()
            scores = {
                **base.model_dump(mode="json"),
                "knowledge_gaps": evaluation.knowledge_gaps,
                "demonstrated_strengths": evaluation.demonstrated_strengths,
                "job_requirement_relevance": evaluation.job_requirement_relevance,
            }
            db.add(
                InterviewEvaluation(
                    interview_session_id=session.id,
                    answer_id=answer_row.id,
                    question_id=question_row.id,
                    scores=scores,
                    knowledge_gaps=evaluation.knowledge_gaps,
                    demonstrated_strengths=evaluation.demonstrated_strengths,
                    job_requirement_relevance=evaluation.job_requirement_relevance,
                    next_difficulty=evaluation.next_difficulty,
                    follow_up_needed=bool(evaluation.follow_up_needed),
                    follow_up_focus=evaluation.follow_up_focus,
                    suggested_answer=evaluation.suggested_answer,
                    feedback={
                        "communication_feedback": evaluation.communication_feedback,
                        "technical_feedback": evaluation.technical_feedback,
                        "private_reasoning": evaluation.private_reasoning,
                    },
                    rubric_version=evaluation.rubric_version,
                )
            )
            question_row.answer_text = text
            question_row.overall_score = float(evaluation.overall_score)
            question_row.strengths = evaluation.strengths
            question_row.weaknesses = evaluation.weaknesses
            question_row.missing_points = list(evaluation.knowledge_gaps)
            question_row.communication_feedback = evaluation.communication_feedback
            question_row.ideal_answer = evaluation.suggested_answer
            question_row.answered_at = now
            session.current_question_index = answered_count
            session.answer_mode = state.config.mode
            answered_scores = [
                float((item.evaluation or {}).get("overall_score", 0))
                for item in state.turns
                if item.evaluation
            ]
            answered_scores.append(float(evaluation.overall_score))
            session.overall_score = round(sum(answered_scores) / len(answered_scores), 2)
            session.duration_seconds = int(
                max(0, (now - (session.started_at or now)).total_seconds())
            )
            if next_question is not None:
                max_number = (
                    db.query(InterviewQuestion)
                    .filter(InterviewQuestion.interview_session_id == session.id)
                    .count()
                )
                next_question.question_number = int(max_number) + 1
                db.add(_question_to_row(next_question, session.id))
            db.flush()
            session_id = int(session.id)
    except IntegrityError as exc:
        raise JobInterviewError("This question has already been answered.") from exc
    return get_job_interview_state(session_id, actor_user_id, session_token, engine=ai)



def complete_job_interview(
    *,
    interview_id: int,
    actor_user_id: int,
    session_token: str,
    engine: AdaptiveInterviewEngine | None = None,
) -> dict[str, Any]:
    ai = _new_engine(engine)
    state = get_job_interview_state(interview_id, actor_user_id, session_token, engine=ai)
    if state.is_complete:
        return state.report or {}
    if state.answered_count == 0:
        raise JobInterviewError("Answer at least one question before generating the report.")
    turns = _history_from_state(state)
    report = ai.generate_report(state.blueprint, state.config, turns)
    payload = report.to_dict()
    now = datetime.now(UTC).replace(tzinfo=None)
    with get_db_context() as db:
        session = _verify_owned_interview(db, interview_id, actor_user_id, session_token)
        if session.status == "completed" and session.report_json:
            return dict(session.report_json)
        session.status = "completed"
        session.completed_at = now
        session.report_json = payload
        session.overall_score = float(report.overall_score)
        session.strengths = report.strengths
        session.weaknesses = report.areas_to_improve
        session.missed_concepts = [gap for item in report.answers for gap in item.knowledge_gaps][
            :20
        ]
        session.recommended_topics = report.recommended_topics
        session.technical_score = report.category_scores.get("Technical", 0)
        session.hr_score = report.category_scores.get("HR", 0)
        session.problem_solving_score = report.category_scores.get("Problem-Solving", 0)
        db.flush()
    log_event(
        "interview.job_completed", f"Job interview completed: {report.job_title}", actor_user_id
    )
    return payload


def get_job_interview_report(
    interview_id: int, *, actor_user_id: int, session_token: str
) -> dict[str, Any]:
    state = get_job_interview_state(interview_id, actor_user_id, session_token)
    if not state.is_complete or not state.report:
        raise JobInterviewError("This interview report is not available yet.")
    return state.report


def list_job_interviews(
    *, actor_user_id: int, session_token: str, completed: bool | None = None
) -> list[dict[str, Any]]:
    workspace_id = require_owned_session_token(session_token, actor_user_id)
    with get_db_context() as db:
        query = db.query(InterviewSession).filter(
            InterviewSession.session_id == workspace_id,
            InterviewSession.is_job_interview.is_(True),
        )
        if completed is True:
            query = query.filter(InterviewSession.status == "completed")
        elif completed is False:
            query = query.filter(InterviewSession.status.in_(list(_ACTIVE_STATUSES)))
        rows = query.order_by(InterviewSession.created_at.desc(), InterviewSession.id.desc()).all()
        results: list[dict[str, Any]] = []
        for row in rows:
            blueprint = row.blueprint_json or {}
            results.append(
                {
                    "id": int(row.id),
                    "job_title": str(blueprint.get("target_role") or "Job interview"),
                    "company": str(blueprint.get("company_context") or "Target company"),
                    "mode": str(row.interview_mode or "text"),
                    "status": str(row.status),
                    "overall_score": row.overall_score,
                    "created_at": row.created_at,
                    "completed_at": row.completed_at,
                }
            )
        return results


def abandon_job_interview(*, interview_id: int, actor_user_id: int, session_token: str) -> None:
    with get_db_context() as db:
        session = _verify_owned_interview(db, interview_id, actor_user_id, session_token)
        if session.status not in _ACTIVE_STATUSES:
            raise JobInterviewError("Only active interviews can be abandoned.")
        session.status = "abandoned"
        session.completed_at = datetime.now(UTC).replace(tzinfo=None)
        db.flush()
