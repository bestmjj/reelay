"""Completed Aliyun task restoration: live audit, immutable DONE, no duplicate copies."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from reelay.core.contracts import ParsedResource, PipelineContext, RawMessage, TaskState
from reelay.providers.aliyun import AliyunAPIError
from tests.fakes import WebCase


class Cloud:
    def __init__(self):
        self.exists11 = False
        self.moved11 = False
        self.uncertain11 = False
        self.error11 = False
        self.files = {
            "old10": {"id": "old10", "name": "K狂王 - S01E10.mp4",
                      "parent_id": "season-folder", "size": 1024, "is_dir": False},
            "old11": {"id": "old11", "name": "K狂王 - S01E11.mp4",
                      "parent_id": "season-folder", "size": 2048, "is_dir": False},
        }

    def get_attr(self, fid):
        if fid == "folder-base":
            return {"id": fid, "name": "影视", "parent_id": "root", "is_dir": True}
        if fid in ("cat-folder", "show-folder", "season-folder"):
            return {"id": fid, "is_dir": True}
        if fid == "old11":
            if self.error11:
                raise RuntimeError("timeout")
            if not self.exists11:
                raise AliyunAPIError(404, "NotFound.File")
            return {**self.files[fid], "parent_id": "moved" if self.moved11 else "season-folder",
                    "size": 500 if self.uncertain11 else 2048}
        if fid in self.files:
            return self.files[fid]
        raise AliyunAPIError(404, "NotFound.File")

    def iterdir(self, cid):
        if cid == "folder-base":
            return [{"id": "cat-folder", "name": "动漫", "is_dir": True}]
        if cid == "cat-folder":
            return [{"id": "show-folder", "name": "K狂王", "is_dir": True}]
        if cid == "show-folder":
            return [{"id": "season-folder", "name": "Season 01", "is_dir": True}]
        if cid == "season-folder":
            return [self.files["old10"],
                    *([self.get_attr("old11")] if self.exists11 and not self.moved11 else []),
                    *([self.files["new11"]] if "new11" in self.files else []),
                    *([self.files["alternate11"]] if "alternate11" in self.files else [])]
        return []


class SuccessfulRepairWebTests(WebCase):
    CONFIG = {"save_dirs": {"默认": "aliyun:folder-base"},
              "defaults": {"storage": "aliyun", "save_to": "默认", "parser": "regex"},
              "sources": []}

    def setUp(self):
        super().setUp()
        self.cloud = Cloud()
        self.ctx = PipelineContext(RawMessage(
            "@shareAliyun", "10-11", "K狂王 第一季 https://www.alipan.com/s/gsQB8YFoLZQ"))
        self.ctx.parsed = ParsedResource(
            is_media=True, title="K狂王", media_type="anime", year=2026,
            season=1, episodes=[10, 11], share_code="gsQB8YFoLZQ",
            resolution="2160p", raw_fields={"share_provider": "aliyun"})
        self.ctx.state = TaskState.DONE
        self.ctx.source_rule = {"storage": "aliyun", "save_to": "默认",
                                "episodes_from": "share"}
        self.ctx.save_dir_cid, self.ctx.save_dir_alias = "folder-base", "默认"
        self.ctx.result = {"storage_verified": True, "landed": [
            {"id": "old10", "name": "10 4K.mp4"},
            {"id": "old11", "name": "11 4K.mp4"}]}
        self.ctx.meta["storage"] = {
            "provider": "aliyun", "phase": "stored",
            "manifest": {
                "drive_id": "resource-drive",
                "files": [
                    {"id": "source10", "name": "10 4K.mp4", "size": 1024,
                     "season": 1, "episode": 10},
                    {"id": "source11", "name": "11 4K.mp4", "size": 2048,
                     "season": 1, "episode": 11},
                ],
                "season": 1, "episodes": [10, 11],
            },
            "landed": self.ctx.result["landed"],
        }
        self.ctx.meta["organize"] = {
            "plan": [
                {"src_id": "old10", "src_name": "10 4K.mp4", "size": 1024,
                 "target_dir": "动漫/K狂王/Season 01", "target_name": "K狂王 - S01E10.mp4"},
                {"src_id": "old11", "src_name": "11 4K.mp4", "size": 2048,
                 "target_dir": "动漫/K狂王/Season 01", "target_name": "K狂王 - S01E11.mp4"},
            ],
        }
        self.ctx.meta["library"] = {"relative_category": "动漫"}
        self.app.state.store.save_task(self.ctx)
        self.api = f"/api/tasks/{self.ctx.task_id}"
        self.storage = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        self.mocks = (
            patch.object(self.storage, "client", SimpleNamespace(drive_id="resource-drive")),
            patch.object(self.storage, "library", return_value=self.cloud),
            patch.object(self.storage, "dir_path", return_value="/影视"),
        )
        for mocker in self.mocks:
            mocker.start()
            self.addCleanup(mocker.stop)

    def inspect(self):
        return self.client.get(self.api + "/repair-check")

    def test_only_deleted_file_can_be_repaired_and_done_receipt_is_immutable(self):
        view = self.inspect()
        self.assertEqual(view.status_code, 200, view.text)
        self.assertEqual([r["old_id"] for r in view.json()["missing"]], ["old11"])
        self.assertEqual([r["old_id"] for r in view.json()["present"]], ["old10"])
        self.assertIn("repair_missing", self.client.get(self.api).json()["actions"])
        cannot_copy_existing = self.client.post(
            self.api + "/repair-missing", json={"missing_ids": ["old10"]})
        self.assertEqual(cannot_copy_existing.status_code, 409)
        made = self.client.post(self.api + "/repair-missing", json={"missing_ids": ["old11"]})
        self.assertEqual(made.status_code, 200, made.text)
        child = self.app.state.store.get_task(made.json()["task_id"])
        parent = self.app.state.store.get_task(self.ctx.task_id)
        self.assertEqual(parent.state, TaskState.DONE)
        self.assertEqual(parent.result["storage_verified"], True)
        self.assertEqual(child.state, TaskState.QUEUED)
        self.assertEqual(child.parsed.episodes, [11])
        self.assertEqual(child.ns("library")["direct_existing"]["mode"], "repair")
        self.assertEqual(child.ns("task")["repair"]["files"][0]["old_id"], "old11")
        self.assertEqual(child.ns("share_selection")["file_id"], "source11")
        self.assertEqual(parent.ns("task")["repair_children"], [child.task_id])
        again = self.client.post(self.api + "/repair-missing", json={"missing_ids": ["old11"]})
        self.assertEqual(again.status_code, 409)

    def test_file_moved_or_reappeared_is_not_falsely_missing(self):
        self.cloud.exists11 = True
        self.cloud.moved11 = True
        view = self.inspect()
        self.assertEqual(view.status_code, 200, view.text)
        self.assertEqual(len(view.json()["missing"]), 0)
        self.assertEqual(len(view.json()["present"]), 2)
        self.assertEqual(
            self.client.post(self.api + "/repair-missing", json={"missing_ids": ["old11"]}).status_code,
            409,
        )

    def test_alternative_name_same_episode_is_ambiguous_not_deleted(self):
        self.cloud.files["alternate11"] = {
            "id": "alternate11", "name": "K狂王.S01E11.1080p.mkv",
            "parent_id": "season-folder", "size": 1500, "is_dir": False,
        }
        report = self.inspect()
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(len(report.json()["uncertain"]), 1)
        self.assertFalse(report.json()["missing"])
        self.assertEqual(self.client.post(
            self.api + "/repair-missing", json={"missing_ids": ["old11"]}).status_code, 409)

    def test_uncertain_or_network_error_fails_closed(self):
        self.cloud.exists11 = True
        self.cloud.uncertain11 = True
        view = self.inspect()
        self.assertEqual(view.status_code, 200, view.text)
        self.assertEqual(len(view.json()["uncertain"]), 1)
        self.assertEqual(
            self.client.post(self.api + "/repair-missing", json={"missing_ids": ["old11"]}).status_code,
            409)
        self.cloud.error11 = True
        self.assertEqual(self.inspect().status_code, 409)

    def test_cloud_api_failure_never_claims_missing_or_starts_copy(self):
        # A cloud listing outage is not proof that any video was deleted.
        with patch.object(self.cloud, "iterdir",
                          side_effect=AliyunAPIError(503, "ServiceUnavailable")):
            check = self.inspect()
            self.assertEqual(check.status_code, 409, check.text)
            repair = self.client.post(self.api + "/repair-missing",
                                      json={"missing_ids": ["old11"]})
            self.assertEqual(repair.status_code, 409, repair.text)
        self.assertEqual(self.app.state.store.get_task(self.ctx.task_id).state,
                         TaskState.DONE)
        self.assertFalse(self.app.state.store.get_task(
            self.ctx.task_id).ns("task").get("repair_children"))

    def test_worker_stops_when_file_was_restored_after_queue(self):
        made = self.client.post(self.api + "/repair-missing", json={"missing_ids": ["old11"]})
        self.assertEqual(made.status_code, 200, made.text)
        self.cloud.exists11 = True
        child_id = made.json()["task_id"]
        with patch.object(self.storage, "save") as cloud_copy, \
                patch.object(self.storage, "prepare") as stage:
            child = self.app.state.store.get_task(child_id)
            asyncio.run(self.app.state.pipeline.execute_save(child))
        self.assertEqual(cloud_copy.call_count, 0)
        self.assertEqual(stage.call_count, 0)
        self.assertEqual(self.app.state.store.get_task(child_id).state, TaskState.FAILED)
        self.assertEqual(self.app.state.store.get_task(self.ctx.task_id).state, TaskState.DONE)

    def test_finished_repair_is_seen_as_existing_even_when_original_id_is_gone(self):
        queued = self.client.post(self.api + "/repair-missing", json={"missing_ids": ["old11"]})
        self.assertEqual(queued.status_code, 200, queued.text)
        child_id = queued.json()["task_id"]
        child = self.app.state.store.get_task(child_id)
        child.state = TaskState.DONE
        child.result = {"storage_verified": True}
        child.ns("organize")["plan"] = [{
            "src_id": "new11", "src_name": "11 4K.mp4", "size": 2048,
            "target_dir": "动漫/K狂王/Season 01",
            "target_name": "K狂王 - S01E11.mp4",
        }]
        self.app.state.store.save_task(child)
        self.cloud.files["new11"] = {
            "id": "new11", "name": "K狂王 - S01E11.mp4",
            "parent_id": "season-folder", "size": 2048, "is_dir": False,
        }
        report = self.inspect()
        self.assertEqual(report.status_code, 200, report.text)
        self.assertFalse(report.json()["missing"])
        self.assertEqual(len(report.json()["present"]), 2)

    def test_wrong_source_file_in_prepared_manifest_cannot_copy(self):
        queued = self.client.post(self.api + "/repair-missing", json={"missing_ids": ["old11"]})
        self.assertEqual(queued.status_code, 200, queued.text)
        child = self.app.state.store.get_task(queued.json()["task_id"])
        manifest = {
            "files": [{"id": "another-version", "name": "11 4K.mp4", "size": 2048,
                       "season": 1, "episode": 11}],
            "season": 1, "episodes": [11], "pairs": [[1, 11]],
            "staging_cid": "unused-stage", "to_cid": "folder-base",
            "drive_id": "resource-drive",
        }
        with patch.object(self.storage, "prepare", new_callable=AsyncMock,
                          return_value=manifest), patch.object(
                              self.storage, "save", new_callable=AsyncMock) as copy:
            asyncio.run(self.app.state.pipeline.execute_save(child))
        self.assertEqual(copy.call_count, 0)
        after = self.app.state.store.get_task(child.task_id)
        self.assertEqual(after.state, TaskState.FAILED)
        self.assertIn("源文件 ID", str(self.app.state.store.get_task_record(child.task_id)["error"]))
        self.assertEqual(self.app.state.store.get_task(self.ctx.task_id).state, TaskState.DONE)

    def test_wrong_drive_and_legacy_metadata_cannot_repair(self):
        with patch.object(self.storage, "client", SimpleNamespace(drive_id="backup-drive")):
            self.assertEqual(self.inspect().status_code, 409)
        stale = self.app.state.store.get_task(self.ctx.task_id)
        stale.ns("storage")["manifest"].pop("drive_id")
        self.app.state.store.save_task(stale)
        self.assertEqual(self.inspect().status_code, 409)


if __name__ == "__main__":
    unittest.main()
