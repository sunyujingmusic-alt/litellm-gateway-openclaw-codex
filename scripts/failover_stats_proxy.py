#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


UPSTREAM_URL = os.environ.get("FAILOVER_STATS_UPSTREAM_URL", "http://127.0.0.1:4028").rstrip("/")
DB_PATH = Path(os.environ.get("FAILOVER_STATS_DB_PATH", "./logs/failover-stats/failover_stats.sqlite3"))
JSONL_PATH = Path(os.environ.get("FAILOVER_STATS_JSONL_PATH", "./logs/failover-stats/failover_events.jsonl"))
DEFAULT_HOST = os.environ.get("FAILOVER_STATS_PROXY_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("FAILOVER_STATS_PROXY_PORT", "4130"))

_LOCK = threading.Lock()
SQLITE_RETRIES = 5


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def safe_str(value: Any, limit: int = 400) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def ensure_db() -> None:
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


def write_event(event: dict[str, Any]) -> None:
    event.setdefault("ts", now_iso())
    raw = json.dumps(event, ensure_ascii=False, sort_keys=True)
    with _LOCK:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with JSONL_PATH.open("a", encoding="utf-8") as handle:
            handle.write(raw + "\n")

    last_exc: Exception | None = None
    for attempt in range(SQLITE_RETRIES):
        try:
            ensure_db()
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
                        safe_int(event.get("depth")),
                        safe_int(event.get("maxFallbacks")),
                        1 if event.get("success") else 0,
                        event.get("exceptionType", ""),
                        event.get("error", ""),
                        event.get("durationMs"),
                        event.get("modelId", ""),
                        raw,
                    ),
                )
                conn.commit()
            return
        except sqlite3.Error as exc:
            last_exc = exc
            time.sleep(0.05 * (attempt + 1))
    print(f"failover-stats-proxy sqlite write skipped: {last_exc}", flush=True)


def request_model(body: bytes) -> str:
    try:
        parsed = json.loads(body.decode("utf-8")) if body else {}
        return safe_str(parsed.get("model"), 200) if isinstance(parsed, dict) else ""
    except Exception:
        return ""


def filtered_headers(headers: Any) -> dict[str, str]:
    blocked = {"host", "content-length", "connection", "accept-encoding"}
    return {key: value for key, value in headers.items() if key.lower() not in blocked}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() in {"content-length", "transfer-encoding", "connection", "content-encoding"}:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self) -> None:
        started = datetime.now(timezone.utc)
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else b""
        url = f"{UPSTREAM_URL}{self.path}"
        headers = filtered_headers(self.headers)
        req = urllib.request.Request(url, data=body if self.command not in {"GET", "HEAD"} else None, headers=headers, method=self.command)
        response_headers: dict[str, str] = {}
        status = 502
        response_body = b""
        error = ""
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                status = getattr(response, "status", 200)
                response_headers = dict(response.headers.items())
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_headers = dict(exc.headers.items())
            response_body = exc.read()
            error = response_body.decode("utf-8", errors="replace")[:600]
        except Exception as exc:
            response_headers = {"Content-Type": "application/json"}
            response_body = json.dumps({"error": str(exc)}).encode("utf-8")
            error = str(exc)

        ended = datetime.now(timezone.utc)
        header_lower = {key.lower(): value for key, value in response_headers.items()}
        if self.path.startswith("/v1/"):
            depth = safe_int(header_lower.get("x-litellm-attempted-fallbacks"))
            event = {
                "ts": now_iso(),
                "eventType": "client_request_success" if 200 <= status < 400 else "client_request_failure",
                "callId": safe_str(header_lower.get("x-litellm-call-id"), 160),
                "requestedModel": request_model(body),
                "originalModelGroup": request_model(body),
                "targetModelGroup": safe_str(header_lower.get("x-litellm-model-group"), 200),
                "finalModelGroup": safe_str(header_lower.get("x-litellm-model-group"), 200),
                "depth": depth,
                "maxFallbacks": safe_int(header_lower.get("x-litellm-max-fallbacks")),
                "success": 200 <= status < 400,
                "exceptionType": "" if 200 <= status < 400 else f"HTTP{status}",
                "error": error,
                "durationMs": round((ended - started).total_seconds() * 1000, 3),
                "modelId": safe_str(header_lower.get("x-litellm-model-id"), 200),
                "route": self.path,
                "statusCode": status,
            }
            try:
                write_event(event)
            except Exception as exc:
                print(f"failover-stats-proxy event write failed: {exc}", flush=True)
        self._send(status, response_headers, response_body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            body = json.dumps({"ok": True, "upstream": UPSTREAM_URL, "generatedAt": now_iso()}).encode("utf-8")
            self._send(200, {"Content-Type": "application/json"}, body)
            return
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    print(f"failover-stats-proxy listening on http://{args.host}:{args.port} -> {UPSTREAM_URL}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
