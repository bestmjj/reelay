"""任务操作：批准、忽略、重试、重新识别、重新整理、核验转存、修正识别结果。

人工裁决（批准、忽略、修正）在这里记成带标签的样本（``feedback`` 表），供解析示例、偏好决策、通过率统计与本地评测使用。
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
from dataclasses import asdict, replace
from typing import Any


from .. import messages, repair, source_audit, subscriptions
from ..media import is_video
from ..contracts import Decision, ParsedResource, PipelineContext, TaskState
from ..validation import MEDIA_SCHEMA, validate_resource
from .base import ConflictError, NotFoundError, ServiceBase

log = logging.getLogger("reelay.service")

# 排队中、执行中或已完成的任务：不能重新识别或修改
_BUSY = (TaskState.RECEIVED, TaskState.PARSED, TaskState.QUEUED, TaskState.SAVING, TaskState.STORED,
         TaskState.ORGANIZING, TaskState.DONE)


class TaskOpsMixin(ServiceBase):
    """任务的人工操作；只改任务状态，115 操作由 Worker 执行。"""

    def _verify_existing_episode_folder(
        self, ctx: PipelineContext, folder_id: str, drive_id: str,
        mode: str = "existing",
    ) -> dict[str, Any]:
        """Verify a user-picked Aliyun destination without writing cloud data.

        "existing" preserves strict legacy E247 checks. "exact" saves files
        directly into any existing folder; "organized" uses that folder as
        the root for ordinary <title>/Season XX organization. The latter two
        are available to corrected Telegram tasks, including whole seasons.
        """
        if mode not in ("existing", "exact", "organized"):
            raise ValueError("保存方式无效，请重新选择")
        if mode == "existing":
            if not ctx.raw.extra.get("manual_import") or len(ctx.episode_pairs()) != 1:
                raise ConflictError("原有剧集目录模式仅支持人工确认的单集任务")
            if not ctx.ns("share_selection").get("file_id"):
                raise ConflictError("请先锁定单个源视频，再选择已有剧集目录")
        if not ctx.parsed or not ctx.parsed.title or ctx.parsed.media_type not in ("anime", "tv", "movie"):
            raise ConflictError("请先修正并保存真实片名和影视类型，再指定目标目录")
        if ctx.parsed.media_type in ("anime", "tv") and not ctx.episode_pairs():
            raise ConflictError("请先修正并保存季号及明确集数，防止整季误转存")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", folder_id) or folder_id in ("0", "root"):
            raise ValueError("请选择阿里云盘已有的非根目录文件夹")
        provider = (ctx.ns("storage").get("provider") or ctx.source_rule.get("storage")
                    or self.pipeline.cfg["defaults"].get("storage", "p115"))
        if provider != "aliyun":
            raise ConflictError("指定阿里云盘目录只支持阿里云盘任务，不能跨网盘转存")
        storage = self.pipeline.reg.get_provider("storage", "aliyun")
        try:
            actual_drive = str(storage.client.drive_id)
            if not drive_id or actual_drive != drive_id:
                raise ConflictError("阿里云盘资源库已变化，请重新选择目录")
            library = storage.library()
            attr = library.get_attr(folder_id)
            path = storage.dir_path(folder_id)
            if mode == "existing":
                children = library.iterdir(folder_id)
        except ConflictError:
            raise
        except Exception:
            raise ConflictError("无法核验所选目录是否仍存在于阿里云盘资源库，请刷新后重选") from None
        if (not attr.get("is_dir") or str(attr.get("id")) != folder_id
                or not path or not path.startswith("/")
                or any(part.casefold().startswith(".reelay-") for part in path.split("/"))):
            raise ConflictError("所选位置不是可用的阿里云盘资源库目录")
        title = ctx.parsed.title.strip()
        if mode == "existing":
            if not any(not item.get("is_dir") and is_video(item.get("name", ""))
                       for item in children):
                raise ConflictError("这个文件夹没有已有视频，请进入实际存放旧集的最终目录后再选择")
            if title.casefold() not in path.casefold():
                raise ConflictError(f"所选目录路径没有包含「{title}」，请选该作品原有的剧集文件夹")
        return {
            "id": folder_id, "drive_id": actual_drive, "path": path,
            "title": title, "media_type": ctx.parsed.media_type,
            "season": ctx.parsed.season, "pairs": [list(p) for p in ctx.episode_pairs()],
            "share_code": ctx.parsed.share_code, "provider": "aliyun", "mode": mode,
        }

    async def approve(self, task_id: str, *, save_dir_alias: str | None = None,
                      existing_folder_id: str | None = None,
                      existing_drive_id: str | None = None,
                      destination_mode: str = "existing") -> None:
        """save_dir_alias 是手动指定的目录：与自动分类不一致时直接存入，不套用分类子目录。"""
        ctx = self._require_context(task_id)
        if ctx.state != TaskState.PENDING_APPROVAL:
            raise ConflictError("任务不是待审批状态")
        if (ctx.raw.extra.get("manual_review")
                or (ctx.raw.extra.get("manual_import") and ctx.raw.extra.get("manual_unlabeled"))):
            p = ctx.parsed
            if not p or not p.title or p.media_type not in ("anime", "tv", "movie"):
                raise ConflictError("人工提交的分享请先填写真实片名、类型，保存后再确认转存")
            if p.media_type in ("anime", "tv"):
                is_bulk_recovery = bool(ctx.ns("task").get("force_missing_range"))
                if len(ctx.episode_pairs()) != 1 and not is_bulk_recovery:
                    raise ConflictError("人工单集转存必须明确唯一季集号，禁止盲目整季复制")
                # Original manual-import rule applies to a single exact episode.
                # Bulk recovery validates its immutable season/range and chosen
                # source folder below, even if a prior "保存修改" reset the
                # legacy episodes_from rule to "share".
                if not is_bulk_recovery and ctx.source_rule.get("episodes_from") != "message":
                    raise ConflictError("已修正的单集范围未绑定，请先保存识别结果")
                if not (ctx.ns("share_selection").get("file_id")
                        or ctx.ns("share_selection").get("folder_id")):
                    raise ConflictError("请先读取分享列表，选择正确的源文件或源文件夹，再确认转存")
            if ctx.raw.extra.get("manual_unlabeled") and not (
                    save_dir_alias or existing_folder_id or ctx.ns("library").get("pinned")):
                raise ConflictError("只有分享链接的手动任务必须明确选择目标保存目录，避免误存到默认路径")
        forced_range = ctx.ns("task").get("force_missing_range") or {}
        if forced_range:
            selected = ctx.ns("share_selection")
            if (ctx.source_rule.get("storage") != "aliyun"
                    or ctx.parsed.season != forced_range.get("season")
                    or ctx.parsed.share_code != forced_range.get("share_code")
                    or ctx.parsed.episodes != forced_range.get("episodes")
                    or selected.get("only_episodes") != forced_range.get("episodes")
                    or not selected.get("folder_id")
                    or selected.get("share_code") != ctx.parsed.share_code
                    or selected.get("provider") != "aliyun"
                    or selected.get("file_id")):
                raise ConflictError("批量补集需先选择同一季明确集号，并确认唯一的分享版本文件夹")
            if not existing_folder_id or destination_mode != "exact":
                raise ConflictError("批量补集必须选择阿里云盘最终 Season 文件夹，保存方式为直接存入")
            # Persist the reviewed subset as the source scope. A no-op parsed
            # save must never turn this authorized 1-15 batch into whole-share.
            ctx.source_rule["episodes_from"] = "message"
        forced = ctx.ns("task").get("force_single_copy") or {}
        if forced:
            selected = ctx.ns("share_selection")
            if (ctx.source_rule.get("storage") != "aliyun"
                    or not selected.get("file_id")
                    or selected.get("share_code") != ctx.parsed.share_code
                    or selected.get("provider") != "aliyun"
                    or [list(pair) for pair in ctx.episode_pairs()]
                    != [[forced.get("season"), forced.get("episode")]]):
                raise ConflictError("强制转存仅支持已核对的单集；请先选择精确源文件")
            if not existing_folder_id or destination_mode != "exact":
                raise ConflictError("强制转存必须明确选择阿里云盘最终剧集文件夹，保存方式为直接存入")
        if existing_folder_id and save_dir_alias:
            raise ConflictError("按已有剧集目录保存与选择分类目录只能二选一")
        if bool(existing_folder_id) != bool(existing_drive_id):
            raise ValueError("已有目录的文件夹 ID 和资源库 ID 必须同时提供")
        if existing_folder_id:
            existing = await asyncio.to_thread(self._verify_existing_episode_folder,
                                               ctx, existing_folder_id, existing_drive_id,
                                               destination_mode)
            if forced_range:
                storage_provider = self.pipeline.reg.get_provider("storage", "aliyun")
                try:
                    report = await asyncio.to_thread(
                        repair.scan_exact_season_episodes, storage_provider,
                        existing_folder_id, forced_range["season"], forced_range["episodes"])
                except Exception as exc:
                    raise ConflictError(f"读取实际云盘目录失败，未排队补集：{exc}") from exc
                if report["drive_id"] != existing_drive_id:
                    raise ConflictError("资源库身份发生变化，禁止将补集复制到另一个网盘")
                if not report["missing"]:
                    raise ConflictError("您选择的集数已全部存在于此文件夹，无需批量转存")
                ctx.log("批量补集提交前只读扫描：" +
                        f"已有 {len(report['present'])} 集，实际缺 {len(report['missing'])} 集")
            ctx.ns("library")["direct_existing"] = existing
            ctx.ns("library").pop("pinned", None)
            mode_text = ("按作品和季整理" if destination_mode == "organized"
                         else "直接存入，不套用自动分类")
            ctx.log(f"人工指定阿里云盘最终保存位置：{existing['path']}（{mode_text}）")
        else:
            ctx.ns("library").pop("direct_existing", None)
        advice = self.advice_action(ctx)
        if save_dir_alias:
            provider = (ctx.ns("storage").get("provider") or ctx.source_rule.get("storage")
                        or self.pipeline.cfg["defaults"].get("storage", "p115"))
            self._validate_alias(save_dir_alias, provider=provider)
            ctx.save_dir_alias = save_dir_alias
            ctx.ns("library")["pinned"] = save_dir_alias
        elif ctx.ns("library").pop("pinned", None):
            ctx.save_dir_alias = None  # 撤销此前手动指定的目录，重新按分类
        # Preview and approval audit must reflect the manually pinned target.
        target = self.pipeline.target_preview(ctx)
        ctx.state = TaskState.QUEUED
        ctx.ns("task")["user_approved"] = True
        ctx.ns("task").pop("skip", None)
        ctx.log("管理端批准并加入队列；只有网盘复制完成并核验后才算已入库，去重可能跳过")
        self._update(ctx, expected=TaskState.PENDING_APPROVAL)
        self._record(ctx, "approve", {"save_dir_alias": save_dir_alias or None, "target": target,
                                      "existing_folder": bool(existing_folder_id),
                                      "destination_mode": destination_mode if existing_folder_id else None}, advice)
        try:
            self._suggest_subscription(ctx)
        except Exception as exc:  # noqa: BLE001 - 订阅建议不影响批准
            log.warning("订阅建议生成失败：%s", exc)

    def repair_check(self, task_id: str) -> dict[str, Any]:
        """Read-only inspection of originally organized cloud file IDs."""
        ctx = self._require_context(task_id)
        if "repair_missing" not in self.task_actions(ctx):
            raise ConflictError("只有阿里云盘已完成且未隔离的任务支持核验补转")
        original = ctx.ns("storage").get("manifest") or {}
        if not original.get("drive_id"):
            raise ConflictError("旧任务没有资源库 ID，不能安全核验丢失文件")
        storage = self.pipeline.reg.get_provider("storage", "aliyun")
        children = [self.store.get_task(child_id)
                    for child_id in ctx.ns("task").get("repair_children", [])]
        try:
            return repair.check_saved_files(
                ctx, storage, [child for child in children if child is not None])
        except (ValueError, RuntimeError) as exc:
            raise ConflictError(str(exc)) from exc
        except Exception as exc:  # Aliyun API/network errors are not evidence of absence
            log.warning("任务 %s 云盘实时补存核验失败（%s），未修改任务",
                        ctx.task_id, type(exc).__name__)
            raise ConflictError("无法完成云盘实时核验（网络或接口错误）；未执行转存，请稍后重试") from exc

    def repair_missing(self, task_id: str, missing_ids: list[str]) -> dict[str, Any]:
        """Queue *only verified missing* files as a new task, never replay DONE.

        This is not a version upgrade or forced overwrite. Every file is
        rechecked here and again by the Worker before sending a copy request.
        """
        if not missing_ids or len(missing_ids) > 100 or len(set(missing_ids)) != len(missing_ids):
            raise ValueError("请选择核验为丢失的 1～100 个文件")
        ctx = self._require_context(task_id)
        report = self.repair_check(task_id)
        if report["uncertain"]:
            raise ConflictError("部分文件位置不确定，请先人工核对，禁止批量重新转存")
        missing = {row["old_id"]: row for row in report["missing"]}
        if any(file_id not in missing for file_id in missing_ids):
            raise ConflictError("文件状态已变化或并未丢失，请重新核验后再补转")
        selected = [missing[file_id] for file_id in missing_ids]
        seasons = {row["season"] for row in selected if row["episode"] is not None}
        if len(seasons) > 1:
            raise ConflictError("跨多季合集请每次选择同一季的丢失文件补转")
        new_ctx = PipelineContext(copy.deepcopy(ctx.raw))
        new_ctx.raw.msg_id = f"{ctx.raw.msg_id}:repair:{new_ctx.task_id}"
        new_ctx.raw.extra["repair_of"] = ctx.task_id
        new_ctx.parsed = copy.deepcopy(ctx.parsed)
        if seasons:
            new_ctx.parsed.season = next(iter(seasons))
            new_ctx.parsed.episodes = sorted({row["episode"] for row in selected})
        new_ctx.state, new_ctx.decision = TaskState.QUEUED, Decision.SAVE
        new_ctx.source_rule = copy.deepcopy(ctx.source_rule)
        new_ctx.source_rule["storage"] = "aliyun"
        new_ctx.source_rule["episodes_from"] = "message"
        new_ctx.save_dir_alias = ctx.save_dir_alias
        new_ctx.meta["runtime"] = copy.deepcopy(ctx.meta.get("runtime") or {})
        new_ctx.meta["tmdb"] = copy.deepcopy(ctx.meta.get("tmdb") or {})
        original_library = ctx.meta.get("library") or {}
        original_mode = (original_library.get("direct_existing") or {}).get("mode")
        # Exact old destination keeps a flat layout. Otherwise preserve the
        # original normalized category + title + Season path under the old root.
        mode = "exact" if original_mode in ("exact", "existing") else "repair"
        new_ctx.meta["library"] = {
            "relative_category": "" if mode == "exact"
                                 else original_library.get("relative_category", ""),
            "direct_existing": {
                "id": report["base_cid"], "drive_id": report["drive_id"],
                "path": report["base_path"], "provider": "aliyun",
                "title": new_ctx.parsed.title, "media_type": new_ctx.parsed.media_type,
                "share_code": new_ctx.parsed.share_code, "mode": mode,
                "pairs": [list((row["season"], row["episode"])) for row in selected
                          if row["episode"] is not None],
            },
        }
        selection = ctx.meta.get("share_selection") or {}
        new_selection = {}
        if seasons:
            new_selection["only_episodes"] = list(new_ctx.parsed.episodes)
        if len(selected) == 1 and selected[0]["episode"] is not None:
            row = selected[0]
            original_info = next((item for item in
                                  (ctx.ns("storage")["manifest"].get("files") or [])
                                  if str(item.get("id")) == row["source_id"]), {})
            new_selection.update(
                file_id=row["source_id"], file_name=row["source_name"],
                file_path=original_info.get("share_path") or "",
                file_size=row["source_size"],
                share_code=new_ctx.parsed.share_code, provider="aliyun")
        elif selection.get("folder_id"):
            for name in ("folder_id", "path", "share_code", "provider"):
                if name in selection:
                    new_selection[name] = selection[name]
        new_ctx.meta["share_selection"] = new_selection
        new_ctx.ns("task")["repair"] = {
            "source_task_id": ctx.task_id, "drive_id": report["drive_id"],
            "files": [dict(row) for row in selected],
        }
        new_ctx.log(f"人工确认原任务 {ctx.task_id} 缺失 {len(selected)} 个云盘文件；"
                    "新任务仅补缺失文件，保留旧入库历史和去重表")
        try:
            self.store.queue_repair_task(ctx, new_ctx)
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc
        return {"ok": True, "task_id": new_ctx.task_id, "state": new_ctx.state.value,
                "file_count": len(selected)}

    def reopen_duplicate(self, task_id: str) -> dict[str, Any]:
        """Let an operator inspect a fully skipped bundle for newly missing
        episodes. This changes only the local task state, not any cloud file.
        The first automatic duplicate decision still remains in task history.
        """
        ctx = self._require_context(task_id)
        previous = ctx.state
        storage = ctx.ns("storage")
        skip = ctx.ns("task").get("skip") or {}
        if (previous != TaskState.SKIPPED or skip.get("kind") != "duplicate"
                or not ctx.parsed or ctx.parsed.media_type not in ("tv", "anime")
                or not ctx.parsed.title or not ctx.parsed.share_code
                or storage.get("manifest") or storage.get("landed")
                or storage.get("phase") in ("receiving", "stored")):
            raise ConflictError("只有未发生转存的剧集「重复跳过」任务可以重新检查缺集")
        ctx.state = TaskState.PENDING_APPROVAL
        ctx.decision = Decision.ASK
        ctx.ns("task")["generation"] = ctx.ns("task").get("generation", 0) + 1
        ctx.ns("task")["user_reopened_duplicate"] = True
        ctx.ns("task").pop("skip", None)
        ctx.source_rule["approval_mode"] = "manual"
        ctx.log("人工重新检查此前自动跳过的分享：可指定需要补的集数；不会关闭已有集去重")
        self._update(ctx, expected=previous)
        return self.get_task(task_id)

    def force_reopen_duplicate(self, task_id: str) -> dict[str, Any]:
        """Explicit manual override of a stale SxxExx ledger, NOT a global dedup switch.

        Requires an exact source file and an existing destination before approval.
        It does not remove historical DONE receipts or delete cloud files.
        """
        ctx = self._require_context(task_id)
        if "force_reopen_duplicate" not in self.task_actions(ctx):
            raise ConflictError("只能对未发生转存的阿里云盘单集重复任务人工强制转存")
        season, episode = ctx.episode_pairs()[0]
        ctx.ns("task")["force_single_copy"] = {
            "season": season, "episode": episode,
            "share_code": ctx.parsed.share_code,
        }
        ctx.ns("task")["generation"] = ctx.ns("task").get("generation", 0) + 1
        ctx.ns("task")["user_reopened_duplicate"] = True
        ctx.ns("task").pop("skip", None)
        ctx.ns("share_selection").clear()
        ctx.ns("share_selection")["only_episodes"] = [episode]
        ctx.ns("library").pop("direct_existing", None)
        ctx.state, ctx.decision = TaskState.PENDING_APPROVAL, Decision.ASK
        ctx.source_rule["approval_mode"] = "manual"
        ctx.source_rule["episodes_from"] = "message"
        ctx.log(f"人工选择强制重新转存 S{season:02d}E{episode:03d}："
                "旧入库记录保留；必须选择精确分享源文件及最终目标文件夹，禁止自动覆盖或删除")
        self._update(ctx, expected=TaskState.SKIPPED)
        return self.get_task(task_id)

    def force_missing_range(self, task_id: str, episodes: list[int]) -> dict[str, Any]:
        """Human-authorized batch retransfer of physically absent episodes.

        A historical duplicate task is reopened once. After user selects ONE
        share version and the original season folder, the Worker independently
        checks the actual files before any copy. Old DONE records are retained.
        """
        ctx = self._require_context(task_id)
        if "force_missing_range" not in self.task_actions(ctx):
            raise ConflictError("仅支持阿里云盘人工补集或已核验的成功任务")
        original = ctx if ctx.state == TaskState.DONE else None
        previous_state = ctx.state
        if (not episodes or len(episodes) > 100
                or any(type(ep) is not int or not 1 <= ep <= 2000 for ep in episodes)
                or len(set(episodes)) != len(episodes)):
            raise ValueError("请输入同一季的 1～100 个不重复集号，范围 1～2000")
        season = ctx.parsed.season
        if type(season) is not int or not 1 <= season <= 100:
            raise ConflictError("请先确认作品季号，再批量补集")
        requested = sorted(episodes)
        if original:
            # A completed task must remain DONE with its original receipt.
            # Clone only the proven share/work metadata into a new pending
            # draft. No cloud write and no old episode-ledger mutation.
            ctx = PipelineContext(copy.deepcopy(original.raw))
            ctx.raw.msg_id = f"{original.raw.msg_id}:bulk:{ctx.task_id}"
            ctx.raw.extra["bulk_from"] = original.task_id
            ctx.parsed = copy.deepcopy(original.parsed)
            ctx.source_rule = copy.deepcopy(original.source_rule)
            ctx.source_rule["storage"] = "aliyun"
            ctx.save_dir_alias = original.save_dir_alias
            ctx.meta["runtime"] = copy.deepcopy(original.meta.get("runtime") or {})
            ctx.meta["tmdb"] = copy.deepcopy(original.meta.get("tmdb") or {})
        ctx.ns("task")["force_missing_range"] = {
            "season": season, "episodes": requested, "share_code": ctx.parsed.share_code,
        }
        ctx.ns("task")["generation"] = ctx.ns("task").get("generation", 0) + 1
        ctx.ns("task")["user_reopened_duplicate"] = True
        ctx.ns("task").pop("skip", None)
        ctx.ns("share_selection").clear()
        ctx.ns("share_selection")["only_episodes"] = requested
        ctx.ns("library").pop("direct_existing", None)
        ctx.parsed.episodes = requested
        ctx.state, ctx.decision = TaskState.PENDING_APPROVAL, Decision.ASK
        ctx.source_rule["approval_mode"] = "manual"
        ctx.source_rule["episodes_from"] = "message"
        ctx.log(f"人工申请批量补回 S{season:02d} 的 {len(requested)} 集："
                "旧入库记录不会删除；需明确选分享版本与最终保存文件夹，"
                "实际缺集以云盘实时只读扫描为准")
        if original:
            try:
                self.store.queue_bulk_missing_draft(original, ctx)
            except ValueError as exc:
                raise ConflictError(str(exc)) from exc
        else:
            self._update(ctx, expected=previous_state, reset_retries=previous_state == TaskState.FAILED)
        return self.get_task(ctx.task_id)

    def set_episode_selection(self, task_id: str, episodes: list[int]) -> dict[str, Any]:
        """Per-task subset of episodes, with mandatory existing-content dedup.

        Empty list restores automatic share/message selection. Never let this
        edit override a verified transfer, quarantine, or a busy Worker task.
        """
        ctx = self._require_context(task_id)
        storage = ctx.ns("storage")
        if ctx.ns("task").get("force_single_copy") or ctx.ns("task").get("force_missing_range"):
            raise ConflictError("强制转存已锁定申请集数；如需修改，请重新提交新的人工补集任务")
        if ctx.state not in (TaskState.PENDING_APPROVAL, TaskState.FAILED):
            raise ConflictError("只能在待确认或尚未转存的失败任务中指定补集")
        if storage.get("manifest") or storage.get("landed") or storage.get("phase") == "receiving":
            raise ConflictError("该任务存在转存检查点，禁止修改要复制的集数")
        if not ctx.parsed or not ctx.parsed.title or ctx.parsed.media_type not in ("tv", "anime"):
            raise ConflictError("请先保存真实片名和剧集/动漫类型")
        if ctx.parsed.season is None or not 1 <= ctx.parsed.season <= 100:
            raise ConflictError("请先确认季号，再选择要补的集数（不能跨季）")
        if len(episodes) > 100 or any(type(ep) is not int or not 1 <= ep <= 2000 for ep in episodes):
            raise ValueError("本次最多选择 100 集，集号必须是 1～2000 的整数")
        if len(episodes) != len(set(episodes)):
            raise ValueError("集号有重复，请去重后提交")
        selected = sorted(episodes)
        selection = ctx.ns("share_selection")
        # File ID and folder version are tied to the previous requested set:
        # force a fresh read instead of reusing a wrong file or HDR folder.
        old = selection.get("only_episodes") or []
        if selected != old:
            ctx.ns("library").pop("direct_existing", None)
            for key in ("file_id", "file_name", "file_path", "file_size",
                        "folder_id", "path"):
                selection.pop(key, None)
            selection.pop("only_episodes", None)
            if selected:
                selection["only_episodes"] = selected
            ctx.log("人工指定只补 " + (", ".join(f"E{ep:03d}" for ep in selected)
                                       if selected else "恢复自动判断")
                    + "；已有核验集仍自动跳过，不允许借此洗版")
            self._update(ctx, expected=ctx.state)
        return self.get_task(task_id)

    def task_actions(self, ctx: PipelineContext) -> list[str]:
        """这条任务现在允许的人工操作（管理台按它显示按钮）；判断与各操作自己的检查一致。"""
        state, storage = ctx.state, ctx.meta.get("storage") or {}
        unknown_transfer = storage.get("phase") == "receiving" and bool(storage.get("manifest"))
        out: list[str] = []
        if (state == TaskState.SKIPPED and ctx.ns("task").get("skip", {}).get("kind") == "duplicate"
                and ctx.parsed and ctx.parsed.media_type in ("tv", "anime")
                and ctx.parsed.share_code and not storage.get("manifest") and not storage.get("landed")):
            out.append("reopen_duplicate")
            if ((ctx.ns("storage").get("provider") or ctx.source_rule.get("storage")) == "aliyun"
                    and (ctx.parsed.raw_fields or {}).get("share_provider") == "aliyun"
                    and type(ctx.parsed.season) is int and 1 <= ctx.parsed.season <= 100):
                out.append("force_missing_range")
            if (len(ctx.episode_pairs()) == 1
                    and (ctx.ns("storage").get("provider") or ctx.source_rule.get("storage")) == "aliyun"
                    and (ctx.parsed.raw_fields or {}).get("share_provider") == "aliyun"):
                out.append("force_reopen_duplicate")
        if (state == TaskState.DONE and (ctx.result or {}).get("storage_verified")
                and (storage.get("provider") or ctx.source_rule.get("storage")) == "aliyun"
                and ctx.parsed and ctx.parsed.title
                and ctx.parsed.media_type in ("anime", "tv")
                and ctx.parsed.share_code
                and (ctx.parsed.raw_fields or {}).get("share_provider") == "aliyun"
                and type(ctx.parsed.season) is int and 1 <= ctx.parsed.season <= 100
                and not (self.store.disputed_transfer(ctx.task_id) or {}).get("active")):
            out.append("force_missing_range")
        if state == TaskState.PENDING_APPROVAL:
            out += ["approve", "reject"]
            if subscriptions.subscribable(ctx) and not subscriptions.subscribed(self.store.subscriptions(), ctx):
                out.append("subscribe")
        if (state == TaskState.DONE and storage.get("provider") == "aliyun"
                and (ctx.result or {}).get("storage_verified")
                and (ctx.meta.get("organize") or {}).get("plan")
                and not (self.store.disputed_transfer(ctx.task_id) or {}).get("active")):
            out.append("repair_missing")
        if state == TaskState.FAILED:
            out.append("retry")
        if (state in (TaskState.PENDING_APPROVAL, TaskState.FAILED)
                and not ctx.ns("task").get("force_single_copy")
                and not ctx.ns("task").get("force_missing_range")
                and not storage.get("manifest") and not storage.get("landed")
                and ctx.parsed and ctx.parsed.title and ctx.parsed.media_type in ("tv", "anime")):
            out.append("select_episodes")
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
        only = ctx.ns("share_selection").get("only_episodes") or []
        preview_parsed = replace(ctx.parsed, episodes=list(only)) if only else ctx.parsed
        with self.pipeline.scope(ctx):
            inventory = storage.share_source_inventory(preview_parsed)
        folders = inventory["folders"]
        files = inventory["files"]
        if not folders and not files:
            raise ValueError("分享中找不到可以确认的源视频或文件夹")
        key = f"{provider}:{ctx.parsed.share_code}"
        preference = (self.store.state_get("share_folder_preferences", "items", {}) or {}).get(key)
        selection = ctx.ns("share_selection")
        selected = (None if selection.get("file_id") else
                    selection.get("folder_id") or (preference or {}).get("folder_id"))
        return {"folders": folders, "files": files,
                "selected_file_id": ctx.ns("share_selection").get("file_id"),
                "selected_folder_id": selected,
                "saved_preference": bool(preference), "provider": provider}

    def choose_share_folder(self, task_id: str, folder_id: str, *, remember: bool = True) -> dict[str, Any]:
        """Explicit user selection of a verified share branch, never a drive CID.

        Folder names are informational. The actual transfer filter is an
        immutable folder ID from a freshly enumerated share listing.
        """
        ctx, provider = self._selectable_share_task(task_id)
        if ctx.ns("task").get("force_single_copy"):
            raise ConflictError("强制转存必须选择精确的单集源文件，不能只选择版本文件夹")
        if not folder_id or len(folder_id) > 128:
            raise ValueError("文件夹 ID 无效")
        preview = self.share_folders(task_id)
        folder = next((entry for entry in preview["folders"] if entry["folder_id"] == folder_id), None)
        if folder is None:
            raise ConflictError("分享目录已改变或不存在，请重新读取后选择")
        if folder["count"] <= 0:
            raise ValueError("这个版本文件夹中没有可转存视频")
        previous = ctx.state
        selection = ctx.ns("share_selection")
        for key in ("file_id", "file_name", "file_path", "file_size"):
            selection.pop(key, None)
        selection.update(folder_id=folder_id, path=folder["path"],
                         provider=provider, share_code=ctx.parsed.share_code)
        ctx.log(f"人工选择分享版本「{folder['path']}」，只从所选文件夹取文件；其他版本不参与转存")
        self._update(ctx, expected=previous)
        if remember:
            key = f"{provider}:{ctx.parsed.share_code}"
            choices = self.store.state_get("share_folder_preferences", "items", {}) or {}
            choices[key] = {"folder_id": folder_id, "path": folder["path"]}
            self.store.state_set("share_folder_preferences", "items", choices)
        return self.share_folders(task_id)

    def choose_share_file(self, task_id: str, file_id: str) -> dict[str, Any]:
        """Pin a single source file ID; NEVER initiate a remote transfer.

        A fresh share scan is required at selection time and again during
        prepare; plain filenames/paths are only audit evidence.
        """
        ctx, provider = self._selectable_share_task(task_id)
        if ctx.ns("task").get("force_missing_range"):
            raise ConflictError("批量补集必须选择同一分享版本文件夹，而不是单个源文件")
        if not file_id or len(file_id) > 128:
            raise ValueError("源文件 ID 无效")
        only = ctx.ns("share_selection").get("only_episodes") or []
        count = len(only) if only else len(ctx.episode_pairs())
        if ctx.parsed.media_type not in ("tv", "anime") or count != 1:
            raise ConflictError("精确选择源视频之前请先指定唯一目标集数")
        preview = self.share_folders(task_id)
        matches = [item for item in preview["files"] if item["file_id"] == file_id]
        if len(matches) != 1 or not matches[0]["selectable"]:
            raise ConflictError("源文件不属于已确认单集、缺少大小信息或已失效，请重新选择")
        selected = matches[0]
        existing = ctx.ns("share_selection")
        only = existing.get("only_episodes")
        existing.clear()
        if only:
            existing["only_episodes"] = list(only)
        existing.update(file_id=file_id, file_name=selected["name"],
                        file_path=selected["path"], file_size=selected["size"],
                        share_code=ctx.parsed.share_code, provider=provider)
        ctx.log(f"人工选择精确分享源文件：{selected['path']}/{selected['name']}，"
                f"大小 {selected['size']} 字节；正式转存前会重新核验文件 ID")
        self._update(ctx, expected=ctx.state)
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
        ctx.meta.pop("share_selection", None)  # previous override/file ID cannot survive a new identity
        ctx.meta.get("library", {}).pop("preferred", None)
        ctx.meta.get("library", {}).pop("direct_existing", None)
        ctx.meta.get("library", {}).pop("pinned", None)
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
            provider = (ctx.ns("storage").get("provider") or ctx.source_rule.get("storage")
                        or self.pipeline.cfg["defaults"].get("storage", "p115"))
            self._validate_alias(save_dir_alias, provider=provider)
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
        forced = ctx.ns("task").get("force_single_copy") or {}
        if forced and (ctx.parsed.share_code != forced["share_code"]
                       or list(ctx.episode_pairs()) != [(forced["season"], forced["episode"])]
                       or any(before.get(key) != getattr(ctx.parsed, key)
                              for key in ("title", "media_type", "year", "season", "episodes"))):
            ctx.ns("task").pop("force_single_copy", None)
            ctx.log("修改作品身份或季集后已取消此前的强制转存授权")
        if ctx.ns("task").get("force_missing_range"):
            forced = ctx.ns("task")["force_missing_range"]
            if (ctx.parsed.share_code != forced["share_code"]
                    or ctx.parsed.season != forced["season"]
                    or ctx.parsed.episodes != forced["episodes"]
                    or any(before.get(key) != getattr(ctx.parsed, key)
                           for key in ("title", "year", "media_type", "season", "episodes"))):
                ctx.ns("task").pop("force_missing_range", None)
                ctx.log("修改作品身份或集数后已撤销原批量补集授权")
        if (ctx.ns("share_selection").get("only_episodes")
                and any(before.get(key) != getattr(ctx.parsed, key)
                        for key in ("title", "year", "media_type", "season", "episodes"))):
            ctx.ns("share_selection").pop("only_episodes", None)
            ctx.log("修改作品季集后已清除原指定补集，请重新确认")
        if (ctx.ns("library").get("direct_existing")
                and any(before.get(key) != getattr(ctx.parsed, key)
                        for key in ("title", "year", "media_type", "season", "episodes"))):
            ctx.ns("library").pop("direct_existing", None)
            ctx.log("修改作品或季集后，原来选择的已有剧集目标目录已失效")
        if (ctx.ns("share_selection").get("file_id")
                and any(before.get(key) != getattr(ctx.parsed, key)
                        for key in ("title", "year", "media_type", "season", "episodes"))):
            ctx.ns("share_selection").clear()
            ctx.log("修改了作品身份或季集：之前选中的源文件已失效，需重新确认")
        # The initial URL may have been parsed with unknown type/episodes.
        # A subsequent human correction to E247 MUST rebind the file
        # selector to that one episode, not the original whole-share scope.
        # This is a per-task source rule, never a persistent bot setting.
        if ctx.raw.extra.get("manual_import"):
            if ctx.ns("task").get("force_missing_range"):
                # A bulk recovery approved by a human is always restricted
                # to its already-bound episode subset. Saving title/reason
                # again must not silently revert the source scope to "share".
                ctx.source_rule["episodes_from"] = "message"
                ctx.log("人工批量补集仍锁定原已确认集数；不扩大为整个分享")
            elif ctx.parsed.media_type in ("tv", "anime") and len(ctx.episode_pairs()) == 1:
                ctx.source_rule["episodes_from"] = "message"
                ctx.log(f"人工确认单集 E{ctx.parsed.episodes[0]:03d}：仅选择该集的源文件")
            else:
                ctx.source_rule["episodes_from"] = "share"
                ctx.log("手动分享不是唯一单集；转存前必须再核对分享清单")
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
