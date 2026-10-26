"""追剧订阅与「不再接收」的作品（用户数据，存在数据库里）。"""
from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field


class SubscriptionPayload(BaseModel):
    id: int | None = None  # 已有订阅的 ID；取消、继续订阅时可以只给它
    title: str = ""
    season: int | None = None
    tmdbid: int | None = None
    total: int | None = None
    auto: bool = True
    start_episode: int | None = Field(default=None, ge=1, le=100000)


class SubscriptionDestinationPayload(BaseModel):
    id: int
    save_to: str | None = None


class SubscriptionStartPayload(BaseModel):
    id: int = Field(gt=0)
    start_episode: int | None = Field(default=None, ge=1, le=100000)


class ExistingFolderScanPayload(BaseModel):
    id: int = Field(gt=0)
    provider: Literal["p115", "aliyun"]
    cid: str = Field(min_length=1, max_length=128)


class ExistingDestinationPayload(BaseModel):
    id: int = Field(gt=0)
    naming_mode: Literal["simple", "default"] = "simple"


class ExistingDestinationClearPayload(BaseModel):
    id: int = Field(gt=0)


class ExistingAssociatePayload(BaseModel):
    id: int = Field(gt=0)
    file_keys: list[str] = Field(min_length=1, max_length=50)


class TextPayload(BaseModel):
    text: str


class SubscriptionTestPayload(BaseModel):
    id: int = Field(gt=0)
    text: str | None = Field(default=None, max_length=12000)
    source: str | None = Field(default=None, max_length=150)


class IgnoredWorkPayload(BaseModel):
    id: int | None = None
    title: str = ""
    tmdbid: int | None = None
    kind: str | None = None


def router(service) -> APIRouter:
    api = APIRouter()

    def item(payload: BaseModel) -> dict:
        data = payload.model_dump()
        return {"id": data["id"]} if data.get("id") is not None else data

    @api.get("/api/subscriptions")
    async def subscriptions():
        return service.list_subscriptions()

    @api.post("/api/subscriptions")
    async def add_subscription(payload: SubscriptionPayload):
        return await service.add_subscription(payload.model_dump(exclude={"id"}))

    @api.post("/api/subscriptions/simulate")
    async def simulate_subscription(payload: SubscriptionTestPayload):
        """Never enqueues a job or touches a destination cloud."""
        return await service.simulate_subscription(
            payload.id, text=payload.text, source=payload.source)

    @api.post("/api/subscriptions/resolve")
    async def resolve(payload: TextPayload):
        return await service.resolve_subscription(payload.text)

    @api.post("/api/subscriptions/from-task/{task_id}")
    async def subscribe_from_task(task_id: str):
        return await service.subscribe_from_task(task_id)

    @api.post("/api/subscriptions/destination")
    async def set_subscription_destination(payload: SubscriptionDestinationPayload):
        return service.set_subscription_destination(payload.model_dump())

    @api.post("/api/subscriptions/start")
    async def set_start(payload: SubscriptionStartPayload):
        """Per-subscription automatic episode floor; does not alter prior jobs."""
        return service.set_subscription_start(payload.model_dump())

    @api.get("/api/subscriptions/existing")
    async def preview_existing(id: int = Query(gt=0), offset: int = Query(default=0, ge=0)):
        """Safe read-only preview; actual cloud files are checked upon confirmation."""
        return service.existing_subscription_candidates(id, offset=offset)

    scan_lock = asyncio.Lock()

    @api.post("/api/subscriptions/existing/scan-folder")
    async def scan_existing_folder(payload: ExistingFolderScanPayload):
        """Read only the folder chosen in the UI, no save-dir configuration edits."""
        if scan_lock.locked():
            from fastapi import HTTPException
            raise HTTPException(status_code=409, detail="已有文件夹扫描正在进行")
        async with scan_lock:
            return await asyncio.to_thread(
                service.scan_existing_subscription_folder,
                payload.id, payload.provider, payload.cid)

    @api.post("/api/subscriptions/existing/use-folder")
    async def use_existing_folder(payload: ExistingDestinationPayload):
        """Explicit opt-in; only affects future subscriptions."""
        return await asyncio.to_thread(
            service.set_existing_subscription_destination,
            payload.id, naming_mode=payload.naming_mode)

    @api.post("/api/subscriptions/existing/clear-folder")
    async def clear_existing_folder(payload: ExistingDestinationClearPayload):
        return await asyncio.to_thread(service.clear_existing_subscription_destination, payload.id)

    @api.post("/api/subscriptions/existing/associate")
    async def associate_existing(payload: ExistingAssociatePayload):
        """Explicitly bind 1-50 observed file identities to a TMDB subscription."""
        return await service.associate_existing_subscription(payload.id, payload.file_keys)

    @api.post("/api/subscriptions/reopen")
    async def reopen(payload: SubscriptionPayload):
        service.reopen_subscription(item(payload))
        return {"ok": True}

    @api.post("/api/subscriptions/remove")
    async def remove(payload: SubscriptionPayload):
        await service.remove_subscription(item(payload))
        return {"ok": True}

    @api.post("/api/subscriptions/suggestions/dismiss")
    async def dismiss_suggestion(payload: SubscriptionPayload):
        service.dismiss_suggestion(payload.model_dump(exclude={"id"}))
        return {"ok": True}

    @api.get("/api/ignored-works")
    async def ignored_works():
        return service.list_ignored_works()

    @api.post("/api/ignored-works/remove")
    async def unignore(payload: IgnoredWorkPayload):
        await service.unignore_work(item(payload))
        return {"ok": True}

    return api
