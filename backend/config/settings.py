import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

load_dotenv(ENV_FILE, override=False)

os.environ.setdefault("FAISS_OPT_LEVEL", "generic")

_UNSET = object()
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_AI_PRIMARY_PROVIDER = "groq"
DEFAULT_PROVIDER_PRIORITY = "groq,gemini,ollama"
SUPPORTED_PROVIDER_NAMES = frozenset({"groq", "gemini", "ollama"})
SUPPORTED_AI_PROVIDER_MODES = frozenset({"auto", "manual", "strict", "fixed", "single"})


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


def _warn_invalid(name: str, raw: str, default: Any) -> None:
    logger.warning(
        "config.invalid_value",
        extra={"event": "config.invalid_value", "setting": name, "default": default},
    )


def _clamp_number(value: Any, *, minimum: Any, maximum: Any) -> Any:
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


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
        _warn_invalid(name, raw, default)
        return default


def _resolve_number(
    current: Any,
    *names: str,
    parse: Callable[[str], Any],
    default: Any,
    minimum: Any = None,
    maximum: Any = None,
) -> Any:
    if not _is_unset(current):
        value = current
    else:
        value = default
        found = False
        for name in names:
            raw = _text(os.getenv(name))
            if raw is None:
                continue
            try:
                value = parse(raw)
                found = True
                break
            except (TypeError, ValueError):
                _warn_invalid(name, raw, default)
                continue
        if not found and names:
            # keep default
            pass
    try:
        parsed = parse(str(value)) if not isinstance(value, (int, float)) else value
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None or maximum is not None:
        lo = minimum if minimum is not None else parsed
        hi = maximum if maximum is not None else parsed
        parsed = _clamp_number(parsed, minimum=lo, maximum=hi)
    return parsed


def _resolve_bool(current: Any, name: str, default: bool) -> bool:
    if not _is_unset(current):
        return bool(current)
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    _warn_invalid(name, raw, default)
    return default


def _resolve_list(current: Any, name: str, default: list[str]) -> list[str]:
    if not _is_unset(current):
        return [str(item).strip() for item in current if str(item).strip()]
    raw = _text(os.getenv(name))
    if raw is None:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _normalize_provider_priority(value: str) -> str:
    raw_items = [item.strip().lower() for item in str(value or "").split(",") if item.strip()]
    seen: set[str] = set()
    ordered: list[str] = []
    for item in raw_items:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    for item in ("groq", "gemini", "ollama"):
        if item not in seen:
            ordered.append(item)
    return ",".join(ordered) if ordered else DEFAULT_PROVIDER_PRIORITY


