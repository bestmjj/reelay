"""Aliyun Drive storage: share copying with a durable, read-only recovery boundary.

Uses the consumer Aliyun Drive JSON API (not the unrelated PDS Open OAuth token).
A refresh token is stored in Reelay's secret store.  Each transfer uses its own
.reelay-<task> folder, and a transfer with an uncertain outcome is NEVER resent.
"""
from __future__ import annotations

import json
import re
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ..core import secret_store
from ..core.contracts import (NeedsManual, PipelineContext, ShareUnavailable,
                              TransferRejected, TransferUncertain)
from ..core.io import run_blocking
from ..core.media import is_video
from ..core.registry import register
from .storage import (ShareStorage, StorageBrowseError, _await_landing,
                      _landing_progress, _verified_files)

API = "https://api.aliyundrive.com"
TOKEN_API = "https://auth.aliyundrive.com/v2/account/token"
_ID = re.compile(r"^(?:root|0|[A-Za-z0-9_-]{1,128})$")
_SHARE_GONE = ("ShareLinkExpired", "ShareLinkNotFound", "ShareLinkInvalid",
               "ShareLinkCancelled", "ShareLinkCanceled", "ShareLinkDisabled",
               "ShareLinkForbidden", "SharePasswordError", "ShareLinkViolation",
               "InvalidSharePwd", "SharePwdError")


def _id(value: str | int) -> str:
    value = str(value)
    if not _ID.fullmatch(value):
        raise ValueError("无效的阿里云盘文件 ID")
    return "root" if value == "0" else value


def _item(data: dict) -> dict:
    name = data.get("name", "")
    digest = data.get("content_hash")
    algorithm = (data.get("content_hash_name") or "sha1").lower()
    return {"id": str(data["file_id"]), "name": str(name),
            "parent_id": str(data.get("parent_file_id") or "root"),
            "is_dir": data.get("type") == "folder",
            "size": data.get("size"), "sha1": digest if algorithm == "sha1" else None}


def _resource_drive_id(payload: dict) -> str:
    """Choose the non-backup (resource/data) drive; never silently use backup.

    Some older accounts only expose default_drive_id. Accept that legacy ID
    only when it is not explicitly identified as the backup drive.
    """
    resource = payload.get("resource_drive_id")
    if isinstance(resource, str) and resource.strip():
        return resource
    default = payload.get("default_drive_id")
    backup = payload.get("backup_drive_id")
    if isinstance(default, str) and default.strip() and (not backup or default != backup):
        return default
    raise ValueError(
        "阿里云盘未返回资源库（数据盘）ID，拒绝自动使用备份盘；请核实账号盘位"
    )


class AliyunAPIError(RuntimeError):
    """Structured upstream error; do not log response bodies containing credentials."""

    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(f"阿里云盘返回 {code}（HTTP {status}）")


def _is_expired_share(exc: AliyunAPIError) -> bool:
    return any(code.lower() in exc.code.lower() for code in _SHARE_GONE)


