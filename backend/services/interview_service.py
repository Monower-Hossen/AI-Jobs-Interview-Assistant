from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from database import get_db_context
from database.models import InterviewQuestion, InterviewSession, JobDescription, ResumeAnalysis
from services.answer_evaluator import RUBRIC_VERSION, AnswerEvaluation
from services.auth_service import AuthError, require_owned_session_token
from services.interview_generator import Difficulty, QuestionCategory
from services.interview_generator import InterviewQuestion as GeneratedQuestion
from services.report_generator import InterviewReport

_ACTIVE_STATUSES = ("created", "in_progress")
_SCORE_FIELDS = (
    "technical_accuracy",
    "relevance",
    "communication",
    "confidence",
    "completeness",
    "problem_solving",
    "structure",
    "overall_score",
)
_FEEDBACK_FIELDS = (
    "strengths",
    "weaknesses",
    "missing_points",
    "incorrect_points",
    "suggested_answer",
    "ideal_answer",
    "communication_feedback",
    "technical_feedback",
    "rubric_version",
)


@dataclass
class ActiveInterview:
    id: int
    interview_type: str
    difficulty: str
    status: str
    total_questions: int
    current_question_index: int
    started_at: datetime | None
    created_at: datetime | None
    answer_mode: str


@dataclass
class InterviewDetail:
    session: dict[str, Any]
    questions: list[dict[str, Any]]
    evaluation_available: bool


@dataclass
class QuestionRecord:
    id: int
    question: GeneratedQuestion
    answer_text: str | None = None
    answer_audio_path: str | None = None
    answer_mode: str | None = None
    overall_score: float | None = None


def _resolve_session_id(session_token: str, actor_user_id: int | None) -> int:
    if not session_token:
        raise AuthError("Sign in to access this workspace.")
    if actor_user_id is not None:
        return require_owned_session_token(session_token, actor_user_id)
    with get_db_context() as db:
        from database.models import UserSession

        row = (
            db.query(UserSession)
            .filter(
                UserSession.session_id == session_token,
                UserSession.user_id.is_(None),
            )
            .first()
        )
        if row is None:
            raise AuthError("This workspace does not belong to the signed-in account.")
        return int(row.id)


def _category(value: Any) -> QuestionCategory:
    raw = getattr(value, "value", value)
    try:
        return QuestionCategory(str(raw).strip().lower().replace("-", "_").replace(" ", "_"))
    except ValueError:
        return QuestionCategory.TECHNICAL


def _difficulty(value: Any) -> Difficulty:
    raw = getattr(value, "value", value)
    try:
        return Difficulty(str(raw).strip().lower())
    except ValueError:
        return Difficulty.MEDIUM


def _text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _question_from_row(row: InterviewQuestion) -> GeneratedQuestion:
    return GeneratedQuestion(
        question_number=int(row.question_number),
        category=_category(row.category),
        difficulty=_difficulty(row.difficulty),
        question_text=str(row.question_text),
        expected_key_points=_text_list(row.expected_key_points),
    )


def get_active_interview(session_token: str, actor_user_id: int | None) -> ActiveInterview | None:
    session_id = _resolve_session_id(session_token, actor_user_id)
    with get_db_context() as db:
        row = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.session_id == session_id,
                InterviewSession.status.in_(_ACTIVE_STATUSES),
            )
            .order_by(InterviewSession.created_at.desc(), InterviewSession.id.desc())
            .first()
        )
        if row is None:
            return None
        return ActiveInterview(
            id=int(row.id),
            interview_type=str(row.interview_type or "Full Interview"),
            difficulty=str(row.difficulty or "medium"),
            status=str(row.status or "created"),
            total_questions=int(row.total_questions or 0),
            current_question_index=int(row.current_question_index or 0),
            started_at=row.started_at,
            created_at=row.created_at,
            answer_mode=str(row.answer_mode or "text"),
        )


def abandon_active_interviews(
    session_token: str, actor_user_id: int | None, except_id: int | None = None
) -> int:
    session_id = _resolve_session_id(session_token, actor_user_id)
    with get_db_context() as db:
        query = db.query(InterviewSession).filter(
            InterviewSession.session_id == session_id,
            InterviewSession.status.in_(_ACTIVE_STATUSES),
        )
        if except_id is not None:
            query = query.filter(InterviewSession.id != int(except_id))
        rows = query.all()
        for row in rows:
            row.status = "abandoned"
        return len(rows)


