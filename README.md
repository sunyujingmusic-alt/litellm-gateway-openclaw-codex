# LiteLLM Gateway Failover

An opinionated LiteLLM gateway setup for running one public model behind multiple upstream providers with automatic failover, Redis-backed cooldown state, and a lightweight web panel for routing control.

## What It Does

- Routes one public model name such as `gpt-5.4` to one or more upstream providers
- Uses Redis to persist router cooldown state across container restarts
- Supports primary and fallback upstream chains
- Exposes a small local WebUI to inspect, reorder, enable, disable, edit, add, and delete upstreams
- Lets you remove only bypassed upstreams; active upstreams are protected from deletion

## Why This Exists

Cheap relay providers are often unstable. This project is built to make them usable in practice:

- use a cheaper upstream as the default path
- keep one or more backup upstreams as failover targets
- switch quickly when the current upstream becomes unhealthy

The result is lower cost with much better uptime.

## Architecture

```text
Client -> LiteLLM Router -> Primary Upstream
                         -> Fallback Upstream A
                         -> Fallback Upstream B
```

Core components:

- `docker-compose.yml`: production LiteLLM + Redis
- `litellm/config.yaml`: router model and fallback chain
- `scripts/openclaw_codex_status_api.py`: local status API and WebUI
- `tests/router-panel.spec.js`: browser E2E coverage for the panel workflow

## Features

- Multi-upstream failover for a single public model
- Redis-backed `allowed_fails` and `cooldown_time` behavior
- WebUI for:
  - viewing active and bypassed upstreams
  - drag-and-drop ordering
  - editing upstream name, base URL, and API key
  - adding new upstreams
  - deleting bypassed upstreams
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

## WebUI Behavior

- `active` upstreams participate in the current route
- `bypassed` upstreams stay registered but do not receive traffic
- delete is only available for `bypassed` upstreams
- deleting an upstream removes:
  - its `model_list` entry
  - fallback references to it
  - its related environment-variable keys

## API Surface

- `GET /healthz`
- `GET /status`
- `GET /summary`
- `GET /router-config`
- `POST /router-config`
- `POST /router-config/model`
- `POST /router-config/model/delete`

## Configuration Notes

- The public-facing model name is controlled in LiteLLM config
- Upstream API credentials are read from environment variables
- Redis is used for router state persistence
- The WebUI writes back to `litellm/config.yaml` and restarts the LiteLLM container

## Testing

Install test dependencies:

```bash
npm install
npx playwright install
```

Run E2E:

```bash
npm run test:e2e
```

By default the E2E suite expects a separate local test copy of the project. You can override paths with:

```bash
LITELLM_UI_TEST_BASE_URL=http://127.0.0.1:4110 \
LITELLM_UI_TEST_ROOT=/path/to/test-copy \
npm run test:e2e
```

## Security Notes

- Do not commit live `.env` files
- Do not commit provider API keys
- Do not expose the local WebUI directly to the public internet without additional access control

## Repository Scope

This public repository contains the reusable code only.

Local operational documents, machine-specific launch scripts, private infrastructure details, and secrets should stay outside the repository.
