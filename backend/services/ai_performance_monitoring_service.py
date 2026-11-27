from __future__ import annotations

import logging
import math
import os
import queue
import re
import threading
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Any

from config.settings import settings
from database import get_db_context
from database.models import AIAlert, AIPerformanceEvent
from sqlalchemy import and_, case, func

logger = logging.getLogger(__name__)

EVENT_LLM_REQUEST = "llm_request"
EVENT_PROVIDER_HEALTH = "provider_health"
EVENT_RESOURCE_SNAPSHOT = "resource_snapshot"

_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(api[_-]?key|token|secret|password|authorization)\b\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE)
_SECRET_QUERY_RE = re.compile(r"([?&](?:api[_-]?key|key|token)=)[^&\s]+", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

_EVENT_QUEUE: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)
_WORKER_LOCK = threading.Lock()
_WORKER_THREAD: threading.Thread | None = None
_LAST_PRUNE = 0.0
_LAST_ALERT_EVALUATION = 0.0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def monitoring_enabled() -> bool:
    return _env_bool("AI_MONITORING_ENABLED", True)


def alert_thresholds() -> dict[str, float | int]:
    return {
        "error_rate_percent": _env_float("AI_MONITORING_ERROR_RATE_PERCENT", 20.0),
        "min_samples": _env_int("AI_MONITORING_MIN_SAMPLES", 5),
        "high_latency_ms": _env_float("AI_MONITORING_HIGH_LATENCY_MS", 8000.0),
        "consecutive_failures": _env_int("AI_MONITORING_CONSECUTIVE_FAILURES", 3),
        "repeated_error_count": _env_int("AI_MONITORING_REPEATED_ERROR_COUNT", 5),
        "window_minutes": _env_int("AI_MONITORING_WINDOW_MINUTES", 5),
        "gpu_used_percent": _env_float("AI_MONITORING_GPU_ALERT_PERCENT", 85.0),
        "cpu_percent": _env_float("AI_MONITORING_CPU_ALERT_PERCENT", 90.0),
        "retention_days": max(1, _env_int("AI_MONITORING_RETENTION_DAYS", 90)),
    }


def get_pricing_config() -> dict[str, Any]:
    rates: dict[str, dict[str, float]] = {}
    global_input = os.getenv("AI_MONITORING_PRICE_INPUT_PER_1M")
    global_output = os.getenv("AI_MONITORING_PRICE_OUTPUT_PER_1M")
    if global_input is not None and global_output is not None:
        try:
            rates["*"] = {
                "input": max(0.0, float(global_input)),
                "output": max(0.0, float(global_output)),
            }
        except (TypeError, ValueError):
            rates = {}
    for provider in ("groq", "gemini", "ollama"):
        prefix = f"AI_MONITORING_{provider.upper()}"
        input_rate = os.getenv(f"{prefix}_INPUT_PER_1M")
        output_rate = os.getenv(f"{prefix}_OUTPUT_PER_1M")
        if input_rate is None or output_rate is None:
            continue
        try:
            rates[provider] = {
                "input": max(0.0, float(input_rate)),
                "output": max(0.0, float(output_rate)),
            }
        except (TypeError, ValueError):
            continue
    verified = _env_bool("AI_MONITORING_PRICING_VERIFIED", False) and bool(rates)
    return {"verified": verified, "rates": rates}


