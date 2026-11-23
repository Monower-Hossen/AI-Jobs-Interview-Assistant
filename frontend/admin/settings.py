"""Admin AI Performance Monitoring page."""

import streamlit as st
from config.settings import settings

from frontend.shared.components import data_table, page_header


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

    from services.llm_service import (
        check_provider_health,
        get_provider_registry,
        get_provider_status,
    )

    try:
        status = get_provider_status()
    except Exception:
        status = {"primary": "unavailable", "mode": "unavailable", "providers": {}, "chain": []}
    registry = get_provider_registry()

    with st.expander("Provider Configuration", expanded=True):
        st.markdown(f"**Primary Provider:** {str(status.get('primary', 'none')).title()}")
        st.markdown(f"**Active Provider:** {str(status.get('active', 'none')).title()}")
        st.markdown(f"**Fallback Enabled:** {'Yes' if status.get('fallback_enabled') else 'No'}")
        st.markdown(f"**Mode:** {str(status.get('mode', 'not set')).title()}")
        st.markdown(f"**Priority:** {', '.join(status.get('priority', [])) or 'Not set'}")

        health = st.session_state.get("_admin_provider_health", {})
        data_table(
            [
                {
                    "Provider": registry[name]["label"],
                    "Status": health.get(name, {}).get("status", "Not checked"),
                    "Configured model": registry[name]["model"],
                    "In chain": "Yes" if name in status.get("chain", []) else "No",
                    "Error": health.get(name, {}).get("error") or "",
                }
                for name in registry
            ],
            "No providers registered.",
            "Provider configuration and health status",
        )
        if st.button(
            "Refresh provider health",
            icon=":material/refresh:",
            width="stretch",
            key="admin_refresh_provider_health",
        ):
            with st.spinner("Checking providers…"):
                st.session_state["_admin_provider_health"] = check_provider_health()
            st.rerun()

    with st.expander("Resource Admission", expanded=False):
        diagnostics = st.session_state.get("_admin_resource_admission_diagnostics")
        refresh = st.button(
            "Refresh resource diagnostics",
            icon=":material/refresh:",
            width="stretch",
            key="admin_refresh_resource_diagnostics",
        )
        if diagnostics is None or refresh:
            from services.resource_monitor import get_resource_admission_diagnostics

            try:
                diagnostics = get_resource_admission_diagnostics()
                st.session_state["_admin_resource_admission_diagnostics"] = diagnostics
            except Exception as exc:
                st.error(f"Resource monitor unavailable: {exc}", icon=":material/error:")
                diagnostics = None
        if diagnostics is not None:
            st.markdown(
                f"**Active leases:** {diagnostics['active_lease_count']} · "
                f"**Expired:** {diagnostics['expired_lease_count']} · "
                f"**Recovered:** {diagnostics['recovered_lease_count']} · "
                f"**Pressure:** {diagnostics['resource_pressure']}"
            )
            st.caption(
                f"Monitoring available: {'Yes' if diagnostics['monitoring_available'] else 'No'}"
            )
            data_table(
                [
                    {"Task": task, "Active leases": count}
                    for task, count in diagnostics["active_leases_by_task"].items()
                ],
                "No active resource leases.",
                "Active leases by task",
            )
            data_table(
                [
                    {"Reason code": code, "Rejections": count}
                    for code, count in diagnostics["rejected_admissions"].items()
                ],
                "No admissions have been rejected.",
                "Rejected admissions by reason",
            )

    with st.expander("Database & Storage", expanded=False):
        st.markdown(f"**Database Path:** {settings.database_path}")
        st.markdown(f"**Vector Store Path:** {settings.vector_store_path}")
        st.markdown(f"**Embedding Model:** {settings.embedding_model}")
        st.markdown(f"**Max File Size:** {settings.max_file_size_mb} MB")

    with st.expander("Request Policy", expanded=False):
        st.markdown(f"**Request Timeout:** {settings.ai_request_timeout} seconds")
        st.markdown(f"**Max Retries:** {settings.ai_max_retries}")


show_admin_settings()
