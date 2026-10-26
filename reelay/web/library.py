"""媒体库与统计。"""
from __future__ import annotations

import asyncio
from fastapi import APIRouter, HTTPException, Query


def router(service) -> APIRouter:
    api = APIRouter()
    scan_lock = asyncio.Lock()

    @api.get("/api/library")
    async def library(q: str | None = None, media_type: str | None = None,
                      save_dir_alias: str | None = None):
        """已入库作品（不限条数，按作品汇总后返回），与 MCP 的 search_library 同一套汇总。"""
        return service.library(query=q, media_type=media_type, save_dir_alias=save_dir_alias)

    @api.get("/api/library/scan")
    async def scanned_library(q: str | None = None, media_type: str | None = None,
                              save_dir_alias: str | None = None):
        """Previously inventoried cloud files; never presented as completed transfers."""
        return service.scanned_library(query=q, media_type=media_type, save_dir_alias=save_dir_alias)

    @api.post("/api/library/scan")
    async def run_library_scan():
        """Explicit, bounded read-only scan; no copy, move, deletion or dedup changes."""
        if scan_lock.locked():
            raise HTTPException(status_code=409, detail="已有媒体库扫描正在执行")
        async with scan_lock:
            return await asyncio.to_thread(service.scan_existing_library)

    @api.get("/api/stats")
    async def stats(days: int = Query(default=30, ge=1, le=365)):
        return service.stats(days)

    return api
