"""Aliyun Drive login for the management UI.

The QR workflow uses Aliyun Drive's web passport endpoint. Sessions (including
the QR code payload and scan parameters) stay server-side, are short-lived, and
never expose the refresh token to the browser. Credentials are validated *before*
replacing the existing one and are stored via SecretStore with permissions 0600.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import secret_store
from ..providers.aliyun import AliyunAPIError, AliyunClient, TOKEN_API

PASSPORT = "https://passport.aliyundrive.com/newlogin/qrcode/"
QR_TTL = 300
MAX_SESSIONS = 8
_PARAMS = {"appName": "aliyun_drive", "fromSite": "52", "appEntrance": "web",
           "isMobile": "false", "lang": "zh_CN", "returnUrl": "", "bizParams": ""}


def _passport_request(path: str, params: dict[str, str], *, post: bool = False) -> dict:
    """Only fixed upstream URLs are used. No user-controlled URL or response body is logged."""
    data = urlencode(params).encode("utf-8") if post else None
    query = {"appName": "aliyun_drive", "fromSite": "52"} if post else params
    url = PASSPORT + path + "?" + urlencode(query)
    request = Request(url, data=data, headers={
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 Reelay",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }, method="POST" if post else "GET")
    with urlopen(request, timeout=20) as response:
        result = json.loads(response.read(65537))
    if not isinstance(result, dict) or not isinstance(result.get("content"), dict):
        raise ValueError("阿里云盘扫码接口响应无效")
    content = result["content"]
    if not isinstance(content.get("data"), dict):
        raise ValueError("阿里云盘扫码接口未返回数据")
    return content["data"]


def _refresh_from_biz_ext(encoded: str) -> str:
    """Decode passport's base64 GB18030/UTF-8 JSON without revealing other account fields."""
    if not isinstance(encoded, str) or len(encoded) > 65536:
        raise ValueError("扫码确认数据不完整")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > 65536:
            raise ValueError("扫码数据过大")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            payload = json.loads(raw.decode("gb18030"))
        token = payload["pds_login_result"]["refreshToken"]
    except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("扫码确认没有返回有效的阿里云盘登录凭证") from exc
    if not isinstance(token, str) or len(token.strip()) < 16:
        raise ValueError("扫码确认没有返回有效的阿里云盘登录凭证")
    return token.strip()


