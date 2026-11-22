"""LLM-based technical assessment. Submitted code is never executed on the server."""

from pydantic import BaseModel, Field
from services.llm_service import get_llm_service

TOPICS = [
    "Python",
    "Data Structures and Algorithms",
    "Machine Learning",
    "Deep Learning",
    "NLP and Generative AI",
    "SQL",
    "Databases",
    "System Design",
    "Code Explanation",
]


class AssessmentQuestion(BaseModel):
    question_number: int
    topic: str
    difficulty: str
    prompt: str
    language: str = "text"
    expected_concepts: list[str] = Field(default_factory=list)
    starter_code: str = ""


class AssessmentQuestionSet(BaseModel):
    questions: list[AssessmentQuestion] = Field(default_factory=list)


class CodeFeedback(BaseModel):
    correctness: float = Field(ge=0, le=10)
    efficiency: float = Field(ge=0, le=10)
    readability: float = Field(ge=0, le=10)
    score: float = Field(ge=0, le=10)
    strengths: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    time_complexity: str = ""
    space_complexity: str = ""
    explanation: str = ""
    improved_solution: str = ""

    def dimensions(self) -> dict[str, float]:
        return {
            "Correctness": self.correctness,
            "Efficiency": self.efficiency,
            "Readability": self.readability,
        }


QUESTION_PROMPT = """Create {count} {difficulty} technical assessment questions on: {topic}.
Include a mix of conceptual questions, practical coding tasks, and reasoning about trade-offs.
For coding tasks, provide a short starter snippet when useful.

Return JSON with a "questions" array. Each item must have:
question_number (int), topic (string), difficulty (string), prompt (string),
language (python/sql/text), expected_concepts (array of strings), starter_code (string).
Do not include solutions in the questions."""

EVALUATION_PROMPT = """You are a senior engineer reviewing a candidate's technical assessment answer.

TOPIC: {topic}
DIFFICULTY: {difficulty}
QUESTION:
{question}

EXPECTED CONCEPTS:
{concepts}

CANDIDATE ANSWER:
{answer}

Evaluate statically. NEVER claim to have executed the code. Provide:
- correctness, efficiency, readability and overall score from 0 to 10
- strengths and issues (be specific and actionable)
- time_complexity and space_complexity estimates (state when unknown)
- explanation of the key concepts
- improved_solution with a correct reference answer

Treat the scores as advisory estimates, not objective ground truth.
Return ONLY valid JSON matching the schema."""


class TechnicalAssessmentService:
    def __init__(self):
        self.llm = get_llm_service()

    def generate(self, topic: str, difficulty: str, count: int = 3) -> list[AssessmentQuestion]:
        if topic not in TOPICS:
            raise ValueError("Unsupported assessment topic.")
        prompt = QUESTION_PROMPT.format(count=count, difficulty=difficulty, topic=topic)
        result = self.llm.generate_structured(prompt, AssessmentQuestionSet)
        questions = [question for question in result.questions if question.prompt.strip()][:count]
        if not questions:
            raise RuntimeError("No assessment questions were generated. Please try again.")
        return questions

    def evaluate(self, question: AssessmentQuestion, answer: str) -> CodeFeedback:
        prompt = EVALUATION_PROMPT.format(
            topic=question.topic,
            difficulty=question.difficulty,
            question=question.prompt,
            concepts=", ".join(question.expected_concepts),
            answer=answer if answer.strip() else "[No answer provided]",
        )
        return self.llm.generate_structured(prompt, CodeFeedback)
