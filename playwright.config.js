import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  timeout: 120000,
  use: {
    baseURL: process.env.LITELLM_UI_TEST_BASE_URL || 'http://127.0.0.1:4110',
    headless: true,
    viewport: { width: 1440, height: 1200 },
  },
});
