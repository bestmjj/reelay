"""Regression tests for 'updated to E106' subscribed releases (no real cloud writes)."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from reelay.core import subscriptions
from reelay.core.contracts import AlreadyStored, Decision, NeedsManual, TaskState
from reelay.providers.decision import SubscriptionDecision
from reelay.providers.storage import _select_video_items
from tests.fakes import ServiceCase, tv_ctx


class ProgressEpisodeTests(ServiceCase):
    CFG = {"save_dirs": {"默认": "1"},
           "defaults": {"save_to": "默认", "storage": "p115", "decision": "subscription",
                        "approval_mode": "manual"}}
    SUBSCRIPTIONS = [{"title": "牧神记", "season": 1, "tmdbid": 236534, "start_episode": 100}]

    def progress(self, *, text="牧神记 (2024) 更新至第106集", episodes=range(1, 107),
                 tmdb_id=236534):
        ctx = tv_ctx(title="牧神记", tmdbid=tmdb_id, episodes=tuple(episodes))
        ctx.raw.text = text
        ctx.parsed.year = 2024
        ctx.source_rule["episodes_from"] = "share"
        return ctx

    async def test_subscribed_progress_chooses_e106_not_e01_e106(self):
        ctx = self.progress()
        self.assertEqual(subscriptions.scope_latest_progress(ctx, self.store.subscriptions()), "scoped")
        self.assertEqual(ctx.parsed.episodes, [106])
        self.assertEqual(ctx.source_rule["episodes_from"], "message")
        self.assertEqual(ctx.meta["subscription_progress"]["latest"], 106)
        with self.pl.scope(self.pl.cfg):
            self.assertEqual(await SubscriptionDecision().decide(ctx), Decision.SAVE)
        self.assertEqual(ctx.parsed.episodes, [106])
        self.assertTrue(any("不复制整季" in line for line in ctx.trail))
        before = len(ctx.trail)
        self.assertEqual(subscriptions.scope_latest_progress(ctx, self.store.subscriptions()), "scoped")
        self.assertEqual(len(ctx.trail), before)  # safe if called again on retry

    async def test_progress_below_subscription_floor_is_not_auto_copied(self):
        sub = self.store.subscriptions()[0]
        self.store.set_subscription_start(sub["id"], 107)
        ctx = self.progress()
        self.assertEqual(subscriptions.scope_latest_progress(ctx, self.store.subscriptions()), "below_floor")
        self.assertEqual(ctx.parsed.episodes, list(range(1, 107)))
        with self.pl.scope(self.pl.cfg):
            self.assertEqual(await SubscriptionDecision().decide(ctx), Decision.ASK)

    def test_missing_or_untrusted_subscription_does_not_restrict_bulk_source(self):
        other = self.progress(tmdb_id=999999)
        self.assertIsNone(subscriptions.scope_latest_progress(other, self.store.subscriptions()))
        self.assertEqual(len(other.parsed.episodes), 106)
        unmatched = self.progress()
        unmatched.meta["tmdb"]["matched"] = False
        self.assertIsNone(subscriptions.scope_latest_progress(unmatched, self.store.subscriptions()))
        complete_pack = self.progress(text="牧神记 (2024) 全106集")
        self.assertIsNone(subscriptions.scope_latest_progress(complete_pack, self.store.subscriptions()))
        explicit_pack = self.progress(text="牧神记 S01E01-E106")
        self.assertIsNone(subscriptions.scope_latest_progress(explicit_pack, self.store.subscriptions()))
        contradictory = self.progress(text="牧神记 更新至第105集")
        self.assertIsNone(subscriptions.scope_latest_progress(contradictory, self.store.subscriptions()))

    def test_share_listing_requires_exact_e106_file(self):
        ctx = self.progress()
        subscriptions.scope_latest_progress(ctx, self.store.subscriptions())
        listing = [{"id": f"f{e}", "name": f"牧神记.S01E{e:03d}.mkv",
                    "size": 100, "sha1": "A" * 40, "is_dir": False}
                   for e in range(1, 107)]
        selected = _select_video_items(listing, ctx.parsed, scope=ctx.source_rule["episodes_from"])
        self.assertEqual([entry["name"] for entry in selected["files"]], ["牧神记.S01E106.mkv"])
        self.assertEqual(selected["episodes"], [106])
        with self.assertRaisesRegex(NeedsManual, "106"):
            _select_video_items(listing[:-1], ctx.parsed, scope="message")
        # Even a valid E106 cannot be chosen if two releases claim the same episode.
        with self.assertRaises(NeedsManual):
            _select_video_items(listing + [{**listing[-1], "id": "alternate",
                                            "name": "牧神记.S01E106.other.mkv"}],
                                ctx.parsed, scope="message")

    async def test_old_failed_task_retry_skips_105_historical_scan_matches(self):
        """Prove narrowing happens BEFORE transfer claims and the historical-file guard."""
        ctx = self.progress()
        ctx.raw.msg_id = "old-progress-task"
        ctx.state = TaskState.QUEUED
        ctx.decision = Decision.SAVE
        ctx.source_rule.update({"storage": "p115", "save_to": "默认"})
        self.store.save_task(ctx)
        observed = [
            {"provider": "p115", "drive_id": "115", "id": str(ep), "parent_id": "old",
             "title": "牧神记", "name": f"牧神记.S01E{ep:03d}.mkv", "media_type": "anime",
             "season": 1, "episode": ep, "tmdbid": 236534, "year": 2024,
             "size": 100, "sha1": ""}
            for ep in range(1, 106)
        ]
        self.store.state_set("library_scan", "files", observed)
        storage = self.registry.get_provider("storage", "p115")
        with patch.object(self.pl, "run_hooks", new_callable=AsyncMock, return_value=True), \
                patch.object(storage, "prepare", new_callable=AsyncMock,
                             side_effect=AlreadyStored("mock prepare: no cloud writes")) as prepared:
            await self.pl.execute_save(ctx)
        prepared.assert_awaited_once()
        self.assertEqual(ctx.parsed.episodes, [106])
        self.assertEqual(ctx.source_rule["episodes_from"], "message")
        self.assertEqual(ctx.state, TaskState.SKIPPED)
        self.assertNotIn("媒体库中存在太多同集候选", "\n".join(ctx.trail))


if __name__ == "__main__":
    unittest.main()
