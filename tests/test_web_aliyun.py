"""Aliyun Drive login HTTP API, including auth guard and QR image."""
from __future__ import annotations

import base64
import json
import time
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
        self.assertEqual(self.client.get("/api/dashboard").json()["connections"]["aliyun"]["status"], "active")
        response = self.client.delete("/api/authaliyun/token")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["status"]["configured"])
        self.assertEqual(self.client.get("/api/dashboard").json()["connections"]["aliyun"]["status"],
                         "not_configured")

    def test_ping_success_reaches_dashboard_and_survives_a_status_read(self):
        """Regression: the login card was active while Dashboard stayed unverified."""
        secret_store.current().update({"aliyun_refresh_token": "preexisting-refresh-token-123"})
        before = self.client.get("/api/dashboard").json()["connections"]["aliyun"]
        self.assertEqual(before["status"], "unverified")
        with patch("reelay.core.authaliyun.AliyunAuth._exchange",
                   return_value=("rotated-after-ping-12345", "drive123")):
            ping = self.client.post("/api/authaliyun/ping")
        self.assertEqual(ping.status_code, 200, ping.text)
        self.assertEqual(ping.json()["state"], "active")
        dashboard = self.client.get("/api/dashboard").json()
        self.assertEqual(dashboard["connections"]["aliyun"]["status"], "active")
        self.assertEqual(dashboard["runtime"]["aliyun"]["status"], "active")
        self.assertEqual(dashboard["runtime"]["aliyun"]["check_interval"], 3600)
        self.assertNotIn("rotated-after-ping-12345", json.dumps(dashboard))
        self.assertEqual(secret_store.get("aliyun_refresh_token"), "rotated-after-ping-12345")

    def test_recent_auth_status_survives_process_restart_and_worker_rotation(self):
        from reelay.core.authaliyun import AliyunAuth
        from reelay.web.aliyun import observed_aliyun_status

        secret_store.current().update({
            "aliyun_refresh_token": "initial-refresh-token-123456",
        })
        with patch("reelay.core.authaliyun.AliyunAuth._exchange",
                   return_value=("rotated-token-1234567890", "resource-drive")):
            result = self.client.post("/api/authaliyun/ping")
        self.assertEqual(result.json()["state"], "active")
        store = self.app.state.store
        # New Web process: its AliyunAuth had no in-memory validation.
        recreated = observed_aliyun_status(AliyunAuth(), store)
        self.assertEqual(recreated["state"], "active")
        self.assertTrue(recreated["last_observation"])
        self.assertIsNotNone(recreated["checked_at"])
        self.assertIsNone(recreated["drive_id"])
        # Worker rotates token independently while browsing the cloud.
        secret_store.current().update({
            "aliyun_refresh_token": "worker-rotated-token-987654",
        })
        with patch("reelay.core.authaliyun.AliyunAuth._exchange",
                   side_effect=AssertionError("GET status must never refresh token")):
            status = self.client.get("/api/authaliyun/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["state"], "active")
        self.assertTrue(status.json()["last_observation"])
        self.assertNotIn("worker-rotated-token-987654", status.text)
        # Recent evidence is not permanent; expiration is auto-recheckable.
        with store._lock, store._conn:
            store._conn.execute(
                "UPDATE runtime_health SET updated_at=? WHERE component='aliyun'",
                (time.time() - 7205,),
            )
        stale = self.client.get("/api/authaliyun/status").json()
        self.assertEqual(stale["state"], "unknown")
        # Clearing via a different settings route may not leave an active
        # badge merely because a historical probe was successful.
        secret_store.current().update({"aliyun_refresh_token": None})
        self.assertEqual(self.client.get("/api/authaliyun/status").json()["state"],
                         "not_configured")

    def test_ping_expired_or_network_error_reaches_dashboard(self):
        secret_store.current().update({"aliyun_refresh_token": "saved-refresh-token-12345"})
        for exception, expected in (
            (AliyunAPIError(401, "InvalidToken"), "expired"),
            (ConnectionError("temporary failure"), "error"),
        ):
            with self.subTest(expected=expected):
                with patch("reelay.core.authaliyun.AliyunAuth._exchange", side_effect=exception):
                    response = self.client.post("/api/authaliyun/ping")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["state"], expected)
                dashboard = self.client.get("/api/dashboard").json()["connections"]["aliyun"]
                self.assertEqual(dashboard["status"], expected)
                self.assertEqual(secret_store.get("aliyun_refresh_token"), "saved-refresh-token-12345")

    def test_aliyun_probe_expires_to_stale_then_clears(self):
        secret_store.current().update({"aliyun_refresh_token": "refresh-token-123456789"})
        with patch("reelay.core.authaliyun.AliyunAuth._exchange",
                   return_value=("refresh-token-rotated-123", "drive123")):
            self.assertEqual(self.client.post("/api/authaliyun/ping").json()["state"], "active")
        store = self.app.state.store
        # No continuously running probe: a successful observation is not immortal.
        with store._lock, store._conn:
            store._conn.execute(
                "UPDATE runtime_health SET updated_at=? WHERE component='aliyun'",
                (time.time() - 7201,),
            )
        self.assertEqual(self.client.get("/api/dashboard").json()["connections"]["aliyun"]["status"], "stale")
        self.assertEqual(self.client.delete("/api/authaliyun/token").status_code, 200)
        dashboard = self.client.get("/api/dashboard").json()["connections"]["aliyun"]
        self.assertEqual((dashboard["configured"], dashboard["status"]), (False, "not_configured"))
        self.assertEqual(store.health_status()["aliyun"]["status"], "not_configured")

    def test_credential_removed_by_other_settings_route_never_looks_connected(self):
        secret_store.current().update({"aliyun_refresh_token": "refresh-token-123456789"})
        with patch("reelay.core.authaliyun.AliyunAuth._exchange",
                   return_value=("refresh-token-rotated-123", "drive123")):
            self.client.post("/api/authaliyun/ping")
        # Generic secret management also allows clearing the token.
        secret_store.current().update({"aliyun_refresh_token": None})
        self.assertEqual(self.client.get("/api/dashboard").json()["connections"]["aliyun"]["status"],
                         "not_configured")

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
            self.assertEqual(self.client.post("/api/authaliyun/qr/status", json={"uid": uid}).json()["status"], "SCANED")
            confirmed = self.client.post("/api/authaliyun/qr/status", json={"uid": uid})
            self.assertEqual(confirmed.status_code, 200, confirmed.text)
            self.assertTrue(confirmed.json()["authenticated"])
            self.assertTrue(self.client.post("/api/authaliyun/qr/status", json={"uid": uid}).json()["authenticated"])
        self.assertEqual(secret_store.get("aliyun_refresh_token"), "qr-rotated-token-123456")
        self.assertNotIn("qr-rotated-token", confirmed.text)
        self.assertEqual(self.client.get("/api/dashboard").json()["connections"]["aliyun"]["status"], "active")

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
