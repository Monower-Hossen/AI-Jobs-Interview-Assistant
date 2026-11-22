from unittest.mock import Mock, patch

import pytest


# Test Resume Analyzer
class TestResumeAnalyzer:
    def test_resume_analysis_result_structure(self):
        from services.resume_analyzer import ResumeAnalysisResult

        result = ResumeAnalysisResult(
            candidate_name="John Doe",
            email="john@example.com",
            skills=["Python", "SQL"],
            programming_languages=["Python"],
            frameworks=["FastAPI"],
            databases=["PostgreSQL"],
            tools=["Git", "Docker"],
            projects=[],
            certifications=[],
            education=[],
            experience=[],
        )

        assert result.candidate_name == "John Doe"
        assert result.email == "john@example.com"
        assert "Python" in result.skills

    def test_resume_scores_validation(self):
        from services.resume_analyzer import ResumeScores

        scores = ResumeScores(
            technical_skills=85.0,
            experience=70.0,
            projects=90.0,
            education=80.0,
            certifications=60.0,
            structure=75.0,
            overall=78.0,
        )

        assert scores.overall == 78.0
        assert 0 <= scores.technical_skills <= 100


# Test Job Analyzer
class TestJobAnalyzer:
    def test_job_analysis_result_structure(self):
        from services.job_analyzer import JobAnalysisResult

        result = JobAnalysisResult(
            job_title="Software Engineer",
            company="Tech Corp",
            required_skills=["Python", "AWS"],
            preferred_skills=["Docker"],
            programming_languages=["Python"],
            frameworks=["FastAPI"],
            databases=["PostgreSQL"],
            tools=["Git"],
            experience_requirements=["3+ years"],
            education_requirements=["BS Computer Science"],
            responsibilities=["Build APIs"],
            keywords=["python", "aws", "api"],
        )

        assert result.job_title == "Software Engineer"
        assert "Python" in result.required_skills


# Test Job Matcher
class TestJobMatcher:
    def test_match_result_structure(self):
        from services.job_matcher import MissingSkill, ResumeJobMatchResult, SkillMatch

        match = ResumeJobMatchResult(
            technical_skills_match=80.0,
            experience_match=70.0,
            projects_match=90.0,
            education_match=85.0,
            overall_match=81.0,
            matching_skills=[
                SkillMatch(
                    skill="Python",
                    resume_evidence="5 years",
                    job_requirement="Python required",
                    match_strength="strong",
                )
            ],
            missing_skills=[
                MissingSkill(
                    skill="Docker",
                    job_requirement="Containerization",
                    importance="important",
                )
            ],
            skill_gaps=["Docker", "Kubernetes"],
            experience_gaps=[],
            recommendations=["Learn Docker"],
        )

        assert match.overall_match == 81.0
        assert len(match.matching_skills) == 1
        assert len(match.missing_skills) == 1


