"""Read-only provenance review for a transferred media task.

Cloud readback proves a file was copied, but cannot prove that the source
actually belongs to the claimed TMDB work or episode. Never present this
report as a visual/content verification or use file size to infer identity.
"""
from __future__ import annotations

from .media import episode_of


def source_audit(ctx) -> dict:
    storage = ctx.meta.get("storage") or {}
    manifest = storage.get("manifest") or {}
    files = manifest.get("files") or []
    suspect = False
    entries = []
    for file in files:
        name = str(file.get("name") or "")
        season, episode = episode_of(name)
        claimed = file.get("episode")
        source_path = "/".join(filter(None, (str(file.get("share_path") or ""), name)))
        reason = None
        if type(claimed) is int and episode is None:
            reason = "原始分享文件名没有可核验的集号，可能被错误地映射到目标集"
        elif type(claimed) is int and episode != claimed:
            reason = f"原始分享文件名标识 E{episode}，但清单将其当作 E{claimed}"
        elif type(claimed) is int and season is not None and season != file.get("season"):
            reason = "原始分享文件名的季号与入库清单不一致"
        if reason:
            suspect = True
        entries.append({
            "file_id": str(file.get("id") or ""),
            "source_name": name, "source_path": source_path,
            "size": file.get("size"), "share_folder_id": file.get("share_folder_id"),
            "season": file.get("season"), "episode": claimed,
            "filename_episode": episode, "issue": reason,
        })
    return {
        "available": bool(files),
        "suspect": suspect,
        "entries": entries,
        "content_verified": False,  # no video decoding, fingerprint, or TMDB comparison
        "note": ("转存成功仅证明文件到达网盘，不证明文件内容属于当前作品。"
                 "如原始文件名无集号、大小异常或画面不符，请标记错片并人工核对。"),
    }
