/** 整理冲突的两种人工处理：删除本次转存的文件 / 覆盖保存。 */
import { confirmDialog } from './confirm';
import type { IOrganizeConflict, ITransferPartial } from './types';

export type ConflictChoice = 'suffix';

export const CONFLICT_DONE: Record<ConflictChoice, string> = {
  suffix: '已排队：保留旧文件，给新文件加序号',
};

/** Default conflict action is non-destructive. */
export function confirmConflict(_choice: ConflictChoice, conflict: IOrganizeConflict): Promise<boolean> {
  return confirmDialog({
    title: '保留库里与新转存的两份文件？',
    description: '原文件保持不变，新文件用数字后缀避免重名，不删除或覆盖任何一份。',
    details: [{ label: '原文件', value: conflict.existing.name ?? '（未知）' },
      { label: '新文件', value: conflict.incoming.name ?? '（未知）' }],
    confirmText: '保留两份',
    destructive: false,
  });
}

/** 放弃转存：暂存目录连同已到的文件移进 115 回收站，任务改为忽略。 */
export function confirmAbandon(title: string | null | undefined, partial?: ITransferPartial | null): Promise<boolean> {
  return confirmDialog({
    title: `放弃《${title ?? '这条资源'}》的这次转存？`,
    description: '暂存目录连同已到的文件移进 115 回收站，任务改为忽略。',
    details: partial ? [{ label: '已到', value: `${partial.landed} / ${partial.expected} 个文件` },
      { label: '没到', value: partial.missing.join('\n') }] : undefined,
    confirmText: '放弃并删除',
    destructive: true,
  });
}
