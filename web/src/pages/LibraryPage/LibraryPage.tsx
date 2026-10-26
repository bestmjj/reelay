import { FolderTree, LayoutGrid, LibraryBig, List, RefreshCw, Search } from 'lucide-react';
import { useMemo, useState } from 'react';
import { libraryApi, type ITitleGroup } from '@/api/library';
import { configApi } from '@/api/config';
import { Button } from '@/components/ui/button';
import { toast } from 'sonner';
import { errorMessage } from '@/lib/utils';
import { EmptyState } from '@/components/app/EmptyState';
import { ErrorState } from '@/components/app/ErrorState';
import { MediaCover } from '@/components/app/MediaCover';
import { Page } from '@/components/app/Page';
import { Segmented } from '@/components/app/Segmented';
import { Select } from '@/components/app/Select';
import { Sheet } from '@/components/app/Sheet';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { useAsync } from '@/hooks/useAsync';
import { useDebounced } from '@/hooks/useDebounced';
import { compactEpisodes, fullTime, mediaTypeLabel, relativeTime, seasonsLabel } from '@/lib/format';
import { MEDIA_TYPE_LABELS } from '@/lib/status';

type Sort = 'recent' | 'title' | 'size';
type View = 'grid' | 'list';
type Origin = 'transfers' | 'scanned';
const ORIGINS = [{ value: 'transfers', label: 'Reelay 已入库' }, { value: 'scanned', label: '网盘已有文件' }];

const TYPES = [
  { value: '', label: '全部' },
  ...['tv', 'movie', 'anime'].map((value) => ({ value, label: MEDIA_TYPE_LABELS[value] })),
];
const SORTS: Array<{ value: Sort; label: string }> = [
  { value: 'recent', label: '最近更新' },
  { value: 'title', label: '按片名' },
  { value: 'size', label: '按体积' },
];
const VIEWS = [
  { value: 'grid' as const, title: '海报墙', label: <><LayoutGrid className="size-4" /><span className="sr-only">海报墙</span></> },
  { value: 'list' as const, title: '列表', label: <><List className="size-4" /><span className="sr-only">列表</span></> },
];

/** 同名作品（如翻拍）按 TMDB 编号区分。 */
const groupKey = (group: ITitleGroup) => `${group.origin ?? 'transfer'}:${group.provider ?? ''}:${group.save_dir_alias ?? ''}:${group.media_type ?? ''}:${group.tmdbid ?? group.title}`;

function gbText(bytes: number): string {
  const gb = bytes / 1024 ** 3;
  return gb >= 100 ? `${gb.toFixed(0)} GB` : `${gb.toFixed(1)} GB`;
}

/** 已收录集数：跨多季时按（季, 集）计，否则各季的同号集会被当成一集。 */
function episodeCount(group: ITitleGroup): number {
  return group.episode_pairs?.length ?? group.episodes.length;
}

/** 按季归并已收录的集；单季作品只有一组（季为 null）。 */
function seasonGroups(group: ITitleGroup): Array<[number | null, number[]]> {
  if (!group.episode_pairs) return [[null, group.episodes]];
  const bySeason = new Map<number, number[]>();
  for (const [season, ep] of group.episode_pairs) bySeason.set(season, [...(bySeason.get(season) ?? []), ep]);
  return [...bySeason].sort((a, b) => a[0] - b[0]).map(([season, eps]) => [season, eps.sort((a, b) => a - b)]);
}

/** 海报角标：剧集显示已收录集数，电影不显示。 */
function summary(group: ITitleGroup): string {
  if (group.media_type === 'movie') return group.year ? String(group.year) : '电影';
  const eps = episodeCount(group);
  return [group.year, eps ? `${eps} 集` : ''].filter(Boolean).join(' · ');
}