# Test Interview Generator
class TestInterviewGenerator:
    def test_question_structure(self):
        from services.interview_generator import (
            Difficulty,
            InterviewQuestion,
            QuestionCategory,
        )

        question = InterviewQuestion(
            question_number=1,
            category=QuestionCategory.TECHNICAL,
            difficulty=Difficulty.MEDIUM,
            question_text="Explain Python decorators",
            expected_key_points=["Function wrapping", "@ syntax", "Use cases"],
            follow_up_hints=["Ask about functools.wraps", "Ask about class decorators"],
        )

        assert question.question_number == 1
        assert question.category == QuestionCategory.TECHNICAL
        assert question.difficulty == Difficulty.MEDIUM

    def test_question_set(self):
        from services.interview_generator import (
            Difficulty,
            InterviewQuestion,
            QuestionCategory,
            QuestionSet,
        )

        questions = [
            InterviewQuestion(
                question_number=1,
                category=QuestionCategory.TECHNICAL,
                difficulty=Difficulty.EASY,
                question_text="Q1",
                expected_key_points=[],
            ),
            InterviewQuestion(
                question_number=2,
                category=QuestionCategory.HR,
                difficulty=Difficulty.EASY,
                question_text="Q2",
                expected_key_points=[],
            ),
        ]

        qset = QuestionSet(
            questions=questions,
            total_count=2,
            category_distribution={"technical": 1, "hr": 1},
        )

        assert qset.total_count == 2
        assert len(qset.questions) == 2

    def test_question_set_coerces_sparse_questions(self):
        from services.interview_generator import QuestionSet

        qset = QuestionSet.model_validate(
            {
                "questions": [
                    {"question": "Explain FastAPI DI"},
                    {
                        "question_text": "Walk me through a project",
                        "category": "Project Based",
                        "expected_key_points": "architecture",
                    },
                ]
            }
        )

        assert qset.total_count == 2
        assert qset.questions[0].question_number == 1
        assert qset.questions[0].difficulty.value == "medium"
        assert qset.questions[1].category.value == "project_based"
        assert qset.questions[1].expected_key_points == ["architecture"]
        assert qset.category_distribution == {"technical": 1, "project_based": 1}

    def test_question_generator_retry_prompt_has_no_format_error(self):
        from services.interview_generator import (
            InterviewGenerator,
            QuestionSet,
        )
        from services.job_analyzer import JobAnalysisResult
        from services.job_matcher import ResumeJobMatchResult
        from services.resume_analyzer import ResumeAnalysisResult

        generator = InterviewGenerator.__new__(InterviewGenerator)
        generator.llm = Mock()

        calls = []

        def fake_structured(prompt, response_model, **kwargs):
            calls.append(prompt)
            if len(calls) == 1:
                raise RuntimeError("primary prompt failed")
            return QuestionSet(questions=[], total_count=0, category_distribution={})

        generator.llm.generate_structured.side_effect = fake_structured

        match = ResumeJobMatchResult(
            technical_skills_match=50.0,
            experience_match=50.0,
            projects_match=50.0,
            education_match=50.0,
            overall_match=50.0,
        )

        result = generator.generate(
            ResumeAnalysisResult(),
            JobAnalysisResult(),
            match,
            total_questions=3,
        )

        assert len(calls) == 2
        assert "3 interview questions" in calls[1]
        assert "{total_questions}" not in calls[1]
        assert result.total_count == 0


# Test Answer Evaluator
class TestAnswerEvaluator:
    def test_evaluation_structure(self):
        from services.answer_evaluator import AnswerEvaluation

        eval_result = AnswerEvaluation(
            technical_accuracy=8.0,
            relevance=9.0,
            communication=7.0,
            confidence=8.0,
            completeness=7.5,
            overall_score=7.9,
            strengths=["Good technical knowledge"],
            weaknesses=["Could elaborate more"],
            missing_points=["Time complexity"],
            incorrect_points=[],
            suggested_answer="Better answer...",
            communication_feedback="Clear but concise",
            technical_feedback="Accurate",
        )

        assert eval_result.overall_score == 7.9
        assert 0 <= eval_result.technical_accuracy <= 10


# Test Report Generator
class TestReportGenerator:
    def test_report_structure(self):
        from services.report_generator import ImprovementPlanItem, InterviewReport

        report = InterviewReport(
            overall_score=7.5,
            technical_score=7.8,
            communication_score=7.0,
            hr_score=8.0,
            problem_solving_score=7.2,
            strengths=["Strong technical skills"],
            weaknesses=["Communication needs work"],
            frequently_missed_concepts=["System design"],
            recommended_topics=["Distributed systems"],
            recommended_projects=["Build a microservice"],
            recommended_questions=["Practice system design"],
            improvement_plan_7_day=[
                ImprovementPlanItem(
                    day=1,
                    focus="System Design",
                    activity="Study CAP theorem",
                    resources=["DDIA book"],
                    estimated_hours=2,
                )
            ],
            improvement_plan_30_day=[
                ImprovementPlanItem(
                    day=1,
                    focus="Week 1",
                    activity="System design basics",
                    resources=[],
                    estimated_hours=10,
                )
            ],
        )

        assert report.overall_score == 7.5
        assert len(report.improvement_plan_7_day) == 1


# Test Utilities
class TestUtilities:
    def test_clean_text(self):
        from utils.helpers import clean_text

        text = "  Hello    World\n\n\nTest  "
        cleaned = clean_text(text)
        assert cleaned == "Hello World\n\nTest"

    def test_extract_email(self):
        from utils.helpers import extract_email

        text = "Contact me at john.doe@example.com for more info"
        email = extract_email(text)
        assert email == "john.doe@example.com"

    def test_extract_phone(self):
        from utils.helpers import extract_phone

        text = "Call me at +1-555-123-4567"
        phone = extract_phone(text)
        assert phone is not None

    def test_deduplicate_skills(self):
        from utils.helpers import deduplicate_skills

        skills = ["Python", "python", "PYTHON", "Java", "java"]
        deduped = deduplicate_skills(skills)
        assert len(deduped) == 2
        assert "Python" in deduped
        assert "Java" in deduped

    def test_safe_json_parse(self):
        from utils.helpers import safe_json_parse

        result = safe_json_parse('{"key": "value"}')
        assert result == {"key": "value"}

        result = safe_json_parse("invalid json")
        assert result is None

    def test_calculate_percentage(self):
        from utils.helpers import calculate_percentage

        assert calculate_percentage(75, 100) == 75.0
        assert calculate_percentage(0, 100) == 0.0
        assert calculate_percentage(50, 0) == 0.0


