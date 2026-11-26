import logging
import os
import random
import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
from config.settings import settings
from pydantic import BaseModel, ValidationError
from utils.helpers import extract_json_from_text, safe_json_parse

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)

GEMINI_KEY_HINT = (
    "Set GEMINI_API_KEY (or GOOGLE_API_KEY for backward compatibility) in .env "
    "or the provider settings."
)

SUPPORTED_PROVIDERS = ("groq", "gemini", "ollama")
DEFAULT_PROVIDER_PRIORITY = "groq,gemini,ollama"
PROVIDER_LABELS = {"groq": "Groq", "gemini": "Gemini", "ollama": "Ollama"}

HEALTH_CONNECTED = "Connected"
HEALTH_NOT_CONFIGURED = "Not configured"
HEALTH_AUTH_FAILED = "Authentication failed"
HEALTH_CONNECTION_FAILED = "Connection failed"
HEALTH_SERVICE_UNAVAILABLE = "Service unavailable"
HEALTH_DISABLED = "Disabled"
HEALTH_NOT_CHECKED = "Not checked"

_SECRET_QUERY_RE = re.compile(
    r"([?&](?:api[_-]?key|key|token)=)[^&\s]+",
    re.IGNORECASE,
)


class GeminiAPIError(Exception):
    pass


class GroqAPIError(Exception):
    pass


def _status_code(exc: BaseException) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return exc.response.status_code
    for attribute in ("code", "status_code", "status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _redact(text: str) -> str:
    result = str(text)
    secrets = (
        settings.gemini_api_key,
        os.getenv("GOOGLE_API_KEY", ""),
        settings.groq_api_key,
    )
    for secret in secrets:
        if secret and len(secret) >= 8:
            result = result.replace(secret, "[REDACTED]")
    result = _SECRET_QUERY_RE.sub(r"\1[REDACTED]", result)
    return result[:500]


def is_api_key_error(exc: BaseException) -> bool:
    msg = str(exc)
    return "API key" in msg or "api key" in msg or isinstance(exc, (GeminiAPIError, GroqAPIError))


def is_auth_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (401, 403) if exc.response else False
    return _status_code(exc) in (401, 403)


def is_rate_limit_error(exc: BaseException) -> bool:
    code = _status_code(exc)
    if code == 429:
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is None:
        return False
    msg = str(exc).lower()
    return "rate limit" in msg or "429" in msg or "quota" in msg


def is_retryable_error(exc: BaseException) -> bool:
    code = _status_code(exc)
    if code in _RETRYABLE_STATUS_CODES:
        return True
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "timeout",
            "timed out",
            "deadline exceeded",
            "deadline_exceeded",
            "temporarily unavailable",
            "service unavailable",
        )
    )


def is_payload_too_large_error(exc: BaseException) -> bool:
    if _status_code(exc) == 413:
        return True
    msg = str(exc).lower()
    return (
        "http 413" in msg
        or "request entity too large" in msg
        or "request too large" in msg
        or "payload too large" in msg
    )


