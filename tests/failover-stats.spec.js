import { test, expect } from '@playwright/test';
import { spawnSync } from 'node:child_process';

const statsUrl = process.env.FAILOVER_STATS_URL || 'http://127.0.0.1:4149';
const proxyUrl = process.env.FAILOVER_STATS_PROXY_URL || 'http://127.0.0.1:4150';
const litellmHealthUrl = process.env.LITELLM_TEST_HEALTH_URL || 'http://127.0.0.1:4042/health/liveliness';
const redisContainer = process.env.LITELLM_TEST_REDIS_CONTAINER || 'litellm-structured-test-redis';
const litellmContainer = process.env.LITELLM_TEST_CONTAINER || 'litellm-structured-test';
const cooldownKey = 'deployment:gpt-5.4-router-test:cooldown';
const mockPorts = [4222, 4223, 4224, 4225, 4226];

function docker(args) {
  const result = spawnSync('docker', args, { encoding: 'utf-8' });
  expect(result.status, result.stderr || result.stdout).toBe(0);
  return result.stdout.trim();
}

async function waitForHealthy(url) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) {
        return;
      }
    } catch {
      // Retry while the restarted LiteLLM process is still binding the port.
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`Timed out waiting for ${url}`);
}

async function setMockStatus(port, status) {
  const response = await fetch(`http://127.0.0.1:${port}/mock/status`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status }),
  });
  expect(response.ok).toBeTruthy();
}

async function setAll(status) {
  await Promise.all(mockPorts.map((port) => setMockStatus(port, status)));
}

async function redisExists(key) {
  const stdout = docker(['exec', redisContainer, 'redis-cli', 'EXISTS', key]);
  return Number(stdout.split(/\s+/).filter(Boolean).at(-1) || 0);
}

async function waitForRedisExists(key, expected) {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    if (await redisExists(key) === expected) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`Timed out waiting for Redis ${key} EXISTS=${expected}`);
}

async function getStats(windowName = 'all') {
  const response = await fetch(`${statsUrl}/failover-stats?window=${windowName}`);
  expect(response.ok).toBeTruthy();
  return response.json();
}

async function waitForStats(predicate, label) {
  let stats;
  for (let attempt = 0; attempt < 40; attempt += 1) {
    stats = await getStats('all');
    if (predicate(stats)) {
      return stats;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`Timed out waiting for stats: ${label}\n${JSON.stringify(stats?.eventCounts || {}, null, 2)}`);
}

async function callGateway() {
  const response = await fetch(`${proxyUrl}/v1/chat/completions`, {
    method: 'POST',
    headers: {
      Authorization: 'Bearer local-litellm-gateway',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      model: 'gpt-5.4-router-test',
      messages: [{ role: 'user', content: 'ping' }],
      max_tokens: 4,
      stream: false,
    }),
  });
  const body = await response.json();
  return {
    ok: response.ok,
    depth: Number(response.headers.get('x-litellm-attempted-fallbacks') || 0),
    modelGroup: response.headers.get('x-litellm-model-group') || '',
    content: body?.choices?.[0]?.message?.content || '',
  };
}

async function resetStats() {
  await setAll(200);
  docker(['exec', redisContainer, 'redis-cli', 'FLUSHALL']);
  docker(['restart', litellmContainer]);
  await waitForHealthy(litellmHealthUrl);

  const reset = await fetch(`${statsUrl}/admin/reset`, { method: 'POST' });
  expect(reset.ok).toBeTruthy();
  await setAll(200);
}