def _resolve_provider_priority(current: Any) -> str:
    if not _is_unset(current):
        return _normalize_provider_priority(str(current))
    alias = _text(os.getenv("AI_PROVIDER_PRIORITY"))
    legacy = _text(os.getenv("PROVIDER_PRIORITY"))
    chosen = alias or legacy or DEFAULT_PROVIDER_PRIORITY
    normalized = _normalize_provider_priority(chosen)
    if chosen.strip().lower() != normalized:
        logger.warning(
            "config.provider_priority_normalized",
            extra={
                "event": "config.provider_priority_normalized",
                "default": DEFAULT_PROVIDER_PRIORITY,
            },
        )
    return normalized


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

    live_signal_poll_seconds: Any = _UNSET
    live_signal_max_pending_per_room: Any = _UNSET
    live_signal_max_per_participant_per_minute: Any = _UNSET

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

    resource_guardrails_enabled: Any = _UNSET
    resource_monitor_enabled: Any = _UNSET
    resource_monitor_cache_seconds: Any = _UNSET
    resource_low_available_mb: Any = _UNSET
    resource_critical_available_mb: Any = _UNSET
    resource_allow_concurrent_heavy_ml: Any = _UNSET
    resource_low_memory_mode: Any = _UNSET
    resource_lease_heartbeat_seconds: Any = _UNSET
    resource_max_active_leases: Any = _UNSET
    resource_cpu_admission_threshold_percent: Any = _UNSET
    resource_task_whisper_transcription_limit: Any = _UNSET
    resource_task_video_processing_limit: Any = _UNSET
    resource_task_embedding_generation_limit: Any = _UNSET
    resource_task_live_recording_limit: Any = _UNSET
    resource_task_llm_inference_limit: Any = _UNSET
    resource_task_video_analysis_limit: Any = _UNSET
    resource_task_audio_analysis_limit: Any = _UNSET
    allow_cuda_inference: Any = _UNSET

    def __post_init__(self) -> None:
        self.llm_provider = str(_resolve(self.llm_provider, "LLM_PROVIDER", str, "auto")).strip().lower() or "auto"
        self.llm_model = str(_resolve(self.llm_model, "LLM_MODEL", str, DEFAULT_GROQ_MODEL)).strip() or DEFAULT_GROQ_MODEL
        self.ai_provider_mode = str(
            _resolve(self.ai_provider_mode, "AI_PROVIDER_MODE", str, "auto")
        ).strip().lower() or "auto"
        if self.ai_provider_mode not in SUPPORTED_AI_PROVIDER_MODES:
            _warn_invalid("AI_PROVIDER_MODE", self.ai_provider_mode, "auto")
            self.ai_provider_mode = "auto"
        self.ai_primary_provider = str(
            _resolve(
                self.ai_primary_provider,
                "AI_PRIMARY_PROVIDER",
                str,
                DEFAULT_AI_PRIMARY_PROVIDER,
            )
        ).strip().lower() or DEFAULT_AI_PRIMARY_PROVIDER
        if self.ai_primary_provider not in SUPPORTED_PROVIDER_NAMES:
            _warn_invalid("AI_PRIMARY_PROVIDER", self.ai_primary_provider, DEFAULT_AI_PRIMARY_PROVIDER)
            self.ai_primary_provider = DEFAULT_AI_PRIMARY_PROVIDER
        self.ai_fallback_enabled = _resolve_bool(
            self.ai_fallback_enabled, "AI_FALLBACK_ENABLED", True
        )
        self.ai_request_timeout = _resolve_number(
            self.ai_request_timeout,
            "AI_REQUEST_TIMEOUT",
            "REQUEST_TIMEOUT",
            parse=int,
            default=60,
            minimum=5,
            maximum=300,
        )
        self.ai_max_retries = _resolve_number(
            self.ai_max_retries,
            "AI_MAX_RETRIES",
            "MAX_RETRIES",
            parse=int,
            default=2,
            minimum=0,
            maximum=10,
        )
        self.provider_circuit_breaker_threshold = _resolve_number(
            self.provider_circuit_breaker_threshold,
            "PROVIDER_CIRCUIT_BREAKER_THRESHOLD",
            parse=int,
            default=3,
            minimum=1,
            maximum=20,
        )
        self.provider_circuit_breaker_cooldown = _resolve_number(
            self.provider_circuit_breaker_cooldown,
            "PROVIDER_CIRCUIT_BREAKER_COOLDOWN",
            parse=int,
            default=60,
            minimum=5,
            maximum=3600,
        )
        self.groq_base_url = str(
            _resolve(self.groq_base_url, "GROQ_BASE_URL", str, "https://api.groq.com/openai/v1")
        ).strip() or "https://api.groq.com/openai/v1"

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

        self.groq_api_key = str(_resolve(self.groq_api_key, "GROQ_API_KEY", str, "") or "").strip()
        self.groq_model = str(_resolve(self.groq_model, "GROQ_MODEL", str, DEFAULT_GROQ_MODEL)).strip() or DEFAULT_GROQ_MODEL
        self.groq_fallback_models = _resolve_list(
            self.groq_fallback_models,
            "GROQ_FALLBACK_MODELS",
            ["openai/gpt-oss-20b", "allam-2-7b"],
        )
        self.groq_fallback_models = [str(m).strip() for m in self.groq_fallback_models if str(m).strip()] or [self.groq_model]

        self.enable_ollama = _resolve_bool(self.enable_ollama, "ENABLE_OLLAMA", False)
        self.ollama_base_url = str(
            _resolve(self.ollama_base_url, "OLLAMA_BASE_URL", str, "http://localhost:11434")
        ).strip() or "http://localhost:11434"
        self.ollama_model = str(_resolve(self.ollama_model, "OLLAMA_MODEL", str, "qwen3.5:4b")).strip() or "qwen3.5:4b"

        self.provider_priority = _resolve_provider_priority(self.provider_priority)

        self.max_resume_chars = _resolve_number(
            self.max_resume_chars, "MAX_RESUME_CHARS", parse=int, default=8000, minimum=500, maximum=50000
        )
        self.max_jd_chars = _resolve_number(
            self.max_jd_chars, "MAX_JD_CHARS", parse=int, default=6000, minimum=500, maximum=50000
        )
        self.max_rag_chunks = _resolve_number(
            self.max_rag_chunks, "MAX_RAG_CHUNKS", parse=int, default=5, minimum=1, maximum=20
        )
        self.max_questions_per_generation = _resolve_number(
            self.max_questions_per_generation,
            "MAX_QUESTIONS_PER_GENERATION",
            parse=int,
            default=5,
            minimum=1,
            maximum=20,
        )

        self.embedding_model = str(
            _resolve(
                self.embedding_model,
                "EMBEDDING_MODEL",
                str,
                "sentence-transformers/all-MiniLM-L6-v2",
            )
        ).strip() or "sentence-transformers/all-MiniLM-L6-v2"
        self.embedding_device = str(_resolve(self.embedding_device, "EMBEDDING_DEVICE", str, "cpu")).strip().lower() or "cpu"
        self.embedding_cache_dir = _env_path("EMBEDDING_CACHE_DIR", "data/embedding_cache")
        self.embedding_cache_enabled = _resolve_bool(
            self.embedding_cache_enabled, "EMBEDDING_CACHE_ENABLED", True
        )

        self.whisper_model = str(_resolve(self.whisper_model, "WHISPER_MODEL", str, "base")).strip().lower() or "base"
        self.whisper_device = str(_resolve(self.whisper_device, "WHISPER_DEVICE", str, "cpu")).strip().lower() or "cpu"

        self.resource_guardrails_enabled = _resolve_bool(
            self.resource_guardrails_enabled, "RESOURCE_GUARDRAILS_ENABLED", True
        )
        self.resource_monitor_enabled = _resolve_bool(
            self.resource_monitor_enabled, "RESOURCE_MONITOR_ENABLED", True
        )
        self.resource_monitor_cache_seconds = _resolve_number(
            self.resource_monitor_cache_seconds,
            "RESOURCE_MONITOR_CACHE_SECONDS",
            parse=int,
            default=15,
            minimum=1,
            maximum=300,
        )
        self.resource_low_available_mb = _resolve_number(
            self.resource_low_available_mb,
            "RESOURCE_LOW_AVAILABLE_MB",
            parse=int,
            default=1600,
            minimum=256,
            maximum=32768,
        )
        self.resource_critical_available_mb = _resolve_number(
            self.resource_critical_available_mb,
            "RESOURCE_CRITICAL_AVAILABLE_MB",
            parse=int,
            default=900,
            minimum=128,
            maximum=32768,
        )
        if self.resource_critical_available_mb >= self.resource_low_available_mb:
            self.resource_critical_available_mb = max(128, self.resource_low_available_mb - 1)
        self.resource_allow_concurrent_heavy_ml = _resolve_bool(
            self.resource_allow_concurrent_heavy_ml,
            "RESOURCE_ALLOW_CONCURRENT_HEAVY_ML",
            False,
        )
        self.resource_low_memory_mode = _resolve_bool(
            self.resource_low_memory_mode, "RESOURCE_LOW_MEMORY_MODE", False
        )
        self.resource_lease_heartbeat_seconds = _resolve_number(
            self.resource_lease_heartbeat_seconds,
            "RESOURCE_LEASE_HEARTBEAT_SECONDS",
            parse=int,
            default=300,
            minimum=30,
            maximum=3600,
        )
        self.resource_max_active_leases = _resolve_number(
            self.resource_max_active_leases,
            "RESOURCE_MAX_ACTIVE_LEASES",
            parse=int,
            default=4,
            minimum=1,
            maximum=8,
        )
        self.resource_cpu_admission_threshold_percent = _resolve_number(
            self.resource_cpu_admission_threshold_percent,
            "RESOURCE_CPU_ADMISSION_THRESHOLD_PERCENT",
            parse=float,
            default=85.0,
            minimum=10.0,
            maximum=100.0,
        )
        task_budget_defaults = {
            "resource_task_whisper_transcription_limit": 1,
            "resource_task_video_processing_limit": 1,
            "resource_task_embedding_generation_limit": 1,
            "resource_task_live_recording_limit": 1,
            "resource_task_llm_inference_limit": 4,
            "resource_task_video_analysis_limit": 1,
            "resource_task_audio_analysis_limit": 2,
        }
        for field_name, default_limit in task_budget_defaults.items():
            setattr(
                self,
                field_name,
                _resolve_number(
                    getattr(self, field_name),
                    field_name.upper(),
                    parse=int,
                    default=default_limit,
                    minimum=1,
                    maximum=8,
                ),
            )
        if self.resource_low_memory_mode:
            self.resource_max_active_leases = min(int(self.resource_max_active_leases), 2)
            self.resource_cpu_admission_threshold_percent = min(
                float(self.resource_cpu_admission_threshold_percent), 70.0
            )
            for field_name in task_budget_defaults:
                setattr(self, field_name, 1)
        self.allow_cuda_inference = _resolve_bool(
            self.allow_cuda_inference, "ALLOW_CUDA_INFERENCE", False
        )

        # Phase 1 safe defaults: keep heavy ML local inference CPU-only unless explicitly allowed.
        if not self.allow_cuda_inference:
            if str(self.whisper_device).lower() != "cpu":
                self.whisper_device = "cpu"
            if str(self.embedding_device).lower() != "cpu":
                self.embedding_device = "cpu"

        if self.resource_low_memory_mode and str(self.whisper_model).lower() == "base":
            self.whisper_model = "tiny"

        self.vector_store_type = str(_resolve(self.vector_store_type, "VECTOR_STORE_TYPE", str, "faiss")).strip().lower() or "faiss"
        self.vector_store_path = _env_path("VECTOR_STORE_PATH", "data/vector_store")
        self.database_path = _env_path("DATABASE_PATH", "data/interview_assistant.db")

        self.live_signal_poll_seconds = _resolve_number(
            self.live_signal_poll_seconds,
            "LIVE_SIGNAL_POLL_SECONDS",
            parse=int,
            default=3,
            minimum=1,
            maximum=30,
        )
        self.live_signal_max_pending_per_room = _resolve_number(
            self.live_signal_max_pending_per_room,
            "LIVE_SIGNAL_MAX_PENDING_PER_ROOM",
            parse=int,
            default=100,
            minimum=10,
            maximum=1000,
        )
        self.live_signal_max_per_participant_per_minute = _resolve_number(
            self.live_signal_max_per_participant_per_minute,
            "LIVE_SIGNAL_MAX_PER_PARTICIPANT_PER_MINUTE",
            parse=int,
            default=30,
            minimum=1,
            maximum=300,
        )

        self.live_media_root = _env_path("LIVE_MEDIA_STORAGE_PATH", "storage/live_interviews")
        self.live_media_max_bytes = _resolve_number(
            self.live_media_max_bytes,
            "LIVE_MEDIA_MAX_BYTES",
            parse=int,
            default=250 * 1024 * 1024,
            minimum=1024 * 1024,
            maximum=2 * 1024 * 1024 * 1024,
        )
        self.live_media_max_seconds = _resolve_number(
            self.live_media_max_seconds,
            "LIVE_MEDIA_MAX_SECONDS",
            parse=int,
            default=3600,
            minimum=30,
            maximum=24 * 3600,
        )

        self.max_file_size_mb = _resolve_number(
            self.max_file_size_mb, "MAX_FILE_SIZE_MB", parse=int, default=10, minimum=1, maximum=500
        )
        self.allowed_extensions = _resolve_list(
            self.allowed_extensions,
            "ALLOWED_EXTENSIONS",
            [".pdf", ".docx", ".txt", ".wav", ".mp3", ".m4a", ".ogg", ".flac"],
        )
        self.allowed_extensions = [
            ext.lower() if str(ext).startswith(".") else f".{str(ext).lower()}"
            for ext in self.allowed_extensions
            if str(ext).strip()
        ] or [".pdf", ".docx", ".txt"]

        self.chunk_size = _resolve_number(
            self.chunk_size, "CHUNK_SIZE", parse=int, default=1000, minimum=100, maximum=10000
        )
        self.chunk_overlap = _resolve_number(
            self.chunk_overlap, "CHUNK_OVERLAP", parse=int, default=200, minimum=0, maximum=5000
        )
        if self.chunk_overlap >= self.chunk_size:
            self.chunk_overlap = max(0, self.chunk_size - 1)
        self.top_k_retrieval = _resolve_number(
            self.top_k_retrieval, "TOP_K_RETRIEVAL", parse=int, default=5, minimum=1, maximum=50
        )
        self.max_context_chars = _resolve_number(
            self.max_context_chars, "MAX_CONTEXT_CHARS", parse=int, default=6000, minimum=500, maximum=50000
        )

        self.request_timeout = _resolve_number(
            self.request_timeout, "REQUEST_TIMEOUT", parse=int, default=60, minimum=5, maximum=300
        )
        self.max_retries = _resolve_number(
            self.max_retries, "MAX_RETRIES", parse=int, default=3, minimum=0, maximum=10
        )
        self.retry_backoff_factor = _resolve_number(
            self.retry_backoff_factor,
            "RETRY_BACKOFF_FACTOR",
            parse=float,
            default=1.5,
            minimum=0.1,
            maximum=10.0,
        )
        self.max_tokens = _resolve_number(
            self.max_tokens, "MAX_TOKENS", parse=int, default=1800, minimum=128, maximum=32768
        )

        self.streamlit_port = _resolve_number(
            self.streamlit_port, "STREAMLIT_PORT", parse=int, default=8501, minimum=1, maximum=65535
        )
        self.streamlit_theme = str(_resolve(self.streamlit_theme, "STREAMLIT_THEME", str, "dark")).strip().lower() or "dark"
        self.temp_dir = _env_path("TEMP_DIR", "data/temp")

        self.enable_registration = _resolve_bool(
            self.enable_registration, "ENABLE_REGISTRATION", True
        )
        self.admin_email = _env("ADMIN_EMAIL").strip().lower()
        self.password_min_length = _resolve_number(
            self.password_min_length, "PASSWORD_MIN_LENGTH", parse=int, default=8, minimum=6, maximum=128
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