def _error_message(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else "unknown"
        try:
            data = exc.response.json()
            detail = data.get("error", {}).get("message") or data.get("detail") or str(exc)
        except Exception:
            detail = exc.response.text.strip() if exc.response is not None else str(exc)
        return _redact(f"HTTP {status}: {detail}")
    return _redact(str(exc))


def is_model_not_found_error(exc: BaseException) -> bool:
    if _status_code(exc) == 404:
        return True
    message = str(exc).lower()
    return "404" in message or ("model" in message and "not found" in message)


def _gemini_http_options(base_url: str | None = None) -> Any:
    from google.genai import types

    raw_base_url = (
        base_url or os.getenv("GEMINI_BASE_URL", settings.gemini_base_url) or ""
    ).strip().rstrip("/")
    api_version = (
        os.getenv("GEMINI_API_VERSION", settings.gemini_api_version) or "v1beta"
    ).strip("/")
    base_url = "https://generativelanguage.googleapis.com"
    if raw_base_url:
        parsed = urlsplit(raw_base_url)
        if parsed.scheme and parsed.netloc:
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            for part in [item for item in parsed.path.split("/") if item]:
                if part in {"v1beta", "v1"}:
                    api_version = part
                    break
        else:
            base_url = raw_base_url
    return types.HttpOptions(
        base_url=base_url,
        api_version=api_version,
        timeout=int(_request_timeout() * 1000),
    )


@dataclass
class LLMResponse:
    content: str
    model: str
    provider: str
    usage: dict | None = None
    latency_ms: float = 0.0


@dataclass
class ProviderRegistration:
    name: str
    factory: Callable[[], "LLMProvider"]
    enabled: bool = True


@dataclass
class _ProviderRuntimeState:
    failures: int = 0
    cooled_until: float = 0.0
    open_until: float = 0.0
    last_success: float | None = None
    last_failure: float | None = None
    last_error: str | None = None
    last_latency_ms: float | None = None


_RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
_MAX_RETRY_AFTER = 120.0
_MAX_RATE_LIMIT_RETRIES = 1
_SHORT_RATE_LIMIT_RETRY = 5.0
_DEFAULT_RATE_LIMIT_COOLDOWN = 30.0
_RATE_LIMIT_BUFFER = 1.0
_HEALTH_TIMEOUT_SECONDS = 8.0
_request_lock = threading.RLock()
_rate_limit_lock = threading.RLock()
_rate_limit_info: dict[tuple[str, str], float] = {}
_last_request_time: dict[tuple[str, str], float] = {}
_provider_states: dict[tuple[str, str], _ProviderRuntimeState] = {}


def _min_request_interval() -> float:
    configured = os.getenv(
        "LLM_MIN_REQUEST_INTERVAL",
        getattr(settings, "llm_min_request_interval", 0.0),
    )
    try:
        return max(0.0, float(configured))
    except (TypeError, ValueError):
        return 0.0


def _wait_for_request_slot(key: tuple[str, str]) -> None:
    interval = _min_request_interval()
    if interval <= 0:
        return
    while True:
        with _request_lock:
            now = time.time()
            last = _last_request_time.get(key)
            remaining = interval - (now - last) if last is not None else 0.0
            if remaining <= 0:
                _last_request_time[key] = now
                return
        time.sleep(remaining)


def _retry_after_seconds(response: httpx.Response | None) -> float | None:
    if response is None:
        return None

    value = response.headers.get("retry-after", "").strip()
    if value:
        try:
            return max(0.0, min(float(value), _MAX_RETRY_AFTER))
        except ValueError:
            pass

        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.astimezone()
            return max(
                0.0,
                min(
                    (retry_at - datetime.now().astimezone()).total_seconds(),
                    _MAX_RETRY_AFTER,
                ),
            )
        except (TypeError, ValueError, OverflowError):
            pass

    value = response.headers.get("retry-after-ms", "").strip()
    if value:
        try:
            return max(0.0, min(float(value) / 1000.0, _MAX_RETRY_AFTER))
        except ValueError:
            pass

    value = response.headers.get("x-ratelimit-reset-after", "").strip()
    if value:
        try:
            return max(0.0, min(float(value), _MAX_RETRY_AFTER))
        except ValueError:
            pass

    value = response.headers.get("x-ratelimit-reset-ms", "").strip()
    if value:
        try:
            return max(0.0, min(float(value) / 1000.0, _MAX_RETRY_AFTER))
        except ValueError:
            pass

    value = response.headers.get("x-ratelimit-reset", "").strip()
    if value:
        try:
            reset_at = float(value)
            if reset_at > 10_000_000_000:
                reset_at /= 1000.0
            return max(0.0, min(reset_at - time.time(), _MAX_RETRY_AFTER))
        except ValueError:
            pass

    return None


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    retry_after = _retry_after_seconds(response)
    if retry_after is not None:
        delay = retry_after
    else:
        delay = min(
            settings.retry_backoff_factor * (2**attempt),
            _MAX_RETRY_AFTER,
        )

    jitter = random.uniform(0.0, min(delay * 0.25, 1.0))
    return min(delay + jitter, _MAX_RETRY_AFTER)


def _max_attempts() -> int:
    configured = getattr(settings, "ai_max_retries", None)
    if configured is None:
        configured = getattr(settings, "max_retries", 2)
    try:
        return max(1, int(configured or 0) + 1)
    except (TypeError, ValueError):
        return 3


def _request_timeout() -> float:
    configured = getattr(settings, "ai_request_timeout", None)
    if configured is None:
        configured = getattr(settings, "request_timeout", 60)
    try:
        return min(300.0, max(5.0, float(configured or 60)))
    except (TypeError, ValueError):
        return 60.0


class LLMProvider(ABC):
    provider_name = ""

    @abstractmethod
    def generate(self, prompt: str, **kwargs) -> LLMResponse:
        raise NotImplementedError

    @abstractmethod
    def generate_structured(self, prompt: str, response_model: type[T], **kwargs) -> T:
        raise NotImplementedError

    @abstractmethod
    def get_model_name(self) -> str:
        raise NotImplementedError

    def get_provider_name(self) -> str:
        return getattr(self, "provider_name", self.__class__.__name__.lower())


def _generate_structured(
    provider: LLMProvider,
    prompt: str,
    response_model: type[T],
    **kwargs,
) -> T:
    json_prompt = (
        prompt + "\n\nIMPORTANT: Return ONLY one valid JSON object. "
        "No markdown, no ``` fences, no explanation."
    )

    kwargs.setdefault("max_tokens", 2200)
    kwargs.setdefault("temperature", 0.1)
    kwargs.setdefault("json_mode", True)

    last_error = None
    for attempt in range(2):
        response = provider.generate(json_prompt, **kwargs)
        parsed = safe_json_parse(response.content)

        if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
            parsed = parsed[0]

        if not isinstance(parsed, dict):
            parsed = extract_json_from_text(response.content)

        if not isinstance(parsed, dict):
            last_error = ValueError(
                f"LLM returned invalid JSON (attempt {attempt + 1}). Raw: {response.content[:500]}"
            )
            if attempt == 0:
                json_prompt = (
                    "Return ONLY a valid JSON object matching this exact structure. "
                    "No text before or after. No markdown.\n\n" + json_prompt
                )
            continue

        try:
            return response_model.model_validate(parsed)
        except ValidationError as exc:
            last_error = ValueError(f"Validation failed: {exc}")
            if attempt == 0:
                json_prompt = (
                    "Return ONLY a valid JSON object. All fields required. "
                    "No optional fields. No explanations.\n\n" + json_prompt
                )
            continue

    raise last_error or ValueError("Failed to generate structured response after retries")


class _HTTPProvider(LLMProvider):
    provider_name = ""
    default_timeout = 30
    supports_json_mode = True

    def __init__(self, api_key: str, model: str, base_url: str):
        if not api_key:
            raise ValueError(f"{self.provider_name.upper()}_API_KEY not provided")

        timeout = httpx.Timeout(
            connect=10.0,
            read=_request_timeout(),
            write=15.0,
            pool=10.0,
        )
        self.model = model
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    def _post(self, path: str, payload: dict) -> httpx.Response:
        max_attempts = _max_attempts()
        throttle_key = (self.provider_name, self.model)

        for attempt in range(max_attempts):
            _wait_for_request_slot(throttle_key)

            try:
                response = self.client.post(path, json=payload)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                if attempt + 1 >= max_attempts:
                    raise
                time.sleep(_retry_delay(None, attempt))
                continue

            if response.status_code == 429:
                retry_after = _retry_after_seconds(response)
                if (
                    attempt < _MAX_RATE_LIMIT_RETRIES
                    and retry_after is not None
                    and retry_after <= _SHORT_RATE_LIMIT_RETRY
                ):
                    time.sleep(_retry_delay(response, attempt))
                    continue
                return response

            if response.status_code not in _RETRYABLE_STATUS_CODES or attempt + 1 >= max_attempts:
                return response

            time.sleep(_retry_delay(response, attempt))

        raise RuntimeError(f"No response from {self.provider_name}")

    def generate(self, prompt: str, **kwargs) -> LLMResponse:
        start = time.perf_counter()

        messages = kwargs.get("messages") or [{"role": "user", "content": prompt}]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", 0.2),
            "max_tokens": kwargs.get("max_tokens", 1800),
        }

        if kwargs.get("json_mode", False) and self.supports_json_mode:
            payload["response_format"] = {"type": "json_object"}

        empty_attempts = min(2, _max_attempts())
        for attempt in range(empty_attempts):
            response = self._post("/chat/completions", payload)
            response.raise_for_status()
            data = response.json()

            choices = data.get("choices") or []
            content = choices[0].get("message", {}).get("content", "") if choices else ""
            usage = data.get("usage")

            if content and content.strip():
                return LLMResponse(
                    content=content,
                    model=self.model,
                    provider=self.provider_name,
                    usage=usage,
                    latency_ms=(time.perf_counter() - start) * 1000,
                )
            if attempt + 1 < empty_attempts:
                time.sleep(_retry_delay(None, attempt))

        raise ValueError("Empty response from " + self.provider_name)

    def generate_structured(self, prompt: str, response_model: type[T], **kwargs) -> T:
        return _generate_structured(self, prompt, response_model, **kwargs)

    def get_model_name(self) -> str:
        return self.model


