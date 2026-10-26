"""An E247 copy from unrelated '4K.mp4' is not a verified E247 identity."""
from __future__ import annotations

import hashlib
import unittest

from reelay.core import works
from reelay.core.contracts import ParsedResource, PipelineContext, RawMessage, TaskState, NeedsManual
from reelay.core.store import Store
from reelay.providers.storage import SHARE_ROOT_DIRECT, _select_video_items
from tests.fakes import WebCase


class WrongCopyQuarantineTests(WebCase):
    CONFIG = {"save_dirs": {"动漫": "aliyun:targetfolder"},
              "defaults": {"storage": "aliyun", "save_to": "动漫"},
              "sources": []}

    def setUp(self):
        super().setUp()
        self.ctx = PipelineContext(RawMessage("@shareAliyun", "247",
                                              "吞噬星空 4K更新至247集"))
        self.ctx.parsed = ParsedResource(is_media=True, media_type="tv", title="吞噬星空",
                                         year=2020, season=1, episodes=[247],
                                         share_code="wrong-share",
                                         raw_fields={"share_provider": "aliyun", "tmdbid": 101172})
        self.ctx.state = TaskState.DONE
        self.ctx.source_rule = {"storage": "aliyun", "episodes_from": "message"}
        self.ctx.save_dir_alias = "动漫"
        self.ctx.save_dir_cid = "targetfolder"
        work = works.of(self.ctx.parsed)
        drive_id = "resource-drive"
        scope = hashlib.sha256(drive_id.encode()).hexdigest()
        self.ctx.ns("storage")["identity"] = {
            "provider": "aliyun", "cid": f"resource_{scope}_targetfolder",
            "share_code": "wrong-share", "title": "吞噬星空", "season": 1,
            "episodes": [247], "work": work, "pairs": [[1, 247]],
        }
        self.ctx.ns("storage")["manifest"] = {
            "to_cid": "targetfolder", "staging_cid": "original-stage",
            "drive_id": drive_id,
            "files": [{"id": "bad-source", "name": "4K.mp4", "size": 4040000000,
                       "share_path": "洛DA", "share_folder_id": "unrelated",
                       "season": 1, "episode": 247}],
            "pairs": [[1, 247]], "season": 1, "episodes": [247],
        }
        self.ctx.ns("storage")["landed"] = [{
            "id": "wrong-target-file", "name": "吞噬星空 - S01E247 - 第 247 集 - 2160p.mp4",
            "size": 4040000000,
        }]
        self.ctx.result = {"storage_verified": True}
        self.app.state.store.save_task(self.ctx)
        self.app.state.store.finish_transfer(self.ctx)
        self.app.state.store.save_task(self.ctx)
        self.work = work
        self.cid = f"resource_{scope}_targetfolder"
        self.store = self.app.state.store
        self.url = f"/api/tasks/{self.ctx.task_id}"

    def test_audit_exposes_original_wrong_source_without_claiming_video_identity(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.text)
        audit = response.json()["source_audit"]
        self.assertTrue(audit["suspect"])
        self.assertFalse(audit["content_verified"])
        self.assertEqual(audit["entries"][0]["source_path"], "洛DA/4K.mp4")
        self.assertEqual(audit["entries"][0]["episode"], 247)
        self.assertIsNone(audit["entries"][0]["filename_episode"])
        self.assertEqual(audit["entries"][0]["size"], 4040000000)
        self.assertIn("quarantine_wrong_source", response.json()["actions"])
        read = self.client.get(self.url + "/source-audit")
        self.assertTrue(read.json()["suspect"])
        self.assertEqual(self.store.stored_episode_pairs(self.work, self.cid, "aliyun"), [[1, 247]])

    def test_quarantine_is_local_and_reversible_without_restoring_bad_dedup(self):
        # A different episode remains legitimate and must not be touched.
        other = Store.episode_key(self.work, 1, 246)
        from reelay.core.store import DEDUP_EPISODE
        self.store.mark(DEDUP_EPISODE, "aliyun:" + self.cid + ":" + other, "other-task")
        self.assertEqual(self.store.stored_episode_pairs(self.work, self.cid, "aliyun"), [[1, 246], [1, 247]])
        self.assertEqual(self.store.stored_episode_pairs_in_drive(self.work, "aliyun", "resource-drive"),
                         [[1, 246], [1, 247]])

        response = self.client.post(self.url + "/quarantine-wrong-copy", json={
            "reason": "实际转存源是洛DA/4K.mp4，内容不是吞噬星空第247集",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["active"])
        self.assertEqual(len(response.json()["ledger_before"]), 2)
        self.assertEqual(self.store.blocked_episode_pairs(self.work, "aliyun"), [[1, 247]])
        self.assertEqual(self.store.stored_episode_pairs(self.work, self.cid, "aliyun"), [[1, 246]])
        self.assertEqual(self.store.stored_episode_pairs_in_drive(self.work, "aliyun", "resource-drive"), [[1, 246]])
        self.assertEqual(self.store.done_snapshots()[0]["task_id"], self.ctx.task_id)
        self.assertEqual(self.app.state.service.library()["groups"], [])
        task = self.client.get(self.url).json()
        self.assertEqual(task["state"], "done")
        self.assertTrue(task["source_audit"]["quarantine"]["active"])
        self.assertIn("resolve_quarantine", task["actions"])
        self.assertNotIn("quarantine_wrong_source", task["actions"])
        self.assertEqual(self.client.post(self.url + "/quarantine-wrong-copy",
                                          json={"reason": "该任务已经隔离，不能再次隔离"}).status_code, 409)
        selected = ParsedResource(is_media=True, media_type="tv", title="吞噬星空",
                                   season=1, episodes=[247])
        with self.assertRaisesRegex(NeedsManual, "错片"):
            _select_video_items(
                [{"id": "right", "name": "247 4K.mp4", "share_ancestor_ids": [],
                  "share_folder_id": "root", "size": 1620000000}],
                selected, scope="message", share_folder_id=SHARE_ROOT_DIRECT,
                blocked_episodes=self.store.blocked_episode_pairs(self.work, "aliyun"))
        self.assertEqual(self.client.post(self.url + "/resolve-wrong-copy",
                                          json={"confirmation": "尚未处理错片"}).status_code, 409)
        ok = self.client.post(self.url + "/resolve-wrong-copy",
                              json={"confirmation": "我已核对并处理错片"})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertFalse(ok.json()["active"])
        self.assertIsNotNone(ok.json()["resolved_at"])
        self.assertEqual(self.store.blocked_episode_pairs(self.work, "aliyun"), [])
        # The wrong old E247 claim is intentionally NOT restored.
        self.assertEqual(self.store.stored_episode_pairs(self.work, self.cid, "aliyun"), [[1, 246]])
        self.assertEqual(self.store.get_task(self.ctx.task_id).state, TaskState.DONE)

    def test_quarantine_rejects_multifile_or_unfinished_task(self):
        self.ctx.ns("storage")["manifest"]["files"].append({
            "id": "extra", "name": "248 4K.mp4", "episode": 248})
        self.store.save_task(self.ctx)
        bad = self.client.post(self.url + "/quarantine-wrong-copy", json={"reason": "需要核对不同视频"})
        self.assertEqual(bad.status_code, 409, bad.text)
        self.ctx.state = TaskState.FAILED
        self.store.save_task(self.ctx)
        bad = self.client.post(self.url + "/quarantine-wrong-copy", json={"reason": "错误不能直接转存"})
        self.assertEqual(bad.status_code, 409, bad.text)


if __name__ == "__main__":
    unittest.main()
