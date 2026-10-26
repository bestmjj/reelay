"""媒体库：已入库作品的汇总（管理台媒体库页、MCP 与对话助手共用同一套汇总）、缺集情况与统计。"""
from __future__ import annotations

import time
from typing import Any

from .. import library, library_scan_progress, library_view, release
from .base import ServiceBase

# 管理台媒体库页每部作品需要的字段
_WEB_FIELDS = ("title", "media_type", "year", "tmdbid", "episodes", "episode_pairs", "tasks", "bytes", "poster_path",
               "save_dir_alias", "files", "updated_at", "resolution", "release", "origin_country", "genre_ids")


class CatalogMixin(ServiceBase):
    """媒体库查询与统计。"""

    def _groups(self, query: str | None = None, media_type: str | None = None,
                save_dir_alias: str | None = None) -> list[dict[str, Any]]:
        """按目录筛选应在作品汇总前进行，避免同名作品跨目录被合并后丢失归属。"""
        snapshots = self.store.done_snapshots(query or None, media_type or None)
        if save_dir_alias:
            snapshots = [item for item in snapshots if item.get("save_dir_alias") == save_dir_alias]
        return library.group_library(snapshots, self.store.replaced_file_ids())

    def library(self, *, query: str | None = None, media_type: str | None = None,
                save_dir_alias: str | None = None) -> dict[str, Any]:
        """管理台媒体库：每部作品的季集、体积、海报与整理后的文件。"""
        groups = [{key: group[key] for key in _WEB_FIELDS} for group in self._groups(query, media_type, save_dir_alias)]
        return {"groups": groups, "total_done": sum(group["tasks"] for group in groups),
                "estimated_gb": round(sum(group["bytes"] for group in groups) / 1024 ** 3, 2)}

    def scanned_library(self, *, query: str | None = None, media_type: str | None = None,
                        save_dir_alias: str | None = None) -> dict[str, Any]:
        """Separate read-only cloud inventory; never counted as verified Reelay transfers."""
        snapshot = self.store.state_get("library_scan", "snapshot") or {
            "groups": [], "scanned_at": None, "file_count": 0, "folder_count": 0,
            "scanned_roots": 0, "errors": [], "complete": False, "truncated": False,
        }
        items = snapshot["groups"]
        if query:
            items = [x for x in items if query.casefold() in x["title"].casefold()]
        if media_type:
            items = [x for x in items if x["media_type"] == media_type]
        if save_dir_alias:
            items = [x for x in items if x.get("save_dir_alias") == save_dir_alias]
        return {**snapshot, "groups": items,
                "estimated_gb": round(sum(x["bytes"] for x in items) / 1024 ** 3, 2),
                "total_done": 0}

    def library_overview(self, *, query: str | None = None, media_type: str | None = None,
                         save_dir_alias: str | None = None) -> dict[str, Any]:
        """Single UI library: existing cloud media plus verified Reelay entries.

        Keep both underlying ledgers independent. Only merge matching entries
        when folder/title and independent file/year/episode evidence agree.
        Scanning must never turn a file into a verified transfer or dedup claim.
        """
        verified = self.library(query=query, media_type=media_type, save_dir_alias=save_dir_alias)
        observed = self.scanned_library(query=query, media_type=media_type, save_dir_alias=save_dir_alias)
        groups = library_view.join_library(verified["groups"], observed["groups"])
        return {
            "groups": groups,
            "total_done": verified["total_done"],
            "estimated_gb": round(sum(group.get("bytes", 0) for group in groups) / 1024 ** 3, 2),
            "scanned_at": observed.get("scanned_at"),
            "file_count": observed.get("file_count", 0),
            "folder_count": observed.get("folder_count", 0),
            "scanned_roots": observed.get("scanned_roots", 0),
            "complete": observed.get("complete", False),
            "truncated": observed.get("truncated", False),
            "errors": observed.get("errors", []),
        }

    def scan_existing_library(self) -> dict[str, Any]:
        """Manually triggered, bounded read-only inventory of configured cloud folders."""
        self.pipeline.reload_config()
        # Resume the exact pending folder queue instead of dropping the first
        # 1,647 files when a 45s scan budget is exhausted.
        cursor = self.store.state_get("library_scan", "cursor") or None
        snapshot, progress = library_scan_progress.step(
            self.pipeline.cfg,
            lambda provider: self.pipeline.reg.get_provider("storage", provider),
            cursor=cursor,
        )
        if not snapshot["scanned_roots"] and not snapshot["has_more"]:
            details = "；".join(snapshot["errors"][:3]) or "没有可扫描的目录"
            raise ValueError(f"未能读取任何影视目录；保留上次扫描结果。{details}")
        # Durable, read-only physical file IDs are independent of successful
        # transfers, and are never written to the verified dedup ledger.
        self.store.state_set("library_scan", "cursor", progress if snapshot["has_more"] else None)
        self.store.state_set("library_scan", "files", progress["records"])
        # This is an observational index. Never backfill verified transfer
        # ledger from folder names or guessed TMDB matches.
        self.store.state_set("library_scan", "snapshot", snapshot)
        return snapshot

    def library_query(self, *, query: str | None = None, media_type: str | None = None,
                      min_resolution: str | None = None, origin_country: str | None = None,
                      genre_id: int | None = None, limit: int = 30) -> dict[str, Any]:
        """媒体库问答（MCP、对话助手）：按类型（动漫按 TMDB 动画题材判断）、最低清晰度、地区与题材筛选。"""
        found = library.filter_library(self._groups(query), media_type=media_type, min_resolution=min_resolution,
                                       origin_country=origin_country, genre_id=genre_id)
        return {"total": len(found), "items": found[:max(1, min(limit, 100))]}

    def missing_episodes(self, title: str | None = None) -> dict[str, Any]:
        report = self.pipeline.query("episode_report") or {}
        return {"checked_on": report.get("date"),
                "items": library.missing(report, self.store.stored_series(), title or None)}

    def stats(self, days: int = 30) -> dict[str, Any]:
        """统计页：各状态任务数与已入库的估算体积（全部），最近 days 天按天的任务数与任务最多的片名。"""
        data = self.store.task_stats(time.time() - days * 86400)
        sizes = [release.size_bytes(value) or 0 for value in data.pop("done_sizes")]
        return {"counts": self.store.counts_by_state(), **data,
                "estimated_done_gb": round(sum(sizes) / 1024 ** 3, 2)}
