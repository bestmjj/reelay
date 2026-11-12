import { test, expect } from '@playwright/test';

const ROOT = 'http://127.0.0.1:28080';

test('completed E247 displays its 4K.mp4 source and quarantines wrong dedup without cloud writes', async ({ page }) => {
  const setup = await page.request.post(`${ROOT}/api/auth/setup`, {
    data: { password: 'reelay-e2e-password-123' },
  });
  if (!setup.ok()) {
    const login = await page.request.post(`${ROOT}/api/auth/login`, {
      data: { password: 'reelay-e2e-password-123' },
    });
    expect(login.ok()).toBeTruthy();
  }
  let quarantined = false;
  let resolved = false;
  let cloudWriteRequests = 0;
  const item = {
    task_id: 'wrong-e247', state: 'done', source: '@shareAliyun',
    msg_id: '247', title: '吞噬星空', year: 2020, media_type: 'tv',
    season: 1, episodes: [247], resolution: '2160p', error: null,
    updated_at: Date.now() / 1000, created_at: Date.now() / 1000,
  };
  const audit = () => ({
    available: true, suspect: true, content_verified: false,
    note: '已落地不等于内容正确', entries: [{
      file_id: 'wrong-source', source_name: '4K.mp4',
      source_path: '洛DA/4K.mp4', size: 4040000000,
      share_folder_id: 'unrelated-folder', season: 1,
      episode: 247, filename_episode: null,
      issue: '原始分享文件名没有可核验的集号，可能被错误地映射到目标集',
    }],
    quarantine: quarantined ? {
      active: !resolved,
      reason: '4K.mp4 属于别的电影，不是吞噬星空 E247',
      source_name: '4K.mp4', source_path: '洛DA',
      source_size: 4040000000, pairs: [[1, 247]],
      created_at: 1, resolved_at: resolved ? 2 : null,
    } : null,
  });
  await page.route('**/api/tasks?*', async (route) => {
    await route.fulfill({ json: { items: [item] } });
  });
  await page.route('**/api/tasks/wrong-e247', async (route) => {
    await route.fulfill({ json: {
      ...item,
      context: {
        task_id: item.task_id, state: item.state,
        raw: { source: '@shareAliyun', msg_id: '247',
          text: '吞噬星空 更新至247集', urls: [], received_at: 0, extra: {} },
        parsed: { title: '吞噬星空', media_type: 'tv', year: 2020,
          season: 1, episodes: [247], resolution: '2160p', raw_fields: {} },
        decision: 'save', source_rule: { storage: 'aliyun' },
        save_dir_alias: '动漫', result: { storage_verified: true },
        trail: [], meta: { organize: { plan: [{
          target_dir: '国产动漫/吞噬星空 (2020)/Season 01',
          target_name: '吞噬星空 - S01E247 - 第 247 集 - 2160p.mp4',
        }] } },
      },
      source_audit: audit(),
      actions: !quarantined ? ['quarantine_wrong_source'] :
        resolved ? [] : ['resolve_quarantine'],
    } });
  });
  await page.route('**/api/tasks/wrong-e247/quarantine-wrong-copy', async (route) => {
    const data = route.request().postDataJSON();
    expect(data.reason).toContain('4K.mp4');
    quarantined = true;
    await route.fulfill({ json: { active: true } });
  });
  await page.route('**/api/tasks/wrong-e247/resolve-wrong-copy', async (route) => {
    const data = route.request().postDataJSON();
    expect(data.confirmation).toBe('我已核对并处理错片');
    resolved = true;
    await route.fulfill({ json: { active: false } });
  });
  await page.route('**/api/storage/receive*', async (route) => {
    cloudWriteRequests++;
    await route.fulfill({ status: 403 });
  });
  await page.goto(`${ROOT}/tasks`);
  await page.getByRole('button', { name: /吞噬星空/ }).first().click();
  await expect(page.getByText('实际转存来源核验')).toBeVisible();
  await expect(page.getByText('洛DA/4K.mp4')).toBeVisible();
  await expect(page.getByText(/没有可核验的集号/)).toBeVisible();
  await page.getByPlaceholder(/例如：E247 实际取自/).fill('4K.mp4 属于别的电影，不是吞噬星空 E247');
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: '标记错片并暂停自动转存' }).click();
  await expect.poll(() => quarantined).toBe(true);
  await expect(page.getByText(/此任务已标记错片/)).toBeVisible();
  expect(cloudWriteRequests).toBe(0);
  page.once('dialog', (dialog) => dialog.accept('我已核对并处理错片'));
  await page.getByRole('button', { name: '人工处理完成，解除隔离' }).click();
  await expect.poll(() => resolved).toBe(true);
  expect(cloudWriteRequests).toBe(0);
});