class GeminiProvider(LLMProvider):
    provider_name = "gemini"

    def __init__(self, api_key=None, model=None, base_url=None):
        api_key = api_key or settings.gemini_api_key
        if not api_key:
            raise ValueError(f"Gemini API key not provided. {GEMINI_KEY_HINT}")
        self.api_key = api_key
        self.model = model or os.getenv("GEMINI_MODEL", settings.gemini_model)
        self.base_url = base_url
        self._last_used_model: str | None = None
        self._genai: Any = None
        self._types: Any = None
        self._client: Any = None

    def _load_sdk(self) -> None:
        if self._client is not None:
            return
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise GeminiAPIError(
                "Google Gen AI SDK is not installed. Install google-genai to use Gemini."
            ) from exc
        self._genai = genai
        self._types = types
        self._client = genai.Client(
            api_key=self.api_key,
            http_options=_gemini_http_options(self.base_url),
        )

    def _model_candidates(self) -> list[str]:
        candidates = [self.model]
        for fallback in list(settings.gemini_fallback_models or []):
            name = str(fallback).strip()
            if name and name not in candidates:
                candidates.append(name)
        return candidates

    @staticmethod
    def _prompt_from_messages(prompt: str, messages: Any) -> str:
        if not messages:
            return prompt
        if len(messages) == 1 and isinstance(messages[0], dict):
            if messages[0].get("role") == "user":
                return str(messages[0].get("content", prompt))
        return "\n".join(
            f"{str(message.get('role', 'user')).upper()}: {message.get('content', '')}"
            for message in messages
            if isinstance(message, dict)
        )

    @staticmethod
    def _usage_dict(value: Any) -> dict | None:
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        if hasattr(value, "model_dump"):
            return value.model_dump(exclude_none=True)
        return {}

    def _call_with_retries(self, prompt: str, config: Any, model: str) -> Any:
        for attempt in range(_max_attempts()):
            _wait_for_request_slot((self.provider_name, model))
            try:
                return self._client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config,
                )
            except Exception as exc:
                if is_auth_error(exc):
                    raise GeminiAPIError(
                        f"Gemini authentication failed for {model}: {_error_message(exc)}. "
                        f"{GEMINI_KEY_HINT}"
                    ) from exc
                wrapped = GeminiAPIError(f"Gemini SDK error ({model}): {_error_message(exc)}")
                if attempt + 1 >= _max_attempts() or not is_retryable_error(wrapped):
                    raise wrapped from exc
                time.sleep(_retry_delay(None, attempt))
        raise GeminiAPIError(f"Gemini SDK error ({model}): no response")

    def generate(self, prompt: str, **kwargs) -> LLMResponse:
        start = time.perf_counter()
        self._load_sdk()

        config_values: dict[str, Any] = {
            "temperature": kwargs.get("temperature", 0.2),
            "max_output_tokens": kwargs.get("max_tokens", 1800),
        }
        if kwargs.get("json_mode", False):
            config_values["response_mime_type"] = "application/json"

        config = self._types.GenerateContentConfig(**config_values)
        content_prompt = self._prompt_from_messages(prompt, kwargs.get("messages"))
        errors: list[str] = []
        last_exc: BaseException | None = None

        for model in self._model_candidates():
            try:
                response = self._call_with_retries(content_prompt, config, model)
                content = str(getattr(response, "text", "") or "")
                if not content.strip():
                    raise GeminiAPIError(f"Empty response from Gemini model {model}")
                self._last_used_model = model
                return LLMResponse(
                    content=content,
                    model=model,
                    provider=self.provider_name,
                    usage=self._usage_dict(getattr(response, "usage_metadata", None)),
                    latency_ms=(time.perf_counter() - start) * 1000,
                )
            except Exception as exc:
                last_exc = exc
                errors.append(f"{model}: {_error_message(exc)}")
                if not is_model_not_found_error(exc):
                    raise

        detail = "; ".join(errors)
        raise GeminiAPIError(f"Gemini model candidates unavailable: {detail}") from last_exc

    def generate_structured(self, prompt: str, response_model: type[T], **kwargs) -> T:
        return _generate_structured(self, prompt, response_model, **kwargs)

    def get_model_name(self) -> str:
        return self._last_used_model or self.model


