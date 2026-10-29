"""任务：列表、详情、收件箱与人工操作（批准、忽略、重试、重新识别、重新整理、核验转存、修正识别）。"""
from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field


class ActionPayload(BaseModel):
    save_dir_alias: str | None = None
    existing_folder_id: str | None = Field(default=None, min_length=1, max_length=128)
    existing_drive_id: str | None = Field(default=None, min_length=1, max_length=128)
    destination_mode: Literal["existing", "exact", "organized"] = "existing"
    conflict: Literal["skip", "replace"] | None = None  # 重新整理时处理文件冲突


class QuarantineWrongCopyPayload(BaseModel):
    reason: str = Field(min_length=5, max_length=400)


class ResolveWrongCopyPayload(BaseModel):
    confirmation: str


class ShareFolderPayload(BaseModel):
    folder_id: str = Field(min_length=1, max_length=128)
    remember: bool = True


class ShareFilePayload(BaseModel):
    file_id: str = Field(min_length=1, max_length=128)


class EpisodeSelectionPayload(BaseModel):
    # [] restores ordinary automated dedup. Explicitly selected episodes
    # still pass the same verified-cloud and quarantine checks.
    episodes: list[int] = Field(default_factory=list, max_length=100)


class ForceMissingRangePayload(BaseModel):
    episodes: list[int] = Field(min_length=1, max_length=100)


class RepairMissingPayload(BaseModel):
    # Immutable file IDs returned by the read-only repair-check endpoint.
    # The server rechecks every ID before queuing a new transfer.
    missing_ids: list[str] = Field(min_length=1, max_length=100)


class ParsePatchPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class BatchPayload(BaseModel):
    task_ids: list[str] = Field(default_factory=list)
    action: Literal["approve", "reject", "retry"]
    save_dir_alias: str | None = None  # 仅 approve：统一改投到该目录


class TaskIdsPayload(BaseModel):
    task_ids: list[str] = Field(default_factory=list)


def router(service) -> APIRouter:
    api = APIRouter()

    @api.get("/api/tasks")
    async def tasks(state: str | None = None, source: str | None = None, query: str | None = None,
                    limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0)):
        return {"items": service.list_tasks(state=state, source=source, query=query, limit=limit, offset=offset)}

    @api.post("/api/tasks/batch")
    async def batch(payload: BatchPayload):
        results = await service.bulk(payload.action, payload.task_ids, save_dir_alias=payload.save_dir_alias)
        return {"done": sum(1 for item in results if item["ok"]),
                "failed": [{"task_id": item["task_id"], "error": item["error"]} for item in results if not item["ok"]]}

    @api.get("/api/tasks/{task_id}")
    async def task_detail(task_id: str):
        return service.get_task(task_id)

    @api.get("/api/tasks/{task_id}/source-audit")
    async def transfer_source_audit(task_id: str):
        return service.transfer_source_audit(task_id)

    @api.post("/api/tasks/{task_id}/quarantine-wrong-copy")
    async def quarantine_wrong_copy(task_id: str, payload: QuarantineWrongCopyPayload):
        return service.quarantine_wrong_source(task_id, payload.reason)

    @api.post("/api/tasks/{task_id}/resolve-wrong-copy")
    async def resolve_wrong_copy(task_id: str, payload: ResolveWrongCopyPayload):
        return service.resolve_quarantine(task_id, payload.confirmation)

    @api.get("/api/tasks/{task_id}/share-folders")
    async def task_share_folders(task_id: str):
        return await asyncio.to_thread(service.share_folders, task_id)

    @api.post("/api/tasks/{task_id}/share-folder")
    async def task_select_share_folder(task_id: str, payload: ShareFolderPayload):
        return await asyncio.to_thread(
            service.choose_share_folder, task_id, payload.folder_id, remember=payload.remember)

    @api.post("/api/tasks/{task_id}/share-file")
    async def task_select_share_file(task_id: str, payload: ShareFilePayload):
        return await asyncio.to_thread(service.choose_share_file, task_id, payload.file_id)

    @api.post("/api/tasks/{task_id}/episode-selection")
    async def choose_episodes(task_id: str, payload: EpisodeSelectionPayload):
        return service.set_episode_selection(task_id, payload.episodes)

    @api.get("/api/tasks/{task_id}/repair-check")
    async def repair_check(task_id: str):
        return await asyncio.to_thread(service.repair_check, task_id)

    @api.post("/api/tasks/{task_id}/repair-missing")
    async def repair_missing(task_id: str, payload: RepairMissingPayload):
        return await asyncio.to_thread(service.repair_missing, task_id, payload.missing_ids)

    @api.post("/api/tasks/{task_id}/reopen-duplicate")
    async def reopen_duplicate(task_id: str):
        return service.reopen_duplicate(task_id)

    @api.post("/api/tasks/{task_id}/force-reopen")
    async def force_reopen(task_id: str):
        return service.force_reopen_duplicate(task_id)

    @api.post("/api/tasks/{task_id}/force-missing-range")
    async def force_missing_range(task_id: str, payload: ForceMissingRangePayload):
        return service.force_missing_range(task_id, payload.episodes)

    @api.post("/api/tasks/{task_id}/approve")
    async def approve(task_id: str, payload: ActionPayload | None = None):
        await service.approve(
            task_id,
            save_dir_alias=payload.save_dir_alias if payload else None,
            existing_folder_id=payload.existing_folder_id if payload else None,
            existing_drive_id=payload.existing_drive_id if payload else None,
            destination_mode=payload.destination_mode if payload else "existing",
        )
        return {"ok": True}

    @api.post("/api/tasks/{task_id}/reject")
    async def reject(task_id: str):
        await service.reject(task_id)
        return {"ok": True}

    @api.post("/api/tasks/{task_id}/retry")
    async def retry(task_id: str):
        await service.retry(task_id)
        return {"ok": True}

    @api.post("/api/tasks/{task_id}/reparse")
    async def reparse(task_id: str):
        await service.reparse(task_id)
        return {"ok": True}

    @api.post("/api/tasks/{task_id}/reorganize")
    async def reorganize(task_id: str, payload: ActionPayload):
        await service.requeue_organize(task_id, save_dir_alias=payload.save_dir_alias, conflict=payload.conflict)
        return {"ok": True}

    @api.post("/api/tasks/{task_id}/accept-partial")
    async def accept_partial(task_id: str):
        await service.accept_partial_transfer(task_id)
        return {"ok": True}

    @api.post("/api/tasks/{task_id}/abandon")
    async def abandon(task_id: str):
        await service.abandon_transfer(task_id)
        return {"ok": True}

    @api.post("/api/tasks/{task_id}/reconcile")
    async def reconcile(task_id: str):
        await service.reconcile_transfer(task_id)
        return {"ok": True}

    @api.patch("/api/tasks/{task_id}/parsed")
    async def patch_parsed(task_id: str, payload: ParsePatchPayload):
        return await service.correct_parsed(task_id, payload.values)

    @api.get("/api/inbox")
    async def inbox(state: str = "pending_approval", limit: int = Query(default=200, ge=1, le=500)):
        return service.inbox(state, limit)

    @api.post("/api/inbox/advice")
    async def inbox_advice(payload: TaskIdsPayload):
        """收件箱建议：列表先显示规则写的理由，前端再请求 AI 措辞（按事实指纹缓存）。"""
        return {"items": await service.phrase_advice(payload.task_ids)}

    return api
