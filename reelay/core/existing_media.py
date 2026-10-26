"""Conservative, live-readback checks for episodes already in cloud storage.

Scan observations are never promoted to completed Reelay transfers. They can
prevent a redundant copy only when title/season/episode and a strong work ID
match AND the actual remote file still exists at the indexed location.
"""
from __future__ import annotations

from . import tmdb, works

MAX_CHECKS = 12


def record_key(record: dict) -> str:
    """Immutable cloud file identity. The same file ID in 115 and Aliyun differs."""
    return ":".join(str(record.get(k) or "") for k in ("provider", "drive_id", "id"))


def same_observed_file(before: dict, current: dict) -> bool:
    """Reject stale identity bindings after renames/moves, metadata or cloud changes."""
    keys = ("provider", "drive_id", "id", "parent_id", "name", "size", "sha1")
    return all(before.get(k) == current.get(k) for k in keys)


def apply_associations(records: list[dict], confirmed: dict) -> list[dict]:
    """Overlay user-confirmed work identity without modifying scan or transfer ledgers.

    Association is an explicit mapping from an observed *physical file* to a
    TMDB show. A changed physical identity invalidates the mapping automatically;
    the final copy-skip gate additionally reads the live file from the cloud.
    """
    result = []
    for record in records:
        bound = confirmed.get(record_key(record))
        if isinstance(bound, dict) and same_observed_file(bound, record):
            result.append({**record, "tmdbid": bound["tmdbid"],
                           "title": bound["title"], "year": bound.get("year")})
        else:
            result.append(record)
    return result


def candidates_for_subscription(records: list[dict], sub: dict, *,
                                limit: int = 50) -> list[dict]:
    """Conservative show/season candidates that a human may explicitly adopt."""
    wanted = tmdb.normalize(sub.get("title") or "")
    if not wanted:
        return []
    found = []
    for record in records:
        if (record.get("season") != sub.get("season")
                or record.get("media_type") not in ("tv", "anime")
                or type(record.get("episode")) is not int):
            continue
        rid, sid = record.get("tmdbid"), sub.get("tmdbid")
        if rid and sid and int(rid) != int(sid):
            continue
        if rid and sid and int(rid) == int(sid):
            matched = True
        else:
            matched = tmdb.normalize(record.get("title") or "") == wanted
        if matched:
            found.append(record)
        if len(found) >= limit:
            break
    return found


def candidates(records: list[dict], parsed, tmdb_meta: dict | None,
               *, provider: str, drive_id: str) -> tuple[list[dict], list[dict]]:
    """(strong identity matches, ambiguous matches) for explicitly named episodes."""
    if not parsed or not parsed.title or not parsed.episodes or parsed.media_type not in ("tv", "anime"):
        return [], []
    work = works.of(parsed, tmdb_meta)
    wanted = {(parsed.season or 1, ep) for ep in parsed.episodes}
    strong, ambiguous = [], []
    for record in records:
        if (record.get("provider") != provider or record.get("drive_id") != drive_id
                or (record.get("season"), record.get("episode")) not in wanted
                or record.get("media_type") not in ("tv", "anime")):
            continue
        name = tmdb.normalize(record.get("title") or "")
        observed_id = record.get("tmdbid")
        trusted_id = work.get("tmdb")
        # A TMDB ID in a scanned folder is stronger than a filename; renamed
        # releases may not include the canonical show title at all.
        if observed_id and trusted_id:
            if trusted_id != f"tv:{observed_id}":
                continue
            if record.get("year") and work.get("year") and record["year"] != work["year"]:
                ambiguous.append(record)
            else:
                strong.append(record)
        elif name and name == work["title"]:
            if record.get("year") and work.get("year") and record["year"] != work["year"]:
                continue
            if record.get("year") and work.get("year"):
                strong.append(record)
            else:
                ambiguous.append(record)
    return strong, ambiguous


def observed_for_subscription(records: list[dict], subscription: dict) -> list[int]:
    """Possible episodes visible in cloud inventory, not verified transfers.

    Used ONLY for an informative UI counter. A missing or stale cloud file
    may still exist in this snapshot, so the transfer pipeline always does
    the authoritative remote-ID readback before skipping a copy.
    """
    title = tmdb.normalize(subscription.get("title") or "")
    tmdb_id = subscription.get("tmdbid")
    season = subscription.get("season")
    episodes = set()
    for record in records:
        if record.get("media_type") not in ("tv", "anime") or record.get("season") != season:
            continue
        episode = record.get("episode")
        if type(episode) is not int or episode <= 0:
            continue
        observed_id = record.get("tmdbid")
        if observed_id and tmdb_id:
            same = int(observed_id) == int(tmdb_id)
        else:
            same = bool(title) and tmdb.normalize(record.get("title") or "") == title
        if same:
            episodes.add(episode)
    return sorted(episodes)


def verify_record(fs, record: dict) -> bool:
    """Check the EXACT cloud ID and parent before avoiding a transfer."""
    current = fs.get_attr(record["id"])
    if (str(current.get("id")) != str(record["id"])
            or str(current.get("parent_id")) != str(record["parent_id"])
            or current.get("name") != record["name"]
            or bool(current.get("is_dir"))):
        return False
    if record.get("size") is not None and current.get("size") != record["size"]:
        return False
    if record.get("sha1") and str(current.get("sha1") or "").upper() != str(record["sha1"]).upper():
        return False
    return True


def check(storage, records: list[dict], parsed, tmdb_meta: dict | None,
          *, provider: str) -> dict:
    """Conservative pre-copy gate; all checks read-only, no cloud mutations."""
    drive_id = str(storage.client.drive_id) if provider == "aliyun" else "115"
    strong, ambiguous = candidates(records, parsed, tmdb_meta, provider=provider, drive_id=drive_id)
    if len(strong) + len(ambiguous) > MAX_CHECKS:
        # The previous message incorrectly called a 100-episode season pack
        # "too many versions of the same episode". Keep the live-read budget,
        # but accurately distinguish broad ranges from true collisions.
        pairs = {(row.get("season"), row.get("episode")) for row in [*strong, *ambiguous]}
        if len(pairs) > MAX_CHECKS:
            message = (f"本次涉及 {len(pairs)} 个不同的历史集数候选，超过单次 "
                       f"{MAX_CHECKS} 个文件的核验上限；请缩小到明确的目标单集，不会认定这些集已入库")
        else:
            message = (f"目标集数存在 {len(strong) + len(ambiguous)} 个历史文件或重复版本，"
                       "超过实时核验上限；请核对媒体库或缩小选集范围")
        return {"covered": [], "uncertain": message}
    covered = set()
    if strong:
        fs = storage.library()
        for record in strong:
            try:
                if not verify_record(fs, record):
                    return {"covered": [], "uncertain": "索引中的已有文件已移动或改名，请重新扫描媒体库后重试"}
            except Exception:  # noqa: BLE001 - no silent copy when cloud readback fails
                return {"covered": [], "uncertain": "无法实时核验网盘已有文件，禁止可能重复的转存"}
            covered.add((record["season"], record["episode"]))
    uncertain = ""
    if ambiguous:
        uncertain = "找到同名同集的已有文件，但缺少可信 TMDB ID 或年份；请先核对媒体库，避免重复转存"
    return {"covered": [list(x) for x in sorted(covered)], "uncertain": uncertain}