# Test File Utils
class TestFileUtils:
    def test_validate_extension(self):
        from utils.file_utils import validate_file_extension

        assert validate_file_extension("test.pdf") == True
        assert validate_file_extension("test.docx") == True
        assert validate_file_extension("test.txt") == True
        assert validate_file_extension("test.exe") == False

    def test_sanitize_filename(self):
        from utils.file_utils import sanitize_filename

        assert sanitize_filename("My Resume.pdf") == "My_Resume.pdf"
        assert sanitize_filename("test@#$%.docx") == "test.docx"

    def test_get_file_extension(self):
        from utils.file_utils import get_file_extension

        assert get_file_extension("test.PDF") == ".pdf"
        assert get_file_extension("test.Docx") == ".docx"

    def test_is_audio_file(self):
        from utils.file_utils import is_audio_file

        assert is_audio_file("test.wav") == True
        assert is_audio_file("test.mp3") == True
        assert is_audio_file("test.pdf") == False

    def test_is_document_file(self):
        from utils.file_utils import is_document_file

        assert is_document_file("test.pdf") == True
        assert is_document_file("test.docx") == True
        assert is_document_file("test.mp3") == False

    def test_mime_validation_fails_closed_without_content_sniffer(self, tmp_path, monkeypatch):
        from utils import file_utils

        path = tmp_path / "fake.pdf"
        path.write_bytes(b"not a pdf")
        monkeypatch.setattr(file_utils, "HAS_MAGIC", False)

        assert file_utils.validate_mime_type(str(path), ".pdf") is False

    def test_mime_validation_rejects_content_extension_mismatch(self, tmp_path, monkeypatch):
        from utils import file_utils

        path = tmp_path / "fake.pdf"
        path.write_bytes(b"MZ\x90\x00")
        sniffer = Mock()
        sniffer.from_file.return_value = "application/x-msdownload"
        monkeypatch.setattr(file_utils, "HAS_MAGIC", True)
        monkeypatch.setattr(file_utils, "magic", sniffer, raising=False)

        assert file_utils.validate_mime_type(str(path), ".pdf") is False
        sniffer.from_file.assert_called_once_with(str(path), mime=True)

    def test_mime_validation_accepts_docx_container_for_generic_mime(self, tmp_path, monkeypatch):
        from docx import Document
        from utils import file_utils

        path = tmp_path / "resume.docx"
        Document().save(str(path))
        sniffer = Mock()
        sniffer.from_file.return_value = "application/octet-stream"
        monkeypatch.setattr(file_utils, "HAS_MAGIC", True)
        monkeypatch.setattr(file_utils, "magic", sniffer, raising=False)

        assert file_utils.validate_mime_type(str(path), ".docx") is True


# Test RAG Components
class TestRAGComponents:
    @patch("rag.document_loader.pypdf.PdfReader")
    def test_pdf_loader(self, mock_reader):
        from rag.document_loader import PDFLoader

        mock_page = Mock()
        mock_page.extract_text.return_value = "Test content"
        mock_reader.return_value.pages = [mock_page]

        loader = PDFLoader()
        assert loader.supports("test.pdf") == True
        assert loader.supports("test.docx") == False

    def test_text_splitter(self):
        from rag.text_splitter import TextChunk, TextSplitter

        splitter = TextSplitter(chunk_size=100, chunk_overlap=20)
        chunks = splitter.split_text("This is a test. " * 20, metadata={}, source="test")

        assert len(chunks) > 0
        assert all(isinstance(c, TextChunk) for c in chunks)

    def test_embedding_model_singleton(self):
        from rag.embeddings import get_embedding_model

        model1 = get_embedding_model()
        model2 = get_embedding_model()

        assert model1 is model2  # Singleton
        assert model1.dimension > 0

    def test_reset_embedding_model_clears_singleton(self, monkeypatch):
        from config.settings import settings
        from rag import embeddings

        class FakeSentenceTransformer:
            def __init__(self, model_name, device=None):
                self.model_name = model_name

            def get_embedding_dimension(self):
                return 3

        original_model = settings.embedding_model
        monkeypatch.setattr(embeddings, "SentenceTransformer", FakeSentenceTransformer)
        try:
            settings.embedding_model = "model-a"
            embeddings.reset_embedding_model()
            first = embeddings.get_embedding_model()
            assert first._model.model_name == "model-a"

            settings.embedding_model = "model-b"
            embeddings.reset_embedding_model()
            second = embeddings.get_embedding_model()
            assert second is not first
            assert second._model.model_name == "model-b"
        finally:
            settings.embedding_model = original_model
            embeddings.reset_embedding_model()


