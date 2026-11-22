import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from database import JobDescription, ResumeAnalysis, ResumeJobMatch, get_db_context
from pydantic import BaseModel, Field
from rag import load_document
from services.auth_service import AuthError, require_owned_session_id, require_owned_session_token
from services.job_analyzer import JobAnalysisResult
from services.job_matcher import ResumeJobMatchResult
from services.llm_service import is_api_key_error, is_auth_error, is_rate_limit_error
from services.resume_analyzer import ResumeAnalysisResult, ResumeScores
from sqlalchemy import func
from utils.file_utils import (
    compute_file_hash,
    get_file_extension,
    save_uploaded_file,
    validate_file_extension,
    validate_file_size,
    validate_mime_type,
)
from utils.helpers import normalize_skill

logger = logging.getLogger(__name__)

_DOCUMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".txt"})
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}

_SKILL_ALIASES = {
    "ai": "AI",
    "aws": "AWS",
    "amazon web services": "AWS",
    "cicd": "CI/CD",
    "ci/cd": "CI/CD",
    "css": "CSS",
    "gcp": "Google Cloud Platform",
    "google cloud": "Google Cloud Platform",
    "google cloud platform": "Google Cloud Platform",
    "html": "HTML",
    "js": "JavaScript",
    "javascript": "JavaScript",
    "k8s": "Kubernetes",
    "kubernetes": "Kubernetes",
    "ml": "Machine Learning",
    "machine learning": "Machine Learning",
    "mongo": "MongoDB",
    "mongodb": "MongoDB",
    "nosql": "NoSQL",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "py": "Python",
    "python": "Python",
    "react.js": "React",
    "reactjs": "React",
    "rest api": "REST APIs",
    "rest apis": "REST APIs",
    "restful api": "REST APIs",
    "sql": "SQL",
    "ts": "TypeScript",
    "typescript": "TypeScript",
}

_SOFT_SKILLS = {
    "adaptability": "Adaptability",
    "accountability": "Accountability",
    "collaboration": "Collaboration",
    "communication": "Communication",
    "critical thinking": "Critical thinking",
    "leadership": "Leadership",
    "mentoring": "Mentoring",
    "negotiation": "Negotiation",
    "ownership": "Ownership",
    "presentation": "Presentation",
    "problem solving": "Problem solving",
    "problem-solving": "Problem solving",
    "stakeholder management": "Stakeholder management",
    "teamwork": "Teamwork",
    "time management": "Time management",
}

_TOPIC_TEMPLATES = {
    "python": ["Python syntax and data structures", "Testing and debugging Python code"],
    "sql": ["SQL joins and aggregations", "Query optimisation and indexes"],
    "javascript": ["JavaScript fundamentals", "Asynchronous JavaScript and DOM basics"],
    "typescript": ["TypeScript types and interfaces", "Migrating JavaScript to TypeScript"],
    "react": ["React components and state", "Data fetching and component testing"],
    "docker": ["Containers and images", "Dockerfiles and multi-service workflows"],
    "kubernetes": ["Pods and deployments", "Services, scaling and rollout strategies"],
    "aws": ["Core AWS services", "IAM, networking and cost basics"],
    "machine learning": ["Model evaluation fundamentals", "Feature preparation and overfitting"],
    "communication": ["Concise technical writing", "Structured verbal updates"],
    "leadership": ["Ownership and decision making", "Coaching and feedback conversations"],
    "teamwork": ["Collaborative planning", "Cross-functional handoffs"],
}


class CareerValidationError(ValueError):
    pass


class SkillGapItem(BaseModel):
    skill: str
    category: Literal["technical", "soft"]
    priority: Literal["high", "medium", "low"]
    current_status: Literal["missing"]
    requirement: str
    evidence: str
    learning_topics: list[str] = Field(default_factory=list)
    action_steps: list[str] = Field(default_factory=list)


class SkillGapAnalysis(BaseModel):
    target_role: str
    current_skills: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    matched_skills: list[str] = Field(default_factory=list)
    gaps: list[SkillGapItem] = Field(default_factory=list)
    coverage_score: float = 0.0
    technical_coverage: float = 0.0
    soft_coverage: float = 0.0
    summary: str = ""
    recommendations: list[str] = Field(default_factory=list)
    action_plan: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class PreparedDocument:
    file_path: str
    file_name: str
    text: str
    file_hash: str


