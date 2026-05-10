# OpenClaw + LiteLLM + 官方 ChatGPT Plus/Codex 运维手册

更新时间：2026-05-10

## 1. 目标与当前口径

当前正式链路：

`OpenClaw -> LiteLLM(127.0.0.1:4002) -> ccodex -> OpenAI OAuth Codex -> gmn`

核心目标：

1. 跟随 **Google Chrome 当前 ChatGPT/Plus 账户**
2. 将该账户对应的 `openai-codex` OAuth profile 自动同步进 LiteLLM
3. 可观测 **5 小时窗口** 与 **7 天窗口** 剩余额度
4. 发生漂移时能快速判断：是 Chrome、OpenClaw、还是 LiteLLM 绑定出了问题

---

## 2. 关键目录与文件

项目根目录：

- `/Users/sunyujing/litellm-gateway`

关键文件：

- `litellm/config.yaml`：生产路由配置
- `docker-compose.yml`：生产容器编排
- `.env`：生产环境变量（**本机敏感文件，不进默认 NAS 备份**）
- `.env.codex-oauth-gmn.test`：测试环境变量（**本机敏感文件，不进默认 NAS 备份**）
- `.env.example`：非敏感示例配置
- `scripts/get_chrome_chatgpt_account.js`：从 Chrome 多 Profile 中识别当前 ChatGPT 账户
- `scripts/sync_codex_oauth_test_env.py`：将 OpenClaw `openai-codex` profile 同步到 LiteLLM env
- `scripts/watch_openclaw_codex_profile_and_sync.py`：定时 watcher，同步账户变化
- `scripts/query_openclaw_codex_quota.py`：查询官方 `wham/usage`（5h / 7d）
- `scripts/openclaw_codex_status_api.py`：本地状态接口
- `scripts/backup_litellm_gateway_to_nas.sh`：源码备份到 NAS

OpenClaw 凭据：

- `/Users/sunyujing/.openclaw/agents/main/agent/auth-profiles.json`
- `/Users/sunyujing/.openclaw/agents/main/agent/auth-state.json`

launchd：

- `~/Library/LaunchAgents/com.sunyujing.litellm-codex-profile-sync.plist`
- `~/Library/LaunchAgents/com.sunyujing.litellm-codex-status-api.plist`

---

## 3. 端口与服务

### LiteLLM

- 地址：`http://127.0.0.1:4002`
- 健康检查：`http://127.0.0.1:4002/health/liveliness`

### 状态接口

- 地址：`http://127.0.0.1:4010`
- `GET /healthz`
- `GET /status`
- `GET /summary`
- `GET /summary.txt`
- `GET /quota`

### Redis

- 本机映射：`127.0.0.1:6380`
- 容器内部：`redis://redis:6379`

---

## 4. 当前自动化逻辑

### 4.1 Chrome 当前账户识别

`get_chrome_chatgpt_account.js` 会：

1. 枚举 Chrome profiles
2. 读取各自的 ChatGPT 登录 cookie
3. 按以下优先级判断“当前 profile”
   - `profile.last_used`
   - `profile.last_active_profiles`
   - 只有一个有效 ChatGPT profile
   - cookie 最近更新时间最新

输出关键字段：

- `selectedProfileId`
- `selectedProfileName`
- `email`
- `name`
- `selectionSource`

### 4.2 OpenClaw profile 选择

`scripts/sync_codex_oauth_test_env.py` 当前选择优先级：

1. 显式 `--profile-id`
2. 当前 Chrome ChatGPT 邮箱匹配
3. `auth-state lastGood`
4. 调试 fallback / 最新候选

说明：

- `tmp/openai_plus_account_extracted.json` 不再是生产主真相
- 默认不再盲选“最新 profile”

### 4.3 LiteLLM env 同步

同步后会写入：

- `OAUTH_UPSTREAM_API_KEY`
- `OAUTH_UPSTREAM_EXPIRES`
- `OAUTH_UPSTREAM_ACCOUNT_ID`
- `OAUTH_UPSTREAM_EMAIL`
- `OAUTH_UPSTREAM_PLAN_TYPE`

说明：

- `OAUTH_UPSTREAM_API_KEY` 是 **access token**，不是长期静态 key
- 重新登录 OpenAI Codex 后必须重新同步

---

## 5. 日常运维命令

### 5.1 看当前摘要（最常用）

```bash
cd /Users/sunyujing/litellm-gateway
python3 scripts/openclaw_codex_status_api.py --summary
```

或：

```bash
curl -sS http://127.0.0.1:4010/summary.txt
```

### 5.2 看完整状态

```bash
curl -sS http://127.0.0.1:4010/status
```

### 5.3 看官方配额

```bash
curl -sS http://127.0.0.1:4010/quota
```

或：

```bash
cd /Users/sunyujing/litellm-gateway
python3 scripts/query_openclaw_codex_quota.py
```

