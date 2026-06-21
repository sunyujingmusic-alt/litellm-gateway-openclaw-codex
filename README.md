# Local LiteLLM Gateway

正式生产口径：`OpenClaw -> LiteLLM(127.0.0.1:4002) -> ccodex -> popcorn -> OpenAI OAuth Codex -> gmn`

## 当前职责

- OpenClaw 顶层只连本机 LiteLLM：`http://127.0.0.1:4002/v1`
- LiteLLM 内部负责四跳 failover / failback
- Redis 持久化 Router cooldown，避免容器重启后丢失熔断状态

## 关键文件

- `litellm/config.yaml`：生产四跳路由
- `docker-compose.yml`：生产容器编排（`litellm-router-prod` / `litellm-router-redis`）
- `.env`：生产上游凭据与当前 OAuth access token
- `.env.codex-oauth-gmn.test`：4018 测试路由专用环境变量
- `litellm/config.codex-oauth-gmn.test.yaml`：4018 测试路由配置
- `docker-compose.codex-oauth-gmn.test.yml`：4018 测试容器
- `scripts/sync_codex_oauth_test_env.py`：从 OpenClaw auth profile 同步当前 Chrome ChatGPT 账户对应的 OAuth token 到生产/测试 env（优先按 Chrome 当前账户匹配，而不是盲选最新 profile）
- `scripts/sync_litellm_from_openclaw_codex.sh`：同步 env 后直接重载生产 LiteLLM
- `scripts/login_and_sync_litellm_from_openclaw_codex.sh`：先走 OpenClaw Codex 浏览器登录，再同步并重载 LiteLLM
- `scripts/watch_openclaw_codex_profile_and_sync.py`：监测 Chrome 当前 ChatGPT 账户与 OpenClaw Codex profile 变化，自动同步 LiteLLM
- `~/Library/LaunchAgents/com.sunyujing.litellm-codex-profile-sync.plist`：每 120 秒自动检查并同步
- `~/Library/LaunchAgents/com.sunyujing.litellm-codex-status-api.plist`：常驻本地状态接口（`127.0.0.1:4010`），输出 Chrome → OpenClaw → LiteLLM 绑定状态与官方 Plus/Pro 配额

## 生产路由规则

- 主路：`gpt-5.4` -> `ccodex`
- 第一备用：`gpt-5.4-popcorn`
- 第二备用：`gpt-5.4-oauth`
- 第三备用：`gpt-5.4-gmn`
- Router 参数：`allowed_fails=2`、`cooldown_time=300`、`num_retries=0`
- Redis：生产 `redis://redis:6379`；测试 `redis://host.docker.internal:6380/1`

## 常用操作

### 1) 刷新 OAuth token 并同步 env

```bash
cd /Users/sunyujing/litellm-gateway
python3 scripts/sync_codex_oauth_test_env.py
```

如果要同步后直接让生产 LiteLLM 跟着切换：

```bash
cd /Users/sunyujing/litellm-gateway
./scripts/sync_litellm_from_openclaw_codex.sh
```

如果要把“登录 OpenAI Codex + 同步 LiteLLM”一把跑完：

```bash
cd /Users/sunyujing/litellm-gateway
./scripts/login_and_sync_litellm_from_openclaw_codex.sh
```

查看当前官方 Plus/Pro 额度（5 小时 / 7 天）：

```bash
cd /Users/sunyujing/litellm-gateway
python3 scripts/query_openclaw_codex_quota.py
```

## 自动同步

已接入 launchd watcher：

- Label: `com.sunyujing.litellm-codex-profile-sync`
- 频率：每 120 秒
- 行为：发现 OpenClaw 中最新 `openai-codex` profile 变化后，自动同步 `.env` 并 `docker compose up -d`

常用检查：

```bash
launchctl print gui/$(id -u)/com.sunyujing.litellm-codex-profile-sync | sed -n '1,120p'
sed -n '1,80p' /Users/sunyujing/litellm-gateway/logs/watch_openclaw_codex_profile_and_sync.out.log
sed -n '1,80p' /Users/sunyujing/litellm-gateway/logs/watch_openclaw_codex_profile_and_sync.err.log
```

### 2) 拉起/更新生产 LiteLLM

```bash
cd /Users/sunyujing/litellm-gateway
docker compose up -d
```

### 3) 常用检查

