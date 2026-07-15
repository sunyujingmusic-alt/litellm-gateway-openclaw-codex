# LiteLLM Gateway Failover

An opinionated LiteLLM gateway setup for running one public model behind multiple upstream providers with automatic failover, Redis-backed cooldown state, and a lightweight web panel for routing control.

## What It Does

- Routes public model names such as Codex `gpt-5.5` and OpenClaw `gpt-5.4` to their own upstream fallback chains
- Uses Redis to persist router cooldown state across container restarts
- Supports primary and fallback upstream chains
- Exposes a small local WebUI to inspect, reorder, enable, disable, edit, add, and delete upstreams
- Exposes a read-only failover statistics dashboard backed by SQLite and JSONL event logs
- Lets you remove only bypassed upstreams; active upstreams are protected from deletion

## Why This Exists

Cheap relay providers are often unstable. This project is built to make them usable in practice:

- use a cheaper upstream as the default path
- keep one or more backup upstreams as failover targets
- switch quickly when the current upstream becomes unhealthy

The result is lower cost with much better uptime.

## Why Redis Is Part of the Design

LiteLLM can retry or fall back within a single request, but reliable failover also needs memory across requests.

When an upstream hits the configured `allowed_fails` threshold, the router should put it into a `cooldown_time` window and stop sending new traffic to it for a while. If that state lived only in LiteLLM process memory, a container restart would immediately forget the cooldown and start sending traffic back to the unhealthy upstream too early.

Redis is included to store this short-lived router state:

- deployment cooldown and circuit-breaker state
- minute-level router state used for routing decisions
- shared state that can be reused if the gateway is later expanded to multiple LiteLLM instances

Just as important, Redis is not used here as a chat-content cache or as a primary application database. The source of truth for routing config stays in `litellm/config.yaml`, while secrets stay in environment variables.

One implementation detail matters: this behavior depends on `redis_url` being configured under `router_settings`. A generic LiteLLM cache setting is not enough to make router cooldown state Redis-backed.

## Architecture

```text
Client -> LiteLLM Router -> Primary Upstream
                         -> Fallback Upstream A
                         -> Fallback Upstream B
              \-> Failover callback -> SQLite/JSONL -> Stats dashboard
```

Core components:

- `docker-compose.yml`: production LiteLLM + Redis + failover stats dashboard
- `litellm/config.yaml`: router model and fallback chain
- `scripts/openclaw_codex_status_api.py`: local status API and WebUI
- `scripts/failover_stats_callback.py`: LiteLLM callback that records request and fallback events
- `scripts/failover_stats_api.py`: local read-only stats dashboard/API
- `tests/router-panel.spec.js`: browser E2E coverage for the panel workflow
- `tests/failover-stats.spec.js`: failover depth and stats aggregation regression test

## Features

- Multi-upstream failover for a single public model
- Redis-backed `allowed_fails` and `cooldown_time` behavior
- WebUI for:
  - viewing active and bypassed upstreams
  - drag-and-drop ordering
  - editing upstream name, base URL, and API key
  - adding new upstreams
  - deleting bypassed upstreams
- Failover stats for:
  - total requests
  - primary completions
  - backup requests
  - second-backup-or-deeper requests
  - unresolved failures
  - per-depth and per-model chain summaries
  - per-chain summaries when multiple public chains share the same gateway
- HTTP endpoints for automation-friendly config updates

## Quick Start

### 1. Prepare environment

Copy `.env.example` to `.env` and fill in your own upstream URLs and API keys.

### 2. Start services

```bash
docker compose up -d
```

### 3. Check health

```bash
curl -sS http://127.0.0.1:4002/health/liveliness
```

### 4. Start the local panel

```bash
python3 scripts/openclaw_codex_status_api.py
```

Open:

```text
http://127.0.0.1:4010/
```

The production failover stats dashboard is served by compose at:

```text
http://127.0.0.1:4129/
```

## WebUI Behavior

- `active` upstreams participate in the current route
- `bypassed` upstreams stay registered but do not receive traffic
- delete is only available for `bypassed` upstreams
- deleting an upstream removes:
  - its `model_list` entry
  - fallback references to it
  - its related environment-variable keys

## API Surface

Routing panel API:

- `GET /healthz`
- `GET /status`
- `GET /summary`
- `GET /router-config`
- `GET /failover-stats?window=5m|1h|24h|today|3d|7d|all`
- `POST /router-config`
- `POST /router-config/model`
- `POST /router-config/model/delete`

Failover stats API:

- `GET /healthz`
- `GET /failover-stats?window=5m|1h|24h|today|3d|7d|all`
- `GET /vendor/vue.global.prod.js`

`POST /admin/reset` exists only for isolated test runs and is disabled by default unless `FAILOVER_STATS_ALLOW_RESET=1` is set.

## Configuration Notes

- The public-facing model name is controlled in LiteLLM config
- Upstream API credentials are read from environment variables
- Redis is used for router state persistence
- `FAILOVER_STATS_CHAINS` labels multiple chains for the dashboard, for example Codex on `gpt-5.5` and OpenClaw on `gpt-5.4`
- Failover stats are stored under `logs/failover-stats-prod/`, which is intentionally ignored by git
- The WebUI writes back to `litellm/config.yaml` and restarts the LiteLLM container

## Testing

Install test dependencies:

```bash
npm install
npx playwright install
```

Run panel E2E:

```bash
npm run test:e2e
```

Run failover stats regression against the isolated mock stack:

```bash
docker compose -f docker-compose.gpt-5.4-router-test.yml up -d
npm run test:failover-stats
docker compose -f docker-compose.gpt-5.4-router-test.yml down
```

By default the panel E2E suite expects a separate local test copy of the project. You can override paths with:

```bash
LITELLM_UI_TEST_BASE_URL=http://127.0.0.1:4110 \
LITELLM_UI_TEST_ROOT=/path/to/test-copy \
npm run test:e2e
```

## Security Notes

- Do not commit live `.env` files
- Do not commit provider API keys
- Do not expose the local WebUI or failover stats dashboard directly to the public internet without additional access control
- Keep backup files, SQLite databases, JSONL event logs, test runtime state, and `.env.*` files out of git

## Repository Scope

This public repository contains the reusable code only.

Local operational documents, machine-specific launch scripts, private infrastructure details, and secrets should stay outside the repository.
