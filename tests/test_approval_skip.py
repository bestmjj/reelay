"""Approval means QUEUED, and duplicate skips must explain the reason."""
from __future__ import annotations

import unittest

from reelay.core.contracts import Decision, ParsedResource, PipelineContext, RawMessage, TaskState
from tests.fakes import WebCase


class ApprovalReasonAPITests(WebCase):
    CONFIG = {
        "save_dirs": {"剧集": "aliyun:existing-destination"},
        "defaults": {"save_to": "剧集", "storage": "aliyun", "approval_mode": "manual"},
        "sources": [{"bot": "@shareAliyun", "storage": "aliyun", "save_to": "剧集"}],
    }

    def new(self, number, state=TaskState.PENDING_APPROVAL):
        ctx = PipelineContext(RawMessage("@shareAliyun", str(number), "小城良方 (2026) 更新14集"))
        ctx.parsed = ParsedResource(is_media=True, media_type="tv", title="小城良方",
                                    year=2026, season=1, episodes=[14], share_code="valid",
                                    confidence=1, raw_fields={"share_provider": "aliyun"})
        ctx.state = state
        ctx.decision = Decision.ASK
        ctx.source_rule = {"storage": "aliyun", "approval_mode": "manual", "save_to": "剧集"}
        self.app.state.store.save_task(ctx)
        return ctx

    def test_approval_queues_without_claiming_transfer_or_user_ignore(self):
        item = self.new("approval")
        response = self.client.post(f"/api/tasks/{item.task_id}/approve", json={})
        self.assertEqual(response.status_code, 200, response.text)
        stored = self.app.state.store.get_task(item.task_id)
        self.assertEqual(stored.state, TaskState.QUEUED)
        self.assertTrue(stored.meta["task"]["user_approved"])
        self.assertIsNone(stored.meta["task"].get("skip"))
        self.assertFalse(stored.meta.get("storage", {}).get("manifest"))
        self.assertFalse(stored.result)
        detail = self.client.get(f"/api/tasks/{item.task_id}").json()
        self.assertEqual(detail["state"], "queued")
        self.assertIsNone(detail["skip_kind"])

    def test_user_ignored_is_distinct_from_auto_duplicate(self):
        ignored = self.new("ignored")
        r = self.client.post(f"/api/tasks/{ignored.task_id}/reject")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get(f"/api/tasks/{ignored.task_id}").json()["skip_kind"], "user")
        self.assertEqual(self.app.state.store.get_task(ignored.task_id).state, TaskState.SKIPPED)

        duplicate = self.new("duplicate", state=TaskState.SKIPPED)
        duplicate.ns("task")["user_approved"] = True
        duplicate.ns("task")["skip"] = {
            "kind": "duplicate",
            "reason": "分享内 S01E01-E14 已经在当前网盘保存，本次没有重新转存",
        }
        self.app.state.store.save_task(duplicate)
        rows = self.client.get("/api/tasks", params={"state": "skipped"}).json()["items"]
        named = {x["task_id"]: x for x in rows}
        self.assertEqual(named[ignored.task_id]["skip_kind"], "user")
        self.assertEqual(named[duplicate.task_id]["skip_kind"], "duplicate")
        self.assertIn("没有重新转存", named[duplicate.task_id]["skip_reason"])
        detail = self.client.get(f"/api/tasks/{duplicate.task_id}").json()
        self.assertEqual(detail["skip_kind"], "duplicate")
        self.assertEqual(detail["skip_reason"], named[duplicate.task_id]["skip_reason"])
        self.assertFalse(detail["context"]["result"])

    def test_old_duplicate_without_metadata_is_recognized_from_durable_trail(self):
        old = self.new("legacy", state=TaskState.SKIPPED)
        old.log("执行前去重命中：分享内第 1 季 14 集均已入库")
        self.app.state.store.save_task(old)
        items = self.client.get("/api/tasks", params={"state": "skipped"}).json()["items"]
        row = next(x for x in items if x["task_id"] == old.task_id)
        self.assertEqual(row["skip_kind"], "duplicate")
        self.assertIn("去重命中", row["skip_reason"])
        detail = self.client.get(f"/api/tasks/{old.task_id}").json()
        self.assertEqual(detail["skip_kind"], "duplicate")


if __name__ == "__main__":
    unittest.main()