```bash
curl -sS http://127.0.0.1:4010/healthz
open http://127.0.0.1:4010/
curl -sS http://127.0.0.1:4010/summary.txt
curl -sS http://127.0.0.1:4010/summary
curl -sS http://127.0.0.1:4010/status | jq '.summary'
curl -sS http://127.0.0.1:4010/router-config
curl -sS http://127.0.0.1:4010/quota
curl -sS -m 5 http://127.0.0.1:4002/health/liveliness
curl -sS -m 5 http://127.0.0.1:4002/v1/models -H 'Authorization: Bearer local-litellm-gateway'
curl -sS -m 15 http://127.0.0.1:4002/v1/chat/completions \
  -H 'Authorization: Bearer local-litellm-gateway' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.4","messages":[{"role":"user","content":"ping"}]}'
```

## 说明

- 不要把 OAuth 提升到 OpenClaw 顶层备用；自动回切应由 LiteLLM Router 内部完成。
- `OAUTH_UPSTREAM_API_KEY` 是 access token，不是长期静态 key；重新登录 OpenAI Codex 后要重新同步。
- 生产配置整理进主文件后，4018 测试容器仍可保留做受控演练。

- `OAUTH_UPSTREAM_ACCOUNT_ID` / `OAUTH_UPSTREAM_EMAIL` / `OAUTH_UPSTREAM_PLAN_TYPE` 会随当前选中的官方 ChatGPT 账户一起同步，便于后续做配额观测与账号绑定。
- `scripts/query_openclaw_codex_quota.py` 参考了 `cc-switch` 的 `wham/usage` 查询思路：直接使用当前 OAuth access token + `ChatGPT-Account-Id` 读取官方 5 小时 / 7 天窗口。
- `gpt-5.4-oauth` / `gpt-5.4-router-test-oauth` 已显式启用 `drop_params: true` + `additional_drop_params: [metadata]`，用于修复 LiteLLM grouped fallback 到官方 Codex OAuth 时的 `Unsupported parameter: metadata`。


### 4) 状态接口说明

- `GET /healthz`：状态 API 自身 + LiteLLM 健康检查；若 LiteLLM 不健康返回 `503`
- `GET /`：WebUI 首页，可直接查看和调整生产 LiteLLM 的主模型/备用模型顺序，也可编辑单个中转站的名称、请求地址、API key；只有处于 `bypassed` 的中转站会显示“删除中转站”
- `GET /summary`：适合程序消费的摘要 JSON
- `GET /summary.txt`：适合人直接看的单行摘要
- `GET /status`：完整 JSON，包含：
  - Chrome 当前 ChatGPT 账户
  - OpenClaw 解析到的 `openai-codex` profile
  - LiteLLM 当前绑定的 OAuth 账户元数据
  - 官方 `five_hour` / `seven_day` 配额窗口
  - `shouldResyncLiteLLM` 一致性判断
- `GET /router-config`：当前生产 LiteLLM 的主模型与 fallback 顺序
- `POST /router-config`：写回 `litellm/config.yaml` 并自动重启 `litellm-router-prod`
- `POST /router-config/model`：更新单个中转站的名称、请求地址、API key，并自动重启 `litellm-router-prod`
- `POST /router-config/model/delete`：删除指定中转站的模型定义与对应环境变量，并自动重启 `litellm-router-prod`；active 中转站会被后端拒绝删除
- `GET /quota`：只返回官方配额窗口原始结果

默认地址：

```bash
http://127.0.0.1:4010/
```

### 5) 备份源码到 NAS

```bash
cd /Users/sunyujing/litellm-gateway
./scripts/backup_litellm_gateway_to_nas.sh
```

默认目标目录：

```bash
/Volumes/素材/TEMP/chu/codex余额查询/litellm-gateway-openclaw-codex/
```

默认不包含：`.env`、`.env.codex-oauth-gmn.test`、`logs/`、`tmp/`、`.venv/`、`__pycache__/`。

更完整说明见：`运维手册-OpenClaw-Codex-LiteLLM.md`

### 6) 运行 fallback drill

```bash
cd /Users/sunyujing/litellm-gateway
./scripts/drill_codex_failover.sh all
```

分别单测：

```bash
./scripts/drill_codex_failover.sh popcorn
./scripts/drill_codex_failover.sh oauth
./scripts/drill_codex_failover.sh gmn
```

### 7) 运行 Router Panel E2E

安装依赖：

```bash
cd /Users/sunyujing/litellm-gateway
npm install
npx playwright install
```

默认会连接本机测试副本 `http://127.0.0.1:4110`，并使用 `/Users/sunyujing/litellm-gateway-ui-test` 作为可回滚的测试数据目录；也可以通过环境变量覆盖：

```bash
cd /Users/sunyujing/litellm-gateway
LITELLM_UI_TEST_BASE_URL=http://127.0.0.1:4110 \
LITELLM_UI_TEST_ROOT=/Users/sunyujing/litellm-gateway-ui-test \
npm run test:e2e
```
