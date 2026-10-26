"""Bounded read-only inventory of existing files in configured cloud directories.

This is deliberately separate from Reelay's verified transfer ledger. Scanning
must never create tasks, alter dedup keys, move/delete files or infer that a
previously existing cloud file was successfully saved by Reelay.
"""
from __future__ import annotations

import itertools
import re
import time
from collections import deque
from typing import Callable

from .media import RESOLUTION_LABELS, episode_of, is_video, resolution_rank, season_of, strip_ext
from ..plugins.organizer import guess_from_names

MAX_FOLDERS = 80
MAX_VIDEOS = 2000
MAX_DEPTH = 5
MAX_SECONDS = 45
MAX_ITEMS_PER_FOLDER = 1200

_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_GENERIC = re.compile(r"(?i)^(?:s\d{1,2}|season\s*\d+|第[一二三四五六七八九十0-9]+季|"
                      r"e\d+|ep\d+|\d{1,4}|1080p|2160p|4k|8k)$")


def _target(alias: str, reference: str, config: dict) -> tuple[str, str]:
    """Explicit 'aliyun:' always wins; don't guess cross-provider IDs."""
    if reference.startswith("aliyun:"):
        return "aliyun", reference[len("aliyun:"):]
    fallback = config["defaults"].get("storage", "p115")
    sources = [x for x in config.get("sources", []) if (x.get("save_to") or config["defaults"].get("save_to")) == alias]
    used = {x.get("storage") or fallback for x in sources}
    # Legacy numeric folders are ambiguous for a mixed-provider alias; skip.
    if len(used) > 1 and "aliyun" in used:
        raise ValueError("目录别名同时用于不同网盘，需在保存目录中重新绑定网盘")
    if used == {"aliyun"}:
        return "aliyun", reference
    return fallback, reference


def _best_title(name: str, parents: tuple[str, ...]) -> str:
    guessed = (guess_from_names([name]).get("title") or "").strip()
    # Episode-only or very generic filenames need their show folder for identity.
    if not guessed or _GENERIC.fullmatch(guessed):
        for folder in reversed(parents):
            if folder and not _GENERIC.fullmatch(folder):
                return folder[:160]
    return (guessed or strip_ext(name))[:160]


def _media_type(alias: str, name: str, parents: tuple[str, ...]) -> str:
    context = " ".join([alias, *parents])
    if any(word in context for word in ("动漫电影", "动画电影")):
        return "movie"
    if any(word in context for word in ("动漫", "动画")):
        return "anime"
    if any(word in context for word in ("剧集", "连续剧", "电视剧", "剧场")):
        return "tv"
    if "电影" in context:
        return "movie"
    return "tv" if episode_of(name)[1] is not None else "movie"


