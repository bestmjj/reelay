"""Read-only live-file audit for manually repairing a completed Aliyun transfer.

Never infer a missing file from database dedup alone. Confirm resource drive,
original organized file IDs, and the actual destination directory before
allowing a new transfer. Read failures and changed identities fail closed.
"""
from __future__ import annotations

from typing import Any

from .contracts import PipelineContext, TaskState
from .media import episode_of, is_video
from ..providers.aliyun import AliyunAPIError


def scan_exact_season_episodes(storage, folder_id: str, season: int,
                               episodes: list[int]) -> dict[str, Any]:
    """Read-only inventory of a user-selected *final* Aliyun season folder.

    An old Reelay DONE/dedup entry is not proof that a physical video still
    exists. The scan uses the real Aliyun file list; unknown video identities
    fail closed to prevent copying a second release over a mislabeled file.
    """
    if (type(season) is not int or not 1 <= season <= 100
            or not episodes or len(episodes) > 100
            or len(set(episodes)) != len(episodes)
            or any(type(ep) is not int or not 1 <= ep <= 2000 for ep in episodes)):
        raise ValueError("仅能检查同一季的 1～100 个明确集号")
    requested = set(episodes)
    folder_id = str(folder_id)
    library = storage.library()
    attr = library.get_attr(folder_id)
    if not attr.get("is_dir") or str(attr.get("id")) != folder_id:
        raise ValueError("目标目录已变化或不是文件夹，停止批量补集")
    entries = list(library.iterdir(folder_id))
    if len(entries) > 2000:
        raise ValueError("目标目录文件过多，无法安全核对全部集数")
    present: set[int] = set()
    uncertain: list[str] = []
    for file in entries:
        name = str(file.get("name") or "")
        if file.get("is_dir") or not is_video(name):
            continue
        file_season, ep = episode_of(name)
        if ep is None or (file_season is not None and file_season != season):
            uncertain.append(name)
            continue
        if ep in requested:
            present.add(ep)
    if uncertain:
        raise ValueError("目标目录存在无法确认季集的视频（"
                         + "、".join(uncertain[:3])
                         + "），请先人工核对，未执行转存")
    return {
        "drive_id": str(storage.client.drive_id),
        "folder_id": folder_id,
        "season": season,
        "present": sorted(present),
        "missing": sorted(requested - present),
    }


def _not_found(exc: Exception) -> bool:
    return (isinstance(exc, AliyunAPIError) and exc.status == 404
            and exc.code.lower() in {"notfound.file", "filenotfound"})


def _original_rows(ctx: PipelineContext) -> list[dict[str, Any]]:
    if ctx.state != TaskState.DONE or not (ctx.result or {}).get("storage_verified"):
        raise ValueError("只有已核验入库的成功任务可以检查补转")
    storage = ctx.meta.get("storage") or {}
    manifest = storage.get("manifest") or {}
    plan = (ctx.meta.get("organize") or {}).get("plan") or []
    if storage.get("provider") != "aliyun" or not ctx.save_dir_cid:
        raise ValueError("目前仅支持阿里云盘成功任务的原文件实时核验")
    if not manifest.get("drive_id"):
        raise ValueError("旧任务缺少资源库 ID，不能安全判定文件已丢失")
    if not ctx.parsed or not ctx.parsed.share_code or not ctx.parsed.title:
        raise ValueError("旧任务缺少原分享或作品身份，不能安全补转")
    if (ctx.meta.get("organize") or {}).get("conflicts"):
        raise ValueError("旧任务存在整理冲突/覆盖记录，需先人工核对原文件")
    if manifest.get("variety_parts"):
        raise ValueError("综艺上下期/加更需逐版本处理，不能按普通剧集集号补转")
    files = manifest.get("files") or []
    if not files or not plan or len(files) != len(plan) or len(plan) > 1000:
        raise ValueError("缺少逐文件的原转存清单，或单任务文件超过 1000 个")
    by_name = {str(f.get("name") or ""): f for f in files}
    if len(by_name) != len(files):
        raise ValueError("旧转存清单存在重名文件，无法关联原文件 ID")
    seen = set()
    rows = []
    for item in plan:
        info = by_name.get(str(item.get("src_name") or ""))
        old_id = str(item.get("src_id") or "")
        if not info or not old_id or old_id in seen or not item.get("target_name"):
            raise ValueError("原整理计划与已入库清单不一致，必须人工确认")
        seen.add(old_id)
        season, episode = info.get("season"), info.get("episode")
        if ctx.parsed.media_type in ("tv", "anime"):
            if type(season) is not int or type(episode) is not int:
                raise ValueError("旧视频未记录可核验的季集号，不能自动补转")
        rows.append({
            "old_id": old_id, "source_id": str(info.get("id") or ""),
            "source_name": str(info.get("name") or ""),
            "source_size": info.get("size"), "season": season, "episode": episode,
            "target_dir": str(item.get("target_dir") or ""),
            "target_name": str(item["target_name"]),
            "size": item.get("size"), "sha1": item.get("sha1"),
        })
        if not rows[-1]["source_id"] or type(rows[-1]["size"]) is not int or rows[-1]["size"] <= 0:
            raise ValueError("旧任务缺少源文件 ID 或落地文件大小，不能安全补转")
    if ctx.parsed.media_type == "movie" and len(rows) != 1:
        raise ValueError("多文件电影暂不支持自动补转，防止遗漏分段")
    if ctx.parsed.media_type not in ("tv", "anime", "movie"):
        raise ValueError("仅支持影片、剧集或动漫成功任务补转")
    return rows