def sanitize_error_message(value: Any, limit: int = 300) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    secrets = (
        getattr(settings, "gemini_api_key", ""),
        getattr(settings, "groq_api_key", ""),
        os.getenv("GEMINI_API_KEY", ""),
        os.getenv("GOOGLE_API_KEY", ""),
        os.getenv("GROQ_API_KEY", ""),
    )
    for secret in secrets:
        if secret and len(str(secret)) >= 6:
            text = text.replace(str(secret), "[redacted]")
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = _SECRET_QUERY_RE.sub(lambda match: f"{match.group(1)}[redacted]", text)
    text = _URL_RE.sub("[url]", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def categorize_error(error: Any) -> str:
    code: int | None = None
    for attribute in ("status_code", "code", "status"):
        value = getattr(error, attribute, None)
        if isinstance(value, int):
            code = value
            break
        if isinstance(value, str) and value.isdigit():
            code = int(value)
            break
    text = f"{type(error).__name__} {error}".lower()
    if code in (401, 403) or "authentication" in text or "api key" in text:
        return "authentication"
    if code == 429 or "rate limit" in text:
        return "rate_limit"
    if code in (500, 502, 503, 504) or "unavailable" in text:
        return "provider_unavailable"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "connect" in text or "network" in text or "socket" in text:
        return "network"
    if "configured" in text or "configuration" in text:
        return "configuration"
    if "json" in text or "validation" in text or "empty" in text:
        return "invalid_response"
    return "provider_error"


def _usage_values(usage: Any) -> tuple[int | None, int | None, int | None]:
    if not isinstance(usage, dict):
        return None, None, None

    def pick(*keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return max(0, int(value))
        return None

    input_tokens = pick("prompt_tokens", "input_tokens", "promptTokenCount", "inputTokenCount")
    output_tokens = pick(
        "completion_tokens", "output_tokens", "candidatesTokenCount", "outputTokenCount"
    )
    total_tokens = pick("total_tokens", "totalTokenCount")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return input_tokens, output_tokens, total_tokens


def _utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value or "")


def _maybe_prune() -> None:
    global _LAST_PRUNE
    interval = max(60, _env_int("AI_MONITORING_PRUNE_INTERVAL_SECONDS", 3600))
    now = time.monotonic()
    if now - _LAST_PRUNE < interval:
        return
    _LAST_PRUNE = now
    try:
        prune_old_data()
    except Exception:
        logger.debug("ai monitoring retention cleanup failed", exc_info=True)


def _persist_event(payload: dict[str, Any]) -> None:
    request_id = payload.get("request_id")
    raw_details = payload.get("details")
    details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else {}
    with get_db_context() as db:
        if request_id:
            duplicate = (
                db.query(AIPerformanceEvent.id)
                .filter(AIPerformanceEvent.request_id == request_id)
                .first()
            )
            if duplicate is not None:
                return
        input_tokens, output_tokens, total_tokens = _usage_values(payload.get("usage"))
        latency = payload.get("latency_ms")
        attempts = payload.get("attempts", 1)
        event = AIPerformanceEvent(
            event_type=str(payload.get("event_type") or EVENT_LLM_REQUEST),
            provider=payload.get("provider"),
            model=payload.get("model"),
            feature=payload.get("feature"),
            status=str(payload.get("status") or "failed"),
            severity=payload.get("severity"),
            latency_ms=float(latency) if isinstance(latency, (int, float)) else None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            error_category=payload.get("error_category"),
            error_message=payload.get("error_message"),
            request_id=request_id,
            attempts=max(1, int(attempts) if isinstance(attempts, (int, float)) else 1),
            used_fallback=bool(payload.get("used_fallback", False)),
            details_json=dict(details),
        )
        db.add(event)


def _event_worker() -> None:
    while True:
        payload = _EVENT_QUEUE.get()
        try:
            _persist_event(payload)
            _maybe_prune()
            evaluate_request_alerts(force=False)
        except Exception:
            logger.debug("ai monitoring event dropped", exc_info=True)
        finally:
            _EVENT_QUEUE.task_done()


def _ensure_worker() -> None:
    global _WORKER_THREAD
    if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
        return
    with _WORKER_LOCK:
        if _WORKER_THREAD is None or not _WORKER_THREAD.is_alive():
            _WORKER_THREAD = threading.Thread(
                target=_event_worker,
                name="ai-performance-monitoring",
                daemon=True,
            )
            _WORKER_THREAD.start()


def record_llm_request(
    *,
    request_id: str,
    status: str,
    provider: str | None = None,
    model: str | None = None,
    feature: str | None = None,
    latency_ms: float | None = None,
    usage: dict[str, Any] | None = None,
    error: Any = None,
    attempts: int = 1,
    used_fallback: bool = False,
    details: dict[str, Any] | None = None,
) -> None:
    if not monitoring_enabled():
        return
    payload: dict[str, Any] = {
        "event_type": EVENT_LLM_REQUEST,
        "request_id": request_id,
        "status": status,
        "provider": provider,
        "model": model,
        "feature": feature,
        "latency_ms": latency_ms,
        "usage": usage,
        "attempts": attempts,
        "used_fallback": used_fallback,
        "details": details or {},
    }
    if error is not None:
        payload["error_category"] = categorize_error(error)
        payload["error_message"] = sanitize_error_message(error)
    try:
        _EVENT_QUEUE.put_nowait(payload)
        _ensure_worker()
    except Exception:
        logger.debug("ai monitoring event queue unavailable", exc_info=True)


def flush_events(timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    unfinished = getattr(_EVENT_QUEUE, "unfinished_tasks", 0)
    while unfinished and time.monotonic() < deadline:
        time.sleep(0.01)
        unfinished = getattr(_EVENT_QUEUE, "unfinished_tasks", 0)
    return unfinished == 0


def _upsert_alert(
    db: Any,
    *,
    alert_key: str,
    alert_type: str,
    severity: str,
    message: str,
    provider: str | None = None,
) -> None:
    now = _utc_now_naive()
    alert = db.query(AIAlert).filter(AIAlert.alert_key == alert_key).first()
    if alert is None:
        db.add(
            AIAlert(
                alert_key=alert_key,
                alert_type=alert_type,
                severity=severity,
                provider=provider,
                message=sanitize_error_message(message),
                status="active",
                created_at=now,
                acknowledged_at=None,
            )
        )
        return
    alert.message = sanitize_error_message(message)
    if alert.status == "acknowledged":
        alert.status = "active"
        alert.created_at = now
        alert.acknowledged_at = None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return float(ordered[index])


def evaluate_request_alerts(*, force: bool = False) -> int:
    global _LAST_ALERT_EVALUATION
    if not monitoring_enabled():
        return 0
    now = time.monotonic()
    if not force and now - _LAST_ALERT_EVALUATION < 15.0:
        return 0
    _LAST_ALERT_EVALUATION = now
    thresholds = alert_thresholds()
    window = _utc_now_naive() - timedelta(minutes=max(1, int(thresholds["window_minutes"])))
    created = 0
    with get_db_context() as db:
        rows = (
            db.query(AIPerformanceEvent)
            .filter(
                AIPerformanceEvent.event_type == EVENT_LLM_REQUEST,
                AIPerformanceEvent.created_at >= window,
            )
            .order_by(AIPerformanceEvent.created_at.desc())
            .limit(2000)
            .all()
        )
        if not rows:
            return 0
        failures = [row for row in rows if row.status == "failed"]
        error_rate = 100.0 * len(failures) / len(rows)
        if len(rows) >= int(thresholds["min_samples"]) and error_rate >= float(
            thresholds["error_rate_percent"]
        ):
            _upsert_alert(
                db,
                alert_key="request_error_rate",
                alert_type="request_error_rate",
                severity="critical" if error_rate >= 50 else "warning",
                message=(
                    f"AI request failure rate is {error_rate:.1f}% "
                    f"({len(failures)}/{len(rows)} requests)."
                ),
            )
            created += 1
        latencies = [
            float(row.latency_ms) for row in rows if isinstance(row.latency_ms, (int, float))
        ]
        p95 = _percentile(latencies, 0.95)
        if p95 is not None and p95 >= float(thresholds["high_latency_ms"]):
            _upsert_alert(
                db,
                alert_key="request_latency",
                alert_type="high_latency",
                severity="warning",
                message=f"AI request p95 latency is {p95:.0f} ms over the alert window.",
            )
            created += 1
        providers = {str(row.provider) for row in rows if row.provider}
        consecutive = max(2, int(thresholds["consecutive_failures"]))
        for provider in providers:
            recent = [
                row
                for row in rows
                if row.provider == provider and row.event_type == EVENT_LLM_REQUEST
            ][:consecutive]
            if len(recent) == consecutive and all(row.status == "failed" for row in recent):
                _upsert_alert(
                    db,
                    alert_key=f"provider_failures:{provider}",
                    alert_type="consecutive_failures",
                    severity="critical",
                    message=f"{provider} failed {consecutive} consecutive AI requests.",
                    provider=provider,
                )
                created += 1
        error_counts: dict[str, int] = {}
        for row in failures:
            category = str(row.error_category or "provider_error")
            error_counts[category] = error_counts.get(category, 0) + 1
        for category, count in error_counts.items():
            if count >= int(thresholds["repeated_error_count"]):
                _upsert_alert(
                    db,
                    alert_key=f"error_category:{category}",
                    alert_type="repeated_error",
                    severity="warning",
                    message=f"Repeated AI errors categorized as {category}: {count} occurrences.",
                )
                created += 1
    return created


def _health_status(info: dict[str, Any]) -> str:
    status = str(info.get("status") or "").lower()
    if info.get("healthy") or status == "connected":
        return "healthy"
    if "auth" in status:
        return "auth_failed"
    if "connection" in status or "network" in status:
        return "connection_failed"
    if "disabled" in status or "not configured" in status:
        return "skipped"
    return "unavailable"


def record_provider_health_results(results: dict[str, dict[str, Any]]) -> int:
    if not monitoring_enabled():
        return 0
    now = _utc_now_naive()
    recorded = 0
    with get_db_context() as db:
        for provider, info in results.items():
            status = _health_status(info)
            severity = "info"
            if status in {"auth_failed", "connection_failed", "unavailable"}:
                severity = "critical"
            elif info.get("model_available") is False:
                severity = "warning"
            latency = info.get("latency_ms")
            event = AIPerformanceEvent(
                event_type=EVENT_PROVIDER_HEALTH,
                provider=provider,
                model=info.get("model"),
                status=status,
                severity=severity,
                latency_ms=float(latency) if isinstance(latency, (int, float)) else None,
                error_category=status if status != "healthy" else None,
                error_message=sanitize_error_message(info.get("error")) or None,
                attempts=1,
                used_fallback=False,
                details_json={
                    "configured": bool(info.get("configured")),
                    "enabled": bool(info.get("enabled")),
                    "model_available": info.get("model_available"),
                    "checked_at": info.get("checked_at"),
                },
                created_at=now,
            )
            db.add(event)
            recorded += 1
            if severity == "critical":
                _upsert_alert(
                    db,
                    alert_key=f"provider_connectivity:{provider}",
                    alert_type="provider_connectivity",
                    severity="critical",
                    message=f"{provider} provider health check failed: {info.get('status')}.",
                    provider=provider,
                )
            if info.get("model_available") is False:
                _upsert_alert(
                    db,
                    alert_key=f"provider_model:{provider}",
                    alert_type="model_unavailable",
                    severity="warning",
                    message=f"Configured {provider} model is not listed as available.",
                    provider=provider,
                )
    return recorded


def record_resource_snapshot() -> dict[str, Any]:
    from services.resource_monitor import get_resource_admission_diagnostics, get_resource_snapshot

    snapshot = get_resource_snapshot(force=True)
    diagnostics = get_resource_admission_diagnostics()
    details = snapshot.to_dict()
    details["active_lease_count"] = diagnostics.get("active_lease_count")
    details["resource_pressure"] = diagnostics.get("resource_pressure")
    details["monitoring_available"] = diagnostics.get("monitoring_available")
    thresholds = alert_thresholds()
    pressure = str(details.get("memory_pressure") or "unknown")
    status = pressure if pressure in {"ok", "low", "critical", "unknown"} else "unknown"
    severity = "info"
    if status in {"low", "critical"}:
        severity = "warning"
    if monitoring_enabled():
        now = _utc_now_naive()
        with get_db_context() as db:
            db.add(
                AIPerformanceEvent(
                    event_type=EVENT_RESOURCE_SNAPSHOT,
                    status=status,
                    severity=severity,
                    details_json=details,
                    attempts=1,
                    used_fallback=False,
                    created_at=now,
                )
            )
            if status in {"low", "critical"}:
                _upsert_alert(
                    db,
                    alert_key="resource_memory_pressure",
                    alert_type="memory_pressure",
                    severity="critical" if status == "critical" else "warning",
                    message=f"System memory pressure is {status}.",
                )
            cpu = snapshot.cpu_percent
            if isinstance(cpu, (int, float)) and cpu >= float(thresholds["cpu_percent"]):
                _upsert_alert(
                    db,
                    alert_key="resource_cpu_high",
                    alert_type="high_cpu",
                    severity="warning",
                    message=f"CPU usage is {cpu:.1f}%.",
                )
            total = snapshot.gpu_total_vram_mb
            used = snapshot.gpu_used_vram_mb
            if (
                snapshot.gpu_available
                and isinstance(total, (int, float))
                and isinstance(used, (int, float))
                and total > 0
            ):
                used_percent = 100.0 * used / total
                if used_percent >= float(thresholds["gpu_used_percent"]):
                    _upsert_alert(
                        db,
                        alert_key="resource_gpu_memory",
                        alert_type="gpu_memory",
                        severity="warning",
                        message=f"GPU memory usage is {used_percent:.1f}%.",
                    )
    return details


def prune_old_data() -> dict[str, int]:
    cutoff = _utc_now_naive() - timedelta(days=max(1, int(alert_thresholds()["retention_days"])))
    with get_db_context() as db:
        events = (
            db.query(AIPerformanceEvent)
            .filter(AIPerformanceEvent.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        alerts = (
            db.query(AIAlert)
            .filter(
                AIAlert.created_at < cutoff,
                AIAlert.status == "acknowledged",
            )
            .delete(synchronize_session=False)
        )
    return {"events": int(events or 0), "alerts": int(alerts or 0)}


def acknowledge_alert(alert_id: int) -> bool:
    with get_db_context() as db:
        alert = db.query(AIAlert).filter(AIAlert.id == alert_id).first()
        if alert is None:
            return False
        alert.status = "acknowledged"
        alert.acknowledged_at = _utc_now_naive()
    return True


def _date_bounds(
    start_date: date | datetime | None,
    end_date: date | datetime | None,
) -> tuple[datetime, datetime]:
    end = _utc_now_naive()
    if isinstance(end_date, datetime):
        end = end_date.replace(tzinfo=None)
    elif isinstance(end_date, date):
        end = datetime.combine(end_date, dt_time.max)
    start = end - timedelta(days=7)
    if isinstance(start_date, datetime):
        start = start_date.replace(tzinfo=None)
    elif isinstance(start_date, date):
        start = datetime.combine(start_date, dt_time.min)
    if start > end:
        start, end = end, start
    return start, end


def _request_filters(start: datetime, end: datetime) -> Any:
    return and_(
        AIPerformanceEvent.event_type == EVENT_LLM_REQUEST,
        AIPerformanceEvent.created_at >= start,
        AIPerformanceEvent.created_at <= end,
    )


def _alert_row(row: AIAlert) -> dict[str, Any]:
    return {
        "id": row.id,
        "alert_key": row.alert_key,
        "alert_type": row.alert_type,
        "severity": row.severity,
        "provider": row.provider,
        "message": row.message,
        "status": row.status,
        "created_at": _iso(row.created_at),
        "acknowledged_at": _iso(row.acknowledged_at),
    }


def _event_row(row: AIPerformanceEvent, *, include_details: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": row.id,
        "created_at": _iso(row.created_at),
        "event_type": row.event_type,
        "provider": row.provider,
        "model": row.model,
        "feature": row.feature,
        "status": row.status,
        "severity": row.severity,
        "latency_ms": row.latency_ms,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "total_tokens": row.total_tokens,
        "error_category": row.error_category,
        "error_message": row.error_message,
        "request_id": row.request_id,
        "attempts": row.attempts,
        "used_fallback": row.used_fallback,
    }
    if include_details:
        result["details"] = row.details_json or {}
    return result


def _optional_filter(column: Any, value: Any) -> list[Any]:
    if value is None or str(value).strip() in {"", "all", "All"}:
        return []
    return [column == value]


def list_request_events(
    *,
    start_date: date | datetime | None = None,
    end_date: date | datetime | None = None,
    provider: str | None = None,
    model: str | None = None,
    feature: str | None = None,
    status: str | None = None,
    error_category: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    start, end = _date_bounds(start_date, end_date)
    filters = [_request_filters(start, end)]
    filters.extend(_optional_filter(AIPerformanceEvent.provider, provider))
    filters.extend(_optional_filter(AIPerformanceEvent.model, model))
    filters.extend(_optional_filter(AIPerformanceEvent.feature, feature))
    filters.extend(_optional_filter(AIPerformanceEvent.status, status))
    filters.extend(_optional_filter(AIPerformanceEvent.error_category, error_category))
    with get_db_context() as db:
        total = int(db.query(func.count(AIPerformanceEvent.id)).filter(*filters).scalar() or 0)
        rows = (
            db.query(AIPerformanceEvent)
            .filter(*filters)
            .order_by(AIPerformanceEvent.created_at.desc(), AIPerformanceEvent.id.desc())
            .offset(max(0, int(offset)))
            .limit(max(1, min(500, int(limit))))
            .all()
        )
    return {"total": total, "rows": [_event_row(row) for row in rows]}


def get_filter_options() -> dict[str, list[str]]:
    with get_db_context() as db:
        def values(column: Any) -> list[str]:
            rows = (
                db.query(column)
                .filter(column.is_not(None))
                .distinct()
                .order_by(column)
                .limit(200)
                .all()
            )
            return sorted({str(row[0]) for row in rows if str(row[0]).strip()})

        return {
            "providers": values(AIPerformanceEvent.provider),
            "models": values(AIPerformanceEvent.model),
            "features": values(AIPerformanceEvent.feature),
            "statuses": values(AIPerformanceEvent.status),
            "error_categories": values(AIPerformanceEvent.error_category),
        }


def _cost_for(
    provider: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    pricing: dict[str, Any],
) -> float:
    if not pricing.get("verified"):
        return 0.0
    rates = pricing.get("rates") or {}
    rate = rates.get(provider or "") or rates.get("*")
    if not isinstance(rate, dict):
        return 0.0
    input_cost = (input_tokens or 0) * float(rate.get("input", 0.0)) / 1_000_000
    output_cost = (output_tokens or 0) * float(rate.get("output", 0.0)) / 1_000_000
    return input_cost + output_cost


def get_dashboard_data(
    *,
    start_date: date | datetime | None = None,
    end_date: date | datetime | None = None,
    health: dict[str, dict[str, Any]] | None = None,
    provider_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from services.llm_service import get_provider_registry

    start, end = _date_bounds(start_date, end_date)
    filters = _request_filters(start, end)
    registry = get_provider_registry()
    health = health or {}
    provider_status = provider_status or {}
    pricing = get_pricing_config()
    thresholds = alert_thresholds()
    with get_db_context() as db:
        total = int(db.query(func.count(AIPerformanceEvent.id)).filter(filters).scalar() or 0)
        successes = int(
            db.query(func.count(AIPerformanceEvent.id))
            .filter(filters, AIPerformanceEvent.status == "success")
            .scalar()
            or 0
        )
        failures = int(
            db.query(func.count(AIPerformanceEvent.id))
            .filter(filters, AIPerformanceEvent.status == "failed")
            .scalar()
            or 0
        )
        avg_latency = (
            db.query(func.avg(AIPerformanceEvent.latency_ms))
            .filter(filters, AIPerformanceEvent.latency_ms.is_not(None))
            .scalar()
        )
        latency_count = int(
            db.query(func.count(AIPerformanceEvent.id))
            .filter(filters, AIPerformanceEvent.latency_ms.is_not(None))
            .scalar()
            or 0
        )
        p95_latency = None
        if latency_count:
            offset = min(latency_count - 1, max(0, math.ceil(latency_count * 0.95) - 1))
            p95_latency = (
                db.query(AIPerformanceEvent.latency_ms)
                .filter(filters, AIPerformanceEvent.latency_ms.is_not(None))
                .order_by(AIPerformanceEvent.latency_ms)
                .offset(offset)
                .limit(1)
                .scalar()
            )
        token_totals = (
            db.query(
                func.coalesce(func.sum(AIPerformanceEvent.input_tokens), 0),
                func.coalesce(func.sum(AIPerformanceEvent.output_tokens), 0),
                func.coalesce(func.sum(AIPerformanceEvent.total_tokens), 0),
            )
            .filter(filters)
            .one()
        )
        provider_rows = (
            db.query(
                AIPerformanceEvent.provider,
                func.count(AIPerformanceEvent.id),
                func.sum(case((AIPerformanceEvent.status == "success", 1), else_=0)),
                func.avg(AIPerformanceEvent.latency_ms),
                func.coalesce(func.sum(AIPerformanceEvent.used_fallback), 0),
                func.coalesce(func.sum(AIPerformanceEvent.input_tokens), 0),
                func.coalesce(func.sum(AIPerformanceEvent.output_tokens), 0),
            )
            .filter(filters)
            .group_by(AIPerformanceEvent.provider)
            .all()
        )
        model_rows = (
            db.query(
                AIPerformanceEvent.provider,
                AIPerformanceEvent.model,
                func.count(AIPerformanceEvent.id),
                func.sum(case((AIPerformanceEvent.status == "success", 1), else_=0)),
                func.avg(AIPerformanceEvent.latency_ms),
                func.coalesce(func.sum(AIPerformanceEvent.input_tokens), 0),
                func.coalesce(func.sum(AIPerformanceEvent.output_tokens), 0),
            )
            .filter(filters)
            .group_by(AIPerformanceEvent.provider, AIPerformanceEvent.model)
            .order_by(func.count(AIPerformanceEvent.id).desc())
            .all()
        )
        error_rows = (
            db.query(
                AIPerformanceEvent.error_category,
                func.count(AIPerformanceEvent.id),
            )
            .filter(filters, AIPerformanceEvent.status == "failed")
            .group_by(AIPerformanceEvent.error_category)
            .order_by(func.count(AIPerformanceEvent.id).desc())
            .all()
        )
        bucket_format = "%Y-%m-%d" if (end - start).days > 2 else "%Y-%m-%d %H:00"
        trend_rows = (
            db.query(
                func.strftime(bucket_format, AIPerformanceEvent.created_at).label("bucket"),
                func.count(AIPerformanceEvent.id),
                func.avg(AIPerformanceEvent.latency_ms),
                func.sum(case((AIPerformanceEvent.status == "success", 1), else_=0)),
            )
            .filter(filters)
            .group_by("bucket")
            .order_by("bucket")
            .all()
        )
        alerts = (
            db.query(AIAlert)
            .order_by(
                case((AIAlert.status == "active", 0), else_=1),
                AIAlert.created_at.desc(),
            )
            .limit(100)
            .all()
        )
        active_alerts = int(
            db.query(func.count(AIAlert.id)).filter(AIAlert.status == "active").scalar() or 0
        )
        resource_rows = (
            db.query(AIPerformanceEvent)
            .filter(AIPerformanceEvent.event_type == EVENT_RESOURCE_SNAPSHOT)
            .order_by(AIPerformanceEvent.created_at.desc())
            .limit(30)
            .all()
        )
        health_rows = (
            db.query(AIPerformanceEvent)
            .filter(AIPerformanceEvent.event_type == EVENT_PROVIDER_HEALTH)
            .order_by(AIPerformanceEvent.created_at.desc())
            .limit(60)
            .all()
        )
        recent_rows = (
            db.query(AIPerformanceEvent)
            .filter(filters)
            .order_by(AIPerformanceEvent.created_at.desc(), AIPerformanceEvent.id.desc())
            .limit(100)
            .all()
        )

    provider_metrics: dict[str, dict[str, Any]] = {}
    for row in provider_rows:
        provider = str(row[0] or "unknown")
        input_tokens = int(row[5] or 0)
        output_tokens = int(row[6] or 0)
        provider_metrics[provider] = {
            "provider": provider,
            "requests": int(row[1] or 0),
            "successes": int(row[2] or 0),
            "avg_latency_ms": float(row[3]) if isinstance(row[3], (int, float)) else None,
            "fallbacks": int(row[4] or 0),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost": _cost_for(provider, input_tokens, output_tokens, pricing),
        }
    provider_rows_ui: list[dict[str, Any]] = []
    for name, info in registry.items():
        metrics = provider_metrics.get(name, {})
        status_info = (provider_status.get("providers") or {}).get(name, {})
        health_info = health.get(name, {})
        provider_rows_ui.append(
            {
                "Provider": info["label"],
                "Role": "Primary" if name == provider_status.get("primary") else "Fallback",
                "In chain": "Yes" if name in (provider_status.get("chain") or []) else "No",
                "Health": health_info.get("status") or "Not checked",
                "Configured model": info["model"],
                "Available model": health_info.get("model") or info["model"],
                "Model available": (
                    "N/A"
                    if health_info.get("model_available") is None
                    else ("Yes" if health_info.get("model_available") else "No")
                ),
                "Requests": metrics.get("requests", 0),
                "Success rate": (
                    f"{100.0 * metrics['successes'] / metrics['requests']:.1f}%"
                    if metrics.get("requests")
                    else "N/A"
                ),
                "Avg latency": (
                    f"{metrics['avg_latency_ms']:.0f} ms"
                    if metrics.get("avg_latency_ms") is not None
                    else "N/A"
                ),
                "Fallbacks": metrics.get("fallbacks", 0),
                "Circuit open": "Yes" if status_info.get("circuit_open") else "No",
                "Health error": sanitize_error_message(health_info.get("error")) or "-",
            }
        )
    model_rows_ui = []
    for row in model_rows:
        input_tokens = int(row[5] or 0)
        output_tokens = int(row[6] or 0)
        model_rows_ui.append(
            {
                "Provider": row[0] or "unknown",
                "Model": row[1] or "unknown",
                "Requests": int(row[2] or 0),
                "Success rate": (
                    f"{100.0 * int(row[3] or 0) / int(row[2] or 1):.1f}%"
                    if row[2]
                    else "N/A"
                ),
                "Avg latency": (
                    f"{float(row[4]):.0f} ms" if isinstance(row[4], (int, float)) else "N/A"
                ),
                "Input tokens": input_tokens,
                "Output tokens": output_tokens,
                "Estimated cost": (
                    f"${_cost_for(str(row[0] or ''), input_tokens, output_tokens, pricing):.6f}"
                    if pricing.get("verified")
                    else "N/A"
                ),
            }
        )
    error_rows_ui = [
        {
            "Error category": row[0] or "provider_error",
            "Failures": int(row[1] or 0),
        }
        for row in error_rows
    ]
    trend_rows_ui = [
        {
            "bucket": str(row[0]),
            "requests": int(row[1] or 0),
            "avg_latency_ms": float(row[2]) if isinstance(row[2], (int, float)) else 0.0,
            "successes": int(row[3] or 0),
        }
        for row in trend_rows
    ]
    total_input = int(token_totals[0] or 0)
    total_output = int(token_totals[1] or 0)
    total_tokens = int(token_totals[2] or total_input + total_output)
    estimated_cost = (
        sum(
            _cost_for(
                metrics.get("provider"),
                metrics.get("input_tokens"),
                metrics.get("output_tokens"),
                pricing,
            )
            for metrics in provider_metrics.values()
        )
        if pricing.get("verified")
        else None
    )
    return {
        "start": _iso(start),
        "end": _iso(end),
        "kpis": {
            "total_requests": total,
            "success_rate": (100.0 * successes / total) if total else None,
            "failures": failures,
            "avg_latency_ms": float(avg_latency) if isinstance(avg_latency, (int, float)) else None,
            "p95_latency_ms": float(p95_latency) if isinstance(p95_latency, (int, float)) else None,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_tokens,
            "estimated_cost": estimated_cost,
            "active_alerts": active_alerts,
            "fallbacks": int(sum(int(item.get("fallbacks", 0)) for item in provider_metrics.values())),
        },
        "providers": provider_rows_ui,
        "models": model_rows_ui,
        "errors": error_rows_ui,
        "trend": trend_rows_ui,
        "alerts": [_alert_row(row) for row in alerts],
        "resources": [_event_row(row, include_details=True) for row in resource_rows],
        "health_history": [_event_row(row) for row in health_rows],
        "recent_requests": [_event_row(row) for row in recent_rows],
        "pricing": pricing,
        "thresholds": thresholds,
        "provider_status": provider_status,
    }
