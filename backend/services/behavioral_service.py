"""Behavioral interview practice built around the STAR framework."""

from pydantic import BaseModel, Field
from services.llm_service import get_llm_service

CATEGORIES = [
    "Leadership",
    "Teamwork",
    "Conflict resolution",
    "Problem solving",
    "Situational",
    "Strengths and weaknesses",
    "Communication",
]


class BehavioralQuestion(BaseModel):
    question_number: int = 1
    question_text: str
    category: str = "behavioral"
    difficulty: str = "medium"
    expected_key_points: list[str] = Field(default_factory=list)
    follow_up_hints: list[str] = Field(default_factory=list)

    def to_interview_question(self):
        from services.interview_generator import Difficulty, InterviewQuestion, QuestionCategory

        return InterviewQuestion(
            question_number=self.question_number,
            category=QuestionCategory.BEHAVIORAL,
            difficulty=Difficulty(
                self.difficulty if self.difficulty in {"easy", "medium", "hard"} else "medium"
            ),
            question_text=self.question_text,
            expected_key_points=self.expected_key_points,
            follow_up_hints=self.follow_up_hints,
        )


class BehavioralQuestionSet(BaseModel):
    questions: list[BehavioralQuestion] = Field(default_factory=list)


PROMPT = """Generate {count} behavioral interview questions focused on: {category}.
Difficulty: {difficulty}. Questions should require concrete past examples or situational reasoning.
For each question provide:
- question_number, question_text, category ("behavioral"), difficulty
- expected_key_points: 3-5 checkpoints mapped to Situation, Task, Action, Result
- follow_up_hints: 2 probing follow-ups

Return JSON with a "questions" array."""


STAR_EXAMPLE_PROMPT = """Rewrite the candidate's behavioral answer into a strong STAR-format example answer.

QUESTION: {question}
ORIGINAL ANSWER: {answer}
EVALUATION FEEDBACK: {feedback}

Write four clearly labeled sections: Situation, Task, Action, Result.
Keep the candidate's real context; do not invent achievements or metrics."""


class BehavioralService:
    def __init__(self):
        self.llm = get_llm_service()

    def generate(self, category: str, difficulty: str, count: int = 3) -> list[BehavioralQuestion]:
        if category not in CATEGORIES:
            raise ValueError("Unsupported behavioral category.")
        prompt = PROMPT.format(count=count, category=category, difficulty=difficulty)
        result = self.llm.generate_structured(prompt, BehavioralQuestionSet)
        questions = [question for question in result.questions if question.question_text.strip()][
            :count
        ]
        if not questions:
            raise RuntimeError("No behavioral questions were generated. Please try again.")
        return questions

    def star_example(self, question: str, answer: str, feedback: str) -> str:
        prompt = STAR_EXAMPLE_PROMPT.format(question=question, answer=answer, feedback=feedback)
        return self.llm.generate(prompt).content
