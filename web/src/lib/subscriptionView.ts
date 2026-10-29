/** Pure subscription-list filtering/paging: never changes a source subscription. */
import type { ISubscription } from '@/api/subscriptions';

export type SubscriptionStatusFilter = 'all' | 'active' | 'finished';
export type SubscriptionSort = 'recent' | 'name' | 'unrecorded';

export interface SubscriptionListFilters {
  query: string;
  category: string;
  status: SubscriptionStatusFilter;
  sort: SubscriptionSort;
}

const normalized = (text: string) => text.normalize('NFKC').trim().toLocaleLowerCase();

export function filterSubscriptionItems(items: ISubscription[], filter: SubscriptionListFilters): ISubscription[] {
  const term = normalized(filter.query);
  return items.filter((item) => {
    if (filter.category && item.category !== filter.category) return false;
    if (filter.status === 'active' && item.finished) return false;
    if (filter.status === 'finished' && !item.finished) return false;
    if (!term) return true;
    return [item.title, ...(item.aliases ?? []), item.tmdbid?.toString() ?? '']
      .some((name) => normalized(name).includes(term));
  }).sort((a, b) => {
    // Always put finished seasons below active subscriptions.
    const finished = Number(Boolean(a.finished)) - Number(Boolean(b.finished));
    if (finished) return finished;
    if (filter.sort === 'name') return a.title.localeCompare(b.title, 'zh-CN') || a.id - b.id;
    if (filter.sort === 'unrecorded') {
      const count = (b.missing?.length ?? 0) - (a.missing?.length ?? 0);
      if (count) return count;
    }
    // The latest discovery check is meaningful only within one subscription;
    // unknown checks go last. The ID tie-break prevents rows jumping around.
    const recent = (b.discovery?.checked_at ?? 0) - (a.discovery?.checked_at ?? 0);
    return recent || a.id - b.id;
  });
}

export function pageSubscriptionItems<T>(items: T[], requestedPage: number, pageSize: number) {
  const size = [10, 20, 50].includes(pageSize) ? pageSize : 10;
  const pages = Math.max(1, Math.ceil(items.length / size));
  const page = Math.min(Math.max(1, Math.floor(requestedPage) || 1), pages);
  return {
    items: items.slice((page - 1) * size, page * size),
    page, pages, size,
    start: items.length ? (page - 1) * size + 1 : 0,
    end: Math.min(page * size, items.length),
    total: items.length,
  };
}
