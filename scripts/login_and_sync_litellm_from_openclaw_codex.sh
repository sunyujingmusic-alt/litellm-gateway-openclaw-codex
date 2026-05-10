#!/bin/zsh
set -euo pipefail

ROOT=/Users/sunyujing/litellm-gateway
cd "$ROOT"

python3 scripts/auto_login_openclaw_codex_via_chrome.py

echo 'OpenClaw Codex login via current Chrome session completed, LiteLLM env synced, production container reloaded.'
