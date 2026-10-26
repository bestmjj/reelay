"""Subscription simulation does not enqueue tasks or interact with cloud storage."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from reelay.core import discovery, subscriptions
from tests.fakes import WebCase


class SubscriptionSimulationWebTests(WebCase):
    CONFIG = {
        "save_dirs": {"默认": "aliyun:show_folder"},
        "defaults": {"save_to": "默认", "storage": "aliyun",
                     "parser": "regex", "approval_mode": "manual", "decision": "subscription"},
        "sources": [{"bot": "@shareAliyun", "storage": "aliyun",
                     "parser": "regex", "approval_mode": "manual", "decision": "subscription"}],
    }

    def subscribe(self):
        entry = subscriptions.normalize({"title": "牧神记", "season": 1, "tmdbid": 236534})
        return self.app.state.store.add_subscription(subscriptions.key(entry), entry)

    def test_default_simulation_without_seen_messages_is_safe(self):
        sub = self.subscribe()
        before = self.app.state.store.counts_by_state()
        result = self.client.post("/api/subscriptions/simulate", json={"id": sub["id"]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"], "no_sample")
        self.assertFalse(result.json()["task_created"])
        self.assertEqual(self.app.state.store.counts_by_state(), before)
        self.assertEqual(self.client.post("/api/subscriptions/simulate",
            json={"id": sub["id"], "source": "@other", "text": "test"}).status_code, 422)

    def test_simulation_classifies_automatic_and_manual_without_creating_tasks(self):
        sub = self.subscribe()
        service = self.app.state.service
        base = {
            "parsed": {"title": "牧神记", "season": 1, "episodes": [106]},
            "matched_subscription": {"id": sub["id"], "title": "牧神记", "season": 1},
            "released_by_subscription": True, "decision": "save",
            "routing": {"share_provider": "aliyun", "target_storage": "aliyun", "compatible": True},
            "target_preview": "动漫/国产动漫", "confidence": {}, "trail": ["订阅新集自动放行"],
        }
        with patch.object(service, "test_parse", new_callable=AsyncMock, return_value=base) as parser:
            result = self.client.post("/api/subscriptions/simulate", json={
                "id": sub["id"], "text": "牧神记 第106集 https://www.alipan.com/s/test123"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"], "automatic")
        self.assertEqual(result.json()["target_preview"], "动漫/国产动漫")
        self.assertFalse(result.json()["task_created"])
        parser.assert_awaited_once()
        self.assertEqual(self.app.state.store.counts_by_state(), {})
        with patch.object(service, "test_parse", new_callable=AsyncMock,
                          return_value={**base, "decision": "ask", "released_by_subscription": False}):
            pending = self.client.post("/api/subscriptions/simulate", json={
                "id": sub["id"], "text": "牧神记 第106集"})
        self.assertEqual(pending.json()["status"], "manual")
        with patch.object(service, "test_parse", new_callable=AsyncMock,
                          return_value={**base, "routing": {**base["routing"], "compatible": False}}):
            blocked = self.client.post("/api/subscriptions/simulate", json={
                "id": sub["id"], "text": "牧神记 第106集"})
        self.assertEqual(blocked.json()["status"], "blocked")
        self.assertEqual(self.app.state.store.counts_by_state(), {})

    def test_discovery_health_is_visible_in_subscription_list(self):
        sub = self.subscribe()
        key = discovery.state_key("@shareAliyun", sub)
        self.app.state.store.state_set(discovery.NAMESPACE, key, {
            "source": "@shareAliyun", "checked_at": 1000, "matches": 2,
            "emitted": 2, "error": None, "next_at": 0,
        })
        result = self.client.get("/api/subscriptions")
        self.assertEqual(result.status_code, 200, result.text)
        status = result.json()["items"][0]["discovery"]
        self.assertEqual(status["source"], "@shareAliyun")
        self.assertEqual(status["matches"], 2)
        self.assertEqual(status["checked_at"], 1000)


if __name__ == "__main__":
    unittest.main()
