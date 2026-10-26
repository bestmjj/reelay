"""Aliyun login regressions: no network, QR state machine, token rotation and safety."""
from __future__ import annotations

import base64
import json
import time
import unittest
from unittest.mock import patch

from reelay.core import secret_store
from reelay.core.authaliyun import AliyunAuth, _refresh_from_biz_ext
from reelay.providers.aliyun import AliyunAPIError


def encoded(token: str) -> str:
    return base64.b64encode(json.dumps({
        "pds_login_result": {"refreshToken": token},
    }).encode()).decode()


class AuthAliyunTest(unittest.TestCase):
    def setUp(self):
        self.context = secret_store.override()
        self.secrets = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.auth = AliyunAuth()

    def test_validated_manual_token_rotates_and_hides_secret(self):
        self.assertEqual(self.auth.status()["state"], "not_configured")
        with patch.object(self.auth, "_exchange", return_value=("rotated-refresh-token-123", "drive123")):
            status = self.auth.set_token("old-refresh-token-12345")
        self.assertEqual(self.secrets.get("aliyun_refresh_token"), "rotated-refresh-token-123")
        self.assertEqual(status["state"], "active")
        self.assertNotIn("rotated-refresh-token", str(status))

    def test_user_login_status_identifies_resource_not_backup_drive(self):
        with patch("reelay.providers.aliyun.AliyunClient._send", return_value={
            "access_token": "access", "refresh_token": "rotated-refresh-token-123456",
            "resource_drive_id": "resource123", "default_drive_id": "backup456",
            "backup_drive_id": "backup456",
        }):
            state = self.auth.set_token("initial-refresh-token-123456")
        self.assertEqual(state["state"], "active")
        self.assertEqual(state["drive_id"], "resource123")
        self.assertEqual(self.secrets.get("aliyun_refresh_token"), "rotated-refresh-token-123456")

    def test_failed_replacement_preserves_existing_credentials(self):
        self.secrets.update({"aliyun_refresh_token": "previous-working-token-123"})
        with patch.object(self.auth, "_exchange", side_effect=AliyunAPIError(401, "InvalidToken")):
            with self.assertRaises(AliyunAPIError):
                self.auth.set_token("invalid-new-token-123")
        self.assertEqual(self.secrets.get("aliyun_refresh_token"), "previous-working-token-123")

    def test_ping_reports_expired_without_discarding_token(self):
        self.secrets.update({"aliyun_refresh_token": "expired-refresh-token-123"})
        with patch.object(self.auth, "_exchange", side_effect=AliyunAPIError(401, "InvalidToken")):
            result = self.auth.ping()
        self.assertEqual(result["state"], "expired")
        self.assertEqual(self.secrets.get("aliyun_refresh_token"), "expired-refresh-token-123")

    def test_qr_lifecycle_scanned_confirmed_saved_once(self):
        with patch("reelay.core.authaliyun._passport_request", side_effect=[
            {"t": 123456789, "ck": "test-check", "codeContent": "https://example.com/qr"},
            {"qrCodeStatus": "SCANED"},
            {"qrCodeStatus": "CONFIRMED", "bizExt": encoded("qr-refresh-token-123456")},
        ]) as upstream, patch.object(self.auth, "_exchange", return_value=(
                "verified-rotated-token-123", "drive123")) as exchange:
            uid = self.auth.qr_start()["uid"]
            self.assertEqual(self.auth.qr_code(uid), "https://example.com/qr")
            self.assertEqual(self.auth.qr_status(uid)["status"], "SCANED")
            confirmed = self.auth.qr_status(uid)
            self.assertTrue(confirmed["authenticated"])
            self.assertEqual(self.auth.qr_status(uid)["status"], "CONFIRMED")
        self.assertEqual(upstream.call_count, 3)
        self.assertEqual(exchange.call_count, 1)
        self.assertEqual(self.secrets.get("aliyun_refresh_token"), "verified-rotated-token-123")
        self.assertEqual(self.auth._sessions[uid]["code"], "")

    def test_invalid_confirmed_token_does_not_replace_existing(self):
        self.secrets.update({"aliyun_refresh_token": "working-original-token-123"})
        with patch("reelay.core.authaliyun._passport_request", side_effect=[
            {"t": 123, "ck": "test-check", "codeContent": "https://example.com/qr"},
            {"qrCodeStatus": "CONFIRMED", "bizExt": encoded("candidate-token-123456")},
        ]), patch.object(self.auth, "_exchange", side_effect=AliyunAPIError(403, "Denied")):
            uid = self.auth.qr_start()["uid"]
            with self.assertRaises(ValueError):
                self.auth.qr_status(uid)
        self.assertEqual(self.secrets.get("aliyun_refresh_token"), "working-original-token-123")

    def test_expired_and_cancelled_sessions_cannot_be_reused(self):
        with patch("reelay.core.authaliyun._passport_request", return_value={
            "t": 1234, "ck": "test-check", "codeContent": "https://example.com/qr",
        }):
            old = self.auth.qr_start()["uid"]
            self.auth._sessions[old]["expires_at"] = time.time() - 1
            with self.assertRaises(KeyError):
                self.auth.qr_status(old)
            current = self.auth.qr_start()["uid"]
            self.auth.qr_cancel(current)
            with self.assertRaises(KeyError):
                self.auth.qr_code(current)

    def test_malformed_confirmed_data_cannot_be_used(self):
        for sample in ("", "@@@", encoded("short"), base64.b64encode(b"{}").decode()):
            with self.subTest(sample=sample):
                with self.assertRaises(ValueError):
                    _refresh_from_biz_ext(sample)


if __name__ == "__main__":
    unittest.main()