class TestRuntimeLimits:
    def test_limits_are_read_at_call_time(self, monkeypatch):
        from config.settings import settings
        from services.job_analyzer import get_job_input_limit
        from services.job_matcher import _job_match_limits
        from services.resume_analyzer import get_resume_input_limit

        monkeypatch.setattr(settings, "max_jd_chars", 1234)
        monkeypatch.setattr(settings, "max_resume_chars", 4321)

        assert get_job_input_limit() == 1234
        assert get_resume_input_limit() == 4321
        assert _job_match_limits() == (4321, 1234)


class TestConfigurationStability:
    def test_explicit_zero_and_false_values_are_preserved(self, monkeypatch):
        from config.settings import Settings

        for name in (
            "CHUNK_OVERLAP",
            "AI_MAX_RETRIES",
            "AI_FALLBACK_ENABLED",
            "ENABLE_REGISTRATION",
        ):
            monkeypatch.delenv(name, raising=False)

        values = Settings(
            chunk_overlap=0,
            ai_max_retries=0,
            ai_fallback_enabled=False,
            enable_registration=False,
        )

        assert values.chunk_overlap == 0
        assert values.ai_max_retries == 0
        assert values.ai_fallback_enabled is False
        assert values.enable_registration is False

    def test_runtime_paths_are_project_root_relative(self, monkeypatch, tmp_path):
        from config.settings import PROJECT_ROOT, Settings, runtime_path

        assert runtime_path("data/reports") == PROJECT_ROOT / "data/reports"

        custom = tmp_path / "reports"
        assert runtime_path(custom) == custom

        database = tmp_path / "nested" / "app.db"
        monkeypatch.setenv("DATABASE_PATH", str(database))
        values = Settings()
        assert values.database_path == str(database)

    def test_settings_construction_does_not_create_directories(self, monkeypatch, tmp_path):
        from config.settings import Settings

        target = tmp_path / "new-dir"
        monkeypatch.setenv("TEMP_DIR", str(target))
        Settings()
        assert not target.exists()


class TestGeminiConfiguration:
    def test_http_options_use_v1beta(self, monkeypatch):
        from services.llm_service import _gemini_http_options

        monkeypatch.setenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
        monkeypatch.setenv("GEMINI_API_VERSION", "v1beta")

        options = _gemini_http_options()

        assert options.api_version == "v1beta"
        assert options.base_url.rstrip("/") == "https://generativelanguage.googleapis.com"

    def test_model_not_found_detection(self):
        from services.llm_service import is_model_not_found_error

        assert is_model_not_found_error(Exception("HTTP 404: model not found"))
        assert not is_model_not_found_error(Exception("HTTP 500: server error"))

    def test_fallback_candidates_are_unique(self, monkeypatch):
        from config.settings import settings
        from services.llm_service import GeminiProvider

        monkeypatch.setattr(settings, "gemini_fallback_models", ["model-a", "model-b", "model-a"])
        provider = GeminiProvider(api_key="dummy", model="model-a")

        assert provider._model_candidates() == ["model-a", "model-b"]


# Test Database Models
class TestDatabaseModels:
    def test_user_session_model(self):
        from database.models import UserSession

        session = UserSession(session_id="test123")
        assert session.session_id == "test123"

    def test_resume_analysis_model(self):
        from database.models import ResumeAnalysis

        resume = ResumeAnalysis(
            session_id=1,
            file_name="test.pdf",
            candidate_name="John Doe",
            overall_resume_score=85.0,
        )

        assert resume.candidate_name == "John Doe"
        assert resume.overall_resume_score == 85.0