class AliyunClient:
    """Refresh-token rotation is serialized; read calls may recover after 401.

    Mutating API calls are sent at most once.  Their network errors are ambiguous
    and must be reconciled by the Storage Provider instead of automatically retried.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._access = ""
        self._drive = ""
        self._used_refresh = ""
        self._expires = 0.0

    @staticmethod
    def _send(url: str, payload: dict, headers: dict | None = None) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=body, headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Reelay/1.1",
            **(headers or {}),
        }, method="POST")
        try:
            with urlopen(req, timeout=25) as response:
                status = response.status
                result = json.loads(response.read())
        except HTTPError as exc:
            try:
                result = json.loads(exc.read())
            except (ValueError, UnicodeError):
                result = {}
            code = result.get("code") if isinstance(result, dict) else None
            raise AliyunAPIError(exc.code, str(code or "HTTPError")) from None
        if not isinstance(result, dict):
            raise ValueError("阿里云盘返回了无效 JSON")
        if status >= 400 or result.get("code"):
            raise AliyunAPIError(status, str(result.get("code") or "Unknown"))
        return result

    def _refresh(self, *, force: bool = False) -> None:
        with self._lock:
            saved = secret_store.get("aliyun_refresh_token") or ""
            if not saved:
                raise RuntimeError("尚未配置阿里云盘 Refresh Token")
            if (not force and self._access and self._used_refresh == saved
                    and time.time() < self._expires):
                return
            result = self._send(TOKEN_API, {"grant_type": "refresh_token", "refresh_token": saved})
            token = result.get("access_token")
            if not token:
                raise RuntimeError("阿里云盘令牌刷新缺少 access_token")
            drive = _resource_drive_id(result)
            # Save rotated refresh token before using access_token, so restarts
            # and other processes do not continue using the obsolete credential.
            replacement = result.get("refresh_token") or saved
            if replacement != saved:
                secrets = secret_store.current()
                if secrets is None:
                    raise RuntimeError("无法持久化轮换后的阿里云盘 Refresh Token")
                secrets.update({"aliyun_refresh_token": replacement})
            self._used_refresh = replacement
            self._access = token
            self._drive = str(drive)
            self._expires = time.time() + max(60, int(result.get("expires_in") or 7200) - 120)

    @property
    def drive_id(self) -> str:
        self._refresh()
        return self._drive

    def post(self, path: str, data: dict, *, share_token: str = "",
             auth: bool = True, read: bool = True) -> dict:
        if auth:
            self._refresh()
        headers = {"Authorization": f"Bearer {self._access}"} if auth else {}
        if share_token:
            headers["x-share-token"] = share_token
        try:
            return self._send(API + path, data, headers)
        except AliyunAPIError as exc:
            expired = exc.status == 401 or exc.code in ("AccessTokenInvalid", "AccessTokenExpired", "InvalidAccessToken")
            if auth and not read and expired:
                # Do not resend this mutation; the *next* attempt must reauthenticate.
                with self._lock:
                    self._expires = 0
            # Only read requests may be retried automatically after refreshing.
            if not (auth and read and expired):
                raise
            self._refresh(force=True)
            headers["Authorization"] = f"Bearer {self._access}"
            return self._send(API + path, data, headers)

    def share_token(self, share: str, password: str | None) -> str:
        try:
            data = self.post("/v2/share_link/get_share_token",
                             {"share_id": share, "share_pwd": password or ""}, auth=False)
        except AliyunAPIError as exc:
            if _is_expired_share(exc):
                raise ShareUnavailable(str(exc)) from None
            raise
        token = data.get("share_token")
        if not token:
            raise RuntimeError("阿里云盘未返回 Share Token")
        return str(token)

    def _pages(self, path: str, data: dict, *, share_token: str = "") -> list[dict]:
        output: list[dict] = []
        marker = ""
        seen = set()
        while True:
            result = self.post(path, {**data, "marker": marker, "limit": 100},
                               share_token=share_token, auth=not bool(share_token))
            items = result.get("items")
            if not isinstance(items, list):
                raise ValueError("阿里云盘目录列表格式错误，不能按空目录处理")
            output.extend(items)
            marker = result.get("next_marker") or ""
            if not marker:
                return output
            if marker in seen or len(output) > 10000:
                raise ValueError("阿里云盘分页异常或目录过大，需要人工核对")
            seen.add(marker)

    def children(self, parent: str | int) -> list[dict]:
        return [_item(x) for x in self._pages("/adrive/v3/file/list", {
            "drive_id": self.drive_id, "parent_file_id": _id(parent)})]

    def shared_children(self, share: str, token: str, parent: str = "root") -> list[dict]:
        try:
            return [_item(x) for x in self._pages("/adrive/v2/file/list_by_share", {
                "share_id": share, "parent_file_id": _id(parent)}, share_token=token)]
        except AliyunAPIError as exc:
            if _is_expired_share(exc):
                raise ShareUnavailable(str(exc)) from None
            raise

    def attr(self, file_id: str | int) -> dict:
        return _item(self.post("/v2/file/get", {"drive_id": self.drive_id, "file_id": _id(file_id)}))

    def mkdir(self, parent: str | int, name: str) -> str:
        data = self.post("/v2/file/create", {
            "drive_id": self.drive_id, "parent_file_id": _id(parent),
            "name": name, "type": "folder", "check_name_mode": "refuse"}, read=False)
        if not data.get("file_id"):
            raise RuntimeError("阿里云盘建目录未返回文件 ID，请检查目录")
        return str(data["file_id"])

    def rename(self, file_id: str | int, name: str) -> None:
        self.post("/v3/file/update", {"drive_id": self.drive_id, "file_id": _id(file_id),
                                     "name": name}, read=False)

    def move(self, file_id: str | int, parent: str | int) -> None:
        self.post("/v2/file/move", {"drive_id": self.drive_id, "file_id": _id(file_id),
                                   "to_drive_id": self.drive_id,
                                   "to_parent_file_id": _id(parent)}, read=False)

    def delete(self, file_ids: list[str | int]) -> None:
        for file_id in file_ids:
            self.post("/v2/recyclebin/trash",
                      {"drive_id": self.drive_id, "file_id": _id(file_id)}, read=False)

    def copy(self, share: str, token: str, file_id: str, parent: str) -> dict:
        return self.post("/v2/file/copy", {
            "share_id": share, "file_id": _id(file_id),
            "to_drive_id": self.drive_id, "to_parent_file_id": _id(parent),
            "auto_rename": False}, share_token=token, read=False)


class AliyunDrive:
    """Organizer-compatible library; Aliyun IDs must stay opaque strings."""

    def __init__(self, client: AliyunClient):
        self.client = client

    def iterdir(self, cid):
        return self.client.children(cid)

    def get_attr(self, fid):
        return self.client.attr(fid)

    def mkdir(self, parent, name):
        return self.client.mkdir(parent, name)

    def rename(self, fid, name):
        self.client.rename(fid, name)

    def move(self, fid, cid):
        self.client.move(fid, cid)

    def delete(self, ids):
        self.client.delete(ids)


@register(kind="provider", ptype="storage")
class AliyunStorage(ShareStorage):
    name = "aliyun"
    ui_label = "阿里云盘"
    ui_description = "阿里云盘分享转存：刷新登录、持久化清单及只读恢复核验"

    def __init__(self):
        self.client = AliyunClient()

    def ready(self) -> bool:
        return bool(secret_store.get("aliyun_refresh_token"))

    def library(self) -> AliyunDrive:
        if not self.ready():
            raise RuntimeError("尚未配置阿里云盘 Refresh Token")
        return AliyunDrive(self.client)

    def dir_path(self, cid: str) -> str | None:
        if _id(cid) == "root":
            return "/"
        parts = []
        current = _id(cid)
        for _ in range(32):
            item = self.client.attr(current)
            parts.append(item["name"])
            current = item["parent_id"]
            if current == "root":
                return "/" + "/".join(reversed(parts))
        raise ValueError("阿里云盘目录路径过深")

    def _share_files(self, ctx: PipelineContext) -> list[dict]:
        p = ctx.parsed
        token = self.client.share_token(p.share_code, p.receive_code)
        out = []

        def walk(parent: str, depth: int):
            for item in self.client.shared_children(p.share_code, token, parent):
                if item["is_dir"]:
                    if depth >= 3:
                        raise NeedsManual("阿里云盘分享目录过深，无法核验完整文件")
                    walk(item["id"], depth + 1)
                elif is_video(item["name"]):
                    out.append(item)
                if len(out) > 10000:
                    raise NeedsManual("分享文件过多，需要人工选择")
        walk("root", 0)
        return out

    def inspect_share(self, parsed, *, describe: bool) -> dict:
        from types import SimpleNamespace
        temp = SimpleNamespace(parsed=parsed)
        try:
            files = self._share_files(temp)
        except ShareUnavailable as exc:
            return {"status": "unavailable", "note": str(exc)}
        if not describe:
            return {"status": "available"}
        from .storage import _select_video_items
        try:
            choice = _select_video_items(files, parsed)
        except NeedsManual as exc:
            return {"status": "issue", "videos": len(files), "note": str(exc)}
        except Exception as exc:
            from ..core.contracts import AlreadyStored
            if not isinstance(exc, AlreadyStored):
                raise
            return {"status": "ok", "videos": len(files)}
        return {"status": "ok", "videos": len(files),
                "size": sum(f["size"] for f in files if isinstance(f["size"], int)),
                "season": choice.get("season"), "episodes": choice.get("episodes", []),
                "pairs": choice.get("pairs", [])}

    def _stage(self, ctx: PipelineContext, selection: dict) -> dict:
        files = selection["files"]
        if len({x["name"] for x in files}) != len(files):
            raise NeedsManual("分享中存在重名视频，无法保证转存去重")
        parent = _id(ctx.save_dir_cid)
        name = f".reelay-{ctx.task_id}"
        found = [x for x in self.client.children(parent) if x["name"] == name]
        if found:
            if len(found) != 1 or not found[0]["is_dir"]:
                raise NeedsManual("转存暂存目录名称冲突，请人工核对")
            stage = found[0]["id"]
        else:
            try:
                stage = self.client.mkdir(parent, name)
            except Exception:
                # A create request may have succeeded despite a lost reply;
                # its name is non-renaming, so re-list instead of creating again.
                found = [x for x in self.client.children(parent) if x["name"] == name and x["is_dir"]]
                if len(found) != 1:
                    raise
                stage = found[0]["id"]
        return {"to_cid": parent, "staging_cid": stage, "drive_id": self.client.drive_id, **selection}

    async def prepare(self, ctx: PipelineContext) -> dict:
        if not self.ready():
            raise RuntimeError("阿里云盘未登录：请在设置中填写 Refresh Token")
        if ctx.parsed.raw_fields.get("share_provider") != "aliyun":
            raise NeedsManual("分享链接属于 115，不可用阿里云盘转存")
        listing = await run_blocking(self._share_files, ctx)
        selection = await self.select(ctx, listing)
        return await run_blocking(self._stage, ctx, selection)

    def _assert_manifest_drive(self, manifest: dict) -> None:
        saved = manifest.get("drive_id")
        if not saved:
            # Legacy manifests were produced before a resource-drive selection
            # was persisted. Reading/writing them on another disk is unsafe.
            raise NeedsManual("旧转存清单缺少目标 Drive ID，需人工核对原网盘后恢复")
        if str(saved) != self.client.drive_id:
            raise NeedsManual("任务原目标盘与当前资源库不同，禁止跨盘恢复、重复复制或删除")

    def _actual(self, manifest: dict) -> list[dict]:
        self._assert_manifest_drive(manifest)
        return self.client.children(manifest["staging_cid"])

    def _verify(self, manifest: dict) -> dict:
        landed = _verified_files(manifest["files"], self._actual(manifest))
        return self._receipt(manifest, landed)

    async def reconcile(self, ctx: PipelineContext) -> dict:
        return await run_blocking(self._verify, ctx.ns("storage")["manifest"])

    def _receive(self, ctx: PipelineContext) -> dict:
        manifest = ctx.ns("storage")["manifest"]
        if self._actual(manifest):
            # Existing partial copy or an earlier ambiguous write: read ONLY.
            return self._verify(manifest)
        p = ctx.parsed
        try:
            share_token = self.client.share_token(p.share_code, p.receive_code)
        except ShareUnavailable as exc:
            raise TransferRejected(str(exc), permanent=True) from None
        sent = False
        for item in manifest["files"]:
            try:
                response = self.client.copy(p.share_code, share_token, item["id"], manifest["staging_cid"])
                sent = True
            except AliyunAPIError as exc:
                # A confirmed HTTP rejection, with no previous copies and no
                # files in the stage, means no remote side effect occurred.
                if not sent and exc.status in (400, 401, 403, 404, 409) and not self._actual(manifest):
                    raise TransferRejected(str(exc), permanent=_is_expired_share(exc)) from None
                raise TransferUncertain(f"阿里云盘复制响应不确定：{exc}") from None
            except Exception as exc:
                raise TransferUncertain(f"阿里云盘复制请求结果不确定（{type(exc).__name__}）") from None
            if not response.get("file_id") and not response.get("async_task_id") and not response.get("task_id"):
                raise TransferUncertain("阿里云盘未确认复制结果，必须只读核验")
        return _await_landing(lambda: self._verify(manifest))

    async def save(self, ctx: PipelineContext) -> dict:
        return await run_blocking(self._receive, ctx)

    async def reconcile_partial(self, ctx: PipelineContext) -> dict:
        manifest = ctx.ns("storage")["manifest"]
        def verify():
            actual = self._actual(manifest)
            if len(actual) == len(manifest["files"]):
                return {**self._receipt(manifest, _verified_files(manifest["files"], actual)), "missing": []}
            partial = _landing_progress(manifest["files"], actual)
            if not partial:
                raise TransferUncertain("暂存目录内容与清单不符，不允许按部分文件继续")
            return {**self._receipt(manifest, partial["landed"]), "missing": partial["missing"]}
        return await run_blocking(verify)

    def _remove_stage(self, manifest: dict, *, must_be_empty: bool) -> None:
        self._assert_manifest_drive(manifest)
        parent = manifest["to_cid"]
        stage = str(manifest["staging_cid"])
        found = [x for x in self.client.children(parent) if x["id"] == stage]
        if not found:
            return
        if len(found) != 1 or not found[0]["is_dir"] or not found[0]["name"].startswith(".reelay-"):
            raise NeedsManual("暂存目录已被更改，拒绝删除")
        if must_be_empty and self._actual(manifest):
            return
        self.client.delete([stage])
        if any(x["id"] == stage for x in self.client.children(parent)):
            raise RuntimeError("删除暂存目录后读回仍存在")

    async def discard(self, ctx: PipelineContext) -> None:
        manifest = ctx.ns("storage").get("manifest")
        if manifest:
            await run_blocking(self._remove_stage, manifest, must_be_empty=True)

    async def abandon(self, ctx: PipelineContext) -> None:
        manifest = ctx.ns("storage")["manifest"]
        await run_blocking(self._remove_stage, manifest, must_be_empty=False)

    def browse(self, cid: str, offset: int = 0, limit: int = 100) -> dict:
        """List opaque Aliyun IDs, with complete root-to-folder breadcrumbs."""
        try:
            cid = _id(cid)
            files = self.client.children(cid)
            folders = sorted((x for x in files if x["is_dir"]), key=lambda x: x["name"])
            ancestors = []
            current = cid
            visited = set()
            while current != "root":
                if current in visited or len(ancestors) >= 32:
                    raise StorageBrowseError("invalid_path", "目录层级异常，请核对云盘路径", 502)
                visited.add(current)
                node = self.client.attr(current)
                if not node["is_dir"]:
                    raise StorageBrowseError("not_directory", "目标文件不是目录", 404)
                ancestors.append({"id": node["id"], "name": node["name"]})
                current = node["parent_id"]
            path = [{"id": "0", "name": "根目录"}, *reversed(ancestors)]
            items = [{"id": x["id"], "name": x["name"], "pid": "0" if cid == "root" else cid}
                     for x in folders[offset:offset + limit]]
            next_offset = offset + limit if offset + limit < len(folders) else None
            return {"items": items, "cid": "0" if cid == "root" else cid,
                    "path": path, "next_offset": next_offset,
                    "total_entries": len(folders), "via": "aliyun"}
        except StorageBrowseError:
            raise
        except AliyunAPIError as exc:
            status = 409 if exc.status == 401 else 503
            raise StorageBrowseError("aliyun_unavailable", str(exc), status) from None
