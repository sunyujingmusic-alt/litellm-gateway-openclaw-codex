#!/bin/zsh
set -euo pipefail

ROOT=/Users/sunyujing/litellm-gateway
cd "$ROOT"

python3 scripts/sync_codex_oauth_test_env.py "$@"
docker compose up -d

echo 'LiteLLM OAuth env synced from OpenClaw auth profile and production container reloaded.'
