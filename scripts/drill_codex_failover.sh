#!/bin/zsh
set -euo pipefail

ROOT=/Users/sunyujing/litellm-gateway
BASE_ENV="$ROOT/.env.codex-oauth-gmn.test"
BASE_CONFIG="$ROOT/litellm/config.codex-oauth-gmn.test.yaml"
MASTER_KEY=$(awk -F= '$1=="GATEWAY_API_KEY"{print $2}' "$BASE_ENV")
MODE=${1:-all}
TMPDIR=$(mktemp -d)

cleanup() {
  docker rm -f litellm-router-drill-fallback-oauth litellm-router-drill-fallback-gmn >/dev/null 2>&1 || true
  rm -rf "$TMPDIR"
}
trap cleanup EXIT

write_env() {
  local outfile="$1"
  local disable_oauth="$2"
  python3 - <<'PY' "$BASE_ENV" "$outfile" "$disable_oauth"
from pathlib import Path
import sys
base_env = Path(sys.argv[1])
outfile = Path(sys.argv[2])
disable_oauth = sys.argv[3] == '1'
lines = base_env.read_text(encoding='utf-8').splitlines()
kv = {}
for line in lines:
    if not line or line.lstrip().startswith('#') or '=' not in line:
        continue
    k,v = line.split('=',1)
    kv[k]=v
kv['CCODEX_UPSTREAM_BASE_URL']='http://127.0.0.1:9/v1'
kv['CCODEX_UPSTREAM_API_KEY']='invalid-ccodex'
if disable_oauth:
    kv['OAUTH_UPSTREAM_BASE_URL']='http://127.0.0.1:9/v1'
    kv['OAUTH_UPSTREAM_API_KEY']='invalid-oauth'
order = [
  'CCODEX_UPSTREAM_BASE_URL','CCODEX_UPSTREAM_API_KEY',
  'GMN_UPSTREAM_BASE_URL','GMN_UPSTREAM_API_KEY',
  'OAUTH_UPSTREAM_BASE_URL','OAUTH_UPSTREAM_API_KEY','OAUTH_UPSTREAM_EXPIRES',
  'OAUTH_UPSTREAM_ACCOUNT_ID','OAUTH_UPSTREAM_EMAIL','OAUTH_UPSTREAM_PLAN_TYPE',
  'GATEWAY_API_KEY','TEST_REDIS_URL'
]
outfile.write_text('\n'.join(f'{k}={kv[k]}' for k in order if k in kv)+'\n', encoding='utf-8')
PY
}

run_case() {
  local case_name="$1"
  local disable_oauth="$2"
  local port="$3"
  local cname="$4"
  local expected_base="$5"
  local envfile="$TMPDIR/${case_name}.env"
  local headers="$TMPDIR/${case_name}.headers.txt"
  local body="$TMPDIR/${case_name}.body.txt"

  write_env "$envfile" "$disable_oauth"
  docker rm -f "$cname" >/dev/null 2>&1 || true
  docker run -d \
    --name "$cname" \
    --env-file "$envfile" \
    -e HTTP_PROXY= -e HTTPS_PROXY= -e NO_PROXY= -e ALL_PROXY= -e http_proxy= -e https_proxy= -e no_proxy= -e all_proxy= \
    -p "127.0.0.1:${port}:4000" \
    -v "$BASE_CONFIG:/app/config.yaml:ro" \
    ghcr.io/berriai/litellm:main-stable \
      --config /app/config.yaml --host 0.0.0.0 --port 4000 >/dev/null

  for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${port}/health/liveliness" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  curl -sS -N -D "$headers" "http://127.0.0.1:${port}/v1/responses" \
    -H "Authorization: Bearer $MASTER_KEY" \
    -H 'Content-Type: application/json' \
    -d '{"model":"gpt-5.4-router-test","instructions":"Reply with exactly: ok","input":[{"role":"user","content":[{"type":"input_text","text":"ok"}]}],"store":false,"stream":true}' > "$body"

  local status_line api_base preview
  status_line=$(sed -n '1p' "$headers" | tr -d '\r')
  api_base=$(grep -i '^x-litellm-model-api-base:' "$headers" | sed 's/\r$//' | cut -d' ' -f2-)
  preview=$(tail -n 8 "$body" | tr '\n' ' ' | sed 's/  */ /g')

  echo "case=$case_name"
  echo "status_line=$status_line"
  echo "api_base=$api_base"
  echo "expected_base=$expected_base"
  echo "body_preview=${preview:0:300}"

  if [[ "$api_base" != "$expected_base" ]]; then
    echo "FAIL: expected $expected_base but got $api_base" >&2
    return 1
  fi
  echo "PASS: $case_name"
}

case "$MODE" in
  oauth)
    run_case oauth 0 4025 litellm-router-drill-fallback-oauth https://chatgpt.com/backend-api/codex
    ;;
  gmn)
    run_case gmn 1 4026 litellm-router-drill-fallback-gmn https://gmn.chuangzuoli.com/v1
    ;;
  all)
    run_case oauth 0 4025 litellm-router-drill-fallback-oauth https://chatgpt.com/backend-api/codex
    run_case gmn 1 4026 litellm-router-drill-fallback-gmn https://gmn.chuangzuoli.com/v1
    ;;
  *)
    echo "Usage: $0 [oauth|gmn|all]" >&2
    exit 2
    ;;
esac
