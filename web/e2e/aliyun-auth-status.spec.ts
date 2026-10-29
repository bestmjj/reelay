import { test, expect } from '@playwright/test';

const ROOT = 'http://127.0.0.1:28080';

async function login(page: import('@playwright/test').Page) {
  const setup = await page.request.post(`${ROOT}/api/auth/setup`, {
    data: { password: 'reelay-e2e-password-123' },
  });
  if (!setup.ok()) {
    const response = await page.request.post(`${ROOT}/api/auth/login`, {
      data: { password: 'reelay-e2e-password-123' },
    });
    expect(response.ok()).toBeTruthy();
  }
}

const verified = {
  configured: true, state: 'active', error: null,
  checked_at: 1800000000, drive_id: 'resource123',
};

test('unknown saved Aliyun session automatically checks ONCE, no repeated click needed', async ({ page }) => {
  await login(page);
  let current = { ...verified, state: 'unknown', checked_at: null as number | null };
  let pings = 0;
  await page.route('**/api/authaliyun/status', route => route.fulfill({ json: current }));
  await page.route('**/api/authaliyun/ping', route => {
    pings++;
    current = { ...verified };
    return route.fulfill({ json: current });
  });

  await page.goto(`${ROOT}/config?tab=connections`);
  await expect(page.getByText('阿里云盘连接', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '重新检测' })).toBeVisible();
  await expect.poll(() => pings).toBe(1);

  // Re-reading the status must be read-only. This used to display unknown
  // after other processes rotated the token.
  await page.getByRole('button', { name: '同步状态显示' }).click();
  await expect(page.getByRole('button', { name: '重新检测' })).toBeVisible();
  expect(pings).toBe(1);
});

test('recent persisted success is shown without a needless refresh-token exchange', async ({ page }) => {
  await login(page);
  let pings = 0;
  await page.route('**/api/authaliyun/status', route => route.fulfill({
    json: { ...verified, last_observation: true },
  }));
  await page.route('**/api/authaliyun/ping', route => {
    pings++;
    return route.fulfill({ json: verified });
  });
  await page.goto(`${ROOT}/config?tab=connections`);
  await expect(page.getByText(/最近验证成功（读取历史检测结果）/)).toBeVisible();
  expect(pings).toBe(0);
  await page.getByRole('button', { name: '重新检测' }).click();
  await expect.poll(() => pings).toBe(1);
});
