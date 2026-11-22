import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import database.db as db_module
import httpx
import pytest
import services.career_service as career_module
from config.settings import settings
from database import User, UserSession
from docx import Document as DocxDocument
from reportlab.pdfgen import canvas
from services.auth_service import AuthError
from services.career_service import (
    CareerValidationError,
    PreparedDocument,
    build_skill_gap,
    document_fingerprint,
    get_career_snapshot,
    list_job_history,
    list_match_history,
    list_resume_history,
    load_job_record,
    load_match_record,
    load_resume_record,
    prepare_document_upload,
    save_job_analysis,
    save_match_analysis,
    save_resume_analysis,
    validate_job_text,
    workspace_id,
)
from services.job_analyzer import JobAnalysisResult
from services.job_matcher import (
    ExperienceGap,
    MissingSkill,
    ResumeJobMatchResult,
    SkillMatch,
)
from services.report_generator import render_career_pdf
from services.resume_analyzer import ResumeAnalysisResult, ResumeScores


@pytest.fixture
def career_db(tmp_path):
    original_database = settings.database_path
    original_vectors = settings.vector_store_path
    original_engine = db_module.engine
    original_session_local = db_module.SessionLocal

    settings.database_path = str(tmp_path / "career_features.db")
    settings.vector_store_path = str(tmp_path / "vectors")
    db_module.engine = None
    db_module.SessionLocal = None
    db_module.init_database()

    with db_module.get_db_context() as db:
        one = User(
            email="career-one@example.com",
            username="career-one",
            full_name="Career One",
            password_hash="isolated-test-hash",
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        two = User(
            email="career-two@example.com",
            username="career-two",
            full_name="Career Two",
            password_hash="isolated-test-hash",
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        db.add_all([one, two])
        db.flush()
        session_one = UserSession(
            session_id="career-user-1",
            user_id=one.id,
            label="one",
        )
        session_two = UserSession(
            session_id="career-user-2",
            user_id=two.id,
            label="two",
        )
        db.add_all([session_one, session_two])
        db.flush()
        ids = {
            "user1": int(one.id),
            "user2": int(two.id),
            "session1": int(session_one.id),
            "session2": int(session_two.id),
        }

    yield ids

    db_module.close_database()
    db_module.engine = original_engine
    db_module.SessionLocal = original_session_local
    settings.database_path = original_database
    settings.vector_store_path = original_vectors


def _resume() -> ResumeAnalysisResult:
    return ResumeAnalysisResult(
        candidate_name="Ada Lovelace",
        email="ada@example.com",
        skills=["Python", "SQL"],
        programming_languages=["Python"],
        databases=["PostgreSQL"],
    )


def _scores() -> ResumeScores:
    return ResumeScores(
        technical_skills=82,
        experience=74,
        projects=88,
        education=80,
        certifications=60,
        structure=85,
        overall=79,
    )


def _job() -> JobAnalysisResult:
    return JobAnalysisResult(
        job_title="Backend Engineer",
        company="Example Labs",
        required_skills=["Python", "Docker", "Communication"],
        programming_languages=["Python"],
        tools=["Docker"],
        experience_requirements=["3+ years building APIs"],
        responsibilities=["Collaborate with product stakeholders"],
        keywords=["backend", "api"],
    )


def _match() -> ResumeJobMatchResult:
    return ResumeJobMatchResult(
        technical_skills_match=78,
        experience_match=70,
        projects_match=80,
        education_match=85,
        overall_match=76,
        matching_skills=[
            SkillMatch(
                skill="Python",
                resume_evidence="Built Python APIs",
                job_requirement="Python required",
                match_strength="strong",
            )
        ],
        missing_skills=[
            MissingSkill(
                skill="Docker",
                job_requirement="Containerisation",
                importance="important",
                learning_resources=["Docker fundamentals"],
            )
        ],
        skill_gaps=["Docker"],
        experience_gaps=[
            ExperienceGap(
                area="Cloud deployment",
                job_requirement="Deploy containerised services",
                resume_status="Not evidenced",
                gap_description="No container deployment example",
            )
        ],
        recommendations=["Build and deploy a containerised API"],
    )


def _upload(name: str, data: bytes) -> BytesIO:
    upload = BytesIO(data)
    upload.name = name
    return upload


def _assert_upload_dir_empty(upload_dir: Path) -> None:
    assert not list(upload_dir.iterdir())


def _txt_bytes() -> bytes:
    return b"Ada Lovelace Python SQL PostgreSQL APIs and backend experience"


def _pdf_bytes() -> bytes:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, "Ada Lovelace Python SQL PostgreSQL APIs")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _docx_bytes() -> bytes:
    buffer = BytesIO()
    document = DocxDocument()
    document.add_paragraph("Ada Lovelace Python SQL PostgreSQL APIs")
    document.save(buffer)
    return buffer.getvalue()


def _corrupt_docx_bytes() -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "not xml")
        archive.writestr("word/document.xml", "not xml")
    return buffer.getvalue()


