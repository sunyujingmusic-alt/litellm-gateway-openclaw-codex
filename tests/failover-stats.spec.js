import { test, expect } from '@playwright/test';
import { spawnSync } from 'node:child_process';

const statsUrl = process.env.FAILOVER_STATS_URL || 'http://127.0.0.1:4128';
const proxyUrl = process.env.FAILOVER_STATS_PROXY_URL || 'http://127.0.0.1:4138';
const litellmHealthUrl = process.env.LITELLM_TEST_HEALTH_URL || 'http://127.0.0.1:4028/health/liveliness';
const mockPorts = [4210, 4211, 4212, 4213, 4214];

function docker(args) {
  const result = spawnSync('docker', args, { encoding: 'utf-8' });
  expect(result.status, result.stderr || result.stdout).toBe(0);
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

async function callGateway() {
  const response = await fetch(`${proxyUrl}/v1/chat/completions`, {
    method: 'POST',
    headers: {
      Authorization: 'Bearer local-litellm-gateway',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      model: 'gpt-5.5-router-test',
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
  docker(['exec', 'litellm-router-gpt-5.5-test-redis', 'redis-cli', 'FLUSHALL']);
  docker(['restart', 'litellm-router-gpt-5.5-test']);
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
      modelGroup: 'gpt-5.5-router-test',
      content: 'ok:primary',
    });

    await setMockStatus(4210, 500);
    await setMockStatus(4211, 200);
    await setMockStatus(4212, 200);
    const depth1 = await callGateway();
    expect(depth1).toMatchObject({
      ok: true,
      depth: 1,
      modelGroup: 'gpt-5.5-router-test-claudecoder',
      content: 'ok:fallback1',
    });

    await setMockStatus(4210, 500);
    await setMockStatus(4211, 500);
    await setMockStatus(4212, 200);
    const depth2 = await callGateway();
    expect(depth2).toMatchObject({
      ok: true,
      depth: 2,
      modelGroup: 'gpt-5.5-router-test-popcorn',
      content: 'ok:fallback2',
    });

    await setAll(200);

    const statsResponse = await fetch(`${statsUrl}/failover-stats?window=all`);
    expect(statsResponse.ok).toBeTruthy();
    const stats = await statsResponse.json();
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
      model: 'gpt-5.5-router-test-claudecoder',
      called: 2,
      finalSuccesses: 1,
    });
    expect(stats.chain.find((item) => item.depth === 2)).toMatchObject({
      model: 'gpt-5.5-router-test-popcorn',
      called: 1,
      finalSuccesses: 1,
    });
    expect(stats.prometheus.ok).toBeTruthy();

    await page.goto(statsUrl);
    await expect(page.getByRole('heading', { name: 'LiteLLM Failover Stats' })).toBeVisible();
    await expect(page.getByText('测试链路聚合看板')).toBeVisible();
    await expect(page.getByText('进入备用')).toBeVisible();
  });
});
