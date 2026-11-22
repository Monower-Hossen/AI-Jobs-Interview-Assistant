import re
import unicodedata
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from pydantic import BaseModel, Field, field_validator, model_validator
from services.answer_evaluator import AnswerEvaluation
from services.interview_generator import InterviewQuestion
from services.job_analyzer import JobAnalysisResult
from services.llm_service import get_llm_service
from services.resume_analyzer import ResumeAnalysisResult


class ImprovementPlanItem(BaseModel):
    day: int
    focus: str
    activity: str
    resources: list[str] = Field(default_factory=list)
    estimated_hours: float

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_plan(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        data = dict(value)
        if "estimated_hours" not in data and "hours" in data:
            data["estimated_hours"] = data["hours"]
        if "day" not in data and "week" in data:
            data["day"] = data["week"]
        if "focus" not in data and "milestone" in data:
            data["focus"] = data["milestone"]
        if "activity" not in data and "milestone" in data:
            data["activity"] = data["milestone"]
        return data

    @field_validator("resources", mode="before")
    @classmethod
    def normalize_resources(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [resource.strip() for resource in re.split(r"[,\n]+", value) if resource.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(resource).strip() for resource in value if str(resource).strip()]
        return value


class InterviewReport(BaseModel):
    overall_score: float = Field(ge=0, le=10)
    technical_score: float = Field(ge=0, le=10)
    communication_score: float = Field(ge=0, le=10)
    hr_score: float = Field(ge=0, le=10)
    problem_solving_score: float = Field(ge=0, le=10)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    frequently_missed_concepts: list[str] = Field(default_factory=list)
    recommended_topics: list[str] = Field(default_factory=list)
    recommended_projects: list[str] = Field(default_factory=list)
    recommended_questions: list[str] = Field(default_factory=list)
    improvement_plan_7_day: list[ImprovementPlanItem] = Field(default_factory=list)
    improvement_plan_30_day: list[ImprovementPlanItem] = Field(default_factory=list)


REPORT_GENERATION_PROMPT = """You are an expert career coach. Generate a comprehensive interview performance report.

INTERVIEW SUMMARY:
- Total Questions: {total_questions}
- Categories Covered: {categories}
- Average Scores: Technical={avg_technical:.1f}, Communication={avg_comm:.1f}, Relevance={avg_relevance:.1f}, Completeness={avg_complete:.1f}, Problem-solving={avg_problem:.1f}, Structure={avg_structure:.1f}

QUESTION-BY-QUESTION BREAKDOWN:
{question_breakdown}

CANDIDATE PROFILE:
{resume_summary}

TARGET JOB:
{job_summary}

Generate a detailed report with:

1. overall_score: Weighted overall (0-10)
2. technical_score: Average technical accuracy for technical questions (0-10)
3. communication_score: Average communication score (0-10)
4. hr_score: Average score for HR/behavioral/situational questions (0-10)
5. problem_solving_score: Inferred from technical + situational answers (0-10)
6. strengths: Top 5-7 strengths demonstrated
7. weaknesses: Top 5-7 areas for improvement
8. frequently_missed_concepts: Concepts missed across multiple questions
9. recommended_topics: Topics to study (specific, actionable)
10. recommended_projects: Project ideas to build portfolio
11. recommended_questions: Questions to practice for next interview
12. improvement_plan_7_day: exactly 7 objects. Each object must use this exact shape:
    {"day": 1, "focus": "topic", "activity": "action", "resources": ["resource 1", "resource 2"], "estimated_hours": 2.0}
13. improvement_plan_30_day: exactly 4 objects using the same exact shape. For this plan, set day to the week number (1-4), focus to the milestone, activity to the planned activity, resources to a list, and estimated_hours to a number.
Do not use "hours", "week", or "milestone" as field names; use "estimated_hours", "day", "focus", and "activity" exactly.

Be specific and actionable. Reference actual interview content.

Return ONLY valid JSON matching the schema."""


class ReportGenerator:
    def __init__(self):
        self.llm = get_llm_service()

    def generate(
        self,
        questions: list[InterviewQuestion],
        evaluations: list[AnswerEvaluation],
        resume: ResumeAnalysisResult,
        job: JobAnalysisResult,
    ) -> InterviewReport:

        tech_scores = [e.technical_accuracy for e in evaluations]
        comm_scores = [e.communication for e in evaluations]
        relevance_scores = [e.relevance for e in evaluations]
        complete_scores = [e.completeness for e in evaluations]

        breakdown_lines = []
        for i, (q, e) in enumerate(zip(questions, evaluations)):
            breakdown_lines.append(
                f"Q{i+1} [{q.category.value}/{q.difficulty.value}]: "
                f"Tech={e.technical_accuracy:.1f}, Comm={e.communication:.1f}, "
                f"Rel={e.relevance:.1f}, Comp={e.completeness:.1f}, "
                f"Overall={e.overall_score:.1f} - {q.question_text[:80]}..."
            )
        question_breakdown = "\n".join(breakdown_lines)

        resume_summary = f"""
Name: {resume.candidate_name or 'N/A'}
Experience: {len(resume.experience)} roles
Skills: {', '.join(resume.skills[:15])}
Projects: {len(resume.projects)}
"""

        job_summary = f"""
Title: {job.job_title or 'N/A'}
Required: {', '.join(job.required_skills[:10])}
Responsibilities: {', '.join(job.responsibilities[:5])}
"""

        prompt = REPORT_GENERATION_PROMPT.format(
            total_questions=len(questions),
            categories=", ".join({q.category.value for q in questions}),
            avg_technical=sum(tech_scores) / len(tech_scores) if tech_scores else 0,
            avg_comm=sum(comm_scores) / len(comm_scores) if comm_scores else 0,
            avg_relevance=(
                sum(relevance_scores) / len(relevance_scores) if relevance_scores else 0
            ),
            avg_complete=(sum(complete_scores) / len(complete_scores) if complete_scores else 0),
            avg_problem=(
                sum(e.problem_solving for e in evaluations) / len(evaluations) if evaluations else 0
            ),
            avg_structure=(
                sum(e.structure for e in evaluations) / len(evaluations) if evaluations else 0
            ),
            question_breakdown=question_breakdown,
            resume_summary=resume_summary,
            job_summary=job_summary,
        )

        result = self.llm.generate_structured(prompt, InterviewReport)
        return result


def generate_interview_report(
    questions: list[InterviewQuestion],
    evaluations: list[AnswerEvaluation],
    resume: ResumeAnalysisResult,
    job: JobAnalysisResult,
) -> InterviewReport:
    generator = ReportGenerator()
    return generator.generate(questions, evaluations, resume, job)


def _score_label(score: float) -> str:
    if score >= 8.5:
        return "Excellent"
    if score >= 7:
        return "Good"
    if score >= 5:
        return "Average"
    return "Needs improvement"


def _pdf_font() -> tuple[str, bool]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    registered = pdfmetrics.getRegisteredFontNames()
    if "ReportSans" in registered:
        return "ReportSans", True
    candidates = [
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/Library/Fonts/Arial Unicode.ttf"),
    ]
    for font_path in candidates:
        if font_path.exists():
            pdfmetrics.registerFont(TTFont("ReportSans", str(font_path)))
            return "ReportSans", True
    return "Helvetica", False


def _pdf_text(value: Any, unicode_safe: bool) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        text = "; ".join(f"{key}: {item}" for key, item in value.items())
    elif isinstance(value, (list, tuple, set)):
        text = " • ".join(str(item) for item in value)
    else:
        text = str(value)
    if not unicode_safe:
        text = unicodedata.normalize("NFKD", text).encode("latin-1", "replace").decode("latin-1")
    return escape(text)


def render_interview_pdf(data: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    font_name, unicode_safe = _pdf_font()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=22,
        leading=26,
        textColor=colors.HexColor("#E9D5FF"),
        alignment=TA_LEFT,
    )
    heading_style = ParagraphStyle(
        "ReportHeading",
        parent=styles["Heading2"],
        fontName=font_name,
        fontSize=13,
        leading=17,
        spaceBefore=12,
        spaceAfter=6,
        textColor=colors.HexColor("#8B5CF6"),
    )
    body_style = ParagraphStyle(
        "ReportBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#E2E8F0"),
    )
    label_style = ParagraphStyle(
        "ReportLabel",
        parent=body_style,
        fontSize=8,
        textColor=colors.HexColor("#94A3B8"),
    )

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title="Interview Performance Report",
        author="AI Interview Assistant",
    )
    story: list[Any] = []
    story.append(Paragraph("Interview Performance Report", title_style))
    story.append(
        Paragraph(
            _pdf_text(
                f"{data.get('interview_type') or 'Mock interview'} · "
                f"{data.get('difficulty') or 'medium'} · "
                f"{data.get('interview_date') or ''}",
                unicode_safe,
            ),
            label_style,
        )
    )
    story.append(Spacer(1, 5 * mm))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#334155")))

    scores = data.get("scores") or {}
    score_rows = [["Dimension", "Score", "Rating"]]
    for key, label in (
        ("overall", "Overall"),
        ("technical", "Technical"),
        ("communication", "Communication"),
        ("hr", "HR / Behavioral"),
        ("problem_solving", "Problem solving"),
    ):
        value = scores.get(key)
        if value is None:
            continue
        numeric = float(value)
        score_rows.append(
            [
                Paragraph(_pdf_text(label, unicode_safe), body_style),
                Paragraph(_pdf_text(f"{numeric:.1f}/10", unicode_safe), body_style),
                Paragraph(_pdf_text(_score_label(numeric), unicode_safe), body_style),
            ]
        )
    if len(score_rows) > 1:
        score_table = Table(score_rows, colWidths=[65 * mm, 30 * mm, 45 * mm])
        score_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E1B4B")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#DDD6FE")),
                    ("FONTNAME", (0, 0), (-1, 0), font_name),
                    ("FONTSIZE", (0, 0), (-1, 0), 9),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.HexColor("#0F172A"), colors.HexColor("#111827")],
                    ),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#334155")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            )
        )
        story.append(score_table)

    sections = [
        ("Strengths", data.get("strengths")),
        ("Weaknesses", data.get("weaknesses")),
        ("Frequently missed concepts", data.get("missed_concepts")),
        ("Recommended topics", data.get("recommended_topics")),
        ("Recommended projects", data.get("recommended_projects")),
        ("Practice questions", data.get("recommended_questions")),
    ]
    for heading, items in sections:
        if not items:
            continue
        story.append(Paragraph(_pdf_text(heading, unicode_safe), heading_style))
        for item in items:
            story.append(Paragraph(f"• {_pdf_text(item, unicode_safe)}", body_style))

    for heading, key in (
        ("7-day improvement plan", "improvement_plan_7_day"),
        ("30-day improvement plan", "improvement_plan_30_day"),
    ):
        plan = data.get(key)
        if not plan:
            continue
        story.append(Paragraph(_pdf_text(heading, unicode_safe), heading_style))
        plan_rows = [["Day", "Focus", "Activity", "Hours"]]
        for item in plan:
            if isinstance(item, dict):
                plan_rows.append(
                    [
                        Paragraph(_pdf_text(item.get("day", ""), unicode_safe), body_style),
                        Paragraph(_pdf_text(item.get("focus", ""), unicode_safe), body_style),
                        Paragraph(_pdf_text(item.get("activity", ""), unicode_safe), body_style),
                        Paragraph(
                            _pdf_text(item.get("estimated_hours", ""), unicode_safe), body_style
                        ),
                    ]
                )
            else:
                plan_rows.append([Paragraph(_pdf_text(item, unicode_safe), body_style), "", "", ""])
        plan_table = Table(plan_rows, colWidths=[14 * mm, 42 * mm, 90 * mm, 18 * mm])
        plan_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E1B4B")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#DDD6FE")),
                    ("FONTNAME", (0, 0), (-1, 0), font_name),
                    ("FONTSIZE", (0, 0), (-1, 0), 8),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.HexColor("#0F172A"), colors.HexColor("#111827")],
                    ),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#334155")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(plan_table)

    story.append(Spacer(1, 8 * mm))
    story.append(
        Paragraph(
            _pdf_text(
                "AI-generated guidance. Use it as a practice aid, not a hiring decision.",
                unicode_safe,
            ),
            label_style,
        )
    )

    document.build(story)
    return buffer.getvalue()