class GroqProvider(_HTTPProvider):
    provider_name = "groq"
    supports_json_mode = False

    def __init__(self, api_key=None, model=None, base_url=None):
        super().__init__(
            api_key or settings.groq_api_key,
            model or os.getenv("GROQ_MODEL", settings.groq_model),
            base_url or os.getenv("GROQ_BASE_URL", settings.groq_base_url),
        )


class OllamaProvider(_HTTPProvider):
    provider_name = "ollama"

    def __init__(self, api_key=None, model=None, base_url=None):
        base = (base_url or os.getenv("OLLAMA_BASE_URL", settings.ollama_base_url)).rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        super().__init__(
            api_key or "ollama",
            model or os.getenv("OLLAMA_MODEL", settings.ollama_model),
            base,
        )


def _aggregate_provider_error(errors: list[str]) -> RuntimeError:
    for name, label in (("groq", "Groq"), ("gemini", "Gemini")):
        if any(item.startswith(f"{name}:") for item in errors):
            return RuntimeError(f"{label} request failed. {'; '.join(errors)}")
    return RuntimeError(f"All providers failed. {'; '.join(errors)}")


def _configured_model(name: str) -> str:
    values = {
        "groq": ("GROQ_MODEL", settings.groq_model),
        "gemini": ("GEMINI_MODEL", settings.gemini_model),
        "ollama": ("OLLAMA_MODEL", settings.ollama_model),
    }
    env_name, default = values.get(name, (name.upper() + "_MODEL", name))
    return os.getenv(env_name, default)