class TestMultiProvider:
    """Test multi-provider LLM service with fallback."""

    def test_provider_chain_auto(self, monkeypatch):
        from config.settings import settings

        for name in (
            "AI_PROVIDER_MODE",
            "AI_PRIMARY_PROVIDER",
            "AI_FALLBACK_ENABLED",
            "PROVIDER_PRIORITY",
            "LLM_PROVIDER",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(settings, "llm_provider", "auto")
        monkeypatch.setattr(settings, "gemini_api_key", "fake-gemini-key")
        monkeypatch.setattr(settings, "mistral_api_key", "fake-mistral-key")
        monkeypatch.setattr(settings, "groq_api_key", "fake-groq-key")
        monkeypatch.setattr(settings, "enable_ollama", False)
        monkeypatch.setattr(settings, "provider_priority", "groq,gemini,mistral")

        from services.llm_service import LLMService

        service = LLMService()
        names = [p.get_provider_name() for p in service.providers]
        assert names == ["groq", "gemini", "mistral"]

    def test_provider_chain_forced(self, monkeypatch):
        from config.settings import settings

        for name in (
            "AI_PROVIDER_MODE",
            "AI_PRIMARY_PROVIDER",
            "AI_FALLBACK_ENABLED",
            "PROVIDER_PRIORITY",
            "LLM_PROVIDER",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(settings, "llm_provider", "groq")
        monkeypatch.setattr(settings, "gemini_api_key", "fake-gemini-key")
        monkeypatch.setattr(settings, "mistral_api_key", "fake-mistral-key")
        monkeypatch.setattr(settings, "groq_api_key", "fake-groq-key")
        monkeypatch.setattr(settings, "enable_ollama", False)
        monkeypatch.setattr(settings, "provider_priority", "groq,gemini,mistral")

        from services.llm_service import LLMService

        service = LLMService()
        names = [p.get_provider_name() for p in service.providers]
        assert names == ["groq", "gemini", "mistral"]

    def test_routing_config_prefers_primary_and_manual_disables_fallback(self, monkeypatch):
        from config.settings import settings
        from services.llm_service import ProviderManager

        monkeypatch.setattr(settings, "ai_primary_provider", "primary")
        monkeypatch.setattr(settings, "llm_provider", "legacy")
        monkeypatch.setattr(settings, "ai_provider_mode", "manual")
        monkeypatch.setattr(settings, "ai_fallback_enabled", True)
        monkeypatch.setattr(settings, "provider_priority", "primary,legacy,primary,secondary")

        manager = ProviderManager.__new__(ProviderManager)
        manager.requested_provider = None

        requested, mode, fallback, priority = manager._routing_config()

        assert requested == "primary"
        assert mode == "manual"
        assert fallback is False
        assert priority == ["primary", "legacy", "secondary"]

    def test_no_providers_raises(self, monkeypatch):
        from config.settings import settings

        for name in (
            "AI_PROVIDER_MODE",
            "AI_PRIMARY_PROVIDER",
            "AI_FALLBACK_ENABLED",
            "PROVIDER_PRIORITY",
            "LLM_PROVIDER",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(settings, "llm_provider", "auto")
        monkeypatch.setattr(settings, "gemini_api_key", "")
        monkeypatch.setattr(settings, "mistral_api_key", "")
        monkeypatch.setattr(settings, "groq_api_key", "")
        monkeypatch.setattr(settings, "enable_ollama", False)

        from services.llm_service import LLMService

        with pytest.raises(ValueError, match="No LLM providers"):
            LLMService()

    def test_error_classification(self):
        import httpx
        from services.llm_service import (
            is_api_key_error,
            is_auth_error,
            is_rate_limit_error,
            is_retryable_error,
        )

        req = httpx.Request("POST", "https://api.test/v1/chat/completions")
        resp429 = httpx.Response(status_code=429)
        resp401 = httpx.Response(status_code=401)
        resp500 = httpx.Response(status_code=500)

        exc_429 = httpx.HTTPStatusError("rate limit", request=req, response=resp429)
        exc_401 = httpx.HTTPStatusError("unauthorized", request=req, response=resp401)
        exc_500 = httpx.HTTPStatusError("server error", request=req, response=resp500)

        assert is_rate_limit_error(exc_429)
        assert not is_rate_limit_error(exc_500)

        assert is_auth_error(exc_401)
        assert not is_auth_error(exc_500)

        assert is_retryable_error(exc_500)
        assert not is_retryable_error(exc_401)

        assert is_api_key_error(ValueError("Gemini API key not provided"))
        assert not is_api_key_error(exc_401)

    def test_structured_output_coercion(self):
        """Test that string project entries are coerced to ProjectItem dicts."""
        from services.resume_analyzer import ResumeAnalysisWithScores

        data = {
            "analysis": {
                "candidate_name": "John",
                "projects": ["Project A", "Project B"],
                "experience": ["Job 1", "Job 2"],
                "education": ["BS Computer Science"],
            },
            "scores": {
                "technical_skills": 85,
                "experience": 75,
                "projects": 80,
                "education": 90,
                "certifications": 0,
                "structure": 70,
                "overall": 76,
            },
        }

        result = ResumeAnalysisWithScores.model_validate(data)
        assert len(result.analysis.projects) == 2
        assert result.analysis.projects[0].name == "Project A"
        assert len(result.analysis.experience) == 2
        assert result.analysis.experience[0].title == "Job 1"
        assert len(result.analysis.education) == 1
        assert result.analysis.education[0].degree == "BS Computer Science"

    def test_missing_scores_defaults_to_empty(self):
        """Test that missing scores field doesn't cause ValidationError."""
        from services.resume_analyzer import ResumeAnalysisWithScores

        data = {
            "analysis": {
                "candidate_name": "Jane",
                "projects": [],
            }
        }

        result = ResumeAnalysisWithScores.model_validate(data)
        assert result.scores is None  # Optional field

    def test_single_education_item_is_coerced(self):
        from services.resume_analyzer import ResumeAnalysisWithScores

        result = ResumeAnalysisWithScores.model_validate(
            {"degree": "BSc Computer Science", "institution": "Example University"}
        )

        assert result.analysis.education[0].degree == "BSc Computer Science"
        assert result.analysis.education[0].institution == "Example University"

    def test_empty_malformed_response_gets_empty_analysis(self):
        from services.resume_analyzer import ResumeAnalysisWithScores

        result = ResumeAnalysisWithScores.model_validate({})

        assert result.analysis.skills == []
        assert result.scores is None


class TestDatabaseSessions:
    def test_rows_stay_readable_after_context_closes(self, tmp_path):
        import database.db as db_module
        from config.settings import settings
        from database.models import UserSession

        original_path = settings.database_path
        settings.database_path = str(tmp_path / "sessions.db")
        db_module.engine = None
        db_module.SessionLocal = None

        try:
            db_module.init_database()

            with db_module.get_db_context() as db:
                row = UserSession(session_id="abc12345")
                db.add(row)
                db.flush()
                row_id = row.id

            assert row.session_id == "abc12345"
            assert row.id == row_id
            assert row.created_at is not None

            with db_module.get_db_context() as db:
                loaded = db.query(UserSession).filter(UserSession.session_id == "abc12345").first()
                assert loaded is not None

            assert loaded.id == row_id
        finally:
            db_module.close_database()
            db_module.SessionLocal = None
            settings.database_path = original_path

    def test_failed_context_rolls_back_and_leaves_readable(self, tmp_path):
        import database.db as db_module
        from config.settings import settings
        from database.models import UserSession

        original_path = settings.database_path
        settings.database_path = str(tmp_path / "rollback.db")
        db_module.engine = None
        db_module.SessionLocal = None

        try:
            db_module.init_database()

            with pytest.raises(RuntimeError), db_module.get_db_context() as db:
                db.add(UserSession(session_id="rolled-back"))
                db.flush()
                raise RuntimeError("boom")

            with db_module.get_db_context() as db:
                count = (
                    db.query(UserSession).filter(UserSession.session_id == "rolled-back").count()
                )

            assert count == 0
        finally:
            db_module.close_database()
            db_module.SessionLocal = None
            settings.database_path = original_path


class TestReportAndVectorPersistence:
    def test_render_interview_pdf_returns_pdf_bytes(self):
        from services.report_generator import render_interview_pdf

        payload = {
            "interview_date": "2026-01-01T10:00:00",
            "interview_type": "Technical",
            "difficulty": "medium",
            "scores": {"overall": 8.4, "technical": 7.9},
            "strengths": ["Clear explanations"],
            "improvement_plan_7_day": [
                {
                    "day": 1,
                    "focus": "System design",
                    "activity": "Practice caching strategies",
                    "resources": ["design docs"],
                    "estimated_hours": 2.0,
                }
            ],
        }

        pdf = render_interview_pdf(payload)

        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 1000

    def test_vector_store_persists_embedding_model(self, tmp_path):
        import numpy as np
        from rag.text_splitter import TextChunk
        from rag.vector_store import FAISSVectorStore

        class FakeEmbedding:
            dimension = 3
            model_name = "test-model-v1"

            def encode(self, texts):
                if isinstance(texts, str):
                    texts = [texts]
                return np.asarray([[1.0, 0.0, 0.0] for _ in texts], dtype="float32")

        store = FAISSVectorStore(embedding_model=FakeEmbedding())
        store.add_documents(
            [TextChunk(text="hello", metadata={"source": "doc:1"}, chunk_index=0, source="doc:1")]
        )
        store.save(tmp_path)

        metadata = (tmp_path / FAISSVectorStore.META_FILE).read_text(encoding="utf-8")
        assert "test-model-v1" in metadata

        loaded = FAISSVectorStore(embedding_model=FakeEmbedding())
        assert loaded.load(tmp_path) is True
        assert loaded.count() == 1

    def test_vector_store_rejects_index_from_another_model(self, tmp_path):
        import json

        from rag.vector_store import FAISSVectorStore

        class FakeEmbedding:
            dimension = 3
            model_name = "current-model"

            def encode(self, texts):
                return [[1.0, 0.0, 0.0] for _ in texts]

        payload = {
            "dimension": 3,
            "embedding_model": "different-model",
            "documents": [],
            "embeddings": [],
            "faiss_available": False,
        }
        (tmp_path / FAISSVectorStore.META_FILE).write_text(json.dumps(payload), encoding="utf-8")

        store = FAISSVectorStore(embedding_model=FakeEmbedding())
        assert store.load(tmp_path) is False
        assert store.count() == 0


class TestProviderFallbackManager:
    def _manager(self, first, second):
        from services.llm_service import ProviderManager

        manager = ProviderManager.__new__(ProviderManager)
        manager.requested_provider = None
        manager.registry = {}
        manager._last_used_provider = None
        manager._last_used_model = None
        manager.providers = [first, second]
        return manager

    def _provider(self, name, content="answer", error=None):
        from services.llm_service import LLMProvider, LLMResponse

        class FakeProvider(LLMProvider):
            provider_name = name

            def __init__(self):
                self.calls = []

            def generate(self, prompt, **kwargs):
                self.calls.append((prompt, dict(kwargs)))
                if error:
                    raise error
                return LLMResponse(content=content, model=f"{name}-model", provider=name)

            def generate_structured(self, prompt, response_model, **kwargs):
                self.calls.append((prompt, dict(kwargs)))
                if error:
                    raise error
                return response_model.model_validate({"value": "ok"})

            def get_model_name(self):
                return f"{name}-model"

        return FakeProvider()

    def test_fallback_reuses_original_prompt_and_options(self, monkeypatch):
        from services.llm_service import LLMService, reset_llm_service

        reset_llm_service()
        monkeypatch.setattr("services.llm_service.settings.provider_circuit_breaker_threshold", 3)
        first = self._provider("primary", error=RuntimeError("HTTP 500: boom"))
        second = self._provider("secondary", content="grounded answer")
        service = LLMService(manager=self._manager(first, second))

        prompt = "[Source: handbook.pdf]\nPython interview focus"
        response = service.generate(prompt, temperature=0.7, max_tokens=321)

        assert response.provider == "secondary"
        assert response.content == "grounded answer"
        assert second.calls[0][0] == prompt
        assert second.calls[0][1]["temperature"] == 0.7
        assert second.calls[0][1]["max_tokens"] == 321

    def test_fallback_status_records_last_used_provider(self):
        from services.llm_service import LLMService, reset_llm_service

        reset_llm_service()
        first = self._provider("primary", error=RuntimeError("HTTP 500: boom"))
        second = self._provider("secondary", content="grounded answer")
        manager = self._manager(first, second)
        manager.requested_provider = "primary"
        service = LLMService(manager=manager)

        assert service.generate("grounded question").provider == "secondary"
        status = service.manager.status()

        assert status["last_used_provider"] == "secondary"
        assert status["fallback_triggered"] is True

    def test_provider_health_uses_reasoning_token_budget(self):
        from services.llm_service import ProviderManager, ProviderRegistration

        provider = self._provider("ollama")
        manager = ProviderManager.__new__(ProviderManager)
        manager.requested_provider = None
        manager.registry = {"ollama": ProviderRegistration("ollama", lambda: provider)}
        manager._last_used_provider = None
        manager._last_used_model = None
        manager.providers = []

        result = manager.test_provider("ollama")

        assert result["healthy"] is True
        assert result["model"]
        assert result["checked_at"] > 0
        assert provider.calls[0][0] == "Reply with exactly OK"
        assert provider.calls[0][1]["max_tokens"] >= 512

    def test_manual_mode_builds_single_provider_chain(self, monkeypatch):
        from services.llm_service import (
            ProviderManager,
            ProviderRegistration,
            reset_llm_service,
        )

        reset_llm_service()
        for name in (
            "AI_PROVIDER_MODE",
            "AI_PRIMARY_PROVIDER",
            "AI_FALLBACK_ENABLED",
            "PROVIDER_PRIORITY",
            "LLM_PROVIDER",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr("services.llm_service.settings.ai_provider_mode", "manual")
        monkeypatch.setattr("services.llm_service.settings.ai_primary_provider", "primary")
        monkeypatch.setattr("services.llm_service.settings.ai_fallback_enabled", False)
        monkeypatch.setattr("services.llm_service.settings.provider_priority", "primary,secondary")

        manager = ProviderManager.__new__(ProviderManager)
        manager.requested_provider = None
        manager.registry = {
            "primary": ProviderRegistration("primary", lambda: self._provider("primary")),
            "secondary": ProviderRegistration("secondary", lambda: self._provider("secondary")),
        }
        manager.providers = manager._build_chain()

        assert [item.get_provider_name() for item in manager.providers] == ["primary"]

    def test_circuit_breaker_skips_repeatedly_failing_provider(self, monkeypatch):
        from services.llm_service import LLMService, reset_llm_service

        reset_llm_service()
        monkeypatch.setattr("services.llm_service.settings.provider_circuit_breaker_threshold", 1)
        monkeypatch.setattr("services.llm_service.settings.provider_circuit_breaker_cooldown", 60)
        first = self._provider("primary", error=RuntimeError("HTTP 500: boom"))
        second = self._provider("secondary")
        service = LLMService(manager=self._manager(first, second))

        assert service.generate("one").provider == "secondary"
        assert service.generate("two").provider == "secondary"
        assert len(first.calls) == 1
        assert len(second.calls) == 2

    def test_rate_limit_sets_cooldown_without_opening_circuit(self, monkeypatch):
        import httpx
        from services.llm_service import LLMService, reset_llm_service

        reset_llm_service()
        monkeypatch.setattr("services.llm_service.settings.provider_circuit_breaker_threshold", 99)
        request = httpx.Request("POST", "https://api.test/v1/chat/completions")
        response = httpx.Response(429, headers={"retry-after": "30"}, request=request)
        error = httpx.HTTPStatusError("rate limit", request=request, response=response)
        first = self._provider("primary", error=error)
        second = self._provider("secondary")
        service = LLMService(manager=self._manager(first, second))

        assert service.generate("one").provider == "secondary"
        assert service.generate("two").provider == "secondary"
        assert len(first.calls) == 1

    def test_structured_prompt_suffix_is_preserved(self):
        from pydantic import BaseModel
        from services.llm_service import _generate_structured

        class Payload(BaseModel):
            value: str

        provider = self._provider("primary", content='{"value":"ok"}')
        result = _generate_structured(provider, "base prompt", Payload)

        assert result.value == "ok"
        assert provider.calls[0][0].startswith(
            "base prompt\n\nIMPORTANT: Return ONLY one valid JSON object."
        )
        assert provider.calls[0][1]["json_mode"] is True

    def test_gemini_sdk_adapter_maps_structured_options(self):
        from types import SimpleNamespace

        from services.llm_service import GeminiProvider

        class FakeModels:
            def __init__(self):
                self.kwargs = None

            def generate_content(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(text='{"ok": true}', usage_metadata=None)

        models = FakeModels()
        types = SimpleNamespace(
            GenerateContentConfig=lambda **values: values,
            HttpOptions=lambda **values: values,
        )
        provider = GeminiProvider(api_key="fake-key", model="gemini-test")
        provider._types = types
        provider._client = SimpleNamespace(models=models)

        response = provider.generate("Same RAG context", json_mode=True, max_tokens=77)

        assert response.content == '{"ok": true}'
        assert models.kwargs["model"] == "gemini-test"
        assert models.kwargs["contents"] == "Same RAG context"
        assert models.kwargs["config"]["response_mime_type"] == "application/json"
        assert models.kwargs["config"]["max_output_tokens"] == 77


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
