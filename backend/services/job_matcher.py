import json

from config.settings import settings
from pydantic import BaseModel, Field
from services.job_analyzer import JobAnalysisResult
from services.llm_service import get_llm_service
from services.resume_analyzer import ResumeAnalysisResult


class SkillMatch(BaseModel):
    skill: str
    resume_evidence: str
    job_requirement: str
    match_strength: str


class MissingSkill(BaseModel):
    skill: str
    job_requirement: str
    importance: str
    learning_resources: list[str] = []


class ExperienceGap(BaseModel):
    area: str
    job_requirement: str
    resume_status: str
    gap_description: str


class ResumeJobMatchResult(BaseModel):
    technical_skills_match: float = Field(ge=0, le=100)
    experience_match: float = Field(ge=0, le=100)
    projects_match: float = Field(ge=0, le=100)
    education_match: float = Field(ge=0, le=100)
    overall_match: float = Field(ge=0, le=100)
    matching_skills: list[SkillMatch] = []
    missing_skills: list[MissingSkill] = []
    skill_gaps: list[str] = []
    experience_gaps: list[ExperienceGap] = []
    recommendations: list[str] = []


MATCHING_PROMPT = """You are an expert career coach and hiring manager. Compare the resume analysis with the job description analysis and provide a detailed match assessment.

RESUME ANALYSIS:
{resume_json}

JOB DESCRIPTION ANALYSIS:
{job_json}

Provide a comprehensive match analysis as JSON with:

1. technical_skills_match: Percentage (0-100) - How well technical skills align
2. experience_match: Percentage (0-100) - Experience relevance and depth
3. projects_match: Percentage (0-100) - Project relevance to the role
4. education_match: Percentage (0-100) - Education requirements satisfaction
5. overall_match: Weighted overall percentage (0-100)
6. matching_skills: Array of objects with skill, resume_evidence, job_requirement, match_strength
7. missing_skills: Array of objects with skill, job_requirement, importance, learning_resources
8. skill_gaps: List of strings describing specific skill gaps
9. experience_gaps: Array of objects with area, job_requirement, resume_status, gap_description
10. recommendations: List of actionable recommendation strings

CRITICAL: Return a single JSON OBJECT (not an array) with ALL 10 fields listed above. The top level must be an object, not an array. Do not omit any required fields.

Be honest and specific. Use evidence from both documents.

Return ONLY valid JSON matching the schema."""


RETRY_MATCHING_PROMPT = """You are an expert career coach. Compare a resume against a job description and output ONLY a single JSON object with these exact fields:

{
  "technical_skills_match": 75,
  "experience_match": 68,
  "projects_match": 80,
  "education_match": 85,
  "overall_match": 76,
  "matching_skills": [
    {
      "skill": "Python",
      "resume_evidence": "Built APIs in Python for five years",
      "job_requirement": "Python required",
      "match_strength": "strong"
    }
  ],
  "missing_skills": [
    {
      "skill": "Docker",
      "job_requirement": "Containerization experience",
      "importance": "important",
      "learning_resources": ["Docker getting started guide"]
    }
  ],
  "skill_gaps": ["No Kubernetes experience listed"],
  "experience_gaps": [
    {
      "area": "Cloud deployment",
      "job_requirement": "AWS experience",
      "resume_status": "Not mentioned",
      "gap_description": "No AWS projects listed"
    }
  ],
  "recommendations": ["Complete an introductory AWS deployment course"]
}

Use whole numbers from 0 to 100 for every score. Use empty arrays when a list has no entries.

Resume data: {resume_json}
Job data: {job_json}

Return ONLY the JSON object. No markdown, no explanation."""


def _job_match_limits() -> tuple[int, int]:
    return (
        max(1, int(settings.max_resume_chars)),
        max(1, int(settings.max_jd_chars)),
    )


class ResumeJobMatcher:
    def __init__(self):
        self.llm = get_llm_service()

    def match(self, resume: ResumeAnalysisResult, job: JobAnalysisResult) -> ResumeJobMatchResult:
        resume_limit, job_limit = _job_match_limits()
        resume_json = resume.model_dump_json(indent=2)[:resume_limit]
        job_json = job.model_dump_json(indent=2)[:job_limit]

        prompt = MATCHING_PROMPT.format(resume_json=resume_json, job_json=job_json)

        try:
            return self.llm.generate_structured(prompt, ResumeJobMatchResult)
        except Exception as primary_exc:
            retry_prompt = RETRY_MATCHING_PROMPT.replace("{resume_json}", resume_json).replace(
                "{job_json}", job_json
            )
            try:
                retry_response = self.llm.generate(retry_prompt, max_tokens=3000)
                parsed = json.loads(retry_response.content)
                if not isinstance(parsed, dict):
                    raise TypeError("Retry response was not a JSON object")
                return ResumeJobMatchResult(**parsed)
            except Exception as retry_exc:
                raise RuntimeError(
                    "Resume-job matching failed after structured generation and retry. "
                    f"Initial error: {primary_exc}. Retry error: {retry_exc}"
                ) from retry_exc


def match_resume_job(resume: ResumeAnalysisResult, job: JobAnalysisResult) -> ResumeJobMatchResult:
    matcher = ResumeJobMatcher()
    return matcher.match(resume, job)
