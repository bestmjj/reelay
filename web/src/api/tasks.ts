import type { IParsedResource, ISourceAudit, ITaskDetail, ITaskSummary } from '@/lib/types';
import { get, patch, post, query } from './request';

/** 任务操作的接口（POST /api/tasks/<id>/<endpoint>）。accept-partial：转存只到一部分时按已到的文件继续；
 *  abandon：放弃这次转存，删除暂存目录后忽略。 */
export type TaskEndpoint = 'approve' | 'reject' | 'retry' | 'reparse' | 'reorganize' | 'reconcile'
  | 'accept-partial' | 'abandon';

export interface IShareFolderChoice {
  folder_id: string;
  path: string;
  count: number;
  episodes: number[][];
  unrecognized: number;
  direct_count?: number;
  selectable?: boolean;
  samples?: Array<{ name: string; path: string; size: number | null }>;
}

export interface IShareFileChoice {
  file_id: string;
  name: string;
  path: string;
  size: number | null;
  episode: number | null;
  selectable: boolean;
}

export interface IShareFolderPreview {
  folders: IShareFolderChoice[];
  /** Exact source file options are shown only after a unique episode is known. */
  files: IShareFileChoice[];
  selected_file_id: string | null;
  selected_folder_id: string | null;
  saved_preference: boolean;
  provider: string;
}

export interface ITaskFilters {
  /** 可传逗号分隔的多个状态。 */
  state?: string;
  source?: string;
  query?: string;
  limit?: number;
  offset?: number;
}

export interface IRepairFile {
  old_id: string;
  season: number | null;
  episode: number | null;
  source_name: string;
  target_dir: string;
  target_name: string;
  note?: string;
  reason?: string;
}

export interface IRepairReport {
  source_task_id: string;
  base_path: string;
  total: number;
  missing: IRepairFile[];
  present: IRepairFile[];
  uncertain: IRepairFile[];
}

export interface IBatchResult {
  done: number;
  failed: Array<{ task_id: string; error: string }>;
}

export const taskApi = {
  list: (filters: ITaskFilters = {}) => get<{ items: ITaskSummary[] }>(`tasks${query({ ...filters })}`),
  get: (taskId: string) => get<ITaskDetail>(`tasks/${taskId}`),
  sourceAudit: (taskId: string) =>
    get<ISourceAudit>(`tasks/${taskId}/source-audit`),
  quarantineWrongCopy: (taskId: string, reason: string) =>
    post(`tasks/${taskId}/quarantine-wrong-copy`, { reason }),
  resolveWrongCopy: (taskId: string) =>
    post(`tasks/${taskId}/resolve-wrong-copy`, { confirmation: '我已核对并处理错片' }),
  shareFolders: (taskId: string) =>
    get<IShareFolderPreview>(`tasks/${taskId}/share-folders`),
  chooseShareFolder: (taskId: string, folderId: string, remember = true) =>
    post<IShareFolderPreview>(`tasks/${taskId}/share-folder`, { folder_id: folderId, remember }),
  chooseShareFile: (taskId: string, fileId: string) =>
    post<IShareFolderPreview>(`tasks/${taskId}/share-file`, { file_id: fileId }),
  /** Change only this task's target episodes. Empty list restores auto-selection. */
  selectEpisodes: (taskId: string, episodes: number[]) =>
    post<ITaskDetail>(`tasks/${taskId}/episode-selection`, { episodes }),
  /** An auto-skipped duplicate can be manually inspected for newly missing episodes. */
  reopenDuplicate: (taskId: string) =>
    post<ITaskDetail>(`tasks/${taskId}/reopen-duplicate`),
  /** Explicitly bypass only the old episode ledger for one manually selected Aliyun file. */
  forceReopenDuplicate: (taskId: string) =>
    post<ITaskDetail>(`tasks/${taskId}/force-reopen`),
  /** Read-only inspection; old DONE record remains unchanged. */
  repairCheck: (taskId: string) =>
    get<IRepairReport>(`tasks/${taskId}/repair-check`),
  /** Fresh server-side recheck before queueing only confirmed missing files. */
  repairMissing: (taskId: string, missingIds: string[]) =>
    post<{ ok: boolean; task_id: string; file_count: number }>(
      `tasks/${taskId}/repair-missing`, { missing_ids: missingIds }),
  act: (taskId: string, endpoint: TaskEndpoint, body?: unknown) =>
    post<{ ok: boolean }>(`tasks/${taskId}/${endpoint}`, body),
  /** 修正识别结果，返回更新后的详情。 */
  correct: (taskId: string, values: Partial<IParsedResource>) =>
    patch<ITaskDetail>(`tasks/${taskId}/parsed`, { values }),
  /** saveDirAlias 只对 approve 生效：统一改存到该目录。 */
  batch: (taskIds: string[], action: 'approve' | 'reject' | 'retry', saveDirAlias?: string) =>
    post<IBatchResult>('tasks/batch', { task_ids: taskIds, action, save_dir_alias: saveDirAlias || null }),
};
