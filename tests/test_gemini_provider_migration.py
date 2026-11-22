from types import SimpleNamespace

import httpx
import pytest

PROVIDER_ENV_NAMES = (
    "AI_PROVIDER_MODE",
    "AI_PRIMARY_PROVIDER",
    "AI_FALLBACK_ENABLED",
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
        "llm_model": "gemini-3.5-flash",
        "ai_provider_mode": "auto",
        "ai_primary_provider": "",
        "ai_fallback_enabled": True,
        "provider_priority": "gemini,groq,ollama",
        "gemini_api_key": "test-gemini-credential",
        "gemini_model": "gemini-3.5-flash",
        "gemini_fallback_models": ["gemini-3.5-flash"],
        "groq_api_key": "test-groq-credential",
        "enable_ollama": False,
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    return settings


class _GeminiFakeLLM:
    provider_name = "gemini"
    model_name = "gemini-3.5-flash"

    def __init__(self, response_model, payload):
        self.response_model = response_model
        self.payload = payload
        self.calls = []

    def generate_structured(self, prompt, response_model, **kwargs):
        self.calls.append({"prompt": prompt, "model": response_model, "kwargs": kwargs})
        assert response_model is self.response_model
        return response_model.model_validate(self.payload)


def test_gemini_api_key_loads_from_gemini_env(monkeypatch):
    from config.settings import Settings

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-credential")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-legacy-credential")

    assert Settings().gemini_api_key == "test-gemini-credential"


def test_gemini_api_key_supports_google_env_alias(monkeypatch):
    from config.settings import Settings

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-legacy-credential")

    assert Settings().gemini_api_key == "test-legacy-credential"


def test_gemini_is_default_provider_without_explicit_selection(monkeypatch):
    from services.llm_service import ProviderManager

    _clear_provider_env(monkeypatch)
    _configure_settings(monkeypatch)
    manager = ProviderManager()
    names = [provider.get_provider_name() for provider in manager.providers]

    assert names[0] == "gemini"
    assert "mistral" not in names
    assert manager.status()["active"] == "gemini"


def test_missing_gemini_api_key_raises_actionable_error(monkeypatch):
    from services.llm_service import GeminiProvider

    _configure_settings(monkeypatch, gemini_api_key="")
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiProvider()


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


def test_no_automatic_mistral_fallback(monkeypatch):
    from services.llm_service import ProviderManager

    _clear_provider_env(monkeypatch)
    _configure_settings(monkeypatch, provider_priority="gemini,mistral,groq")
    manager = ProviderManager()

    assert [provider.get_provider_name() for provider in manager.providers] == ["gemini", "groq"]
    assert "mistral" not in manager.status()["providers"]
    assert manager.test_provider("mistral")["error"] == "Unknown provider"


def test_gemini_provider_failure_error_is_gemini_specific():
    from services.llm_service import _aggregate_provider_error

    error = _aggregate_provider_error(["gemini: Gemini authentication failed. Check credentials"])
    assert "Gemini request failed" in str(error)


def test_gemini_interview_question_generation(monkeypatch):
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
    fake_llm = _GeminiFakeLLM(QuestionSet, payload)
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


def test_gemini_answer_evaluation(monkeypatch):
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
    fake_llm = _GeminiFakeLLM(AnswerEvaluation, payload)
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
