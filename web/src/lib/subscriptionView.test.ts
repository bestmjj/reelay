import { describe, expect, it } from 'vitest';
import type { ISubscription } from '@/api/subscriptions';
import { filterSubscriptionItems, pageSubscriptionItems } from './subscriptionView';

function make(id: number, extra: Partial<ISubscription> = {}): ISubscription {
  return {
    id, title: `动漫第${String(id).padStart(2, '0')}部`, season: 1,
    tmdbid: id + 2000, total: 110, auto: true, category: '动漫',
    start_episode: null, have: [106], missing: [1, 2, 107],
    observed: [], scan_complete: false, aired: 107, finished: null,
    discovery: { checked_at: id * 1000 },
    ...extra,
  };
}

const view = { query: '', category: '', status: 'all' as const, sort: 'recent' as const };

describe('subscriptions list is view-only and remains stable after filtering', () => {
  const rows = Array.from({ length: 29 }, (_, i) =>
    make(i + 1, i >= 22 ? { category: '电视剧' } : {}));

  it('splits 29 subscriptions into 10 / 10 / 9 without modifying their identity', () => {
    const filtered = filterSubscriptionItems(rows, view);
    expect(filtered).toHaveLength(29);
    expect(pageSubscriptionItems(filtered, 1, 10).items).toHaveLength(10);
    expect(pageSubscriptionItems(filtered, 2, 10).items).toHaveLength(10);
    expect(pageSubscriptionItems(filtered, 3, 10).items).toHaveLength(9);
    expect(pageSubscriptionItems(filtered, 3, 10)).toMatchObject({
      page: 3, pages: 3, start: 21, end: 29, total: 29,
    });
    expect(rows[0].id).toBe(1);
    expect(rows[0].have).toEqual([106]);
  });

  it('searches Chinese titles, TMDB numbers and alternate known titles', () => {
    const sample = [make(30, { title: '牧神记', aliases: ['Tales of Herding Gods'], tmdbid: 236534 }),
      make(31, { title: '吞噬星空', tmdbid: 101172 })];
    expect(filterSubscriptionItems(sample, { ...view, query: 'Herding' }).map(x => x.id)).toEqual([30]);
    expect(filterSubscriptionItems(sample, { ...view, query: '  236534  ' }).map(x => x.id)).toEqual([30]);
    expect(filterSubscriptionItems(sample, { ...view, query: '吞噬' }).map(x => x.id)).toEqual([31]);
    expect(filterSubscriptionItems(sample, { ...view, query: '不存在' })).toEqual([]);
  });

  it('filters 22 anime and 7 dramas, pages after filtering, and clamps deleted last page', () => {
    const anime = filterSubscriptionItems(rows, { ...view, category: '动漫' });
    expect(anime).toHaveLength(22);
    expect(pageSubscriptionItems(anime, 3, 10)).toMatchObject({
      page: 3, pages: 3, start: 21, end: 22,
    });
    const dramas = filterSubscriptionItems(rows, { ...view, category: '电视剧' });
    expect(dramas).toHaveLength(7);
    expect(pageSubscriptionItems(dramas, 3, 10)).toMatchObject({
      page: 1, pages: 1, start: 1, end: 7,
    });
    expect(pageSubscriptionItems(rows, 50, 20).page).toBe(2);
    expect(pageSubscriptionItems([], 2, 10)).toMatchObject({ total: 0, page: 1, pages: 1, start: 0 });
  });

  it('supports status selection and never treats a quarantined wrong episode as recorded', () => {
    const data = [make(1, { quarantined: [247], have: [], missing: [247] }),
      make(2, { finished: { date: '2026-10-01', episodes: 12 } })];
    const active = filterSubscriptionItems(data, { ...view, status: 'active' });
    expect(active.map(x => x.id)).toEqual([1]);
    expect(active[0].have).toEqual([]);
    expect(active[0].quarantined).toEqual([247]);
    expect(filterSubscriptionItems(data, { ...view, status: 'finished' }).map(x => x.id)).toEqual([2]);
    expect(filterSubscriptionItems(data, { ...view, sort: 'name' }).map(x => x.id)).toEqual([1, 2]);
  });

  it('invalid page size and page numbers fail safe, and sort is deterministic', () => {
    expect(pageSubscriptionItems(rows, -10, 999)).toMatchObject({ page: 1, size: 10 });
    expect(filterSubscriptionItems(rows, { ...view, sort: 'unrecorded' })).toHaveLength(29);
  });
});
