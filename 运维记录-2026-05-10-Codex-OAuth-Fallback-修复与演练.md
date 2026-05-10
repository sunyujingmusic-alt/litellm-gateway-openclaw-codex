# 运维记录：Codex OAuth Fallback `metadata` 兼容修复与故障演练

日期：2026-05-10  
项目：`litellm-gateway-openclaw-codex`

---

## 1. 背景

目标链路是：

`ccodex (主路) -> OpenAI Codex OAuth (第一备用) -> gmn (第二备用)`

此前虽然配置层已经写成上述顺序，但实际故障演练发现：

- `gmn` 第二备用可以接住
- `oauth` 第一备用在 LiteLLM grouped `/v1/responses` fallback 路径下会失败
- 容器日志关键报错为：

```text
{"detail":"Unsupported parameter: metadata"}
```

这说明不是 OAuth token 失效，也不是上游彻底不可用，而是 **LiteLLM fallback 到 Codex OAuth 时把不被上游接受的 `metadata` 传了过去**。

---

## 2. 参考思路

本次处理参考了两类线索：

### 2.1 CC Switch 的实现思路

CC Switch 对 Codex OAuth / OpenAI Responses 的处理核心不是“全量透传”，而是：

- 使用更接近 Codex 真正支持的请求形态
- 对 `store / stream / input 结构` 等做更严格约束
- 尽量避免把通用 OpenAI / 代理层附加字段直接透给 Codex 上游

### 2.2 LiteLLM 官方资料与 issue

查到 LiteLLM 官方文档与 issue 已明确讨论过类似问题：

- `drop_params: true`
- `additional_drop_params`
- ChatGPT Codex 上游会拒绝某些不支持参数，如：
  - `metadata`
  - `user`
  - `temperature`
  - `context_management`

---

## 3. 实施改动

### 3.1 在 OAuth deployment 上显式丢弃 `metadata`

修改文件：

- `litellm/config.yaml`
- `litellm/config.codex-oauth-gmn.test.yaml`

新增内容：

```yaml
litellm_params:
  model: openai/gpt-5.4
  api_base: os.environ/OAUTH_UPSTREAM_BASE_URL
  api_key: os.environ/OAUTH_UPSTREAM_API_KEY
  drop_params: true
  additional_drop_params:
    - metadata
```

说明：

- `drop_params: true` 开启通用不支持字段丢弃
- `additional_drop_params: [metadata]` 明确把这次演练中已知的 blocker 字段直接剔掉

---

## 4. 验证结果

### 4.1 生产 `gpt-5.4-oauth` 单模型 smoke

请求口径：

- `/v1/responses`
- `instructions`
- `input` 为 list
- `store=false`
- `stream=true`

结果：成功，返回 `ok`

关键响应头：

```text
x-litellm-model-api-base: https://chatgpt.com/backend-api/codex
```

结论：**OAuth 单模型本身可用。**

---

### 4.2 受控 grouped fallback drill：primary 故障后由 OAuth 接住

演练方式：

- 故意打坏 primary `ccodex`
- 保持 `oauth` 可用
- 发送 grouped request 到 `gpt-5.4-router-test`

结果：成功，返回 `ok`

关键响应头：

```text
x-litellm-model-api-base: https://chatgpt.com/backend-api/codex
```

结论：**修复后，第一备用 OAuth 已能在真实 fallback 路径中接住请求。**

---

### 4.3 第二备用 gmn 继续可用

演练方式：

- 故意打坏 primary `ccodex`
- 再故意打坏 `oauth`
- 保持 `gmn` 可用

结果：成功，返回 `ok`

关键响应头：

```text
x-litellm-model-api-base: https://gmn.chuangzuoli.com/v1
```

结论：**第二备用 gmn 继续有效。**

---

## 5. 新增可重复演练脚本

新增文件：

- `scripts/drill_codex_failover.sh`

用途：重复验证当前三层链路的 fallback 逻辑。

### 用法

只测 OAuth 第一备用：

```bash
cd /Users/sunyujing/litellm-gateway
./scripts/drill_codex_failover.sh oauth
```

只测 gmn 第二备用：

```bash
./scripts/drill_codex_failover.sh gmn
```

两项都测：

```bash
./scripts/drill_codex_failover.sh all
```

### 判定标准

- `oauth` 演练应命中：
  - `https://chatgpt.com/backend-api/codex`
- `gmn` 演练应命中：
  - `https://gmn.chuangzuoli.com/v1`

---

## 6. 当前正式口径（修复后）

现在可以把运行口径更新为：

1. **默认主路**：`ccodex`
2. **第一备用**：`OpenAI Codex OAuth`（已验证真实 fallback 可接）
3. **第二备用**：`gmn`（已验证真实 fallback 可接）

这意味着：

- 配置层顺序正确
- 运行层也已与配置层对齐

---

## 7. 同步动作

本次修复完成后已执行：

1. 生产 LiteLLM 容器重建 / 重启
2. 测试 LiteLLM 容器重建 / 重启
3. NAS 再备份
4. GitHub 再推送

---

## 8. 后续建议

后续若继续增强稳定性，优先级建议：

1. 把 `drill_codex_failover.sh` 纳入定期巡检
2. 继续观察 Codex 上游是否还会拒绝其他字段（如 `user`、`temperature` 等）
3. 若再出现新的不兼容字段，优先在 OAuth deployment 层做**最小显式剔除**，避免影响其他上游

---

## 9. 本次结论

本次问题已经从：

> “配置上 oauth 是第一备用，但真实 fallback 会被 `metadata` 挡住”

修正为：

> “OAuth 第一备用已在真实 grouped fallback 路径中成功接住请求，整条 `ccodex -> oauth -> gmn` 链路已可按预期工作。”
