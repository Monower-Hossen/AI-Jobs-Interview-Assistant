import os
from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

load_dotenv(ENV_FILE, override=False)

os.environ.setdefault("FAISS_OPT_LEVEL", "generic")

_UNSET = object()
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"


def _is_unset(value: Any) -> bool:
    if value is _UNSET or value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return len(value) == 0
    return False


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _env(name: str, default: str = "") -> str:
    return _text(os.getenv(name)) or default


def _env_list(name: str, default: str = "") -> list[str]:
    raw = _text(os.getenv(name))
    if raw is None:
        return [item.strip() for item in default.split(",") if item.strip()]
    return [item.strip() for item in raw.split(",") if item.strip()]


def _resolve(
    current: Any,
    name: str,
    parse: Callable[[str], Any],
    default: Any,
) -> Any:
    if not _is_unset(current):
        return current
    raw = _text(os.getenv(name))
    if raw is None:
        return default
    try:
        return parse(raw)
    except (TypeError, ValueError):
        return default


def _resolve_number(
    current: Any,
    *names: str,
    parse: Callable[[str], Any],
    default: Any,
) -> Any:
    if not _is_unset(current):
        return current
    for name in names:
        raw = _text(os.getenv(name))
        if raw is None:
            continue
        try:
            return parse(raw)
        except (TypeError, ValueError):
            continue
    return default


def _resolve_bool(current: Any, name: str, default: bool) -> bool:
    if not _is_unset(current):
        return bool(current)
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _resolve_list(current: Any, name: str, default: list[str]) -> list[str]:
    if not _is_unset(current):
        return [str(item).strip() for item in current if str(item).strip()]
    raw = _text(os.getenv(name))
    if raw is None:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def runtime_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _env_path(name: str, default: str) -> str:
    raw = _text(os.getenv(name))
    return str(runtime_path(raw or default).resolve())


