import { expect, test } from '@playwright/test';

const ROOT = 'http://127.0.0.1:28080';

test('operator selects only missing episodes without changing global dedup', async ({ page }) => {
  const setup = await page.request.post(`${ROOT}/api/auth/setup`, {
    data: { password: 'reelay-e2e-password-123' },
  });
  if (!setup.ok()) {
    const login = await page.request.post(`${ROOT}/api/auth/login`, {
      data: { password: 'reelay-e2e-password-123' },
    });
    expect(login.ok()).toBeTruthy();
  }

  let selected: number[] = [];
  let savedRequests = 0;
  const item = {
    task_id: 'missing-episodes', state: 'pending_approval',
    source: '@shareAliyun', created_at: 1800000000, updated_at: 1800000000,
    title: '尚公主', year: 2026, media_type: 'tv', season: 1,
    episodes: [11], resolution: '2160p', reason: 'S01 E11',
    confidence: 1, confidence_caps: [], fallback: false, tmdb_matched: true,
    poster_path: null, size: null, error: null,
    save_dir_alias: '剧集', auto_dir: '剧集', auto_path: '/影视/连续剧',
    episode_pairs: null, share: null, preview: 'E11',
    storage_phase: null, has_landed: false, organize_error: null,
    organize_conflict: null, transfer_partial: null, release: null,
    release_tags: [], version: null, subscribed: false, advice: null,
    actions: ['approve', 'correct', 'choose_share_folder', 'select_episodes'],
  };
  const detail = () => ({
    ...item,
    context: {
      task_id: item.task_id, state: item.state,
      raw: {
        source: '@shareAliyun', msg_id: 'missing-episodes',
        text: '尚公主 更新 E11 https://www.alipan.com/s/test',
        urls: [], received_at: 1800000000, extra: {},
      },
      parsed: {
        is_media: true, is_ad: false, title: '尚公主',
        media_type: 'tv', year: 2026, season: 1,
        episodes: [11], resolution: '2160p', confidence: 1,
        share_code: 'test', raw_fields: { share_provider: 'aliyun' },
      },
      decision: 'ask', source_rule: { storage: 'aliyun', episodes_from: 'share' },
      save_dir_alias: '剧集', save_dir_cid: null, result: null,
      meta: selected.length ? { share_selection: { only_episodes: selected } } : {},
      trail: [],
    },
  });
  await page.route('**/api/inbox?*', async route => {
    const state = new URL(route.request().url()).searchParams.get('state');
    await route.fulfill({ json: {
      items: state === 'pending_approval' ? [item] : [],
      save_dirs: ['剧集'], save_dir_paths: { 剧集: '/影视/连续剧' },
    } });
  });
  await page.route('**/api/tasks/missing-episodes', async route => {
    await route.fulfill({ json: detail() });
  });
  await page.route('**/api/tasks/missing-episodes/episode-selection', async route => {
    const body = route.request().postDataJSON() as { episodes: number[] };
    selected = body.episodes;
    savedRequests += 1;
    await route.fulfill({ json: detail() });
  });

  await page.goto(`${ROOT}/inbox`);
  await page.getByRole('button', { name: '查看 尚公主 详情' }).click();
  await expect(page.getByText('本次只补指定集数（可选）')).toBeVisible();
  const input = page.getByRole('textbox', { name: '本次指定补集' });
  await input.fill('11, 13-15');
  await page.getByRole('button', { name: '保存本次补集范围' }).click();
  await expect.poll(() => selected.join(',')).toBe('11,13,14,15');
  await expect(page.getByText(/已设置：仅处理第 11、13、14、15 集/)).toBeVisible();

  await input.fill('');
  await page.getByRole('button', { name: '保存本次补集范围' }).click();
  await expect.poll(() => selected.length).toBe(0);
  await expect(page.getByText('当前：自动补齐分享中尚未入库的剧集')).toBeVisible();
  expect(savedRequests).toBe(2); // local backend mock, no cloud writes
});