def start_interview(
    *,
    session_token: str,
    actor_user_id: int | None,
    questions: list[GeneratedQuestion],
    interview_type: str,
    difficulty: str,
    answer_mode: str = "text",
    total_questions: int | None = None,
) -> tuple[int, list[int]]:
    session_id = _resolve_session_id(session_token, actor_user_id)
    clean_type = str(interview_type or "Full Interview").strip()[:50]
    clean_difficulty = _difficulty(difficulty).value
    clean_mode = "voice" if answer_mode == "voice" else "text"
    question_count = total_questions if total_questions is not None else len(questions)
    if not clean_type:
        raise ValueError("Interview type is required.")
    if not 1 <= int(question_count) <= 50:
        raise ValueError("Question count must be between 1 and 50.")
    if not questions:
        raise ValueError("Generate questions before starting an interview.")

    abandon_active_interviews(session_token, actor_user_id)
    now = datetime.now()
    with get_db_context() as db:
        resume = (
            db.query(ResumeAnalysis)
            .filter(ResumeAnalysis.session_id == session_id)
            .order_by(ResumeAnalysis.created_at.desc())
            .first()
        )
        job = (
            db.query(JobDescription)
            .filter(JobDescription.session_id == session_id)
            .order_by(JobDescription.created_at.desc())
            .first()
        )
        interview = InterviewSession(
            session_id=session_id,
            resume_analysis_id=resume.id if resume else None,
            job_description_id=job.id if job else None,
            interview_type=clean_type,
            difficulty=clean_difficulty,
            total_questions=int(question_count),
            current_question_index=0,
            duration_seconds=0,
            status="in_progress",
            answer_mode=clean_mode,
            started_at=now,
        )
        db.add(interview)
        db.flush()
        question_ids: list[int] = []
        for question in questions:
            row = InterviewQuestion(
                interview_session_id=interview.id,
                question_number=int(question.question_number),
                category=_category(question.category).value,
                difficulty=_difficulty(question.difficulty).value,
                question_text=str(question.question_text).strip(),
                expected_key_points=_text_list(question.expected_key_points),
            )
            db.add(row)
            db.flush()
            question_ids.append(int(row.id))
        return int(interview.id), question_ids


def add_follow_up_question(
    *,
    interview_id: int,
    session_token: str,
    actor_user_id: int | None,
    question: GeneratedQuestion,
    question_number: int,
) -> int:
    session_id = _resolve_session_id(session_token, actor_user_id)
    with get_db_context() as db:
        interview = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.id == int(interview_id),
                InterviewSession.session_id == session_id,
            )
            .first()
        )
        if interview is None:
            raise AuthError("This interview is not accessible.")
        row = InterviewQuestion(
            interview_session_id=int(interview_id),
            question_number=int(question_number),
            category=_category(question.category).value,
            difficulty=_difficulty(question.difficulty).value,
            question_text=str(question.question_text).strip(),
            expected_key_points=_text_list(question.expected_key_points),
        )
        db.add(row)
        db.flush()
        return int(row.id)


def load_interview_questions(
    *, interview_id: int, session_token: str, actor_user_id: int | None
) -> tuple[list[GeneratedQuestion], list[int], list[dict[str, Any]]]:
    session_id = _resolve_session_id(session_token, actor_user_id)
    with get_db_context() as db:
        interview = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.id == int(interview_id),
                InterviewSession.session_id == session_id,
            )
            .first()
        )
        if interview is None:
            raise AuthError("This interview is not accessible.")
        rows = (
            db.query(InterviewQuestion)
            .filter(InterviewQuestion.interview_session_id == int(interview_id))
            .order_by(InterviewQuestion.question_number, InterviewQuestion.id)
            .all()
        )
        questions = [_question_from_row(row) for row in rows]
        question_ids = [int(row.id) for row in rows]
        answers = [
            {
                "answer": row.answer_text,
                "evaluation": _evaluation_from_row(row) if row.overall_score is not None else None,
            }
            for row in rows
        ]
        return questions, question_ids, answers


