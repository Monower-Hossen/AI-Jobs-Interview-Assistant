"""Runtime settings with immediate reload and no secret disclosure."""

import os
from typing import Any

import streamlit as st
from config.settings import reload_settings, settings
from database import UserSettings, get_db_context
from services.auth_service import AuthError, require_admin

from frontend.shared.auth_ui import clear_credential_state
from frontend.shared.components import (
    ai_provider_status,
    error_state,
    page_header,
    section,
    success_toast,
)


def show_settings_page() -> None:
    page_header(
        "Settings",
        "Configure providers, speech, retrieval and workspace preferences",
        ":material/settings:",
    )

    tabs = st.tabs(["Preferences", "Providers", "Speech", "Retrieval", "Data", "About"])

    with tabs[0]:
        _preferences()
    with tabs[1]:
        _provider_settings()
    with tabs[2]:
        _speech_settings()
    with tabs[3]:
        _retrieval_settings()
    with tabs[4]:
        _data_settings()
    with tabs[5]:
        _about()


def _preferences() -> None:
    section("Workspace preferences", ":material/tune:")
    user_id = st.session_state.get("user_id")
    if not user_id:
        st.info("Sign in to save personal preferences.")
        return

    with get_db_context() as db:
        row = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
        current: dict[str, Any] = {
            "theme": row.theme if row else "dark",
            "default_role": row.default_role if row else "",
            "default_difficulty": row.default_difficulty if row else "medium",
            "default_interview_type": row.default_interview_type if row else "Full Interview",
            "llm_provider": row.llm_provider if row else "auto",
            "voice_enabled": row.voice_enabled if row else True,
            "privacy_share_analytics": row.privacy_share_analytics if row else False,
        }

    with st.container(border=True):
        col1, col2 = st.columns(2)
        with col1:
            theme = st.selectbox(
                "Theme", ["dark", "light"], index=0 if current["theme"] == "dark" else 1
            )
            default_role = st.text_input("Default target role", value=current["default_role"])
            difficulty = st.selectbox(
                "Default difficulty",
                ["easy", "medium", "hard"],
                index=(
                    ["easy", "medium", "hard"].index(current["default_difficulty"])
                    if current["default_difficulty"] in ["easy", "medium", "hard"]
                    else 1
                ),
            )
        with col2:
            interview_types = ["Full Interview", "Technical", "Behavioral", "HR", "Mixed"]
            interview_type = st.selectbox(
                "Default interview type",
                interview_types,
                index=(
                    interview_types.index(current["default_interview_type"])
                    if current["default_interview_type"] in interview_types
                    else 0
                ),
            )
            providers_list = ["auto", "groq", "gemini", "mistral", "ollama"]
            provider = st.selectbox(
                "Preferred provider",
                providers_list,
                index=(
                    providers_list.index(current["llm_provider"])
                    if current["llm_provider"] in providers_list
                    else 0
                ),
            )
            voice_enabled = st.toggle("Enable voice answers", value=bool(current["voice_enabled"]))
            share_analytics = st.toggle(
                "Share anonymous usage analytics", value=bool(current["privacy_share_analytics"])
            )

        if st.button("Save preferences", type="primary", icon=":material/save:", width="stretch"):
            with get_db_context() as db:
                row = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
                if not row:
                    row = UserSettings(user_id=user_id)
                    db.add(row)
                row.theme = theme
                row.default_role = default_role
                row.default_difficulty = difficulty
                row.default_interview_type = interview_type
                row.llm_provider = provider
                row.voice_enabled = voice_enabled
                row.privacy_share_analytics = share_analytics
            st.session_state.user_preferences = {
                "default_role": default_role,
                "default_difficulty": difficulty,
                "default_interview_type": interview_type,
                "voice_enabled": voice_enabled,
            }
            success_toast("Preferences saved to your account.")


def _require_admin_settings_access() -> bool:
    account = st.session_state.get("account")
    user_id = st.session_state.get("user_id")
    if account is None or not getattr(account, "is_admin", False):
        error_state("Administrator access required.", "Only administrators can change providers.")
        return False
    try:
        require_admin(user_id)
    except AuthError as exc:
        error_state("Administrator access required.", str(exc))
        return False
    return True