@dataclass(frozen=True)
class CareerRecord:
    id: int
    label: str
    detail: str
    score: float | None
    created_at: datetime | None


@dataclass(frozen=True)
class ResumeDetails:
    id: int
    analysis: ResumeAnalysisResult
    scores: ResumeScores
    raw_text: str
    file_name: str
    created_at: datetime | None


@dataclass(frozen=True)
class JobDetails:
    id: int
    analysis: JobAnalysisResult
    raw_text: str
    source: str
    created_at: datetime | None


@dataclass(frozen=True)
class MatchDetails:
    id: int
    resume_id: int
    job_id: int
    match: ResumeJobMatchResult
    created_at: datetime | None


@dataclass(frozen=True)
class CareerSnapshot:
    resume_count: int
    job_count: int
    match_count: int
    latest_score: float | None
    latest_role: str


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value is None or value == "":
        return []
    return [value]


def _string_list(value: Any) -> list[str]:
    return [str(item).strip() for item in _as_list(value) if str(item).strip()]


def _canonical_key(value: str) -> str:
    normalized = normalize_skill(value)
    return _SKILL_ALIASES.get(normalized.lower(), normalized).strip().lower()


def _canonical_label(value: str) -> str:
    normalized = normalize_skill(value)
    return _SKILL_ALIASES.get(normalized.lower(), normalized)


def _soft_label(value: str) -> str | None:
    key = value.strip().lower()
    if key in _SOFT_SKILLS:
        return _SOFT_SKILLS[key]
    for phrase, label in _SOFT_SKILLS.items():
        if phrase in key:
            return label
    return None


def _contains_phrase(text: str, phrase: str) -> bool:
    pattern = rf"(?<!\w){re.escape(phrase)}(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _skill_priority(current: str, candidate: str) -> str:
    return candidate if _PRIORITY_RANK[candidate] < _PRIORITY_RANK[current] else current


def _learning_topics(skill: str, category: str) -> list[str]:
    key = _canonical_key(skill)
    topics = _TOPIC_TEMPLATES.get(key)
    if topics:
        return list(topics)
    if category == "soft":
        return [
            f"{skill} principles and examples",
            f"Practising {skill.lower()} in team scenarios",
        ]
    return [
        f"{skill} fundamentals",
        f"Hands-on project using {skill}",
        f"Common {skill} interview questions",
    ]


def _action_steps(skill: str, category: str) -> list[str]:
    if category == "soft":
        return [
            f"Write one STAR story that demonstrates {skill.lower()}.",
            f"Ask for feedback on {skill.lower()} in your next project or review.",
        ]
    return [
        f"Review {_learning_topics(skill, category)[0].lower()}.",
        f"Build a small project that uses {skill} and record the result.",
        f"Add a concise, evidence-based {skill} bullet to your resume.",
    ]


def _parse_extra_skills(extra_skills: list[str] | str | None) -> list[str]:
    if extra_skills is None:
        return []
    values = [extra_skills] if isinstance(extra_skills, str) else extra_skills
    result: list[str] = []
    for value in values:
        for part in re.split(r"[,\n;]+", value):
            cleaned = part.strip()
            if cleaned:
                result.append(cleaned)
    return result


def _current_skill_signals(
    analysis: ResumeAnalysisResult, extra_skills: list[str] | str | None
) -> dict[str, dict[str, str]]:
    signals: dict[str, dict[str, str]] = {}

    def add(value: Any, source: str) -> None:
        label = _canonical_label(str(value))
        key = label.lower()
        if not key:
            return
        soft = _soft_label(label)
        category = "soft" if soft else "technical"
        display = soft or label
        display_key = display.lower()
        if display_key not in signals:
            signals[display_key] = {
                "key": _canonical_key(display),
                "label": display,
                "category": category,
                "evidence": source,
            }

    for field in ("skills", "programming_languages", "frameworks", "databases", "tools"):
        for value in _string_list(getattr(analysis, field, [])):
            add(value, "Resume skills")
    for experience in analysis.experience:
        for value in _string_list(getattr(experience, "technologies", [])):
            add(value, "Resume experience")
    for project in analysis.projects:
        for value in _string_list(getattr(project, "technologies", [])):
            add(value, "Resume projects")
    for value in _parse_extra_skills(extra_skills):
        add(value, "User-provided")
    return signals


