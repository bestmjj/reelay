import { test, expect } from '@playwright/test';

const ROOT = 'http://127.0.0.1:28080';

test('historical share UI selects a source, enqueues once and rejects duplicate', async ({ page }) => {
  // Set up an isolated CI-only admin session. No cloud or Telegram credentials
  // exist, and this test deliberately never starts a storage worker.
  const setup = await page.request.post(`${ROOT}/api/auth/setup`, {
    data: { password: 'reelay-e2e-password-123' },
  });
  if (!setup.ok()) {
    const login = await page.request.post(`${ROOT}/api/auth/login`, {
      data: { password: 'reelay-e2e-password-123' },
    });
    expect(login.ok()).toBeTruthy();
  }

  await page.goto(`${ROOT}/subscriptions`);
  await expect(page.getByText('立即转存已有分享')).toBeVisible();
  const input = page.getByPlaceholder(/t.me\/shareAliyun/);
  await input.fill('牧神记 (2024) 第106集 https://www.alipan.com/s/e2e106');
  const button = page.getByRole('button', { name: /立即加入转存队列/ });
  await expect(button).toBeDisabled();
  const source = page.locator('#manual-resource-source');
  await source.selectOption('@shareAliyun');
  await expect(button).toBeEnabled();
  await button.click();
  await expect(page.getByText(/已创建任务/)).toBeVisible();

  const tasks = await page.request.get(`${ROOT}/api/tasks?limit=20`);
  expect(tasks.ok()).toBeTruthy();
  const body = await tasks.json();
  const records = Array.isArray(body) ? body : body.items;
  expect(records.some((task: { source: string }) => task.source === '@shareAliyun')).toBeTruthy();

  await input.fill('牧神记 (2024) 第106集 https://www.alipan.com/s/e2e106');
  await button.click();
  await expect(page.getByText(/转存队列中|提交资源失败/)).toBeVisible();
});