def _apply(env_values: dict[str, str]) -> None:
    require_admin(st.session_state.get("user_id"))
    for key, value in env_values.items():
        if key.endswith("_KEY") and not value:
            continue
        os.environ[key] = value
    reload_settings()
    from services.knowledge_base import reset_knowledge_base
    from services.llm_service import reset_llm_service

    reset_llm_service()
    reset_knowledge_base()
    success_toast("Settings applied for this session.")


def _provider_settings() -> None:
    if not _require_admin_settings_access():
        return
    section("AI providers", ":material/cloud:")
    st.caption(
        "Keys are kept in process memory and are never displayed or written to the app code."
    )
    ai_provider_status()

    providers = ["groq", "gemini", "mistral", "ollama"]
    modes = ["auto", "manual"]
    current_mode = settings.ai_provider_mode if settings.ai_provider_mode in modes else "auto"
    current_primary = settings.ai_primary_provider or settings.llm_provider
    if current_primary == "auto" or current_primary not in providers:
        current_primary = "groq"

    with st.form("provider_settings_form", border=False):
        mode = st.segmented_control("Provider mode", modes, default=current_mode) or "auto"
        col1, col2 = st.columns(2)
        with col1:
            primary = st.selectbox(
                "Primary provider", providers, index=providers.index(current_primary)
            )
            priority = st.text_input("Fallback priority", value=settings.provider_priority)
        with col2:
            fallback = st.toggle(
                "Automatic fallback",
                value=settings.ai_fallback_enabled,
                disabled=mode == "manual",
            )
            timeout = st.number_input(
                "Request timeout (seconds)",
                min_value=5,
                max_value=300,
                value=settings.ai_request_timeout,
            )
            retries = st.number_input(
                "Retries per provider",
                min_value=0,
                max_value=8,
                value=settings.ai_max_retries,
            )
        st.caption("Comma-separated order used when provider mode is `auto`.")

        st.markdown("**Cloud providers**")
        col1, col2 = st.columns(2)
        with col1:
            groq_key = st.text_input("Groq API key", type="password", key="settings_groq_key")
            mistral_key = st.text_input(
                "Mistral API key", type="password", key="settings_mistral_key"
            )
            gemini_key = st.text_input("Gemini API key", type="password", key="settings_gemini_key")
        with col2:
            groq_model = st.text_input("Groq model", value=settings.groq_model)
            mistral_model = st.text_input("Mistral model", value=settings.mistral_model)
            gemini_model = st.text_input("Gemini model", value=settings.gemini_model)

        st.markdown("**Local provider**")
        enable_ollama = st.toggle("Enable Ollama", value=settings.enable_ollama)
        ollama_url = st.text_input("Ollama base URL", value=settings.ollama_base_url)
        ollama_model = st.text_input("Ollama model", value=settings.ollama_model)

        if st.form_submit_button(
            "Save provider settings",
            type="primary",
            icon=":material/save:",
            width="stretch",
        ):
            values = {
                "AI_PROVIDER_MODE": mode,
                "AI_PRIMARY_PROVIDER": primary,
                "AI_FALLBACK_ENABLED": str(fallback and mode == "auto").lower(),
                "AI_REQUEST_TIMEOUT": str(timeout),
                "AI_MAX_RETRIES": str(retries),
                "LLM_PROVIDER": primary,
                "PROVIDER_PRIORITY": priority,
                "GROQ_API_KEY": groq_key,
                "GROQ_MODEL": groq_model,
                "MISTRAL_API_KEY": mistral_key,
                "MISTRAL_MODEL": mistral_model,
                "GEMINI_API_KEY": gemini_key,
                "GEMINI_MODEL": gemini_model,
                "ENABLE_OLLAMA": str(enable_ollama).lower(),
                "OLLAMA_BASE_URL": ollama_url,
                "OLLAMA_MODEL": ollama_model,
            }
            clear_credential_state()
            try:
                _apply(values)
            except AuthError as exc:
                error_state("Provider settings were not applied.", str(exc))
            else:
                st.caption("Add the same variables to `.env` to persist them across restarts.")

    if st.button("Check provider health", icon=":material/cloud:", width="stretch"):
        from services.llm_service import check_provider_health

        with st.spinner("Checking providers…"):
            health = check_provider_health()
        for name, info in health.items():
            if info.get("healthy"):
                latency = info.get("latency_ms")
                suffix = f" · {latency:.0f} ms" if isinstance(latency, (int, float)) else ""
                st.success(
                    f"{name}: healthy · {info.get('model')}{suffix}",
                    icon=":material/check_circle:",
                )
            else:
                st.warning(f"{name}: {info.get('error')}", icon=":material/warning:")


