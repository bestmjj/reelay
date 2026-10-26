"""Resumable, READ-ONLY cloud inventory with immutable file identities.

One call reads at most the existing inventory budget; next calls continue the
queue rather than starting over. This is an OBSERVATION, not a verified Reelay
transfer: it must never add records to the transfer ledger.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import re
import time
from collections import defaultdict
from copy import deepcopy
from typing import Callable

from . import library_scan
from .media import RESOLUTION_LABELS, episode_of, is_video, resolution_rank, season_of

_VERSION = 1
_TMDB_ID = re.compile(r"\[tmdbid=(\d+)\]", re.I)
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def _fingerprint(config: dict) -> str:
    scope = {key: config.get(key) for key in ("save_dirs", "defaults", "sources")}
    return hashlib.sha256(json.dumps(scope, sort_keys=True, ensure_ascii=False,
                                      default=str).encode()).hexdigest()


def _start(config: dict) -> dict:
    aliases = sorted((config.get("save_dirs") or {}).items(),
                     key=lambda x: (x[0] == "默认", x[0]))
    return {"version": _VERSION, "fingerprint": _fingerprint(config),
            "queue": [{"alias": alias, "ref": str(ref), "cid": None, "parents": [], "depth": 0,
                       "root": True, "disk": None} for alias, ref in aliases],
            "seen_folders": [], "seen_files": [], "records": [],
            "errors": [], "folder_count": 0, "scanned_roots": 0,
            "hard_truncated": False, "scanned_at": time.time()}


def _report(state: dict, *, has_more: bool) -> dict:
    groups = {}
    for record in state["records"]:
        key = (record["provider"], record["drive_id"], record["alias"],
               record["media_type"], record["title"].casefold())
        group = groups.setdefault(key, {
            "title": record["title"], "media_type": record["media_type"],
            "year": None, "tmdbid": None, "episodes": [], "episode_pairs": None,
            "tasks": 0, "bytes": 0, "poster_path": None,
            "save_dir_alias": record["alias"], "files": [],
            "updated_at": state["scanned_at"], "resolution": None, "release": [],
            "origin_country": [], "genre_ids": [], "origin": "scanned",
            "provider": record["provider"], "file_count": 0, "_rank": 0,
            "_pairs": set(),
        })
        group["file_count"] += 1
        group["bytes"] += record.get("size") or 0
        group["_rank"] = max(group["_rank"], resolution_rank(record["name"]))
        if group["year"] is None:
            group["year"] = record.get("year")
        if group["tmdbid"] is None:
            group["tmdbid"] = record.get("tmdbid")
        if record.get("episode") is not None:
            group["_pairs"].add((record["season"], record["episode"]))
        if len(group["files"]) < 100:
            group["files"].append("/".join([*record["parents"], record["name"]]))
    for group in groups.values():
        pairs = sorted(group.pop("_pairs"))
        group["resolution"] = RESOLUTION_LABELS.get(group.pop("_rank"))
        group["episodes"] = sorted({ep for _, ep in pairs})
        # Always retain actual season identity for safe cross-season dedup.
        group["episode_pairs"] = [list(pair) for pair in pairs] if pairs else None
    result = sorted(groups.values(), key=lambda item: (item["save_dir_alias"], item["title"]))
    hard = bool(state["hard_truncated"] or state["errors"])
    return {
        "groups": result, "total_done": 0,
        "estimated_gb": round(sum(g["bytes"] for g in result) / 1024 ** 3, 2),
        "scanned_at": state["scanned_at"],
        "file_count": len(state["records"]),
        "folder_count": state["folder_count"],
        "scanned_roots": state["scanned_roots"],
        "errors": state["errors"][:30],
        "complete": not has_more and not hard,
        "truncated": hard or has_more,
        "has_more": has_more,
        "limits": {"folders": library_scan.MAX_FOLDERS, "videos": library_scan.MAX_VIDEOS,
                   "depth": library_scan.MAX_DEPTH, "seconds": library_scan.MAX_SECONDS},
    }


def step(config: dict, get_provider: Callable[[str], object],
         cursor: dict | None = None, *, now: Callable[[], float] = time.monotonic) -> tuple[dict, dict | None]:
    """Read the next bounded step. Return (UI snapshot, durable resume cursor).

    Errors preserve existing partial observations and never claim completeness.
    A resource-drive switch invalidates the scan and must be restarted; it must
    not combine different accounts into one inventory.
    """
    if (not isinstance(cursor, dict) or cursor.get("version") != _VERSION
            or cursor.get("fingerprint") != _fingerprint(config)):
        cursor = _start(config)
    state = deepcopy(cursor)
    started = now()
    visited = set(state["seen_folders"])
    known_files = set(state["seen_files"])
    per_run_folders = per_run_videos = 0

    while state["queue"]:
        if (per_run_folders >= library_scan.MAX_FOLDERS
                or per_run_videos >= library_scan.MAX_VIDEOS
                or now() - started >= library_scan.MAX_SECONDS):
            break
        current = state["queue"].pop(0)
        alias, ref = current["alias"], current["ref"]
        try:
            provider_name, root = library_scan._target(alias, ref, config)
            if provider_name not in ("p115", "aliyun"):
                raise ValueError("不支持此网盘")
            if root in ("0", "root"):
                raise ValueError("请在设置中选择具体的影视目录，不能扫描整个网盘根目录")
            cid = current["cid"] if current["cid"] is not None else root
            provider = get_provider(provider_name)
            if not provider.ready():
                raise RuntimeError("网盘未登录")
            disk = str(provider.client.drive_id) if provider_name == "aliyun" else "115"
            if current["disk"] and disk != current["disk"]:
                raise ValueError("资源库 Drive ID 已切换，禁止混合两个网盘的数据")
            physical = f"{provider_name}:{disk}:{cid}"
            if physical in visited:
                if current["root"]:
                    state["scanned_roots"] += 1
                continue
            fs = provider.library()
            # The iterator itself can be paginated by the provider; cap our
            # own consumption and flag a folder we cannot fully enumerate.
            iterator = fs.iterdir(int(cid) if provider_name == "p115" else cid)
            offset = int(current.get("offset") or 0)
            listing = list(itertools.islice(iterator, offset,
                       offset + library_scan.MAX_ITEMS_PER_FOLDER + 1))
        except Exception as exc:  # noqa: BLE001 - continue with other configured roots
            state["errors"].append(f"「{alias}」目录读取失败（{type(exc).__name__}），请检查网盘连接或目录")
            continue

        has_next_page = len(listing) > library_scan.MAX_ITEMS_PER_FOLDER
        usable = listing[:library_scan.MAX_ITEMS_PER_FOLDER]
        # For a directory with >1200 entries, re-list from a saved offset
        # next time, without claiming this folder has been fully enumerated.
        if has_next_page:
            state["queue"].insert(0, {**current,
                                      "offset": offset + library_scan.MAX_ITEMS_PER_FOLDER})
        else:
            visited.add(physical)
            state["seen_folders"].append(physical)
            state["folder_count"] += 1
            if current["root"]:
                state["scanned_roots"] += 1
        per_run_folders += 1
        for item in usable:
            if not isinstance(item, dict):
                continue
            fid, name = str(item.get("id") or ""), str(item.get("name") or "")
            if not fid or not name:
                continue
            if item.get("is_dir") or item.get("is_directory"):
                if current["depth"] >= library_scan.MAX_DEPTH:
                    state["hard_truncated"] = True
                    state["errors"].append(f"「{alias}」目录层级超过最大 {library_scan.MAX_DEPTH} 层")
                else:
                    state["queue"].append({
                        "alias": alias, "ref": ref, "cid": fid,
                        "parents": [*current["parents"], name], "depth": current["depth"] + 1,
                        "root": False, "disk": disk,
                    })
                continue
            if not is_video(name):
                continue
            file_key = f"{provider_name}:{disk}:{fid}"
            if file_key in known_files:
                continue
            known_files.add(file_key)
            state["seen_files"].append(file_key)
            kind = library_scan._media_type(alias, name, tuple(current["parents"]))
            ep_season, episode = episode_of(name)
            season = ep_season or next(
                (n for p in reversed(current["parents"]) if (n := season_of(p)) is not None), 1)
            combined = " ".join([*current["parents"], name])
            tmdb_id = _TMDB_ID.search(combined)
            year = _YEAR.search(combined)
            state["records"].append({
                "provider": provider_name, "drive_id": disk, "alias": alias,
                "parent_id": str(cid), "id": fid, "name": name,
                "parents": list(current["parents"]),
                "title": library_scan._best_title(name, tuple(current["parents"])),
                "media_type": kind, "season": season,
                "episode": episode, "tmdbid": int(tmdb_id.group(1)) if tmdb_id else None,
                "year": int(year.group()) if year else None,
                "size": item.get("size") if type(item.get("size")) is int else None,
                "sha1": item.get("sha1"),
            })
            per_run_videos += 1
    state["scanned_at"] = time.time()
    more = bool(state["queue"])
    return _report(state, has_more=more), state if more else None