def inventory(config: dict, get_provider: Callable[[str], object], *, now: Callable[[], float] = time.monotonic) -> dict:
    """One explicit bounded scan; returns an immutable snapshot for atomic KV replacement."""
    started = now()
    clock_time = time.time()
    groups: dict[tuple[str, str, str, str, str], dict] = {}
    seen_files: set[tuple[str, str, str]] = set()
    seen_folders: set[tuple[str, str, str]] = set()
    errors: list[str] = []
    folders = videos = roots = 0
    truncated = False
    aliases = list((config.get("save_dirs") or {}).items())
    # Prefer specific named roots to "默认", which often overlaps everything.
    aliases.sort(key=lambda x: (x[0] == "默认", x[0]))

    for alias, ref in aliases:
        if folders >= MAX_FOLDERS or videos >= MAX_VIDEOS or now() - started >= MAX_SECONDS:
            truncated = True
            break
        try:
            provider_name, root = _target(alias, str(ref), config)
            if provider_name not in ("p115", "aliyun"):
                errors.append(f"「{alias}」网盘 {provider_name} 不支持只读扫描")
                continue
            # Root 0 may contain enormous personal data. A named subfolder is
            # the safe default; never traverse a whole cloud account implicitly.
            if root in ("0", "root"):
                errors.append(f"「{alias}」指向网盘根目录，请先在设置中选择具体的影视文件夹")
                continue
            provider = get_provider(provider_name)
            if not provider.ready():
                errors.append(f"「{alias}」所属网盘尚未登录")
                continue
            disk = str(provider.client.drive_id) if provider_name == "aliyun" else "115"
            fs = provider.library()
        except Exception as exc:  # noqa: BLE001 - safe per-alias failure, no credentials in message
            errors.append(f"「{alias}」读取网盘失败（{type(exc).__name__}）")
            continue

        pending = deque([(str(root), tuple(), 0)])
        read_root = False
        while pending:
            if folders >= MAX_FOLDERS or videos >= MAX_VIDEOS or now() - started >= MAX_SECONDS:
                truncated = True
                break
            cid, parents, depth = pending.popleft()
            folder_key = (provider_name, disk, cid)
            if folder_key in seen_folders:
                # Nested aliases may overlap; physical files should be counted once.
                read_root = True
                continue
            try:
                # islice bounds generators, avoiding a full account inventory.
                cursor = int(cid) if provider_name == "p115" else cid
                entries = list(itertools.islice(fs.iterdir(cursor), MAX_ITEMS_PER_FOLDER + 1))
                if len(entries) > MAX_ITEMS_PER_FOLDER:
                    entries = entries[:MAX_ITEMS_PER_FOLDER]
                    truncated = True
                    errors.append(f"「{alias}」目录项目过多，仅检查前 {MAX_ITEMS_PER_FOLDER} 项")
                read_root = True
            except Exception as exc:  # noqa: BLE001 - don't turn a failed list into an empty folder
                errors.append(f"「{alias}」目录读取失败（{type(exc).__name__}），结果可能不完整")
                continue
            seen_folders.add(folder_key)
            folders += 1
            for item in entries:
                if not isinstance(item, dict):
                    continue
                file_id, name = str(item.get("id") or ""), str(item.get("name") or "")
                if not file_id or not name:
                    continue
                is_dir = bool(item.get("is_dir") or item.get("is_directory"))
                if is_dir:
                    if depth < MAX_DEPTH:
                        pending.append((file_id, (*parents, name), depth + 1))
                    else:
                        truncated = True
                    continue
                if not is_video(name):
                    continue
                file_key = (provider_name, disk, file_id)
                if file_key in seen_files:
                    continue
                if videos >= MAX_VIDEOS:
                    truncated = True
                    break
                seen_files.add(file_key)
                videos += 1
                kind = _media_type(alias, name, parents)
                title = _best_title(name, parents)
                key = (provider_name, disk, alias, kind, title.casefold())
                group = groups.setdefault(key, {
                    "title": title, "media_type": kind, "year": None, "tmdbid": None,
                    "episodes": [], "episode_pairs": None, "tasks": 0, "bytes": 0,
                    "poster_path": None, "save_dir_alias": alias, "files": [], "updated_at": clock_time,
                    "resolution": None, "release": [], "origin_country": [], "genre_ids": [],
                    "origin": "scanned", "provider": provider_name, "file_count": 0, "rank": 0,
                    "_pairs": set(),
                })
                year = _YEAR.search(name)
                if year and group["year"] is None:
                    group["year"] = int(year.group())
                size = item.get("size")
                group["bytes"] += size if type(size) is int and size > 0 else 0
                group["file_count"] += 1
                group["rank"] = max(group["rank"], resolution_rank(name))
                season, episode = episode_of(name)
                if kind != "movie" and episode is not None:
                    if season is None:
                        season = next((x for x in (season_of(p) for p in reversed(parents)) if x is not None), 1)
                    group["_pairs"].add((season, episode))
                if len(group["files"]) < 100:
                    group["files"].append("/".join((*parents, name)))
        if read_root:
            roots += 1

    result = []
    for item in groups.values():
        pairs = sorted(item.pop("_pairs"))
        rank = item.pop("rank")
        item["resolution"] = RESOLUTION_LABELS.get(rank)
        item["episodes"] = sorted({ep for _, ep in pairs})
        item["episode_pairs"] = [[season, ep] for season, ep in pairs] if len({x[0] for x in pairs}) > 1 else None
        result.append(item)
    result.sort(key=lambda x: (x["save_dir_alias"] or "", x["title"]))
    return {
        "groups": result, "total_done": 0,
        "estimated_gb": round(sum(g["bytes"] for g in result) / 1024**3, 2),
        "scanned_at": clock_time, "file_count": videos, "folder_count": folders,
        "scanned_roots": roots, "errors": errors[:30], "complete": not truncated and not errors,
        "truncated": truncated, "limits": {
            "folders": MAX_FOLDERS, "videos": MAX_VIDEOS,
            "depth": MAX_DEPTH, "seconds": MAX_SECONDS,
        },
    }
