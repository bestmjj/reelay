"""Full-season Aliyun manual copy: copy every episode in the chosen source version.

All calls use an isolated fake cloud, never a real account or remote mutation.
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from reelay.core.contracts import Decision, NeedsManual, ParsedResource, PipelineContext, RawMessage, TaskState
from reelay.providers.storage import SHARE_ROOT_DIRECT, ShareStorage
from tests.fakes import WebCase


def season_source(folder="dovi-folder", path="Season 01 DoVi", episodes=range(1, 47)):
    files = []
    for episode in episodes:
        name = ("光阴之外 - S01E46 - 第 46 集 - 2160p.mp4"
                if episode == 46 else f"光阴之外.S01E{episode:02d}.mp4")
        files.append({
            "id": f"{folder}-{episode}", "name": name,
            "size": 500_000_000 + episode, "sha1": None, "is_dir": False,
            "share_folder_id": folder, "share_path": path,
        })
    return files


class Folder:
    def __init__(self):
        self.entries = [{
            "id": "existing-e01", "name": "光阴之外 - S01E01 - 2160p.mp4",
            "size": 999, "is_dir": False, "parent_id": "target-season",
        }]

    def get_attr(self, file_id):
        if file_id != "target-season":
            raise RuntimeError("Unexpected target folder")
        return {"id": file_id, "name": "Season 01", "is_dir": True}

    def iterdir(self, folder_id):
        if folder_id != "target-season":
            raise RuntimeError("Unexpected target folder")
        return list(self.entries)


class ForceAllApiTests(WebCase):
    CONFIG = {
        "save_dirs": {"动漫": "aliyun:target-season"},
        "defaults": {"save_to": "动漫", "storage": "aliyun",
                     "parser": "regex", "approval_mode": "manual"},
        "sources": [],
    }

    def setUp(self):
        super().setUp()
        self.ctx = PipelineContext(RawMessage(
            "@shareAliyun", "e46-announcement",
            "光阴之外 (2026) 更新至46集 https://www.alipan.com/s/gsQB8YFoLZQ",
            extra={"manual_import": True, "manual_review": True,
                   "manual_unlabeled": True},
        ))
        self.ctx.parsed = ParsedResource(
            is_media=True, title="光阴之外", year=2026, media_type="anime",
            season=1, episodes=[46], share_code="gsQB8YFoLZQ",
            raw_fields={"share_provider": "aliyun"},
        )
        self.ctx.source_rule = {
            "storage": "aliyun", "save_to": "动漫",
            "episodes_from": "message", "approval_mode": "manual",
        }
        self.ctx.state, self.ctx.decision = TaskState.PENDING_APPROVAL, Decision.ASK
        self.app.state.store.save_task(self.ctx)
        self.url = f"/api/tasks/{self.ctx.task_id}"
        self.files = season_source()
        self.folder = Folder()
        self.storage = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        for patched in (
            patch.object(self.storage, "ready", return_value=True),
            patch.object(self.storage, "_share_files", side_effect=lambda *a, **kw: list(self.files)),
            patch.object(self.storage, "client", SimpleNamespace(drive_id="resource-drive")),
            patch.object(self.storage, "library", return_value=self.folder),
            patch.object(self.storage, "dir_path",
                         return_value="/动漫/Dad/国产动漫/光阴之外 (2026)/Season 01"),
        ):
            patched.start()
            self.addCleanup(patched.stop)

    def enable(self):
        response = self.client.post(self.url + "/force-copy-all")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "pending_approval")
        return response.json()

    def choose(self, folder_id="dovi-folder"):
        preview = self.client.get(self.url + "/share-folders")
        self.assertEqual(preview.status_code, 200, preview.text)
        selected = self.client.post(self.url + "/share-folder", json={
            "folder_id": folder_id, "remember": True,
        })
        self.assertEqual(selected.status_code, 200, selected.text)
        return self.app.state.store.get_task(self.ctx.task_id)

    def approve(self):
        return self.client.post(self.url + "/approve", json={
            "existing_folder_id": "target-season",
            "existing_drive_id": "resource-drive",
            "destination_mode": "exact",
        })

    def test_all_46_including_e46_ignore_old_message_scope_and_dedup(self):
        self.assertIn("force_copy_all", self.client.get(self.url).json()["actions"])
        self.enable()
        self.assertEqual(self.approve().status_code, 409)
        selected = self.choose()
        pinned = selected.ns("task")["force_copy_all"]["source_files"]
        self.assertEqual(len(pinned), 46)
        self.assertEqual([f["episode"] for f in pinned], list(range(1, 47)))
        # This is a *one-time* source version choice, not a preference for
        # future automatic updates (even if UI sent remember=True).
        pref = self.app.state.store.state_get("share_folder_preferences", "items", {})
        self.assertFalse(pref)
        # Old dedup ledger says E01 and E46 exist. Explicit full copy must
        # copy both rather than selecting the announcement's E46 only.
        selected.ns("storage")["stored_episodes"] = [[1, 1], [1, 46]]
        picked = ShareStorage._select(selected, list(self.files))
        self.assertEqual([f["episode"] for f in picked["files"]], list(range(1, 47)))
        self.assertEqual(picked["pairs"], [[1, ep] for ep in range(1, 47)])
        saved = self.client.patch(self.url + "/parsed", json={"values": {
            "title": "光阴之外", "media_type": "anime", "year": 2026,
            "season": 1, "episodes": [46],
        }})
        self.assertEqual(saved.status_code, 200, saved.text)
        response = self.approve()
        self.assertEqual(response.status_code, 200, response.text)
        queued = self.app.state.store.get_task(self.ctx.task_id)
        self.assertEqual(queued.state, TaskState.QUEUED)
        self.assertEqual(queued.source_rule["episodes_from"], "share")
        self.assertEqual(queued.ns("task")["force_copy_all"]["approved_files"], pinned)
        self.assertFalse(queued.ns("storage").get("manifest"))
        self.assertEqual(len(self.folder.entries), 1)  # no deletion or overwrite

    def test_version_changed_after_source_selection_fails_before_queue(self):
        self.enable()
        self.choose()
        self.files[6]["size"] += 1
        response = self.approve()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("已经改变", response.text)
        self.assertEqual(self.app.state.store.get_task(self.ctx.task_id).state,
                         TaskState.PENDING_APPROVAL)

    def test_mixed_seasons_and_two_versions_cannot_be_selected_as_one(self):
        self.enable()
        self.files.append({
            "id": "season-02-file", "name": "光阴之外.S02E01.mp4",
            "size": 2000, "is_dir": False, "share_folder_id": "dovi-folder",
            "share_path": "Season 01 DoVi",
        })
        response = self.client.post(self.url + "/share-folder", json={
            "folder_id": "dovi-folder", "remember": False,
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.app.state.store.get_task(self.ctx.task_id).state,
                         TaskState.PENDING_APPROVAL)
        self.files.pop()
        self.files.append({
            "id": "alternate-01", "name": "光阴之外.S01E01.mkv",
            "size": 999_999, "is_dir": False, "share_folder_id": "dovi-folder",
            "share_path": "Season 01 DoVi",
        })
        result = self.client.post(self.url + "/share-folder", json={
            "folder_id": "dovi-folder", "remember": False,
        })
        self.assertEqual(result.status_code, 409, result.text)

    def test_unrecognized_or_oversized_share_cannot_copy_entire_folder(self):
        self.enable()
        self.files.append({
            "id": "unknown-47", "name": "4K.mp4",
            "size": 1_000_000, "is_dir": False, "share_folder_id": "dovi-folder",
            "share_path": "Season 01 DoVi",
        })
        self.assertEqual(self.client.post(self.url + "/share-folder", json={
            "folder_id": "dovi-folder", "remember": False,
        }).status_code, 409)
        self.files = season_source(episodes=range(1, 102))
        self.assertEqual(self.client.post(self.url + "/share-folder", json={
            "folder_id": "dovi-folder", "remember": False,
        }).status_code, 409)

    def test_root_direct_folder_can_mean_all_files_in_share_root(self):
        self.enable()
        self.files = season_source(folder="root", path="", episodes=range(1, 4))
        selected = self.choose(folder_id=SHARE_ROOT_DIRECT)
        self.assertEqual(len(selected.ns("task")["force_copy_all"]["source_files"]), 3)
        response = self.approve()
        self.assertEqual(response.status_code, 200, response.text)

    def test_manual_done_task_generates_new_pending_copy_without_changing_done(self):
        done = self.app.state.store.get_task(self.ctx.task_id)
        done.state, done.decision = TaskState.DONE, Decision.SAVE
        done.result = {"storage_verified": True}
        done.ns("storage")["provider"] = "aliyun"
        self.app.state.store.save_task(done)
        self.assertIn("force_copy_all", self.client.get(self.url).json()["actions"])
        result = self.client.post(self.url + "/force-copy-all")
        self.assertEqual(result.status_code, 200, result.text)
        child = result.json()["task_id"]
        self.assertNotEqual(child, done.task_id)
        self.assertEqual(self.app.state.store.get_task(child).state, TaskState.PENDING_APPROVAL)
        original = self.app.state.store.get_task(done.task_id)
        self.assertEqual(original.state, TaskState.DONE)
        self.assertTrue(original.result["storage_verified"])
        self.assertEqual(self.client.post(self.url + "/force-copy-all").status_code, 409)

    def test_worker_uses_whole_pinned_source_before_sending(self):
        self.enable()
        self.choose()
        self.assertEqual(self.approve().status_code, 200)
        queued = self.app.state.store.get_task(self.ctx.task_id)
        seen = {}

        async def mock_prepare(ctx):
            seen["chosen"] = ShareStorage._select(ctx, list(self.files))
            seen["stored"] = list(ctx.ns("storage").get("stored_episodes") or [])
            raise NeedsManual("stop in test before any cloud write")

        with patch.object(self.storage, "prepare", new=mock_prepare), patch.object(
                self.storage, "save", new_callable=AsyncMock) as save:
            asyncio.run(self.app.state.pipeline.execute_save(queued))
        self.assertEqual(save.call_count, 0)
        self.assertEqual(seen["stored"], [])
        self.assertEqual(len(seen["chosen"]["files"]), 46)
        self.assertEqual(self.app.state.store.get_task(self.ctx.task_id).state, TaskState.FAILED)


if __name__ == "__main__":
    unittest.main()
