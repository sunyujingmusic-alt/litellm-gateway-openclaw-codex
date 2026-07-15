#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse
from typing import Any


REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
DB_PATH = Path(os.environ.get("FAILOVER_STATS_DB_PATH", "/app/failover-stats/failover_stats.sqlite3"))
JSONL_PATH = Path(os.environ.get("FAILOVER_STATS_JSONL_PATH", "/app/failover-stats/failover_events.jsonl"))
DEPLOYMENT_ID = os.environ["PROBE_DEPLOYMENT_ID"]
BASE_URL = os.environ["PROBE_BASE_URL"].rstrip("/")
API_KEY = os.environ.get("PROBE_API_KEY", "")
MODEL = os.environ.get("PROBE_MODEL", "openai/gpt-5.4")
INTERVAL_SECONDS = float(os.environ.get("PROBE_INTERVAL_SECONDS", "10"))
SUCCESS_THRESHOLD = int(os.environ.get("PROBE_SUCCESS_THRESHOLD", "2"))
TIMEOUT_SECONDS = float(os.environ.get("PROBE_TIMEOUT_SECONDS", "5"))
STATUS_KEY = os.environ.get("PROBE_STATUS_KEY", f"gateway:health:{DEPLOYMENT_ID}")
COOLDOWN_KEY = f"deployment:{DEPLOYMENT_ID}:cooldown"
RECOVERY_URL = os.environ.get("PROBE_RECOVERY_URL", "").strip()
RECOVERY_METHOD = os.environ.get("PROBE_RECOVERY_METHOD", "POST").strip().upper()
RECOVERY_API_KEY = os.environ.get("PROBE_RECOVERY_API_KEY", "")
RECOVERY_API_KEY_HEADER = os.environ.get("PROBE_RECOVERY_API_KEY_HEADER", "X-Probe-Admin-Key")
RECOVERY_COMMAND = os.environ.get("PROBE_RECOVERY_COMMAND", "").strip()
RECOVERY_CONTAINER = os.environ.get("PROBE_RECOVERY_CONTAINER", "").strip()
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")

_LOCK = threading.Lock()
SQLITE_RETRIES = 5


class UnixSocketHTTPConnection(HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def safe_str(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def duration_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 3)


def parse_json_payload(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
    event = {key: value for key, value in event.items() if value is not None}
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
                        "",
                        event.get("model", ""),
                        event.get("model", ""),
                        event.get("deploymentId", ""),
                        event.get("deploymentId", ""),
                        0,
                        0,
                        1 if event.get("success") else 0,
                        event.get("exceptionType", ""),
                        event.get("detail", ""),
                        safe_float(event.get("durationMs")),
                        event.get("deploymentId", ""),
                        raw,
                    ),
                )
                conn.commit()
            return
        except sqlite3.Error as exc:
            last_exc = exc
            time.sleep(0.05 * (attempt + 1))
    print(f"upstream-health-probe sqlite write skipped: {last_exc}", flush=True)