def _speech_settings() -> None:
    if not _require_admin_settings_access():
        return
    section("Speech-to-text", ":material/mic:")
    models = ["tiny", "base", "small", "medium", "large"]
    try:
        model_index = models.index(settings.whisper_model)
    except ValueError:
        model_index = 1
    col1, col2 = st.columns(2)
    with col1:
        model = st.selectbox("Whisper model", models, index=model_index)
    with col2:
        device = st.selectbox(
            "Device", ["cpu", "cuda"], index=0 if settings.whisper_device == "cpu" else 1
        )

    if st.button("Save speech settings", type="primary", icon=":material/save:", width="stretch"):
        from services.speech_to_text import reset_speech_to_text

        _apply({"WHISPER_MODEL": model, "WHISPER_DEVICE": device})
        reset_speech_to_text()


def _retrieval_settings() -> None:
    if not _require_admin_settings_access():
        return
    section("Retrieval and embeddings", ":material/library_books:")
    col1, col2 = st.columns(2)
    with col1:
        chunk_size = st.number_input(
            "Chunk size", value=settings.chunk_size, min_value=100, max_value=5000
        )
        chunk_overlap = st.number_input(
            "Chunk overlap", value=settings.chunk_overlap, min_value=0, max_value=1000
        )
        top_k = st.number_input(
            "Top-K results", value=settings.top_k_retrieval, min_value=1, max_value=20
        )
    with col2:
        max_context = st.number_input(
            "Max context characters",
            value=settings.max_context_chars,
            min_value=1000,
            max_value=30000,
        )
        embedding = st.selectbox(
            "Embedding model",
            [
                "sentence-transformers/all-MiniLM-L6-v2",
                "sentence-transformers/all-mpnet-base-v2",
            ],
            index=0 if "MiniLM" in settings.embedding_model else 1,
        )

    if st.button(
        "Save retrieval settings", type="primary", icon=":material/save:", width="stretch"
    ):
        _apply(
            {
                "CHUNK_SIZE": str(chunk_size),
                "CHUNK_OVERLAP": str(chunk_overlap),
                "TOP_K_RETRIEVAL": str(top_k),
                "MAX_CONTEXT_CHARS": str(max_context),
                "EMBEDDING_MODEL": embedding,
            }
        )


def _data_settings() -> None:
    section("Workspace data", ":material/database:")
    st.caption(f"Database: `{settings.database_path}`")
    st.caption(f"Vector store: `{settings.vector_store_path}`")
    st.caption(
        "Each account is isolated by a stable workspace token. Deleting global data is disabled."
    )

    from services.knowledge_base import get_knowledge_base

    kb = get_knowledge_base(
        st.session_state.user_session_id, st.session_state.get("user_id")
    )
    stats = kb.stats()
    st.metric("Indexed documents", stats.get("documents", 0), border=True)
    st.metric("Indexed chunks", stats.get("chunks", 0), border=True)

    if st.button("Rebuild vector index", icon=":material/refresh:", width="stretch"):
        try:
            count = kb.rebuild_index()
        except Exception as exc:
            st.error(f"Rebuild failed: {exc}")
        else:
            success_toast(f"Rebuilt {count} chunks from stored documents.")


def _about() -> None:
    section("About", ":material/info:")
    st.markdown("""
        **AI Interview Intelligence Platform**

        - Resume analysis and job-description matching
        - Persistent RAG over uploaded documents
        - Adaptive mock interviews with STAR and technical modes
        - Seven-dimension answer evaluation and readiness analytics
        - Personalized learning roadmaps and PDF-ready reports
        - Isolated accounts with role-based administration

        Built with Streamlit, SQLAlchemy, FAISS, Sentence Transformers, Plotly and
        Groq/Gemini/Mistral/Ollama providers.
        """)