def _evaluation_from_row(row: InterviewQuestion) -> AnswerEvaluation:
    return AnswerEvaluation(
        technical_accuracy=float(row.technical_accuracy or 0.0),
        relevance=float(row.relevance or 0.0),
        communication=float(row.communication or 0.0),
        confidence=float(row.confidence or 0.0),
        completeness=float(row.completeness or 0.0),
        problem_solving=float(row.problem_solving or 5.0),
        structure=float(row.structure or 5.0),
        overall_score=float(row.overall_score or 0.0),
        strengths=_text_list(row.strengths),
        weaknesses=_text_list(row.weaknesses),
        missing_points=_text_list(row.missing_points),
        incorrect_points=_text_list(row.incorrect_points),
        suggested_answer=str(row.suggested_answer or ""),
        ideal_answer=str(row.ideal_answer or ""),
        communication_feedback=str(row.communication_feedback or ""),
        technical_feedback=str(row.technical_feedback or ""),
        rubric_version=str(row.rubric_version or RUBRIC_VERSION),
    )


def save_answer(
    *,
    interview_id: int,
    session_token: str,
    actor_user_id: int | None,
    question_id: int | None,
    answer: str,
    evaluation: AnswerEvaluation | None = None,
    answer_mode: str = "text",
    audio_path: str | None = None,
    current_question_index: int | None = None,
    total_questions: int | None = None,
) -> None:
    session_id = _resolve_session_id(session_token, actor_user_id)
    with get_db_context() as db:
        interview = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.id == int(interview_id),
                InterviewSession.session_id == session_id,
            )
            .first()
        )
        if interview is None:
            raise AuthError("This interview is not accessible.")
        row = None
        if question_id is not None:
            row = (
                db.query(InterviewQuestion)
                .filter(
                    InterviewQuestion.id == int(question_id),
                    InterviewQuestion.interview_session_id == int(interview_id),
                )
                .first()
            )
        if row is None:
            raise AuthError("This question is not part of the current interview.")
        row.answer_text = str(answer)
        row.answer_mode = "voice" if answer_mode == "voice" else "text"
        row.answer_audio_path = audio_path if row.answer_mode == "voice" else None
        row.answered_at = datetime.now()
        if evaluation is not None:
            for name in _SCORE_FIELDS + _FEEDBACK_FIELDS:
                value = getattr(evaluation, name, None)
                if value is not None:
                    setattr(row, name, value)
            row.evaluated_at = datetime.now()
        if current_question_index is not None:
            interview.current_question_index = max(0, int(current_question_index))
        if total_questions is not None:
            interview.total_questions = max(0, int(total_questions))


def save_progress(
    *,
    interview_id: int,
    session_token: str,
    actor_user_id: int | None,
    current_question_index: int,
    total_questions: int,
) -> None:
    session_id = _resolve_session_id(session_token, actor_user_id)
    with get_db_context() as db:
        interview = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.id == int(interview_id),
                InterviewSession.session_id == session_id,
            )
            .first()
        )
        if interview is None:
            raise AuthError("This interview is not accessible.")
        interview.current_question_index = max(0, int(current_question_index))
        interview.total_questions = max(0, int(total_questions))


def complete_interview(
    *,
    interview_id: int,
    session_token: str,
    actor_user_id: int | None,
    report: InterviewReport | None,
    current_question_index: int = 0,
    total_questions: int | None = None,
) -> None:
    session_id = _resolve_session_id(session_token, actor_user_id)
    now = datetime.now()
    with get_db_context() as db:
        interview = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.id == int(interview_id),
                InterviewSession.session_id == session_id,
            )
            .first()
        )
        if interview is None:
            raise AuthError("This interview is not accessible.")
        if interview.status != "completed":
            interview.status = "completed"
            interview.completed_at = now
            if interview.started_at:
                interview.duration_seconds = max(0, int((now - interview.started_at).total_seconds()))
        interview.current_question_index = max(0, int(current_question_index))
        if total_questions is not None:
            interview.total_questions = max(0, int(total_questions))
        if report is None:
            return
        interview.overall_score = float(report.overall_score)
        interview.technical_score = float(report.technical_score)
        interview.communication_score = float(report.communication_score)
        interview.hr_score = float(report.hr_score)
        interview.problem_solving_score = float(report.problem_solving_score)
        interview.strengths = list(report.strengths)
        interview.weaknesses = list(report.weaknesses)
        interview.missed_concepts = list(report.frequently_missed_concepts)
        interview.recommended_topics = list(report.recommended_topics)
        interview.recommended_projects = list(report.recommended_projects)
        interview.recommended_questions = list(report.recommended_questions)
        interview.improvement_plan_7_day = [item.model_dump() for item in report.improvement_plan_7_day]
        interview.improvement_plan_30_day = [
            item.model_dump() for item in report.improvement_plan_30_day
        ]