class RedisClient:
    def __init__(self, url: str) -> None:
        parsed = urlparse(url)
        self.host = parsed.hostname or "redis"
        self.port = parsed.port or 6379
        self.db = int((parsed.path or "/0").strip("/") or "0")

    def _command(self, *parts: str) -> Any:
        encoded_parts = []
        for part in parts:
            encoded = str(part).encode("utf-8")
            encoded_parts.append(b"$" + str(len(encoded)).encode("ascii") + b"\r\n" + encoded + b"\r\n")
        payload = b"*" + str(len(parts)).encode("ascii") + b"\r\n" + b"".join(encoded_parts)
        with socket.create_connection((self.host, self.port), timeout=1) as sock:
            sock.settimeout(1)
            sock.sendall(payload)
            return self._read_response(sock)

    def _read_line(self, sock: socket.socket) -> bytes:
        chunks = []
        while True:
            chunk = sock.recv(1)
            if not chunk:
                raise ConnectionError("redis connection closed")
            chunks.append(chunk)
            if len(chunks) >= 2 and chunks[-2:] == [b"\r", b"\n"]:
                return b"".join(chunks[:-2])

    def _read_response(self, sock: socket.socket) -> Any:
        prefix = sock.recv(1)
        if prefix == b"+":
            return self._read_line(sock).decode("utf-8", errors="replace")
        if prefix == b"-":
            raise RuntimeError(self._read_line(sock).decode("utf-8", errors="replace"))
        if prefix == b":":
            return int(self._read_line(sock))
        if prefix == b"$":
            length = int(self._read_line(sock))
            if length < 0:
                return None
            data = b""
            while len(data) < length:
                data += sock.recv(length - len(data))
            sock.recv(2)
            return data.decode("utf-8", errors="replace")
        if prefix == b"*":
            count = int(self._read_line(sock))
            return [self._read_response(sock) for _ in range(count)]
        raise RuntimeError(f"unknown redis response prefix: {prefix!r}")

    def _select(self) -> None:
        if self.db:
            self._command("SELECT", str(self.db))

    def exists(self, key: str) -> bool:
        self._select()
        return bool(self._command("EXISTS", key))

    def get(self, key: str) -> str | None:
        self._select()
        value = self._command("GET", key)
        return str(value) if value is not None else None

    def ttl(self, key: str) -> int:
        self._select()
        return int(self._command("TTL", key))

    def delete(self, key: str) -> None:
        self._select()
        self._command("DEL", key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._select()
        if ex is None:
            self._command("SET", key, value)
        else:
            self._command("SET", key, value, "EX", str(ex))


def redis_client() -> RedisClient:
    return RedisClient(REDIS_URL)


def cooldown_info(client: Any) -> dict[str, Any]:
    ttl = client.ttl(COOLDOWN_KEY)
    active = ttl != -2
    info: dict[str, Any] = {
        "active": active,
        "ttlSeconds": ttl if ttl > 0 else 0,
        "rawTtl": ttl,
        "cooldownAgeMs": 0,
    }
    if not active:
        return info

    parsed = parse_json_payload(client.get(COOLDOWN_KEY))
    timestamp = safe_float(parsed.get("timestamp"))
    if timestamp is not None:
        info["cooldownAgeMs"] = max(0, int((time.time() - timestamp) * 1000))
    else:
        cooldown_time = safe_int(parsed.get("cooldown_time"))
        if cooldown_time and ttl > 0:
            info["cooldownAgeMs"] = max(0, (cooldown_time - ttl) * 1000)
    return info


def base_probe_event(info: dict[str, Any], event_type: str, success: bool) -> dict[str, Any]:
    return {
        "ts": now_iso(),
        "eventType": event_type,
        "requestedModel": DEPLOYMENT_ID,
        "targetModelGroup": DEPLOYMENT_ID,
        "finalModelGroup": DEPLOYMENT_ID,
        "modelId": DEPLOYMENT_ID,
        "deploymentId": DEPLOYMENT_ID,
        "model": MODEL,
        "cooldownKey": COOLDOWN_KEY,
        "cooldownTtlSeconds": safe_int(info.get("ttlSeconds")),
        "cooldownAgeMs": safe_int(info.get("cooldownAgeMs")),
        "probeIntervalSeconds": INTERVAL_SECONDS,
        "probeTimeoutSeconds": TIMEOUT_SECONDS,
        "successThreshold": SUCCESS_THRESHOLD,
        "success": success,
    }


def write_cooldown_observed_event(info: dict[str, Any]) -> None:
    event = base_probe_event(info, "probe_cooldown_observed", True)
    write_event(event)


def write_status(client: Any, state: str, detail: str = "", consecutive_successes: int = 0) -> None:
    payload = {
        "ok": state == "healthy",
        "state": state,
        "detail": detail[:500],
        "checkedAt": now_iso(),
        "deploymentId": DEPLOYMENT_ID,
        "cooldownKey": COOLDOWN_KEY,
        "consecutiveSuccesses": consecutive_successes,
    }
    client.set(STATUS_KEY, json.dumps(payload, ensure_ascii=False), ex=3600)


def formatted_recovery_url() -> str:
    if not RECOVERY_URL:
        return ""
    return RECOVERY_URL.format(
        deployment_id=quote(DEPLOYMENT_ID, safe=""),
        cooldown_key=quote(COOLDOWN_KEY, safe=""),
    )


def event_safe_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def call_recovery_url() -> tuple[str, int, str]:
    url = formatted_recovery_url()
    if not url:
        return "", 0, ""

    headers = {"Content-Type": "application/json"}
    if RECOVERY_API_KEY:
        headers[RECOVERY_API_KEY_HEADER] = RECOVERY_API_KEY

    req = urllib.request.Request(
        url,
        data=json.dumps({"deployment_id": DEPLOYMENT_ID}).encode("utf-8"),
        headers=headers,
        method=RECOVERY_METHOD,
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        status = getattr(resp, "status", 200)
        resp.read(300)
    if not (200 <= status < 300):
        raise RuntimeError(f"recovery endpoint failed ({status})")
    return f"{RECOVERY_METHOD} {event_safe_url(url)} HTTP {status}", status, event_safe_url(url)


def run_recovery_command() -> tuple[str, int]:
    if RECOVERY_CONTAINER:
        conn = UnixSocketHTTPConnection(DOCKER_SOCKET)
        path = f"/containers/{quote(RECOVERY_CONTAINER, safe='')}/restart?t=10"
        conn.request("POST", path)
        response = conn.getresponse()
        body = response.read().decode("utf-8", errors="replace")
        conn.close()
        if response.status not in {204, 304}:
            raise RuntimeError(f"docker restart failed ({response.status}): {body}")
        return f"restarted {RECOVERY_CONTAINER}", response.status
    if not RECOVERY_COMMAND:
        return "", 0
    completed = subprocess.run(
        RECOVERY_COMMAND,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        raise RuntimeError(f"recovery command failed ({completed.returncode}): {output}")
    return output, 0


def clear_cooldown(client: Any) -> tuple[str, str, int, str, float]:
    started = time.monotonic()
    if RECOVERY_URL:
        detail, status, recovery_url = call_recovery_url()
        return "recovery_url", detail, status, recovery_url, duration_ms(started)
    client.delete(COOLDOWN_KEY)
    detail, status = run_recovery_command()
    method = "redis_delete"
    if RECOVERY_CONTAINER:
        method = "redis_delete_and_container_restart"
    elif RECOVERY_COMMAND:
        method = "redis_delete_and_command"
    return method, detail, status, "", duration_ms(started)


def cooldown_active(client: Any) -> bool:
    return bool(client.exists(COOLDOWN_KEY))


def probe_once() -> tuple[bool, str, int, float]:
    started = time.monotonic()
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
        "stream": False,
    }
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            status = getattr(resp, "status", 200)
            resp.read(300)
        if 200 <= status < 400:
            return True, f"POST /chat/completions HTTP {status}", status, duration_ms(started)
        return False, f"POST /chat/completions HTTP {status}", status, duration_ms(started)
    except urllib.error.HTTPError as exc:
        exc.read(300)
        return False, f"POST /chat/completions HTTP {exc.code}", exc.code, duration_ms(started)
    except Exception as exc:
        return False, safe_str(exc, 300), 0, duration_ms(started)


def write_probe_event(info: dict[str, Any], ok: bool, detail: str, status: int, probe_duration_ms: float, consecutive_successes: int) -> None:
    event = base_probe_event(info, "probe_success" if ok else "probe_failure", ok)
    event.update(
        {
            "durationMs": probe_duration_ms,
            "status": status,
            "detail": safe_str(detail),
            "consecutiveSuccesses": consecutive_successes,
        }
    )
    write_event(event)


def write_clear_event(
    info: dict[str, Any],
    event_type: str,
    success: bool,
    consecutive_successes: int,
    clear_method: str,
    recovery_url: str,
    status: int,
    clear_duration_ms: float,
    detail: str,
    exception: Exception | None = None,
) -> None:
    event = base_probe_event(info, event_type, success)
    event.update(
        {
            "consecutiveSuccesses": consecutive_successes,
            "clearMethod": clear_method,
            "recoveryUrl": recovery_url,
            "status": status,
            "durationMs": clear_duration_ms,
            "detail": safe_str(detail),
        }
    )
    if exception is not None:
        event["exceptionType"] = exception.__class__.__name__
    write_event(event)


def main() -> None:
    client = redis_client()
    consecutive_successes = 0
    observed_active_cooldown = False
    print(
        f"primary probe watching {COOLDOWN_KEY}; target={BASE_URL}; interval={INTERVAL_SECONDS}s",
        flush=True,
    )
    while True:
        try:
            info = cooldown_info(client)
            if not info["active"]:
                consecutive_successes = 0
                observed_active_cooldown = False
                write_status(client, "idle", "cooldown key is absent")
                time.sleep(INTERVAL_SECONDS)
                continue

            if not observed_active_cooldown:
                write_cooldown_observed_event(info)
                observed_active_cooldown = True

            ok, detail, status, probe_duration = probe_once()
            if ok:
                consecutive_successes += 1
                write_probe_event(info, True, detail, status, probe_duration, consecutive_successes)
                if consecutive_successes >= SUCCESS_THRESHOLD:
                    try:
                        clear_method, recovery_detail, clear_status, recovery_url, clear_duration = clear_cooldown(client)
                    except Exception as clear_exc:
                        write_clear_event(
                            info,
                            "cooldown_clear_failure",
                            False,
                            consecutive_successes,
                            "recovery_url" if RECOVERY_URL else "redis_delete",
                            event_safe_url(formatted_recovery_url()) if RECOVERY_URL else "",
                            0,
                            0,
                            safe_str(clear_exc),
                            clear_exc,
                        )
                        write_status(client, "unhealthy", f"{detail}; cooldown clear failed: {safe_str(clear_exc, 160)}", consecutive_successes)
                        print(f"{now_iso()} failed to clear {COOLDOWN_KEY}: {clear_exc}", flush=True)
                        consecutive_successes = 0
                        time.sleep(INTERVAL_SECONDS)
                        continue

                    write_clear_event(
                        info,
                        "cooldown_clear_success",
                        True,
                        consecutive_successes,
                        clear_method,
                        recovery_url,
                        clear_status,
                        clear_duration,
                        recovery_detail or "cooldown cleared",
                    )
                    suffix = "; cooldown cleared"
                    if recovery_detail:
                        suffix += f"; recovery command ok: {recovery_detail[:160]}"
                    write_status(client, "healthy", f"{detail}{suffix}", consecutive_successes)
                    print(f"{now_iso()} cleared {COOLDOWN_KEY}: {detail}{suffix}", flush=True)
                    consecutive_successes = 0
                    observed_active_cooldown = False
                else:
                    write_status(client, "probing", detail, consecutive_successes)
            else:
                consecutive_successes = 0
                write_probe_event(info, False, detail, status, probe_duration, consecutive_successes)
                write_status(client, "unhealthy", detail)
                print(f"{now_iso()} probe failed while cooldown active: {detail}", flush=True)
        except Exception as exc:
            consecutive_successes = 0
            print(f"{now_iso()} probe loop error: {exc}", flush=True)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