def _required_skill_signals(job: JobAnalysisResult) -> dict[str, dict[str, str]]:
    signals: dict[str, dict[str, str]] = {}

    def add(value: str, requirement: str, priority: str) -> None:
        label = _canonical_label(value)
        soft = _soft_label(label)
        display = soft or label
        key = display.lower()
        if not key:
            return
        category = "soft" if soft else "technical"
        if key in signals:
            signals[key]["priority"] = _skill_priority(signals[key]["priority"], priority)
            if requirement not in signals[key]["requirement"]:
                signals[key]["requirement"] += f"; {requirement}"
            return
        signals[key] = {
            "key": _canonical_key(display),
            "label": display,
            "category": category,
            "priority": priority,
            "requirement": requirement,
        }

    for value in _string_list(job.required_skills):
        add(value, "Listed as required", "high")
    for field in ("programming_languages", "frameworks", "databases"):
        for value in _string_list(getattr(job, field, [])):
            add(value, "Listed in the technical stack", "high")
    for value in _string_list(job.tools):
        add(value, "Listed as a tool", "medium")
    for value in _string_list(job.preferred_skills):
        add(value, "Listed as preferred", "medium")

    text_sources = [
        ("Required skill", "high", _string_list(job.required_skills)),
        ("Job responsibility", "low", _string_list(job.responsibilities)),
        ("Experience requirement", "low", _string_list(job.experience_requirements)),
        ("Education requirement", "low", _string_list(job.education_requirements)),
        ("Job keyword", "low", _string_list(job.keywords)),
    ]
    for source, priority, values in text_sources:
        for value in values:
            lower = value.lower()
            for phrase, label in _SOFT_SKILLS.items():
                if _contains_phrase(lower, phrase):
                    add(label, source, priority)
    return signals


def build_skill_gap(
    resume: ResumeAnalysisResult,
    job: JobAnalysisResult,
    extra_skills: list[str] | str | None = None,
) -> SkillGapAnalysis:
    current = _current_skill_signals(resume, extra_skills)
    required = _required_skill_signals(job)
    current_keys = {entry["key"] for entry in current.values()}

    matched: list[str] = []
    gaps: list[SkillGapItem] = []
    technical_total = 0
    technical_matched = 0
    soft_total = 0
    soft_matched = 0

    for signal in required.values():
        is_soft = signal["category"] == "soft"
        if is_soft:
            soft_total += 1
        else:
            technical_total += 1
        if signal["key"] in current_keys:
            matched.append(signal["label"])
            if is_soft:
                soft_matched += 1
            else:
                technical_matched += 1
            continue
        skill = signal["label"]
        category = cast(Literal["technical", "soft"], signal["category"])
        priority = cast(Literal["high", "medium", "low"], signal["priority"])
        gaps.append(
            SkillGapItem(
                skill=skill,
                category=category,
                priority=priority,
                current_status="missing",
                requirement=signal["requirement"],
                evidence="Not evidenced in the resume analysis or user-provided skills.",
                learning_topics=_learning_topics(skill, category),
                action_steps=_action_steps(skill, category),
            )
        )

    gaps.sort(key=lambda item: (_PRIORITY_RANK[item.priority], item.skill.lower()))
    total = len(required)
    coverage = round(len(matched) / total * 100, 1) if total else 0.0
    technical_coverage = (
        round(technical_matched / technical_total * 100, 1) if technical_total else 0.0
    )
    soft_coverage = round(soft_matched / soft_total * 100, 1) if soft_total else 0.0

    recommendations: list[str] = []
    action_plan: list[str] = []
    for gap in gaps[:5]:
        if gap.category == "soft":
            recommendations.append(f"Prepare a specific example that demonstrates {gap.skill.lower()}.")
        else:
            recommendations.append(f"Show evidence of {gap.skill} through a focused project or role task.")
        action_plan.append(gap.action_steps[0])
    if not gaps and total:
        recommendations.append("All detected job requirements are evidenced in the available profile.")
        action_plan.append("Keep the resume examples current and quantify their outcomes.")

    summary = (
        f"{len(matched)} of {total} detected job requirements are evidenced."
        if total
        else "No explicit job requirements were detected in the selected job analysis."
    )
    return SkillGapAnalysis(
        target_role=job.job_title or "Target role not specified",
        current_skills=sorted(current.keys()),
        required_skills=sorted(required.keys()),
        matched_skills=sorted(set(matched)),
        gaps=gaps,
        coverage_score=coverage,
        technical_coverage=technical_coverage,
        soft_coverage=soft_coverage,
        summary=summary,
        recommendations=recommendations,
        action_plan=action_plan,
    )


