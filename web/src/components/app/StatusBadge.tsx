import { ACTIVE_STATES, STATE_META, TONE_CLASS, type Tone } from '@/lib/status';
import type { TaskState } from '@/lib/types';
import { cn } from '@/lib/utils';

export function ToneBadge({ tone, children, className, pulse = false }: {
  tone: Tone; children: React.ReactNode; className?: string; pulse?: boolean;
}) {
  const style = TONE_CLASS[tone];
  return (
    <span className={cn('inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium', style.badge, className)}>
      <span className={cn('size-1.5 shrink-0 rounded-full', style.dot, pulse && 'animate-pulse')} />
      {children}
    </span>
  );
}

/** 任务状态徽标；进行中的状态带呼吸点。 */
export function StatusBadge({ state, skipKind, className }: {
  state: TaskState; skipKind?: string | null; className?: string;
}) {
  const meta = STATE_META[state];
  const label = state === 'skipped'
    ? skipKind === 'duplicate' ? '重复跳过'
      : skipKind === 'rule' ? '规则跳过' : meta.label
    : meta.label;
  return <ToneBadge tone={meta.tone} pulse={ACTIVE_STATES.includes(state)} className={className}>{label}</ToneBadge>;
}
