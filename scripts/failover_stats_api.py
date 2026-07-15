#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DB_PATH = Path(os.environ.get("FAILOVER_STATS_DB_PATH", "./logs/failover-stats/failover_stats.sqlite3"))
JSONL_PATH = Path(os.environ.get("FAILOVER_STATS_JSONL_PATH", "./logs/failover-stats/failover_events.jsonl"))
RESET_PATH = Path(os.environ.get("FAILOVER_STATS_RESET_PATH", str(DB_PATH.with_name("failover_reset.json"))))
PROMETHEUS_URL = os.environ.get("FAILOVER_STATS_PROMETHEUS_URL", "http://127.0.0.1:4028/metrics/")
CHAIN = [item.strip() for item in os.environ.get("FAILOVER_STATS_CHAIN", "").split(",") if item.strip()]
CHAIN_GROUPS_RAW = os.environ.get("FAILOVER_STATS_CHAINS", "")
VUE_BUNDLE_PATH = Path(os.environ.get("FAILOVER_STATS_VUE_BUNDLE", "./node_modules/vue/dist/vue.global.prod.js"))
DEFAULT_HOST = os.environ.get("FAILOVER_STATS_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("FAILOVER_STATS_PORT", "4129"))
SUBTITLE = os.environ.get("FAILOVER_STATS_SUBTITLE", "生产链路聚合看板，只读取 /failover-stats 的脱敏数据。")
ALLOW_RESET = os.environ.get("FAILOVER_STATS_ALLOW_RESET", "0").strip().lower() in {"1", "true", "yes", "on"}
LOCAL_TZ_OFFSET_HOURS = float(os.environ.get("FAILOVER_STATS_LOCAL_TZ_OFFSET_HOURS", "8"))
LOCAL_TZ = timezone(timedelta(hours=LOCAL_TZ_OFFSET_HOURS))


HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LiteLLM Failover Stats</title>
  <style>
    :root { --bg: #f6f7f9; --panel: #ffffff; --ink: #18212f; --muted: #647084; --line: #d9dee7; --ok: #16794f; --warn: #a35d00; --bad: #b42318; --info: #1d4ed8; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif; color: var(--ink); background: var(--bg); }
    main { width: min(1180px, calc(100vw - 32px)); margin: 20px auto 44px; }
    header { display: flex; justify-content: space-between; gap: 16px; align-items: flex-end; margin-bottom: 16px; }
    h1 { margin: 0; font-size: 28px; font-weight: 760; }
    h2 { margin: 0 0 12px; font-size: 16px; }
    .muted { color: var(--muted); font-size: 13px; }
    .toolbar { display: flex; gap: 8px; align-items: center; }
    button, select { border: 1px solid var(--line); background: #fff; color: var(--ink); border-radius: 6px; padding: 8px 10px; font: inherit; }
    button { cursor: pointer; }
    .summary { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 16px; }
    .metric, .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    .metric { padding: 14px; }
    .metric span { display: block; color: var(--muted); font-size: 12px; margin-bottom: 6px; }
    .metric strong { font-size: 26px; line-height: 1; }
    .metric.ok strong { color: var(--ok); }
    .metric.warn strong { color: var(--warn); }
    .metric.bad strong { color: var(--bad); }
    .grid { display: grid; grid-template-columns: 1.25fr .75fr; gap: 16px; align-items: start; }
    .panel { padding: 16px; margin-bottom: 16px; }
    .depth-row, .chain-row, .event-row { display: grid; gap: 10px; align-items: center; padding: 10px 0; border-top: 1px solid var(--line); }
    .depth-row { grid-template-columns: 90px 1fr 90px 90px; }
    .chain-row { grid-template-columns: 100px 1.2fr repeat(4, 78px); }
    .group-row { grid-template-columns: 1.1fr repeat(5, 86px); }
    .event-row { grid-template-columns: 150px 120px 80px 1fr; }
    .bar { height: 10px; background: #edf0f5; border-radius: 99px; overflow: hidden; }
    .bar > i { display: block; height: 100%; background: var(--info); }
    .tag { display: inline-flex; align-items: center; width: fit-content; border-radius: 999px; padding: 2px 8px; border: 1px solid var(--line); font-size: 12px; color: var(--muted); }
    .tag.ok { color: var(--ok); border-color: rgba(22, 121, 79, .3); }
    .tag.warn { color: var(--warn); border-color: rgba(163, 93, 0, .3); }
    .tag.bad { color: var(--bad); border-color: rgba(180, 35, 24, .3); }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
    @media (max-width: 900px) { .summary, .grid { grid-template-columns: 1fr; } header { align-items: flex-start; flex-direction: column; } .chain-row, .depth-row, .event-row { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <main id="app">
    <header>
      <div>
        <h1>LiteLLM Failover Stats</h1>
        <div class="muted">{{ subtitle }}</div>
      </div>
      <div class="toolbar">
        <select v-model="selectedGroupId">
          <option value="__all">全部链路</option>
          <option v-for="group in chainGroups" :key="group.id" :value="group.id">{{ group.label }}</option>
        </select>
        <select v-model="windowName" @change="load">
          <option value="5m">最近 5 分钟</option>
          <option value="1h">最近 1 小时</option>
          <option value="24h">最近 24 小时</option>
          <option value="today">今日</option>
          <option value="3d">最近 3 天</option>
          <option value="7d">最近 7 天</option>
          <option value="all">全部</option>
        </select>
        <button @click="load">刷新</button>
      </div>
    </header>

    <section class="summary">
      <div class="metric"><span>总请求</span><strong>{{ s.totalRequests }}</strong></div>
      <div class="metric ok"><span>主链完成</span><strong>{{ s.primaryCompletions }}</strong></div>
      <div class="metric warn"><span>进入备用</span><strong>{{ s.backupRequests }}</strong></div>
      <div class="metric warn"><span>到达第二备用</span><strong>{{ s.depth2OrMore }}</strong></div>
      <div class="metric bad"><span>未恢复失败</span><strong>{{ s.unresolvedFailures }}</strong></div>
    </section>

    <section class="grid">
      <div>
        <section class="panel">
          <h2>深度分布</h2>
          <div class="depth-row muted"><strong>深度</strong><strong>占比</strong><strong>成功</strong><strong>失败</strong></div>
          <div class="depth-row" v-for="row in activeStats.depthBuckets" :key="row.depth">
            <div><span class="tag" :class="row.depth === 0 ? 'ok' : 'warn'">{{ row.label }}</span></div>
            <div class="bar"><i :style="{ width: row.sharePercent + '%' }"></i></div>
            <code>{{ row.successes }}</code>
            <code>{{ row.failures }}</code>
          </div>
        </section>

        <section class="panel" v-if="selectedGroupId === '__all' && chainGroups.length">
          <h2>链路拆分</h2>
          <div class="chain-row group-row muted"><strong>链路</strong><strong>总请求</strong><strong>主链</strong><strong>备用</strong><strong>第二备用</strong><strong>失败</strong></div>
          <div class="chain-row group-row" v-for="group in chainGroups" :key="group.id">
            <code>{{ group.label }}</code>
            <code>{{ group.summary?.totalRequests || 0 }}</code>
            <code>{{ group.summary?.primaryCompletions || 0 }}</code>
            <code>{{ group.summary?.backupRequests || 0 }}</code>
            <code>{{ group.summary?.depth2OrMore || 0 }}</code>
            <code>{{ group.summary?.unresolvedFailures || 0 }}</code>
          </div>
        </section>

        <section class="panel" v-if="selectedGroupId !== '__all' || chainGroups.length <= 1">
          <h2>当前调用链</h2>
          <div class="chain-row muted"><strong>角色</strong><strong>模型</strong><strong>被调用</strong><strong>接住</strong><strong>下探失败</strong><strong>最终成功</strong></div>
          <div class="chain-row" v-for="row in activeStats.chain" :key="row.depth">
            <span class="tag" :class="row.depth === 0 ? 'ok' : 'warn'">{{ row.role }}</span>
            <code>{{ row.model }}</code>
            <code>{{ row.called }}</code>
            <code>{{ row.fallbackSuccesses }}</code>
            <code>{{ row.fallbackFailures }}</code>
            <code>{{ row.finalSuccesses }}</code>
          </div>
        </section>
      </div>

      <aside>
        <section class="panel">
          <h2>Prometheus</h2>
          <p><span class="tag" :class="stats.prometheus?.ok ? 'ok' : 'bad'">{{ stats.prometheus?.ok ? 'enabled' : 'unavailable' }}</span></p>
          <p class="muted"><code>{{ stats.prometheus?.url }}</code></p>
          <p class="muted">匹配指标：{{ stats.prometheus?.matchedMetricNames?.length || 0 }}</p>
        </section>

        <section class="panel">
          <h2>最近事件</h2>
          <div class="event-row" v-for="event in activeStats.recentEvents" :key="event.id">
            <code>{{ event.ts }}</code>
            <span class="tag" :class="event.success ? 'ok' : 'bad'">{{ event.eventType }}</span>
            <code>depth {{ event.depth }}</code>
            <code>{{ event.targetModelGroup || event.finalModelGroup || '-' }}</code>
          </div>
        </section>
      </aside>
    </section>
  </main>
  <script src="/vendor/vue.global.prod.js"></script>
  <script>
    const { createApp } = Vue;
    createApp({
      data() {
        return {
          windowName: 'today',
          selectedGroupId: '__all',
          subtitle: '',
          stats: { summary: {}, depthBuckets: [], chain: [], chainGroups: [], recentEvents: [], prometheus: {} },
        };
      },
      computed: {
        chainGroups() { return this.stats.chainGroups || []; },
        activeStats() {
          if (this.selectedGroupId === '__all') return this.stats;
          return this.chainGroups.find((group) => group.id === this.selectedGroupId) || this.stats;
        },
        s() { return this.activeStats.summary || {}; },
      },
      methods: {
        async load() {
          const response = await fetch('/failover-stats?window=' + encodeURIComponent(this.windowName));
          this.stats = await response.json();
          this.subtitle = this.stats.subtitle || '';
          if (this.selectedGroupId !== '__all' && !this.chainGroups.find((group) => group.id === this.selectedGroupId)) {
            this.selectedGroupId = '__all';
          }
        },
      },
      mounted() {
        this.load();
        setInterval(() => this.load(), 10000);
      },
    }).mount('#app');
  </script>
</body>
</html>
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_window(value: str) -> tuple[str, str | None]:
    normalized = (value or "1h").strip().lower()
    now = datetime.now(timezone.utc)
    if normalized in {"today", "day"}:
        local_start = now.astimezone(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        return "today", local_start.astimezone(timezone.utc).isoformat()
    if normalized == "5m":
        return normalized, (now - timedelta(minutes=5)).isoformat()
    if normalized == "24h":
        return normalized, (now - timedelta(hours=24)).isoformat()
    if normalized == "3d":
        return normalized, (now - timedelta(days=3)).isoformat()
    if normalized == "7d":
        return normalized, (now - timedelta(days=7)).isoformat()
    if normalized == "all":
        return normalized, None
    return "1h", (now - timedelta(hours=1)).isoformat()


def read_reset_marker() -> dict[str, Any]:
    if not RESET_PATH.exists():
        return {"enabled": False, "resetAt": None, "path": str(RESET_PATH)}
    try:
        payload = json.loads(RESET_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"enabled": False, "resetAt": None, "path": str(RESET_PATH), "error": str(exc)}
    reset_at = str(payload.get("resetAt") or "").strip()
    return {
        "enabled": bool(reset_at),
        "resetAt": reset_at or None,
        "path": str(RESET_PATH),
    }


def write_reset_marker() -> dict[str, Any]:
    marker = {
        "resetAt": now_iso(),
        "mode": "baseline",
        "note": "Displayed counters ignore events before resetAt; raw SQLite and JSONL logs are kept.",
    }
    RESET_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESET_PATH.write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return read_reset_marker()


def newest_since(*values: str | None) -> str | None:
    candidates = [value for value in values if value]
    return max(candidates) if candidates else None


def parse_chain_groups() -> list[dict[str, Any]]:
    groups = []
    for index, chunk in enumerate(CHAIN_GROUPS_RAW.split(";"), start=1):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            label, models_raw = chunk.split(":", 1)
        else:
            label, models_raw = f"Chain {index}", chunk
        models = [item.strip() for item in models_raw.split(",") if item.strip()]
        if models:
            groups.append(
                {
                    "id": re.sub(r"[^a-zA-Z0-9_.-]+", "-", (models[0] or label).lower()).strip("-"),
                    "label": label.strip() or models[0],
                    "primary": models[0],
                    "models": models,
                }
            )
    if not groups and CHAIN:
        groups.append(
            {
                "id": re.sub(r"[^a-zA-Z0-9_.-]+", "-", CHAIN[0].lower()).strip("-"),
                "label": CHAIN[0],
                "primary": CHAIN[0],
                "models": CHAIN,
            }
        )
    return groups


CHAIN_GROUPS = parse_chain_groups()


def fetch_prometheus() -> dict[str, Any]:
    try:
        with urllib.request.urlopen(PROMETHEUS_URL, timeout=5) as response:
            status = getattr(response, "status", 200)
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return {"ok": False, "url": PROMETHEUS_URL, "status": exc.code, "error": exc.read().decode("utf-8", errors="replace")[:300]}
    except Exception as exc:
        return {"ok": False, "url": PROMETHEUS_URL, "status": 0, "error": str(exc)}

    names = []
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[{\\s]", line, maxsplit=1)[0]
        if any(token in name.lower() for token in ["fallback", "cooldown", "fail", "request", "deployment"]):
            names.append(name)
    return {
        "ok": 200 <= status < 300,
        "url": PROMETHEUS_URL,
        "status": status,
        "matchedMetricNames": sorted(set(names))[:80],
    }


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        rows = conn.execute(f"pragma table_info({table_name})").fetchall()
    except Exception:
        return set()
    return {str(row[1]) for row in rows}


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def parse_raw_event(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("eventType") or "unknown")
        counts[event_type] = counts.get(event_type, 0) + 1
    return dict(sorted(counts.items()))


def percentile(sorted_values: list[float], percent: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return round(sorted_values[0], 3)
    rank = (len(sorted_values) - 1) * percent
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    value = sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight
    return round(value, 3)


def duration_stats(values: list[Any]) -> dict[str, Any]:
    numbers = sorted(value for item in values for value in [safe_float(item)] if value is not None)
    if not numbers:
        return {"count": 0, "avg": None, "p50": None, "p90": None, "p95": None, "p99": None, "max": None}
    return {
        "count": len(numbers),
        "avg": round(sum(numbers) / len(numbers), 3),
        "p50": percentile(numbers, 0.50),
        "p90": percentile(numbers, 0.90),
        "p95": percentile(numbers, 0.95),
        "p99": percentile(numbers, 0.99),
        "max": round(numbers[-1], 3),
    }


def count_by_field(events: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        value = str(event.get(field) or "unknown").strip() or "unknown"
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def tuning_summary(
    events: list[dict[str, Any]],
    request_events: list[dict[str, Any]],
    fallback_events: list[dict[str, Any]],
) -> dict[str, Any]:
    attempt_starts = [event for event in events if event["eventType"] == "attempt_start"]
    request_successes = [event for event in events if event["eventType"] == "request_success"]
    request_failures = [event for event in events if event["eventType"] == "request_failure"]
    primary_successes = [event for event in request_successes if safe_int(event.get("depth")) == 0]
    primary_failures = [event for event in request_failures if safe_int(event.get("depth")) == 0]
    fallback_successes = [event for event in fallback_events if event["eventType"] == "fallback_success"]
    fallback_failures = [event for event in fallback_events if event["eventType"] == "fallback_failure"]
    cooldown_sets = [event for event in events if event["eventType"] == "cooldown_set"]
    cooldown_skips = [event for event in events if event["eventType"] == "cooldown_skipped"]
    cooldown_clears = [event for event in events if event["eventType"] == "cooldown_clear_success"]
    probe_observed = [event for event in events if event["eventType"] == "probe_cooldown_observed"]
    probe_successes = [event for event in events if event["eventType"] == "probe_success"]
    probe_failures = [event for event in events if event["eventType"] == "probe_failure"]
    error_events = [event for event in request_failures + fallback_failures + cooldown_skips if event.get("errorCategory")]
    fallback_request_events = [event for event in request_events if safe_int(event.get("depth")) >= 1]
    return {
        "attempts": {
            "started": len(attempt_starts),
            "success": len(request_successes),
            "failure": len(request_failures),
            "primarySuccessLatencyMs": duration_stats([event.get("durationMs") for event in primary_successes]),
            "primaryFailureLatencyMs": duration_stats([event.get("durationMs") for event in primary_failures]),
        },
        "fallback": {
            "success": len(fallback_successes),
            "failure": len(fallback_failures),
            "clientVisibleTotalMs": duration_stats([event.get("durationMs") for event in fallback_request_events]),
            "decisionDelayMs": duration_stats([event.get("fallbackDecisionDelayMs") for event in fallback_events]),
            "attemptDurationMs": duration_stats([event.get("fallbackAttemptDurationMs") for event in fallback_events]),
            "totalDurationMs": duration_stats([event.get("totalDurationMs") for event in fallback_events]),
        },
        "errors": {
            "byCategory": count_by_field(error_events, "errorCategory"),
            "byExceptionType": count_by_field([event for event in error_events if event.get("exceptionType")], "exceptionType"),
        },
        "cooldown": {
            "set": len(cooldown_sets),
            "skipped": len(cooldown_skips),
            "clearSuccess": len(cooldown_clears),
            "seconds": duration_stats([event.get("cooldownSeconds") for event in cooldown_sets]),
            "ageBeforeClearMs": duration_stats([event.get("cooldownAgeMs") for event in cooldown_clears]),
            "skipReasons": count_by_field(cooldown_skips, "reason"),
        },
        "probe": {
            "cooldownObserved": len(probe_observed),
            "success": len(probe_successes),
            "failure": len(probe_failures),
            "durationMs": duration_stats([event.get("durationMs") for event in probe_successes + probe_failures]),
            "failureDetails": count_by_field(probe_failures, "detail"),
        },
    }


def load_events(since: str | None) -> list[dict[str, Any]]:
    if not DB_PATH.exists():
        return []
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        columns = table_columns(conn, "failover_events")
        raw_expr = "raw_json" if "raw_json" in columns else "'' as raw_json"
        query = f"""
          select id, ts, event_type, call_id, requested_model, original_model_group,
                 target_model_group, final_model_group, depth, max_fallbacks,
                 success, exception_type, error, duration_ms, model_id, {raw_expr}
          from failover_events
        """
        params: list[Any] = []
        if since:
            query += " where ts >= ?"
            params.append(since)
        query += " order by ts asc, id asc"
        rows = conn.execute(query, params).fetchall()
    events = []
    for row in rows:
        raw_event = parse_raw_event(row["raw_json"])
        event = {
            "id": row["id"],
            "ts": row["ts"],
            "eventType": row["event_type"],
            "callId": row["call_id"],
            "requestedModel": row["requested_model"],
            "originalModelGroup": row["original_model_group"],
            "targetModelGroup": row["target_model_group"],
            "finalModelGroup": row["final_model_group"],
            "depth": safe_int(row["depth"]),
            "maxFallbacks": safe_int(row["max_fallbacks"]),
            "success": bool(row["success"]),
            "exceptionType": row["exception_type"],
            "error": row["error"],
            "durationMs": row["duration_ms"],
            "modelId": row["model_id"],
        }
        events.append({**raw_event, **event})
    return events


def depth_label(depth: int) -> str:
    if depth == 0:
        return "Depth 0"
    return f"Depth {depth}"


def synthesize_request_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    direct = [event for event in events if event["eventType"] in {"client_request_success", "client_request_failure"}]
    if direct:
        return direct

    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if event["eventType"] not in {"request_success", "request_failure", "fallback_success", "fallback_failure"}:
            continue
        key = str(event.get("callId") or "").strip() or f"id:{event.get('id')}"
        grouped.setdefault(key, []).append(event)

    synthesized = []
    for key, rows in grouped.items():
        success_rows = [event for event in rows if event["eventType"] == "request_success" and event.get("success")]
        request_failure_rows = [event for event in rows if event["eventType"] == "request_failure"]
        fallback_rows = [event for event in rows if event["eventType"] in {"fallback_success", "fallback_failure"}]
        fallback_success_rows = [event for event in fallback_rows if event["eventType"] == "fallback_success"]
        depth = max([int(event.get("depth") or 0) for event in fallback_rows] or [0])
        success = bool(success_rows or fallback_success_rows)
        final_source = (success_rows[-1:] or fallback_success_rows[-1:] or request_failure_rows[-1:] or rows[-1:])[0]
        original_source = rows[0]
        synthesized.append(
            {
                "id": final_source.get("id"),
                "ts": final_source.get("ts"),
                "eventType": "client_request_success" if success else "client_request_failure",
                "callId": "" if key.startswith("id:") else key,
                "requestedModel": original_source.get("requestedModel") or final_source.get("requestedModel"),
                "originalModelGroup": original_source.get("originalModelGroup") or original_source.get("requestedModel"),
                "targetModelGroup": final_source.get("targetModelGroup"),
                "finalModelGroup": final_source.get("finalModelGroup") or final_source.get("targetModelGroup"),
                "depth": depth,
                "maxFallbacks": max([int(event.get("maxFallbacks") or 0) for event in rows] or [0]),
                "success": success,
                "exceptionType": "" if success else final_source.get("exceptionType", ""),
                "error": "" if success else final_source.get("error", ""),
                "durationMs": final_source.get("totalDurationMs") or final_source.get("durationMs"),
                "modelId": final_source.get("modelId"),
            }
        )
    synthesized.sort(key=lambda event: (str(event.get("ts") or ""), int(event.get("id") or 0)))
    return synthesized


def chain_models(events: list[dict[str, Any]]) -> list[str]:
    if CHAIN:
        return CHAIN
    discovered = []
    for event in events:
        for key in ["requestedModel", "originalModelGroup", "targetModelGroup", "finalModelGroup"]:
            model = str(event.get(key) or "").strip()
            if model and model not in discovered:
                discovered.append(model)
    return discovered


def summary_for_request_events(request_events: list[dict[str, Any]], fallback_events: list[dict[str, Any]]) -> dict[str, int]:
    total_requests = len(request_events)
    return {
        "totalRequests": total_requests,
        "primaryCompletions": sum(1 for event in request_events if event["eventType"] == "client_request_success" and event["depth"] == 0),
        "backupRequests": sum(1 for event in request_events if event["depth"] >= 1),
        "depth2OrMore": sum(1 for event in request_events if event["depth"] >= 2),
        "unresolvedFailures": sum(1 for event in request_events if event["eventType"] == "client_request_failure"),
        "maxDepth": max([event["depth"] for event in request_events + fallback_events] or [0]),
        "fallbackEvents": len(fallback_events),
    }


def chain_for_models(
    models: list[str],
    request_events: list[dict[str, Any]],
    fallback_events: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    chain = []
    for depth, model in enumerate(models):
        final_successes = [
            event for event in request_events
            if event["eventType"] == "client_request_success"
            and event["depth"] == depth
            and (not event.get("finalModelGroup") or event.get("finalModelGroup") == model or depth == 0)
        ]
        final_failures = [
            event for event in request_events
            if event["eventType"] == "client_request_failure"
            and event["depth"] == depth
            and (not event.get("finalModelGroup") or event.get("finalModelGroup") == model or depth == 0)
        ]
        fallback_successes = [
            event for event in fallback_events
            if event["eventType"] == "fallback_success"
            and (event["depth"] == depth or event.get("targetModelGroup") == model)
        ]
        fallback_failures = [
            event for event in fallback_events
            if event["eventType"] == "fallback_failure"
            and (event["depth"] == depth or event.get("targetModelGroup") == model)
        ]
        called = len(request_events) if depth == 0 else len(fallback_successes) + len(fallback_failures)
        last_event = next((event for event in reversed(events) if event.get("targetModelGroup") == model or event.get("finalModelGroup") == model), None)
        chain.append(
            {
                "depth": depth,
                "role": "Primary" if depth == 0 else f"Fallback #{depth}",
                "model": model,
                "called": called,
                "fallbackSuccesses": len(fallback_successes),
                "fallbackFailures": len(fallback_failures),
                "finalSuccesses": len(final_successes),
                "finalFailures": len(final_failures),
                "lastSeenAt": last_event.get("ts") if last_event else None,
                "lastError": last_event.get("error") if last_event and last_event.get("error") else "",
            }
        )
    return chain


def depth_buckets_for_request_events(request_events: list[dict[str, Any]], max_depth: int) -> list[dict[str, Any]]:
    total_requests = len(request_events)
    depth_buckets = []
    for depth in range(max_depth + 1):
        bucket = [event for event in request_events if event["depth"] == depth]
        successes = sum(1 for event in bucket if event["eventType"] == "client_request_success")
        failures = sum(1 for event in bucket if event["eventType"] == "client_request_failure")
        count = len(bucket)
        depth_buckets.append(
            {
                "depth": depth,
                "label": depth_label(depth),
                "requests": count,
                "successes": successes,
                "failures": failures,
                "sharePercent": round((count / total_requests) * 100, 2) if total_requests else 0,
            }
        )
    return depth_buckets


def event_belongs_to_group(event: dict[str, Any], group: dict[str, Any]) -> bool:
    models = set(group["models"])
    origin_candidates = {
        str(event.get("requestedModel") or ""),
        str(event.get("originalModelGroup") or ""),
    }
    if any(origin_candidates) and (bool(models.intersection(origin_candidates)) or group["primary"] in origin_candidates):
        return True
    target_candidates = {
        str(event.get("targetModelGroup") or ""),
        str(event.get("finalModelGroup") or ""),
        str(event.get("modelId") or ""),
        str(event.get("deploymentId") or ""),
    }
    return bool(models.intersection(target_candidates))


def build_call_group_index(events: list[dict[str, Any]], groups: list[dict[str, Any]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for event in events:
        call_id = str(event.get("callId") or "").strip()
        if not call_id or call_id in index:
            continue
        for group in groups:
            if event_belongs_to_group(event, group):
                index[call_id] = str(group["id"])
                break
    return index


def event_belongs_to_group_by_call(
    event: dict[str, Any],
    group: dict[str, Any],
    call_group_index: dict[str, str],
) -> bool:
    call_id = str(event.get("callId") or "").strip()
    if call_id and call_id in call_group_index:
        return call_group_index[call_id] == group["id"]
    return event_belongs_to_group(event, group)


def build_chain_groups(
    events: list[dict[str, Any]],
    request_events: list[dict[str, Any]],
    fallback_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    configured_groups = CHAIN_GROUPS
    if not configured_groups:
        grouped_models = chain_models(events)
        configured_groups = [{"id": "all", "label": "All", "primary": grouped_models[0] if grouped_models else "", "models": grouped_models}]

    call_group_index = build_call_group_index(events, configured_groups)
    groups = []
    for group in configured_groups:
        group_request_events = [
            event for event in request_events
            if event_belongs_to_group_by_call(event, group, call_group_index)
        ]
        group_fallback_events = [
            event for event in fallback_events
            if event_belongs_to_group_by_call(event, group, call_group_index)
        ]
        group_events = [
            event for event in events
            if event_belongs_to_group_by_call(event, group, call_group_index)
        ]
        summary = summary_for_request_events(group_request_events, group_fallback_events)
        groups.append(
            {
                **group,
                "summary": summary,
                "tuning": tuning_summary(group_events, group_request_events, group_fallback_events),
                "depthBuckets": depth_buckets_for_request_events(group_request_events, summary["maxDepth"]),
                "chain": chain_for_models(group["models"], group_request_events, group_fallback_events, group_events),
                "recentEvents": list(reversed(group_events[-20:])),
            }
        )
    return groups


def build_stats(window_name: str = "1h") -> dict[str, Any]:
    resolved_window, window_since = parse_window(window_name)
    reset = read_reset_marker()
    since = newest_since(window_since, reset.get("resetAt"))
    events = load_events(since)
    request_events = synthesize_request_events(events)
    fallback_events = [event for event in events if event["eventType"] in {"fallback_success", "fallback_failure"}]
    summary = summary_for_request_events(request_events, fallback_events)

    chain_groups = build_chain_groups(events, request_events, fallback_events)
    recent_events = list(reversed(events[-50:]))
    return {
        "ok": True,
        "generatedAt": now_iso(),
        "subtitle": SUBTITLE,
        "window": {"name": resolved_window, "since": since, "windowSince": window_since},
        "reset": reset,
        "source": {"dbPath": str(DB_PATH), "jsonlPath": str(JSONL_PATH)},
        "summary": {
            **summary,
        },
        "depthBuckets": depth_buckets_for_request_events(request_events, summary["maxDepth"]),
        "chain": chain_for_models(chain_models(events), request_events, fallback_events, events),
        "chainGroups": chain_groups,
        "tuning": tuning_summary(events, request_events, fallback_events),
        "recentEvents": recent_events,
        "eventCounts": event_counts(events),
        "prometheus": fetch_prometheus(),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        self._send_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8", status)

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        if path == "/":
            self._send_bytes(HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/vendor/vue.global.prod.js":
            try:
                body = VUE_BUNDLE_PATH.read_bytes()
                self._send_bytes(body, "application/javascript; charset=utf-8")
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc), "path": str(VUE_BUNDLE_PATH)}, 404)
            return
        if path == "/healthz":
            self._send_json({"ok": True, "generatedAt": now_iso(), "dbExists": DB_PATH.exists(), "prometheusUrl": PROMETHEUS_URL})
            return
        if path == "/failover-stats":
            params = dict(item.split("=", 1) if "=" in item else (item, "") for item in query.split("&") if item)
            self._send_json(build_stats(params.get("window", "1h")))
            return
        self._send_json({"ok": False, "error": "not_found", "path": path}, 404)

    def do_POST(self) -> None:
        if self.path == "/admin/reset":
            if not ALLOW_RESET:
                self._send_json({"ok": False, "error": "reset_disabled"}, 403)
                return
            self._send_json({"ok": True, "generatedAt": now_iso(), "reset": write_reset_marker()})
            return
        self._send_json({"ok": False, "error": "not_found", "path": self.path}, 404)


def serve(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"failover-stats-api listening on http://{host}:{port}", flush=True)
    while True:
        server.handle_request()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        print(json.dumps(build_stats("all"), ensure_ascii=False, indent=2))
        return
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
