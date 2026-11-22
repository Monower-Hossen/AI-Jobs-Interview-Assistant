from pydantic import BaseModel, Field
from services.interview_generator import InterviewQuestion
from services.job_analyzer import JobAnalysisResult
from services.llm_service import get_llm_service
from services.resume_analyzer import ResumeAnalysisResult

RUBRIC_VERSION = "v2"


class AnswerEvaluation(BaseModel):
    """Structured, model-generated evaluation. Scores are advisory, not objective truth."""

    technical_accuracy: float = Field(ge=0, le=10)
    relevance: float = Field(ge=0, le=10)
    communication: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=10)
    completeness: float = Field(ge=0, le=10)
    problem_solving: float = Field(default=5.0, ge=0, le=10)
    structure: float = Field(default=5.0, ge=0, le=10)
    overall_score: float = Field(ge=0, le=10)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    missing_points: list[str] = Field(default_factory=list)
    incorrect_points: list[str] = Field(default_factory=list)
    suggested_answer: str
    ideal_answer: str = ""
    communication_feedback: str
    technical_feedback: str
    rubric_version: str = RUBRIC_VERSION

    def dimension_scores(self) -> dict[str, float]:
        return {
            "Relevance": self.relevance,
            "Technical accuracy": self.technical_accuracy,
            "Completeness": self.completeness,
            "Communication": self.communication,
            "Problem solving": self.problem_solving,
            "Structure": self.structure,
        }


ANSWER_EVALUATION_PROMPT = """You are an expert interview evaluator. Evaluate the candidate's answer to the interview question.

QUESTION:
Category: {category}
Difficulty: {difficulty}
Question: {question}
Expected Key Points: {key_points}

CANDIDATE'S ANSWER:
{answer}

CANDIDATE CONTEXT:
{resume_context}

JOB CONTEXT:
{job_context}

Evaluate the answer across these seven dimensions (0-10 each):

1. technical_accuracy: Correctness of technical content (N/A for non-technical questions - use 5 as neutral)
2. relevance: How well the answer addresses the question
3. communication: Clarity, structure, and articulation
4. confidence: Inferred confidence from answer quality (text only - be conservative)
5. completeness: Coverage of expected key points
6. problem_solving: Reasoning, trade-offs, and approach to the problem
7. structure: Organization of the answer, logical flow, and use of examples
8. overall_score: Weighted overall score

Also provide:
- strengths: What was good in the answer
- weaknesses: What could be improved
- missing_points: Expected points not covered
- incorrect_points: Any factual errors or misconceptions
- suggested_answer: A stronger example answer the candidate could adapt
- ideal_answer: A concise model answer that fully covers the key points
- communication_feedback: Specific feedback on communication style
- technical_feedback: Specific feedback on technical content

IMPORTANT:
- For non-technical categories (HR, behavioral, situational), set technical_accuracy to 5 (neutral)
- For confidence, only infer from text quality - do not overclaim. Do not present scores as objective ground truth.
- Be honest and constructive
- If answer is empty/very short, score accordingly

Return ONLY valid JSON matching the schema."""

STAR_PROMPT_SUFFIX = """
BEHAVIORAL / STAR MODE:
Judge the answer specifically against the STAR structure:
- Situation: was the context and setting established?
- Task: was the candidate's responsibility clearly stated?
- Action: were concrete actions taken by the candidate described (not just the team)?
- Result: was a measurable or concrete outcome shared?
Reflect missing STAR components in the weaknesses and missing_points fields and in the suggested_answer.
For technical_accuracy use the neutral value 5 unless the answer contains factual claims."""


class AnswerEvaluator:
    def __init__(self):
        self.llm = get_llm_service()

    def evaluate(
        self,
        question: InterviewQuestion,
        answer: str,
        resume: ResumeAnalysisResult,
        job: JobAnalysisResult,
        mode: str = "standard",
    ) -> AnswerEvaluation:

        resume_context = f"""
Candidate: {resume.candidate_name or 'Not specified'}
Experience: {len(resume.experience)} roles
Key Skills: {', '.join(resume.skills[:15])}
Projects: {len(resume.projects)}
"""

        job_context = f"""
Target Role: {job.job_title or 'Not specified'}
Required Skills: {', '.join(job.required_skills[:10])}
Key Responsibilities: {', '.join(job.responsibilities[:5])}
"""

        prompt = ANSWER_EVALUATION_PROMPT.format(
            category=question.category.value,
            difficulty=question.difficulty.value,
            question=question.question_text,
            key_points=", ".join(question.expected_key_points),
            answer=answer if answer.strip() else "[No answer provided]",
            resume_context=resume_context,
            job_context=job_context,
        )
        if mode == "behavioral":
            prompt += STAR_PROMPT_SUFFIX

        result = self.llm.generate_structured(prompt, AnswerEvaluation)
        result.rubric_version = RUBRIC_VERSION
        return result

    def evaluate_batch(
        self,
        questions: list[InterviewQuestion],
        answers: list[str],
        resume: ResumeAnalysisResult,
        job: JobAnalysisResult,
        mode: str = "standard",
    ) -> list[AnswerEvaluation]:
        evaluations = []
        for question, answer in zip(questions, answers):
            evaluations.append(self.evaluate(question, answer, resume, job, mode=mode))
        return evaluations


def evaluate_answer(
    question: InterviewQuestion,
    answer: str,
    resume: ResumeAnalysisResult,
    job: JobAnalysisResult,
    mode: str = "standard",
) -> AnswerEvaluation:
    evaluator = AnswerEvaluator()
    return evaluator.evaluate(question, answer, resume, job, mode=mode)
