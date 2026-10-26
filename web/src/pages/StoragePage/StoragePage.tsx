import {
  ArrowRight, Check, ChevronRight, CircleAlert, Copy, File, FileVideo, Folder,
  FolderOpen, FolderPlus, Hash, House, Pencil, RefreshCw,
} from 'lucide-react';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import { configApi } from '@/api/config';
import { ApiError } from '@/api/request';
import { storageApi, type CloudProvider, type IDirItem } from '@/api/storage';
import { EmptyState } from '@/components/app/EmptyState';
import { Page } from '@/components/app/Page';
import { Panel } from '@/components/app/Panel';
import { Button } from '@/components/ui/button';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { useDirListing } from '@/hooks/useDirListing';
import { cn, errorMessage } from '@/lib/utils';

type Edit = {
  kind: 'create' | 'rename';
  provider: CloudProvider;
  cid: string;
  driveId?: string;
  item?: IDirItem;
};

const PROVIDERS: Array<{ id: CloudProvider; label: string }> = [
  { id: 'p115', label: '115 网盘' },
  { id: 'aliyun', label: '阿里云盘（资源库）' },
];
const STORAGE_KEY = 'reelay-storage-browser-provider';
const VIDEO = /\.(mp4|mkv|avi|mov|webm|m2ts|ts|wmv|flv|mpg|mpeg|rmvb|iso)$/i;

function readableSize(size?: number | null): string {
  if (typeof size !== 'number' || !Number.isFinite(size)) return '—';
  if (size < 1024) return size + ' B';
  if (size < 1024 ** 2) return (size / 1024).toFixed(1) + ' KB';
  if (size < 1024 ** 3) return (size / 1024 ** 2).toFixed(1) + ' MB';
  return (size / 1024 ** 3).toFixed(2) + ' GB';
}

function fileExt(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot).toLowerCase() : '';
}

async function copyText(value: string) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const area = document.createElement('textarea');
  area.value = value;
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.append(area);
  area.select();
  const ok = document.execCommand('copy');
  area.remove();
  if (!ok) throw new Error('copy failed');
}