def get_provider_registry() -> dict[str, dict[str, str]]:
    """Return the single source of truth for selectable providers and models."""
    return {
        name: {
            "name": name,
            "label": PROVIDER_LABELS[name],
            "model": _configured_model(name),
            "credential_env": "OLLAMA_BASE_URL" if name == "ollama" else f"{name.upper()}_API_KEY",
        }
        for name in SUPPORTED_PROVIDERS
    }


def _health_result_base(name: str, model: str) -> dict[str, Any]:
    return {
        "provider": name,
        "healthy": False,
        "status": HEALTH_NOT_CHECKED,
        "configured": False,
        "enabled": False,
        "model": model,
        "model_available": None,
        "error": None,
        "latency_ms": None,
        "checked_at": time.time(),
    }


class _HealthAuthError(Exception):
    pass


class _HealthServiceError(Exception):
    pass


def _probe_provider(name: str, model: str) -> dict[str, Any]:
    if name == "groq":
        base_url = (os.getenv("GROQ_BASE_URL", settings.groq_base_url) or "").rstrip("/")
        headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
        with httpx.Client(base_url=base_url, headers=headers, timeout=_HEALTH_TIMEOUT_SECONDS) as client:
            response = client.get("/models")
        if response.status_code in (401, 403):
            raise _HealthAuthError("Groq rejected the configured API key")
        response.raise_for_status()
        data = response.json()
        available = {str(item.get("id")) for item in data.get("data", []) if item.get("id")}
        return {
            "model_available": model in available if available else None,
            "detail": None if model in available or not available else "Configured model not listed",
        }

    if name == "gemini":
        from google import genai

        gemini_client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=_gemini_http_options(),
        )
        gemini_models: set[str] = set()
        try:
            for item in gemini_client.models.list():
                value = getattr(item, "name", None)
                if value:
                    gemini_models.add(str(value).split("/")[-1])
        except Exception as exc:
            if is_auth_error(exc) or is_api_key_error(exc):
                raise _HealthAuthError("Gemini rejected the configured API key") from exc
            if is_retryable_error(exc):
                raise httpx.ConnectError(_error_message(exc)) from exc
            raise _HealthServiceError(_error_message(exc)) from exc
        return {
            "model_available": model in gemini_models if gemini_models else None,
            "detail": (
                None
                if model in gemini_models or not gemini_models
                else "Configured model not listed"
            ),
        }

    base_url = (os.getenv("OLLAMA_BASE_URL", settings.ollama_base_url) or "").rstrip("/")
    base_url = base_url.removesuffix("/v1")
    with httpx.Client(base_url=base_url, timeout=_HEALTH_TIMEOUT_SECONDS) as client:
        response = client.get("/api/tags")
    if response.status_code in (401, 403):
        raise _HealthAuthError("Ollama rejected the request")
    response.raise_for_status()
    data = response.json()
    available = {str(item.get("name")) for item in data.get("models", []) if item.get("name")}
    return {
        "model_available": model in available if available else None,
        "detail": None if model in available or not available else "Configured model not listed",
    }