def document_fingerprint(uploaded_file: Any) -> str:
    if hasattr(uploaded_file, "getbuffer"):
        data = bytes(uploaded_file.getbuffer())
    elif hasattr(uploaded_file, "read"):
        data = uploaded_file.read()
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)
    else:
        raise CareerValidationError("The uploaded file could not be read.")
    if not data:
        raise CareerValidationError("The selected file is empty.")
    return hashlib.sha256(data).hexdigest()


def prepare_document_upload(uploaded_file: Any, upload_dir: str | None = None) -> PreparedDocument:
    name = str(getattr(uploaded_file, "name", "") or "")
    extension = get_file_extension(name)
    if extension not in _DOCUMENT_EXTENSIONS or not validate_file_extension(name):
        raise CareerValidationError("Choose a PDF, DOCX or TXT document.")
    document_fingerprint(uploaded_file)

    saved_path: str | None = None
    try:
        path, safe_name = save_uploaded_file(uploaded_file, upload_dir)
        saved_path = path
        if not validate_file_size(path):
            raise CareerValidationError("The file is too large. Reduce its size and try again.")
        if not validate_mime_type(path, extension):
            raise CareerValidationError("The file content does not match its document type.")
        documents = load_document(path)
        text = "\n\n".join(document.content for document in documents if document.content).strip()
        if len(text) < 20:
            raise CareerValidationError("The document has too little readable text to analyse.")
        return PreparedDocument(path, safe_name, text, compute_file_hash(path))
    except Exception as exc:
        if saved_path is not None:
            try:
                if os.path.exists(saved_path):
                    os.remove(saved_path)
            except OSError as cleanup_exc:
                logger.warning(
                    "Rejected upload cleanup failed after %s: %s",
                    type(exc).__name__,
                    type(cleanup_exc).__name__,
                )
        raise


def validate_job_text(text: str, title: str = "") -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) < 40:
        raise CareerValidationError("Paste at least 40 characters from the job description.")
    if len(title) > 200:
        raise CareerValidationError("The job title must be 200 characters or fewer.")
    return cleaned


def workspace_id(session_token: str, actor_user_id: int | None) -> int:
    return require_owned_session_token(session_token, actor_user_id)


def save_resume_analysis(
    session_id: int,
    actor_user_id: int | None,
    analysis: ResumeAnalysisResult,
    scores: ResumeScores,
    document: PreparedDocument,
) -> int:
    require_owned_session_id(session_id, actor_user_id)
    values = {
        "file_name": document.file_name,
        "file_path": document.file_path,
        "file_hash": document.file_hash,
        "raw_text": document.text,
        "candidate_name": analysis.candidate_name or None,
        "email": analysis.email or None,
        "phone": analysis.phone or None,
        "linkedin": analysis.linkedin or None,
        "github": analysis.github or None,
        "portfolio": analysis.portfolio or None,
        "education": [item.model_dump() for item in analysis.education],
        "experience": [item.model_dump() for item in analysis.experience],
        "skills": list(analysis.skills),
        "programming_languages": list(analysis.programming_languages),
        "frameworks": list(analysis.frameworks),
        "databases": list(analysis.databases),
        "tools": list(analysis.tools),
        "projects": [item.model_dump() for item in analysis.projects],
        "certifications": list(analysis.certifications),
        "strengths": list(analysis.strengths),
        "weaknesses": list(analysis.weaknesses),
        "technical_skills_score": scores.technical_skills,
        "experience_score": scores.experience,
        "projects_score": scores.projects,
        "education_score": scores.education,
        "certifications_score": scores.certifications,
        "structure_score": scores.structure,
        "overall_resume_score": scores.overall,
    }
    with get_db_context() as db:
        row = (
            db.query(ResumeAnalysis)
            .filter(ResumeAnalysis.session_id == session_id, ResumeAnalysis.file_hash == document.file_hash)
            .first()
        )
        if row is None:
            row = ResumeAnalysis(session_id=session_id)
            db.add(row)
        for key, value in values.items():
            setattr(row, key, value)
        db.flush()
        return int(row.id)


