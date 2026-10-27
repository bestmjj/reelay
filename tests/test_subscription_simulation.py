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

    def test_historical_share_needs_source_review_without_changing_source_mode(self):
        store = self.app.state.store
        pipeline = self.app.state.pipeline
        original = pipeline.rule_for("@shareAliyun")["approval_mode"]
        self.assertEqual(original, "manual")
        response = self.client.post("/api/resources/submit", json={
            "text": "牧神记 (2024) 第106集 https://www.alipan.com/s/abc123",
        })
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["state"], "received")
        ctx = store.get_task(body["task_id"])
        self.assertTrue(ctx.raw.extra["manual_import"])
        self.assertEqual(ctx.source_rule["approval_mode"], "auto")
        self.assertEqual(pipeline.rule_for("@shareAliyun")["approval_mode"], "manual")
        # Historical Telegram permalinks are supported, but the test instance
        # has no live listener. Fail safely without silently using another post.
        self.assertEqual(self.client.post("/api/resources/submit",
            json={"text": "https://t.me/shareAliyun/12345"}).status_code, 409)
        self.assertEqual(self.client.post("/api/resources/submit", json={
            "text": "牧神记 https://www.alipan.com/s/abc123", "source": "@untrusted",
        }).status_code, 422)
        self.assertEqual(store.counts_by_state().get("received"), 1)

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

    def test_no_local_message_queries_telegram_history_without_creating_a_task(self):
        sub = self.subscribe()
        service = self.app.state.service
        base = {
            "parsed": {"title": "牧神记", "season": 1, "episodes": [106]},
            "matched_subscription": {"id": sub["id"], "title": "牧神记", "season": 1},
            "released_by_subscription": True, "decision": "save",
            "routing": {"share_provider": "aliyun", "target_storage": "aliyun", "compatible": True},
            "target_preview": "动漫/国产动漫", "confidence": {}, "trail": ["订阅自动放行"],
        }
        history = {"messages": [{"text": "牧神记 第106集 https://www.alipan.com/s/abc123",
                                  "urls": [], "msg_id": "123"}],
                   "origin": "telegram_search", "problem": None}
        with patch.object(service, "source_messages", new_callable=AsyncMock,
                          return_value=history) as source, \
                patch.object(service, "test_parse", new_callable=AsyncMock,
                             return_value=base) as parser:
            reply = self.client.post("/api/subscriptions/simulate", json={"id": sub["id"]})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["status"], "automatic")
        self.assertEqual(reply.json()["example_origin"], "telegram_search")
        source.assert_awaited_once_with("@shareAliyun", limit=5, search="牧神记")
        parser.assert_awaited_once()
        self.assertEqual(self.app.state.store.counts_by_state(), {})

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

    def test_subscribe_start_api_changes_only_future_episode_floor(self):
        sub = self.subscribe()
        response = self.client.post("/api/subscriptions/start", json={
            "id": sub["id"], "start_episode": 100})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["start_episode"], 100)
        self.assertEqual(self.client.get("/api/subscriptions").json()["items"][0]["start_episode"], 100)
        invalid = self.client.post("/api/subscriptions/start", json={
            "id": sub["id"], "start_episode": 0})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(self.app.state.store.subscription(sub["id"])["start_episode"], 100)

    def test_existing_file_requires_explicit_confirmation_and_remote_readback(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from reelay.core import existing_media
        sub = self.subscribe()
        record = {
            "alias": "默认", "provider": "aliyun", "drive_id": "resourceA",
            "id": "f106", "parent_id": "show_folder", "name": "牧神记.S01E106.mkv",
            "parents": ["牧神记"], "title": "牧神记", "season": 1, "episode": 106,
            "media_type": "anime", "year": None, "tmdbid": None, "size": 1000, "sha1": "ABC",
        }
        store = self.app.state.store
        store.state_set("library_scan", "files", [record])
        store.state_set("library_scan", "snapshot", {"complete": False})
        preview = self.client.get("/api/subscriptions/existing", params={"id": sub["id"]})
        self.assertEqual(preview.status_code, 200, preview.text)
        item = preview.json()["items"][0]
        self.assertFalse(item["associated"])
        self.assertEqual(item["episode"], 106)
        self.assertEqual(store.counts_by_state(), {})
        provider = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        fs = SimpleNamespace(get_attr=Mock(return_value={
            "id": "f106", "parent_id": "show_folder", "name": "牧神记.S01E106.mkv",
            "is_dir": False, "size": 1000, "sha1": "ABC",
        }))
        with patch.object(provider, "ready", return_value=True), \
                patch.object(provider, "client", SimpleNamespace(drive_id="resourceA")), \
                patch.object(provider, "library", return_value=fs):
            linked = self.client.post("/api/subscriptions/existing/associate",
                                      json={"id": sub["id"], "file_keys": [item["file_key"]]})
        self.assertEqual(linked.status_code, 200, linked.text)
        self.assertEqual(linked.json()["associated"], 1)
        self.assertTrue(self.client.get("/api/subscriptions/existing",
            params={"id": sub["id"]}).json()["items"][0]["associated"])
        self.assertEqual(store.counts_by_state(), {})  # no "done" or transfer created
        overlay = existing_media.apply_associations(
            store.state_get("library_scan", "files", []),
            store.state_get("media_associations", "confirmed", {}))
        self.assertEqual(overlay[0]["tmdbid"], 236534)
        self.assertEqual(overlay[0]["title"], "牧神记")
        self.assertEqual(overlay[0]["year"], None)
        fs.get_attr.assert_called_once_with("f106")
        changed = {**record, "name": "renamed.mkv"}
        self.assertIsNone(existing_media.apply_associations([changed],
            store.state_get("media_associations", "confirmed", {}))[0]["tmdbid"])
        self.assertEqual(store.state_get("library_scan", "files")[0]["tmdbid"], None)

    def test_stale_existing_file_is_never_associated(self):
        from types import SimpleNamespace
        sub = self.subscribe()
        record = {"alias": "默认", "provider": "aliyun", "drive_id": "resourceA",
                  "id": "f100", "parent_id": "show_folder", "name": "牧神记.S01E100.mkv",
                  "parents": [], "title": "牧神记", "season": 1, "episode": 100,
                  "media_type": "anime", "size": 10, "sha1": None}
        self.app.state.store.state_set("library_scan", "files", [record])
        provider = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        with patch.object(provider, "ready", return_value=True), \
                patch.object(provider, "client", SimpleNamespace(drive_id="resourceA")), \
                patch.object(provider, "library", return_value=SimpleNamespace(
                    get_attr=lambda fid: {**record, "parent_id": "different", "is_dir": False})):
            response = self.client.post("/api/subscriptions/existing/associate", json={
                "id": sub["id"], "file_keys": ["aliyun:resourceA:f100"]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.app.state.store.state_get("media_associations", "confirmed", {}), {})

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
