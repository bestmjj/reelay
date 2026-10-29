import { expect, test } from '@playwright/test';

const ROOT = 'http://127.0.0.1:28080';

test('corrected manual E247 can select exact 1.62GB source, not unnumbered 4GB', async ({ page }) => {
  const setup = await page.request.post(`${ROOT}/api/auth/setup`, {
    data: { password: 'reelay-e2e-password-123' },
  });
  if (!setup.ok()) {
    const login = await page.request.post(`${ROOT}/api/auth/login`, {
      data: { password: 'reelay-e2e-password-123' },
    });
    expect(login.ok()).toBeTruthy();
  }

  let selectedFile: string | null = null;
  let approvedTarget: Record<string, string> | null = null;
  let cloudWrites = 0;
  const task = {
    task_id: 'manual-e247', state: 'pending_approval', source: '@shareAliyun',
    created_at: 1800000000, updated_at: 1800000000,
    title: '吞噬星空', year: 2020, media_type: 'anime', season: 1,
    episodes: [247], resolution: '2160p', reason: '人工核对第247集',
    confidence: 1, confidence_caps: [], fallback: false,
    tmdb_matched: true, poster_path: null, size: null, error: null,
    save_dir_alias: null, auto_dir: '动漫', auto_path: '影视/动漫/Dad',
    episode_pairs: null, share: null, preview: '第247集', storage_phase: null,
    has_landed: false, organize_error: null, organize_conflict: null,
    transfer_partial: null, release: null, release_tags: [],
    version: null, subscribed: false, advice: null,
    actions: ['approve', 'correct', 'choose_share_folder'],
  };
  const files = [
    { file_id: 'f247', name: '247 4K.mp4', path: '', size: 1620000000,
      episode: 247, selectable: true },
    { file_id: 'wrong4gb', name: '4K.mp4', path: '洛DA', size: 4040000000,
      episode: null, selectable: false },
  ];
  const folderResponse = () => ({
    folders: [
      { folder_id: '__share_root_direct__', path: '分享根目录（仅当前层）',
        count: 1, direct_count: 1, episodes: [[1, 247]], unrecognized: 0 },
      { folder_id: 'wrong-folder', path: '洛DA', count: 1, direct_count: 1,
        episodes: [], unrecognized: 1 },
    ],
    files, selected_folder_id: null, selected_file_id: selectedFile,
    saved_preference: false, provider: 'aliyun',
  });

  await page.route('**/api/inbox?*', async route => {
    const state = new URL(route.request().url()).searchParams.get('state');
    await route.fulfill({ json: {
      items: state === 'pending_approval' ? [task] : [],
      save_dirs: ['动漫'], save_dir_paths: { 动漫: '/影视/动漫/Dad' },
    } });
  });
  await page.route('**/api/tasks/manual-e247', async route => {
    await route.fulfill({ json: {
      ...task,
      context: {
        task_id: task.task_id, state: task.state,
        raw: {
          source: '@shareAliyun', msg_id: 'manual-e247',
          text: 'https://www.alipan.com/s/gsQB8YFoLZQ 选择分享根目录中的 247',
          urls: [], received_at: 1800000000, extra: { manual_import: true },
        },
        parsed: {
          is_media: true, is_ad: false, title: '吞噬星空',
          media_type: 'anime', year: 2020, season: 1, episodes: [247],
          resolution: '2160p', confidence: 1,
          share_code: 'gsQB8YFoLZQ',
          raw_fields: { share_provider: 'aliyun' },
          reason: '人工修正',
        },
        decision: 'ask', save_dir_alias: null, save_dir_cid: null,
        result: null, source_rule: { storage: 'aliyun', episodes_from: 'message' },
        trail: [], meta: selectedFile ? {
          share_selection: { file_id: selectedFile, file_name: '247 4K.mp4',
            file_path: '', file_size: 1620000000 },
        } : {},
      },
    } });
  });
  await page.route('**/api/tasks/manual-e247/share-folders', async route => {
    await route.fulfill({ json: folderResponse() });
  });
  await page.route('**/api/tasks/manual-e247/share-file', async route => {
    const body = route.request().postDataJSON();
    if (body.file_id !== 'f247') {
      await route.fulfill({ status: 409, json: { detail: '不是目标 E247' } });
      return;
    }
    selectedFile = body.file_id;
    await route.fulfill({ json: folderResponse() });
  });
  const folders = [
    { id: 'root-media', name: '影视' },
    { id: 'root-anime', name: '动漫' },
    { id: 'root-dad', name: 'Dad' },
    { id: 'existing-show', name: '吞噬星空' },
    { id: 'existing-season', name: 'Season 01' },
  ];
  await page.route('**/api/storage/dirs?*', async route => {
    const cid = new URL(route.request().url()).searchParams.get('cid') ?? '0';
    const i = folders.findIndex(x => x.id === cid);
    const crumbs = i >= 0 ? folders.slice(0, i + 1) : [];
    await route.fulfill({ json: {
      items: folders[i + 1] ? [{ ...folders[i + 1], is_dir: true }] : [],
      path: [{ id: '0', name: '根目录' }, ...crumbs],
      next_offset: null, total_entries: folders[i + 1] ? 1 : 0,
      drive_id: 'resource-drive-e247', via: 'aliyun',
    } });
  });
  await page.route('**/api/tasks/manual-e247/approve', async route => {
    cloudWrites++;
    approvedTarget = route.request().postDataJSON() as Record<string, string>;
    await route.fulfill({ json: { ok: true } });
  });

  await page.goto(`${ROOT}/inbox`);
  await page.getByRole('button', { name: '查看 吞噬星空 详情' }).click();
  await expect(page.getByText('选择分享源文件或文件夹', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '读取分享文件夹' }).click();
  const picker = page.getByRole('combobox', { name: '精确选择分享源文件' });
  await expect(picker).toBeVisible();
  await expect(picker.locator('option[value="wrong4gb"]')).toBeDisabled();
  await picker.selectOption('f247');
  await page.getByRole('button', { name: '确认只转存这个源文件' }).click();
  await expect.poll(() => selectedFile).toBe('f247');
  await expect(page.getByText(/已锁定单集源文件：分享根目录\/247 4K.mp4/)).toBeVisible();
  expect(cloudWrites).toBe(0);
  await page.getByRole('button', { name: '浏览阿里云盘选择保存位置' }).click();
  const dialog = page.getByRole('dialog', { name: '选择网盘目录' });
  await expect(dialog.getByRole('button', { name: '115 网盘' })).toHaveCount(0);
  for (const name of ['影视', '动漫', 'Dad', '吞噬星空', 'Season 01']) {
    await dialog.getByRole('button', { name, exact: true }).last().click();
  }
  await dialog.getByRole('button', { name: '选择当前目录' }).click();
  await expect(page.getByText(/已选择：\/影视\/动漫\/Dad\/吞噬星空\/Season 01/)).toBeVisible();
  await page.getByRole('button', { name: '确认转存' }).click();
  expect(approvedTarget).toEqual({
    existing_folder_id: 'existing-season',
    existing_drive_id: 'resource-drive-e247',
    destination_mode: 'exact',
  });
  expect(cloudWrites).toBe(1); // mocked HTTP approval, never a cloud copy
});
