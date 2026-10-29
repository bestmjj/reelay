import { AlertTriangle, BellPlus, Check, EyeOff, FolderSearch, FolderTree, RefreshCw, RotateCcw, ShieldCheck, Trash2, X } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { inboxApi, type ISaveDirs } from '@/api/inbox';
import { subscriptionApi } from '@/api/subscriptions';
import { taskApi, type IRepairReport, type IShareFolderPreview } from '@/api/tasks';
import DirectoryPicker, { type PickedDir } from '@/components/DirectoryPicker';
import { ErrorState } from '@/components/app/ErrorState';
import { Field } from '@/components/app/Field';
import { MediaCover } from '@/components/app/MediaCover';
import { MediaMeta } from '@/components/app/MediaMeta';
import { ConflictDialog, OrganizeConflict } from '@/components/OrganizeConflict';
import { Select } from '@/components/app/Select';
import { Sheet } from '@/components/app/Sheet';
import { StatusBadge, ToneBadge } from '@/components/app/StatusBadge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { Textarea } from '@/components/ui/textarea';
import { useAsync } from '@/hooks/useAsync';
import { CONFLICT_DONE, confirmAbandon, type ConflictChoice } from '@/lib/conflict';
import { fullTime, relativeTime } from '@/lib/format';
import { CONFIDENCE_CAP_LABELS, MEDIA_TYPE_LABELS } from '@/lib/status';
import { announceSubscription } from '@/lib/subscriptions';
import { ACTION_DONE, ACTION_ENDPOINT, actionBody, allows, type PostedAction } from '@/lib/taskActions';
import type { IOrganizeConflict, IParsedResource, ITaskDetail, ITransferPartial, TaskActionName } from '@/lib/types';
import { errorMessage } from '@/lib/utils';

const MEDIA_TYPES = Object.entries(MEDIA_TYPE_LABELS).map(([value, label]) => ({ value, label }));

function parseEpisodes(text: string): number[] {
  return [...new Set(text.split(/[,，\s]+/).map((v) => Number(v)).filter((v) => Number.isInteger(v) && v > 0))];
}

function parseSelectedEpisodes(text: string): number[] {
  if (!text.trim()) return [];
  const out = new Set<number>();
  for (const token of text.split(/[,，、\s]+/).filter(Boolean)) {
    const match = /^E?(\d{1,4})(?:-E?(\d{1,4}))?$/i.exec(token);
    if (!match) throw new Error('集数格式不正确，请输入 11, 13-15 或 E11 E13');
    const start = Number(match[1]);
    const end = match[2] ? Number(match[2]) : start;
    if (start < 1 || end > 2000 || end < start || end - start > 99) {
      throw new Error('集数必须在 1～2000，范围不能倒序，单次最多 100 集');
    }
    for (let ep = start; ep <= end; ep += 1) {
      out.add(ep);
      if (out.size > 100) throw new Error('一次最多指定 100 集');
    }
  }
  return [...out].sort((a, b) => a - b);
}

function Section({ title, icon: Icon, children }: { title: string; icon?: typeof FolderTree; children: React.ReactNode }) {
  return (
    <section className="space-y-2.5">
      <h3 className="flex items-center gap-1.5 text-[13px] font-semibold text-foreground">
        {Icon ? <Icon className="size-3.5 text-muted-foreground" /> : null}{title}
      </h3>
      {children}
    </section>
  );
}

interface TaskDetailSheetProps {
  taskId: string | null;
  onClose: () => void;
  /** 任务状态被修改后回调，用于刷新列表。 */
  onChanged?: () => void;
  /** 页面已经有的保存目录（收件箱接口顺带返回）；不传时打开需要选目录的任务再读取。 */
  saveDirs?: ISaveDirs;
}

interface TaskMeta {
  confidence?: { score?: number; caps?: string[] };
  organize?: { plan?: Array<{ target_dir: string; target_name: string }>; error?: string; pending_conflict?: IOrganizeConflict };
  storage?: { partial?: ITransferPartial };
  share_selection?: { folder_id?: string; path?: string; file_id?: string; only_episodes?: number[];
    file_name?: string; file_path?: string; file_size?: number };
}

