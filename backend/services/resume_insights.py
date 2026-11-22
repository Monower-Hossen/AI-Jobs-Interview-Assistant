import re
from dataclasses import dataclass, field

from services.job_analyzer import JobAnalysisResult
from services.resume_analyzer import ResumeAnalysisResult, ResumeScores

_SECTION_PATTERNS = {
    "Contact details": r"(email|phone|linkedin|github|portfolio)",
    "Experience": r"(experience|employment|work history)",
    "Education": r"(education|academic|degree)",
    "Projects": r"(projects|portfolio)",
    "Skills": r"(skills|technical skills|technologies)",
}
_ACTION_VERBS = (
    "achieved",
    "built",
    "designed",
    "developed",
    "led",
    "optimized",
    "reduced",
    "delivered",
    "created",
    "improved",
)


@dataclass
class ResumeQuality:
    ats_score: float
    keyword_coverage: float
    word_count: int
    checked_keywords: list[str] = field(default_factory=list)
    missing_keywords: list[str] = field(default_factory=list)
    section_checks: dict[str, bool] = field(default_factory=dict)
    quantified_points: int = 0
    suggestions: list[str] = field(default_factory=list)


def _keywords(target_role: str, job: JobAnalysisResult | None) -> list[str]:
    values: list[str] = []
    if job:
        values.extend(
            [
                job.job_title or "",
                *job.required_skills,
                *job.preferred_skills,
                *job.programming_languages,
                *job.frameworks,
                *job.keywords[:12],
            ]
        )
    if target_role:
        values.append(target_role)
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = value.strip()
        key = clean.lower()
        if len(clean) < 3 or key in seen:
            continue
        seen.add(key)
        result.append(clean)
    return result[:25]


def analyze_resume_quality(
    analysis: ResumeAnalysisResult,
    scores: ResumeScores,
    raw_text: str,
    target_role: str = "",
    job: JobAnalysisResult | None = None,
) -> ResumeQuality:
    text = raw_text or ""
    lower = text.lower()
    words = re.findall(r"\b[\w+#.-]+\b", text)
    word_count = len(words)
    sections = {
        name: bool(re.search(pattern, lower)) for name, pattern in _SECTION_PATTERNS.items()
    }
    contact = bool(analysis.email or re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text))
    quantified = len(re.findall(r"(?:\b\d+(?:\.\d+)?\s?%?\b|\$\s?\d+)", text))
    action_verbs = sum(1 for verb in _ACTION_VERBS if verb in lower)

    keyword_list = _keywords(target_role, job)
    missing = [keyword for keyword in keyword_list if keyword.lower() not in lower]
    coverage = (
        round((len(keyword_list) - len(missing)) / len(keyword_list) * 100, 1)
        if keyword_list
        else 0.0
    )

    section_component = sum(sections.values()) / len(sections) * 30
    keyword_component = coverage * 0.3
    evidence_component = min(quantified, 5) / 5 * 15
    action_component = min(action_verbs, 5) / 5 * 10
    contact_component = 10 if contact else 0
    length_component = 10 if 300 <= word_count <= 1400 else (5 if word_count else 0)
    structure_component = min(scores.structure, 15) / 15 * 10
    ats = round(
        min(
            100.0,
            section_component
            + keyword_component
            + evidence_component
            + action_component
            + contact_component
            + length_component
            + structure_component,
        ),
        1,
    )

    suggestions: list[str] = []
    for name, present in sections.items():
        if not present:
            suggestions.append(f"Add a clearly labelled {name.lower()} section.")
    if not keyword_list:
        suggestions.append("Set a target role or analyze a job description for keyword checks.")
    elif missing:
        suggestions.append("Include missing keywords naturally: " + ", ".join(missing[:6]) + ".")
    if quantified < 3:
        suggestions.append("Quantify achievements with numbers, percentages or time saved.")
    if action_verbs < 3:
        suggestions.append("Start bullets with action verbs such as built, led or optimized.")
    if not contact:
        suggestions.append("Include a professional email address in the resume header.")
    if word_count < 300:
        suggestions.append("Add more detail about scope, impact and outcomes.")
    elif word_count > 1400:
        suggestions.append("Trim the resume to keep the most relevant two pages.")

    return ResumeQuality(
        ats_score=ats,
        keyword_coverage=coverage,
        word_count=word_count,
        checked_keywords=keyword_list,
        missing_keywords=missing,
        section_checks=sections,
        quantified_points=quantified,
        suggestions=suggestions[:8],
    )