def get_interview_history(
    session_token: str,
    actor_user_id: int | None,
    *,
    search: str = "",
    interview_type: str = "all",
    status: str = "all",
    minimum_score: float = 0.0,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    session_id = _resolve_session_id(session_token, actor_user_id)
    needle = str(search or "").strip().lower()
    with get_db_context() as db:
        rows = (
            db.query(InterviewSession)
            .filter(InterviewSession.session_id == session_id)
            .order_by(InterviewSession.created_at.desc(), InterviewSession.id.desc())
            .limit(max(1, min(int(limit), 500)))
            .all()
        )
        history: list[dict[str, Any]] = []
        for row in rows:
            created = row.created_at
            if date_from and (created is None or created.date() < date_from):
                continue
            if date_to and (created is None or created.date() > date_to):
                continue
            if status != "all" and str(row.status) != status:
                continue
            if interview_type != "all" and str(row.interview_type) != interview_type:
                continue
            score = float(row.overall_score) if row.overall_score is not None else None
            if minimum_score > 0 and (score is None or score < minimum_score):
                continue
            label = f"Session #{row.id}"
            if created:
                label += f" · {created.strftime('%Y-%m-%d %H:%M')}"
            if score is not None:
                label += f" · {score:.1f}/10"
            if needle and needle not in label.lower():
                continue
            history.append(
                {
                    "id": int(row.id),
                    "Label": label,
                    "Type": str(row.interview_type or "N/A"),
                    "Difficulty": str(row.difficulty or "N/A"),
                    "Score": score,
                    "Status": str(row.status or "created").replace("_", " ").title(),
                    "Date": created.strftime("%Y-%m-%d %H:%M") if created else "Unknown",
                    "Duration": f"{int(row.duration_seconds or 0) // 60}m",
                }
            )
        return history


def get_interview_detail(
    *, interview_id: int, session_token: str, actor_user_id: int | None
) -> InterviewDetail:
    session_id = _resolve_session_id(session_token, actor_user_id)
    with get_db_context() as db:
        interview = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.id == int(interview_id),
                InterviewSession.session_id == session_id,
            )
            .first()
        )
        if interview is None:
            raise AuthError("This interview is not accessible.")
        rows = (
            db.query(InterviewQuestion)
            .filter(InterviewQuestion.interview_session_id == int(interview_id))
            .order_by(InterviewQuestion.question_number, InterviewQuestion.id)
            .all()
        )
        questions = []
        for row in rows:
            evaluation = _evaluation_from_row(row) if row.overall_score is not None else None
            questions.append(
                {
                    "number": int(row.question_number),
                    "category": str(row.category or "technical"),
                    "difficulty": str(row.difficulty or "medium"),
                    "question": str(row.question_text),
                    "expected_key_points": _text_list(row.expected_key_points),
                    "answer": row.answer_text or "",
                    "answer_mode": row.answer_mode or "text",
                    "score": float(row.overall_score) if row.overall_score is not None else None,
                    "evaluation": evaluation,
                }
            )
        session_data = {
            "id": int(interview.id),
            "type": str(interview.interview_type or "N/A"),
            "difficulty": str(interview.difficulty or "N/A"),
            "status": str(interview.status or "created"),
            "overall_score": (
                float(interview.overall_score) if interview.overall_score is not None else None
            ),
            "technical_score": (
                float(interview.technical_score) if interview.technical_score is not None else None
            ),
            "communication_score": (
                float(interview.communication_score)
                if interview.communication_score is not None
                else None
            ),
            "hr_score": float(interview.hr_score) if interview.hr_score is not None else None,
            "problem_solving_score": (
                float(interview.problem_solving_score)
                if interview.problem_solving_score is not None
                else None
            ),
            "started_at": interview.started_at,
            "completed_at": interview.completed_at,
            "created_at": interview.created_at,
            "duration_seconds": int(interview.duration_seconds or 0),
            "strengths": _text_list(interview.strengths),
            "weaknesses": _text_list(interview.weaknesses),
            "recommended_actions": _text_list(interview.recommended_topics)
            + _text_list(interview.recommended_projects)
            + _text_list(interview.recommended_questions),
        }
    return InterviewDetail(
        session=session_data,
        questions=questions,
        evaluation_available=any(item["evaluation"] is not None for item in questions)
        or session_data["overall_score"] is not None,
    )