def _evaluate_provider_health(
    name: str,
    *,
    registered: bool = True,
    enabled: bool = True,
) -> dict[str, Any]:
    model = _configured_model(name)
    result = _health_result_base(name, model)
    result["enabled"] = bool(registered and enabled)

    if not registered:
        result["status"] = HEALTH_SERVICE_UNAVAILABLE
        result["error"] = "Unknown provider"
        return result
    if not enabled or (name == "ollama" and not settings.enable_ollama):
        result["status"] = HEALTH_DISABLED
        result["error"] = "Provider disabled"
        return result

    configured = bool(settings.ollama_base_url) if name == "ollama" else bool(
        getattr(settings, f"{name}_api_key", "")
    )
    result["configured"] = configured
    if not configured:
        result["status"] = HEALTH_NOT_CONFIGURED
        result["error"] = (
            "Ollama base URL not configured"
            if name == "ollama"
            else f"{name.upper()}_API_KEY not configured"
        )
        return result

    started = time.perf_counter()
    try:
        probe = _probe_provider(name, model)
    except _HealthAuthError as exc:
        result["status"] = HEALTH_AUTH_FAILED
        result["error"] = _error_message(exc)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        result["status"] = HEALTH_CONNECTION_FAILED
        result["error"] = _error_message(exc)
    except Exception as exc:
        result["status"] = HEALTH_SERVICE_UNAVAILABLE
        result["error"] = _error_message(exc)
    else:
        result["status"] = HEALTH_CONNECTED
        result["healthy"] = True
        result["model_available"] = probe.get("model_available")
        result["error"] = probe.get("detail")
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


