"""One media-library view from two independent, read-only evidence sources.

The scanned inventory and Reelay's verified transfer ledger stay independent.
Never write inventory observations into the transfer/dedup tables. Merge cards
only if the content identity has sufficiently strong evidence.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath


def _normal(title: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", title).casefold())


def _names(group: dict) -> set[str]:
    return {unicodedata.normalize("NFKC", PurePosixPath(path).name).casefold()
            for path in group.get("files") or [] if isinstance(path, str) and path}


def _pairs(group: dict) -> set[tuple[int, int]]:
    return {(int(season), int(episode)) for season, episode in (group.get("episode_pairs") or [])
            if season is not None and episode is not None}


def _same_content(transferred: dict, scanned: dict) -> bool:
    """Match by title + folder + media type, then require independent evidence.

    Matching only by title can collapse remakes or different releases into one
    entry. With no evidence, keep both visible rather than silently discard a
    real file or claim a transfer verified something it has not.
    """
    if transferred.get("save_dir_alias") != scanned.get("save_dir_alias"):
        return False
    if not transferred.get("title") or not scanned.get("title"):
        return False
    if _normal(transferred["title"]) != _normal(scanned["title"]):
        return False
    kind_a, kind_b = transferred.get("media_type"), scanned.get("media_type")
    if kind_a and kind_b and kind_a != kind_b:
        return False
    year_a, year_b = transferred.get("year"), scanned.get("year")
    if year_a and year_b and int(year_a) != int(year_b):
        return False
    # Matching actual filenames is the strongest signal when media metadata
    # lacks year/TMDB; the scan only knows filenames, not authoritative TMDB IDs.
    names_a, names_b = _names(transferred), _names(scanned)
    if names_a and names_b and names_a.intersection(names_b):
        return True
    if year_a and year_b and year_a == year_b:
        return True
    # Same show and same numbered episodes is useful independent evidence.
    if kind_a in ("tv", "anime"):
        if _pairs(transferred).intersection(_pairs(scanned)):
            return True
        if transferred.get("episodes") and scanned.get("episodes"):
            return bool(set(transferred["episodes"]) & set(scanned["episodes"]))
    return False


def _combine(transferred: dict, scanned: dict) -> dict:
    """Prefer scanned sizes/files (physical evidence); trusted transfer metadata.

    A scanned file is not a verified Reelay transfer. Retain task count solely
    from the transfer ledger and never add sizes for potentially same files.
    """
    files = list(dict.fromkeys([*(scanned.get("files") or []), *(transferred.get("files") or [])]))[:100]
    scanned_pairs = _pairs(scanned)
    transferred_pairs = _pairs(transferred)
    all_pairs = sorted(scanned_pairs | transferred_pairs)
    scanned_eps = scanned.get("episodes") or []
    transferred_eps = transferred.get("episodes") or []
    eps = sorted(set(scanned_eps) | set(transferred_eps) | {ep for _, ep in all_pairs})
    return {
        **scanned,
        "title": transferred.get("title") or scanned["title"],
        "media_type": transferred.get("media_type") or scanned.get("media_type"),
        "tmdbid": transferred.get("tmdbid") or scanned.get("tmdbid"),
        "year": transferred.get("year") or scanned.get("year"),
        "poster_path": transferred.get("poster_path") or scanned.get("poster_path"),
        "resolution": transferred.get("resolution") or scanned.get("resolution"),
        "release": list(dict.fromkeys([*(transferred.get("release") or []), *(scanned.get("release") or [])])),
        "origin_country": transferred.get("origin_country") or scanned.get("origin_country") or [],
        "genre_ids": transferred.get("genre_ids") or scanned.get("genre_ids") or [],
        "bytes": max(int(scanned.get("bytes") or 0), int(transferred.get("bytes") or 0)),
        "file_count": max(int(scanned.get("file_count") or 0), int(transferred.get("file_count") or 0)),
        "tasks": transferred.get("tasks", 0),
        "episodes": eps,
        "episode_pairs": [list(pair) for pair in all_pairs] if len({x[0] for x in all_pairs}) > 1 else None,
        "files": files,
        "updated_at": max(transferred.get("updated_at") or 0, scanned.get("updated_at") or 0),
        "origin": "combined",
    }


def join_library(transfers: list[dict], inventory: list[dict]) -> list[dict]:
    """Unified, safe-to-render view; no DB writes and no side effects."""
    remaining = [dict(group) for group in inventory]
    result = []
    for group in transfers:
        transfer = {**group, "origin": "verified"}
        matching = next((i for i, found in enumerate(remaining) if _same_content(transfer, found)), None)
        if matching is None:
            result.append(transfer)
        else:
            scanned = remaining.pop(matching)
            result.append(_combine(transfer, scanned))
    result.extend({**group, "origin": "scanned"} for group in remaining)
    return sorted(result, key=lambda x: x.get("updated_at") or 0, reverse=True)