function GroupSheet({ group, onClose }: { group: ITitleGroup | null; onClose: () => void }) {
  return (
    <Sheet
      open={group !== null}
      onOpenChange={(open) => { if (!open) onClose(); }}
      title={group?.title ?? ''}
      description={group ? `${group.origin === 'scanned' ? '网盘扫描发现 · ' : 'Reelay 已入库 · '}${mediaTypeLabel(group.media_type)}${group.year ? ` · ${group.year}` : ''}` : undefined}
    >
      {group ? (
        <div className="space-y-6">
          <div className="flex gap-4">
            <MediaCover title={group.title} posterPath={group.poster_path} mediaType={group.media_type} className="w-28 shrink-0" />
            <dl className="grid content-start gap-x-4 gap-y-2 text-sm [grid-template-columns:auto_1fr]">
              <dt className="text-muted-foreground">保存目录</dt><dd>{group.save_dir_alias ?? '—'}</dd>
              {group.resolution ? <><dt className="text-muted-foreground">清晰度</dt><dd>{group.resolution}</dd></> : null}
              <dt className="text-muted-foreground">估算体积</dt><dd className="tabular-nums">{gbText(group.bytes)}</dd>
              <dt className="text-muted-foreground">{group.origin === 'scanned' ? '扫描视频' : '转存次数'}</dt>
              <dd className="tabular-nums">{group.origin === 'scanned' ? (group.file_count ?? 0) : group.tasks}</dd>
              <dt className="text-muted-foreground">{group.origin === 'scanned' ? '扫描时间' : '最近入库'}</dt>
              <dd title={fullTime(group.updated_at)}>{relativeTime(group.updated_at)}</dd>
            </dl>
          </div>
          {group.release.length ? (
            <div className="flex flex-wrap gap-1">
              {group.release.map((tag) => (
                <span key={tag} className="rounded bg-muted px-1.5 text-[11px] leading-[18px] text-muted-foreground">{tag}</span>
              ))}
            </div>
          ) : null}
          {group.media_type !== 'movie' && group.episodes.length ? (
            <section className="space-y-2">
              <h3 className="text-[13px] font-semibold">{group.origin === 'scanned' ? '扫描发现' : '已收录'} {episodeCount(group)} 集</h3>
              {seasonGroups(group).map(([season, eps]) => (
                <div key={season ?? 'all'} className="flex items-start gap-2">
                  {season !== null ? (
                    <span className="w-9 shrink-0 pt-0.5 text-xs font-medium tabular-nums text-muted-foreground">S{String(season).padStart(2, '0')}</span>
                  ) : null}
                  <div className="flex flex-wrap gap-1.5">
                    {eps.map((ep) => (
                      <span key={ep} className="min-w-9 rounded-md bg-primary/10 px-1.5 py-0.5 text-center text-xs font-medium tabular-nums text-primary">
                        {String(ep).padStart(2, '0')}
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </section>
          ) : null}
          <section className="space-y-2">
            <h3 className="flex items-center gap-1.5 text-[13px] font-semibold"><FolderTree className="size-3.5 text-muted-foreground" />文件</h3>
            {group.files.length ? (
              <ul className="space-y-1 rounded-lg border bg-muted/40 p-3 font-mono text-xs leading-5">
                {group.files.map((path) => {
                  const cut = path.lastIndexOf('/');
                  return (
                    <li key={path} className="break-all">
                      {cut > 0 ? <span className="text-muted-foreground">{path.slice(0, cut + 1)}</span> : null}
                      {path.slice(cut + 1)}
                    </li>
                  );
                })}
              </ul>
            ) : <p className="text-xs text-muted-foreground">{group.origin === 'scanned' ? '尚无扫描路径记录。' : '没有整理记录。'}</p>}
          </section>
        </div>
      ) : null}
    </Sheet>
  );
}

export default function LibraryPage() {
  const [query, setQuery] = useState('');
  const [origin, setOrigin] = useState<Origin>('transfers');
  const [scanning, setScanning] = useState(false);
  const submitted = useDebounced(query);
  const [type, setType] = useState('');
  const [directory, setDirectory] = useState('');
  const [sort, setSort] = useState<Sort>('recent');
  const [view, setView] = useState<View>(() => (localStorage.getItem('reelay-library-view') as View) || 'grid');
  const [open, setOpen] = useState<ITitleGroup | null>(null);

  // Directory aliases are configuration, not media types: show empty aliases too.
  const config = useAsync(() => configApi.get(), [], '读取保存目录失败');
  const directoryOptions = useMemo(() => {
    const aliases = config.data?.config.save_dirs;
    const names = aliases && typeof aliases === 'object' && !Array.isArray(aliases)
      ? Object.keys(aliases)
      : [];
    return [{ value: '', label: '全部保存目录' }, ...names.map((value) => ({ value, label: value }))];
  }, [config.data]);

  const library = useAsync(
    () => libraryApi.list(submitted || undefined, type || undefined, directory || undefined),
    [submitted, type, directory], '读取媒体库失败');
  const scanned = useAsync(
    () => libraryApi.scanned(submitted || undefined, type || undefined, directory || undefined),
    [submitted, type, directory], '读取网盘已有文件索引失败');
  const result = origin === 'scanned'
    ? (scanned.stale ? undefined : scanned.data)
    : (library.stale ? undefined : library.data);
  const active = origin === 'scanned' ? scanned : library;
  const scanNow = async () => {
    setScanning(true);
    try {
      const snapshot = await libraryApi.scan();
      scanned.reload();
      toast.success(snapshot.complete
        ? `扫描完成：发现 ${snapshot.file_count} 个视频文件`
        : `扫描获得 ${snapshot.file_count} 个视频文件；请查看未完成提示`);
    } catch (e) {
      toast.error(errorMessage(e, '扫描失败，保留上次索引'));
    } finally {
      setScanning(false);
    }
  };

  const groups = useMemo(() => {
    const list = [...(result?.groups ?? [])];
    if (sort === 'title') list.sort((a, b) => a.title.localeCompare(b.title, 'zh-CN'));
    else if (sort === 'size') list.sort((a, b) => b.bytes - a.bytes);
    else list.sort((a, b) => b.updated_at - a.updated_at);
    return list;
  }, [result, sort]);

  const switchView = (next: View) => { setView(next); localStorage.setItem('reelay-library-view', next); };

  return (
    <Page
      title="媒体库"
      description="Reelay 已完成的转存与网盘原有文件分开统计；扫描不会移动、删除或转存任何文件。"
      actions={result ? (
        <span className="text-sm text-muted-foreground">
          <b className="font-semibold tabular-nums text-foreground">{result.groups.length}</b> 部 ·
          约 <b className="font-semibold tabular-nums text-foreground">{result.estimated_gb}</b> GB
        </span>
      ) : null}
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <Segmented label="媒体库来源" value={origin} onChange={(v) => setOrigin(v as Origin)}
          options={ORIGINS} className="shrink-0" />
        {origin === 'scanned' ? (
          <Button size="sm" variant="outline" disabled={scanning} onClick={() => void scanNow()}>
            <RefreshCw className={scanning ? 'animate-spin' : ''} />
            {scanning ? '正在扫描' : '扫描已配置目录'}
          </Button>
        ) : null}
      </div>
      {origin === 'scanned' ? (
        <div className="mb-4 space-y-1 text-xs leading-5 text-muted-foreground">
          <p>
            {scanned.data?.scanned_at
              ? `上次扫描：${fullTime(scanned.data.scanned_at)} · ${scanned.data.scanned_roots} 个目录入口 · ${scanned.data.file_count} 个视频`
              : '尚未扫描。点击「扫描已配置目录」读取现有网盘文件，不会改变 Reelay 的转存记录。'}
          </p>
          {scanned.data?.truncated ? (
            <p className="text-warning">扫描达到深度、文件数或时间上限，仅显示部分内容。</p>
          ) : null}
          {scanned.data?.errors.map((err, i) => (
            <p key={i} role="status">未扫描完整：{err}</p>
          ))}
          <p>这是只读文件索引，不能证明已完成 Reelay 转存，也不会自动修改重复转存判断。</p>
        </div>
      ) : null}
      <div className="mb-5 flex flex-col gap-3 md:flex-row md:items-center">
        <Segmented label="媒体类型" size="md" value={type} onChange={setType} options={TYPES} className="shrink-0 self-start" />
        <Select aria-label="按保存目录筛选" value={directory} onValueChange={setDirectory}
          options={directoryOptions} className="w-full shrink-0 sm:w-44" />
        <div className="flex flex-1 gap-2">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索片名" className="h-9 bg-card pl-9 shadow-xs" />
          </div>
          <Select value={sort} onValueChange={(v) => setSort(v as Sort)} options={SORTS} aria-label="排序" className="w-28 shrink-0" />
          <Segmented label="显示方式" size="md" value={view} onChange={switchView} options={VIEWS} className="hidden shrink-0 sm:inline-flex" />
        </div>
      </div>

      {directory && result?.groups.length === 0 && !submitted && !type ? (
        <p className="mb-4 rounded-lg border border-border bg-card px-4 py-3 text-xs leading-5 text-muted-foreground">
          「{directory}」已在保存目录中配置，但{origin === 'transfers'
            ? '目前没有 Reelay 完成的入库记录。' : '当前扫描索引还没有属于此目录的作品。'}
          {origin === 'scanned' ? '如需发现网盘已有影视文件，请点击「扫描已配置目录」。' : ''}
        </p>
      ) : null}
      {config.error ? (
        <p role="status" className="mb-3 text-xs text-muted-foreground">
          保存目录列表暂时无法读取，<button type="button" className="text-primary underline"
            onClick={config.reload}>重试加载</button>。
        </p>
      ) : null}
      {!result ? (
        active.error ? (
          <ErrorState title="读取媒体库失败" error={active.error} onRetry={active.reload} retrying={active.loading} />
        ) : (
          <div className="grid grid-cols-3 gap-4 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6">
            {Array.from({ length: 12 }, (_, i) => <Skeleton key={i} className="aspect-[2/3] rounded-lg" />)}
          </div>
        )
      ) : groups.length === 0 ? (
        <EmptyState
          icon={submitted || type || directory ? Search : LibraryBig}
          title={submitted || type || directory ? '这个筛选下暂无作品' : origin === 'scanned' ? '还没有扫描到网盘视频' : '媒体库还是空的'}
          description={submitted || type || directory ? '可以切换媒体类型或保存目录；空目录不会被当作已入库作品。' : origin === 'scanned' ? '点击「扫描已配置目录」获取网盘中已有的影视文件。' : '资源转存并整理完成后，会按片名汇总到这里。'}
        />
      ) : view === 'grid' ? (
        <div className="grid grid-cols-3 gap-x-3 gap-y-5 sm:grid-cols-4 sm:gap-x-4 md:grid-cols-5 lg:grid-cols-6">
          {groups.map((g) => (
            <button key={groupKey(g)} type="button" onClick={() => setOpen(g)} className="group min-w-0 text-left">
              <MediaCover title={g.title} posterPath={g.poster_path} mediaType={g.media_type}
                className="transition-all duration-200 group-hover:-translate-y-1 group-hover:shadow-lg" />
              <div className="mt-2 truncate text-[13px] font-medium group-hover:text-primary">{g.title}</div>
              <div className="truncate text-xs text-muted-foreground">{summary(g)}</div>
            </button>
          ))}
        </div>
      ) : (
        <div className="divide-y divide-border/70 overflow-hidden rounded-xl border border-card-border bg-card shadow-xs">
          {groups.map((g) => (
            <button key={groupKey(g)} type="button" onClick={() => setOpen(g)}
              className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-muted/50">
              <MediaCover title={g.title} posterPath={g.poster_path} mediaType={g.media_type} quality="w185" className="w-9 shrink-0 rounded-md" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium">
                  {g.title}{g.year ? <span className="ml-1.5 text-xs font-normal text-muted-foreground">{g.year}</span> : null}
                </div>
                <div className="truncate text-xs text-muted-foreground">
                  {mediaTypeLabel(g.media_type)}{g.resolution ? ` · ${g.resolution}` : ''}
                  {g.media_type !== 'movie' && g.episodes.length
                    ? ` · ${g.episode_pairs ? seasonsLabel(g.episode_pairs) : `${g.episodes.length} 集：${compactEpisodes(g.episodes, 4)}`}` : ''}
                </div>
              </div>
              <span className="hidden w-16 shrink-0 text-xs text-muted-foreground sm:block">{g.save_dir_alias ?? ''}</span>
              <span className="w-16 shrink-0 text-right text-xs tabular-nums text-muted-foreground">{gbText(g.bytes)}</span>
            </button>
          ))}
        </div>
      )}

      <GroupSheet group={open} onClose={() => setOpen(null)} />
    </Page>
  );
}