export default function StoragePage() {
  const [provider, setProvider] = useState<CloudProvider>(() =>
    localStorage.getItem(STORAGE_KEY) === 'aliyun' ? 'aliyun' : 'p115');
  const dir = useDirListing(undefined, provider, true);
  const [manualCid, setManualCid] = useState('');
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [edit, setEdit] = useState<Edit | null>(null);
  const [name, setName] = useState('');
  const [confirmExt, setConfirmExt] = useState(false);
  const [saving, setSaving] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const copiedTimer = useRef(0);

  useEffect(() => () => window.clearTimeout(copiedTimer.current), []);
  useEffect(() => {
    if (localStorage.getItem(STORAGE_KEY)) return;
    let alive = true;
    configApi.get().then((config) => {
      if (!alive || localStorage.getItem(STORAGE_KEY)) return;
      const defaults = config.config.defaults as { storage?: string } | undefined;
      if (defaults?.storage === 'aliyun') setProvider('aliyun');
    }).catch(() => {});
    return () => { alive = false; };
  }, []);

  const { cid: current, path, items, next } = dir;
  const busy = dir.loading || dir.loadingMore;
  const error = dir.loading ? '' : dir.error;
  const readyToWrite = dir.ready && !busy && !error && (provider !== 'aliyun' || !!dir.driveId);

  const chooseProvider = (selected: CloudProvider) => {
    if (selected === provider || saving) return;
    localStorage.setItem(STORAGE_KEY, selected);
    setProvider(selected);
    setManualCid('');
    setEdit(null);
    dir.open('0', []);
  };

  const copy = (id: string) => {
    copyText(id).then(() => {
      setCopied(id);
      toast.success('文件或目录 ID 已复制');
      window.clearTimeout(copiedTimer.current);
      copiedTimer.current = window.setTimeout(() => setCopied(null), 1600);
    }).catch(() => toast.error('无法访问剪贴板，请手动复制'));
  };

  const jump = () => {
    const cid = manualCid.trim();
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(cid) ||
        (provider === 'p115' && !/^[0-9]+$/.test(cid))) {
      toast.error(provider === 'p115' ? '115 目录 ID 必须是数字' : '阿里云盘目录 ID 格式无效');
      return;
    }
    const id = cid === 'root' ? '0' : cid;
    dir.open(id, id === '0' ? [] : [{ id, name: '目录 ' + id }]);
  };

  const startCreate = () => {
    if (!readyToWrite) return;
    setName('');
    setConfirmExt(false);
    setEdit({ kind: 'create', provider, cid: current, driveId: dir.driveId });
  };

  const startRename = (item: IDirItem) => {
    if (!readyToWrite) return;
    setName(item.name);
    setConfirmExt(false);
    setEdit({ kind: 'rename', provider, cid: current, driveId: dir.driveId, item });
  };

  const extChanged = edit?.kind === 'rename' && edit.item?.is_dir === false &&
    fileExt(name) !== fileExt(edit.item.name);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!edit || saving || edit.provider !== provider || edit.cid !== current) return;
    if (!name.trim() || name !== name.trim()) {
      toast.error('名称不能为空，也不能以空格开头或结尾');
      return;
    }
    if (extChanged && !confirmExt) {
      toast.error('更改文件扩展名需要先确认风险');
      return;
    }
    setSaving(true);
    try {
      if (edit.kind === 'create') {
        await storageApi.createFolder({
          provider: edit.provider, cid: edit.cid, drive_id: edit.driveId, name,
        });
        toast.success('文件夹创建成功');
      } else if (edit.item) {
        await storageApi.renameEntry({
          provider: edit.provider, cid: edit.cid, drive_id: edit.driveId, name,
          fid: edit.item.id, old_name: edit.item.name, is_dir: edit.item.is_dir === true,
        });
        toast.success('重命名成功');
      }
      setEdit(null);
      dir.reload();
    } catch (e) {
      toast.error(errorMessage(e, '网盘操作失败'));
      if (e instanceof ApiError && e.status === 502) {
        // Upstream may have committed despite a lost response: read back first.
        setEdit(null);
        dir.reload();
      }
    } finally {
      setSaving(false);
    }
  };

  const firstLoad = !dir.ready && !error;
  const cloudName = PROVIDERS.find((x) => x.id === provider)?.label ?? provider;

  return (
    <Page
      title="网盘目录"
      description="可选择 115 或阿里云盘资源库，浏览文件夹和文件。支持新建文件夹、重命名；不提供删除、移动或覆盖。"
      actions={
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" className="bg-card" disabled={busy || saving} onClick={dir.reload}>
            <RefreshCw className={cn(busy && 'animate-spin')} />刷新
          </Button>
          <Button disabled={!readyToWrite || saving} onClick={startCreate}>
            <FolderPlus />新建文件夹
          </Button>
        </div>
      }
    >
      <div className="mb-3 flex flex-wrap items-center gap-2" role="group" aria-label="选择网盘">
        {PROVIDERS.map((option) => (
          <Button key={option.id} size="sm" variant={option.id === provider ? 'default' : 'outline'}
            disabled={option.id === provider || saving} onClick={() => chooseProvider(option.id)}>
            {option.label}
          </Button>
        ))}
        <span className="text-xs text-muted-foreground">两家网盘分别浏览，操作不会跨盘进行</span>
      </div>
      <Panel bodyClassName="p-0">
        <div className="flex items-center gap-3 border-b border-border/70 px-3 py-2.5 sm:px-4">
          <nav aria-label="当前位置" className="min-w-0 flex-1 overflow-x-auto">
            <ol className="flex items-center gap-0.5 whitespace-nowrap text-sm">
              <li>
                <button type="button" disabled={busy || path.length === 0}
                  onClick={() => dir.open('0', [])}
                  className={cn('inline-flex min-h-9 items-center gap-1.5 rounded-md px-2 font-medium sm:min-h-8',
                    path.length ? 'text-muted-foreground hover:bg-muted hover:text-foreground' : 'text-foreground')}>
                  <House className="size-4" />全部文件
                </button>
              </li>
              {path.map((crumb, i) => {
                const last = i === path.length - 1;
                return (
                  <li key={crumb.id} className="flex items-center gap-0.5">
                    <ChevronRight className="size-3.5 shrink-0 text-muted-foreground/60" />
                    <button type="button" disabled={busy || last} aria-current={last ? 'page' : undefined}
                      onClick={() => dir.open(crumb.id, path.slice(0, i + 1))}
                      className={cn('inline-flex min-h-9 max-w-56 items-center rounded-md px-2 font-medium sm:min-h-8',
                        last ? 'text-foreground' : 'text-muted-foreground hover:bg-muted hover:text-foreground')}>
                      <span className="truncate">{crumb.name}</span>
                    </button>
                  </li>
                );
              })}
            </ol>
          </nav>
          {current !== '0' ? (
            <button type="button" onClick={() => copy(current)} title="复制当前目录 ID"
              className="hidden min-h-8 shrink-0 items-center gap-1.5 rounded-md px-2 text-xs text-muted-foreground hover:bg-muted hover:text-foreground sm:inline-flex">
              <Hash className="size-3.5" />
              <span className="max-w-36 truncate font-mono tabular-nums">{current}</span>
              {copied === current ? <Check className="size-3.5 text-success" /> : <Copy className="size-3.5" />}
            </button>
          ) : null}
        </div>

        {error ? (
          <div role="alert" className="flex flex-col items-center px-6 py-14 text-center">
            <span className="grid size-11 place-items-center rounded-full bg-destructive/10 text-destructive">
              <CircleAlert className="size-5" />
            </span>
            <p className="mt-4 text-sm font-medium">无法读取{cloudName}中的目录</p>
            <p className="mt-1 max-w-sm text-xs leading-5 text-muted-foreground">{error}</p>
            <div className="mt-5 flex flex-wrap justify-center gap-2">
              <Button variant="outline" onClick={dir.reload}><RefreshCw />重新查询</Button>
              <Button asChild variant="ghost" className="text-primary">
                <Link to="/config?tab=connections">检查网盘连接<ArrowRight /></Link>
              </Button>
            </div>
          </div>
        ) : firstLoad ? (
          <div className="space-y-1 p-2" role="status" aria-label="正在读取网盘文件">
            {[0, 1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-11 rounded-lg" />)}
          </div>
        ) : items.length === 0 ? (
          <EmptyState icon={FolderOpen} title="这个目录没有文件或文件夹"
            description={next !== null ? '还可以加载后面的内容。' : '可以在此创建新的文件夹。'}
            className="m-4 border-none py-12" />
        ) : (
          <ul className={cn('p-1.5 transition-opacity sm:p-2', busy && 'opacity-60')}>
            {items.map((item) => {
              const folder = item.is_dir === true;
              const Icon = folder ? Folder : VIDEO.test(item.name) ? FileVideo : File;
              return (
                <li key={item.id} className="group flex items-center gap-1 rounded-lg transition-colors hover:bg-muted/70">
                  <button type="button" disabled={!folder || busy}
                    onClick={() => dir.open(item.id, [...path, { id: item.id, name: item.name }])}
                    className={cn('flex min-h-11 min-w-0 flex-1 items-center gap-3 rounded-lg px-2.5 text-left',
                      !folder && 'cursor-default')}>
                    <span className={cn('grid size-8 shrink-0 place-items-center rounded-lg',
                      folder ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground')}>
                      <Icon className="size-4" />
                    </span>
                    <span className="min-w-0 flex-1 truncate text-sm font-medium" title={item.name}>{item.name}</span>
                    {folder ? <ChevronRight className="size-4 shrink-0 text-muted-foreground/50" />
                      : <span className="shrink-0 text-xs text-muted-foreground">{readableSize(item.size)}</span>}
                  </button>
                  <button type="button" aria-label={'重命名「' + item.name + '」'}
                    title="重命名" disabled={!readyToWrite || saving} onClick={() => startRename(item)}
                    className="grid size-9 shrink-0 place-items-center rounded-md text-muted-foreground transition hover:bg-card hover:text-foreground disabled:opacity-40">
                    <Pencil className="size-4" />
                  </button>
                  <button type="button" aria-label={'复制「' + item.name + '」的 ID'}
                    title="复制文件或目录 ID" onClick={() => copy(item.id)}
                    className="mr-1 grid size-9 shrink-0 place-items-center rounded-md text-muted-foreground transition hover:bg-card hover:text-foreground">
                    {copied === item.id ? <Check className="size-4 text-success" /> : <Copy className="size-4" />}
                  </button>
                </li>
              );
            })}
          </ul>
        )}

        {next !== null && !error ? (
          <div className="border-t border-border/70 p-3 text-center">
            <Button variant="outline" disabled={busy || saving} onClick={() => void dir.loadMore()}>
              加载更多文件
            </Button>
          </div>
        ) : null}
      </Panel>

      <div className="mt-4 px-1">
        <button type="button" onClick={() => setShowAdvanced((v) => !v)} aria-expanded={showAdvanced}
          className="inline-flex min-h-9 items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground sm:min-h-8">
          <Hash className="size-3.5" />{showAdvanced ? '收起按 ID 定位' : '知道目录 ID？直接定位'}
        </button>
        {showAdvanced ? (
          <form className="mt-2 flex max-w-md gap-2" onSubmit={(e) => { e.preventDefault(); jump(); }}>
            <Input aria-label="目录 ID" inputMode={provider === 'p115' ? 'numeric' : 'text'}
              value={manualCid} onChange={(e) => setManualCid(e.target.value)}
              className="bg-card font-mono" placeholder="输入当前网盘的目录 ID" />
            <Button type="submit" variant="outline" className="bg-card"
              disabled={busy || !manualCid.trim()}>定位</Button>
          </form>
        ) : null}
        <p className="mt-1.5 text-xs leading-5 text-muted-foreground">
          此处修改只作用于当前选择的网盘。保存目录别名仍在
          <Link to="/config" className="mx-0.5 font-medium text-primary hover:underline">设置</Link>
          中配置。重命名已被任务引用的文件或目录，可能影响进行中的整理和恢复任务。
        </p>
      </div>

      <Dialog open={edit !== null} onOpenChange={(open) => { if (!open && !saving) setEdit(null); }}>
        <DialogContent showCloseButton={!saving}>
          <DialogHeader>
            <DialogTitle>{edit?.kind === 'create' ? '新建文件夹' : '重命名文件或目录'}</DialogTitle>
            <DialogDescription>
              当前网盘：{cloudName}。操作只修改名称或创建空目录；不会删除文件。
              修改进行中任务正在使用的路径可能影响整理，请先核对。
            </DialogDescription>
          </DialogHeader>
          <form className="space-y-4" onSubmit={(e) => void submit(e)}>
            <div className="space-y-1.5">
              <label className="text-sm font-medium" htmlFor="cloud-entry-name">
                {edit?.kind === 'create' ? '新文件夹名称' : '新名称'}
              </label>
              <Input id="cloud-entry-name" value={name} onChange={(e) => setName(e.target.value)}
                maxLength={255} autoFocus autoComplete="off" disabled={saving} />
            </div>
            {edit?.kind === 'rename' && edit.item ? (
              <p className="break-all text-xs text-muted-foreground">原名称：{edit.item.name}</p>
            ) : null}
            {extChanged ? (
              <label className="flex items-start gap-2 text-sm text-warning">
                <input type="checkbox" checked={confirmExt}
                  onChange={(e) => setConfirmExt(e.target.checked)} className="mt-1" />
                我确认要修改文件扩展名。修改后某些播放器可能无法识别。
              </label>
            ) : null}
            <DialogFooter>
              <Button type="button" variant="outline" disabled={saving} onClick={() => setEdit(null)}>取消</Button>
              <Button type="submit" disabled={!name.trim() || saving || (extChanged && !confirmExt)}>
                {saving ? '执行中…' : edit?.kind === 'create' ? '创建文件夹' : '确认重命名'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </Page>
  );
}
