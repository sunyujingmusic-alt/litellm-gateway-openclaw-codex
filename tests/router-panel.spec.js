import { test, expect } from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';

const rootDir = process.env.LITELLM_UI_TEST_ROOT || path.resolve(process.cwd(), '../litellm-gateway-ui-test');
const envPath = `${rootDir}/.env`;
const configPath = `${rootDir}/litellm/config.yaml`;
const testModelName = 'gpt-5.4-ui-test';
const testModelBaseUrl = 'https://ui-test.example.com/v1';
const testModelApiKey = 'sk-ui-test-1234567890';
const editedBaseUrl = 'https://edited.example.com/v1';
const editedApiKey = 'sk-edit-test-1234567890';
const probeBaseUrl = 'https://probe.example.com/v1';
const probeApiKey = 'sk-probe-test-1234567890';

async function getRouterConfig(request) {
  const response = await request.get('/router-config');
  expect(response.ok()).toBeTruthy();
  return response.json();
}

async function waitForRouterConfig(request, predicate, timeoutMs = 8000) {
  const start = Date.now();
  let last;
  while (Date.now() - start < timeoutMs) {
    last = await getRouterConfig(request);
    if (predicate(last)) {
      return last;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error(`router-config did not reach expected state in ${timeoutMs}ms: ${JSON.stringify(last)}`);
}

async function restoreOriginalFiles(snapshot) {
  await fs.writeFile(envPath, snapshot.env, 'utf8');
  await fs.writeFile(configPath, snapshot.config, 'utf8');
}

test.describe('LiteLLM Router Panel', () => {
  test('supports drag, toggle, edit, add, delete, save, and reload in test copy', async ({ page, request }) => {
    const original = {
      env: await fs.readFile(envPath, 'utf8'),
      config: await fs.readFile(configPath, 'utf8'),
    };

    try {
      const initial = await getRouterConfig(request);
      expect(initial.models.some((model) => model.model_name === 'gpt-5.4-fastCode' && model.enabled)).toBeTruthy();
      expect(initial.models.some((model) => model.model_name === 'gpt-5.4-ccodex' && !model.enabled)).toBeTruthy();

      await page.goto('/');
      await expect(page.getByRole('heading', { name: 'LiteLLM Router Panel' })).toBeVisible();
      await expect(page.locator('#save-btn')).toBeVisible();

      const activeStack = page.locator('#active-stack');
      const getActiveNames = async () => {
        const cards = activeStack.locator('.card .model-name');
        return cards.allInnerTexts();
      };

      await expect.poll(getActiveNames).toEqual([
        'gpt-5.4-fastCode',
        'gpt-5.4-popcorn',
        'gpt-5.4-gmn',
      ]);

      const fastCodeCard = activeStack.locator('.card').filter({ has: page.locator('.model-name', { hasText: 'gpt-5.4-fastCode' }) });
      const gmnCard = activeStack.locator('.card').filter({ has: page.locator('.model-name', { hasText: 'gpt-5.4-gmn' }) });
      await fastCodeCard.dragTo(gmnCard, {
        targetPosition: { x: 40, y: 180 },
      });
      await expect.poll(getActiveNames).toEqual([
        'gpt-5.4-popcorn',
        'gpt-5.4-gmn',
        'gpt-5.4-fastCode',
      ]);
      await expect(page.locator('#pending-bar')).toHaveClass(/is-visible/);

      const oauthPoolItem = page.locator('.pool-item').filter({ has: page.getByText('gpt-5.4-oauth', { exact: true }) });
      await oauthPoolItem.getByText('bypassed', { exact: true }).click();
      await expect.poll(getActiveNames).toEqual([
        'gpt-5.4-popcorn',
        'gpt-5.4-gmn',
        'gpt-5.4-fastCode',
        'gpt-5.4-oauth',
      ]);

      const fastCodePoolItem = page.locator('.pool-item').filter({ has: page.getByText('gpt-5.4-fastCode', { exact: true }) });
      await fastCodePoolItem.getByRole('button', { name: 'edit' }).click();
      await expect(page.locator('#edit-overlay')).toHaveClass(/open/);
      await expect(page.locator('#edit-delete-btn')).toBeHidden();
      await expect(page.locator('#edit-test-btn')).toBeVisible();
      await page.fill('#edit-model-name', 'gpt-5.4-probe');
      await page.fill('#edit-base-url', probeBaseUrl);
      await page.fill('#edit-api-key', probeApiKey);
      await page.locator('#edit-test-btn').click();
      await expect(page.locator('#message')).toContainText(/测试成功|测试失败/);
      await page.fill('#edit-model-name', 'gpt-5.4-fastCode');
      await page.fill('#edit-base-url', editedBaseUrl);
      await page.fill('#edit-api-key', editedApiKey);
      await page.locator('#edit-save-btn').click();
      await expect(page.locator('#message')).toContainText('中转站参数已更新');

      const afterEdit = await getRouterConfig(request);
      const editedModel = afterEdit.models.find((model) => model.model_name === 'gpt-5.4-fastCode');
      expect(editedModel?.baseUrlValue).toBe(editedBaseUrl);

      await page.getByRole('button', { name: '添加新模型' }).click();
      await expect(page.locator('#new-model-overlay')).toHaveClass(/open/);
      await page.fill('#new-model-name', testModelName);
      await page.fill('#new-model-base-url', testModelBaseUrl);
      await page.fill('#new-model-api-key', testModelApiKey);
      await page.getByRole('button', { name: '创建并重启 LiteLLM' }).click();
      await expect(page.locator('#message')).toContainText('新模型已创建');

      const afterAdd = await getRouterConfig(request);
      expect(afterAdd.models.some((model) => model.model_name === testModelName && !model.enabled)).toBeTruthy();

      const testPoolItem = page.locator('.pool-item').filter({ has: page.getByText(testModelName, { exact: true }) });
      await testPoolItem.getByRole('button', { name: 'edit' }).click();
      await expect(page.locator('#edit-overlay')).toHaveClass(/open/);
      await expect(page.locator('#edit-delete-btn')).toBeVisible();
      page.once('dialog', (dialog) => dialog.accept());
      await page.locator('#edit-delete-btn').click();
      await expect(page.locator('#message')).toContainText('中转站已删除');

      const afterDelete = await getRouterConfig(request);
      expect(afterDelete.models.some((model) => model.model_name === testModelName)).toBeFalsy();
      await expect(page.locator('.pool-item').filter({ has: page.getByText(testModelName, { exact: true }) })).toHaveCount(0);

      await page.locator('#save-btn').click();
      await expect(page.locator('#message')).toContainText('保存成功');

      const afterSave = await getRouterConfig(request);
      expect(afterSave.entryUpstreamId).toBe('popcorn');
      expect(afterSave.fallbackChain).toEqual([
        'gpt-5.4-gmn',
        'gpt-5.4-fastCode',
        'gpt-5.4-oauth',
      ]);
      expect(afterSave.models.some((model) => model.model_name === testModelName)).toBeFalsy();
      expect(afterSave.models.find((model) => model.model_name === 'gpt-5.4-fastCode')?.baseUrlValue).toBe(editedBaseUrl);

      await page.getByRole('button', { name: '重新读取当前配置' }).click();
      await expect(page.locator('#pending-bar')).not.toHaveClass(/is-visible/);
      await expect.poll(getActiveNames).toEqual([
        'gpt-5.4-popcorn',
        'gpt-5.4-gmn',
        'gpt-5.4-fastCode',
        'gpt-5.4-oauth',
      ]);
    } finally {
      await restoreOriginalFiles(original);
      const restored = await waitForRouterConfig(request, (config) => (
        config.entryUpstreamId === 'claudecoder'
        && JSON.stringify(config.fallbackChain) === JSON.stringify(['gpt-5.4-popcorn', 'gpt-5.4-gmn'])
        && !config.models.some((model) => model.model_name === testModelName)
      ));
      expect(restored.entryUpstreamId).toBe('claudecoder');
      expect(restored.fallbackChain).toEqual([
        'gpt-5.4-popcorn',
        'gpt-5.4-gmn',
      ]);
      expect(restored.models.some((model) => model.model_name === 'gpt-5.4-fastCode' && model.enabled)).toBeTruthy();
      expect(restored.models.some((model) => model.model_name === 'gpt-5.4-oauth' && !model.enabled)).toBeTruthy();
      expect(restored.models.some((model) => model.model_name === testModelName)).toBeFalsy();
    }
  });
});