def render_career_pdf(data: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    font_name, unicode_safe = _pdf_font()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CareerReportTitle",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=21,
        leading=25,
        textColor=colors.HexColor("#E9D5FF"),
        alignment=TA_LEFT,
    )
    heading_style = ParagraphStyle(
        "CareerReportHeading",
        parent=styles["Heading2"],
        fontName=font_name,
        fontSize=12,
        leading=16,
        spaceBefore=10,
        spaceAfter=5,
        textColor=colors.HexColor("#8B5CF6"),
    )
    body_style = ParagraphStyle(
        "CareerReportBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#E2E8F0"),
    )
    label_style = ParagraphStyle(
        "CareerReportLabel",
        parent=body_style,
        fontSize=8,
        textColor=colors.HexColor("#94A3B8"),
    )

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=str(data.get("title") or "Career report"),
        author="AI Interview Assistant",
    )
    story: list[Any] = []
    story.append(Paragraph(_pdf_text(data.get("title") or "Career report", unicode_safe), title_style))
    subtitle = data.get("subtitle")
    if subtitle:
        story.append(Paragraph(_pdf_text(subtitle, unicode_safe), label_style))
    story.append(Spacer(1, 4 * mm))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#334155")))

    scores = data.get("scores") or {}
    if isinstance(scores, dict) and scores:
        score_rows = [["Measure", "Value"]]
        for label, value in scores.items():
            score_rows.append(
                [
                    Paragraph(_pdf_text(label, unicode_safe), body_style),
                    Paragraph(_pdf_text(value, unicode_safe), body_style),
                ]
            )
        table = Table(score_rows, colWidths=[70 * mm, 100 * mm])
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E1B4B")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#DDD6FE")),
                    ("FONTNAME", (0, 0), (-1, 0), font_name),
                    ("FONTSIZE", (0, 0), (-1, 0), 9),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.HexColor("#0F172A"), colors.HexColor("#111827")],
                    ),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#334155")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(table)

    sections = data.get("sections") or []
    for index, section in enumerate(sections):
        if index and index % 3 == 0:
            story.append(PageBreak())
        heading = section.get("heading") if isinstance(section, dict) else str(section)
        story.append(Paragraph(_pdf_text(heading, unicode_safe), heading_style))
        items = section.get("items") if isinstance(section, dict) else []
        for item in items or []:
            if isinstance(item, dict):
                label = item.get("label") or item.get("skill") or item.get("title") or ""
                detail = item.get("detail") or item.get("evidence") or item.get("description") or ""
                text = f"{label}: {detail}" if label and detail else label or detail
            else:
                text = str(item)
            story.append(Paragraph(f"• {_pdf_text(text, unicode_safe)}", body_style))
        if isinstance(section, dict) and section.get("caption"):
            story.append(Paragraph(_pdf_text(section["caption"], unicode_safe), label_style))

    story.append(Spacer(1, 6 * mm))
    story.append(
        Paragraph(
            _pdf_text(
                "Automated analysis is advisory. Verify all recommendations before decisions.",
                unicode_safe,
            ),
            label_style,
        )
    )
    document.build(story)
    return buffer.getvalue()
