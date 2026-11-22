from types import SimpleNamespace

import httpx
import pytest

REMOVED_PROVIDER = "mistr" + "al"
PROVIDER_ENV_NAMES = (
    "AI_PROVIDER_MODE",
    "AI_PRIMARY_PROVIDER",
    "AI_FALLBACK_ENABLED",
    "AI_PROVIDER_PRIORITY",
    "PROVIDER_PRIORITY",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "ENABLE_OLLAMA",
)


def _clear_provider_env(monkeypatch):
    for name in PROVIDER_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _configure_settings(monkeypatch, **overrides):
    from config.settings import settings

    values = {
        "llm_provider": "auto",
        "llm_model": "openai/gpt-oss-20b",
        "ai_provider_mode": "auto",
        "ai_primary_provider": "",
        "ai_fallback_enabled": True,
        "provider_priority": "groq,gemini,ollama",
        "groq_api_key": "test-groq-credential",
        "groq_model": "openai/gpt-oss-20b",
        "gemini_api_key": "test-gemini-credential",
        "gemini_model": "gemini-3.5-flash",
        "gemini_fallback_models": ["gemini-3.5-flash"],
        "enable_ollama": False,
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    return settings


class _FakeStructuredLLM:
    def __init__(self, response_model, payload):
        self.response_model = response_model
        self.payload = payload
        self.calls = []

    def generate_structured(self, prompt, response_model, **kwargs):
        self.calls.append({"prompt": prompt, "model": response_model, "kwargs": kwargs})
        assert response_model is self.response_model
        return response_model.model_validate(self.payload)


def test_settings_default_to_groq_primary(monkeypatch):
    from config.settings import Settings

    _clear_provider_env(monkeypatch)
    configured = Settings()

    assert configured.ai_primary_provider == "groq"
    assert configured.provider_priority == "groq,gemini,ollama"


def test_settings_support_ai_provider_priority_alias(monkeypatch):
    from config.settings import Settings

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("AI_PROVIDER_PRIORITY", "groq,ollama,gemini")
    monkeypatch.setenv("PROVIDER_PRIORITY", "gemini,groq,ollama")

    assert Settings().provider_priority == "groq,ollama,gemini"


def test_groq_api_key_loads_from_env(monkeypatch):
    from config.settings import Settings

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-credential")

    assert Settings().groq_api_key == "test-groq-credential"


def test_gemini_api_key_supports_google_env_alias(monkeypatch):
    from config.settings import Settings

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-legacy-credential")

    assert Settings().gemini_api_key == "test-legacy-credential"


def test_groq_is_default_provider_without_explicit_selection(monkeypatch):
    from services.llm_service import ProviderManager

    _clear_provider_env(monkeypatch)
    _configure_settings(monkeypatch)
    manager = ProviderManager()
    names = [provider.get_provider_name() for provider in manager.providers]

    assert names == ["groq", "gemini"]
    assert manager.status()["active"] == "groq"
    assert manager.status()["priority"] == ["groq", "gemini", "ollama"]


def test_missing_groq_api_key_raises_actionable_error(monkeypatch):
    from services.llm_service import GroqProvider

    _configure_settings(monkeypatch, groq_api_key="")
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        GroqProvider()


def test_invalid_gemini_api_key_raises_gemini_specific_error():
    from services.llm_service import GeminiAPIError, GeminiProvider

    class _UnauthorizedModels:
        def generate_content(self, **kwargs):
            request = httpx.Request("POST", "https://generativelanguage.googleapis.com")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    provider = GeminiProvider(api_key="test-invalid-credential", model="gemini-3.5-flash")
    provider._types = SimpleNamespace(
        GenerateContentConfig=lambda **values: values,
        HttpOptions=lambda **values: values,
    )
    provider._client = SimpleNamespace(models=_UnauthorizedModels())

    with pytest.raises(GeminiAPIError, match="GEMINI_API_KEY"):
        provider.generate("Reply with exactly OK")


def test_no_automatic_removed_provider_fallback(monkeypatch):
    from services.llm_service import ProviderManager, test_provider_connection

    _clear_provider_env(monkeypatch)
    _configure_settings(
        monkeypatch,
        provider_priority=f"groq,{REMOVED_PROVIDER},gemini",
    )
    manager = ProviderManager()

    assert [provider.get_provider_name() for provider in manager.providers] == ["groq", "gemini"]
    assert REMOVED_PROVIDER not in manager.status()["providers"]
    assert test_provider_connection(REMOVED_PROVIDER)["status"] == "Service unavailable"


def test_groq_provider_failure_error_is_groq_specific():
    from services.llm_service import _aggregate_provider_error

    error = _aggregate_provider_error(["groq: Groq authentication failed. Check credentials"])
    assert "Groq request failed" in str(error)


def test_health_reports_not_configured(monkeypatch):
    from services.llm_service import test_provider_connection

    _clear_provider_env(monkeypatch)
    _configure_settings(monkeypatch, groq_api_key="")

    result = test_provider_connection("groq")

    assert result["status"] == "Not configured"
    assert result["healthy"] is False
    assert result["provider"] == "groq"


def test_health_reports_disabled(monkeypatch):
    from services.llm_service import test_provider_connection

    _clear_provider_env(monkeypatch)
    _configure_settings(monkeypatch, enable_ollama=False)

    result = test_provider_connection("ollama")

    assert result["status"] == "Disabled"
    assert result["healthy"] is False


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        ("auth", "Authentication failed"),
        ("connection", "Connection failed"),
        ("service", "Service unavailable"),
    ],
)
def test_health_classifies_probe_failures(monkeypatch, raised, expected):
    from services import llm_service

    _clear_provider_env(monkeypatch)
    _configure_settings(monkeypatch)

    def _probe(name, model):
        if raised == "auth":
            raise llm_service._HealthAuthError("invalid credential")
        if raised == "connection":
            raise httpx.ConnectError("connect failed")
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(llm_service, "_probe_provider", _probe)

    result = llm_service.test_provider_connection("groq")

    assert result["status"] == expected
    assert result["healthy"] is False


