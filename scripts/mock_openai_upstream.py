#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


MOCK_NAME = os.environ.get("MOCK_UPSTREAM_NAME", "mock")
STATE_FILE = Path(os.environ.get("MOCK_UPSTREAM_STATE_FILE", "/app/state/mock.status"))
DEFAULT_STATUS = int(os.environ.get("MOCK_UPSTREAM_DEFAULT_STATUS", "200"))


def read_status() -> int:
    try:
        raw = STATE_FILE.read_text(encoding="utf-8").strip()
        return int(raw or DEFAULT_STATUS)
    except FileNotFoundError:
        return DEFAULT_STATUS
    except Exception:
        return 500


def response_payload() -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{MOCK_NAME}",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-5.5",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": f"ok:{MOCK_NAME}"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def error_payload(status_code: int) -> dict[str, Any]:
    return {
        "error": {
            "message": f"{MOCK_NAME} forced status {status_code}",
            "type": "mock_upstream_error",
            "code": str(status_code),
        }
    }


def write_status(status_code: int) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(f"{status_code}\n", encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in {"/healthz", "/v1/healthz"}:
            self._send_json({"ok": True, "name": MOCK_NAME, "status": read_status()})
            return
        if self.path == "/v1/models":
            self._send_json({"object": "list", "data": [{"id": "gpt-5.5", "object": "model", "owned_by": MOCK_NAME}]})
            return
        self._send_json({"ok": False, "error": "not_found", "path": self.path}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        if self.path == "/mock/status":
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
                status_code = int(parsed.get("status", DEFAULT_STATUS))
            except Exception:
                status_code = DEFAULT_STATUS
            write_status(status_code)
            self._send_json({"ok": True, "name": MOCK_NAME, "status": status_code})
            return
        status_code = read_status()
        if status_code >= 400:
            self._send_json(error_payload(status_code), status_code)
            return
        if self.path in {"/v1/chat/completions", "/chat/completions"}:
            self._send_json(response_payload())
            return
        self._send_json({"ok": False, "error": "not_found", "path": self.path}, 404)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