class ProviderManager:
    def __init__(self, requested_provider: str | None = None):
        self.requested_provider = requested_provider
        self.registry: dict[str, ProviderRegistration] = {}
        self._last_used_provider: str | None = None
        self._last_used_model: str | None = None
        self._register_defaults()
        self.providers = self._build_chain()

    def _register_defaults(self) -> None:
        factories = {
            "groq": GroqProvider,
            "gemini": GeminiProvider,
            "ollama": OllamaProvider,
        }
        for name in SUPPORTED_PROVIDERS:
            self.register_provider(name, factories[name])

    def register_provider(
        self,
        name: str,
        factory: Callable[[], LLMProvider],
        *,
        enabled: bool = True,
    ) -> None:
        self.registry[name] = ProviderRegistration(name=name, factory=factory, enabled=enabled)

    def _routing_config(self) -> tuple[str, str, bool, list[str]]:
        requested = (
            self.requested_provider
            or settings.ai_primary_provider
            or settings.llm_provider
            or "auto"
        )
        mode = str(getattr(settings, "ai_provider_mode", "auto") or "auto").strip().lower()
        if mode not in {"auto", "manual", "strict", "fixed", "single"}:
            mode = "auto"
        manual_mode = mode in {"manual", "strict", "fixed", "single"}
        fallback = bool(getattr(settings, "ai_fallback_enabled", True)) and not manual_mode
        requested = str(requested).strip().lower()
        priority: list[str] = []
        for item in str(getattr(settings, "provider_priority", DEFAULT_PROVIDER_PRIORITY) or DEFAULT_PROVIDER_PRIORITY).split(","):
            name = item.strip().lower()
            if name and name not in priority:
                priority.append(name)
        if not priority:
            priority = list(SUPPORTED_PROVIDERS)
        return requested, mode, fallback, priority

    def _build_chain(self) -> list[LLMProvider]:
        requested, mode, fallback, priority = self._routing_config()
        primary = (
            requested if requested and requested != "auto" else (priority[0] if priority else "")
        )

        if not fallback or mode in {"manual", "strict", "fixed", "single"}:
            ordered = [primary] if primary else []
        elif primary:
            ordered = [primary] + [name for name in priority if name != primary]
        else:
            ordered = priority

        providers: list[LLMProvider] = []
        for name in ordered:
            registration = self.registry.get(name)
            if registration is None or not registration.enabled:
                continue
            if name == "ollama" and not settings.enable_ollama:
                continue
            try:
                providers.append(registration.factory())
            except (ValueError, ImportError, RuntimeError) as exc:
                logger.debug(
                    "provider.registration_skipped",
                    extra={
                        "event": "provider.registration_skipped",
                        "provider": name,
                        "reason": _error_message(exc),
                    },
                )
                continue

        if not providers:
            available = ", ".join(self.registry)
            raise ValueError(
                f"No LLM providers configured. Set API keys and models for: {available}"
            )
        logger.info(
            "provider.chain_ready",
            extra={
                "event": "provider.chain_ready",
                "requested": requested,
                "mode": mode,
                "fallback_enabled": fallback,
                "chain": [p.get_provider_name() for p in providers],
            },
        )
        return providers

    @staticmethod
    def _state(key: tuple[str, str]) -> _ProviderRuntimeState:
        with _rate_limit_lock:
            return _provider_states.setdefault(key, _ProviderRuntimeState())

    @staticmethod
    def _provider_key(provider: LLMProvider) -> tuple[str, str]:
        return provider.get_provider_name(), provider.get_model_name()

    @staticmethod
    def _rate_limit_cooldown(exc: BaseException) -> float:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            retry_after = _retry_after_seconds(exc.response)
            if retry_after is not None:
                return min(retry_after + _RATE_LIMIT_BUFFER, _MAX_RETRY_AFTER)
        return float(_DEFAULT_RATE_LIMIT_COOLDOWN)

    def _candidates(self) -> list[LLMProvider]:
        now = time.time()
        available: list[LLMProvider] = []
        for provider in self.providers:
            state = self._state(self._provider_key(provider))
            if state.open_until and state.open_until <= now:
                state.open_until = 0.0
                state.failures = 0
            if state.cooled_until and state.cooled_until <= now:
                state.cooled_until = 0.0
            if state.open_until > now or state.cooled_until > now:
                continue
            available.append(provider)
        return available

    def _record_success(self, key: tuple[str, str], latency_ms: float) -> None:
        state = self._state(key)
        state.failures = 0
        state.cooled_until = 0.0
        state.open_until = 0.0
        state.last_success = time.time()
        state.last_latency_ms = latency_ms
        state.last_error = None
        with _rate_limit_lock:
            _rate_limit_info.pop(key, None)

    def _record_failure(self, key: tuple[str, str], exc: BaseException) -> None:
        now = time.time()
        state = self._state(key)
        state.failures += 1
        state.last_failure = now
        state.last_error = _error_message(exc)
        if is_rate_limit_error(exc):
            state.cooled_until = now + self._rate_limit_cooldown(exc)
            with _rate_limit_lock:
                _rate_limit_info[key] = state.cooled_until
        threshold = max(1, int(getattr(settings, "provider_circuit_breaker_threshold", 3)))
        if state.failures >= threshold:
            cooldown = max(1, int(getattr(settings, "provider_circuit_breaker_cooldown", 60)))
            state.open_until = now + cooldown
            logger.warning(
                "provider.circuit_open",
                extra={
                    "event": "provider.circuit_open",
                    "provider": key[0],
                    "model": key[1],
                    "cooldown_seconds": cooldown,
                },
            )

    @property
    def provider_name(self) -> str:
        return self.providers[0].get_provider_name()

    @property
    def model_name(self) -> str:
        return self.providers[0].get_model_name()

    def generate(self, prompt: str, **kwargs) -> LLMResponse:
        errors: list[str] = []
        last_exc: BaseException | None = None
        for provider in self._candidates():
            key = self._provider_key(provider)
            name, model = key
            logger.info(
                "provider.request",
                extra={"event": "provider.request", "provider": name, "model": model},
            )
            try:
                response = provider.generate(prompt, **kwargs)
            except Exception as exc:
                last_exc = exc
                self._record_failure(key, exc)
                errors.append(f"{name}: {_error_message(exc)}")
                logger.warning(
                    "provider.failure",
                    extra={
                        "event": "provider.failure",
                        "provider": name,
                        "model": model,
                        "error_type": type(exc).__name__,
                        "error": _error_message(exc),
                    },
                )
                continue
            if not isinstance(response, LLMResponse) or not response.content or not str(response.content).strip():
                last_exc = ValueError(f"Empty/invalid response from {name}")
                self._record_failure(key, last_exc)
                errors.append(f"{name}: empty/invalid response")
                logger.warning(
                    "provider.invalid_response",
                    extra={"event": "provider.invalid_response", "provider": name, "model": model},
                )
                continue
            self._record_success(key, response.latency_ms)
            fallback_triggered = bool(self.providers) and name != self.providers[0].get_provider_name()
            if fallback_triggered:
                logger.info(
                    "provider.fallback_used",
                    extra={"event": "provider.fallback_used", "provider": name, "model": model},
                )
            self._last_used_provider = name
            self._last_used_model = model
            return response

        if not errors:
            errors.append("all providers temporarily unavailable")
        raise _aggregate_provider_error(errors) from last_exc

    def generate_structured(self, prompt: str, response_model: type[T], **kwargs) -> T:
        errors: list[str] = []
        last_exc: BaseException | None = None
        for provider in self._candidates():
            key = self._provider_key(provider)
            name, model = key
            logger.info(
                "provider.request",
                extra={
                    "event": "provider.request",
                    "provider": name,
                    "model": model,
                    "structured": True,
                },
            )
            try:
                result = provider.generate_structured(prompt, response_model, **kwargs)
            except Exception as exc:
                last_exc = exc
                self._record_failure(key, exc)
                errors.append(f"{name}: {_error_message(exc)}")
                logger.warning(
                    "provider.failure",
                    extra={
                        "event": "provider.failure",
                        "provider": name,
                        "model": model,
                        "structured": True,
                        "error_type": type(exc).__name__,
                        "error": _error_message(exc),
                    },
                )
                continue
            if result is None:
                last_exc = ValueError(f"Empty structured response from {name}")
                self._record_failure(key, last_exc)
                errors.append(f"{name}: empty structured response")
                continue
            self._record_success(key, 0.0)
            fallback_triggered = bool(self.providers) and name != self.providers[0].get_provider_name()
            if fallback_triggered:
                logger.info(
                    "provider.fallback_used",
                    extra={"event": "provider.fallback_used", "provider": name, "model": model, "structured": True},
                )
            self._last_used_provider = name
            self._last_used_model = model
            return result

        if not errors:
            errors.append("all providers temporarily unavailable")
        raise _aggregate_provider_error(errors) from last_exc

    def status(self) -> dict[str, Any]:
        requested, mode, fallback, priority = self._routing_config()
        providers: dict[str, Any] = {}
        for name in self.registry:
            model = _configured_model(name)
            state = self._state((name, model))
            configured = (
                settings.enable_ollama
                if name == "ollama"
                else bool(getattr(settings, f"{name}_api_key", ""))
            )
            providers[name] = {
                "configured": configured,
                "in_chain": name in [item.get_provider_name() for item in self.providers],
                "model": model,
                "circuit_open": state.open_until > time.time(),
                "last_success": state.last_success,
                "last_failure": state.last_failure,
                "last_error": state.last_error,
                "last_latency_ms": state.last_latency_ms,
            }
        return {
            "mode": mode,
            "primary": requested,
            "fallback_enabled": fallback,
            "priority": priority,
            "active": self.provider_name,
            "active_model": self.model_name,
            "last_used_provider": self._last_used_provider,
            "last_used_model": self._last_used_model,
            "fallback_triggered": (
                self._last_used_provider is not None
                and self._last_used_provider != self.provider_name
            ),
            "chain": [item.get_provider_name() for item in self.providers],
            "providers": providers,
        }

    def test_provider(self, name: str) -> dict[str, Any]:
        registration = self.registry.get(name)
        if registration is None:
            return _evaluate_provider_health(name, registered=False)
        return _evaluate_provider_health(
            name,
            registered=True,
            enabled=registration.enabled,
        )

    def health(self) -> dict[str, dict]:
        return {
            name: _evaluate_provider_health(
                name,
                registered=True,
                enabled=registration.enabled,
            )
            for name, registration in self.registry.items()
        }


