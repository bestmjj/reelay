import { expect, test } from '@playwright/test';

const ROOT = 'http://127.0.0.1:28080';

test('DONE task verifies live Aliyun files, selectively queues missing E11 only', async ({ page }) => {
  const setup = await page.request.post(`${ROOT}/api/auth/setup`, {
    data: { password: 'reelay-e2e-password-123' },
  });
  if (!setup.ok()) {
    const login = await page.request.post(`${ROOT}/api/auth/login`, {
      data: { password: 'reelay-e2e-password-123' },
    });
    expect(login.ok()).toBeTruthy();
  }
  let queued: string[] = [];
  let cloudWriteRequests = 0;
  const item = {
    task_id: 'repair-e11', state: 'done', source: '@shareAliyun',
    msg_id: '11', title: 'K狂王', year: 2026, media_type: 'anime',
    season: 1, episodes: [10, 11], resolution: '2160p', error: null,
    updated_at: Date.now() / 1000, created_at: Date.now() / 1000,
  };
  await page.route('**/api/tasks?*', async route => {
    await route.fulfill({ json: { items: [item] } });
  });
  await page.route('**/api/tasks/repair-e11', async route => {
    await route.fulfill({ json: {
      ...item, actions: ['repair_missing'],
      context: {
        task_id: item.task_id, state: item.state,
        raw: { source: '@shareAliyun', msg_id: '11',
          text: 'K狂王 10-11', urls: [], received_at: 0, extra: {} },
        parsed: { title: 'K狂王', media_type: 'anime', year: 2026,
          season: 1, episodes: [10, 11], resolution: '2160p', raw_fields: {} },
        decision: 'save', source_rule: { storage: 'aliyun' },
        save_dir_alias: '动漫', result: { storage_verified: true },
        trail: [], meta: { organize: { plan: [
          { target_dir: '动漫/K狂王/Season 01', target_name: 'K狂王 - S01E10.mp4' },
          { target_dir: '动漫/K狂王/Season 01', target_name: 'K狂王 - S01E11.mp4' },
        ] } },
      },
    } });
  });
  await page.route('**/api/tasks/repair-e11/repair-check', async route => {
    await route.fulfill({ json: {
      source_task_id: item.task_id, base_path: '/影视',
      total: 2, uncertain: [], present: [{
        old_id: 'old10', season: 1, episode: 10,
        source_name: '10 4K.mp4', target_dir: '动漫/K狂王/Season 01',
        target_name: 'K狂王 - S01E10.mp4', note: '原文件仍在云盘',
      }],
      missing: [{
        old_id: 'old11', season: 1, episode: 11,
        source_name: '11 4K.mp4', target_dir: '动漫/K狂王/Season 01',
        target_name: 'K狂王 - S01E11.mp4',
      }],
    } });
  });
  await page.route('**/api/tasks/repair-e11/repair-missing', async route => {
    const body = route.request().postDataJSON() as { missing_ids: string[] };
    queued = body.missing_ids;
    await route.fulfill({ json: { ok: true, task_id: 'repair-child-e11', file_count: 1 } });
  });
  await page.route('**/api/storage/**', async route => {
    cloudWriteRequests++;
    await route.fulfill({ status: 403 });
  });
  await page.goto(`${ROOT}/tasks`);
  await page.getByRole('button', { name: /K狂王/ }).first().click();
  await page.getByRole('button', { name: '核验已入库文件' }).click();
  await expect(page.getByText('补回云盘里丢失的文件')).toBeVisible();
  await expect(page.getByText(/已存在 1 个，确认丢失 1 个/)).toBeVisible();
  await expect(page.getByText('K狂王 - S01E11.mp4', { exact: true })).toBeVisible();
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: '确认补转选中的 1 个缺失文件' }).click();
  await expect.poll(() => queued).toEqual(['old11']);
  expect(cloudWriteRequests).toBe(0);
});
