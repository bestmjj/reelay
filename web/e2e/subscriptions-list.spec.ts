import { test, expect } from '@playwright/test';

const ROOT = 'http://127.0.0.1:28080';

test('29 subscriptions have working search, filters and real pagination', async ({ page }) => {
  const setup = await page.request.post(`${ROOT}/api/auth/setup`, {
    data: { password: 'reelay-e2e-password-123' },
  });
  if (!setup.ok()) {
    const login = await page.request.post(`${ROOT}/api/auth/login`, {
      data: { password: 'reelay-e2e-password-123' },
    });
    expect(login.ok()).toBeTruthy();
  }
  const all = Array.from({ length: 29 }, (_, i) => ({
    id: i + 1, title: i === 0 ? '牧神记' : i === 1 ? '吞噬星空' :
      (i < 22 ? `动漫第${String(i + 1).padStart(2, '0')}部` : `电视剧第${i - 21}部`),
    aliases: i === 0 ? ['Tales of Herding Gods'] : [],
    tmdbid: i === 0 ? 236534 : i === 1 ? 101172 : 40000 + i,
    season: 1, total: i === 0 ? 110 : 260,
    auto: true, category: i < 22 ? '动漫' : '电视剧',
    have: i === 0 ? [106] : [], observed: [], scan_complete: false,
    missing: i === 0 ? [...Array.from({ length: 105 }, (_, n) => n + 1), 107, 108, 109, 110]
      : i === 1 ? [247] : [1, 2, 3],
    missing_truncated: false,
    quarantined: i === 1 ? [247] : [],
    start_episode: null, aired: 110,
    finished: i === 28 ? { date: '2026-10-01', episodes: 30 } : null,
    discovery: { checked_at: 1800000000 + i },
  }));
  await page.route('**/api/subscriptions', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({ json: {
      items: all, suggestions: [], inactive: [], save_dirs: {}, sources: ['@shareAliyun'],
    } });
  });

  await page.goto(`${ROOT}/subscriptions`);
  await expect(page.getByRole('heading', { name: '已订阅' })).toBeVisible();
  await expect(page.getByText('显示 1–10 条，共 29 条')).toBeVisible();
  await expect(page.getByRole('button', { name: /删除订阅《/ })).toHaveCount(10);

  await page.getByRole('button', { name: '订阅下一页' }).click();
  await expect(page.getByText('显示 11–20 条，共 29 条')).toBeVisible();
  await page.getByRole('button', { name: '订阅下一页' }).click();
  await expect(page.getByText('显示 21–29 条，共 29 条')).toBeVisible();
  await expect(page.getByRole('button', { name: '订阅下一页' })).toBeDisabled();

  await page.getByRole('combobox', { name: '订阅每页条数' }).selectOption('50');
  await expect(page.getByRole('button', { name: /删除订阅《/ })).toHaveCount(29);

  const search = page.getByRole('textbox', { name: '搜索已订阅片名' });
  await search.fill('Herding');
  await expect(page.getByText('显示 1–1 条，共 1 条')).toBeVisible();
  await expect(page.getByText('《牧神记》第 1 季')).toBeVisible();
  await expect(page.getByText(/尚未登记 109 集（E01–E105, E107–E110/)).toBeVisible();
  await search.fill('吞噬星空');
  await expect(page.getByText('错片隔离中：E247。', { exact: false })).toBeVisible();
  await expect(page.getByText('Reelay 已登记 0/260 集', { exact: false })).toBeVisible();

  await search.fill('');
  await page.getByRole('radio', { name: /动漫/ }).click();
  await expect(page.getByText('显示 1–22 条，共 22 条')).toBeVisible();
  await page.getByRole('combobox', { name: '订阅状态筛选' }).selectOption('finished');
  await expect(page.getByText(/没有匹配的订阅/)).toBeVisible();

  await page.getByRole('combobox', { name: '订阅状态筛选' }).selectOption('all');
  await page.getByRole('radio', { name: /电视剧/ }).click();
  await expect(page.getByText('显示 1–7 条，共 7 条')).toBeVisible();
  await page.getByRole('combobox', { name: '订阅每页条数' }).selectOption('10');
  await expect(page.getByRole('button', { name: '订阅下一页' })).toBeDisabled();
});