export function TaskDetailSheet({ taskId, onClose, onChanged, saveDirs }: TaskDetailSheetProps) {
  const loaded = useAsync(() => (taskId ? taskApi.get(taskId) : Promise.resolve(null)), [taskId], '读取任务详情失败');
  const detail = loaded.stale ? null : loaded.data ?? null;
  const [shown, setShown] = useState<ITaskDetail | null>(null);
  const [draft, setDraft] = useState<Partial<IParsedResource>>({});
  const [episodesText, setEpisodesText] = useState('');
  const [onlyEpisodesText, setOnlyEpisodesText] = useState('');
  const [targetDir, setTargetDir] = useState('');
  const [existingPickerOpen, setExistingPickerOpen] = useState(false);
  const [existingFolder, setExistingFolder] = useState<PickedDir | null>(null);
  const [destinationMode, setDestinationMode] = useState<'exact' | 'organized'>('exact');
  const [repairReport, setRepairReport] = useState<IRepairReport | null>(null);
  const [repairSelected, setRepairSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [choosing, setChoosing] = useState(false);
  const [shareFolders, setShareFolders] = useState<IShareFolderPreview | null>(null);
  const [shareFolderId, setShareFolderId] = useState('');
  const [shareFileId, setShareFileId] = useState('');
  const [rememberShareFolder, setRememberShareFolder] = useState(true);
  const [shareFolderBusy, setShareFolderBusy] = useState(false);
  const [wrongCopyReason, setWrongCopyReason] = useState('');
  // Server refreshes after selecting a share file must not erase the destination
  // picked for the same work. Clear it only when the task/work/share changes.
  // The backend still re-verifies the folder's drive ID, path and title.
  if (detail !== shown) {
    const before = shown?.context.parsed;
    const after = detail?.context.parsed;
    const sameTaskAndWork = Boolean(detail && shown && detail.task_id === shown.task_id
      && before?.title === after?.title
      && before?.year === after?.year
      && before?.media_type === after?.media_type
      && before?.season === after?.season
      && before?.share_code === after?.share_code
      && JSON.stringify(before?.episodes ?? []) === JSON.stringify(after?.episodes ?? []));
    setShown(detail);
    setDraft(detail?.context.parsed ?? {});
    setEpisodesText((detail?.context.parsed?.episodes ?? []).join(', '));
    const selection = detail?.context.meta?.share_selection;
    setOnlyEpisodesText(Array.isArray(selection?.only_episodes)
      ? selection.only_episodes.join(', ') : '');
    if (!sameTaskAndWork) {
      setTargetDir('');  // 空值 = 审批时按自动分类、重新整理时保持当前目录
      setExistingFolder(null);
      setDestinationMode('exact');
      setExistingPickerOpen(false);
    }
    setRepairReport(null);
    setRepairSelected([]);
    setChoosing(false);
    if (detail?.task_id !== shown?.task_id) {
      setShareFolders(null);
      setShareFolderId('');
      setShareFileId('');
      setRememberShareFolder(true);
      setWrongCopyReason('');
    }
  }

  const can = (action: TaskActionName) => allows(detail?.actions, action);
  const needDirs = can('approve') || can('reorganize');
  // 保存目录可能刚在设置里改过：每次打开需要选目录的任务都重新读取，不做页面级缓存
  const fetchedDirs = useAsync(
    () => (needDirs && !saveDirs ? inboxApi.saveDirs() : Promise.resolve(null)),
    [taskId, needDirs, Boolean(saveDirs)],
  );
  const dirs = saveDirs ?? fetchedDirs.data ?? null;

  const finish = (message: string) => {
    toast.success(message);
    onChanged?.();
    onClose();
  };

  const run = async (action: PostedAction, conflict?: ConflictChoice) => {
    if (!detail) return;
    setBusy(true);
    try {
      const body = action === 'approve' && existingFolder
        ? { existing_folder_id: existingFolder.id, existing_drive_id: existingFolder.driveId,
            destination_mode: destinationMode }
        : actionBody(action, { dir: targetDir, conflict });
      await taskApi.act(detail.task_id, ACTION_ENDPOINT[action], body);
      finish(conflict ? CONFLICT_DONE[conflict] : ACTION_DONE[action]);
    } catch (err) {
      toast.error(errorMessage(err, '操作失败'));
    } finally {
      setBusy(false);
    }
  };

  const checkMissingFiles = async () => {
    if (!detail) return;
    setBusy(true);
    try {
      const report = await taskApi.repairCheck(detail.task_id);
      setRepairReport(report);
      setRepairSelected(report.missing.slice(0, 100).map((item) => item.old_id));
      if (!report.missing.length) {
        toast.success('云盘核验没有发现确定丢失的文件，不会重复转存');
      } else {
        toast.success(`确认有 ${report.missing.length} 个文件丢失；请核对后选择补转`);
      }
    } catch (err) {
      setRepairReport(null);
      setRepairSelected([]);
      toast.error(errorMessage(err, '无法实时核验已入库文件'));
    } finally {
      setBusy(false);
    }
  };

  const repairMissingFiles = async () => {
    if (!detail || !repairReport || !repairSelected.length || repairSelected.length > 100
        || repairReport.uncertain.length) return;
    if (!window.confirm(
      `只补转选中的 ${repairSelected.length} 个、已核实丢失的文件？`
      + '\n不覆盖云盘里仍存在的文件；原成功任务和历史记录不变。'
      + '\n转存前还会再检查，如果文件恢复则停止。'
    )) return;
    setBusy(true);
    try {
      const result = await taskApi.repairMissing(detail.task_id, repairSelected);
      finish(`已创建独立补转任务 ${result.task_id}，将重新核验后只补缺失文件`);
    } catch (err) {
      toast.error(errorMessage(err, '补转未排队，请重新核验'));
    } finally {
      setBusy(false);
    }
  };

  const quarantineWrongCopy = async () => {
    if (!detail || wrongCopyReason.trim().length < 5) return;
    if (!window.confirm('将撤回这条任务的入库去重记录，并暂停对应集数后续自动转存。\n不会修改或删除云盘文件。确认标记错片？')) return;
    setBusy(true);
    try {
      await taskApi.quarantineWrongCopy(detail.task_id, wrongCopyReason.trim());
      toast.success('已隔离疑似错片：不会继续当作正确集数去重，也不会自动重转');
      loaded.reload();
      onChanged?.();
    } catch (err) {
      toast.error(errorMessage(err, '标记错片失败'));
    } finally {
      setBusy(false);
    }
  };

  const resolveWrongCopy = async () => {
    if (!detail) return;
    const message = window.prompt(
      '请先在阿里云盘人工核实并处理错误文件（例如移至正确作品目录）。'
      + '\n解除后才允许该集后续转存，旧的错误去重记录不会恢复。'
      + '\n确认已处理后，请输入：我已核对并处理错片',
    );
    if (message !== '我已核对并处理错片') return;
    setBusy(true);
    try {
      await taskApi.resolveWrongCopy(detail.task_id);
      toast.success('已解除隔离。请手动选择具有明确集号的正确分享来源，切勿自动混用');
      loaded.reload();
      onChanged?.();
    } catch (err) {
      toast.error(errorMessage(err, '解除隔离失败'));
    } finally {
      setBusy(false);
    }
  };

  const browseShareFolders = async () => {
    if (!detail) return;
    setShareFolderBusy(true);
    try {
      const preview = await taskApi.shareFolders(detail.task_id);
      setShareFolders(preview);
      setShareFolderId(preview.selected_folder_id ?? '');
      setShareFileId(preview.selected_file_id ?? '');
    } catch (err) {
      toast.error(errorMessage(err, '读取分享版本失败'));
    } finally {
      setShareFolderBusy(false);
    }
  };

  const chooseShareFolder = async () => {
    if (!detail || !shareFolderId) return;
    setShareFolderBusy(true);
    try {
      const preview = await taskApi.chooseShareFolder(
        detail.task_id, shareFolderId, rememberShareFolder,
      );
      setShareFolders(preview);
      setShareFolderId(preview.selected_folder_id ?? '');
      setShareFileId('');
      toast.success('已选定分享来源文件夹；可在安全核验后转存指定集');
      loaded.reload();
      onChanged?.();
    } catch (err) {
      toast.error(errorMessage(err, '确认分享版本失败'));
    } finally {
      setShareFolderBusy(false);
    }
  };

  const chooseShareFile = async () => {
    if (!detail || !shareFileId) return;
    setShareFolderBusy(true);
    try {
      const preview = await taskApi.chooseShareFile(detail.task_id, shareFileId);
      setShareFolders(preview);
      setShareFileId(preview.selected_file_id ?? '');
      setShareFolderId('');
      toast.success('已固定精确源视频文件 ID；转存前会重新核验名称、路径和大小');
      loaded.reload();
      onChanged?.();
    } catch (err) {
      toast.error(errorMessage(err, '选择源文件失败'));
    } finally {
      setShareFolderBusy(false);
    }
  };

  const forceReopenDuplicate = async () => {
    if (!detail || !window.confirm(
      '确定强制重新转存这一集？\\n旧的已入库记录不会删除，且不会自动覆盖云盘文件。'
      + '\\n接下来必须精确选择分享中的单集文件，并指定阿里云盘最终保存文件夹。'
    )) return;
    setBusy(true);
    try {
      const next = await taskApi.forceReopenDuplicate(detail.task_id);
      loaded.mutate(() => next);
      setShareFolders(null);
      setShareFileId('');
      setShareFolderId('');
      toast.success('已进入人工强制模式；先锁定源文件，再指定最终保存目录');
      onChanged?.();
    } catch (err) {
      toast.error(errorMessage(err, '无法启用强制转存'));
    } finally {
      setBusy(false);
    }
  };

  const reopenDuplicate = async () => {
    if (!detail) return;
    setBusy(true);
    try {
      const next = await taskApi.reopenDuplicate(detail.task_id);
      loaded.mutate(() => next);
      toast.success('已恢复到待确认，可指定本次要补齐的集数');
      onChanged?.();
    } catch (err) {
      toast.error(errorMessage(err, '重新检查失败'));
    } finally {
      setBusy(false);
    }
  };

  const saveSelectedEpisodes = async () => {
    if (!detail) return;
    let episodes: number[];
    try {
      episodes = parseSelectedEpisodes(onlyEpisodesText);
    } catch (err) {
      toast.error(errorMessage(err, '集号无效'));
      return;
    }
    setBusy(true);
    try {
      const next = await taskApi.selectEpisodes(detail.task_id, episodes);
      loaded.mutate(() => next);
      setShareFolders(null); // previously loaded source choices may target a different episode
      setShareFileId('');
      setShareFolderId('');
      toast.success(episodes.length
        ? '已锁定本次补集范围；已入库的集仍自动跳过，请再确认分享版本'
        : '已恢复自动识别分享集数和原有去重规则');
      onChanged?.();
    } catch (err) {
      toast.error(errorMessage(err, '指定补集失败'));
    } finally {
      setBusy(false);
    }
  };

  const subscribe = async () => {
    if (!detail) return;
    setBusy(true);
    try {
      const { subscription, pending } = await subscriptionApi.addFromTask(detail.task_id);
      announceSubscription(subscription, pending, () => onChanged?.());
      onChanged?.();
      loaded.reload();
    } catch (err) {
      toast.error(errorMessage(err, '订阅失败'));
    } finally {
      setBusy(false);
    }
  };

  const saveParsed = async () => {
    if (!detail) return;
    setBusy(true);
    try {
      const next = await taskApi.correct(detail.task_id, {
        title: draft.title, media_type: draft.media_type,
        year: draft.year ? Number(draft.year) : null,
        season: draft.season == null ? null : Number(draft.season),
        episodes: parseEpisodes(episodesText), resolution: draft.resolution, reason: draft.reason,
      });
      loaded.mutate(() => next);
      toast.success('识别信息已保存');
      onChanged?.();
    } catch (err) {
      toast.error(errorMessage(err, '保存失败'));
    } finally {
      setBusy(false);
    }
  };

  const parsed = detail?.context.parsed;
  const hasVerifiedIdentity = Boolean(parsed?.title?.trim()
    && ['movie', 'tv', 'anime'].includes(parsed.media_type ?? '')
    && (parsed.media_type === 'movie'
      || (parsed.season != null && (parsed.episodes?.length ?? 0) > 0)));
  const hasPendingMetadataEdits = can('correct') && (
    JSON.stringify(draft) !== JSON.stringify(parsed ?? {})
    || episodesText !== (parsed?.episodes ?? []).join(', ')
  );
  const requireExplicitTarget = detail?.context.raw.extra?.manual_import === true
    && detail?.context.raw.extra?.manual_unlabeled === true;
  const meta = (detail?.context.meta ?? {}) as TaskMeta;
  const forcedCopy = Boolean((detail?.context.meta?.task as { force_single_copy?: unknown } | undefined)?.force_single_copy);
  const isAliyun = detail?.context.source_rule.storage === 'aliyun'
    || (detail?.context.meta?.storage as { provider?: string } | undefined)?.provider === 'aliyun';
  const plan = meta.organize?.plan ?? [];
  const organizeError = meta.organize?.error;
  const partial = meta.storage?.partial;
  const conflict = can('resolve_conflict') ? meta.organize?.pending_conflict : undefined;
  const reorganize = () => (conflict ? setChoosing(true) : void run('reorganize'));
  const currentSeason = `Season ${String(parsed?.season ?? 1).padStart(2, '0')}`;
  const forceBlockReason = !forcedCopy ? null
    : hasPendingMetadataEdits ? '识别结果有未保存的修改，请先点击「保存修改」'
    : !meta.share_selection?.file_id ? '请在「选择分享源文件或文件夹」中锁定准确的单集视频'
    : !existingFolder ? `还没有指定最终保存位置：请在上方「指定阿里云盘保存目录」选择已有的 ${currentSeason} 文件夹`
    : !existingFolder.driveId ? '所选文件夹缺少阿里云盘资源库 ID，请重新选择'
    : destinationMode !== 'exact' ? '保存方式必须选择「直接存入所选文件夹」'
    : null;

  const footer = detail && detail.actions.length ? (
    <div className="flex flex-wrap items-center justify-between gap-2">
      {can('approve') && forceBlockReason ? (
        <p role="status" className="w-full text-xs text-destructive">
          暂时无法强制转存：{forceBlockReason}。
        </p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        {can('correct') ? <Button variant="outline" size="sm" disabled={busy} onClick={() => void saveParsed()}>保存修改</Button> : null}
        {can('reparse') ? <Button variant="outline" size="sm" disabled={busy} onClick={() => void run('reparse')}><RotateCcw />重新识别</Button> : null}
        {can('retry') ? <Button variant="outline" size="sm" disabled={busy} onClick={() => void run('retry')}><RefreshCw />重试</Button> : null}
        {can('repair_missing') ? (
          <Button variant="outline" size="sm" disabled={busy} onClick={() => void checkMissingFiles()}>
            <ShieldCheck />核验已入库文件
          </Button>
        ) : null}
        {can('reopen_duplicate') ? (
          <Button variant="outline" size="sm" disabled={busy} onClick={() => void reopenDuplicate()}>
            <RefreshCw />手动检查缺集
          </Button>
        ) : null}
        {can('force_reopen_duplicate') ? (
          <Button variant="outline" size="sm" disabled={busy} onClick={() => void forceReopenDuplicate()}>
            <AlertTriangle />强制转存本集
          </Button>
        ) : null}
        {can('reconcile') ? <Button variant="outline" size="sm" disabled={busy} onClick={() => void run('reconcile')}><ShieldCheck />核验转存</Button> : null}
        {can('accept_partial') ? (
          <Button variant="outline" size="sm" disabled={busy} onClick={() => void run('accept_partial')}>
            <Check />按已到的{partial ? ` ${partial.landed} 个` : '文件'}继续
          </Button>
        ) : null}
        {can('abandon') ? (
          <Button variant="outline" size="sm" disabled={busy} className="text-destructive"
            onClick={() => void confirmAbandon(parsed?.title, partial).then((ok) => { if (ok) void run('abandon'); })}>
            <Trash2 />放弃并删除
          </Button>
        ) : null}
        {can('reorganize') || conflict ? (
          <Button variant="outline" size="sm" disabled={busy} onClick={reorganize}><FolderTree />重新整理</Button>
        ) : null}
        {can('subscribe') ? <Button variant="outline" size="sm" disabled={busy} onClick={() => void subscribe()}><BellPlus />追这部剧</Button> : null}
      </div>
      <div className="flex gap-2">
        {can('dismiss') ? <Button variant="outline" size="sm" disabled={busy} onClick={() => void run('dismiss')} className="text-destructive"><EyeOff />忽略</Button> : null}
        {can('reject') ? <Button variant="outline" size="sm" disabled={busy} onClick={() => void run('reject')} className="text-destructive"><X />忽略</Button> : null}
        {can('approve') ? <Button size="sm"
              disabled={busy || Boolean(forceBlockReason) || Boolean(hasPendingMetadataEdits)
                || (requireExplicitTarget && !targetDir && !existingFolder)
                || Boolean(existingFolder && !existingFolder.driveId)}
              onClick={() => void run('approve')}><Check />{forcedCopy ? '确认强制转存本集' : '确认转存'}</Button> : null}
      </div>
    </div>
  ) : null;

  const autoAlias = detail?.state === 'pending_approval' ? detail.auto_dir?.split('/')[0] : detail?.context.save_dir_alias;

  return (
    <Sheet
      open={taskId !== null}
      onOpenChange={(open) => { if (!open) onClose(); }}
      title={detail ? (parsed?.title || '尚未识别片名') : '任务详情'}
      description={detail ? `${detail.context.raw.source} · ${relativeTime(detail.created_at)}` : undefined}
      footer={footer}
    >
      {!detail ? (
        loaded.error ? (
          <ErrorState title="读取任务详情失败" error={loaded.error} onRetry={loaded.reload} retrying={loaded.loading} className="border-none" />
        ) : (
          <div className="space-y-3"><Skeleton className="h-32 w-full" /><Skeleton className="h-48 w-full" /></div>
        )
      ) : (
        <div className="space-y-6">
          <div className="flex gap-4">
            <MediaCover title={parsed?.title} posterPath={parsed?.raw_fields?.poster_path as string | undefined}
              mediaType={parsed?.media_type} className="w-20 shrink-0" quality="w185" />
            <div className="min-w-0 space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge state={detail.state} skipKind={detail.skip_kind} />
                {parsed?.year ? <span className="text-sm text-muted-foreground">{parsed.year}</span> : null}
              </div>
              <MediaMeta mediaType={parsed?.media_type} season={parsed?.season} episodes={parsed?.episodes}
                resolution={parsed?.resolution} size={parsed?.size} />
              {meta.confidence?.score !== undefined ? (
                <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                  置信度 <span className="font-medium tabular-nums text-foreground">{Math.round(meta.confidence.score * 100)}%</span>
                  {(meta.confidence.caps ?? []).map((cap) => <ToneBadge key={cap} tone="warning">{CONFIDENCE_CAP_LABELS[cap] ?? cap}</ToneBadge>)}
                </div>
              ) : null}
              <div className="text-xs text-muted-foreground">
                {detail.context.save_dir_alias ? <>保存到「{detail.context.save_dir_alias}」 · </>
                  : detail.auto_dir ? <>将存到「{detail.auto_path ?? detail.auto_dir}」 · </> : null}
                <span title={fullTime(detail.updated_at)}>更新于 {relativeTime(detail.updated_at)}</span>
              </div>
            </div>
          </div>

          {detail.state === 'skipped' && detail.skip_reason ? (
            <div className="rounded-lg border bg-muted/40 p-3 text-sm leading-6">
              <p className="font-semibold">
                {detail.skip_kind === 'duplicate' ? '安全去重：没有进行新的转存' : '任务跳过原因'}
              </p>
              <p className="mt-1 break-words text-muted-foreground">{detail.skip_reason}</p>
              {detail.skip_kind === 'duplicate' ? (
                <p className="mt-1 text-xs text-muted-foreground">
                  去重依据是已有入库记录，不能证明所选 DoVi/HDR.Vivid 与库内版本完全相同。
                  如认为已有文件有误，请先核验原文件或使用错片隔离，切勿直接重复提交。
                </p>
              ) : null}
            </div>
          ) : null}

          {detail.error || organizeError || detail.explanation ? (
            <div className="flex gap-2.5 rounded-lg border border-destructive/25 bg-destructive/5 p-3 text-sm text-destructive">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" />
              <div className="min-w-0 space-y-1">
                <p className="break-words">{detail.explanation?.text ?? (organizeError || detail.error)}</p>
                {detail.explanation ? <p className="font-medium">建议：{detail.explanation.label}</p> : null}
                {detail.explanation && (organizeError || detail.error) ? (
                  <p className="break-words text-xs text-destructive/80">原始错误：{organizeError || detail.error}</p>
                ) : null}
              </div>
            </div>
          ) : null}

          {can('repair_missing') && repairReport ? (
            <Section title="补回云盘里丢失的文件" icon={ShieldCheck}>
              <p className="text-xs leading-5 text-muted-foreground">
                已实时检查原文件 ID 和保存目录：{repairReport.base_path}。
                已存在 {repairReport.present.length} 个，确认丢失 {repairReport.missing.length} 个，
                待人工核对 {repairReport.uncertain.length} 个。未丢失的不允许重复转存。
              </p>
              {repairReport.missing.length > 100 ? (
                <p className="text-xs text-muted-foreground">
                  缺失文件较多，每次最多勾选 100 个；这一批完成后可再次检查剩余缺失文件。
                </p>
              ) : null}
              {repairReport.missing.length > 0 ? (
                <div className="max-h-48 space-y-2 overflow-y-auto rounded-lg border p-3">
                  {repairReport.missing.map((file) => (
                    <label key={file.old_id} className="flex items-start gap-2 text-xs">
                      <input type="checkbox" className="mt-0.5"
                        checked={repairSelected.includes(file.old_id)}
                        onChange={(e) => setRepairSelected((prev) =>
                          e.target.checked ? (prev.length < 100 ? [...prev, file.old_id] : prev)
                            : prev.filter((v) => v !== file.old_id))} />
                      <span className="break-all">
                        {file.target_name}
                        <span className="block text-muted-foreground">原目录：{file.target_dir || '所选根目录'}</span>
                      </span>
                    </label>
                  ))}
                </div>
              ) : null}
              {repairReport.uncertain.length ? (
                <p className="text-xs text-destructive">
                  部分文件存在同名冲突或状态异常；为避免重复/错片，本次禁止自动补转，请人工核对。
                </p>
              ) : null}
              {repairReport.missing.length ? (
                <Button size="sm" disabled={busy || !repairSelected.length || Boolean(repairReport.uncertain.length)}
                  onClick={() => void repairMissingFiles()}>
                  <RefreshCw />确认补转选中的 {repairSelected.length} 个缺失文件
                </Button>
              ) : null}
            </Section>
          ) : null}

          {detail.source_audit?.available ? (
            <Section title="实际转存来源核验" icon={ShieldCheck}>
              <p className="text-xs leading-5 text-muted-foreground">
                转存校验只证明文件已落到网盘，不能证明影片内容与片名一致。以下是当时实际选择的原始分享文件。
              </p>
              <ul className="space-y-2 rounded-lg border bg-muted/30 p-3 text-xs">
                {detail.source_audit.entries.map((file, index) => (
                  <li key={`${file.file_id}-${index}`} className="break-all">
                    <p className="font-medium">{file.source_path || file.source_name}</p>
                    <p className="text-muted-foreground">
                      当时映射：{file.season != null ? `S${String(file.season).padStart(2, '0')}` : '未知季'}
                      {file.episode != null ? ` E${String(file.episode).padStart(3, '0')}` : ' · 集号未知'}
                      {typeof file.size === 'number' ? ` · ${(file.size / (1024 ** 3)).toFixed(2)} GiB` : ''}
                    </p>
                    {file.issue ? <p className="mt-1 font-medium text-destructive">{file.issue}</p> : null}
                  </li>
                ))}
              </ul>
              {detail.source_audit.quarantine?.active ? (
                <div className="space-y-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-xs">
                  <p className="font-semibold text-destructive">此任务已标记错片并暂停对应集数自动转存</p>
                  <p>原因：{detail.source_audit.quarantine.reason}</p>
                  <p>请先在网盘里人工核对、移走或整理错误视频。Reelay 不会自动删除，也不会恢复旧的错误去重记录。</p>
                  {can('resolve_quarantine') ? (
                    <Button size="sm" variant="outline" disabled={busy}
                      onClick={() => void resolveWrongCopy()}>人工处理完成，解除隔离</Button>
                  ) : null}
                </div>
              ) : detail.source_audit.quarantine ? (
                <p className="text-xs text-muted-foreground">历史错片隔离已解除，旧错误去重记录没有恢复。</p>
              ) : can('quarantine_wrong_source') ? (
                <div className="space-y-2 rounded-lg border p-3">
                  {detail.source_audit.suspect ? (
                    <p className="font-medium text-xs text-destructive">
                      原始文件名的集号无法支持入库结论，请立即核对视频内容。
                    </p>
                  ) : null}
                  <Textarea value={wrongCopyReason} onChange={(e) => setWrongCopyReason(e.target.value)}
                    placeholder="例如：E247 实际取自其他目录的 4K.mp4，核对后发现不是《吞噬星空》"
                    rows={2} />
                  <Button variant="outline" size="sm" disabled={busy || wrongCopyReason.trim().length < 5}
                    onClick={() => void quarantineWrongCopy()}>
                    <AlertTriangle />标记错片并暂停自动转存
                  </Button>
                  <p className="text-xs text-muted-foreground">
                    仅撤回这条任务自己的去重声明并阻止后续自动转存；保留原任务与回执，不修改网盘文件。
                  </p>
                </div>
              ) : null}
            </Section>
          ) : null}

          {forcedCopy ? (
            <Section title="人工强制转存（只限本集）" icon={AlertTriangle}>
              <p className="text-xs leading-5 text-muted-foreground">
                已明确绕过本集旧的数据库入库去重。请在下方精确选择分享中的目标源文件，
                然后浏览阿里云盘，选中已经存在的 {currentSeason} 最终剧集文件夹。
                保存方式必须为「直接存入」。不会自动删除、替换已有文件；
                同名文件采用安全后缀保留两份。
              </p>
              <p className="text-xs text-destructive">
                请务必核对季号、集号及源文件 ID。强制复制并不证明已有库内版本与新版本相同。
              </p>
            </Section>
          ) : null}
          {can('select_episodes') ? (
            <Section title="本次只补指定集数（可选）">
              <p className="text-xs leading-5 text-muted-foreground">
                默认会读取完整分享，按季、集自动跳过已有记录，只补缺集。
                你可以指定本次只考虑哪些集，例如 11, 13-15。
                已经核验入库的集仍会去重，不会重复复制、洗版或覆盖。
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <Input aria-label="本次指定补集" className="min-w-48 flex-1"
                  placeholder="留空＝自动；如 11, 13-15"
                  value={onlyEpisodesText} onChange={(e) => setOnlyEpisodesText(e.target.value)} />
                <Button variant="outline" size="sm" disabled={busy}
                  onClick={() => void saveSelectedEpisodes()}>保存本次补集范围</Button>
              </div>
              {meta.share_selection?.only_episodes?.length ? (
                <p className="text-xs text-primary">
                  已设置：仅处理第 {meta.share_selection.only_episodes.join('、')} 集；
                  现在先读取分享清单，再选择需要的版本或源文件。
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">当前：自动补齐分享中尚未入库的剧集</p>
              )}
              <p className="text-xs text-muted-foreground">
                修改补集范围后，旧的源文件和版本选择会失效，避免误用。
                若云盘文件已删除但仍显示已入库，请先核验并修正旧记录，不能借指定集数强行覆盖。
              </p>
            </Section>
          ) : null}

          {can('choose_share_folder') ? (
            <Section title="选择分享源文件或文件夹" icon={FolderTree}>
              <p className="text-xs leading-5 text-muted-foreground">
                想只转存 E247：先保存识别结果中的季与集号，再读取分享并选中精确的
                「247 4K.mp4」源视频。文件夹选择适合单一版本目录；两种方式互斥。
                这里选择的是分享来源，不会改变下方「保存到」的目标目录。
              </p>
              <Button variant="outline" size="sm" disabled={shareFolderBusy || busy}
                onClick={() => void browseShareFolders()}>
                <FolderTree />{shareFolders ? '刷新分享文件夹' : '读取分享文件夹'}
              </Button>
              {shareFolders?.files?.length ? (
                <div className="space-y-2 rounded-lg border p-3">
                  <p className="text-sm font-semibold">精确选择单集源视频（推荐）</p>
                  <Select aria-label="精确选择分享源文件" value={shareFileId}
                    onValueChange={setShareFileId}
                    options={shareFolders.files.map((file) => ({
                      value: file.file_id,
                      label: `${file.path || '分享根目录'}/${file.name}${typeof file.size === 'number'
                        ? ` · ${(file.size / (1024 ** 3)).toFixed(2)} GiB` : ''}${file.selectable
                        ? '' : ' · 无法核验为目标集数'}`,
                      disabled: !file.selectable,
                    }))}
                    placeholder="选取真正的目标集（无集号的 4K.mp4 不能选）" />
                  {shareFileId ? (
                    <p className="text-xs text-muted-foreground">
                      文件确认：
                      {shareFolders.files.find((file) => file.file_id === shareFileId)?.path || '分享根目录'}/
                      {shareFolders.files.find((file) => file.file_id === shareFileId)?.name}。
                      正式复制前会重新读取网盘核对源文件 ID、路径、名称和字节大小。
                    </p>
                  ) : null}
                  <Button size="sm" disabled={shareFolderBusy || busy || !shareFileId}
                    onClick={() => void chooseShareFile()}>确认只转存这个源文件</Button>
                </div>
              ) : shareFolders ? (
                <p className="text-xs text-muted-foreground">
                  未发现可精确选择的单集文件。请先确认片名、类型、季和唯一集号，点击保存修改后再刷新。
                </p>
              ) : null}
              {shareFolders ? (
                <div className="space-y-2 rounded-lg border p-3">
                  <p className="text-sm font-semibold">按整个来源文件夹选择（慎用于多个视频）</p>
                  <Select value={shareFolderId} onValueChange={setShareFolderId}
                    aria-label="分享版本"
                    options={shareFolders.folders.map((folder) => ({
                      value: folder.folder_id,
                      label: `${folder.path} · ${folder.count} 个视频${folder.unrecognized ? `，${folder.unrecognized} 个未识别` : ''}`,
                      disabled: folder.count === 0 || folder.selectable === false,
                    }))}
                    placeholder="请选择一个版本（不会自动混用）" />
                  {shareFolderId ? (
                    <p className="text-xs text-muted-foreground">
                      当前版本：{shareFolders.folders.find((folder) => folder.folder_id === shareFolderId)?.path}
                      {shareFolders.folders.find((folder) => folder.folder_id === shareFolderId)?.episodes.length
                        ? ` · 已识别 ${shareFolders.folders.find((folder) => folder.folder_id === shareFolderId)?.episodes.length} 个季集`
                        : ''}。所选版本缺集时会停止，不会改用另一个文件夹。
                    </p>
                  ) : null}
                  <label className="flex items-center gap-2 text-xs text-muted-foreground">
                    <input type="checkbox" checked={rememberShareFolder}
                      onChange={(e) => setRememberShareFolder(e.target.checked)} />
                    此分享链接以后沿用该版本（文件夹改变时仍会停止核对）
                  </label>
                  {shareFolderId ? (
                    <div className="max-h-36 space-y-1 overflow-y-auto rounded border bg-muted/30 p-2 text-xs">
                      {(shareFolders.folders.find((folder) => folder.folder_id === shareFolderId)?.samples ?? []).map((file, i) => (
                        <div key={`${file.path}-${i}`} className="break-all">
                          {file.path}{typeof file.size === 'number' ? ` · ${(file.size / (1024 ** 3)).toFixed(2)} GiB` : ''}
                        </div>
                      ))}
                      {(shareFolders.folders.find((folder) => folder.folder_id === shareFolderId)?.count ?? 0) > 6
                        ? <p className="text-muted-foreground">仅预览前 6 个文件；完整视频数见上方。</p> : null}
                      {shareFolders.folders.find((folder) => folder.folder_id === shareFolderId)?.unrecognized
                        ? <p className="text-destructive">此文件夹含无法识别集号的视频，不会由 AI 强行匹配目标集数。</p> : null}
                    </div>
                  ) : null}
                  <Button size="sm" disabled={shareFolderBusy || busy || !shareFolderId}
                    onClick={() => void chooseShareFolder()}>确认这个版本</Button>
                </div>
              ) : null}
              {meta.share_selection?.file_id ? (
                <p className="text-xs font-medium text-primary">
                  已锁定单集源文件：{meta.share_selection.file_path || '分享根目录'}/
                  {meta.share_selection.file_name}（文件 ID 校验）。
                </p>
              ) : meta.share_selection?.folder_id ? (
                <p className="text-xs text-muted-foreground">
                  已选择来源目录：{meta.share_selection.path || '分享根目录（仅当前层）'}。
                  不会修改已经开始转存的任务。
                </p>
              ) : null}
            </Section>
          ) : null}

          {conflict ? (
            <Section title="库里已有同一份资源">
              <OrganizeConflict conflict={conflict} />
              <p className="text-xs leading-5 text-muted-foreground">点下方「重新整理」选择保留哪一份。</p>
              <ConflictDialog conflict={conflict} open={choosing} onOpenChange={setChoosing} canReplan={can('reorganize')}
                onChoose={(choice) => void (choice ? run('resolve_conflict', choice) : run('reorganize'))} />
            </Section>
          ) : null}

          {can('approve') && isAliyun ? (
            <Section title="指定阿里云盘保存目录（本次任务）" icon={FolderSearch}>
              <p className="text-xs leading-5 text-muted-foreground">
                自动识别的片名或分类不对时，先在下方「识别结果」修正片名、类型、季和集，
                点击「保存修改」后再选择实际目标。此处只修改本条任务，不影响其他订阅。
              </p>
              {!hasVerifiedIdentity || hasPendingMetadataEdits ? (
                <p className="text-xs text-destructive">
                  {!hasVerifiedIdentity
                    ? '请先保存正确的片名、类型、季和集号，再指定目录。'
                    : '识别信息还有未保存的修改，请先点击下方「保存修改」。'}
                </p>
              ) : null}
              <Button variant="outline" size="sm"
                disabled={busy || !hasVerifiedIdentity || Boolean(hasPendingMetadataEdits)}
                onClick={() => setExistingPickerOpen(true)}>
                <FolderSearch />{existingFolder ? '更换目标文件夹' : '浏览阿里云盘选择保存位置'}
              </Button>
              {existingFolder ? (
                <div className="space-y-2 rounded-md border border-primary/40 bg-primary/5 p-3 text-xs">
                  <p className="font-medium">已选择：/{existingFolder.path.replaceAll(' / ', '/')}</p>
                  <p className="text-muted-foreground">选择本次转存的具体保存方式：</p>
                  <Select value={destinationMode}
                    onValueChange={(v) => setDestinationMode(v as 'exact' | 'organized')}
                    options={[
                      { value: 'exact', label: '直接存入所选文件夹（不再建立作品/Season 子目录）' },
                      { value: 'organized', label: '以所选文件夹为根，按片名/Season 整理' },
                    ]} />
                  <p className="text-muted-foreground">
                    {destinationMode === 'exact'
                      ? '新文件会在这个文件夹中按已确认的作品及集号命名；不会再次套用自动分类。'
                      : '在选中的文件夹下建立作品及季目录，不再叠加原来的自动分类。'}
                  </p>
                  {!existingFolder.driveId
                    ? <p className="text-destructive">未读到资源库 ID，请重新选择文件夹。</p> : null}
                  <Button variant="outline" size="sm" onClick={() => {
                    setExistingFolder(null);
                    setTargetDir('');
                  }}>取消指定目录，恢复自动分类</Button>
                </div>
              ) : null}
              {existingPickerOpen ? (
                <DirectoryPicker open initialProvider="aliyun" fixedProvider="aliyun" forbidRoot
                  onOpenChange={setExistingPickerOpen}
                  onSelect={(dir) => {
                    if (!dir.driveId || dir.id === '0') {
                      toast.error('请选择阿里云盘资源库中已有的非根目录文件夹');
                      return;
                    }
                    setExistingFolder(dir);
                    setDestinationMode('exact');
                    setTargetDir('');
                  }} />
              ) : null}
            </Section>
          ) : null}

          {needDirs && dirs?.dirs.length && !conflict && !existingFolder ? (
            <Section title="预设保存目录（可选）">
              <Select value={targetDir} onValueChange={setTargetDir}
                options={dirs.dirs.map((d) => ({ value: d, label: dirs.paths[d] ? `${d}（${dirs.paths[d]}）` : d }))}
                placeholder={detail.state === 'pending_approval'
                  ? `自动 · ${detail.auto_path ?? detail.auto_dir ?? '按分类'}`
                  : `保持当前目录「${detail.context.save_dir_alias ?? '默认'}」`}
                fallback={detail.state === 'pending_approval'
                  ? { value: autoAlias ?? '', label: `${detail.auto_path ?? detail.auto_dir} · 自动` }
                  : { value: autoAlias ?? '', note: '当前' }} />
              {requireExplicitTarget ? (
                <p className="mt-1.5 text-xs text-muted-foreground">
                  这条任务最初只有网盘分享链接，必须明确选择保存目录。
                  请确认该目录属于阿里云盘资源库，并与《吞噬星空》之前的集数所在位置一致。
                </p>
              ) : null}
              {targetDir && targetDir !== autoAlias
                ? <p className="mt-1.5 text-xs text-muted-foreground">直接存入「{dirs.paths[targetDir] ?? targetDir}」，不再套用分类子目录。</p> : null}
            </Section>
          ) : null}

          {can('correct') ? (
            <Section title="识别结果（可修改）">
              <div className="grid grid-cols-2 gap-3">
                <Field label="片名" className="col-span-2"><Input value={String(draft.title ?? '')} onChange={(e) => setDraft({ ...draft, title: e.target.value })} /></Field>
                <Field label="类型">
                  <Select value={String(draft.media_type ?? '')} onValueChange={(v) => setDraft({ ...draft, media_type: v || null })}
                    placeholder="未识别" options={MEDIA_TYPES} />
                </Field>
                <Field label="年份"><Input inputMode="numeric" value={String(draft.year ?? '')} onChange={(e) => setDraft({ ...draft, year: Number(e.target.value) || null })} /></Field>
                <Field label="季"><Input inputMode="numeric" value={String(draft.season ?? '')} onChange={(e) => setDraft({ ...draft, season: e.target.value === '' ? null : Number(e.target.value) })} /></Field>
                <Field label="清晰度"><Input value={String(draft.resolution ?? '')} onChange={(e) => setDraft({ ...draft, resolution: e.target.value })} /></Field>
                <Field label="集号（逗号分隔）" className="col-span-2"><Input value={episodesText} onChange={(e) => setEpisodesText(e.target.value)} /></Field>
                <Field label="判断理由" className="col-span-2"><Textarea value={String(draft.reason ?? '')} onChange={(e) => setDraft({ ...draft, reason: e.target.value })} className="min-h-16" /></Field>
              </div>
            </Section>
          ) : parsed?.reason ? (
            <Section title="判断理由">
              <p className="text-sm leading-6 text-muted-foreground">{parsed.reason}</p>
            </Section>
          ) : null}

          {plan.length ? (
            <Section title="整理结果" icon={FolderTree}>
              <ul className="space-y-1 rounded-lg border bg-muted/40 p-3 font-mono text-xs leading-5">
                {plan.map((item) => (
                  <li key={`${item.target_dir}/${item.target_name}`} className="break-all">
                    <span className="text-muted-foreground">{item.target_dir}/</span>{item.target_name}
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}

          <Section title="原始消息">
            <div className="max-h-56 overflow-y-auto whitespace-pre-wrap break-words rounded-lg border bg-muted/40 p-3 text-[13px] leading-6">
              {detail.context.raw.text || '（空消息）'}
            </div>
          </Section>

          <Section title="处理过程">
            <ol className="relative ml-1.5 border-l border-border pl-4">
              {detail.context.trail.length ? detail.context.trail.map((line, index) => (
                <li key={`${index}-${line}`} className="relative pb-2 text-xs leading-5 text-muted-foreground last:pb-0">
                  <span className="absolute -left-[21px] top-1.5 size-2 rounded-full bg-primary/60 ring-2 ring-background" />
                  {line}
                </li>
              )) : <li className="text-xs text-muted-foreground">暂无处理记录</li>}
            </ol>
          </Section>
        </div>
      )}
    </Sheet>
  );
}
