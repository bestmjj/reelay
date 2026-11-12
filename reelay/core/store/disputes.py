"""Manual quarantine of a wrongly attributed completed transfer.

This never mutates cloud files or replays a copy. The historical task and
receipt are retained, while its *own* content dedup claims are withdrawn and
the work/episode is blocked from automatic retransfer until explicitly
reviewed. The record is durable and scoped to this task.
"""
from __future__ import annotations

import json
import time

from .. import works
from ..contracts import TaskState
from .base import DEDUP_EPISODE, DEDUP_SHARE, DEDUP_VARIETY, _locked


PLUGIN = "disputed_transfers"


class DisputedTransferRepository:
    @_locked
    def disputed_transfer(self, task_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT value FROM plugin_state WHERE plugin=? AND key=?",
            (PLUGIN, task_id)).fetchone()
        return json.loads(row["value"]) if row else None

    @_locked
    def disputed_task_ids(self) -> set[str]:
        return {row["key"] for row in self._conn.execute(
            "SELECT key, value FROM plugin_state WHERE plugin=?", (PLUGIN,))
            if json.loads(row["value"]).get("active")}

    @_locked
    def blocked_episode_pairs(self, work: dict, provider: str) -> list[list[int]]:
        blocked = set()
        for row in self._conn.execute(
                "SELECT value FROM plugin_state WHERE plugin=?", (PLUGIN,)):
            data = json.loads(row["value"])
            if (data.get("active") and data.get("provider") == provider and
                    works.same(data["work"], work)):
                blocked.update((int(s), int(ep)) for s, ep in data["pairs"])
        return [list(pair) for pair in sorted(blocked)]

    def quarantine_transfer(self, ctx, *, reason: str) -> dict:
        """Quarantine a confirmed single-episode task, retaining audit history.

        A source with no episode evidence (e.g. '4K.mp4') is suspicious, but
        the user can also mark a misidentified file after visual inspection.
        """
        if ctx.state != TaskState.DONE or not (ctx.result or {}).get("storage_verified"):
            raise ValueError("只能隔离已完成且曾通过落地校验的转存任务")
        storage = ctx.meta.get("storage") or {}
        manifest = storage.get("manifest") or {}
        files = manifest.get("files") or []
        pairs = ctx.episode_pairs()
        if len(files) != 1 or len(pairs) != 1:
            raise ValueError("为避免影响其他集数，目前只支持隔离单文件、单集的已入库任务")
        reason = reason.strip()
        if not 5 <= len(reason) <= 400:
            raise ValueError("请提供明确的错片原因（5-400 字）")
        provider = (storage.get("identity") or {}).get("provider") or ctx.source_rule.get("storage")
        if provider not in ("aliyun", "p115"):
            raise ValueError("只支持 115 与阿里云盘转存记录")
        work = (storage.get("identity") or {}).get("work") or works.of(ctx.parsed, ctx.meta.get("tmdb"))
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT value FROM plugin_state WHERE plugin=? AND key=?",
                (PLUGIN, ctx.task_id)).fetchone()
            if existing:
                data = json.loads(existing["value"])
                if data.get("active"):
                    return data
                raise ValueError("这条错片记录已解除隔离，不能重复覆盖审计历史")
            ledger = [dict(row) for row in self._conn.execute(
                "SELECT kind, key, created_at FROM dedup WHERE task_id=? AND kind IN (?,?,?)",
                (ctx.task_id, DEDUP_SHARE, DEDUP_EPISODE, DEDUP_VARIETY))]
            source = files[0]
            data = {"active": True, "task_id": ctx.task_id,
                    "reason": reason, "provider": provider,
                    "work": work, "pairs": [list(pair) for pair in pairs],
                    "source_name": source.get("name"),
                    "source_path": source.get("share_path"),
                    "source_id": str(source.get("id") or ""),
                    "source_size": source.get("size"),
                    "ledger_before": ledger, "created_at": time.time(),
                    "resolved_at": None}
            self._conn.execute(
                "INSERT INTO plugin_state(plugin,key,value,updated_at) VALUES (?,?,?,?)",
                (PLUGIN, ctx.task_id, json.dumps(data, ensure_ascii=False), time.time()))
            # Only this original task's claims are removed; do not erase a
            # verified claim owned by a different task or any message ledger.
            self._conn.execute(
                "DELETE FROM dedup WHERE task_id=? AND kind IN (?,?,?)",
                (ctx.task_id, DEDUP_SHARE, DEDUP_EPISODE, DEDUP_VARIETY))
        return data

    def resolve_disputed_transfer(self, task_id: str, *, confirmation: str) -> dict:
        """Explicitly acknowledge independent cloud inspection/correction.

        No cloud file move/delete takes place, and the erroneous old content
        dedup record is never restored. Future tasks must select a real source.
        """
        if confirmation != "我已核对并处理错片":
            raise ValueError("请先核对实际网盘文件，再输入完整确认语")
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT value FROM plugin_state WHERE plugin=? AND key=?",
                (PLUGIN, task_id)).fetchone()
            if not row:
                raise ValueError("找不到这条错片隔离记录")
            data = json.loads(row["value"])
            if data.get("active"):
                data["active"] = False
                data["resolved_at"] = time.time()
                self._conn.execute(
                    "UPDATE plugin_state SET value=?, updated_at=? WHERE plugin=? AND key=?",
                    (json.dumps(data, ensure_ascii=False), time.time(), PLUGIN, task_id))
            return data
