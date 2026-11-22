"""Reusable native Streamlit building blocks for the platform UI."""

import json
import time
from collections.abc import Sequence

import pandas as pd
import streamlit as st


def page_header(title: str, subtitle: str = "", icon: str = ":material/dashboard:") -> None:
    st.title(title, icon=icon)
    if subtitle:
        st.caption(subtitle)


def section(title: str, icon: str = "") -> None:
    st.subheader(title, icon=icon or None)


def kpi_row(items: Sequence[tuple[str, str | int | float, str | int | float | None]]) -> None:
    """Render responsive bordered metric cards: (label, value, delta)."""
    with st.container(horizontal=True, wrap=True):
        for label, value, delta in items:
            st.metric(label, value, delta, border=True)


def ai_provider_status() -> None:
    from services.llm_service import (
        HEALTH_NOT_CHECKED,
        check_provider_health,
        get_provider_registry,
        get_provider_status,
        test_provider_connection,
    )

    def _time(value: float | None) -> str:
        return time.strftime("%H:%M:%S", time.localtime(value)) if value else "-"

    registry = get_provider_registry()
    names = list(registry)
    health_key = "_ai_provider_health_results"
    success_key = "_ai_provider_last_success"
    if health_key not in st.session_state:
        st.session_state[health_key] = {}
    if success_key not in st.session_state:
        st.session_state[success_key] = {}
    health: dict[str, dict] = st.session_state[health_key]
    last_success: dict[str, float] = st.session_state[success_key]

    def _remember(name: str, info: dict) -> None:
        health[name] = info
        if info.get("healthy"):
            last_success[name] = float(info.get("checked_at") or time.time())
        st.session_state[health_key] = health
        st.session_state[success_key] = last_success

    try:
        status = get_provider_status()
    except Exception:
        status = {
            "mode": "unavailable",
            "fallback_enabled": False,
            "active": "none",
            "active_model": "-",
            "last_used_provider": None,
            "fallback_triggered": False,
            "chain": [],
            "providers": {},
        }

    for name in names:
        if name not in health:
            provider_info = status.get("providers", {}).get(name, {})
            health[name] = {
                "status": HEALTH_NOT_CHECKED,
                "configured": provider_info.get("configured", False),
                "model": provider_info.get("model") or registry[name]["model"],
                "error": None,
            }
    st.session_state[health_key] = health

    fallback_label = (
        "Triggered"
        if status.get("fallback_triggered")
        else ("Enabled" if status.get("fallback_enabled") else "Off")
    )
    with st.container(border=True):
        st.subheader("AI provider health monitor", icon=":material/cloud:")
        st.caption(
            "Status comes from the provider health service. Keys are never displayed."
        )
        with st.container(horizontal=True, wrap=True):
            st.metric("Active provider", str(status.get("active", "none")).title(), border=True)
            st.metric("Fallback", fallback_label, border=True)
            st.metric("Mode", str(status.get("mode", "-")).title(), border=True)
            st.metric(
                "Last used", str(status.get("last_used_provider") or "-").title(), border=True
            )

        if st.button(
            "Test all providers",
            icon=":material/bolt:",
            width="stretch",
            key="_ai_provider_test_all",
        ):
            with st.spinner("Testing all providers…"):
                fresh = check_provider_health()
            for name, info in fresh.items():
                _remember(name, info)
            st.rerun()

        with st.container(horizontal=True, wrap=True):
            for name in names:
                if st.button(
                    f"Test {registry[name]['label']}",
                    icon=":material/cloud_done:",
                    key=f"_ai_provider_test_{name}",
                ):
                    _remember(name, test_provider_connection(name))
                    st.rerun()

        rows = []
        for name in names:
            info = health.get(name, {})
            model_info = status.get("providers", {}).get(name, {})
            latency = info.get("latency_ms")
            fallback = (
                "Used"
                if status.get("fallback_triggered") and status.get("last_used_provider") == name
                else ("Ready" if name in status.get("chain", []) else "-")
            )
            rows.append(
                {
                    "Provider": registry[name]["label"],
                    "Status": info.get("status", HEALTH_NOT_CHECKED),
                    "Configured": (
                        "Yes" if info.get("configured", model_info.get("configured", False)) else "No"
                    ),
                    "Configured model": (
                        info.get("model") or model_info.get("model") or registry[name]["model"]
                    ),
                    "Response time": (
                        f"{latency:.0f} ms" if isinstance(latency, (int, float)) else "-"
                    ),
                    "Last success": _time(last_success.get(name)),
                    "Error message": info.get("error") or "-",
                    "Active provider": "Yes" if name == status.get("active") else "No",
                    "Fallback": fallback,
                }
            )
        st.dataframe(
            pd.DataFrame(rows),
            hide_index=True,
            width="stretch",
            alt="AI provider status, health and response times",
        )


def score_tone(score: float) -> str:
    if score >= 8:
        return "green"
    if score >= 6:
        return "blue"
    if score >= 4:
        return "orange"
    return "red"


def score_badge(label: str, score: float) -> None:
    st.markdown(f"**{label}:** :{score_tone(score)}[{score:.1f}/10]")


