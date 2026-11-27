"""Admin AI Performance Monitoring page."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st
from config.settings import settings

from frontend.shared.components import data_table, empty_state, kpi_row, page_header, section


def _range_dates(choice: str) -> tuple:
    today = datetime.now().date()
    if choice == "Last 24 hours":
        return today - timedelta(days=1), today
    if choice == "Last 30 days":
        return today - timedelta(days=30), today
    return today - timedelta(days=7), today


def _format_ms(value: float | None) -> str:
    return f"{value:.0f} ms" if value is not None else "N/A"


def _format_pct(value: float | None) -> str:
    return f"{value:.1f}%" if value is not None else "N/A"


def _format_cost(value: float | None) -> str:
    return f"${value:.6f}" if value is not None else "N/A"


def _format_number(value: object, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float):
            return f"{value:,.1f}{suffix}"
        return f"{value:,}{suffix}"
    return str(value)


def show_admin_settings() -> None:
    account = st.session_state.get("account")
    if not account or not getattr(account, "is_admin", False):
        st.error("Administrator access required.", icon=":material/error:")
        st.stop()

    from services import ai_performance_monitoring_service as monitor
    from services.auth_service import require_admin
    from services.llm_service import (
        check_provider_health,
        get_provider_registry,
        get_provider_status,
        test_provider_connection,
    )
    from services.resource_monitor import get_resource_admission_diagnostics, get_resource_snapshot

    require_admin(account.id)
    page_header(
        "AI Performance Monitoring",
        "Provider health, request telemetry, alerts, model usage, and resource context",
        ":material/monitoring:",
    )
    st.caption(f"Admin panel · {account.email}")
    st.caption("Monitoring stores sanitized metadata only. Prompts, API keys, and user data are excluded.")

    health_key = "_admin_ai_monitoring_health"
    if health_key not in st.session_state:
        st.session_state[health_key] = {}
    health = st.session_state[health_key]
    registry = get_provider_registry()
    status = get_provider_status()

    control_left, control_right, control_third = st.columns([2, 2, 2])
    with control_left:
        range_choice = st.selectbox(
            "Time range",
            ["Last 24 hours", "Last 7 days", "Last 30 days", "Custom"],
            key="admin_ai_monitoring_range",
        )
    if range_choice == "Custom":
        with control_right:
            custom_dates = st.date_input(
                "Monitoring dates",
                value=(datetime.now().date() - timedelta(days=7), datetime.now().date()),
                key="admin_ai_monitoring_dates",
            )
        if isinstance(custom_dates, (tuple, list)) and len(custom_dates) == 2:
            start_date = custom_dates[0]
            end_date = custom_dates[1]
        elif isinstance(custom_dates, datetime):
            start_date = end_date = custom_dates.date()
        elif isinstance(custom_dates, date):
            start_date = end_date = custom_dates
    else:
        start_date, end_date = _range_dates(range_choice)
        with control_right:
            st.caption(f"From {start_date} to {end_date}")
    with control_third:
        st.caption("Refresh checks are manual to avoid expensive polling.")

    refresh_left, refresh_right = st.columns(2)
    with refresh_left:
        if st.button(
            "Refresh provider health",
            icon=":material/refresh:",
            width="stretch",
            key="admin_ai_refresh_health",
        ):
            with st.spinner("Testing configured providers…"):
                results = check_provider_health()
            st.session_state[health_key] = results
            monitor.record_provider_health_results(results)
            st.rerun()
    with refresh_right:
        if st.button(
            "Refresh resource snapshot",
            icon=":material/refresh:",
            width="stretch",
            key="admin_ai_refresh_resources",
        ):
            with st.spinner("Collecting resource telemetry…"):
                monitor.record_resource_snapshot()
            st.rerun()

    try:
        data = monitor.get_dashboard_data(
            start_date=start_date,
            end_date=end_date,
            health=health,
            provider_status=status,
        )
    except Exception as exc:
        st.error(f"AI monitoring data unavailable: {exc}", icon=":material/error:")
        st.stop()

    kpis = data["kpis"]
    kpi_row(
        [
            ("AI requests", kpis["total_requests"], None, ":material/chat:"),
            ("Success rate", _format_pct(kpis["success_rate"]), None, ":material/check_circle:"),
            ("Failures", kpis["failures"], None, ":material/error:"),
            ("Avg latency", _format_ms(kpis["avg_latency_ms"]), None, ":material/speed:"),
            ("P95 latency", _format_ms(kpis["p95_latency_ms"]), None, ":material/query_stats:"),
            ("Total tokens", f"{int(kpis['total_tokens']):,}", None, ":material/token:"),
            ("Est. cost", _format_cost(kpis["estimated_cost"]), None, ":material/payments:"),
            ("Active alerts", kpis["active_alerts"], None, ":material/warning:"),
        ]
    )

    tabs = st.tabs(
        [
            "Overview",
            "Providers & models",
            "Alerts",
            "Request logs",
            "Resources",
            "Configuration",
        ]
    )

    with tabs[0]:
        section("Request and latency trend", ":material/trending_up:")
        trend = pd.DataFrame(data["trend"])
        if trend.empty:
            empty_state("No AI request telemetry in the selected range.")
        else:
            st.line_chart(trend.set_index("bucket")[["requests", "avg_latency_ms"]], height=260)
            st.caption("Request volume and average latency are read from recorded AI requests.")
        section("Error breakdown", ":material/bug_report:")
        errors = pd.DataFrame(data["errors"])
        if errors.empty:
            empty_state("No failed AI requests in the selected range.")
        else:
            st.bar_chart(errors.set_index("Error category"), height=240)
        st.caption(
            f"Fallback requests in range: {kpis['fallbacks']} · "
            f"Input tokens: {int(kpis['input_tokens']):,} · "
            f"Output tokens: {int(kpis['output_tokens']):,}"
        )

    with tabs[1]:
        section("Provider configuration and health", ":material/cloud:")
        data_table(
            data["providers"],
            "No providers registered.",
            "Provider health, availability, routing, and recorded performance",
        )
        st.caption(
            f"Primary: {str(status.get('primary', 'none')).title()} · "
            f"Active: {str(status.get('active', 'none')).title()} · "
            f"Mode: {str(status.get('mode', 'not set')).title()} · "
            f"Fallback: {'enabled' if status.get('fallback_enabled') else 'disabled'}"
        )
        for name, info in registry.items():
            if st.button(
                f"Test {info['label']}",
                icon=":material/cloud_done:",
                width="stretch",
                key=f"admin_ai_test_{name}",
            ):
                with st.spinner(f"Testing {info['label']}…"):
                    result = test_provider_connection(name)
                updated = dict(health)
                updated[name] = result
                st.session_state[health_key] = updated
                monitor.record_provider_health_results({name: result})
                st.rerun()
        section("Model usage", ":material/model_training:")
        data_table(
            data["models"],
            "No model telemetry in the selected range.",
            "AI model usage, latency, token totals, and estimated cost",
        )
        section("Provider health history", ":material/history:")
        data_table(
            [
                {
                    "Checked": row["created_at"],
                    "Provider": row["provider"],
                    "Status": row["status"],
                    "Latency": _format_ms(row["latency_ms"]),
                    "Error": row["error_message"] or "-",
                }
                for row in data["health_history"]
            ],
            "No health checks recorded yet.",
            "Recent provider health check history",
        )

    with tabs[2]:
        section("Active alerts", ":material/warning:")
        active_alerts = [alert for alert in data["alerts"] if alert["status"] == "active"]
        if not active_alerts:
            empty_state("No active AI monitoring alerts.")
        for alert in active_alerts:
            left, middle, right = st.columns([2, 6, 2])
            left.markdown(f"**{str(alert['severity']).upper()}**")
            middle.write(alert["message"])
            if right.button(
                "Acknowledge",
                width="stretch",
                key=f"admin_ai_ack_{alert['id']}",
            ):
                monitor.acknowledge_alert(int(alert["id"]))
                st.rerun()
        section("Alert history", ":material/history:")
        data_table(
            [
                {
                    "Created": alert["created_at"],
                    "Severity": alert["severity"],
                    "Type": alert["alert_type"],
                    "Provider": alert["provider"] or "-",
                    "Message": alert["message"],
                    "Status": alert["status"],
                    "Acknowledged": alert["acknowledged_at"] or "-",
                }
                for alert in data["alerts"]
            ],
            "No alerts have been recorded.",
            "AI monitoring alert history",
        )
        alert_left, alert_right = st.columns(2)
        with alert_left:
            if st.button(
                "Evaluate alerts now",
                icon=":material/notifications_active:",
                width="stretch",
                key="admin_ai_evaluate_alerts",
            ):
                with st.spinner("Evaluating alert thresholds…"):
                    monitor.evaluate_request_alerts(force=True)
                st.rerun()
        with alert_right:
            if st.button(
                "Run retention cleanup",
                icon=":material/delete_sweep:",
                width="stretch",
                key="admin_ai_prune",
            ):
                removed = monitor.prune_old_data()
                st.success(
                    f"Removed {removed['events']} events and {removed['alerts']} acknowledged alerts."
                )

    with tabs[3]:
        section("AI request logs", ":material/list_alt:")
        options = monitor.get_filter_options()
        filter_left, filter_middle, filter_right = st.columns(3)
        with filter_left:
            provider_filter = st.selectbox(
                "Provider", ["All", *options["providers"]], key="admin_ai_log_provider"
            )
            status_filter = st.selectbox(
                "Status", ["All", *options["statuses"]], key="admin_ai_log_status"
            )
        with filter_middle:
            model_filter = st.selectbox(
                "Model", ["All", *options["models"]], key="admin_ai_log_model"
            )
            feature_filter = st.selectbox(
                "Feature", ["All", *options["features"]], key="admin_ai_log_feature"
            )
        with filter_right:
            category_filter = st.selectbox(
                "Error category", ["All", *options["error_categories"]], key="admin_ai_log_error"
            )
            page = st.number_input("Page", min_value=1, value=1, step=1, key="admin_ai_log_page")
        logs = monitor.list_request_events(
            start_date=start_date,
            end_date=end_date,
            provider=provider_filter,
            model=model_filter,
            feature=feature_filter,
            status=status_filter,
            error_category=category_filter,
            limit=50,
            offset=(int(page) - 1) * 50,
        )
        data_table(
            [
                {
                    "Time": row["created_at"],
                    "Request ID": row["request_id"],
                    "Provider": row["provider"],
                    "Model": row["model"],
                    "Feature": row["feature"],
                    "Status": row["status"],
                    "Latency": _format_ms(row["latency_ms"]),
                    "Tokens": row["total_tokens"],
                    "Attempts": row["attempts"],
                    "Fallback": "Yes" if row["used_fallback"] else "No",
                    "Error": row["error_category"] or "-",
                    "Message": row["error_message"] or "-",
                }
                for row in logs["rows"]
            ],
            "No AI requests match the selected filters.",
            "Filtered AI request telemetry",
        )
        st.caption(
            f"Showing {len(logs['rows'])} of {logs['total']} matching requests · "
            "Error messages are sanitized and prompts are never stored."
        )

    with tabs[4]:
        section("Current resource context", ":material/memory:")
        snapshot = get_resource_snapshot()
        diagnostics = get_resource_admission_diagnostics()
        data_table(
            [
                {
                    "Metric": "System RAM total",
                    "Value": _format_number(snapshot.system_ram_total_mb, " MB"),
                },
                {
                    "Metric": "System RAM available",
                    "Value": _format_number(snapshot.system_ram_available_mb, " MB"),
                },
                {
                    "Metric": "System RAM used",
                    "Value": _format_number(snapshot.system_ram_used_mb, " MB"),
                },
                {
                    "Metric": "Application RSS",
                    "Value": _format_number(snapshot.process_rss_mb, " MB"),
                },
                {
                    "Metric": "CPU usage",
                    "Value": _format_number(snapshot.cpu_percent, "%"),
                },
                {
                    "Metric": "Memory pressure",
                    "Value": str(snapshot.memory_pressure),
                },
                {
                    "Metric": "GPU",
                    "Value": str(snapshot.gpu_name or "N/A"),
                },
                {
                    "Metric": "GPU VRAM used / total",
                    "Value": (
                        f"{_format_number(snapshot.gpu_used_vram_mb)} / "
                        f"{_format_number(snapshot.gpu_total_vram_mb)} MB"
                        if snapshot.gpu_available
                        else "N/A"
                    ),
                },
                {
                    "Metric": "Active leases",
                    "Value": _format_number(diagnostics.get("active_lease_count")),
                },
                {
                    "Metric": "Admission pressure",
                    "Value": str(diagnostics.get("resource_pressure")),
                },
            ],
            "Resource metrics are unavailable.",
            "Current Windows-compatible resource telemetry",
        )
        if snapshot.gpu_available:
            total_vram = snapshot.gpu_total_vram_mb or 0
            used_vram = snapshot.gpu_used_vram_mb or 0
            fraction = min(1.0, max(0.0, used_vram / total_vram)) if total_vram else 0.0
            st.progress(fraction, text=f"GPU memory used: {fraction:.0%}")
        for message in snapshot.monitor_errors:
            st.caption(f"Resource monitor note: {monitor.sanitize_error_message(message)}")
        section("Recorded resource snapshots", ":material/history:")
        data_table(
            [
                {
                    "Checked": row["created_at"],
                    "Status": row["status"],
                    "Severity": row["severity"] or "-",
                    "RAM available (MB)": _format_number(
                        (row.get("details") or {}).get("system_ram_available_mb")
                    ),
                    "CPU %": _format_number((row.get("details") or {}).get("cpu_percent")),
                    "GPU %": (
                        _format_number(
                            100.0
                            * float((row.get("details") or {}).get("gpu_used_vram_mb") or 0)
                            / float((row.get("details") or {}).get("gpu_total_vram_mb") or 1)
                        )
                        if (row.get("details") or {}).get("gpu_available")
                        else "N/A"
                    ),
                    "Leases": _format_number((row.get("details") or {}).get("active_lease_count")),
                }
                for row in data["resources"]
            ],
            "No resource snapshots recorded yet.",
            "Historical resource telemetry captured by manual refresh",
        )

    with tabs[5]:
        section("Alert thresholds and retention", ":material/tune:")
        thresholds = data["thresholds"]
        data_table(
            [
                {"Setting": name.replace("_", " ").title(), "Value": value}
                for name, value in thresholds.items()
            ],
            "No monitoring configuration available.",
            "Configurable AI monitoring thresholds",
        )
        section("Cost estimation", ":material/payments:")
        pricing = data["pricing"]
        st.markdown(
            f"**Verified pricing configured:** {'Yes' if pricing['verified'] else 'No'}"
        )
        if pricing["verified"]:
            data_table(
                [
                    {
                        "Provider": provider,
                        "Input per 1M tokens": rates["input"],
                        "Output per 1M tokens": rates["output"],
                    }
                    for provider, rates in pricing["rates"].items()
                ],
                "No pricing rates configured.",
                "Operator-verified provider pricing rates",
            )
        else:
            empty_state(
                "Cost estimates are disabled until verified rates are configured.",
                "Set AI_MONITORING_PRICING_VERIFIED=true and provider rate variables in the deployment environment.",
            )
        section("Application configuration", ":material/settings:")
        st.markdown(f"**Database path:** {settings.database_path}")
        st.markdown(f"**Vector store path:** {settings.vector_store_path}")
        st.markdown(f"**Embedding model:** {settings.embedding_model}")
        st.markdown(f"**Request timeout:** {settings.ai_request_timeout} seconds")
        st.markdown(f"**Max retries:** {settings.ai_max_retries}")


show_admin_settings()
