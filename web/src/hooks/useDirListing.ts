import { useState } from 'react';
import { toast } from 'sonner';
import { storageApi, type CloudProvider, type IDirItem } from '@/api/storage';
import { useAsync } from '@/hooks/useAsync';
import { errorMessage } from '@/lib/utils';

export type Crumb = { id: string; name: string };

/**
 * Folder picker and file manager share breadcrumb/pagination behavior, but only
 * the file manager asks for ordinary files. Include provider in every cache key:
 * switching between 115 and Aliyun while at root ID "0" must never leak
 * either cloud's previously loaded entries into the other.
 */
export function useDirListing(initial: { cid: string; path: Crumb[] } = { cid: '0', path: [] },
                              provider?: CloudProvider, includeFiles = false) {
  const [target, setTarget] = useState(initial);
  const key = (provider ?? 'default') + ':' + (includeFiles ? 'files' : 'dirs') + ':' + target.cid;
  const load = (cid: string, offset: number) =>
    includeFiles && provider ? storageApi.items(cid, offset, provider) : storageApi.dirs(cid, offset, provider);
  const first = useAsync(() => load(target.cid, 0), [target.cid, provider, includeFiles], '目录查询失败');
  const [more, setMore] = useState<{ key: string; items: IDirItem[]; next: number | null } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  const page = first.stale ? undefined : first.data;
  const extra = more?.key === key ? more : null;
  const serverPath = (page?.path ?? []).filter((p) => p.id !== '0').map((p) => ({ id: p.id, name: p.name }));
  const path = page && (serverPath.length || target.cid === '0') ? serverPath : target.path;

  const open = (cid: string, crumbs: Crumb[]) => {
    setMore(null);
    setTarget({ cid, path: crumbs });
  };

  const loadMore = async () => {
    const next = extra ? extra.next : page?.next_offset ?? null;
    if (next === null || loadingMore) return;
    const requestKey = key;
    setLoadingMore(true);
    try {
      const r = await load(target.cid, next);
      setMore((prev) => ({
        key: requestKey,
        items: [...(prev?.key === requestKey ? prev.items : []), ...r.items],
        next: r.next_offset,
      }));
    } catch (e) {
      toast.error(errorMessage(e, '目录查询失败'));
    } finally {
      setLoadingMore(false);
    }
  };

  return {
    cid: target.cid,
    path,
    loading: first.loading,
    ready: page !== undefined,
    error: first.stale ? '' : first.error,
    items: page ? [...page.items, ...(extra?.items ?? [])] : [],
    next: extra ? extra.next : page?.next_offset ?? null,
    driveId: page?.drive_id,
    totalEntries: page?.total_entries,
    loadingMore,
    open,
    loadMore,
    reload: () => { setMore(null); first.reload(); },
  };
}
