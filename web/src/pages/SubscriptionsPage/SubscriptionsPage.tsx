import { Ban, BellPlus, BellRing, CircleAlert, Flag, FlaskConical, Lightbulb, Loader2, RefreshCw, Search, Trash2, Undo2 } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import { subscriptionApi, type IIgnoredWork, type IResolveResp, type IShowCandidate, type ISubscription, type ISubscriptionSimulation } from '@/api/subscriptions';
import { ErrorState } from '@/components/app/ErrorState';
import { MediaCover } from '@/components/app/MediaCover';
import { Page } from '@/components/app/Page';
import { Panel } from '@/components/app/Panel';
import { Segmented } from '@/components/app/Segmented';
import { Select } from '@/components/app/Select';
import { ToneBadge } from '@/components/app/StatusBadge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Skeleton } from '@/components/ui/skeleton';
import { useAsync } from '@/hooks/useAsync';
import { compactEpisodes, relativeTime } from '@/lib/format';
import { MEDIA_TYPE_LABELS } from '@/lib/status';
import { announceSubscription } from '@/lib/subscriptions';
import { cn, errorMessage } from '@/lib/utils';

function CandidateCard({ candidate, wanted, onSubscribe, busy }: {
  candidate: IShowCandidate; wanted: number | null; busy: boolean;
  onSubscribe: (season: number, total: number | null) => void;
}) {
  const seasons = candidate.seasons;
  const initial = seasons.some((s) => s.season === wanted) ? wanted : seasons.at(-1)?.season ?? wanted ?? 1;
  const [season, setSeason] = useState(String(initial));
  const picked = seasons.find((s) => String(s.season) === season);
  return (
    <div className="flex gap-3 rounded-lg border border-border bg-background/60 p-3">
      <MediaCover title={candidate.title} posterPath={candidate.poster_path} mediaType="tv" quality="w185" className="w-14 shrink-0" />
      <div className="flex min-w-0 flex-1 flex-col">
        <p className="truncate text-sm font-medium">
          {candidate.title}
          {candidate.year ? <span className="ml-1.5 text-xs font-normal text-muted-foreground">{candidate.year}</span> : null}
        </p>
        {candidate.original_title && candidate.original_title !== candidate.title ? (
          <p className="truncate text-xs text-muted-foreground">{candidate.original_title}</p>
        ) : null}
        {candidate.overview ? <p className="mt-1 line-clamp-2 text-xs leading-5 text-muted-foreground">{candidate.overview}</p> : null}
        <div className="mt-auto flex items-center gap-2 pt-2">
          {seasons.length ? (
            <Select value={season} onValueChange={setSeason} aria-label="选择季" className="w-36"
              options={seasons.map((s) => ({ value: String(s.season), label: `第 ${s.season} 季${s.episodes ? ` · ${s.episodes} 集` : ''}` }))} />
          ) : null}
          <Button size="sm" disabled={busy} className="ml-auto"
            onClick={() => onSubscribe(Number(season), picked?.episodes ?? null)}>
            <BellPlus />订阅
          </Button>
        </div>
      </div>
    </div>
  );
}

