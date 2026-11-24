from database import (
    InterviewQuestion,
    InterviewSession,
    LearningNote,
    QuestionBookmark,
    get_db_context,
)
from services.auth_service import require_owned_session_id


def _normalized_question_text(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def list_bookmarks(session_id: int, actor_user_id: int | None) -> list[dict]:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        rows = (
            db.query(QuestionBookmark)
            .filter(QuestionBookmark.session_id == session_id)
            .order_by(QuestionBookmark.created_at.desc(), QuestionBookmark.id.desc())
            .all()
        )
        return [
            {
                "id": row.id,
                "Question": row.question_text,
                "Category": row.category,
                "Difficulty": row.difficulty,
                "Source": row.source,
                "Key points": ", ".join(row.expected_key_points or []),
            }
            for row in rows
        ]


def list_asked_questions(session_id: int, actor_user_id: int | None) -> list[dict]:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        rows = (
            db.query(InterviewQuestion)
            .join(
                InterviewSession,
                InterviewQuestion.interview_session_id == InterviewSession.id,
            )
            .filter(InterviewSession.session_id == session_id)
            .order_by(InterviewQuestion.asked_at.desc(), InterviewQuestion.id.desc())
            .all()
        )
        return [
            {
                "id": int(row.id),
                "question_number": int(row.question_number or 0),
                "question_text": str(row.question_text or ""),
                "category": str(row.category or "technical"),
                "difficulty": str(row.difficulty or "medium"),
                "overall_score": row.overall_score,
                "expected_key_points": list(row.expected_key_points or []),
                "suggested_answer": str(row.suggested_answer or ""),
                "asked_at": row.asked_at,
            }
            for row in rows
        ]


def add_bookmark(
    session_id: int,
    question_text: str,
    category: str = "technical",
    difficulty: str = "medium",
    expected_key_points: list[str] | None = None,
    source: str = "generated",
    actor_user_id: int | None = None,
) -> int:
    require_owned_session_id(session_id, actor_user_id)
    text = question_text.strip()
    if not text:
        raise ValueError("Question text is required.")
    if len(text) > 4000:
        raise ValueError("Question text is too long.")
    with get_db_context() as db:
        normalized = _normalized_question_text(text)
        existing_rows = (
            db.query(QuestionBookmark)
            .filter(QuestionBookmark.session_id == session_id)
            .all()
        )
        for existing in existing_rows:
            if _normalized_question_text(existing.question_text) == normalized:
                return int(existing.id)
        row = QuestionBookmark(
            session_id=session_id,
            question_text=text,
            category=category[:50] or "technical",
            difficulty=difficulty[:20] or "medium",
            expected_key_points=list(expected_key_points or []),
            source=source[:30] or "generated",
        )
        db.add(row)
        db.flush()
        return int(row.id)


def delete_bookmark(bookmark_id: int, session_id: int, actor_user_id: int | None) -> bool:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        row = (
            db.query(QuestionBookmark)
            .filter(
                QuestionBookmark.id == bookmark_id,
                QuestionBookmark.session_id == session_id,
            )
            .first()
        )
        if not row:
            return False
        db.delete(row)
        return True


def list_notes(session_id: int, actor_user_id: int | None) -> list[dict]:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        rows = (
            db.query(LearningNote)
            .filter(LearningNote.session_id == session_id)
            .order_by(LearningNote.updated_at.desc(), LearningNote.id.desc())
            .all()
        )
        return [
            {
                "id": row.id,
                "Title": row.title,
                "Topic": row.topic or "General",
                "Notes": row.content or "",
                "Updated": row.updated_at.strftime("%Y-%m-%d") if row.updated_at else "",
            }
            for row in rows
        ]


def save_note(
    session_id: int,
    title: str,
    content: str,
    topic: str = "",
    note_id: int | None = None,
    actor_user_id: int | None = None,
) -> int:
    require_owned_session_id(session_id, actor_user_id)
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("A note title is required.")
    if len(clean_title) > 200:
        raise ValueError("Note title is too long.")
    with get_db_context() as db:
        row = None
        if note_id is not None:
            row = (
                db.query(LearningNote)
                .filter(LearningNote.id == note_id, LearningNote.session_id == session_id)
                .first()
            )
        if row is None:
            row = LearningNote(session_id=session_id, title=clean_title)
            db.add(row)
        row.title = clean_title
        row.content = content
        row.topic = topic[:120]
        db.flush()
        return int(row.id)


def delete_note(note_id: int, session_id: int, actor_user_id: int | None) -> bool:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        row = (
            db.query(LearningNote)
            .filter(LearningNote.id == note_id, LearningNote.session_id == session_id)
            .first()
        )
        if not row:
            return False
        db.delete(row)
        return True
