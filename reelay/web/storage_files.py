"""Explicitly scoped cloud file manager: list, create folder, rename only.

No remove, move, upload or copy endpoint is exposed here. Existing storage
providers own the actual mutations; the UI must send the currently selected
provider and (for Aliyun) the observed resource drive identity. Every write
checks the current folder and refuses collisions or stale targets first.
"""
from __future__ import annotations

import asyncio
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..providers.storage import StorageBrowseError

Cloud = Literal["p115", "aliyun"]
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_INVALID_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_WINDOWS_NAMES = re.compile(r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$")
MAX_CHECK_ENTRIES = 1000


class FolderCreate(BaseModel):
    provider: Cloud
    cid: str = Field(min_length=1, max_length=128)
    drive_id: str | None = None
    name: str = Field(min_length=1, max_length=255)


class RenameEntry(FolderCreate):
    fid: str = Field(min_length=1, max_length=128)
    old_name: str = Field(min_length=1, max_length=255)
    is_dir: bool


class MoveFile(FolderCreate):
    """Only move an existing ordinary file between directories in ONE cloud."""
    fid: str = Field(min_length=1, max_length=128)
    destination_cid: str = Field(min_length=1, max_length=128)


def _validate_id(provider: Cloud, value: str, *, allow_root: bool = True) -> str:
    if not _ID.fullmatch(value) or (provider == "p115" and not value.isdecimal()):
        raise HTTPException(status_code=422, detail="无效的网盘文件或目录 ID")
    if not allow_root and value in ("0", "root"):
        raise HTTPException(status_code=422, detail="网盘根目录不能重命名")
    if provider == "p115" and value == "root":
        raise HTTPException(status_code=422, detail="115 根目录编号必须是 0")
    return value


def _validate_name(value: str) -> str:
    # Validate rather than modify the requested name: silently cleaning a name
    # can rename a different file than the operator intended.
    if (not value or value != value.strip() or value in (".", "..")
            or value.endswith(".") or _INVALID_NAME.search(value)
            or _WINDOWS_NAMES.fullmatch(value) or len(value.encode("utf-8")) > 255):
        raise HTTPException(status_code=422, detail="名称不能留空、包含路径或特殊符号，也不能以空格/句点结尾")
    if value.casefold().startswith(".reelay-"):
        raise HTTPException(status_code=409, detail="不能创建或重命名为 Reelay 内部暂存目录")
    return value


def router(service) -> APIRouter:
    api = APIRouter()
    write_lock = asyncio.Lock()

    def provider_for(name: Cloud):
        return service.pipeline.reg.get_provider("storage", name)

    def browse(storage, cid: str, *, offset: int = 0, limit: int = 100):
        try:
            return storage.browse(cid, offset, limit, include_files=True)
        except StorageBrowseError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None

    def require_scope(storage, name: Cloud, expected_drive: str | None) -> None:
        if name == "aliyun":
            # A saved refresh token alone is not enough: match the physical
            # resource drive seen by the browser before accepting any write.
            if not expected_drive or str(storage.client.drive_id) != expected_drive:
                raise HTTPException(status_code=409, detail="阿里云盘账号或资源库已变化，请刷新后再修改")

    def before_write(storage, request: FolderCreate) -> tuple[object, list[dict]]:
        _validate_id(request.provider, request.cid)
        require_scope(storage, request.provider, request.drive_id)
        listing = browse(storage, request.cid, limit=MAX_CHECK_ENTRIES)
        if listing.get("next_offset") is not None or listing.get("total_entries", 0) > MAX_CHECK_ENTRIES:
            raise HTTPException(status_code=409, detail="当前目录文件过多，为避免误改，请先整理到较小的子目录")
        if request.provider == "aliyun" and str(listing.get("drive_id")) != request.drive_id:
            raise HTTPException(status_code=409, detail="阿里云盘资源库在查询期间发生变化，请刷新")
        return storage.library(), listing["items"]

    def create(storage, body: FolderCreate):
        name = _validate_name(body.name)
        fs, items = before_write(storage, body)
        if any(str(item["name"]).casefold() == name.casefold() for item in items):
            raise HTTPException(status_code=409, detail="同名文件或文件夹已存在，请刷新目录")
        require_scope(storage, body.provider, body.drive_id)
        parent = int(body.cid) if body.provider == "p115" else body.cid
        # Exactly one mutation call; an uncertain outcome must never be
        # retried automatically by the server.
        identifier = fs.mkdir(parent, name)
        return {"ok": True, "id": str(identifier), "name": name}

    def rename(storage, body: RenameEntry):
        name = _validate_name(body.name)
        _validate_id(body.provider, body.fid, allow_root=False)
        fs, items = before_write(storage, body)
        if body.old_name.casefold().startswith(".reelay-"):
            raise HTTPException(status_code=409, detail="Reelay 内部暂存目录禁止手动重命名")
        fid = int(body.fid) if body.provider == "p115" else body.fid
        actual = fs.get_attr(fid)
        expected_parent = str(int(body.cid)) if body.provider == "p115" else (
            "root" if body.cid == "0" else body.cid)
        if (str(actual.get("id")) != body.fid or str(actual.get("parent_id")) != expected_parent
                or str(actual.get("name")) != body.old_name or bool(actual.get("is_dir")) != body.is_dir):
            raise HTTPException(status_code=409, detail="目标文件已发生变化，请刷新目录后重试")
        if name == body.old_name:
            return {"ok": True, "id": body.fid, "name": name, "unchanged": True}
        if any(item["id"] != body.fid and str(item["name"]).casefold() == name.casefold()
               for item in items):
            raise HTTPException(status_code=409, detail="同名文件或文件夹已存在，请更换名称")
        require_scope(storage, body.provider, body.drive_id)
        fs.rename(fid, name)
        return {"ok": True, "id": body.fid, "name": name}

    def move_file(storage, body: MoveFile):
        _validate_id(body.provider, body.fid, allow_root=False)
        _validate_id(body.provider, body.destination_cid, allow_root=False)
        if body.cid == body.destination_cid:
            raise HTTPException(status_code=422, detail="源目录和目标目录相同，无需移动")
        name = _validate_name(body.name)
        fs, items = before_write(storage, body)
        entry = next((x for x in items if str(x["id"]) == body.fid), None)
        if not entry or entry.get("is_dir") or str(entry.get("name")) != name:
            raise HTTPException(status_code=409, detail="源文件已发生变化或目标是目录，请刷新")
        fid = int(body.fid) if body.provider == "p115" else body.fid
        original = fs.get_attr(fid)
        expected_parent = str(int(body.cid)) if body.provider == "p115" else (
            "root" if body.cid == "0" else body.cid)
        if (str(original.get("id")) != body.fid
                or str(original.get("parent_id")) != expected_parent
                or original.get("name") != name or original.get("is_dir")):
            raise HTTPException(status_code=409, detail="文件属性或位置已变化，请刷新")
        # Never move Reelay's in-flight staging files.
        if name.casefold().startswith(".reelay-"):
            raise HTTPException(status_code=409, detail="禁止移动 Reelay 暂存文件")
        dest = browse(storage, body.destination_cid, limit=MAX_CHECK_ENTRIES)
        if dest.get("next_offset") is not None or dest.get("total_entries", 0) > MAX_CHECK_ENTRIES:
            raise HTTPException(status_code=409, detail="目标目录文件太多，无法安全检查同名冲突")
        if (body.provider == "aliyun" and str(dest.get("drive_id")) != body.drive_id):
            raise HTTPException(status_code=409, detail="目标资源库已变化，请刷新")
        destination = int(body.destination_cid) if body.provider == "p115" else body.destination_cid
        dst = fs.get_attr(destination)
        if str(dst.get("id")) != body.destination_cid or not dst.get("is_dir"):
            raise HTTPException(status_code=409, detail="目标不是有效文件夹")
        if str(dst.get("name") or "").casefold().startswith(".reelay-"):
            raise HTTPException(status_code=409, detail="不能把旧文件移动到 Reelay 暂存目录")
        if any(str(item.get("name") or "").casefold() == name.casefold() for item in dest["items"]):
            raise HTTPException(status_code=409, detail="目标目录已有同名文件，已拒绝覆盖")
        require_scope(storage, body.provider, body.drive_id)
        # One mutation, no automatic retry if upstream response is uncertain.
        fs.move(fid, destination)
        after = fs.get_attr(fid)
        if str(after.get("parent_id")) != ("root" if body.destination_cid == "0" and body.provider == "aliyun" else body.destination_cid):
            raise RuntimeError("移动结果尚未得到确认，请刷新源与目标目录")
        return {"ok": True, "id": body.fid, "destination_cid": body.destination_cid, "name": name}

    @api.get("/api/storage/items")
    async def list_items(provider: Cloud, cid: str = Query(default="0", min_length=1, max_length=128),
                         offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=200)):
        """Files and folders, paginated, in the *explicitly selected* cloud."""
        _validate_id(provider, cid)
        try:
            return await asyncio.to_thread(browse, provider_for(provider), cid, offset=offset, limit=limit)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=502, detail="读取网盘目录失败，请检查登录或稍后重试") from None

    @api.post("/api/storage/folders")
    async def create_folder(body: FolderCreate):
        if write_lock.locked():
            raise HTTPException(status_code=409, detail="已有网盘操作正在执行，请稍后刷新")
        async with write_lock:
            try:
                return await asyncio.to_thread(create, provider_for(body.provider), body)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(
                    status_code=502,
                    detail="新建目录的结果不确定，请先刷新确认是否已存在；不要立即重复提交",
                ) from None

    @api.patch("/api/storage/names")
    async def rename_entry(body: RenameEntry):
        if write_lock.locked():
            raise HTTPException(status_code=409, detail="已有网盘操作正在执行，请稍后刷新")
        async with write_lock:
            try:
                return await asyncio.to_thread(rename, provider_for(body.provider), body)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(
                    status_code=502,
                    detail="重命名的结果不确定，请先刷新核对文件名；不要立即重复提交",
                ) from None

    @api.post("/api/storage/move")
    async def move_file_entry(body: MoveFile):
        if write_lock.locked():
            raise HTTPException(status_code=409, detail="已有网盘操作正在执行")
        async with write_lock:
            try:
                return await asyncio.to_thread(move_file, provider_for(body.provider), body)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(
                    status_code=502,
                    detail="移动结果无法确认，请先刷新两个目录核对；不要立即重复提交",
                ) from None

    return api
