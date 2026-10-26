"""追剧订阅与「不再接收」的作品（用户数据，存在数据库里）。"""
from __future__ import annotations

from fastapi import APIRouter
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