_provider_manager: ProviderManager | None = None


def get_provider_manager() -> ProviderManager:
    global _provider_manager
    if _provider_manager is None:
        _provider_manager = ProviderManager()
    return _provider_manager


class LLMService:
    def __init__(self, manager: ProviderManager | None = None):
        self.manager = manager or ProviderManager()
        self.providers = self.manager.providers

    @property
    def provider_name(self) -> str:
        return self.manager.provider_name

    @property
    def model_name(self) -> str:
        return self.manager.model_name

    def generate(self, prompt: str, **kwargs) -> LLMResponse:
        return self.manager.generate(prompt, **kwargs)

    def generate_structured(self, prompt: str, response_model: type[T], **kwargs) -> T:
        return self.manager.generate_structured(prompt, response_model, **kwargs)

    def get_provider_name(self) -> str:
        return self.provider_name

    def get_model_name(self) -> str:
        return self.model_name


_llm_service: LLMService | None = None


def get_llm_service() -> LLMService:
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService(manager=get_provider_manager())
    return _llm_service


def reset_llm_service() -> None:
    global _llm_service, _provider_manager
    with _request_lock:
        _last_request_time.clear()
    with _rate_limit_lock:
        _rate_limit_info.clear()
        _provider_states.clear()
    _provider_manager = None
    _llm_service = None


def _build_provider_chain(provider_name: str) -> list[LLMProvider]:
    return ProviderManager(requested_provider=provider_name).providers


def check_provider_health() -> dict[str, dict]:
    return {
        name: _evaluate_provider_health(name)
        for name in get_provider_registry()
    }


def test_provider_connection(name: str) -> dict[str, Any]:
    return _evaluate_provider_health(name, registered=name in get_provider_registry())


def get_provider_status() -> dict[str, Any]:
    return get_provider_manager().status()
