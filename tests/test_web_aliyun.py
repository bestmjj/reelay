"""Aliyun Drive login HTTP API, including auth guard and QR image."""
from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import patch

from reelay.core import secret_store
from reelay.providers.aliyun import AliyunAPIError
from tests.fakes import WebCase


class AliyunWebTest(WebCase):
    def test_status_manual_replace_failure_and_clear(self):
        status = self.client.get("/api/authaliyun/status")
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()["configured"])
        with patch("reelay.core.authaliyun.AliyunAuth._exchange",
                   return_value=("rotated-manual-token-123456", "drive123")):
            response = self.client.put("/api/authaliyun/token", json={
                "refresh_token": "initial-manual-token-123456"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["status"]["configured"])
        self.assertNotIn("rotated-manual-token-123456", response.text)
        with patch("reelay.core.authaliyun.AliyunAuth._exchange",
                   side_effect=AliyunAPIError(401, "InvalidToken")):
            failed = self.client.put("/api/authaliyun/token", json={
                "refresh_token": "bad-refresh-token-123456"})
        self.assertEqual(failed.status_code, 409, failed.text)
        self.assertEqual(secret_store.get("aliyun_refresh_token"), "rotated-manual-token-123456")
        response = self.client.delete("/api/authaliyun/token")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["status"]["configured"])

    def test_qr_image_scan_confirm_and_replay(self):
        code = base64.b64encode(json.dumps({
            "pds_login_result": {"refreshToken": "qr-new-token-123456789"}
        }).encode()).decode()
        with patch("reelay.core.authaliyun._passport_request", side_effect=[
            {"t": 1234, "ck": "check", "codeContent": "https://example.com/qr"},
            {"qrCodeStatus": "SCANED"},
            {"qrCodeStatus": "CONFIRMED", "bizExt": code},
        ]), patch("reelay.core.authaliyun.AliyunAuth._exchange",
                 return_value=("qr-rotated-token-123456", "drive123")):
            start = self.client.post("/api/authaliyun/qr")
            self.assertEqual(start.status_code, 200, start.text)
            uid = start.json()["uid"]
            image = self.client.get("/api/authaliyun/qr/image", params={"uid": uid})
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image.headers["content-type"], "image/svg+xml")
            self.assertEqual(image.headers["cache-control"], "no-store")
            self.assertEqual(self.client.get("/api/authaliyun/qr", params={"uid": uid}).json()["status"], "SCANED")
            confirmed = self.client.get("/api/authaliyun/qr", params={"uid": uid})
            self.assertEqual(confirmed.status_code, 200, confirmed.text)
            self.assertTrue(confirmed.json()["authenticated"])
            self.assertTrue(self.client.get("/api/authaliyun/qr", params={"uid": uid}).json()["authenticated"])
        self.assertEqual(secret_store.get("aliyun_refresh_token"), "qr-rotated-token-123456")
        self.assertNotIn("qr-rotated-token", confirmed.text)

    def test_restricted_management_endpoints_require_login(self):
        secret_store.current().update({"web_password": "test-password-123"})
        for method, url in (
            ("get", "/api/authaliyun/status"),
            ("post", "/api/authaliyun/qr"),
            ("put", "/api/authaliyun/token"),
            ("get", "/api/authaliyun/qr/image?uid=1234567890123456"),
        ):
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(self.client, method)(url).status_code, 401)


if __name__ == "__main__":
    unittest.main()