test.describe('failover stats test chain', () => {
  test('records depth 0, depth 1, and depth 2 client requests', async ({ page }) => {
    await resetStats();

    await setAll(200);
    const depth0 = await callGateway();
    expect(depth0).toMatchObject({
      ok: true,
      depth: 0,
      modelGroup: 'gpt-5.4-router-test',
      content: 'ok:primary',
    });

    await setMockStatus(4222, 500);
    await setMockStatus(4223, 200);
    await setMockStatus(4224, 200);
    const depth1 = await callGateway();
    expect(depth1).toMatchObject({
      ok: true,
      depth: 1,
      modelGroup: 'gpt-5.4-router-test-claudecoder',
      content: 'ok:fallback1',
    });
    await waitForRedisExists(cooldownKey, 1);

    let stats = await waitForStats(
      (body) => (body.eventCounts?.cooldown_set || 0) >= 1 && (body.eventCounts?.attempt_start || 0) >= 2,
      'attempt_start and cooldown_set after primary failure',
    );
    expect(stats.eventCounts.attempt_start).toBeGreaterThanOrEqual(2);
    const requestFailure = stats.recentEvents.find((event) => event.eventType === 'request_failure' && event.modelId === 'gpt-5.4-router-test');
    expect(requestFailure).toMatchObject({
      modelId: 'gpt-5.4-router-test',
      errorCategory: 'upstream_5xx',
      configuredTimeoutMs: 5000,
      configuredStreamTimeoutMs: 5000,
      configuredCooldownSeconds: 300,
      shouldCooldown: true,
      stream: false,
    });
    expect(requestFailure.startedAt).toBeTruthy();
    expect(requestFailure.endedAt).toBeTruthy();
    expect(Number(requestFailure.durationMs)).toBeGreaterThanOrEqual(0);
    const cooldownSet = stats.recentEvents.find((event) => event.eventType === 'cooldown_set' && event.modelId === 'gpt-5.4-router-test');
    expect(cooldownSet).toMatchObject({
      cooldownKey,
      cooldownSeconds: 300,
      redisWriteOk: true,
      triggerEventType: 'request_failure',
      success: true,
    });
    const fallbackSuccess = stats.recentEvents.find((event) => event.eventType === 'fallback_success' && event.targetModelGroup === 'gpt-5.4-router-test-claudecoder');
    expect(fallbackSuccess).toMatchObject({
      originalErrorCategory: 'upstream_5xx',
      lastModelGroup: 'gpt-5.4-router-test-claudecoder',
      lastModelId: 'gpt-5.4-router-test-claudecoder',
    });
    expect(fallbackSuccess.originalFailureAt).toBeTruthy();
    expect(fallbackSuccess.fallbackStartedAt).toBeTruthy();
    expect(fallbackSuccess.fallbackCompletedAt).toBeTruthy();
    expect(Number(fallbackSuccess.fallbackDecisionDelayMs)).toBeGreaterThanOrEqual(0);
    expect(Number(fallbackSuccess.totalDurationMs)).toBeGreaterThanOrEqual(0);

    await setMockStatus(4222, 200);
    await waitForRedisExists(cooldownKey, 0);
    stats = await waitForStats(
      (body) => (body.eventCounts?.probe_success || 0) >= 1 && (body.eventCounts?.cooldown_clear_success || 0) >= 1,
      'probe_success and cooldown_clear_success after recovery',
    );
    expect(stats.recentEvents.find((event) => event.eventType === 'probe_cooldown_observed' && event.deploymentId === 'gpt-5.4-router-test')).toMatchObject({
      cooldownKey,
      probeIntervalSeconds: 1,
      probeTimeoutSeconds: 2,
      successThreshold: 2,
    });
    expect(stats.recentEvents.find((event) => event.eventType === 'probe_success' && event.deploymentId === 'gpt-5.4-router-test')).toMatchObject({
      cooldownKey,
      probeIntervalSeconds: 1,
      probeTimeoutSeconds: 2,
      successThreshold: 2,
    });
    expect(stats.recentEvents.find((event) => event.eventType === 'cooldown_clear_success' && event.deploymentId === 'gpt-5.4-router-test')).toMatchObject({
      cooldownKey,
      clearMethod: 'recovery_url',
      status: 200,
      success: true,
    });

    await setMockStatus(4222, 500);
    await setMockStatus(4223, 500);
    await setMockStatus(4224, 200);
    const depth2 = await callGateway();
    expect(depth2).toMatchObject({
      ok: true,
      depth: 2,
      modelGroup: 'gpt-5.4-router-test-popcorn',
      content: 'ok:fallback2',
    });

    await setAll(200);

    stats = await getStats('all');
    expect(stats.summary).toMatchObject({
      totalRequests: 3,
      primaryCompletions: 1,
      backupRequests: 2,
      depth2OrMore: 1,
      unresolvedFailures: 0,
      maxDepth: 2,
    });
    expect(stats.depthBuckets.map((item) => [item.depth, item.requests])).toEqual([
      [0, 1],
      [1, 1],
      [2, 1],
    ]);
    expect(stats.chain.find((item) => item.depth === 1)).toMatchObject({
      model: 'gpt-5.4-router-test-claudecoder',
      called: 2,
      finalSuccesses: 1,
    });
    expect(stats.chain.find((item) => item.depth === 2)).toMatchObject({
      model: 'gpt-5.4-router-test-popcorn',
      called: 1,
      finalSuccesses: 1,
    });
    expect(stats.chainGroups).toHaveLength(2);
    const gpt54Group = stats.chainGroups.find((group) => group.primary === 'gpt-5.4-router-test');
    expect(gpt54Group).toMatchObject({
      label: 'Test gpt-5.4',
      primary: 'gpt-5.4-router-test',
      summary: {
        totalRequests: 3,
        primaryCompletions: 1,
        backupRequests: 2,
        depth2OrMore: 1,
        unresolvedFailures: 0,
      },
    });
    expect(gpt54Group.chain.find((item) => item.depth === 2)).toMatchObject({
      model: 'gpt-5.4-router-test-popcorn',
      called: 1,
    });
    expect(gpt54Group.tuning.attempts.primaryFailureLatencyMs.count).toBeGreaterThanOrEqual(1);
    expect(gpt54Group.tuning.fallback.decisionDelayMs.count).toBeGreaterThanOrEqual(1);
    expect(gpt54Group.tuning.cooldown.set).toBeGreaterThanOrEqual(1);
    expect(gpt54Group.tuning.probe.cooldownObserved).toBeGreaterThanOrEqual(1);
    expect(stats.tuning.fallback.totalDurationMs.count).toBeGreaterThanOrEqual(1);
    expect(stats.prometheus.ok).toBeTruthy();

    for (const windowName of ['today', '3d', '7d']) {
      const windowResponse = await fetch(`${statsUrl}/failover-stats?window=${windowName}`);
      expect(windowResponse.ok).toBeTruthy();
      const windowStats = await windowResponse.json();
      expect(windowStats.window.name).toBe(windowName);
      expect(windowStats.summary.totalRequests).toBe(3);
    }

    await page.goto(statsUrl);
    await expect(page.getByRole('heading', { name: 'LiteLLM Failover Stats' })).toBeVisible();
    await expect(page.getByText('测试链路聚合看板')).toBeVisible();
    await expect(page.getByText('进入备用')).toBeVisible();
  });

});