### 5.4 强制同步当前 Chrome 账户到 LiteLLM

```bash
cd /Users/sunyujing/litellm-gateway
./scripts/sync_litellm_from_openclaw_codex.sh
```

### 5.5 登录 + 同步一把跑完

```bash
cd /Users/sunyujing/litellm-gateway
./scripts/login_and_sync_litellm_from_openclaw_codex.sh
```

### 5.6 拉起 / 重载 LiteLLM

```bash
cd /Users/sunyujing/litellm-gateway
docker compose up -d
```

### 5.7 检查 launchd 服务

```bash
launchctl print gui/$(id -u)/com.sunyujing.litellm-codex-profile-sync | sed -n '1,120p'
launchctl print gui/$(id -u)/com.sunyujing.litellm-codex-status-api | sed -n '1,120p'
```

---

## 6. 漂移 / 故障排查顺序

### 情况 A：`/summary.txt` 里 `resync=yes`

说明：

- OpenClaw 当前解析出的 OAuth profile 与 LiteLLM `.env` 已不一致

处理：

```bash
cd /Users/sunyujing/litellm-gateway
./scripts/sync_litellm_from_openclaw_codex.sh
```

然后复查：

```bash
curl -sS http://127.0.0.1:4010/summary.txt
```

### 情况 B：Chrome 账户识别为空

先查：

```bash
cd /Users/sunyujing/litellm-gateway
node scripts/get_chrome_chatgpt_account.js
```

常见原因：

1. Chrome 当前未登录 ChatGPT
2. ChatGPT cookie 失效
3. launchd 环境里 PATH 太干净，找不到 `node`

当前已做的修复：

- `sync_codex_oauth_test_env.py` 已显式解析 `node` 路径（优先 `shutil.which('node')`，fallback `/opt/homebrew/bin/node` / `/usr/local/bin/node`）

### 情况 C：`/quota` 失败

优先判断：

1. OAuth access token 过期 / 无效
2. `ChatGPT-Account-Id` 不匹配
3. 官方接口临时失败

处理：

- 重新登录 OpenAI Codex
- 再执行同步

```bash
cd /Users/sunyujing/litellm-gateway
./scripts/login_and_sync_litellm_from_openclaw_codex.sh
```

### 情况 D：LiteLLM 不健康

先查：

```bash
curl -sS -m 5 http://127.0.0.1:4002/health/liveliness
```

再查容器：

```bash
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
```

重拉：

```bash
cd /Users/sunyujing/litellm-gateway
docker compose up -d
```

---

## 7. 配额解释

当前读取官方接口：

- `https://chatgpt.com/backend-api/wham/usage`

窗口映射：

- `18000` 秒 -> `five_hour`
- `604800` 秒 -> `seven_day`

字段含义：

- `usedPercent`：已用百分比
- `remainingPercent`：剩余百分比（本地换算）
- `resetAtIso`：窗口重置时间（UTC ISO）

---

## 8. 备份与恢复

### 8.1 备份源码到 NAS（默认不含 secrets）

```bash
cd /Users/sunyujing/litellm-gateway
./scripts/backup_litellm_gateway_to_nas.sh
```

默认目标目录：

- `/Volumes/素材/TEMP/chu/codex余额查询/litellm-gateway-openclaw-codex/`

默认排除：

- `.env`
- `.env.codex-oauth-gmn.test`
- `logs/`
- `tmp/`
- `.venv/`
- `__pycache__/`

如果确实要做**含 secrets 的本地受控备份**：

```bash
./scripts/backup_litellm_gateway_to_nas.sh --include-secrets
```

> 不建议默认这么做，除非明确知道 NAS 侧访问边界。

### 8.2 恢复思路

恢复源码：

1. 从 NAS 取回 `litellm-gateway-source-*.tar.gz`
2. 解压到目标目录
3. 手动补本机 `.env` / `.env.codex-oauth-gmn.test`
4. 检查 launchd plist 是否在本机存在
5. 执行：

```bash
cd /Users/sunyujing/litellm-gateway
docker compose up -d
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sunyujing.litellm-codex-profile-sync.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sunyujing.litellm-codex-status-api.plist
```

---

## 9. 当前运维判断原则

1. **Chrome 当前账户** 是上游真实意图
2. **OpenClaw OAuth profile** 是中间凭据层
3. **LiteLLM `.env`** 是实际流量绑定层
4. **`/status` 与 `/summary.txt`** 是最快的事实检查入口
5. 默认备份源码与文档，**不默认把 live token 扔进 NAS**

---

## 10. 建议的最小巡检清单

每天或每次切账号后，至少看一次：

```bash
curl -sS http://127.0.0.1:4010/summary.txt
```

预期关键点：

- `account=...` 正确
- `litellm=healthy`
- `resync=no`
- `five_hour[...]` / `seven_day[...]` 能正常返回

如果这 4 个都对，说明当前整条链路基本正常。
