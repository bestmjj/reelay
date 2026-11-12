"""Explicit one-off cloud share submissions, using the normal durable pipeline."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field


class ManualResource(BaseModel):
    text: str = Field(min_length=5, max_length=12000)
    source: str | None = Field(default=None, max_length=120)


def router(service) -> APIRouter:
    api = APIRouter()

    @api.post("/api/resources/submit")
    async def submit(payload: ManualResource):
        """Queue one user-requested resource; no direct cloud writes."""
        return await service.submit_manual_resource(payload.text, source=payload.source)

    return api