def _failing_llm(message: str) -> Mock:
    llm = Mock()
    llm.generate_structured.side_effect = RuntimeError(message)
    llm.generate.side_effect = RuntimeError(message)
    return llm


class TestCareerValidation:
    def test_document_fingerprint_rejects_empty_upload(self):
        empty = BytesIO(b"")
        with pytest.raises(CareerValidationError, match="empty"):
            document_fingerprint(empty)

    def test_document_fingerprint_is_repeatable(self):
        first = BytesIO(b"resume content")
        second = BytesIO(b"resume content")
        assert document_fingerprint(first) == document_fingerprint(second)

    def test_prepare_document_upload_rejects_invalid_extension(self):
        upload = BytesIO(b"Python SQL Docker Communication")
        upload.name = "malware.exe"
        with pytest.raises(CareerValidationError, match="PDF, DOCX or TXT"):
            prepare_document_upload(upload)

    @pytest.mark.parametrize(
        ("filename", "builder"),
        [
            ("resume.txt", _txt_bytes),
            ("resume.pdf", _pdf_bytes),
            ("resume.docx", _docx_bytes),
        ],
    )
    def test_prepare_document_upload_extracts_supported_documents(
        self, tmp_path, filename, builder
    ):
        upload_dir = tmp_path / "uploads"
        prepared = prepare_document_upload(
            _upload(filename, builder()), str(upload_dir)
        )

        assert "Ada Lovelace" in prepared.text
        assert len(prepared.text) >= 20
        assert prepared.file_hash
        assert Path(prepared.file_path).exists()

    def test_valid_docx_upload_remains_available_after_validation(self, tmp_path):
        upload_dir = tmp_path / "uploads"
        data = _docx_bytes()
        prepared = prepare_document_upload(_upload("resume.docx", data), str(upload_dir))
        saved = Path(prepared.file_path)

        assert "Ada Lovelace" in prepared.text
        assert saved.exists()
        assert saved.read_bytes() == data

    def test_prepare_document_upload_rejects_corrupt_pdf_and_removes_saved_file(self, tmp_path):
        upload_dir = tmp_path / "uploads"
        upload = _upload("corrupt.pdf", b"%PDF-1.4\nnot a valid pdf but long enough to parse")

        with pytest.raises(Exception, match="PDF|readable text"):
            prepare_document_upload(upload, str(upload_dir))

        _assert_upload_dir_empty(upload_dir)

    def test_prepare_document_upload_rejects_corrupt_docx_and_removes_saved_file(self, tmp_path):
        upload_dir = tmp_path / "uploads"
        upload = _upload("corrupt.docx", _corrupt_docx_bytes())

        with pytest.raises(Exception, match="DOCX|readable text"):
            prepare_document_upload(upload, str(upload_dir))

        _assert_upload_dir_empty(upload_dir)

    def test_prepare_document_upload_rejects_empty_extracted_text_and_removes_saved_file(
        self, tmp_path
    ):
        upload_dir = tmp_path / "uploads"
        upload = _upload("blank.txt", b"   \n")

        with pytest.raises(CareerValidationError, match="too little readable text"):
            prepare_document_upload(upload, str(upload_dir))

        _assert_upload_dir_empty(upload_dir)

    def test_prepare_document_upload_rejects_mime_extension_mismatch_and_cleans_temp(
        self, tmp_path
    ):
        upload_dir = tmp_path / "uploads"
        upload_dir.mkdir()
        upload = _upload("resume.pdf", _txt_bytes())

        with pytest.raises(Exception, match="Rejected MIME type|does not match"):
            prepare_document_upload(upload, str(upload_dir))

        _assert_upload_dir_empty(upload_dir)

    def test_prepare_document_upload_rejects_oversized_file_before_temp_file(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(settings, "max_file_size_mb", 0.0001)
        upload_dir = tmp_path / "uploads"
        upload_dir.mkdir()

        with pytest.raises(Exception, match="exceeds maximum size|too large"):
            prepare_document_upload(
                _upload("resume.txt", _txt_bytes() * 5), str(upload_dir)
            )

        _assert_upload_dir_empty(upload_dir)

    def test_prepare_document_upload_removes_saved_file_when_post_save_size_check_fails(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(career_module, "validate_file_size", lambda _path: False)
        upload_dir = tmp_path / "uploads"

        with pytest.raises(CareerValidationError, match="too large"):
            prepare_document_upload(_upload("resume.txt", _txt_bytes()), str(upload_dir))

        _assert_upload_dir_empty(upload_dir)

    def test_validate_job_text_requires_description(self):
        with pytest.raises(CareerValidationError, match="40 characters"):
            validate_job_text("Too short")
        assert validate_job_text("Build Python APIs with Docker and PostgreSQL") == (
            "Build Python APIs with Docker and PostgreSQL"
        )


class TestSkillGap:
    def test_gap_uses_only_resume_and_job_evidence(self):
        gap = build_skill_gap(_resume(), _job())

        assert gap.target_role == "Backend Engineer"
        assert "Python" in gap.matched_skills
        assert any(item.skill == "Docker" and item.category == "technical" for item in gap.gaps)
        assert any(
            item.skill == "Communication" and item.category == "soft" for item in gap.gaps
        )
        assert 0 < gap.coverage_score < 100
        assert gap.action_plan

    def test_user_provided_skills_close_detected_gaps(self):
        gap = build_skill_gap(_resume(), _job(), "Docker, Communication")

        assert gap.coverage_score == 100
        assert not gap.gaps
        assert "Docker" in gap.matched_skills
        assert "Communication" in gap.matched_skills


class TestCareerPersistence:
    def test_workspace_ownership_and_history(self, career_db):
        assert workspace_id("career-user-1", career_db["user1"]) == career_db["session1"]
        with pytest.raises(AuthError):
            workspace_id("career-user-1", career_db["user2"])

        document = PreparedDocument(
            "data/uploads/resume.txt",
            "resume.txt",
            "Ada Lovelace Python SQL",
            "career-resume-hash",
        )
        resume_id = save_resume_analysis(
            career_db["session1"],
            career_db["user1"],
            _resume(),
            _scores(),
            document,
        )
        job_id = save_job_analysis(
            career_db["session1"],
            career_db["user1"],
            _job(),
            "Backend engineer role requiring Python and Docker",
            "pasted",
        )
        match_id = save_match_analysis(
            career_db["session1"],
            career_db["user1"],
            resume_id,
            job_id,
            _match(),
        )

        assert len(list_resume_history(career_db["session1"], career_db["user1"])) == 1
        assert len(list_job_history(career_db["session1"], career_db["user1"])) == 1
        assert len(list_match_history(career_db["session1"], career_db["user1"])) == 1
        assert load_resume_record(resume_id, career_db["session1"], career_db["user1"]).analysis.candidate_name == "Ada Lovelace"
        assert load_job_record(job_id, career_db["session1"], career_db["user1"]).analysis.job_title == "Backend Engineer"
        assert load_match_record(match_id, career_db["session1"], career_db["user1"]).match.overall_match == 76

        snapshot = get_career_snapshot(career_db["session1"], career_db["user1"])
        assert snapshot.resume_count == 1
        assert snapshot.job_count == 1
        assert snapshot.match_count == 1
        assert snapshot.latest_score == 76
        assert snapshot.latest_role == "Backend Engineer"

    def test_cross_user_and_cross_workspace_access_is_denied(self, career_db):
        document = PreparedDocument(
            "data/uploads/resume-private.txt",
            "resume-private.txt",
            "Ada Lovelace Python SQL",
            "career-private-hash",
        )
        resume_id = save_resume_analysis(
            career_db["session1"],
            career_db["user1"],
            _resume(),
            _scores(),
            document,
        )
        job_id = save_job_analysis(
            career_db["session1"],
            career_db["user1"],
            _job(),
            "Private backend engineer description",
            "pasted",
        )
        match_id = save_match_analysis(
            career_db["session1"],
            career_db["user1"],
            resume_id,
            job_id,
            _match(),
        )

        with pytest.raises(AuthError):
            list_resume_history(career_db["session1"], career_db["user2"])
        with pytest.raises(AuthError):
            load_resume_record(resume_id, career_db["session1"], career_db["user2"])
        with pytest.raises(AuthError):
            load_job_record(job_id, career_db["session1"], career_db["user2"])
        with pytest.raises(AuthError):
            load_match_record(match_id, career_db["session1"], career_db["user2"])
        with pytest.raises(AuthError):
            load_match_record(match_id, career_db["session2"], career_db["user1"])
        with pytest.raises(AuthError):
            save_match_analysis(
                career_db["session2"],
                career_db["user1"],
                resume_id,
                job_id,
                _match(),
            )

    def test_duplicate_resume_hash_is_reused(self, career_db):
        document = PreparedDocument(
            "data/uploads/resume-duplicate.txt",
            "resume-duplicate.txt",
            "Ada Lovelace Python SQL",
            "career-duplicate-hash",
        )
        first = save_resume_analysis(
            career_db["session1"],
            career_db["user1"],
            _resume(),
            _scores(),
            document,
        )
        second = save_resume_analysis(
            career_db["session1"],
            career_db["user1"],
            _resume(),
            _scores(),
            document,
        )

        assert first == second
        assert len(list_resume_history(career_db["session1"], career_db["user1"])) == 1


class TestCareerReport:
    def test_career_pdf_report_is_generated(self):
        payload = {
            "title": "Job match report",
            "subtitle": "Backend Engineer",
            "scores": {"Overall": "76%"},
            "sections": [
                {
                    "heading": "Recommendations",
                    "items": ["Build and deploy a containerised API"],
                }
            ],
        }

        result = render_career_pdf(payload)

        assert result.startswith(b"%PDF")

    def test_career_pdf_report_handles_real_payload_with_special_characters(self):
        gap = build_skill_gap(_resume(), _job())
        payload = {
            "title": "Job match report <safe> & \"quoted\"",
            "subtitle": "Backend Engineer · Example Labs",
            "scores": {
                "Overall": "76%",
                "Evidence coverage": f"{gap.coverage_score:.0f}%",
            },
            "sections": [
                {
                    "heading": "Missing skills",
                    "items": [
                        {
                            "skill": "Docker",
                            "evidence": "Required <containerisation> & deployment",
                        },
                        {"skill": "Communication", "evidence": "Stakeholder updates"},
                    ],
                },
                {
                    "heading": "Recommendations",
                    "items": _match().recommendations,
                },
            ],
        }

        result = render_career_pdf(payload)

        assert result.startswith(b"%PDF")
        assert len(result) > 4000

    def test_career_pdf_report_tolerates_missing_optional_fields(self):
        result = render_career_pdf({"title": "Minimal <report> & details"})

        assert result.startswith(b"%PDF")


_PROVIDER_FAILURE_MESSAGES = [
    "HTTP 401: invalid API key",
    "HTTP 429: rate limit exceeded",
    "Request timed out",
    "connection reset by peer",
    "Name or service not known",
    "Invalid JSON object from provider",
]


class TestCareerAiFailures:
    def test_resume_analysis_propagates_missing_credentials(self, monkeypatch):
        from services import resume_analyzer

        message = "No LLM providers configured. Set an API key."
        monkeypatch.setattr(
            resume_analyzer, "get_llm_service", Mock(side_effect=ValueError(message))
        )

        with pytest.raises(ValueError, match="No LLM providers configured"):
            resume_analyzer.analyze_resume("Ada Lovelace Python SQL PostgreSQL APIs")

    def test_job_analysis_propagates_missing_credentials(self, monkeypatch):
        from services import job_analyzer

        message = "No LLM providers configured. Set an API key."
        monkeypatch.setattr(
            job_analyzer, "get_llm_service", Mock(side_effect=ValueError(message))
        )

        with pytest.raises(ValueError, match="No LLM providers configured"):
            job_analyzer.analyze_job_description("Backend engineer role requiring Python and Docker")

    def test_job_matching_propagates_missing_credentials(self, monkeypatch):
        from services import job_matcher

        message = "No LLM providers configured. Set an API key."
        monkeypatch.setattr(
            job_matcher, "get_llm_service", Mock(side_effect=ValueError(message))
        )

        with pytest.raises(ValueError, match="No LLM providers configured"):
            job_matcher.match_resume_job(_resume(), _job())

    @pytest.mark.parametrize("message", _PROVIDER_FAILURE_MESSAGES)
    def test_resume_analysis_propagates_provider_failures(self, monkeypatch, message):
        from services import resume_analyzer

        monkeypatch.setattr(
            resume_analyzer, "get_llm_service", lambda: _failing_llm(message)
        )

        with pytest.raises(Exception) as exc_info:
            resume_analyzer.analyze_resume("Ada Lovelace Python SQL PostgreSQL APIs")
        assert message in str(exc_info.value)

    @pytest.mark.parametrize("message", _PROVIDER_FAILURE_MESSAGES)
    def test_job_analysis_propagates_provider_failures(self, monkeypatch, message):
        from services import job_analyzer

        monkeypatch.setattr(job_analyzer, "get_llm_service", lambda: _failing_llm(message))

        with pytest.raises(Exception) as exc_info:
            job_analyzer.analyze_job_description("Backend engineer role requiring Python and Docker")
        assert message in str(exc_info.value)

    @pytest.mark.parametrize("message", _PROVIDER_FAILURE_MESSAGES)
    def test_job_matching_propagates_provider_failures(self, monkeypatch, message):
        from services import job_matcher

        monkeypatch.setattr(job_matcher, "get_llm_service", lambda: _failing_llm(message))

        with pytest.raises(Exception) as exc_info:
            job_matcher.match_resume_job(_resume(), _job())
        assert message in str(exc_info.value)

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("API key not provided", "provider is missing or invalid"),
            ("HTTP 429: rate limit exceeded", "rate limit was reached"),
            ("Request timed out", "request timed out"),
            ("connection reset by peer", "connection was reset"),
        ],
    )
    def test_friendly_error_maps_known_provider_failures(self, message, expected):
        from services.career_service import friendly_error

        assert expected in friendly_error(RuntimeError(message), "Resume analysis")

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            (
                "No LLM providers configured. Set API keys and models for: gemini, groq",
                "no AI provider is configured",
            ),
            ("Name or service not known", "could not be reached"),
            ("temporary failure in name resolution", "could not be reached"),
            ("getaddrinfo failed", "could not be reached"),
        ],
    )
    def test_friendly_error_maps_no_provider_and_dns_failures(self, message, expected):
        from services.career_service import friendly_error

        assert expected in friendly_error(RuntimeError(message), "Resume analysis")

    @pytest.mark.parametrize("status_code", [401, 403])
    def test_friendly_error_maps_http_authentication_failures(self, status_code):
        from services.career_service import friendly_error

        request = httpx.Request("POST", "https://api.test/v1/chat/completions")
        response = httpx.Response(status_code, request=request)
        exc = httpx.HTTPStatusError("unauthorized", request=request, response=response)

        assert "provider is missing or invalid" in friendly_error(exc, "Resume analysis")

    def test_friendly_error_does_not_echo_unknown_exception_details(self):
        from services.career_service import friendly_error

        secret = "sk-test-secret-1234567890"
        result = friendly_error(RuntimeError(f"{secret} failed internally"), "Resume analysis")

        assert secret not in result
        assert "failed unexpectedly" in result