def check_saved_files(ctx: PipelineContext, storage, completed_repairs=()) -> dict[str, Any]:
    """Read-only cloud inspection; no deletes, moves or copy requests."""
    rows = _original_rows(ctx)
    drive = str(storage.client.drive_id)
    original_drive = str(ctx.ns("storage")["manifest"]["drive_id"])
    if drive != original_drive:
        raise ValueError("资源库 ID 与原任务不一致，拒绝把备份盘当作资源盘")
    library = storage.library()
    root = str(ctx.save_dir_cid)
    attr = library.get_attr(root) if root not in ("root", "0") else {"is_dir": True}
    if not attr.get("is_dir"):
        raise ValueError("原始保存根目录已变成文件，请先指定正确目录")
    base_path = storage.dir_path(root)
    if not isinstance(base_path, str) or not base_path.startswith("/"):
        raise ValueError("原始保存根目录路径不可核验")
    children = {}
    for repair in completed_repairs:
        if repair.state != TaskState.DONE or not (repair.result or {}).get("storage_verified"):
            continue
        for item in (repair.meta.get("organize") or {}).get("plan") or []:
            key = (item.get("target_dir") or "", item.get("target_name") or "")
            children.setdefault(key, []).append(str(item.get("src_id") or ""))
    dir_cache: dict[str, str] = {"": root}
    listed: dict[str, list[dict]] = {}

    def parent_of(relative: str) -> str:
        if relative in dir_cache:
            return dir_cache[relative]
        current = root
        parts = []
        for segment in relative.split("/"):
            if not segment:
                continue
            parts.append(segment)
            key = "/".join(parts)
            if key in dir_cache:
                current = dir_cache[key]
                continue
            matches = [entry for entry in library.iterdir(current)
                       if entry.get("name") == segment and entry.get("is_dir")]
            if len(matches) != 1:
                raise ValueError("原整理目录已删除或重名，先核对保存位置再补转：" + key)
            current = str(matches[0]["id"])
            dir_cache[key] = current
        return current

    present, missing, uncertain = [], [], []
    for row in rows:
        expected_parent = parent_of(row["target_dir"])
        if expected_parent not in listed:
            listed[expected_parent] = list(library.iterdir(expected_parent))
        directory_files = listed[expected_parent]
        current_ids = [row["old_id"], *children.get(
            (row["target_dir"], row["target_name"]), [])]
        exists = False
        changed = False
        for file_id in dict.fromkeys(current_ids):
            if not file_id:
                continue
            # A complete paginated folder listing verifies all surviving
            # files at once. Only IDs absent from that directory need the
            # extra per-file lookup to distinguish "moved" from "deleted".
            found = next((item for item in directory_files
                          if str(item.get("id")) == file_id), None)
            if found is None:
                try:
                    found = library.get_attr(file_id)
                except Exception as exc:
                    if _not_found(exc):
                        continue
                    raise ValueError("无法核验原云盘文件 ID，禁止据此判断丢失") from exc
            if found.get("is_dir") or found.get("size") != row["size"]:
                uncertain.append({**row, "reason": "原文件 ID 指向的类型或大小发生变化"})
                changed = True
                break
            if (str(found.get("parent_id")) != expected_parent
                    or found.get("name") != row["target_name"]):
                present.append({**row, "note": "文件 ID 仍存在，但位置或名称已变化"})
            else:
                present.append({**row, "note": "原文件仍在云盘"})
            exists = True
            break
        if exists or changed:
            continue
        collisions = [item for item in directory_files
                      if not item.get("is_dir") and item.get("name") == row["target_name"]]
        if collisions:
            if len(collisions) == 1 and collisions[0].get("size") == row["size"]:
                present.append({**row, "note": "目标目录有同名同大小文件，未视作丢失"})
            else:
                uncertain.append({**row, "reason": "目标位置存在同名但大小不同或多个文件"})
        else:
            # A replacement can have a different name/extension from the
            # old file. Treat an alternative E11 in this same verified folder
            # as ambiguous, never blindly copy a second E11.
            alternatives = []
            if type(row.get("episode")) is int:
                for item in directory_files:
                    if item.get("is_dir") or not is_video(item.get("name", "")):
                        continue
                    file_season, file_ep = episode_of(item.get("name", ""))
                    if (file_ep == row["episode"]
                            and file_season in (None, row["season"])):
                        alternatives.append(item)
            if alternatives:
                uncertain.append({**row, "reason": "原目录中还有同季同集的其他命名视频，需人工核对"})
            else:
                missing.append(row)

    return {
        "source_task_id": ctx.task_id, "drive_id": drive, "base_cid": root,
        "base_path": base_path, "total": len(rows),
        "present": present, "missing": missing, "uncertain": uncertain,
    }