def test_health_reports_connected_without_generating(monkeypatch):
    from services import llm_service

    _clear_provider_env(monkeypatch)
    _configure_settings(monkeypatch)
    monkeypatch.setattr(
        llm_service,
        "_probe_provider",
        lambda name, model: {"model_available": True, "detail": None},
    )

    result = llm_service.check_provider_health()["groq"]

    assert result["status"] == "Connected"
    assert result["healthy"] is True
    assert result["model_available"] is True
    assert result["error"] is None


def test_health_never_returns_credentials(monkeypatch):
    from services import llm_service

    _clear_provider_env(monkeypatch)
    secret = "top-secret-groq-value"
    _configure_settings(monkeypatch, groq_api_key=secret)

    def _probe(name, model):
        raise RuntimeError(f"failure using {secret}")

    monkeypatch.setattr(llm_service, "_probe_provider", _probe)

    result = llm_service.test_provider_connection("groq")

    assert secret not in str(result)
    assert "[REDACTED]" in str(result.get("error"))


def test_interview_question_generation(monkeypatch):
    from services import interview_generator
    from services.interview_generator import QuestionSet
    from services.job_analyzer import JobAnalysisResult
    from services.job_matcher import ResumeJobMatchResult
    from services.resume_analyzer import ResumeAnalysisResult

    payload = {
        "questions": [
            {
                "question_number": 1,
                "category": "technical",
                "difficulty": "medium",
                "question_text": "Explain Python decorators.",
                "expected_key_points": ["Functions", "Wrappers"],
                "follow_up_hints": ["Give an example"],
            }
        ]
    }
    fake_llm = _FakeStructuredLLM(QuestionSet, payload)
    monkeypatch.setattr(interview_generator, "get_llm_service", lambda: fake_llm)
    match = ResumeJobMatchResult(
        technical_skills_match=80,
        experience_match=80,
        projects_match=80,
        education_match=80,
        overall_match=80,
    )

    result = interview_generator.generate_interview_questions(
        ResumeAnalysisResult(),
        JobAnalysisResult(),
        match,
        total_questions=1,
    )

    assert fake_llm.calls
    assert fake_llm.calls[0]["model"] is QuestionSet
    assert result.questions[0].question_text == "Explain Python decorators."


def test_answer_evaluation(monkeypatch):
    from services import answer_evaluator
    from services.answer_evaluator import AnswerEvaluation
    from services.interview_generator import InterviewQuestion
    from services.job_analyzer import JobAnalysisResult
    from services.resume_analyzer import ResumeAnalysisResult

    payload = {
        "technical_accuracy": 8,
        "relevance": 9,
        "communication": 8,
        "confidence": 7,
        "completeness": 8,
        "problem_solving": 8,
        "structure": 8,
        "overall_score": 8,
        "strengths": ["Clear explanation"],
        "weaknesses": ["Could add an example"],
        "missing_points": [],
        "incorrect_points": [],
        "suggested_answer": "Include a concrete decorator example.",
        "ideal_answer": "A decorator wraps a function to add behavior.",
        "communication_feedback": "Concise and clear.",
        "technical_feedback": "Accurate with minor gaps.",
    }
    fake_llm = _FakeStructuredLLM(AnswerEvaluation, payload)
    monkeypatch.setattr(answer_evaluator, "get_llm_service", lambda: fake_llm)
    question = InterviewQuestion(
        question_number=1,
        category="technical",
        difficulty="medium",
        question_text="Explain Python decorators.",
    )

    result = answer_evaluator.evaluate_answer(
        question,
        "A decorator wraps a function.",
        ResumeAnalysisResult(),
        JobAnalysisResult(),
    )

    assert fake_llm.calls
    assert fake_llm.calls[0]["model"] is AnswerEvaluation
    assert result.overall_score == 8
    assert result.strengths == ["Clear explanation"]