class AliyunAuth:
    """Short-lived QR sessions and authenticated credential status.

    A failed replacement never destroys a previously working refresh token.
    Refresh rotation persists the *returned* token before reporting success.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = {}
        self._fingerprint = ""
        self._state = "unknown"
        self._error = ""
        self._checked_at: float | None = None
        self._drive_id: str | None = None

    def status(self) -> dict:
        with self._lock:
            token = secret_store.get("aliyun_refresh_token")
            if not token:
                return {"configured": False, "state": "not_configured",
                        "error": None, "checked_at": None, "drive_id": None}
            matched = self._fingerprint == hashlib.sha256(token.encode()).hexdigest()
            return {"configured": True, "state": self._state if matched else "unknown",
                    "error": (self._error or None) if matched else None,
                    "checked_at": self._checked_at if matched else None,
                    "drive_id": self._drive_id if matched else None}

    def _exchange(self, token: str) -> tuple[str, str]:
        result = AliyunClient._send(TOKEN_API, {
            "grant_type": "refresh_token", "refresh_token": token})
        rotated = result.get("refresh_token") or token
        drive = AliyunClient().resource_drive(result)
        if not isinstance(result.get("access_token"), str) or not result["access_token"]:
            raise ValueError("阿里云盘没有返回 Access Token")
        if not isinstance(rotated, str) or len(rotated) < 16 or not isinstance(drive, str) or not drive:
            raise ValueError("阿里云盘未返回完整的有效凭证")
        return rotated, drive

    def set_token(self, proposed: str) -> dict:
        token = (proposed or "").strip()
        if len(token) < 16 or len(token) > 8192 or any(ch.isspace() for ch in token):
            raise ValueError("Refresh Token 格式无效，请粘贴完整令牌")
        with self._lock:
            # The request can rotate the supplied token: save its replacement,
            # never the spent token, and never overwrite an old token on failure.
            verified, drive = self._exchange(token)
            store = secret_store.current()
            if store is None:
                raise RuntimeError("密钥存储未初始化")
            store.update({"aliyun_refresh_token": verified})
            self._fingerprint = hashlib.sha256(verified.encode()).hexdigest()
            self._state, self._error = "active", ""
            self._checked_at, self._drive_id = time.time(), drive
            return self.status()

    def ping(self) -> dict:
        with self._lock:
            token = secret_store.get("aliyun_refresh_token")
            if not token:
                return self.status()
            try:
                return self.set_token(token)
            except Exception as exc:
                failure = exc
                # Worker and Web use the same atomic secret file but separate
                # processes. Another request may have *successfully rotated*
                # the refresh token while this probe was in flight. If so,
                # retry the replacement ONCE before declaring it expired.
                latest = secret_store.get("aliyun_refresh_token")
                if latest and latest != token:
                    try:
                        return self.set_token(latest)
                    except Exception as newer:
                        token, failure = latest, newer
                self._fingerprint = hashlib.sha256(token.encode()).hexdigest()
                if isinstance(failure, AliyunAPIError) and (failure.status in (401, 403)
                        or any(part in failure.code.lower() for part in ("invalid", "expired", "token"))):
                    self._state = "expired"
                else:
                    self._state = "error"
                self._error = str(failure)[:180]
                self._checked_at = time.time()
                return self.status()

    def clear(self) -> dict:
        with self._lock:
            store = secret_store.current()
            if store is None:
                raise RuntimeError("密钥存储未初始化")
            store.update({"aliyun_refresh_token": None})
            self._fingerprint = ""
            self._state, self._error = "unknown", ""
            self._checked_at, self._drive_id = None, None
            return self.status()

    def qr_start(self) -> dict:
        with self._lock:
            now = time.time()
            self._sessions = {k: v for k, v in self._sessions.items() if v["expires_at"] > now}
            if len(self._sessions) >= MAX_SESSIONS:
                raise ValueError("已有多个扫码会话，请结束旧会话后重试")
            data = _passport_request("generate.do", _PARAMS)
            t, ck, content = str(data.get("t") or ""), data.get("ck"), data.get("codeContent")
            if not t.isdecimal() or not isinstance(ck, str) or not 0 < len(ck) <= 256 \
                    or not isinstance(content, str) or not 0 < len(content) <= 4096:
                raise ValueError("阿里云盘未返回有效的扫码二维码")
            uid = secrets.token_urlsafe(24)
            expires_at = now + QR_TTL
            self._sessions[uid] = {"t": t, "ck": ck, "code": content,
                                   "expires_at": expires_at, "status": "NEW",
                                   "authenticated": False}
            return {"uid": uid, "expires_at": expires_at}

    def qr_code(self, uid: str) -> str:
        with self._lock:
            session = self._session(uid)
            if session["authenticated"]:
                raise KeyError("扫码会话已完成")
            return session["code"]

    def _session(self, uid: str) -> dict:
        session = self._sessions.get(uid)
        if session is None:
            raise KeyError("扫码会话不存在，请重新生成二维码")
        if time.time() >= session["expires_at"]:
            self._sessions.pop(uid, None)
            raise KeyError("二维码已过期，请重新扫码")
        return session

    def qr_status(self, uid: str) -> dict:
        with self._lock:
            session = self._session(uid)
            if session["authenticated"]:
                return {"status": "CONFIRMED", "message": "阿里云盘登录成功",
                        "authenticated": True}
            if session["status"] in ("CANCELED", "EXPIRED", "ERROR"):
                return {"status": session["status"], "message": "二维码已失效，请重新扫码",
                        "authenticated": False}
            response = _passport_request("query.do", {
                **_PARAMS, "t": session["t"], "ck": session["ck"],
                "navlanguage": "zh-CN", "navPlatform": "MacIntel",
            }, post=True)
            status = str(response.get("qrCodeStatus") or "").upper()
            session["status"] = status
            if status == "CONFIRMED":
                try:
                    token = _refresh_from_biz_ext(response.get("bizExt") or "")
                    self.set_token(token)
                except Exception as exc:
                    session["status"] = "ERROR"
                    # The verified old token remains untouched.
                    raise ValueError(f"扫码已确认，但凭证验证失败，旧登录信息未被覆盖：{exc}") from None
                # Drop the QR content and one-time login parameters after use.
                session["code"] = session["ck"] = session["t"] = ""
                session["authenticated"] = True
                return {"status": "CONFIRMED", "message": "阿里云盘登录成功",
                        "authenticated": True}
            messages = {
                "NEW": "请使用阿里云盘 App 扫描二维码",
                "SCANED": "已扫码，请在手机上确认",
                "EXPIRED": "二维码已过期，请重新生成",
                "CANCELED": "已取消，请重新生成",
            }
            if status not in messages:
                session["status"] = "ERROR"
                raise ValueError("阿里云盘返回了无法识别的扫码状态")
            return {"status": status, "message": messages[status], "authenticated": False}

    def qr_cancel(self, uid: str) -> None:
        with self._lock:
            self._sessions.pop(uid, None)
