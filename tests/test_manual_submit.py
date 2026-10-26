"""Historical Telegram permalink submission is limited to configured channels."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from reelay.core.store import Store
from reelay.main import _serve_fetch
from reelay.providers.source import TelegramUserbotSource
from tests.fakes import WebCase


class PermalinkSubmissionTests(WebCase):
    CONFIG = {
        "save_dirs": {"默认": "aliyun:movie_root"},
        "defaults": {"parser": "regex", "storage": "aliyun", "save_to": "默认",
                     "approval_mode": "manual"},
        "sources": [{"bot": "@shareAliyun", "storage": "aliyun", "parser": "regex",
                     "approval_mode": "manual"}],
    }

    def test_manual_permalink_is_read_once_and_repeated_request_is_idempotent(self):
        service = self.app.state.service
        sample = {
            "text": "牧神记 (2024) 第106集 https://www.alipan.com/s/abc123",
            "urls": ["https://www.alipan.com/s/abc123"],
        }
        with patch.object(service, "source_messages", new_callable=AsyncMock,
                          return_value={"messages": [sample]}) as fetched:
            first = self.client.post("/api/resources/submit", json={
                "text": "https://t.me/shareAliyun/123456"})
            self.assertEqual(first.status_code, 200, first.text)
            second = self.client.post("/api/resources/submit", json={
                "text": "https://t.me/shareAliyun/123456"})
            self.assertEqual(second.status_code, 409, second.text)
        self.assertEqual(fetched.await_count, 2)
        fetched.assert_awaited_with("@shareAliyun", limit=1, message_id=123456)
        ctx = self.app.state.store.get_task(first.json()["task_id"])
        self.assertEqual(ctx.raw.source, "@shareAliyun")
        self.assertIn("abc123", ctx.raw.text)
        self.assertTrue(ctx.raw.extra["manual_import"])
        self.assertEqual(ctx.source_rule["storage"], "aliyun")

    def test_unknown_and_private_channels_are_rejected_without_fetch(self):
        service = self.app.state.service
        with patch.object(service, "source_messages", new_callable=AsyncMock) as fetched:
            for text in (
                "https://t.me/unknownchannel/12345",
                "https://t.me/c/123456/789",
                "https://t.me/shareAliyun/0",
            ):
                result = self.client.post("/api/resources/submit", json={"text": text})
                self.assertIn(result.status_code, (409, 422), (text, result.text))
        fetched.assert_not_awaited()

    def test_manual_cloud_share_reuses_normal_task_dedup(self):
        message = "仙逆 (2024) S01E100 https://www.alipan.com/s/abc123"
        first = self.client.post("/api/resources/submit", json={"text": message})
        second = self.client.post("/api/resources/submit", json={"text": message})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 409, second.text)


class FetchMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_reads_one_message_from_authorized_source(self):
        listener = TelegramUserbotSource()
        listener.configure(["@shareAliyun"])
        listener._authorized = True
        telegram = SimpleNamespace(
            is_connected=lambda: True,
            get_entity=AsyncMock(return_value=SimpleNamespace(username="shareAliyun")),
            get_messages=AsyncMock(return_value=SimpleNamespace(
                id=987, raw_text="牧神记 https://www.alipan.com/s/abc123", entities=[])),
        )
        listener._client = telegram
        result = await listener.message_by_id("@shareAliyun", 987)
        self.assertEqual(result[0]["msg_id"], "987")
        telegram.get_messages.assert_awaited_once_with(
            telegram.get_entity.return_value, ids=987)
        with self.assertRaises(ValueError):
            await listener.message_by_id("@unknown", 987)

    async def test_sqlite_fetch_dispatch_uses_message_id_not_search(self):
        db = Store(":memory:")
        self.addCleanup(db.close)
        fetch_id = db.request_source_fetch("@shareAliyun", 1, message_id=789)
        claimed = db.claim_source_fetches()
        self.assertEqual(claimed[0]["message_id"], 789)
        listener = SimpleNamespace(recent=AsyncMock(), search_messages=AsyncMock(),
                                   message_by_id=AsyncMock(return_value=[
                                       {"text": "节目 https://www.alipan.com/s/abc", "urls": []}
                                   ]))
        await _serve_fetch(db, listener, claimed[0])
        self.assertEqual(db.source_fetch(fetch_id)["status"], "done")
        listener.message_by_id.assert_awaited_once_with("@shareAliyun", 789)
        listener.recent.assert_not_awaited()
        listener.search_messages.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
