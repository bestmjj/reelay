import { test, expect } from '@playwright/test';

const ROOT = 'http://127.0.0.1:28080';

test('approved but deduplicated show is not falsely displayed as user ignored', async ({ page }) => {
  const setup = await page.request.post(`${ROOT}/api/auth/setup`, {
    data: { password: 'reelay-e2e-password-123' },
  });
  if (!setup.ok()) {
    const login = await page.request.post(`${ROOT}/api/auth/login`, {
      data: { password: 'reelay-e2e-password-123' },
    });
    expect(login.ok()).toBeTruthy();
  }
  const reason = '分享内第 1 季的 14 集均已入库；本次未复制任何文件；原有版本可能不是 DoVi';
  const item = {
    task_id: 'mock-duplicate-1', state: 'skipped', source: '@shareAliyun',
    msg_id: 'test1', title: '小城良方', year: 2026, media_type: 'tv',
    season: 1, episodes: Array.from({ length: 14 }, (_, i) => i + 1),
    episode_pairs: null, resolution: '2160p', size: null,
    skip_kind: 'duplicate', skip_reason: reason, error: null,
    retries: 0, created_at: 1761500000, updated_at: 1761500100,
    poster_path: null, save_dir_alias: '剧集',
  };
  await page.route('**/api/tasks?*', async (route) => {
    await route.fulfill({ json: { items: [item] } });
  });
  await page.route('**/api/tasks/mock-duplicate-1', async (route) => {
    await route.fulfill({ json: {
      ...item, actions: [], source_audit: { available: false },
      context: {
        task_id: item.task_id, state: item.state,
        raw: { source: '@shareAliyun', msg_id: 'test1',
          text: '小城良方 更新至14集', urls: [], received_at: 1761500000, extra: {} },
        parsed: { is_media: true, media_type: 'tv', title: '小城良方', year: 2026,
          season: 1, episodes: item.episodes, resolution: '2160p',
          confidence: 1, raw_fields: {} },
        decision: 'ask', save_dir_alias: '剧集', save_dir_cid: null,
        source_rule: { storage: 'aliyun' }, result: null,
        meta: { task: { user_approved: true,
          skip: { kind: 'duplicate', reason } } },
        trail: ['01:00:00 管理端批准转存', '01:00:01 安全去重，未发送转存请求'],
      },
    } });
  });

  await page.goto(`${ROOT}/tasks`);
  await expect(page.getByText('重复跳过')).toBeVisible();
  await expect(page.getByText(/没有再次转存/)).toBeVisible();
  await page.getByRole('button', { name: /小城良方/ }).first().click();
  await expect(page.getByText('安全去重：没有进行新的转存')).toBeVisible();
  await expect(page.getByText(reason, { exact: true })).toBeVisible();
});
