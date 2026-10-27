"""任务操作：批准、忽略、重试、重新识别、重新整理、核验转存、修正识别结果。

人工裁决（批准、忽略、修正）在这里记成带标签的样本（``feedback`` 表），供解析示例、偏好决策、通过率统计与本地评测使用。
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any


from .. import messages, source_audit, subscriptions
from ..contracts import ParsedResource, PipelineContext, TaskState
from ..validation import MEDIA_SCHEMA, validate_resource
from .base import ConflictError, NotFoundError, ServiceBase

log = logging.getLogger("reelay.service")

# 排队中、执行中或已完成的任务：不能重新识别或修改
_BUSY = (TaskState.RECEIVED, TaskState.PARSED, TaskState.QUEUED, TaskState.SAVING, TaskState.STORED,
         TaskState.ORGANIZING, TaskState.DONE)


class TaskOpsMixin(ServiceBase):
    """任务的人工操作；只改任务状态，115 操作由 Worker 执行。"""

    async def approve(self, task_id: str, *, save_dir_alias: str | None = None) -> None:
        """save_dir_alias 是手动指定的目录：与自动分类不一致时直接存入，不套用分类子目录。"""
        ctx = self._require_context(task_id)
        if ctx.state != TaskState.PENDING_APPROVAL:
            raise ConflictError("任务不是待审批状态")
        advice = self.advice_action(ctx)
        target = self.pipeline.target_preview(ctx)
        if save_dir_alias:
            self._validate_alias(save_dir_alias)
            ctx.save_dir_alias = save_dir_alias
            ctx.ns("library")["pinned"] = save_dir_alias
        elif ctx.ns("library").pop("pinned", None):
            ctx.save_dir_alias = None  # 撤销此前手动指定的目录，重新按分类
        ctx.state = TaskState.QUEUED
        ctx.ns("task")["user_approved"] = True
        ctx.ns("task").pop("skip", None)
        ctx.log("管理端批准并加入队列；只有网盘复制完成并核验后才算已入库，去重可能跳过")
        self._update(ctx, expected=TaskState.PENDING_APPROVAL)
        self._record(ctx, "approve", {"save_dir_alias": save_dir_alias or None, "target": target}, advice)
        try:
            self._suggest_subscription(ctx)
        except Exception as exc:  # noqa: BLE001 - 订阅建议不影响批准
            log.warning("订阅建议生成失败：%s", exc)

    def task_actions(self, ctx: PipelineContext) -> list[str]:
        """这条任务现在允许的人工操作（管理台按它显示按钮）；判断与各操作自己的检查一致。"""
        state, storage = ctx.state, ctx.meta.get("storage") or {}
        unknown_transfer = storage.get("phase") == "receiving" and bool(storage.get("manifest"))
        out: list[str] = []
        if state == TaskState.PENDING_APPROVAL:
            out += ["approve", "reject"]
            if subscriptions.subscribable(ctx) and not subscriptions.subscribed(self.store.subscriptions(), ctx):
                out.append("subscribe")
        if state == TaskState.FAILED:
            out.append("retry")
        if state == TaskState.NEED_MANUAL:
            if unknown_transfer:
                out += ["reconcile", *(["accept_partial"] if storage.get("partial") else []), "abandon"]
            elif storage.get("landed"):
                out.append("reorganize")
                if (ctx.meta.get("organize") or {}).get("pending_conflict"):
                    out.append("resolve_conflict")
        if state in (TaskState.FAILED, TaskState.NEED_MANUAL) and not unknown_transfer and not storage.get("landed"):
            out.append("dismiss")
        if (state in (TaskState.PENDING_APPROVAL, TaskState.FAILED, TaskState.NEED_MANUAL)
                and ctx.parsed and ctx.parsed.share_code
                and not storage.get("manifest") and not storage.get("landed")):
            out.append("choose_share_folder")
        if (state == TaskState.DONE and isinstance(storage.get("manifest"), dict)
                and len(storage["manifest"].get("files") or []) == 1
                and len(ctx.episode_pairs()) == 1):
            dispute = self.store.disputed_transfer(ctx.task_id)
            if dispute and dispute.get("active"):
                out.append("resolve_quarantine")
            elif not dispute and (ctx.result or {}).get("storage_verified"):
                out.append("quarantine_wrong_source")
        if state not in _BUSY and not storage.get("manifest") and not storage.get("landed"):
            out.append("reparse")
        if messages.correctable(ctx):
            out.append("correct")
        return out

    def transfer_source_audit(self, task_id: str) -> dict[str, Any]:
        ctx = self._require_context(task_id)
        return {**source_audit.source_audit(ctx),
                "quarantine": self.store.disputed_transfer(task_id)}

    def quarantine_wrong_source(self, task_id: str, reason: str) -> dict[str, Any]:
        ctx = self._require_context(task_id)
        if "quarantine_wrong_source" not in self.task_actions(ctx):
            raise ConflictError("只有已核验完成的单集任务可以标记错片")
        try:
            return self.store.quarantine_transfer(ctx, reason=reason)
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc

    def resolve_quarantine(self, task_id: str, confirmation: str) -> dict[str, Any]:
        ctx = self._require_context(task_id)
        if "resolve_quarantine" not in self.task_actions(ctx):
            raise ConflictError("这条任务没有待处理的错片隔离记录")
        try:
            return self.store.resolve_disputed_transfer(task_id, confirmation=confirmation)
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc

    def _selectable_share_task(self, task_id: str) -> tuple[PipelineContext, str]:
        ctx = self._require_context(task_id)
        if "choose_share_folder" not in self.task_actions(ctx):
            raise ConflictError("只有尚未发起转存的待审或失败任务可以选择分享版本")
        provider = ctx.source_rule.get("storage") or self.pipeline.cfg["defaults"].get("storage", "p115")
        if provider not in ("p115", "aliyun"):
            raise ValueError("该转存网盘不支持分享版本选择")
        expected = (ctx.parsed.raw_fields or {}).get("share_provider")
        if expected == "aliyun" and provider != "aliyun":
            raise ValueError("分享属于阿里云盘，但转存 Provider 配置不符")
        if expected == "p115" and provider != "p115":
            raise ValueError("分享属于 115 网盘，但转存 Provider 配置不符")
        return ctx, provider

    def share_folders(self, task_id: str) -> dict[str, Any]:
        """Read-only per-share folder preview; no side-effect or AI selection."""
        ctx, provider = self._selectable_share_task(task_id)
        storage = self.pipeline.reg.get_provider("storage", provider)
        if not storage.ready():
            raise ConflictError("网盘没有登录，无法读取分享文件列表")
        with self.pipeline.scope(ctx):
            folders = storage.share_folder_choices(ctx.parsed)
        if not folders:
            raise ValueError("分享中找不到可选择的视频文件夹")
        key = f"{provider}:{ctx.parsed.share_code}"
        preference = (self.store.state_get("share_folder_preferences", "items", {}) or {}).get(key)
        selected = ctx.ns("share_selection").get("folder_id") or (preference or {}).get("folder_id")
        return {"folders": folders, "selected_folder_id": selected,
                "saved_preference": bool(preference), "provider": provider}

    def choose_share_folder(self, task_id: str, folder_id: str, *, remember: bool = True) -> dict[str, Any]:
        """Explicit user selection of a verified share branch, never a drive CID.

        Folder names are informational. The actual transfer filter is an
        immutable folder ID from a freshly enumerated share listing.
        """
        ctx, provider = self._selectable_share_task(task_id)
        if not folder_id or len(folder_id) > 128:
            raise ValueError("文件夹 ID 无效")
        preview = self.share_folders(task_id)
        folder = next((entry for entry in preview["folders"] if entry["folder_id"] == folder_id), None)
        if folder is None:
            raise ConflictError("分享目录已改变或不存在，请重新读取后选择")
        if folder["count"] <= 0:
            raise ValueError("这个版本文件夹中没有可转存视频")
        previous = ctx.state
        ctx.ns("share_selection").update(folder_id=folder_id, path=folder["path"],
                                         provider=provider, share_code=ctx.parsed.share_code)
        ctx.log(f"人工选择分享版本「{folder['path']}」，只从所选文件夹取文件；其他版本不参与转存")
        self._update(ctx, expected=previous)
        if remember:
            key = f"{provider}:{ctx.parsed.share_code}"
            choices = self.store.state_get("share_folder_preferences", "items", {}) or {}
            choices[key] = {"folder_id": folder_id, "path": folder["path"]}
            self.store.state_set("share_folder_preferences", "items", choices)
        return self.share_folders(task_id)

    async def bulk(self, action: str, task_ids: list[str], *, save_dir_alias: str | None = None) -> list[dict[str, Any]]:
        """逐条批准 / 忽略 / 重试 / 重新识别，一条失败不影响其余（管理台、控制 Bot 与 MCP 共用）。

        每条返回 ``{task_id, ok, state}``，失败时 ``{task_id, ok: False, error, stale}``：stale 表示任务不在可操作的
        状态（多半是期间已被处理）或已不存在。
        """
        operations = {"approve": lambda task_id: self.approve(task_id, save_dir_alias=save_dir_alias),
                      "reject": self.reject, "retry": self.retry, "reparse": self.reparse}
        if action not in operations:
            raise ValueError(f"批量操作只能是 {'、'.join(operations)}")
        results = []
        for task_id in dict.fromkeys(task_ids):
            try:
                await operations[action](task_id)
            except (ConflictError, NotFoundError) as exc:
                results.append({"task_id": task_id, "ok": False, "error": str(exc), "stale": True})
                continue
            except Exception as exc:  # noqa: BLE001 - 逐条报告，不中断其余任务
                if not isinstance(exc, ValueError):
                    log.exception("批量操作 %s %s 出错", action, task_id)
                results.append({"task_id": task_id, "ok": False, "error": str(exc) or type(exc).__name__,
                                "stale": False})
                continue
            ctx = self.store.get_task(task_id)
            results.append({"task_id": task_id, "ok": True, "state": ctx.state.value if ctx else None})
        return results

    async def reject(self, task_id: str) -> None:
        """忽略：待审批的资源不转存；失败或待人工的任务不再处理（见 dismiss）。"""
        ctx = self.store.get_task(task_id)
        if not ctx:
            raise NotFoundError("任务不存在")
        if ctx.state in (TaskState.FAILED, TaskState.NEED_MANUAL):
            self.dismiss(ctx)
            return
        advice = self.advice_action(ctx) if ctx and ctx.state == TaskState.PENDING_APPROVAL else None
        if not await self.pipeline.reject(task_id):
            raise ConflictError("任务不是待审批状态")
        # 分享已失效时忽略不说明你不想要这部资源，不记为样本，以免影响偏好参考、通过率与来源质量建议
        if (ctx.meta.get("share") or {}).get("status") != "unavailable":
            self._record(ctx, "reject", {}, advice)

    def dismiss(self, ctx: PipelineContext) -> None:
        """失败或待人工的任务不再处理，转为已忽略（如分享已失效）。

        只限网盘里没有留下这次转存的任务：结果不明（receiving）的要先核验，文件已转存的要整理或删除
        转存文件。115 明确拒绝时阶段已退回 ready、暂存目录已清理，可以忽略。
        不记为反馈样本：这不代表你不想要这部资源。
        """
        storage = ctx.ns("storage")
        if storage.get("phase") == "receiving":
            raise ConflictError("转存结果还没核验，不能直接忽略；请先核验转存")
        if storage.get("landed"):
            raise ConflictError("文件已经转存，不能直接忽略；请重新整理或删除转存文件")
        previous = ctx.state
        ctx.state = TaskState.SKIPPED
        ctx.ns("task")["skip"] = {"kind": "user", "reason": "用户在管理端主动忽略失败或待人工任务"}
        ctx.log("管理端忽略了这条任务，不再处理")
        self._update(ctx, expected=previous)

    async def retry(self, task_id: str) -> None:
        ctx = self._require_context(task_id)
        if ctx.state != TaskState.FAILED:
            raise ConflictError("只有失败任务可以重试")
        retry_from = ctx.ns("task").get("retry_from", TaskState.QUEUED.value)
        try:
            next_state = TaskState(retry_from)
        except ValueError:
            next_state = TaskState.QUEUED
        if next_state not in (TaskState.RECEIVED, TaskState.QUEUED, TaskState.STORED):
            raise ConflictError("任务重试阶段无效，请核验检查点")
        ctx.state = next_state
        ctx.ns("task")["generation"] = ctx.ns("task").get("generation", 0) + 1
        ctx.ns("task").pop("no_auto_retry", None)  # 人工已介入，之后的失败可再自动重试
        ctx.log("管理端请求重试")
        self._update(ctx, expected=TaskState.FAILED, reset_retries=True)

    async def reparse(self, task_id: str) -> None:
        ctx = self._require_context(task_id)
        self._require_editable(ctx)
        if ctx.ns("storage").get("manifest") or ctx.ns("storage").get("landed"):
            raise ConflictError("已有转存检查点，不能重新解析；请核验转存或重新整理")
        previous = ctx.state
        ctx.ns("task")["generation"] = ctx.ns("task").get("generation", 0) + 1
        ctx.parsed = None
        ctx.decision = None
        ctx.result = None
        ctx.source_rule = self.pipeline.rule_for(ctx.raw.source)
        self.pipeline.stamp_config(ctx)
        ctx.meta.pop("parse", None)
        ctx.meta.pop("preference", None)
        ctx.meta.get("library", {}).pop("preferred", None)
        ctx.meta.pop("storage", None)  # 无远端写检查点，显式重新解析可重新选择 Provider。
        ctx.meta["parse_request"] = {"bypass_cache": True}  # 人工要求重新解析，不复用缓存结果
        ctx.state = TaskState.RECEIVED
        ctx.ns("task")["retry_from"] = TaskState.RECEIVED.value
        ctx.log("由管理端请求重新解析")
        self._update(ctx, expected=previous, reset_retries=True)

    async def requeue_organize(self, task_id: str, *, save_dir_alias: str | None = None,
                               conflict: str | None = None) -> None:
        """conflict 是库里已有同一集 / 同一部时人工的选择：skip 删除本次转存的文件，replace 覆盖保存。"""
        ctx = self._require_context(task_id)
        if ctx.state != TaskState.NEED_MANUAL:
            raise ConflictError("只有待人工整理任务可以重新整理")
        organize = ctx.ns("organize")
        if conflict is not None:
            if conflict not in ("skip", "replace", "suffix"):
                raise ValueError("conflict 只能是 skip、replace 或 suffix")
            organizer = self.get_config().get("plugins", {}).get("organizer", {})
            if organizer.get("no_delete", True) and conflict in ("skip", "replace"):
                raise ConflictError("安全模式禁止删除或覆盖文件；可选择保留两份并给新文件加后缀")
            if not organize.get("pending_conflict"):
                raise ConflictError("任务没有待处理的文件冲突")
            if save_dir_alias and save_dir_alias != ctx.save_dir_alias:
                raise ConflictError("处理冲突时不能同时更换保存目录")
        if save_dir_alias and save_dir_alias != ctx.save_dir_alias:
            self._validate_alias(save_dir_alias)
            ctx.save_dir_alias = save_dir_alias
            ctx.save_dir_cid = self.pipeline.cfg["save_dirs"][save_dir_alias]
            ctx.meta.pop("library", None)  # 改到其他目录后不再套用分类子目录
        # 按当前的命名模板重新生成整理计划；整理按文件 ID 读取实际位置续跑，已移动、改名的文件会挪到新位置
        organize.pop("plan", None)
        organize.pop("base_cid", None)
        if not ctx.ns("storage").get("landed"):
            raise ConflictError("任务没有已落地文件记录，不能只重新整理")
        pending = organize.pop("pending_conflict", None)
        if conflict:
            organize["resolve"] = conflict
            existing = (pending.get("existing") or {}).get("name")
            ctx.log(f"管理端选择删除本次转存的文件，保留库里的「{existing}」" if conflict == "skip"
                    else f"管理端选择覆盖保存，替换库里的「{existing}」" if conflict == "replace"
                    else f"管理端选择保留两份，新文件自动添加序号，不覆盖「{existing}」")
        else:
            organize.pop("resolve", None)
        ctx.state = TaskState.QUEUED
        ctx.ns("task")["action"] = "organize_only"
        ctx.ns("task")["generation"] = ctx.ns("task").get("generation", 0) + 1
        self.pipeline.stamp_config(ctx)
        ctx.ns("task")["retry_from"] = TaskState.QUEUED.value
        ctx.log("由管理端请求按当前命名重新整理，不重复转存")
        self._update(ctx, expected=TaskState.NEED_MANUAL, reset_retries=True)

    def _require_unknown_transfer(self, task_id: str) -> PipelineContext:
        ctx = self._require_context(task_id)
        storage = ctx.ns("storage")
        if ctx.state != TaskState.NEED_MANUAL or storage.get("phase") != "receiving" \
                or not storage.get("manifest"):
            raise ConflictError("只有转存结果未知且有清单的任务可以核验")
        return ctx

    async def accept_partial_transfer(self, task_id: str) -> None:
        """转存只到了一部分：按已到的文件继续整理入库，没到的不补转存、不记为已入库（以后可再转存）。"""
        ctx = self._require_unknown_transfer(task_id)
        storage = ctx.ns("storage")
        if not storage.get("partial"):
            raise ConflictError("核验时暂存目录里没有能对上清单的部分文件，不能按已到的文件继续")
        storage.pop("abandon", None)
        storage["accept_partial"] = True
        ctx.state = TaskState.QUEUED
        ctx.log("管理端选择按已到的文件继续，只读核验后整理，不补转存没到的文件")
        self._update(ctx, expected=TaskState.NEED_MANUAL)

    async def abandon_transfer(self, task_id: str) -> None:
        """放弃这次转存：删除暂存目录（进 115 回收站）后忽略任务。由 Worker 执行并读回确认。"""
        ctx = self._require_unknown_transfer(task_id)
        storage = ctx.ns("storage")
        storage.pop("accept_partial", None)
        storage["abandon"] = True
        ctx.state = TaskState.QUEUED
        ctx.log("管理端选择放弃这次转存，删除暂存目录后忽略")
        self._update(ctx, expected=TaskState.NEED_MANUAL)

    async def reconcile_transfer(self, task_id: str) -> None:
        ctx = self._require_unknown_transfer(task_id)
        storage = ctx.ns("storage")
        storage.pop("accept_partial", None)
        storage.pop("abandon", None)
        ctx.state = TaskState.QUEUED
        ctx.log("管理端请求只读核验转存，不重发转存请求")
        self._update(ctx, expected=TaskState.NEED_MANUAL)

    def patch_parsed(self, task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_context(task_id)
        self._require_editable(ctx)
        storage = ctx.ns("storage")
        if storage.get("manifest") and storage.get("phase") != "stored":
            raise ConflictError("转存结果未核验，不能修改解析结果")
        validate_resource(patch)
        if storage.get("landed") and set(patch) & {"share_code", "receive_code"}:
            raise ConflictError("已转存任务不能修改分享标识")
        before = asdict(ctx.parsed or ParsedResource())
        values = dict(before)
        values.update(patch)
        ctx.parsed = ParsedResource(**validate_resource(values))
        if any(before.get(key) != getattr(ctx.parsed, key) for key in ("title", "year", "media_type")):
            # 片名或类型改了，原来的 TMDB 匹配（含 tmdbid、海报）不再可信；待审批任务随后按新片名重新匹配
            ctx.meta.pop("tmdb", None)
            for key in ("tmdbid", "poster_path", "backdrop_path", "rating", "languages", "cast", "original_title",
                        "overview"):
                ctx.parsed.raw_fields.pop(key, None)
        ctx.ns("organize").pop("plan", None)
        ctx.log("管理端修正了解析结果")
        self._update(ctx, expected=ctx.state)
        after = asdict(ctx.parsed)
        changes = {key: [before[key], after[key]] for key in MEDIA_SCHEMA["properties"]
                   if key in after and before.get(key) != after[key]}
        if changes:
            # result 是修正后的完整识别结果，作为同来源后续解析的示例与本地评测的标准答案。
            self._record(ctx, "correct", {"changes": changes, "result": {
                key: after[key] for key in MEDIA_SCHEMA["properties"] if key in after}})
        return self.get_task(task_id)

    async def correct_parsed(self, task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """修正识别结果，待审批任务随后按修正后的片名重新核对 TMDB 并重算置信度（不调用 LLM）。"""
        self.patch_parsed(task_id, patch)
        await self.pipeline.refresh_task(task_id)
        return self.get_task(task_id)
