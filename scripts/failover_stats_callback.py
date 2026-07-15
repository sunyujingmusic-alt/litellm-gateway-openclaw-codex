from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from litellm.integrations.custom_logger import CustomLogger


DB_PATH = Path(os.environ.get("FAILOVER_STATS_DB_PATH", "/app/failover-stats/failover_stats.sqlite3"))
JSONL_PATH = Path(os.environ.get("FAILOVER_STATS_JSONL_PATH", "/app/failover-stats/failover_events.jsonl"))
ERROR_LIMIT = int(os.environ.get("FAILOVER_STATS_ERROR_LIMIT", "600"))
ASYNC_COOLDOWN_BRIDGE = os.environ.get("LITELLM_ASYNC_COOLDOWN_BRIDGE", "0").strip().lower() in {"1", "true", "yes", "on"}
ASYNC_COOLDOWN_SECONDS = int(os.environ.get("LITELLM_ASYNC_COOLDOWN_SECONDS", "300"))
REDIS_URL = os.environ.get("REDIS_URL", "")

_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_REDIS_CLIENT: Any = None
_REDIS_INIT_FAILED = False
_REDIS_LOCK = threading.Lock()
SQLITE_RETRIES = 5
STATE_CACHE_LIMIT = int(os.environ.get("FAILOVER_STATS_STATE_CACHE_LIMIT", "2048"))
_ATTEMPT_STARTS: dict[tuple[str, str, int], dict[str, Any]] = {}
_REQUEST_FAILURES: dict[str, dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _safe_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _safe_str(value: Any, limit: int = 300) -> str:
    if value is None:
        return ""
    text = str(value)
    return text[:limit]


def _metadata(kwargs: dict[str, Any]) -> dict[str, Any]:
    metadata = kwargs.get("metadata") or kwargs.get("litellm_metadata")
    return metadata if isinstance(metadata, dict) else {}


def _call_id(kwargs: dict[str, Any]) -> str:
    metadata = _metadata(kwargs)
    candidates = [
        kwargs.get("litellm_call_id"),
        kwargs.get("call_id"),
        kwargs.get("request_id"),
        metadata.get("litellm_call_id"),
        metadata.get("call_id"),
        metadata.get("request_id"),
    ]
    return next((_safe_str(item, 160) for item in candidates if item), "")


def _exception_dict(exc: Any) -> dict[str, str]:
    if exc is None:
        return {"exceptionType": "", "exceptionStatus": 0, "error": "", "errorCategory": "unknown"}
    return {
        "exceptionType": exc.__class__.__name__,
        "exceptionStatus": _exception_status(exc),
        "error": _safe_str(exc, ERROR_LIMIT),
        "errorCategory": _error_category(exc),
    }


def _hidden_params(response_obj: Any) -> dict[str, Any]:
    hidden = getattr(response_obj, "_hidden_params", None)
    return hidden if isinstance(hidden, dict) else {}


def _litellm_params(kwargs: dict[str, Any]) -> dict[str, Any]:
    params = kwargs.get("litellm_params")
    return params if isinstance(params, dict) else {}


def _deployment_id(kwargs: dict[str, Any]) -> str:
    params = _litellm_params(kwargs)
    model_info = params.get("model_info")
    if isinstance(model_info, dict):
        model_id = _safe_str(model_info.get("id"), 200)
        if model_id:
            return model_id
    return _safe_str(kwargs.get("model"), 200)


def _exception_status(exc: Any) -> int:
    status = (
        getattr(exc, "status_code", None)
        or getattr(exc, "http_status", None)
        or getattr(exc, "status", None)
    )
    try:
        return int(status)
    except Exception:
        return 0


def _error_category(exc: Any) -> str:
    if exc is None:
        return "unknown"
    status = _exception_status(exc)
    exc_name = exc.__class__.__name__.lower()
    text = str(exc).lower()
    if "timeout" in exc_name or "timed out" in text or "timeout" in text:
        return "timeout"
    if any(token in text for token in ["cannot connect", "connection", "connect error", "dns", "network is unreachable"]):
        return "connect_error"
    if status in {401, 403} or "unauthorized" in text or "authentication" in text or "invalid api key" in text:
        return "auth"
    if status == 429 or "rate limit" in text or "rate_limit" in text:
        return "rate_limit"
    if "insufficient_user_quota" in text or "quota" in text or "额度" in str(exc):
        return "quota"
    if status and 400 <= status < 500:
        return "bad_request"
    if status and 500 <= status < 600:
        return "upstream_5xx"
    if "bad gateway" in text or "502" in text or "503" in text or "504" in text:
        return "upstream_5xx"
    if "server" in exc_name or "internal server" in text:
        return "server_error"
    return "unknown"


def _cooldown_seconds(kwargs: dict[str, Any]) -> int:
    params = _litellm_params(kwargs)
    value = params.get("cooldown_time", ASYNC_COOLDOWN_SECONDS)
    try:
        seconds = int(float(value))
    except Exception:
        seconds = ASYNC_COOLDOWN_SECONDS
    return max(1, seconds)


def _seconds_to_ms(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(float(value) * 1000)
    except Exception:
        return 0


def _configured_timeout_ms(kwargs: dict[str, Any], key: str) -> int:
    params = _litellm_params(kwargs)
    value = params.get(key)
    if key == "stream_timeout" and (value is None or value == ""):
        value = params.get("timeout")
    return _seconds_to_ms(value)


def _request_stream(kwargs: dict[str, Any]) -> bool | None:
    stream = _safe_bool(kwargs.get("stream"))
    if stream is not None:
        return stream
    optional = kwargs.get("optional_params")
    if isinstance(optional, dict):
        return _safe_bool(optional.get("stream"))
    return None


def _iso_from_time(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        timestamp = value
    else:
        try:
            timestamp = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return _safe_str(value, 80)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat()


def _epoch_ms(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        timestamp = value
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc).timestamp() * 1000
    try:
        number = float(value)
    except Exception:
        return None
    if number > 10_000_000_000:
        return number
    return number * 1000


def _ms_between(start_ms: float | None, end_ms: float | None) -> float | None:
    if start_ms is None or end_ms is None:
        return None
    return round(max(0.0, end_ms - start_ms), 3)


def _response_status(response_obj: Any) -> int:
    hidden = _hidden_params(response_obj)
    for value in [
        hidden.get("status_code"),
        hidden.get("response_status"),
        getattr(response_obj, "status_code", None),
        getattr(response_obj, "status", None),
    ]:
        status = _safe_int(value)
        if status:
            return status
    return 200 if response_obj is not None else 0


def _should_bridge_cooldown(exc: Any) -> bool:
    if not ASYNC_COOLDOWN_BRIDGE or exc is None:
        return False
    exc_name = exc.__class__.__name__
    error_text = str(exc).lower()
    if exc_name in {"BadRequestError", "ContentPolicyViolationError"}:
        return False
    if "unsupported parameter" in error_text:
        return False
    if "insufficient_user_quota" in error_text or "quota" in error_text or "额度" in str(exc):
        return True
    status = _exception_status(exc)
    if status and 400 <= status < 500:
        return status in {401, 404, 408, 429}
    return True


def _cooldown_skip_reason(kwargs: dict[str, Any], exc: Any) -> str:
    if not ASYNC_COOLDOWN_BRIDGE:
        return "bridge_disabled"
    if exc is None:
        return "no_exception"
    if not _should_bridge_cooldown(exc):
        return "non_cooldown_error"
    if not _deployment_id(kwargs):
        return "missing_deployment_id"
    if not REDIS_URL:
        return "redis_unavailable"
    return ""


def _redis_client() -> Any:
    global _REDIS_CLIENT, _REDIS_INIT_FAILED
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    if _REDIS_INIT_FAILED or not REDIS_URL:
        return None
    with _REDIS_LOCK:
        if _REDIS_CLIENT is not None:
            return _REDIS_CLIENT
        if _REDIS_INIT_FAILED or not REDIS_URL:
            return None
        try:
            import redis  # type: ignore

            _REDIS_CLIENT = redis.Redis.from_url(
                REDIS_URL,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
            _REDIS_CLIENT.ping()
        except Exception:
            _REDIS_INIT_FAILED = True
            _REDIS_CLIENT = None
        return _REDIS_CLIENT


def _cooldown_event_base(kwargs: dict[str, Any], exc: Any, trigger_event_type: str) -> dict[str, Any]:
    deployment_id = _deployment_id(kwargs)
    model = _safe_str(kwargs.get("model"), 200)
    error = _exception_dict(exc)
    return {
        "ts": _now_iso(),
        "callId": _call_id(kwargs),
        "requestedModel": _safe_str(kwargs.get("original_model") or kwargs.get("model"), 200),
        "targetModelGroup": model,
        "modelId": deployment_id,
        "cooldownKey": f"deployment:{deployment_id}:cooldown" if deployment_id else "",
        "triggerEventType": trigger_event_type,
        "success": False,
        **error,
    }


def _bridge_async_cooldown(kwargs: dict[str, Any], exc: Any, trigger_event_type: str) -> bool:
    skip_reason = _cooldown_skip_reason(kwargs, exc)
    if skip_reason:
        event = _cooldown_event_base(kwargs, exc, trigger_event_type)
        event.update({"eventType": "cooldown_skipped", "reason": skip_reason})
        _write_event(event)
        return False
    deployment_id = _deployment_id(kwargs)
    client = _redis_client()
    if client is None:
        event = _cooldown_event_base(kwargs, exc, trigger_event_type)
        event.update({"eventType": "cooldown_skipped", "reason": "redis_unavailable"})
        _write_event(event)
        return False
    cooldown = _cooldown_seconds(kwargs)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=cooldown)
    payload = {
        "exception_received": _safe_str(exc, 200),
        "status_code": str(_exception_status(exc) or ""),
        "timestamp": time.time(),
        "cooldown_time": cooldown,
    }
    cooldown_key = f"deployment:{deployment_id}:cooldown"
    try:
        client.set(
            name=cooldown_key,
            value=json.dumps(payload),
            ex=cooldown,
        )
    except Exception as redis_exc:
        event = _cooldown_event_base(kwargs, exc, trigger_event_type)
        event.update({"eventType": "cooldown_skipped", "reason": "redis_write_failed", "detail": _safe_str(redis_exc, 300)})
        _write_event(event)
        return False

    event = _cooldown_event_base(kwargs, exc, trigger_event_type)
    event.update(
        {
            "eventType": "cooldown_set",
            "cooldownSeconds": cooldown,
            "expiresAt": expires_at.isoformat(),
            "redisWriteOk": True,
            "success": True,
        }
    )
    _write_event(event)
    return True


def _duration_ms(start_time: Any, end_time: Any) -> float | None:
    if not start_time or not end_time:
        return None
    try:
        return round((end_time - start_time).total_seconds() * 1000, 3)
    except Exception:
        return _ms_between(_epoch_ms(start_time), _epoch_ms(end_time))


def _prune_state_locked() -> None:
    while len(_ATTEMPT_STARTS) > STATE_CACHE_LIMIT:
        _ATTEMPT_STARTS.pop(next(iter(_ATTEMPT_STARTS)))
    while len(_REQUEST_FAILURES) > STATE_CACHE_LIMIT:
        _REQUEST_FAILURES.pop(next(iter(_REQUEST_FAILURES)))


def _attempt_cache_key(call_id: str, model_id: str, target_model_group: str, depth: int) -> tuple[str, str, int]:
    return (call_id, model_id or target_model_group, depth)


def _record_attempt_start(event: dict[str, Any], started_at: datetime) -> bool:
    call_id = _safe_str(event.get("callId"), 160)
    if not call_id:
        return True
    key = _attempt_cache_key(
        call_id,
        _safe_str(event.get("modelId"), 200),
        _safe_str(event.get("targetModelGroup"), 200),
        _safe_int(event.get("depth")),
    )
    started_ms = _epoch_ms(started_at)
    with _STATE_LOCK:
        existing = _ATTEMPT_STARTS.get(key)
        existing_ms = _safe_float((existing or {}).get("startedAtMs"))
        if existing_ms is not None and started_ms is not None and abs(started_ms - existing_ms) <= 250:
            return False
        _ATTEMPT_STARTS[key] = {
            "startedAt": event.get("startedAt") or event.get("ts"),
            "startedAtMs": started_ms,
            "targetModelGroup": event.get("targetModelGroup", ""),
            "modelId": event.get("modelId", ""),
            "depth": _safe_int(event.get("depth")),
        }
        _prune_state_locked()
    return True


def _lookup_attempt_start(kwargs: dict[str, Any], target_model_group: str, model_id: str) -> dict[str, Any]:
    call_id = _call_id(kwargs)
    if not call_id:
        return {}
    depth = _safe_int(kwargs.get("fallback_depth"))
    exact_keys = [
        _attempt_cache_key(call_id, model_id, target_model_group, depth),
        _attempt_cache_key(call_id, "", target_model_group, depth),
    ]
    with _STATE_LOCK:
        for key in exact_keys:
            if key in _ATTEMPT_STARTS:
                return dict(_ATTEMPT_STARTS[key])
        for (cached_call_id, _cached_model, cached_depth), item in reversed(_ATTEMPT_STARTS.items()):
            if cached_call_id == call_id and cached_depth == depth:
                return dict(item)
        for (cached_call_id, cached_model, _cached_depth), item in reversed(_ATTEMPT_STARTS.items()):
            if cached_call_id == call_id and cached_model in {model_id, target_model_group}:
                return dict(item)
        for (cached_call_id, _cached_model, _cached_depth), item in reversed(_ATTEMPT_STARTS.items()):
            if cached_call_id == call_id:
                return dict(item)
    return {}


def _record_request_failure(kwargs: dict[str, Any], event: dict[str, Any], start_time: Any, end_time: Any, exception: Any) -> None:
    call_id = _call_id(kwargs)
    if not call_id:
        return
    error = _exception_dict(exception)
    with _STATE_LOCK:
        _REQUEST_FAILURES[call_id] = {
            "originalFailureAt": event.get("endedAt") or event.get("ts"),
            "originalFailureAtMs": _epoch_ms(end_time),
            "originalStartedAt": event.get("startedAt"),
            "originalStartedAtMs": _epoch_ms(start_time),
            "originalModelGroup": event.get("targetModelGroup", ""),
            "originalModelId": event.get("modelId", ""),
            **{f"original{key[0].upper()}{key[1:]}": value for key, value in error.items()},
        }
        _prune_state_locked()


def _lookup_request_failure(kwargs: dict[str, Any]) -> dict[str, Any]:
    call_id = _call_id(kwargs)
    if not call_id:
        return {}
    with _STATE_LOCK:
        return dict(_REQUEST_FAILURES.get(call_id) or {})


def _fallback_timing_fields(kwargs: dict[str, Any], target_model_group: str, model_id: str) -> dict[str, Any]:
    completed = _now_dt()
    completed_ms = _epoch_ms(completed)
    attempt = _lookup_attempt_start(kwargs, target_model_group, model_id)
    failure = _lookup_request_failure(kwargs)
    fallback_started_ms = _safe_float(attempt.get("startedAtMs"))
    original_failure_ms = _safe_float(failure.get("originalFailureAtMs"))
    original_started_ms = _safe_float(failure.get("originalStartedAtMs"))
    return {
        "originalFailureAt": failure.get("originalFailureAt", ""),
        "fallbackStartedAt": attempt.get("startedAt", ""),
        "fallbackCompletedAt": completed.isoformat(),
        "fallbackDecisionDelayMs": _ms_between(original_failure_ms, fallback_started_ms),
        "fallbackAttemptDurationMs": _ms_between(fallback_started_ms, completed_ms),
        "totalDurationMs": _ms_between(original_started_ms, completed_ms),
        "originalModelId": failure.get("originalModelId", ""),
    }


def _write_attempt_start_event(model: str, kwargs: dict[str, Any]) -> None:
    started_at = _now_dt()
    target_model_group = _safe_str(model or kwargs.get("model"), 200)
    event = {
        "ts": started_at.isoformat(),
        "eventType": "attempt_start",
        "callId": _call_id(kwargs),
        "requestedModel": _safe_str(kwargs.get("original_model") or kwargs.get("model") or model, 200),
        "originalModelGroup": _safe_str(kwargs.get("original_model_group") or "", 200),
        "targetModelGroup": target_model_group,
        "finalModelGroup": target_model_group,
        "modelId": _deployment_id(kwargs),
        "depth": _safe_int(kwargs.get("fallback_depth")),
        "maxFallbacks": _safe_int(kwargs.get("max_fallbacks")),
        "stream": _request_stream(kwargs),
        "startedAt": started_at.isoformat(),
        "configuredTimeoutMs": _configured_timeout_ms(kwargs, "timeout"),
        "configuredStreamTimeoutMs": _configured_timeout_ms(kwargs, "stream_timeout"),
        "configuredCooldownSeconds": _cooldown_seconds(kwargs),
        "success": False,
    }
    if _record_attempt_start(event, started_at):
        _write_event(event)
    try:
        return round((end_time - start_time).total_seconds() * 1000, 3)
    except Exception:
        return None


def _base_attempt_event(
    kwargs: dict[str, Any],
    event_type: str,
    model: str,
    success: bool,
    start_time: Any,
    end_time: Any,
    exception: Any = None,
) -> dict[str, Any]:
    event = {
        "ts": _now_iso(),
        "eventType": event_type,
        "callId": _call_id(kwargs),
        "requestedModel": _safe_str(kwargs.get("original_model") or kwargs.get("model"), 200),
        "originalModelGroup": _safe_str(kwargs.get("original_model_group") or "", 200),
        "targetModelGroup": model,
        "finalModelGroup": model,
        "depth": _safe_int(kwargs.get("fallback_depth")),
        "maxFallbacks": _safe_int(kwargs.get("max_fallbacks")),
        "success": success,
        "stream": _request_stream(kwargs),
        "startedAt": _iso_from_time(start_time),
        "endedAt": _iso_from_time(end_time),
        "durationMs": _duration_ms(start_time, end_time),
        "configuredTimeoutMs": _configured_timeout_ms(kwargs, "timeout"),
        "configuredStreamTimeoutMs": _configured_timeout_ms(kwargs, "stream_timeout"),
        "configuredCooldownSeconds": _cooldown_seconds(kwargs),
        "modelId": _deployment_id(kwargs),
    }
    if exception is not None:
        event.update(_exception_dict(exception))
        event["shouldCooldown"] = _should_bridge_cooldown(exception)
    return event


def _ensure_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("pragma busy_timeout=10000")
        conn.execute(
            """
            create table if not exists failover_events (
              id integer primary key autoincrement,
              ts text not null,
              event_type text not null,
              call_id text,
              requested_model text,
              original_model_group text,
              target_model_group text,
              final_model_group text,
              depth integer not null default 0,
              max_fallbacks integer not null default 0,
              success integer not null default 0,
              exception_type text,
              error text,
              duration_ms real,
              model_id text,
              raw_json text not null
            )
            """
        )
        conn.execute("create index if not exists idx_failover_events_ts on failover_events(ts)")
        conn.execute("create index if not exists idx_failover_events_type on failover_events(event_type)")
        conn.execute("create index if not exists idx_failover_events_depth on failover_events(depth)")
        conn.commit()


def _write_event(event: dict[str, Any]) -> None:
    event = {key: value for key, value in event.items() if value is not None}
    event.setdefault("ts", _now_iso())
    raw = json.dumps(event, ensure_ascii=False, sort_keys=True)
    with _LOCK:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with JSONL_PATH.open("a", encoding="utf-8") as handle:
            handle.write(raw + "\n")

    last_exc: Exception | None = None
    for attempt in range(SQLITE_RETRIES):
        try:
            _ensure_db()
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute("pragma busy_timeout=10000")
                conn.execute(
                    """
                    insert into failover_events (
                      ts, event_type, call_id, requested_model, original_model_group,
                      target_model_group, final_model_group, depth, max_fallbacks,
                      success, exception_type, error, duration_ms, model_id, raw_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.get("ts", ""),
                        event.get("eventType", ""),
                        event.get("callId", ""),
                        event.get("requestedModel", ""),
                        event.get("originalModelGroup", ""),
                        event.get("targetModelGroup", ""),
                        event.get("finalModelGroup", ""),
                        _safe_int(event.get("depth")),
                        _safe_int(event.get("maxFallbacks")),
                        1 if event.get("success") else 0,
                        event.get("exceptionType", ""),
                        event.get("error", ""),
                        _safe_float(event.get("durationMs")),
                        event.get("modelId", ""),
                        raw,
                    ),
                )
                conn.commit()
            return
        except sqlite3.Error as exc:
            last_exc = exc
            time.sleep(0.05 * (attempt + 1))
    print(f"failover-stats-callback sqlite write skipped: {last_exc}", flush=True)


class FailoverStatsCallback(CustomLogger):
    def log_pre_api_call(self, model: str, messages: Any, kwargs: dict[str, Any]) -> None:
        _write_attempt_start_event(model, kwargs)

    async def async_log_pre_api_call(self, model: str, messages: Any, kwargs: dict[str, Any]) -> None:
        _write_attempt_start_event(model, kwargs)

    async def async_log_success_event(self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any) -> None:
        hidden = _hidden_params(response_obj)
        model = _safe_str(kwargs.get("model"), 200)
        event = _base_attempt_event(kwargs, "request_success", model, True, start_time, end_time)
        event["modelId"] = _safe_str(hidden.get("model_id"), 200) or event["modelId"]
        event["responseStatus"] = _response_status(response_obj)
        _write_event(event)

    async def async_log_failure_event(self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any) -> None:
        model = _safe_str(kwargs.get("model"), 200)
        exception = response_obj or kwargs.get("exception")
        cooldown_written = _bridge_async_cooldown(kwargs, exception, "request_failure")
        event = _base_attempt_event(kwargs, "request_failure", model, False, start_time, end_time, exception)
        event["cooldownWritten"] = cooldown_written
        _record_request_failure(kwargs, event, start_time, end_time, exception)
        _write_event(event)

    async def log_success_fallback_event(self, original_model_group: str, kwargs: dict[str, Any], original_exception: Exception) -> None:
        model = _safe_str(kwargs.get("model"), 200)
        model_id = _deployment_id(kwargs)
        original_error = _exception_dict(original_exception)
        event = {
            "ts": _now_iso(),
            "eventType": "fallback_success",
            "callId": _call_id(kwargs),
            "requestedModel": _safe_str(original_model_group, 200),
            "originalModelGroup": _safe_str(original_model_group, 200),
            "targetModelGroup": model,
            "finalModelGroup": model,
            "depth": _safe_int(kwargs.get("fallback_depth")),
            "maxFallbacks": _safe_int(kwargs.get("max_fallbacks")),
            "success": True,
            "modelId": model_id,
            "lastModelGroup": model,
            "lastModelId": model_id,
            "originalExceptionType": original_error["exceptionType"],
            "originalExceptionStatus": original_error["exceptionStatus"],
            "originalErrorCategory": original_error["errorCategory"],
            "lastExceptionType": original_error["exceptionType"],
            "lastExceptionStatus": original_error["exceptionStatus"],
            "lastErrorCategory": original_error["errorCategory"],
            **_fallback_timing_fields(kwargs, model, model_id),
            **original_error,
        }
        _write_event(event)

    async def log_failure_fallback_event(self, original_model_group: str, kwargs: dict[str, Any], original_exception: Exception) -> None:
        model = _safe_str(kwargs.get("model"), 200)
        model_id = _deployment_id(kwargs)
        cooldown_written = _bridge_async_cooldown(kwargs, original_exception, "fallback_failure")
        original_error = _exception_dict(original_exception)
        event = {
            "ts": _now_iso(),
            "eventType": "fallback_failure",
            "callId": _call_id(kwargs),
            "requestedModel": _safe_str(original_model_group, 200),
            "originalModelGroup": _safe_str(original_model_group, 200),
            "targetModelGroup": model,
            "finalModelGroup": model,
            "depth": _safe_int(kwargs.get("fallback_depth")),
            "maxFallbacks": _safe_int(kwargs.get("max_fallbacks")),
            "success": False,
            "modelId": model_id,
            "lastModelGroup": model,
            "lastModelId": model_id,
            "cooldownWritten": cooldown_written,
            "originalExceptionType": original_error["exceptionType"],
            "originalExceptionStatus": original_error["exceptionStatus"],
            "originalErrorCategory": original_error["errorCategory"],
            "lastExceptionType": original_error["exceptionType"],
            "lastExceptionStatus": original_error["exceptionStatus"],
            "lastErrorCategory": original_error["errorCategory"],
            **_fallback_timing_fields(kwargs, model, model_id),
            **original_error,
        }
        _write_event(event)


proxy_handler_instance = FailoverStatsCallback()
