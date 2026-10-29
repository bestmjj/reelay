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


test('existing episode dialog browses cloud folders instead of asking for save-dir settings', async ({ page }) => {
  // Routes simulate only READ-ONLY cloud listings. No login to a real cloud
  // account and no persistent transfers, deletes, moves or account mutations.
  const login = await page.request.post(`${ROOT}/api/auth/login`, {
    data: { password: 'reelay-e2e-password-123' },
  });
  expect(login.ok()).toBeTruthy();
  const show = {
    id: 237, title: '牧神记', season: 1, total: 110, tmdbid: 236534,
    auto: true, start_episode: 106, have: [], observed: [], scan_complete: false,
    missing: [], aired: 106, category: '动漫', finished: null, discovery: null,
  };
  let scanned = false;
  let selected: { id: number; provider: string; cid: string } | null = null;
  await page.route('**/api/subscriptions', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({ json: { items: [show], suggestions: [], inactive: [], sources: ['@shareAliyun'] } });
  });
  await page.route('**/api/subscriptions/existing?*', async (route) => {
    await route.fulfill({ json: {
      items: scanned ? [{
        file_key: 'aliyun:resource-drive:f105', provider: 'aliyun',
        alias: '手选文件夹', drive_id: 'resource-drive',
        name: '105 4K.mp4', path: '牧神记旧文件/105 4K.mp4',
        season: 1, episode: 105, associated: false,
      }] : [],
      total: scanned ? 1 : 0, next_offset: null,
      scan_complete: scanned,
      folder: scanned ? {
        provider: 'aliyun', cid: 'existingfolder', path: '牧神记旧文件',
        drive_id: 'resource-drive', skipped: 0,
        snapshot: { complete: true, has_more: false, file_count: 1, errors: [] },
      } : null,
      subscription: { id: 237, title: '牧神记', season: 1, tmdbid: 236534 },
    } });
  });
  await page.route('**/api/subscriptions/existing/scan-folder', async (route) => {
    selected = route.request().postDataJSON();
    scanned = true;
    await route.fulfill({ json: {
      ok: true, provider: 'aliyun', path: '牧神记旧文件',
      file_count: 1, eligible: 1, skipped: 0, complete: true, has_more: false, errors: [],
    } });
  });
  await page.route('**/api/storage/dirs?*', async (route) => {
    const cid = new URL(route.request().url()).searchParams.get('cid');
    const root = { id: '0', name: '根目录' };
    const folder = { id: 'existingfolder', name: '牧神记旧文件' };
    await route.fulfill({ json: {
      items: cid === '0' ? [folder] : [], path: cid === '0' ? [root] : [root, folder],
      next_offset: null, via: 'aliyun', drive_id: 'resource-drive', total_entries: cid === '0' ? 1 : 0,
    } });
  });

  await page.goto(`${ROOT}/subscriptions`);
  await page.getByRole('button', { name: '关联旧集' }).click();
  await expect(page.getByRole('button', { name: '选择已有视频文件夹' })).toBeVisible();
  await page.getByRole('button', { name: '选择已有视频文件夹' }).click();
  await expect(page.getByText('选择网盘目录')).toBeVisible();
  await page.getByRole('button', { name: '牧神记旧文件' }).click();
  await page.getByRole('button', { name: '选择当前目录' }).click();
  await expect.poll(() => selected).toEqual({ id: 237, provider: 'aliyun', cid: 'existingfolder' });
  await expect(page.getByText(/105 4K\.mp4/).first()).toBeVisible();
  await expect(page.getByText(/无需去设置保存目录/)).toBeVisible();
});
