"""Aliyun Drive credentials: QR sign-in, manual refresh token, ping and logout."""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel

from ..core.authaliyun import AliyunAuth
from ..providers.aliyun import AliyunAPIError
from .p115 import _qr_svg


class TokenPayload(BaseModel):
    refresh_token: str


class QRPayload(BaseModel):
    uid: str


# The per-web-process AliyunAuth instance is ephemeral. Transfer workers rotate
# refresh tokens independently, invalidating its in-memory fingerprint even
# though the same account is still working. Reuse a *recent* safe SQLite
# observation instead of presenting "never checked" on every page reload.
# This never grants access or stores a credential: storage operations still
# reauthenticate against Aliyun before doing any I/O.
AUTH_OBSERVATION_TTL = 2 * 3600


def observed_aliyun_status(auth: AliyunAuth, store) -> dict:
    state = auth.status()
    if not state["configured"]:
        return state
    now = time.time()
    checked = state.get("checked_at")
    if (state["state"] != "unknown" and checked is not None
            and 0 <= now - checked < AUTH_OBSERVATION_TTL):
        return state
    observed = store.health_status(stale_after=AUTH_OBSERVATION_TTL).get("aliyun")
    if observed and observed.get("configured") and not observed.get("stale"):
        kind = observed.get("status")
        if kind in ("active", "expired", "error"):
            # Explicit last-observation timestamp; "active" is recent success,
            # not a guarantee the remote token will remain valid indefinitely.
            return {"configured": True, "state": kind,
                    "error": observed.get("error") or None,
                    "checked_at": observed["updated_at"],
                    "drive_id": state.get("drive_id") if checked else None,
                    "last_observation": True}
    # An old in-memory success is not eternal when the probe has gone stale.
    return {"configured": True, "state": "unknown", "error": None,
            "checked_at": checked, "drive_id": state.get("drive_id"),
            "last_observation": False}


def router(store) -> APIRouter:
    api = APIRouter()
    auth = AliyunAuth()

    def publish(state: dict) -> None:
        """Persist the last observed Aliyun auth result across web/worker processes.

        A browser page's in-memory AliyunAuth status is not visible to the
        dashboard service or a second web process. No token goes into runtime
        health; a single event-based probe is treated as stale after two hours.
        """
        store.heartbeat(
            "aliyun",
            status=state["state"],
            configured=state["configured"],
            error=state.get("error") or "",
            check_interval=3600,
        )

    @api.get("/api/authaliyun/status")
    async def status():
        # Read-only: no implicit network/token rotation on status polling.
        return observed_aliyun_status(auth, store)

    @api.post("/api/authaliyun/ping")
    async def ping():
        state = await asyncio.to_thread(auth.ping)
        publish(state)
        return state

    @api.put("/api/authaliyun/token")
    async def set_token(payload: TokenPayload):
        try:
            status = await asyncio.to_thread(auth.set_token, payload.refresh_token)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except AliyunAPIError as exc:
            raise HTTPException(status_code=409, detail=f"令牌校验失败（旧凭证已保留）：{exc}") from None
        except Exception:
            raise HTTPException(status_code=502, detail="无法验证阿里云盘令牌，旧凭证未被覆盖") from None
        publish(status)
        return {"ok": True, "status": status}

    @api.delete("/api/authaliyun/token")
    async def clear_token():
        state = await asyncio.to_thread(auth.clear)
        publish(state)
        return {"ok": True, "status": state}

    @api.post("/api/authaliyun/qr")
    async def qr_start():
        try:
            return await asyncio.to_thread(auth.qr_start)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except Exception:
            raise HTTPException(status_code=502, detail="阿里云盘扫码接口暂时不可用，请重试或手动填写令牌") from None

    # QR confirmation changes stored credentials: POST enforces the CSRF/same-origin guard.
    @api.post("/api/authaliyun/qr/status")
    async def qr_status(payload: QRPayload):
        uid = payload.uid
        if not 16 <= len(uid) <= 128:
            raise HTTPException(status_code=422, detail="扫码会话 ID 格式无效")
        try:
            result = await asyncio.to_thread(auth.qr_status, uid)
            if result.get("authenticated"):
                publish(auth.status())
            return result
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except Exception:
            raise HTTPException(status_code=502, detail="查询阿里云盘扫码状态失败，请重新扫码") from None

    @api.get("/api/authaliyun/qr/image")
    async def qr_image(uid: str = Query(min_length=16, max_length=128)):
        try:
            code = auth.qr_code(uid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return Response(content=_qr_svg(code), media_type="image/svg+xml",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @api.delete("/api/authaliyun/qr")
    async def qr_cancel(uid: str = Query(min_length=16, max_length=128)):
        auth.qr_cancel(uid)
        return {"ok": True}

    return api
