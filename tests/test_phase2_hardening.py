from __future__ import annotations

from backend.config.settings import Settings
from backend.services.llm_service import LLMResponse, ProviderManager, _redact


def test_settings_invalid_provider_priority_normalized(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER_PRIORITY", "gemini,unknown,groq,gemini")
    monkeypatch.delenv("PROVIDER_PRIORITY", raising=False)
    s = Settings()
    assert s.provider_priority == "gemini,unknown,groq,ollama"


def test_settings_invalid_ai_mode_and_primary_fallback(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER_MODE", "weird")
    monkeypatch.setenv("AI_PRIMARY_PROVIDER", "unknown")
    s = Settings()
    assert s.ai_provider_mode == "auto"
    assert s.ai_primary_provider == "groq"


def test_settings_numeric_bounds_are_clamped(monkeypatch):
    monkeypatch.setenv("AI_REQUEST_TIMEOUT", "99999")
    monkeypatch.setenv("AI_MAX_RETRIES", "-5")
    monkeypatch.setenv("RESOURCE_CRITICAL_AVAILABLE_MB", "5000")
    monkeypatch.setenv("RESOURCE_LOW_AVAILABLE_MB", "2000")
    s = Settings()
    assert s.ai_request_timeout == 300
    assert s.ai_max_retries == 0
    assert s.resource_low_available_mb == 2000
    assert s.resource_critical_available_mb == 1999


class _BadProvider:
    provider_name = "groq"
    model = "m1"

    def get_provider_name(self):
        return "groq"

    def get_model_name(self):
        return "m1"

    def generate(self, prompt: str, **kwargs):
        return LLMResponse(content="   ", model="m1", provider="groq")

    def generate_structured(self, prompt, response_model, **kwargs):
        raise AssertionError("not used")


class _GoodProvider:
    provider_name = "gemini"
    model = "m2"

    def get_provider_name(self):
        return "gemini"

    def get_model_name(self):
        return "m2"

    def generate(self, prompt: str, **kwargs):
        return LLMResponse(content="ok", model="m2", provider="gemini")

    def generate_structured(self, prompt, response_model, **kwargs):
        raise AssertionError("not used")


def test_provider_manager_falls_back_on_empty_response(monkeypatch):
    from backend.config import settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "ai_provider_mode", "auto")
    monkeypatch.setattr(settings_mod.settings, "ai_fallback_enabled", True)
    monkeypatch.setattr(settings_mod.settings, "ai_primary_provider", "groq")
    monkeypatch.setattr(settings_mod.settings, "provider_priority", "groq,gemini,ollama")
    monkeypatch.setattr(settings_mod.settings, "enable_ollama", False)

    pm = ProviderManager.__new__(ProviderManager)
    pm.requested_provider = None
    pm.registry = {}
    pm._last_used_provider = None
    pm._last_used_model = None
    pm.providers = [_BadProvider(), _GoodProvider()]
    resp = pm.generate("hi")
    assert resp.content == "ok"
    assert resp.provider == "gemini"
    assert pm._last_used_provider == "gemini"


def test_redact_masks_secret_values(monkeypatch):
    import backend.services.llm_service as llm_mod

    monkeypatch.setattr(llm_mod.settings, "groq_api_key", "gsk_abcdefghijklmnopqrstuvwxyz")
    monkeypatch.setattr(llm_mod.settings, "gemini_api_key", "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ1234")
    text = "error gsk_abcdefghijklmnopqrstuvwxyz and AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ1234 ?key=secret-token"
    redacted = _redact(text)
    assert "gsk_abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ1234" not in redacted
    assert "[REDACTED]" in redacted