@dataclass
class Settings:
    """Runtime configuration with explicit constructor values taking precedence."""

    llm_provider: Any = _UNSET
    llm_model: Any = _UNSET
    ai_provider_mode: Any = _UNSET
    ai_primary_provider: Any = _UNSET
    ai_fallback_enabled: Any = _UNSET
    ai_request_timeout: Any = _UNSET
    ai_max_retries: Any = _UNSET
    provider_circuit_breaker_threshold: Any = _UNSET
    provider_circuit_breaker_cooldown: Any = _UNSET
    groq_base_url: Any = _UNSET

    gemini_api_key: Any = _UNSET
    gemini_model: Any = _UNSET
    gemini_base_url: Any = _UNSET
    gemini_api_version: Any = _UNSET
    gemini_fallback_models: Any = _UNSET

    mistral_api_key: Any = _UNSET
    mistral_model: Any = _UNSET
    mistral_base_url: Any = _UNSET
    mistral_fallback_models: Any = _UNSET

    groq_api_key: Any = _UNSET
    groq_model: Any = _UNSET
    groq_fallback_models: Any = _UNSET

    enable_ollama: Any = _UNSET
    ollama_base_url: Any = _UNSET
    ollama_model: Any = _UNSET

    provider_priority: Any = _UNSET

    max_resume_chars: Any = _UNSET
    max_jd_chars: Any = _UNSET
    max_rag_chunks: Any = _UNSET
    max_questions_per_generation: Any = _UNSET

    embedding_model: Any = _UNSET
    embedding_device: Any = _UNSET
    embedding_cache_dir: Any = _UNSET
    embedding_cache_enabled: Any = _UNSET

    whisper_model: Any = _UNSET
    whisper_device: Any = _UNSET

    vector_store_type: Any = _UNSET
    vector_store_path: Any = _UNSET

    database_path: Any = _UNSET

    live_media_root: Any = _UNSET
    live_media_max_bytes: Any = _UNSET
    live_media_max_seconds: Any = _UNSET

    max_file_size_mb: Any = _UNSET
    allowed_extensions: Any = _UNSET

    chunk_size: Any = _UNSET
    chunk_overlap: Any = _UNSET
    top_k_retrieval: Any = _UNSET
    max_context_chars: Any = _UNSET

    request_timeout: Any = _UNSET
    max_retries: Any = _UNSET
    retry_backoff_factor: Any = _UNSET
    max_tokens: Any = _UNSET

    streamlit_port: Any = _UNSET
    streamlit_theme: Any = _UNSET

    temp_dir: Any = _UNSET

    enable_registration: Any = _UNSET
    admin_email: Any = _UNSET
    password_min_length: Any = _UNSET

    def __post_init__(self) -> None:
        self.llm_provider = _resolve(self.llm_provider, "LLM_PROVIDER", str, "auto")
        self.llm_model = _resolve(self.llm_model, "LLM_MODEL", str, "groq/compound")
        self.ai_provider_mode = _resolve(self.ai_provider_mode, "AI_PROVIDER_MODE", str, "auto")
        self.ai_primary_provider = _resolve(self.ai_primary_provider, "AI_PRIMARY_PROVIDER", str, "")
        self.ai_fallback_enabled = _resolve_bool(
            self.ai_fallback_enabled, "AI_FALLBACK_ENABLED", True
        )
        self.ai_request_timeout = _resolve_number(
            self.ai_request_timeout,
            "AI_REQUEST_TIMEOUT",
            "REQUEST_TIMEOUT",
            parse=int,
            default=60,
        )
        self.ai_max_retries = _resolve_number(
            self.ai_max_retries,
            "AI_MAX_RETRIES",
            "MAX_RETRIES",
            parse=int,
            default=2,
        )
        self.provider_circuit_breaker_threshold = _resolve(
            self.provider_circuit_breaker_threshold,
            "PROVIDER_CIRCUIT_BREAKER_THRESHOLD",
            int,
            3,
        )
        self.provider_circuit_breaker_cooldown = _resolve(
            self.provider_circuit_breaker_cooldown,
            "PROVIDER_CIRCUIT_BREAKER_COOLDOWN",
            int,
            60,
        )
        self.groq_base_url = _resolve(
            self.groq_base_url, "GROQ_BASE_URL", str, "https://api.groq.com/openai/v1"
        )

        self.gemini_api_key = _resolve(self.gemini_api_key, "GEMINI_API_KEY", str, "") or _env(
            "GOOGLE_API_KEY"
        )
        self.gemini_model = _resolve(
            self.gemini_model, "GEMINI_MODEL", str, DEFAULT_GEMINI_MODEL
        )
        self.gemini_base_url = _resolve(
            self.gemini_base_url,
            "GEMINI_BASE_URL",
            str,
            "https://generativelanguage.googleapis.com/v1beta/openai",
        )
        self.gemini_api_version = _resolve(
            self.gemini_api_version, "GEMINI_API_VERSION", str, "v1beta"
        )
        self.gemini_fallback_models = _resolve_list(
            self.gemini_fallback_models,
            "GEMINI_FALLBACK_MODELS",
            [self.gemini_model],
        )

        self.mistral_api_key = _resolve(self.mistral_api_key, "MISTRAL_API_KEY", str, "")
        self.mistral_model = _resolve(
            self.mistral_model, "MISTRAL_MODEL", str, "mistral-small-latest"
        )
        self.mistral_base_url = _resolve(
            self.mistral_base_url, "MISTRAL_BASE_URL", str, "https://api.mistral.ai/v1"
        )
        self.mistral_fallback_models = _resolve_list(
            self.mistral_fallback_models,
            "MISTRAL_FALLBACK_MODELS",
            ["mistral-large-latest"],
        )

        self.groq_api_key = _resolve(self.groq_api_key, "GROQ_API_KEY", str, "")
        self.groq_model = _resolve(self.groq_model, "GROQ_MODEL", str, "openai/gpt-oss-20b")
        self.groq_fallback_models = _resolve_list(
            self.groq_fallback_models,
            "GROQ_FALLBACK_MODELS",
            ["openai/gpt-oss-20b", "allam-2-7b"],
        )

        self.enable_ollama = _resolve_bool(self.enable_ollama, "ENABLE_OLLAMA", False)
        self.ollama_base_url = _resolve(
            self.ollama_base_url, "OLLAMA_BASE_URL", str, "http://localhost:11434"
        )
        self.ollama_model = _resolve(self.ollama_model, "OLLAMA_MODEL", str, "qwen3.5:4b")

        self.provider_priority = _resolve(
            self.provider_priority,
            "PROVIDER_PRIORITY",
            str,
            "groq,gemini,ollama,mistral",
        )

        self.max_resume_chars = _resolve(self.max_resume_chars, "MAX_RESUME_CHARS", int, 8000)
        self.max_jd_chars = _resolve(self.max_jd_chars, "MAX_JD_CHARS", int, 6000)
        self.max_rag_chunks = _resolve(self.max_rag_chunks, "MAX_RAG_CHUNKS", int, 5)
        self.max_questions_per_generation = _resolve(
            self.max_questions_per_generation, "MAX_QUESTIONS_PER_GENERATION", int, 5
        )

        self.embedding_model = _resolve(
            self.embedding_model,
            "EMBEDDING_MODEL",
            str,
            "sentence-transformers/all-MiniLM-L6-v2",
        )
        self.embedding_device = _resolve(self.embedding_device, "EMBEDDING_DEVICE", str, "cpu")
        self.embedding_cache_dir = _env_path("EMBEDDING_CACHE_DIR", "data/embedding_cache")
        self.embedding_cache_enabled = _resolve_bool(
            self.embedding_cache_enabled, "EMBEDDING_CACHE_ENABLED", True
        )

        self.whisper_model = _resolve(self.whisper_model, "WHISPER_MODEL", str, "base")
        self.whisper_device = _resolve(self.whisper_device, "WHISPER_DEVICE", str, "cpu")

        self.vector_store_type = _resolve(self.vector_store_type, "VECTOR_STORE_TYPE", str, "faiss")
        self.vector_store_path = _env_path("VECTOR_STORE_PATH", "data/vector_store")
        self.database_path = _env_path("DATABASE_PATH", "data/interview_assistant.db")

        self.live_media_root = _env_path("LIVE_MEDIA_STORAGE_PATH", "storage/live_interviews")
        self.live_media_max_bytes = _resolve(
            self.live_media_max_bytes, "LIVE_MEDIA_MAX_BYTES", int, 250 * 1024 * 1024
        )
        self.live_media_max_seconds = _resolve(
            self.live_media_max_seconds, "LIVE_MEDIA_MAX_SECONDS", int, 3600
        )

        self.max_file_size_mb = _resolve(self.max_file_size_mb, "MAX_FILE_SIZE_MB", int, 10)
        self.allowed_extensions = _resolve_list(
            self.allowed_extensions,
            "ALLOWED_EXTENSIONS",
            [".pdf", ".docx", ".txt", ".wav", ".mp3", ".m4a", ".ogg", ".flac"],
        )

        self.chunk_size = _resolve(self.chunk_size, "CHUNK_SIZE", int, 1000)
        self.chunk_overlap = _resolve(self.chunk_overlap, "CHUNK_OVERLAP", int, 200)
        self.top_k_retrieval = _resolve(self.top_k_retrieval, "TOP_K_RETRIEVAL", int, 5)
        self.max_context_chars = _resolve(self.max_context_chars, "MAX_CONTEXT_CHARS", int, 6000)

        self.request_timeout = _resolve(self.request_timeout, "REQUEST_TIMEOUT", int, 60)
        self.max_retries = _resolve(self.max_retries, "MAX_RETRIES", int, 3)
        self.retry_backoff_factor = _resolve(
            self.retry_backoff_factor, "RETRY_BACKOFF_FACTOR", float, 1.5
        )
        self.max_tokens = _resolve(self.max_tokens, "MAX_TOKENS", int, 1800)

        self.streamlit_port = _resolve(self.streamlit_port, "STREAMLIT_PORT", int, 8501)
        self.streamlit_theme = _resolve(self.streamlit_theme, "STREAMLIT_THEME", str, "dark")
        self.temp_dir = _env_path("TEMP_DIR", "data/temp")

        self.enable_registration = _resolve_bool(
            self.enable_registration, "ENABLE_REGISTRATION", True
        )
        self.admin_email = _env("ADMIN_EMAIL").strip().lower()
        self.password_min_length = _resolve(
            self.password_min_length, "PASSWORD_MIN_LENGTH", int, 8
        )

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ENV_FILE, override=False)
        return cls()


class _SettingsProxy:
    """Stable proxy so every ``from config.settings import settings`` sees reloads."""

    def __init__(self) -> None:
        object.__setattr__(self, "_current", Settings())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._current, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_current":
            object.__setattr__(self, name, value)
        else:
            setattr(self._current, name, value)

    def as_dict(self) -> dict[str, Any]:
        return self._current.as_dict()


settings = cast(Settings, _SettingsProxy())


def reload_settings() -> Settings:
    """Re-read environment variables and refresh the shared settings proxy in place."""
    load_dotenv(ENV_FILE, override=False)
    object.__setattr__(settings, "_current", Settings())
    return cast(Settings, settings)
