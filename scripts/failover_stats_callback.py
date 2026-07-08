from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from litellm.integrations.custom_logger import CustomLogger


DB_PATH = Path(os.environ.get("FAILOVER_STATS_DB_PATH", "/app/failover-stats/failover_stats.sqlite3"))
JSONL_PATH = Path(os.environ.get("FAILOVER_STATS_JSONL_PATH", "/app/failover-stats/failover_events.jsonl"))
ERROR_LIMIT = int(os.environ.get("FAILOVER_STATS_ERROR_LIMIT", "600"))

_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _safe_str(value: Any, limit: int = 300) -> str:
    if value is None:
        return ""
    text = str(value)
    return text[:limit]


def _metadata(kwargs: dict[str, Any]) -> dict[str, Any]:
    metadata = kwargs.get("metadata")
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
        return {"exceptionType": "", "error": ""}
    return {
        "exceptionType": exc.__class__.__name__,
        "error": _safe_str(exc, ERROR_LIMIT),
    }


def _hidden_params(response_obj: Any) -> dict[str, Any]:
    hidden = getattr(response_obj, "_hidden_params", None)
    return hidden if isinstance(hidden, dict) else {}


def _duration_ms(start_time: Any, end_time: Any) -> float | None:
    if not start_time or not end_time:
        return None
    try:
        return round((end_time - start_time).total_seconds() * 1000, 3)
    except Exception:
        return None


def _ensure_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
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
        _ensure_db()
        with JSONL_PATH.open("a", encoding="utf-8") as handle:
            handle.write(raw + "\n")
        with sqlite3.connect(DB_PATH) as conn:
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


class FailoverStatsCallback(CustomLogger):
    async def async_log_success_event(self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any) -> None:
        hidden = _hidden_params(response_obj)
        model = _safe_str(kwargs.get("model"), 200)
        event = {
            "ts": _now_iso(),
            "eventType": "request_success",
            "callId": _call_id(kwargs),
            "requestedModel": _safe_str(kwargs.get("original_model") or kwargs.get("model"), 200),
            "originalModelGroup": _safe_str(kwargs.get("original_model_group") or "", 200),
            "targetModelGroup": model,
            "finalModelGroup": model,
            "depth": _safe_int(kwargs.get("fallback_depth")),
            "maxFallbacks": _safe_int(kwargs.get("max_fallbacks")),
            "success": True,
            "durationMs": _duration_ms(start_time, end_time),
            "modelId": _safe_str(hidden.get("model_id"), 200),
        }
        _write_event(event)

    async def async_log_failure_event(self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any) -> None:
        model = _safe_str(kwargs.get("model"), 200)
        error = _exception_dict(response_obj)
        event = {
            "ts": _now_iso(),
            "eventType": "request_failure",
            "callId": _call_id(kwargs),
            "requestedModel": _safe_str(kwargs.get("original_model") or kwargs.get("model"), 200),
            "originalModelGroup": _safe_str(kwargs.get("original_model_group") or "", 200),
            "targetModelGroup": model,
            "finalModelGroup": model,
            "depth": _safe_int(kwargs.get("fallback_depth")),
            "maxFallbacks": _safe_int(kwargs.get("max_fallbacks")),
            "success": False,
            "durationMs": _duration_ms(start_time, end_time),
            **error,
        }
        _write_event(event)

    async def log_success_fallback_event(self, original_model_group: str, kwargs: dict[str, Any], original_exception: Exception) -> None:
        model = _safe_str(kwargs.get("model"), 200)
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
            **_exception_dict(original_exception),
        }
        _write_event(event)

    async def log_failure_fallback_event(self, original_model_group: str, kwargs: dict[str, Any], original_exception: Exception) -> None:
        model = _safe_str(kwargs.get("model"), 200)
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
            **_exception_dict(original_exception),
        }
        _write_event(event)


proxy_handler_instance = FailoverStatsCallback()
