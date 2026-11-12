import { test, expect } from '@playwright/test';

const ROOT = 'http://127.0.0.1:28080';

test('pending Reelay task lets user choose only the DoVi shared-folder version', async ({ page }) => {
  const setup = await page.request.post(`${ROOT}/api/auth/setup`, {
    data: { password: 'reelay-e2e-password-123' },
  });
  if (!setup.ok()) {
    const login = await page.request.post(`${ROOT}/api/auth/login`, {
      data: { password: 'reelay-e2e-password-123' },
    });
    expect(login.ok()).toBeTruthy();
  }
  let selected: string | null = null;
  let submitted: { folder_id: string; remember: boolean } | null = null;
  let saveRequests = 0;
  const parsed = {
    is_media: true, is_ad: false, title: '小城良方', media_type: 'tv',
    year: 2026, season: 1, episodes: [14], resolution: '2160p',
    reason: '更新至14集', raw_fields: {}, confidence: 1,
  };
  const item = {
    task_id: 'mock-version-1', state: 'pending_approval', source: '@shareAliyun',
    created_at: Date.now() / 1000, updated_at: Date.now() / 1000,
    title: '小城良方', year: 2026, media_type: 'tv', season: 1, episodes: [14],
    resolution: '2160p', reason: '更新至14集', size: null, error: null,
    confidence: 1, confidence_caps: [], fallback: false, tmdb_matched: true,
    poster_path: null, save_dir_alias: null, auto_dir: '动漫/国产动漫', auto_path: null,
    episode_pairs: null, share: null, preview: '更新至14集', storage_phase: null,
    has_landed: false, organize_error: null, organize_conflict: null,
    transfer_partial: null, release: null, release_tags: [], version: null,
    subscribed: false, advice: null, actions: ['approve', 'reject', 'choose_share_folder'],
  };
  const folders = [
    { folder_id: 'hdr-folder', path: 'Season 1 (HDR.Vivid)', count: 14, unrecognized: 0,
      episodes: Array.from({ length: 14 }, (_, i) => [1, i + 1]) },
    { folder_id: 'dovi-folder', path: 'Season 1 (DoVi)', count: 14, unrecognized: 0,
      episodes: Array.from({ length: 14 }, (_, i) => [1, i + 1]) },
  ];
  const preview = () => ({
    folders, selected_folder_id: selected, saved_preference: Boolean(selected), provider: 'aliyun',
  });
  await page.route('**/api/inbox?*', async (route) => {
    const state = new URL(route.request().url()).searchParams.get('state');
    await route.fulfill({ json: {
      items: state === 'pending_approval' ? [item] : [],
      save_dirs: ['默认'], save_dir_paths: { 默认: '/云盘/影视' },
    } });
  });
  await page.route('**/api/tasks/mock-version-1', async (route) => {
    await route.fulfill({ json: {
      ...item,
      context: {
        task_id: item.task_id, state: item.state,
        raw: { source: '@shareAliyun', msg_id: '123', text: '小城良方 更新至14集',
          urls: [], received_at: 0, extra: {} },
        parsed, decision: 'ask', save_dir_alias: null, save_dir_cid: null,
        result: null, source_rule: { storage: 'aliyun' }, trail: [], 
        meta: selected ? { share_selection: { folder_id: selected, path: 'Season 1 (DoVi)' } } : {},
      },
    } });
  });
  await page.route('**/api/tasks/mock-version-1/share-folders', async (route) => {
    await route.fulfill({ json: preview() });
  });
  await page.route('**/api/tasks/mock-version-1/share-folder', async (route) => {
    submitted = route.request().postDataJSON();
    selected = submitted?.folder_id ?? null;
    await route.fulfill({ json: preview() });
  });
  await page.route('**/api/tasks/mock-version-1/approve', async (route) => {
    saveRequests++;
    await route.fulfill({ json: { ok: true } });
  });

  await page.goto(`${ROOT}/inbox`);
  await page.getByRole('button', { name: '查看 小城良方 详情' }).click();
  await expect(page.getByText('选择分享版本', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '读取分享文件夹' }).click();
  await expect(page.getByRole('combobox', { name: '分享版本' })).toBeVisible();
  await page.getByRole('combobox', { name: '分享版本' }).selectOption('dovi-folder');
  await page.getByRole('button', { name: '确认这个版本' }).click();
  await expect.poll(() => submitted).toEqual({ folder_id: 'dovi-folder', remember: true });
  await expect(page.getByText(/已选择：Season 1 \(DoVi\)/)).toBeVisible();
  expect(saveRequests).toBe(0);
});