function SubscriptionRow({ item, busy, onRemove, onReopen, onTest, onStart }: {
  item: ISubscription; busy: boolean; onRemove: () => void; onReopen: () => void; onTest: () => void; onStart: () => void;
}) {
  const finished = item.finished;
  return (
    <div className="flex items-center gap-3 py-3 first:pt-0 last:pb-0">
      <span className={cn('grid size-9 shrink-0 place-items-center rounded-lg',
        finished ? 'bg-muted text-muted-foreground' : 'bg-primary/10 text-primary')}>
        {finished ? <Flag className="size-4" /> : <BellRing className="size-4" />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="truncate text-sm font-medium">《{item.title}》第 {item.season} 季</span>
          <ToneBadge tone="neutral">{item.category}</ToneBadge>
          {finished ? <ToneBadge tone="neutral">已完结</ToneBadge>
            : item.auto ? <ToneBadge tone="success">新集自动转存</ToneBadge> : <ToneBadge tone="neutral">只提醒缺集</ToneBadge>}
          {!item.tmdbid ? <ToneBadge tone="warning" className="max-sm:hidden">按片名匹配</ToneBadge> : null}
        </div>
        <p className="mt-0.5 truncate text-xs text-muted-foreground">
          {finished ? `${finished.episodes} 集全部入库，${finished.date} 自动结束订阅` : <>
            已入库 {item.have.length}{item.total ? ` / ${item.total}` : ''} 集
            {item.have.length ? `（${compactEpisodes(item.have)}）` : ''}
            {item.missing.length ? ` · 缺 ${compactEpisodes(item.missing)}` : ''}
            {item.aired ? ` · TMDB 已播 ${item.aired} 集` : ''}
          </>}
        </p>
        {!finished && item.observed?.length ? (
          <p className="mt-0.5 text-xs text-muted-foreground">
            网盘索引可能已有 {item.observed.length} 集（{compactEpisodes(item.observed)}）。
            这些不是 Reelay 已转存记录，转存前会重新核验真实文件。
            {!item.scan_complete ? ' 扫描尚未完整。' : ''}
          </p>
        ) : null}
        {!finished && item.start_episode !== null ? (
          <p className="mt-0.5 text-xs text-primary">仅从 E{item.start_episode} 起自动转存（含本集）</p>
        ) : null}
        {!finished && item.discovery ? (
          <p className="mt-0.5 text-xs text-muted-foreground">
            自动检索：{item.discovery.checked_at
              ? `上次 ${relativeTime(item.discovery.checked_at)}`
              : '尚未完成'}
            {item.discovery.source ? ` · ${item.discovery.source}` : ''}
            {item.discovery.checked_at ? ` · 候选 ${item.discovery.matches ?? 0} 条` : ''}
            {item.discovery.error ? ` · ${item.discovery.error}` : ''}
          </p>
        ) : null}
      </div>
      <Button size="sm" variant="outline" disabled={busy} onClick={onStart} title="设置本订阅最早自动转存的集号">
        起始集
      </Button>
      <Button size="sm" variant="outline" disabled={busy} onClick={onTest}
        title="用实际识别与订阅规则模拟，不会提交转存任务">
        <FlaskConical />模拟测试
      </Button>
      {finished ? (
        <Button variant="outline" size="sm" disabled={busy} onClick={onReopen} title="恢复自动转存与缺集巡检（如出了特别篇）">
          <Undo2 />继续订阅
        </Button>
      ) : null}
      <Button variant="ghost" size="icon" disabled={busy} onClick={onRemove} aria-label={`删除订阅《${item.title}》`}
        title={finished ? '删除订阅' : '取消订阅'}
        className="size-9 shrink-0 text-muted-foreground hover:bg-destructive/10 hover:text-destructive sm:size-8">
        <Trash2 />
      </Button>
    </div>
  );
}

export default function SubscriptionsPage() {
  const subs = useAsync(() => subscriptionApi.list(), [], '读取订阅失败');
  const ignoredWorks = useAsync(() => subscriptionApi.ignoredWorks(), [], '读取不再接收的作品失败');
  const data = subs.data;
  const ignored = ignoredWorks.data?.items;
  const [query, setQuery] = useState('');
  const [resolved, setResolved] = useState<IResolveResp | null>(null);
  const [searching, setSearching] = useState(false);
  const [busy, setBusy] = useState(false);
  const [category, setCategory] = useState('');  // 空 = 全部类型
  const [testItem, setTestItem] = useState<ISubscription | null>(null);
  const [testText, setTestText] = useState('');
  const [testResult, setTestResult] = useState<ISubscriptionSimulation | null>(null);
  const [testBusy, setTestBusy] = useState(false);
  const [startOnAdd, setStartOnAdd] = useState('');
  const [startItem, setStartItem] = useState<ISubscription | null>(null);
  const [startInput, setStartInput] = useState('');
  const [startBusy, setStartBusy] = useState(false);
  const [resourceText, setResourceText] = useState('');
  const [resourceBusy, setResourceBusy] = useState(false);

  /** 操作后两份列表都重新读取。 */
  const load = () => { subs.reload(); ignoredWorks.reload(); };
  /** 执行一个会改订阅的操作：成功提示后刷新，失败给出原因。 */
  const run = async (fn: () => Promise<unknown>, done: string, failed: string) => {
    setBusy(true);
    try {
      await fn();
      toast.success(done);
      load();
    } catch (e) {
      toast.error(errorMessage(e, failed));
    } finally {
      setBusy(false);
    }
  };

  const search = async () => {
    if (!query.trim()) return;
    setSearching(true);
    try {
      setResolved(await subscriptionApi.resolve(query.trim()));
    } catch (e) {
      toast.error(errorMessage(e, '查找失败'));
    } finally {
      setSearching(false);
    }
  };

  const subscribe = async (item: { title: string; season: number; tmdbid?: number | null; total?: number | null }) => {
    const threshold = startOnAdd.trim() ? Number(startOnAdd) : null;
    if (threshold !== null && (!Number.isInteger(threshold) || threshold < 1 || threshold > 100000)) {
      toast.error('起始集必须是 1–100000 的整数');
      return;
    }
    setBusy(true);
    try {
      const r = await subscriptionApi.add({ ...item, start_episode: threshold });
      announceSubscription(r.subscription, r.pending);
      setResolved(null);
      setQuery('');
      setStartOnAdd('');
      load();
    } catch (e) {
      toast.error(errorMessage(e, '订阅失败'));
    } finally {
      setBusy(false);
    }
  };

  const dismiss = (item: { title: string; season: number; tmdbid: number | null }) => run(
    () => subscriptionApi.dismissSuggestion(item), `不再建议订阅《${item.title}》第 ${item.season} 季`, '操作失败');
  const remove = (item: ISubscription) => run(
    () => subscriptionApi.remove({ title: item.title, season: item.season, tmdbid: item.tmdbid }),
    `已取消订阅《${item.title}》第 ${item.season} 季`, '取消订阅失败');
  const reopen = (item: ISubscription) => run(
    () => subscriptionApi.reopen({ title: item.title, season: item.season, tmdbid: item.tmdbid }),
    `已继续订阅《${item.title}》第 ${item.season} 季`, '操作失败');
  const restore = (item: IIgnoredWork) => run(() => subscriptionApi.unignoreWork(item), `已恢复接收《${item.title}》`, '恢复失败');

  const categories = [...new Set((data?.items ?? []).map((i) => i.category))];
  const shown = (data?.items ?? []).filter((i) => !category || i.category === category);
  const active = shown.filter((i) => !i.finished);
  const finished = shown.filter((i) => i.finished);
  const row = (item: ISubscription) => (
    <SubscriptionRow key={`${item.tmdbid ?? item.title}-${item.season}`} item={item} busy={busy}
      onRemove={() => void remove(item)} onReopen={() => void reopen(item)}
      onTest={() => {
        setTestItem(item);
        setTestText('');
        setTestResult(null);
      }}
      onStart={() => {
        setStartItem(item);
        setStartInput(item.start_episode?.toString() ?? '');
      }} />
  );
  const saveStart = async () => {
    if (!startItem || startBusy) return;
    const floor = startInput.trim() ? Number(startInput) : null;
    if (floor !== null && (!Number.isInteger(floor) || floor < 1 || floor > 100000)) {
      toast.error('起始集必须是 1–100000 的整数');
      return;
    }
    setStartBusy(true);
    try {
      await subscriptionApi.setStart(startItem.id, floor);
      toast.success(floor ? `从 E${floor} 开始自动转存（含本集）` : '已清除集数下限');
      setStartItem(null);
      subs.reload();
    } catch (e) {
      toast.error(errorMessage(e, '保存起始集失败'));
    } finally {
      setStartBusy(false);
    }
  };
  const submitResource = async () => {
    if (!resourceText.trim() || resourceBusy) return;
    setResourceBusy(true);
    try {
      const result = await subscriptionApi.submitManual(resourceText.trim());
      setResourceText('');
      toast.success(`已创建任务 ${result.task_id}，后台将自动识别并核验资源`);
    } catch (e) {
      toast.error(errorMessage(e, '提交资源失败'));
    } finally {
      setResourceBusy(false);
    }
  };
  const runSimulation = async () => {
    if (!testItem || testBusy) return;
    setTestBusy(true);
    try {
      setTestResult(await subscriptionApi.simulate(testItem.id, testText.trim() || undefined));
    } catch (e) {
      toast.error(errorMessage(e, '模拟测试失败'));
    } finally {
      setTestBusy(false);
    }
  };

  return (
    <Page
      title="追剧订阅"
      description="订阅的剧之后的新集直接转存，不再逐条等你确认；过滤规则、置信度、TMDB 匹配与分享清单校验照常生效。每日缺集巡检也会检查订阅的剧。"
    >
      {subs.error && !data ? (
        <ErrorState title="订阅读取失败" error={subs.error} onRetry={subs.reload} retrying={subs.loading} className="mb-5" />
      ) : null}

      {data?.inactive.length ? (
        <div role="alert" className="mb-5 flex items-start gap-3 rounded-xl border border-warning/35 bg-warning/8 px-4 py-3 text-sm">
          <CircleAlert className="mt-0.5 size-4 shrink-0 text-warning" />
          <p className="min-w-0 leading-6">
            「{data.inactive.join('」「')}」的转存方式是「规则判断」，订阅的新集仍会等你确认。
            可以在 <Link to="/config?tab=sources" className="text-primary hover:underline">设置 → 监听来源</Link> 把来源的转存方式改为「规则 + 订阅放行」。
          </p>
        </div>
      ) : null}

      <div className="mb-4 rounded-lg border border-border bg-card px-4 py-3 text-xs leading-5 text-muted-foreground">
        自动追剧已使用现有 Telegram 来源；后台定时搜索最近的订阅资源消息，
        命中后仍经过 TMDB 匹配、过滤、去重及转存核验。无需每集人工操作。
        <span className="ml-1">如需检查某部剧为什么没有自动放行，点击该行「模拟测试」。</span>
      </div>
      <div className="grid gap-5 lg:grid-cols-5 lg:items-start">
        <div className="min-w-0 space-y-5 lg:col-span-3">
          <Panel icon={Search} title="立即转存已有分享"
            description="粘贴 Telegram 频道中的完整资源正文（包含阿里云盘或 115 分享链接），不必等待该资源再次发布。仅本次直接提交，仍执行原有识别、过滤、去重和网盘验证。">
            <div className="space-y-3">
              <Textarea rows={3} maxLength={12000} value={resourceText}
                onChange={(e) => setResourceText(e.target.value)} disabled={resourceBusy}
                placeholder="例如：https://t.me/shareAliyun/123456 或 牧神记 (2024) 第106集；https://www.alipan.com/s/..." />
              <div className="flex items-center gap-3">
                <Button disabled={resourceBusy || !resourceText.trim()} onClick={() => void submitResource()}>
                  {resourceBusy ? <Loader2 className="animate-spin" /> : <BellPlus />}
                  {resourceBusy ? '正在提交' : '立即加入转存队列'}
                </Button>
                <Link to="/tasks" className="text-xs text-primary hover:underline">查看任务</Link>
              </div>
              <p className="text-xs leading-5 text-muted-foreground">
                可粘贴已配置频道的 t.me/频道名/消息ID、完整资源消息或网盘分享链接。
                单独网盘链接若无法识别作品会进入人工处理；同一链接重复提交不会重复建立任务。
                仅允许使用你已经登录且有读取权限的 Telegram 来源。
                不会改变现有监听来源的「人工确认」设置。
              </p>
            </div>
          </Panel>
          <Panel icon={Search} title="添加订阅" description="按 TMDB 选剧并可指定从哪一集开始，防止首次订阅转存旧集。">
            <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void search(); }}>
              <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="如：我在追《一瓯春》第二季"
                className="bg-card" aria-label="在追的剧" />
              <Button type="submit" disabled={searching || !query.trim()}>
                {searching ? <Loader2 className="animate-spin" /> : <Search />}查找
              </Button>
            </form>
            <div className="mt-3 flex flex-col gap-1.5">
              <label htmlFor="new-sub-start" className="text-xs font-medium">最早自动接收集号（可选）</label>
              <Input id="new-sub-start" type="number" inputMode="numeric" min={1} max={100000}
                value={startOnAdd} onChange={(e) => setStartOnAdd(e.target.value)}
                placeholder="如：当前最新 E100，则填写 100" className="bg-card" />
              <p className="text-xs text-muted-foreground">填写 100：只自动接收 E100 及后续新集；填写 101：从下一集开始。留空沿用单集追更规则，不表示已经核实当前最新集。</p>
            </div>
            {resolved ? (
              <div className="mt-4 space-y-3">
                {resolved.candidates.length ? resolved.candidates.map((c) => (
                  <CandidateCard key={c.tmdbid} candidate={c} wanted={resolved.query.season} busy={busy}
                    onSubscribe={(season, total) => void subscribe({ title: c.title, season, tmdbid: c.tmdbid, total })} />
                )) : (
                  <div className="rounded-lg border border-dashed border-border p-3 text-sm">
                    <p className="text-muted-foreground">
                      {resolved.tmdb ? '在 TMDB 上没有找到匹配的剧。' : '未配置 TMDB，无法查找条目。'}
                      可以按片名订阅，资源识别出的规范片名与它一致时才会自动转存。
                    </p>
                    {resolved.query.title ? (
                      <Button size="sm" variant="outline" className="mt-2.5" disabled={busy}
                        onClick={() => void subscribe({ title: resolved.query.title, season: resolved.query.season ?? 1 })}>
                        <BellPlus />按片名订阅《{resolved.query.title}》第 {resolved.query.season ?? 1} 季
                      </Button>
                    ) : null}
                  </div>
                )}
              </div>
            ) : null}
          </Panel>

          <Panel icon={BellRing} title="已订阅"
            actions={<Button size="sm" variant="outline" onClick={subs.reload} disabled={subs.loading}>
              <RefreshCw className={subs.loading ? 'animate-spin' : ''} />刷新检索状态
            </Button>}
            description={data ? `共 ${data.items.length} 部${data.items.some((i) => i.finished)
              ? `，其中 ${data.items.filter((i) => i.finished).length} 部已完结` : ''}` : undefined}>
            {!data ? (subs.error ? <p className="text-sm text-muted-foreground">读取失败，见页面上方。</p>
              : <div className="space-y-3">{[0, 1].map((i) => <Skeleton key={i} className="h-12" />)}</div>)
              : data.items.length ? (
                <div className="space-y-4">
                  {categories.length > 1 ? (
                    <Segmented label="类型" size="md" value={category} onChange={setCategory}
                      options={['', ...categories].map((c) => ({
                        value: c,
                        label: <>{c || '全部'}<span className="text-xs tabular-nums text-muted-foreground">
                          {data.items.filter((i) => !c || i.category === c).length}</span></>,
                      }))} />
                  ) : null}
                  {active.length ? <div className="divide-y divide-border/70">{active.map(row)}</div>
                    : <p className="text-sm text-muted-foreground">这个类型没有在追的剧。</p>}
                  {finished.length ? (
                    <div>
                      <p className="mb-1 text-xs font-medium text-muted-foreground">已完结 · 播完且全部入库后自动结束</p>
                      <div className="divide-y divide-border/70">{finished.map(row)}</div>
                    </div>
                  ) : null}
                </div>
              ) : <p className="text-sm text-muted-foreground">还没有订阅。在待审批里点「追这部剧」，或在上面用一句话添加。</p>}
          </Panel>
        </div>

        <div className="min-w-0 space-y-5 lg:col-span-2">
          <Panel icon={Lightbulb} title="订阅建议"
            description="同一部剧同一季批准过至少 3 条、还没订阅的剧；满 3 条时也会在 Telegram 上问你一次。">
            {!data ? (subs.error ? null : <Skeleton className="h-20" />) : data.suggestions.length ? (
              <div className="space-y-3">
                {data.suggestions.map((s) => (
                  <div key={`${s.tmdbid ?? s.title}-${s.season}`} className="rounded-lg border border-border bg-background/60 p-3">
                    <p className="text-sm leading-6">{s.text}</p>
                    <div className="mt-2 flex flex-wrap gap-2">
                      <Button size="sm" disabled={busy}
                        onClick={() => void subscribe({ title: s.title, season: s.season, tmdbid: s.tmdbid })}>
                        <BellPlus />订阅第 {s.season} 季
                      </Button>
                      <Button size="sm" variant="ghost" className="text-muted-foreground" disabled={busy}
                        onClick={() => void dismiss({ title: s.title, season: s.season, tmdbid: s.tmdbid })}>
                        不用了
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            ) : <p className="text-sm text-muted-foreground">暂时没有。批准过几集同一部剧后，这里会提示你订阅。</p>}
          </Panel>

          <Panel icon={Ban} title="不再接收"
            description="在 Telegram 审批卡片点「不再接收」的作品：之后的消息识别完直接忽略。">
            {!ignored ? (ignoredWorks.error
              ? <ErrorState title="读取失败" error={ignoredWorks.error} onRetry={ignoredWorks.reload} retrying={ignoredWorks.loading} className="border-none py-6" />
              : <Skeleton className="h-12" />) : ignored.length ? (
              <div className="divide-y divide-border/70">
                {ignored.map((item) => (
                  <div key={`${item.kind ?? ''}-${item.tmdbid ?? item.title}`} className="flex items-center gap-3 py-2.5 first:pt-0 last:pb-0">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium">{item.title}</p>
                      <p className="text-xs text-muted-foreground">
                        {item.kind ? MEDIA_TYPE_LABELS[item.kind] : '按片名'}
                        {item.tmdbid ? ` · TMDB ${item.tmdbid}` : ''}
                      </p>
                    </div>
                    <Button size="sm" variant="outline" disabled={busy} onClick={() => void restore(item)}>
                      <Undo2 />恢复接收
                    </Button>
                  </div>
                ))}
              </div>
            ) : <p className="text-sm text-muted-foreground">没有。</p>}
          </Panel>
        </div>
      </div>
      <Dialog open={Boolean(startItem)} onOpenChange={(open) => { if (!open && !startBusy) setStartItem(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>设置起始集 · {startItem?.title}</DialogTitle>
            <DialogDescription>只影响今后自动放行的单集，不补历史合集，也不改变正在排队的任务。</DialogDescription>
          </DialogHeader>
          <label className="text-sm font-medium" htmlFor="edit-sub-start">最早自动接收集号（含本集）</label>
          <Input id="edit-sub-start" type="number" min={1} max={100000}
            value={startInput} disabled={startBusy} onChange={(e) => setStartInput(e.target.value)}
            placeholder="例如：100（留空则取消限制）" />
          <p className="text-xs text-muted-foreground">当前 E100：填写 100 可以接收 E100；填写 101 则从 E101 开始。仍会检查季集、现有文件和去重，无法确认的交给人工。</p>
          <Button onClick={() => void saveStart()} disabled={startBusy}>
            {startBusy ? '保存中' : '保存起始集'}
          </Button>
        </DialogContent>
      </Dialog>
      <Dialog open={Boolean(testItem)} onOpenChange={(open) => { if (!open && !testBusy) setTestItem(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>订阅模拟测试 · {testItem?.title} 第 {testItem?.season} 季</DialogTitle>
            <DialogDescription>
              优先读取最近已接收的对应频道消息；也可以粘贴一条真实资源消息。
              仅运行识别和订阅决策，不产生任务、不访问或修改网盘文件。
            </DialogDescription>
          </DialogHeader>
          <Textarea value={testText} onChange={(event) => setTestText(event.target.value)}
            rows={5} maxLength={12000} disabled={testBusy}
            placeholder="可留空：自动选择已接收的匹配消息。或者粘贴频道中的完整资源消息（含分享链接）。" />
          <Button disabled={testBusy} onClick={() => void runSimulation()}>
            {testBusy ? <Loader2 className="animate-spin" /> : <FlaskConical />}
            {testBusy ? '分析中' : '开始模拟测试'}
          </Button>
          {testResult ? (
            <div role="status" className="max-h-72 space-y-2 overflow-auto rounded-lg border border-border bg-background/60 p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <ToneBadge tone={testResult.status === 'automatic' ? 'success' :
                  testResult.status === 'blocked' ? 'danger' : 'warning'}>
                  {testResult.status === 'automatic' ? '预计自动放行' :
                   testResult.status === 'no_sample' ? '尚无匹配样例' :
                   testResult.status === 'manual' ? '需要人工处理' :
                   testResult.status === 'not_matched' ? '未匹配该订阅' :
                   testResult.status === 'no_source' ? '没有监听来源' : '被规则阻止'}
                </ToneBadge>
                {testResult.source ? <span className="text-xs text-muted-foreground">{testResult.source}</span> : null}
                {testResult.example_origin ? (
                  <span className="text-xs text-muted-foreground">
                    {testResult.example_origin === 'telegram_search' ? '· Telegram 频道历史' :
                     testResult.example_origin === 'pasted' ? '· 粘贴的消息' : '· 已接收的消息'}
                  </span>
                ) : null}
              </div>
              <p>{testResult.reason}</p>
              {testResult.parsed ? (
                <p className="text-muted-foreground">
                  识别：{testResult.parsed.title || '未知'} · 第 {testResult.parsed.season ?? '?'} 季
                  {testResult.parsed.episodes?.length
                    ? ` · 第 ${compactEpisodes(testResult.parsed.episodes)} 集` : ''}
                </p>
              ) : null}
              {testResult.routing ? <p className="text-muted-foreground">
                目标网盘：{testResult.routing.target_storage === 'aliyun' ? '阿里云盘' : '115'}
                {testResult.routing.compatible ? '' : '（与分享网盘不一致）'}
              </p> : null}
              {testResult.target_preview ? <p className="text-muted-foreground">预计目录：{testResult.target_preview}</p> : null}
              {testResult.trail?.length ? (
                <details className="rounded-md border border-border/70 px-2 py-1">
                  <summary className="cursor-pointer text-xs">查看识别与决策轨迹</summary>
                  <div className="mt-2 space-y-1 text-xs text-muted-foreground">
                    {testResult.trail.map((step, i) => <p key={i}>{step}</p>)}
                  </div>
                </details>
              ) : null}
              <p className="text-xs text-muted-foreground">
                本次测试没有创建任务，也未检查真实分享是否有效及目标是否已存在。
              </p>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
    </Page>
  );
}