def list_resume_history(session_id: int, actor_user_id: int | None) -> list[CareerRecord]:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        rows = (
            db.query(ResumeAnalysis)
            .filter(ResumeAnalysis.session_id == session_id)
            .order_by(ResumeAnalysis.created_at.desc(), ResumeAnalysis.id.desc())
            .all()
        )
        return [
            CareerRecord(
                id=int(row.id),
                label=row.file_name or f"Resume #{row.id}",
                detail=row.candidate_name or "Candidate not extracted",
                score=row.overall_resume_score,
                created_at=row.created_at,
            )
            for row in rows
        ]


def load_resume_record(record_id: int, session_id: int, actor_user_id: int | None) -> ResumeDetails:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        row = (
            db.query(ResumeAnalysis)
            .filter(ResumeAnalysis.id == record_id, ResumeAnalysis.session_id == session_id)
            .first()
        )
        if row is None:
            raise AuthError("That resume does not belong to this workspace.")
        analysis = ResumeAnalysisResult.model_validate(
            {
                "candidate_name": row.candidate_name or "",
                "email": row.email or "",
                "phone": row.phone or "",
                "linkedin": row.linkedin or "",
                "github": row.github or "",
                "portfolio": row.portfolio or "",
                "education": _as_list(row.education),
                "experience": _as_list(row.experience),
                "skills": _string_list(row.skills),
                "programming_languages": _string_list(row.programming_languages),
                "frameworks": _string_list(row.frameworks),
                "databases": _string_list(row.databases),
                "tools": _string_list(row.tools),
                "projects": _as_list(row.projects),
                "certifications": _string_list(row.certifications),
                "strengths": _string_list(row.strengths),
                "weaknesses": _string_list(row.weaknesses),
            }
        )
        scores = ResumeScores(
            technical_skills=float(row.technical_skills_score or 0),
            experience=float(row.experience_score or 0),
            projects=float(row.projects_score or 0),
            education=float(row.education_score or 0),
            certifications=float(row.certifications_score or 0),
            structure=float(row.structure_score or 0),
            overall=float(row.overall_resume_score or 0),
        )
        return ResumeDetails(
            id=int(row.id),
            analysis=analysis,
            scores=scores,
            raw_text=row.raw_text or "",
            file_name=row.file_name or "Resume",
            created_at=row.created_at,
        )


def load_latest_resume(session_id: int, actor_user_id: int | None) -> ResumeDetails | None:
    records = list_resume_history(session_id, actor_user_id)
    if not records:
        return None
    return load_resume_record(records[0].id, session_id, actor_user_id)


def save_job_analysis(
    session_id: int,
    actor_user_id: int | None,
    analysis: JobAnalysisResult,
    raw_text: str,
    source: str,
) -> int:
    require_owned_session_id(session_id, actor_user_id)
    title = (analysis.job_title or "Job description")[:200]
    safe_source = source[:50] or "pasted"
    values = {
        "title": title,
        "company": (analysis.company or None),
        "raw_text": raw_text,
        "source": safe_source,
        "required_skills": list(analysis.required_skills),
        "preferred_skills": list(analysis.preferred_skills),
        "programming_languages": list(analysis.programming_languages),
        "frameworks": list(analysis.frameworks),
        "databases": list(analysis.databases),
        "tools": list(analysis.tools),
        "experience_requirements": list(analysis.experience_requirements),
        "education_requirements": list(analysis.education_requirements),
        "responsibilities": list(analysis.responsibilities),
        "keywords": list(analysis.keywords),
    }
    with get_db_context() as db:
        row = (
            db.query(JobDescription)
            .filter(
                JobDescription.session_id == session_id,
                JobDescription.title == title,
                JobDescription.source == safe_source,
                JobDescription.raw_text == raw_text,
            )
            .first()
        )
        if row is None:
            row = JobDescription(session_id=session_id)
            db.add(row)
        for key, value in values.items():
            setattr(row, key, value)
        db.flush()
        return int(row.id)


def list_job_history(session_id: int, actor_user_id: int | None) -> list[CareerRecord]:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        rows = (
            db.query(JobDescription)
            .filter(JobDescription.session_id == session_id)
            .order_by(JobDescription.created_at.desc(), JobDescription.id.desc())
            .all()
        )
        return [
            CareerRecord(
                id=int(row.id),
                label=row.title or "Job description",
                detail=row.company or row.source or "Company not specified",
                score=None,
                created_at=row.created_at,
            )
            for row in rows
        ]


