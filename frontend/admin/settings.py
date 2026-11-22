"""Admin AI Performance Monitoring page."""

import streamlit as st
from config.settings import settings

from frontend.shared.components import page_header


def show_admin_settings() -> None:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        st.error("Administrator access required.", icon=":material/error:")
        st.stop()

    from services.auth_service import require_admin

    require_admin(account.id)
    page_header(
        "AI Performance Monitoring", "Provider configuration and performance", ":material/tune:"
    )
    st.caption(f"Admin panel · {account.email}")

    with st.expander("Provider Configuration", expanded=True):
        st.markdown(f"**Primary Provider:** {settings.ai_primary_provider or 'Not set'}")
        st.markdown(f"**Fallback Enabled:** {'Yes' if settings.ai_fallback_enabled else 'No'}")
        st.markdown(f"**Mode:** {settings.ai_provider_mode or 'Not set'}")

        col1, col2 = st.columns(2)
        with col1:
            st.markdown(f"**Gemini Model:** {settings.gemini_model or 'Not set'}")
            st.markdown(
                f"**Gemini API Key:** {'Configured' if settings.gemini_api_key else 'Not configured'}"
            )
        with col2:
            st.markdown(f"**Groq Model:** {settings.groq_model or 'Not set'}")
            st.markdown(
                f"**Groq API Key:** {'Configured' if settings.groq_api_key else 'Not configured'}"
            )

        st.markdown(f"**Ollama Enabled:** {'Yes' if settings.enable_ollama else 'No'}")

    with st.expander("Database & Storage", expanded=False):
        st.markdown(f"**Database Path:** {settings.database_path}")
        st.markdown(f"**Vector Store Path:** {settings.vector_store_path}")
        st.markdown(f"**Embedding Model:** {settings.embedding_model}")
        st.markdown(f"**Max File Size:** {settings.max_file_size_mb} MB")

    with st.expander("Request Policy", expanded=False):
        st.markdown(f"**Request Timeout:** {settings.ai_request_timeout} seconds")
        st.markdown(f"**Max Retries:** {settings.ai_max_retries}")


show_admin_settings()
