#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export LITELLM_ENV_PATH="${LITELLM_ENV_PATH:-$ROOT/.env}"
export LITELLM_CONFIG_PATH="${LITELLM_CONFIG_PATH:-$ROOT/litellm/config.yaml}"
export LITELLM_COMPOSE_PATH="${LITELLM_COMPOSE_PATH:-$ROOT/docker-compose.yml}"
export LITELLM_HEALTH_URL="${LITELLM_HEALTH_URL:-http://127.0.0.1:4004/health/liveliness}"
export FAILOVER_STATS_URL="${FAILOVER_STATS_URL:-http://127.0.0.1:4151/failover-stats}"
export LITELLM_REDIS_CONTAINER="${LITELLM_REDIS_CONTAINER:-litellm-structured-copy-redis}"
export LITELLM_RESTART_CONTAINER="${LITELLM_RESTART_CONTAINER:-litellm-structured-copy}"
export LITELLM_DEFAULT_ENTRY_MODEL="${LITELLM_DEFAULT_ENTRY_MODEL:-gpt-5.4}"
export LITELLM_PANEL_PORT="${LITELLM_PANEL_PORT:-4110}"
if [[ -z "${LITELLM_PANEL_CHAINS:-}" ]]; then
  export LITELLM_PANEL_CHAINS='[{"id":"gpt-5.5","label":"Test copy gpt-5.5","owner":"Structured logging test","entryModel":"gpt-5.5","statusKey":"gateway:health:gpt-5.5","cooldownKey":"deployment:gpt-5.5:cooldown"},{"id":"gpt-5.4","label":"Test copy gpt-5.4","owner":"Structured logging test","entryModel":"gpt-5.4","statusKey":"gateway:health:gpt-5.4","cooldownKey":"deployment:gpt-5.4:cooldown"}]'
fi

exec python3 scripts/openclaw_codex_status_api.py --serve --host 127.0.0.1 --port "$LITELLM_PANEL_PORT"