def score_table(scores: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame(
        {"Dimension": list(scores), "Score": [round(value, 2) for value in scores.values()]}
    )


def render_score_chart(scores: dict[str, float], caption: str = "Model-generated score") -> None:
    st.bar_chart(score_table(scores).set_index("Dimension"), alt=caption)
    st.caption("AI-generated rubric scores are advisory, not objective ground truth.")


def empty_state(message: str, hint: str = "") -> None:
    st.info(message, icon=":material/inbox:")
    if hint:
        st.caption(hint)


def error_state(message: str, detail: str = "") -> None:
    st.error(message, icon=":material/error:")
    if detail:
        st.caption(detail)


def loading_slot(height: int = 240):
    slot = st.container()
    with slot.skeleton(height=height):
        st.caption("Loading�?�")
    return slot


def render_resource_status_sidebar(key_prefix: str = "resource") -> None:
    """Render lightweight cached resource status in sidebar. Never crashes UI."""
    try:
        from config.settings import settings
        from services.resource_monitor import (
            format_resource_status,
            get_active_heavy_tasks,
            get_resource_snapshot,
        )
    except Exception:
        return

    if not bool(getattr(settings, "resource_monitor_enabled", True)):
        return

    try:
        snapshot = get_resource_snapshot()
    except Exception:
        st.sidebar.caption("Resource monitor unavailable.")
        return

    with st.sidebar.expander("System resources", expanded=False):
        st.caption(format_resource_status(snapshot))
        if snapshot.system_ram_available_mb is not None:
            if snapshot.memory_pressure == "critical":
                st.error(
                    f"Low memory: {snapshot.system_ram_available_mb:.0f} MB available. "
                    "Heavy tasks may be blocked.",
                    icon=":material/warning:",
                )
            elif snapshot.memory_pressure == "low":
                st.warning(
                    f"Memory pressure: {snapshot.system_ram_available_mb:.0f} MB available.",
                    icon=":material/memory:",
                )
        if snapshot.process_rss_mb is not None:
            st.caption(f"App RSS: {snapshot.process_rss_mb:.0f} MB")
        if snapshot.gpu_available:
            st.caption(
                f"GPU: {snapshot.gpu_name or 'NVIDIA'} | "
                f"VRAM free {snapshot.gpu_free_vram_mb:.0f}/{snapshot.gpu_total_vram_mb:.0f} MB"
            )
        else:
            st.caption("GPU metrics unavailable; CPU/RAM monitoring still active.")

        active = get_active_heavy_tasks()
        busy = [name.replace('_', ' ') for name, count in active.items() if count > 0]
        if busy:
            st.caption(f"Busy: {', '.join(busy)}")

        if st.button(
            "Refresh resource status",
            key=f"{key_prefix}_refresh",
            icon=":material/refresh:",
            width="stretch",
        ):
            try:
                get_resource_snapshot(force=True)
            except Exception:
                pass
            st.rerun()


def success_toast(message: str) -> None:
    st.toast(message, icon=":material/check_circle:")


def warning_toast(message: str) -> None:
    st.toast(message, icon=":material/warning:")


def confirm_delete(
    label: str,
    key: str = "_confirm_delete_result",
    reset: bool = False,
) -> bool:
    """Return True only after the user confirms the deletion dialog."""
    if reset or key not in st.session_state:
        st.session_state[key] = False

    def _on_dismiss() -> None:
        st.session_state[key] = False
        st.session_state[f"{key}_resolved"] = True

    @st.dialog("Confirm deletion", width="small", on_dismiss=_on_dismiss)
    def _dialog() -> None:
        st.warning(f"This will permanently delete: **{label}**. This action cannot be undone.")
        confirmed = st.checkbox("I understand this action is permanent", key=f"{key}_ack")
        col1, col2 = st.columns(2)
        if col1.button("Cancel", width="stretch"):
            st.session_state[key] = False
            st.session_state[f"{key}_resolved"] = True
            st.rerun()
        if col2.button("Delete", type="primary", disabled=not confirmed, width="stretch"):
            st.session_state[key] = True
            st.session_state[f"{key}_resolved"] = True
            st.rerun()

    _dialog()
    return bool(st.session_state.get(key, False))


def speak_text(text: str, key: str = "default") -> None:
    """Read text aloud through the browser once per distinct text value."""
    spoken_key = f"_spoken_text_{key}"
    if st.session_state.get(spoken_key) == text:
        return
    st.session_state[spoken_key] = text
    payload = json.dumps(text).replace("</", "<\\/")
    st.html(
        f"""<script>
(function () {{
  if (!('speechSynthesis' in window)) return;
  try {{
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance({payload});
    utterance.rate = 0.98;
    utterance.pitch = 1;
    window.speechSynthesis.speak(utterance);
  }} catch (error) {{ console.warn('Speech synthesis unavailable', error); }}
}})();
</script>""",
        unsafe_allow_javascript=True,
    )


def data_table(
    data: list[dict], empty_message: str = "No records yet.", alt: str = "Data table"
) -> None:
    if not data:
        empty_state(empty_message)
        return
    st.dataframe(pd.DataFrame(data), hide_index=True, width="stretch", alt=alt)