def load_job_record(record_id: int, session_id: int, actor_user_id: int | None) -> JobDetails:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        row = (
            db.query(JobDescription)
            .filter(JobDescription.id == record_id, JobDescription.session_id == session_id)
            .first()
        )
        if row is None:
            raise AuthError("That job description does not belong to this workspace.")
        analysis = JobAnalysisResult.model_validate(
            {
                "job_title": row.title or "",
                "company": row.company or "",
                "required_skills": _string_list(row.required_skills),
                "preferred_skills": _string_list(row.preferred_skills),
                "programming_languages": _string_list(row.programming_languages),
                "frameworks": _string_list(row.frameworks),
                "databases": _string_list(row.databases),
                "tools": _string_list(row.tools),
                "experience_requirements": _string_list(row.experience_requirements),
                "education_requirements": _string_list(row.education_requirements),
                "responsibilities": _string_list(row.responsibilities),
                "keywords": _string_list(row.keywords),
            }
        )
        return JobDetails(
            id=int(row.id),
            analysis=analysis,
            raw_text=row.raw_text or "",
            source=row.source or "pasted",
            created_at=row.created_at,
        )


def load_latest_job(session_id: int, actor_user_id: int | None) -> JobDetails | None:
    records = list_job_history(session_id, actor_user_id)
    if not records:
        return None
    return load_job_record(records[0].id, session_id, actor_user_id)


def save_match_analysis(
    session_id: int,
    actor_user_id: int | None,
    resume_id: int,
    job_id: int,
    match: ResumeJobMatchResult,
    skill_gap: SkillGapAnalysis | None = None,
) -> int:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        resume = (
            db.query(ResumeAnalysis)
            .filter(ResumeAnalysis.id == resume_id, ResumeAnalysis.session_id == session_id)
            .first()
        )
        job = (
            db.query(JobDescription)
            .filter(JobDescription.id == job_id, JobDescription.session_id == session_id)
            .first()
        )
        if resume is None or job is None:
            raise AuthError("Select a saved resume and job from this workspace.")

        gap_labels = [gap.skill for gap in skill_gap.gaps] if skill_gap else []
        recommendations = list(match.recommendations)
        if skill_gap:
            recommendations.extend(skill_gap.recommendations)
        values = {
            "technical_skills_match": match.technical_skills_match,
            "experience_match": match.experience_match,
            "projects_match": match.projects_match,
            "education_match": match.education_match,
            "overall_match": match.overall_match,
            "matching_skills": [item.model_dump() for item in match.matching_skills],
            "missing_skills": [item.model_dump() for item in match.missing_skills],
            "skill_gaps": list(dict.fromkeys([*match.skill_gaps, *gap_labels])),
            "experience_gaps": [item.model_dump() for item in match.experience_gaps],
            "recommendations": list(dict.fromkeys(recommendations)),
        }
        row = (
            db.query(ResumeJobMatch)
            .filter(
                ResumeJobMatch.resume_analysis_id == resume_id,
                ResumeJobMatch.job_description_id == job_id,
            )
            .first()
        )
        if row is None:
            row = ResumeJobMatch(
                resume_analysis_id=resume_id,
                job_description_id=job_id,
            )
            db.add(row)
        for key, value in values.items():
            setattr(row, key, value)
        db.flush()
        return int(row.id)


def list_match_history(session_id: int, actor_user_id: int | None) -> list[CareerRecord]:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        rows = (
            db.query(ResumeJobMatch, JobDescription, ResumeAnalysis)
            .join(JobDescription, ResumeJobMatch.job_description_id == JobDescription.id)
            .join(ResumeAnalysis, ResumeJobMatch.resume_analysis_id == ResumeAnalysis.id)
            .filter(ResumeAnalysis.session_id == session_id, JobDescription.session_id == session_id)
            .order_by(ResumeJobMatch.created_at.desc(), ResumeJobMatch.id.desc())
            .all()
        )
        return [
            CareerRecord(
                id=int(match.id),
                label=job.title or "Job description",
                detail=resume.file_name or "Resume",
                score=match.overall_match,
                created_at=match.created_at,
            )
            for match, job, resume in rows
        ]


