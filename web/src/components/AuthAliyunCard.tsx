import { useEffect, useRef, useState } from 'react';
import { ChevronDown, CircleAlert, Cloud, KeyRound, Loader2, QrCode, RefreshCw, ShieldCheck, Trash2, X } from 'lucide-react';
import { toast } from 'sonner';
import { Panel } from '@/components/app/Panel';
import { ToneBadge } from '@/components/app/StatusBadge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { storageApi, type IAliyunQrStart } from '@/api/storage';
import { useAsync } from '@/hooks/useAsync';
import { confirmDialog } from '@/lib/confirm';
import { fullTime, relativeTime } from '@/lib/format';
import { connectionMeta } from '@/lib/status';
import { errorMessage } from '@/lib/utils';

/** Aliyun shares and file management use a single rotating refresh token. */
export default function AuthAliyunCard() {
  const loaded = useAsync(() => storageApi.authAliyunStatus(), [], '读取阿里云盘登录状态失败');
  const { reload, mutate } = loaded;
  const status = loaded.data;
  const [token, setToken] = useState('');
  const [qr, setQr] = useState<IAliyunQrStart | null>(null);
  const [qrMessage, setQrMessage] = useState('');
  const [qrError, setQrError] = useState('');
  const [starting, setStarting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [pinging, setPinging] = useState(false);
  const [clearing, setClearing] = useState(false);
  const automaticProbe = useRef(false);

  // A new web process does not retain the in-memory auth result. If neither
  // local memory nor a recent database observation exists, verify once without
  // forcing users to click "检测并刷新" on every visit. No endless retry loop:
  // after a transient error the button remains available for manual retry.
  useEffect(() => {
    if (status?.configured === false) { automaticProbe.current = false; return; }
    if (!status?.configured || status.state !== 'unknown' || loaded.loading
        || qr || saving || clearing || automaticProbe.current) return;
    automaticProbe.current = true;
    setPinging(true);
    void storageApi.authAliyunPing().then(
      (result) => mutate(() => result),
      () => reload(),
    ).finally(() => setPinging(false));
  }, [status?.configured, status?.state, loaded.loading, qr, saving, clearing, mutate, reload]);

  const start = async () => {
    setStarting(true);
    setQrError('');
    try {
      setQr(await storageApi.authAliyunQrStart());
      setQrMessage('请使用手机阿里云盘 App 扫码，并确认登录');
    } catch (error) {
      setQrError(errorMessage(error, '无法创建阿里云盘登录二维码'));
    } finally {
      setStarting(false);
    }
  };

  const stop = () => {
    if (qr) void storageApi.authAliyunQrCancel(qr.uid).catch(() => {});
    setQr(null);
  };

  useEffect(() => {
    if (!qr) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = await storageApi.authAliyunQrStatus(qr.uid);
        if (cancelled) return;
        setQrMessage(result.message);
        if (result.authenticated) {
          setQr(null);
          setQrError('');
          reload();
          toast.success('阿里云盘登录成功，令牌已验证并保存');
          return;
        }
        if (result.status === 'EXPIRED' || result.status === 'CANCELED') {
          setQr(null);
          setQrError(result.message);
          return;
        }
        timer = setTimeout(() => void poll(), 2500);
      } catch (error) {
        if (cancelled) return;
        setQr(null);
        setQrError(errorMessage(error, '扫码状态查询失败，请重新扫码'));
        reload();
      }
    };
    timer = setTimeout(() => void poll(), 1500);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [qr, reload]);

  const ping = async () => {
    setPinging(true);
    try {
      const result = await storageApi.authAliyunPing();
      mutate(() => result);
      if (result.state === 'active') toast.success('阿里云盘连接正常，凭证已刷新');
      else toast.error(result.error || '阿里云盘连接未通过检测');
    } catch (error) {
      toast.error(errorMessage(error, '阿里云盘连接检测失败'));
      reload();
    } finally {
      setPinging(false);
    }
  };

  const save = async () => {
    if (!token.trim()) { toast.error('请粘贴阿里云盘 Refresh Token'); return; }
    setSaving(true);
    try {
      const result = await storageApi.authAliyunSetToken(token.trim());
      mutate(() => result.status);
      setToken('');
      toast.success('令牌验证通过，已安全保存');
    } catch (error) {
      toast.error(errorMessage(error, '令牌验证失败，旧登录信息已保留'));
    } finally {
      setSaving(false);
    }
  };

  const clear = async () => {
    if (!(await confirmDialog({
      title: '清除阿里云盘登录信息？',
      description: '仅清除 Reelay 保存的凭证，不会删除网盘文件。正在转存的任务可能暂停，待重新登录后再核验。',
      confirmText: '清除本地凭证', cancelText: '保留登录', destructive: true,
    }))) return;
    setClearing(true);
    try {
      const result = await storageApi.authAliyunClearToken();
      mutate(() => result.status);
      stop();
      toast.success('阿里云盘本地登录信息已清除');
    } catch (error) {
      toast.error(errorMessage(error, '清除登录信息失败'));
    } finally {
      setClearing(false);
    }
  };

  const meta = connectionMeta(status?.state ?? 'unknown');
  const busy = saving || starting || pinging || clearing;

  return (
    <Panel
      icon={Cloud}
      title="阿里云盘连接"
      description="扫码一次登录，默认使用资源库（数据盘），不是备份盘；分享转存、目录浏览和整理共用凭证。"
      actions={<ToneBadge tone={meta.tone}>
        {status?.last_observation && status.state === 'active' ? '最近检测正常' : meta.label}
      </ToneBadge>}
      bodyClassName="space-y-4 p-4 sm:p-5"
    >
      {loaded.error ? (
        <div role="alert" className="flex flex-wrap items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
          <CircleAlert className="size-4 shrink-0" />
          <span className="min-w-0 flex-1">{loaded.error}</span>
          <Button size="sm" variant="outline" onClick={reload}>重新加载</Button>
        </div>
      ) : null}

      <div className="flex flex-wrap items-start gap-3 rounded-lg border border-border bg-background/60 p-4">
        <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary">
          <ShieldCheck className="size-5" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold">账户授权状态</p>
          {status?.configured ? (
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              已保存登录信息{status.drive_id ? ' · 资源库 ID ' + status.drive_id : ''}
              {status.checked_at
                ? <span title={fullTime(status.checked_at)}> · 上次检测 {relativeTime(status.checked_at)}</span>
                : pinging ? ' · 正在自动检查有效性…' : ' · 尚未检测有效性'}
            </p>
          ) : (
            <p className="mt-1 text-xs leading-5 text-muted-foreground">尚未登录。请用阿里云盘 App 扫码。</p>
          )}
          {status?.last_observation && status.state === 'active' ? (
            <p className="mt-1 text-xs text-muted-foreground">
              最近验证成功（读取历史检测结果）；后续云盘操作仍会实时验证登录。
            </p>
          ) : null}
          {status?.error ? <p className="mt-2 text-xs text-destructive">{status.error}</p> : null}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" disabled={busy || Boolean(qr)} onClick={() => void start()}>
          {starting ? <Loader2 className="animate-spin" /> : <QrCode />}
          {status?.configured ? '重新扫码登录' : '扫码登录阿里云盘'}
        </Button>
        <Button size="sm" variant="outline" disabled={busy || !status?.configured} onClick={() => void ping()}>
          {pinging ? <Loader2 className="animate-spin" /> : <RefreshCw />}{pinging ? '自动检测中' : '重新检测'}
        </Button>
        <Button size="sm" variant="ghost" onClick={reload}>同步状态显示</Button>
        {status?.configured ? (
          <Button size="sm" variant="ghost" disabled={busy}
            className="text-destructive hover:bg-destructive/10 hover:text-destructive"
            onClick={() => void clear()}>
            {clearing ? <Loader2 className="animate-spin" /> : <Trash2 />}清除登录
          </Button>
        ) : null}
      </div>

      {qr ? (
        <div className="flex flex-col items-center gap-4 rounded-lg border border-primary/25 bg-primary/[0.04] p-4 sm:flex-row sm:items-start sm:gap-5">
          <img src={storageApi.authAliyunQrImageUrl(qr.uid)}
            alt="阿里云盘扫码登录二维码"
            className="size-44 shrink-0 rounded-lg bg-white p-2 shadow-md ring-1 ring-black/5" />
          <div className="min-w-0 flex-1 text-center sm:text-left">
            <p className="text-xs font-medium text-primary">阿里云盘 App</p>
            <h4 className="mt-1 text-base font-semibold">扫码并确认登录</h4>
            <p role="status" className="mt-2 flex items-center justify-center gap-2 text-sm sm:justify-start">
              <Loader2 className="size-3.5 shrink-0 animate-spin text-primary" />{qrMessage}
            </p>
            <p className="mt-3 text-xs leading-5 text-muted-foreground">
              二维码有效期约 5 分钟；确认后 Reelay 会自动验证并保存刷新令牌，浏览器不会显示令牌原文。
              不要把二维码分享给其他人。
            </p>
            <Button size="sm" variant="ghost" className="mt-3" onClick={stop}><X />取消扫码</Button>
          </div>
        </div>
      ) : null}

      {qrError ? <p role="alert" className="flex items-start gap-2 text-xs text-destructive">
        <CircleAlert className="size-4 shrink-0" />{qrError}
      </p> : null}

      <details className="group rounded-lg border border-border bg-background/60">
        <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 px-4 py-2.5 text-[13px] font-medium [&::-webkit-details-marker]:hidden">
          <span className="flex items-center gap-2"><KeyRound className="size-4 text-muted-foreground" />无法扫码？手动填写 Refresh Token（高级）</span>
          <ChevronDown className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-180" />
        </summary>
        <div className="space-y-3 border-t border-border/70 p-4">
          <p className="text-xs leading-5 text-muted-foreground">
            仅在扫码不可用时使用。输入完整 Refresh Token 后，系统会先验证令牌；验证失败时保留原凭证。
            令牌仅保存在服务端的密钥文件，不会回显。
          </p>
          <Input aria-label="阿里云盘 Refresh Token" type="password"
            autoComplete="off" spellCheck={false} placeholder="粘贴 Refresh Token"
            value={token} onChange={event => setToken(event.target.value)}
            className="bg-card font-mono text-xs md:text-xs" />
          <div className="flex justify-end">
            <Button size="sm" disabled={busy || Boolean(qr) || !token.trim()} onClick={() => void save()}>
              {saving ? <Loader2 className="animate-spin" /> : <KeyRound />}验证并保存
            </Button>
          </div>
        </div>
      </details>
      <p className="text-xs leading-5 text-muted-foreground">
        阿里云盘只需登录一次。Reelay 会自动续期访问令牌，并恢复最近检测结果；首次没有记录时会自动核验。
        “同步状态显示”只读取记录，“重新检测”才请求阿里云盘。切换默认存储为 Aliyun 后，保存目录必须选择资源库里的文件夹；备份盘不可用于资源库转存。
      </p>
    </Panel>
  );
}
