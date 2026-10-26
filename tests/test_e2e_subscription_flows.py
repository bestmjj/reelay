"""HTTP-to-pipeline subscription regressions (mocked cloud; never copies files)."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from reelay.core.contracts import Decision, ParsedResource, TaskState
from reelay.core import works
from tests.fakes import WebCase, tv_ctx


class SubscriptionFlowE2E(WebCase):
    CONFIG = {
        "save_dirs": {"阿里动漫": "aliyun:anime-folder", "115动漫": "123456"},
        "defaults": {"save_to": "阿里动漫", "storage": "aliyun",
                     "parser": "regex", "approval_mode": "manual", "decision": "subscription"},
        "sources": [
            {"bot": "@shareAliyun", "save_to": "阿里动漫", "storage": "aliyun"},
            {"bot": "@share115", "save_to": "115动漫", "storage": "p115"},
        ],
    }

    def _parsed(self, *, episode=106, title="牧神记"):
        return ParsedResource(is_media=True, media_type="anime",
                              title=title, year=2024, season=1,
                              episodes=[episode], share_code="abc123",
                              confidence=0.99, raw_fields={"share_provider": "aliyun"})

    def _process(self, task_id: str, parsed: ParsedResource):
        pipeline = self.app.state.pipeline
        ctx = self.app.state.store.get_task(task_id)
        parser = pipeline.reg.get_provider("parser", "regex")
        decider = pipeline.reg.get_provider("decision", "subscription")
        with patch.object(parser, "parse", new_callable=AsyncMock, return_value=parsed), \
                patch.object(pipeline, "run_hooks", new_callable=AsyncMock, return_value=True), \
                patch.object(decider, "decide", new_callable=AsyncMock, return_value=Decision.SAVE):
            asyncio.run(pipeline.process_received(ctx))
        return self.app.state.store.get_task(task_id)

    def test_manual_request_requires_valid_selected_source_and_is_idempotent(self):
        items = self.client.get("/api/subscriptions").json()
        self.assertEqual(items["sources"], ["@shareAliyun", "@share115"])
        data = {"text": "牧神记 (2024) 第106集 https://www.alipan.com/s/abc123"}
        self.assertEqual(self.client.post("/api/resources/submit", json=data).status_code, 422)
        self.assertEqual(self.client.post("/api/resources/submit",
                         json={**data, "source": "@unknown"}).status_code, 422)
        created = self.client.post("/api/resources/submit",
                                   json={**data, "source": "@shareAliyun"})
        self.assertEqual(created.status_code, 200, created.text)
        task = self.app.state.store.get_task(created.json()["task_id"])
        self.assertEqual(task.source_rule["storage"], "aliyun")
        self.assertEqual(task.raw.source, "@shareAliyun")
        self.assertEqual(self.client.post("/api/resources/submit",
                         json={**data, "source": "@shareAliyun"}).status_code, 409)
        self.assertEqual(self.app.state.store.counts_by_state().get("received"), 1)

    def test_bare_share_is_held_for_review_even_when_source_would_save(self):
        result = self.client.post("/api/resources/submit", json={
            "text": "https://www.alipan.com/s/abc123", "source": "@shareAliyun"})
        self.assertEqual(result.status_code, 200, result.text)
        ctx = self.app.state.store.get_task(result.json()["task_id"])
        self.assertTrue(ctx.raw.extra["manual_unlabeled"])
        ctx = self._process(ctx.task_id, self._parsed())
        self.assertEqual(ctx.state, TaskState.PENDING_APPROVAL)
        self.assertTrue(any("人工补齐" in step for step in ctx.trail))

    def test_known_single_episode_queues_without_expanding_old_pack(self):
        result = self.client.post("/api/resources/submit", json={
            "text": "牧神记 第106集 https://www.alipan.com/s/abc123",
            "source": "@shareAliyun"})
        self.assertEqual(result.status_code, 200, result.text)
        ctx = self._process(result.json()["task_id"], self._parsed())
        self.assertEqual(ctx.state, TaskState.QUEUED)
        self.assertEqual(ctx.source_rule["episodes_from"], "message")
        self.assertEqual(ctx.parsed.episodes, [106])

    def test_global_115_episode_does_not_prevent_aliyun_subscription_decision(self):
        from reelay.core import subscriptions
        from reelay.core import runtime
        from reelay.providers.decision import SubscriptionDecision
        store = self.app.state.store
        sub = subscriptions.normalize({"title": "牧神记", "season": 1,
                                       "tmdbid": 236534, "start_episode": 100})
        store.add_subscription(subscriptions.key(sub), sub)
        earlier = tv_ctx(title="牧神记", tmdbid=236534, episodes=(106,))
        earlier.state = TaskState.STORED
        earlier.result = {"storage_verified": True}
        earlier.ns("storage")["identity"] = {
            "cid": "123456", "provider": "p115", "share_code": "old115",
            "title": "牧神记", "season": 1, "episodes": [106],
            "work": works.identity("牧神记", tmdbid=236534)}
        store.save_task(earlier)
        store.finish_transfer(earlier)
        new = tv_ctx(title="牧神记", tmdbid=236534, episodes=(106,), msg_id="new")
        new.source_rule["storage"] = "aliyun"
        with self.app.state.pipeline.scope(self.app.state.pipeline.cfg):
            decision = SubscriptionDecision.promote(new, Decision.ASK)
        self.assertEqual(decision, Decision.SAVE)
        self.assertEqual(new.source_rule["episodes_from"], "message")
        self.assertTrue(new.meta.get("subscription"))
        # The final transfer gate, not this global UI counter, owns drive-scoped dedup.


if __name__ == "__main__":
    unittest.main()