def load_match_record(record_id: int, session_id: int, actor_user_id: int | None) -> MatchDetails:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        row = (
            db.query(ResumeJobMatch)
            .join(ResumeAnalysis, ResumeJobMatch.resume_analysis_id == ResumeAnalysis.id)
            .join(JobDescription, ResumeJobMatch.job_description_id == JobDescription.id)
            .filter(
                ResumeJobMatch.id == record_id,
                ResumeAnalysis.session_id == session_id,
                JobDescription.session_id == session_id,
            )
            .first()
        )
        if row is None:
            raise AuthError("That match does not belong to this workspace.")
        match = ResumeJobMatchResult.model_validate(
            {
                "technical_skills_match": float(row.technical_skills_match or 0),
                "experience_match": float(row.experience_match or 0),
                "projects_match": float(row.projects_match or 0),
                "education_match": float(row.education_match or 0),
                "overall_match": float(row.overall_match or 0),
                "matching_skills": _as_list(row.matching_skills),
                "missing_skills": _as_list(row.missing_skills),
                "skill_gaps": _string_list(row.skill_gaps),
                "experience_gaps": _as_list(row.experience_gaps),
                "recommendations": _string_list(row.recommendations),
            }
        )
        return MatchDetails(
            id=int(row.id),
            resume_id=int(row.resume_analysis_id),
            job_id=int(row.job_description_id),
            match=match,
            created_at=row.created_at,
        )


def get_career_snapshot(session_id: int, actor_user_id: int | None) -> CareerSnapshot:
    require_owned_session_id(session_id, actor_user_id)
    with get_db_context() as db:
        resume_count = (
            db.query(func.count(ResumeAnalysis.id))
            .filter(ResumeAnalysis.session_id == session_id)
            .scalar()
            or 0
        )
        job_count = (
            db.query(func.count(JobDescription.id))
            .filter(JobDescription.session_id == session_id)
            .scalar()
            or 0
        )
        match_count = (
            db.query(func.count(ResumeJobMatch.id))
            .join(ResumeAnalysis, ResumeJobMatch.resume_analysis_id == ResumeAnalysis.id)
            .filter(ResumeAnalysis.session_id == session_id)
            .scalar()
            or 0
        )
        latest_job = (
            db.query(JobDescription)
            .filter(JobDescription.session_id == session_id)
            .order_by(JobDescription.created_at.desc(), JobDescription.id.desc())
            .first()
        )
        latest_score = (
            db.query(ResumeJobMatch.overall_match)
            .join(ResumeAnalysis, ResumeJobMatch.resume_analysis_id == ResumeAnalysis.id)
            .filter(ResumeAnalysis.session_id == session_id)
            .order_by(ResumeJobMatch.created_at.desc(), ResumeJobMatch.id.desc())
            .first()
        )
    return CareerSnapshot(
        resume_count=int(resume_count),
        job_count=int(job_count),
        match_count=int(match_count),
        latest_score=float(latest_score[0]) if latest_score and latest_score[0] is not None else None,
        latest_role=latest_job.title if latest_job and latest_job.title else "Not set",
    )


def _is_unreachable_message(message: str) -> bool:
    return any(
        marker in message
        for marker in (
            "name or service not known",
            "nodename nor servname provided",
            "temporary failure in name resolution",
            "getaddrinfo",
            "dns",
        )
    )


def friendly_error(exc: Exception, action: str) -> str:
    message = str(exc)
    lowered = message.lower()
    if isinstance(exc, (CareerValidationError, AuthError)):
        return message
    if "no llm providers configured" in lowered:
        return f"{action} failed because no AI provider is configured. Ask an administrator to check provider configuration."
    if is_auth_error(exc) or is_api_key_error(exc) or "not authenticated" in lowered:
        return f"{action} failed because the AI provider is missing or invalid. Ask an administrator to check provider configuration."
    if is_rate_limit_error(exc):
        return f"{action} failed because the provider rate limit was reached. Wait a moment and try again."
    if "timeout" in lowered or "timed out" in lowered:
        return f"{action} failed because the AI request timed out. Check the connection and try again."
    if (
        "winerror 10054" in lowered
        or "connection" in lowered
        or _is_unreachable_message(lowered)
    ):
        return f"{action} failed because the AI connection was reset or the provider could not be reached. Check DNS/network settings and provider status."
    return f"{action} failed unexpectedly. Try again or contact support if the problem persists."
