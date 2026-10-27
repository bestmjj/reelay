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
        "save_dirs": {"默认": "aliyun:movie_root", "动漫": "aliyun:anime_root"},
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

    def test_manual_single_episode_is_scoped_and_whole_season_requires_approval(self):
        from unittest.mock import AsyncMock
        from reelay.core.contracts import Decision, ParsedResource, TaskState
        pipeline = self.app.state.pipeline
        parser = pipeline.reg.get_provider("parser", "regex")
        decider = pipeline.reg.get_provider("decision", "subscription")
        for episodes, expected in (([100], TaskState.PENDING_APPROVAL),
                                   (list(range(1, 101)), TaskState.PENDING_APPROVAL),
                                   ([], TaskState.PENDING_APPROVAL)):
            with self.subTest(episode_count=len(episodes)):
                message = "测试剧 " + str(episodes) + " https://www.alipan.com/s/abc999"
                if not episodes:
                    message += " 合集"
                response = self.client.post("/api/resources/submit", json={"text": message})
                self.assertEqual(response.status_code, 200, response.text)
                ctx = self.app.state.store.get_task(response.json()["task_id"])
                parsed = ParsedResource(is_media=True, media_type="anime",
                                        title="测试剧", year=2024, season=1,
                                        episodes=episodes, share_code="abc999", confidence=0.98)
                with patch.object(parser, "parse", new_callable=AsyncMock, return_value=parsed), \
                        patch.object(pipeline, "run_hooks", new_callable=AsyncMock, return_value=True), \
                        patch.object(decider, "decide", new_callable=AsyncMock, return_value=Decision.SAVE):
                    asyncio.run(pipeline.process_received(ctx))
                self.assertEqual(ctx.state, expected)
                self.assertTrue(ctx.raw.extra["manual_review"])
                self.assertEqual(ctx.source_rule["approval_mode"], "manual")
                if len(episodes) == 1:
                    self.assertEqual(ctx.source_rule.get("episodes_from"), "message")
                else:
                    self.assertTrue(any("禁止盲目转存整季" in line for line in ctx.trail))

    def test_ali_url_and_root_247_instruction_are_not_a_fake_title(self):
        from reelay.core.parse_rules import regex_fields
        from reelay.providers.parser import _regex_parse
        text = "https://www.alipan.com/s/gsQB8YFoLZQ 选择分享根目录中的 247"
        self.assertIsNone(regex_fields(text)["title"])
        response = self.client.post("/api/resources/submit", json={"text": text})
        self.assertEqual(response.status_code, 200, response.text)
        ctx = self.app.state.store.get_task(response.json()["task_id"])
        self.assertTrue(ctx.raw.extra["manual_review"])
        self.assertTrue(ctx.raw.extra["manual_unlabeled"])
        self.assertEqual(ctx.source_rule["approval_mode"], "manual")
        parsed = _regex_parse(ctx)
        self.assertIsNone(parsed.title)
        self.assertEqual(parsed.raw_fields["share_provider"], "aliyun")
        self.assertEqual(parsed.share_code, "gsQB8YFoLZQ")

    def test_ai_guessed_title_from_link_instructions_is_discarded_before_tmdb(self):
        from reelay.core.contracts import Decision, ParsedResource, TaskState

        response = self.client.post("/api/resources/submit", json={
            "text": "https://www.alipan.com/s/another247 选择分享根目录中的 247"})
        self.assertEqual(response.status_code, 200, response.text)
        ctx = self.app.state.store.get_task(response.json()["task_id"])
        fake_parse = ParsedResource(is_media=True, title="选择分享根目录中的 247",
                                    media_type="anime", confidence=0.99, season=1,
                                    episodes=[247], share_code="another247",
                                    raw_fields={"share_provider": "aliyun"})
        parser = self.app.state.pipeline.reg.get_provider("parser", "regex")
        decider = self.app.state.pipeline.reg.get_provider("decision", "subscription")
        with patch.object(parser, "parse", new_callable=AsyncMock, return_value=fake_parse), \
                patch.object(self.app.state.pipeline, "run_hooks",
                             new_callable=AsyncMock, return_value=True), \
                patch.object(decider, "decide", new_callable=AsyncMock,
                             return_value=Decision.SAVE):
            asyncio.run(self.app.state.pipeline.process_received(ctx))
        self.assertEqual(ctx.state, TaskState.PENDING_APPROVAL)
        self.assertEqual(ctx.decision, Decision.ASK)
        self.assertIsNone(ctx.parsed.title)
        self.assertIsNone(ctx.parsed.media_type)
        # "选择根目录中的 247" is an instruction, not a trustworthy
        # episode announcement. The operator must supply E247 explicitly.
        self.assertFalse(ctx.parsed.episodes)
        self.assertNotEqual(ctx.source_rule.get("episodes_from"), "message")

    def test_link_only_reparse_becomes_editable_review_and_requires_exact_file(self):
        """Reproduce the production bug: an explicit bare Aliyun link was SKIPPED."""
        from reelay.core.contracts import Decision, TaskState
        text = ("https://www.alipan.com/s/gsQB8YFoLZQ"
                "/folder/66dedc90db272aa5727046ab8aa6870281939962")
        result = self.client.post("/api/resources/submit", json={
            "text": text, "source": "@shareAliyun",
        })
        self.assertEqual(result.status_code, 200, result.text)
        task_id = result.json()["task_id"]
        db = self.app.state.store
        ctx = db.get_task(task_id)
        self.assertTrue(ctx.raw.extra["manual_unlabeled"])
        # Mimic the existing task created by the old deployed image.
        ctx.state = TaskState.SKIPPED
        ctx.decision = Decision.SKIP
        db.save_task(ctx)
        restored = self.client.post(f"/api/tasks/{task_id}/reparse")
        self.assertEqual(restored.status_code, 200, restored.text)
        ctx = db.get_task(task_id)
        self.assertEqual(ctx.state, TaskState.RECEIVED)

        # No network AI invocation is necessary: a URL does not carry a name.
        llm_parser = self.app.state.pipeline.reg.get_provider("parser", "llm")
        with patch.object(llm_parser, "parse", side_effect=AssertionError(
                "A bare manual share should not spend an AI request")):
            asyncio.run(self.app.state.pipeline.process_received(ctx))
        ctx = db.get_task(task_id)
        self.assertEqual((ctx.state, ctx.decision),
                         (TaskState.PENDING_APPROVAL, Decision.ASK))
        self.assertTrue(ctx.parsed.is_media)
        self.assertFalse(ctx.parsed.is_ad)
        self.assertIsNone(ctx.parsed.title)
        self.assertIsNone(ctx.parsed.media_type)
        self.assertEqual(ctx.parsed.episodes, [])
        self.assertEqual(ctx.parsed.share_code, "gsQB8YFoLZQ")
        self.assertTrue(ctx.meta["parse"]["manual_link_only"])
        detail = self.client.get(f"/api/tasks/{task_id}").json()
        self.assertIn("correct", detail["actions"])
        self.assertIn("choose_share_folder", detail["actions"])

        # No auto-save before confirming the genuine work and single episode.
        response = self.client.post(f"/api/tasks/{task_id}/approve", json={})
        self.assertEqual(response.status_code, 409, response.text)
        corrected = self.client.patch(f"/api/tasks/{task_id}/parsed", json={
            "values": {
                "title": "吞噬星空", "media_type": "anime", "year": 2020,
                "season": 1, "episodes": [247], "resolution": "2160p",
            },
        })
        self.assertEqual(corrected.status_code, 200, corrected.text)
        ctx = db.get_task(task_id)
        self.assertEqual(ctx.source_rule["episodes_from"], "message")
        self.assertEqual(ctx.parsed.episodes, [247])

        # Even corrected metadata alone may not copy a whole share.
        response = self.client.post(f"/api/tasks/{task_id}/approve", json={})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(db.get_task(task_id).state, TaskState.PENDING_APPROVAL)

        # Simulate the read-only share inventory; verify that selecting the
        # true 247 video and a deliberate Aliyun destination makes the task
        # QUEUED without initiating any remote cloud copy.
        storage = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        files = [{
            "file_id": "good247", "name": "247 4K.mp4", "path": "",
            "size": 1620000000, "selectable": True,
        }, {
            "file_id": "wrong4k", "name": "4K.mp4", "path": "洛DA",
            "size": 4040000000, "selectable": False,
        }]
        with patch.object(storage, "ready", return_value=True), \
                patch.object(storage, "share_source_inventory",
                             return_value={"files": files, "folders": []}):
            chosen = self.client.post(f"/api/tasks/{task_id}/share-file",
                                      json={"file_id": "good247"})
        self.assertEqual(chosen.status_code, 200, chosen.text)
        self.assertEqual(chosen.json()["selected_file_id"], "good247")
        response = self.client.post(f"/api/tasks/{task_id}/approve", json={
            "save_dir_alias": "动漫",
        })
        self.assertEqual(response.status_code, 200, response.text)
        queued = db.get_task(task_id)
        self.assertEqual(queued.state, TaskState.QUEUED)
        self.assertEqual(queued.save_dir_alias, "动漫")
        self.assertEqual(queued.ns("share_selection")["file_id"], "good247")
        self.assertFalse(queued.ns("storage").get("landed"))

    def test_link_only_manual_import_still_obeys_keyword_prefilter(self):
        from reelay.core.contracts import TaskState
        result = self.client.post("/api/resources/submit", json={
            "text": "https://www.alipan.com/s/blocked987", "source": "@shareAliyun",
        })
        self.assertEqual(result.status_code, 200, result.text)
        ctx = self.app.state.store.get_task(result.json()["task_id"])
        ctx.source_rule["filters"] = {"block_keywords": ["blocked987"]}
        asyncio.run(self.app.state.pipeline.process_received(ctx))
        self.assertEqual(ctx.state, TaskState.SKIPPED)

    def test_link_only_with_metadata_allow_filter_stays_reviewable(self):
        from reelay.core.contracts import TaskState
        result = self.client.post("/api/resources/submit", json={
            "text": "https://www.alipan.com/s/review987", "source": "@shareAliyun",
        })
        self.assertEqual(result.status_code, 200, result.text)
        ctx = self.app.state.store.get_task(result.json()["task_id"])
        ctx.source_rule["filters"] = {"allow": [{"media_type": "anime"}]}
        asyncio.run(self.app.state.pipeline.process_received(ctx))
        self.assertEqual(ctx.state, TaskState.PENDING_APPROVAL)

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
