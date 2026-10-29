"""Safety regression: bulk recovery of E01-E13 with an existing E14."""
from __future__ import annotations

import asyncio
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from reelay.core import repair, works
from reelay.core.contracts import Decision, NeedsManual, ParsedResource, PipelineContext, RawMessage, TaskState
from reelay.core.store.base import DEDUP_EPISODE
from reelay.providers.storage import _select_video_items
from tests.fakes import WebCase


class FolderInventory:
    def __init__(self):
        self.files = [
            {"id": "saved14", "name": "狂王 - S02E14 - 第14集 - 2160p.mp4",
             "size": 1007320000, "parent_id": "season-folder", "is_dir": False},
        ]
        self.writes = 0

    def get_attr(self, file_id):
        if file_id != "season-folder":
            raise RuntimeError("no unexpected file query")
        return {"id": file_id, "is_dir": True, "name": "Season 02"}

    def iterdir(self, folder_id):
        if folder_id != "season-folder":
            raise RuntimeError("wrong target folder")
        return list(self.files)


def source_files():
    return [
        {"id": f"dovi-{ep}", "name": f"狂王.S02E{ep:02d}.2160p.mp4",
         "size": 500000000 + ep, "sha1": None, "is_dir": False,
         "share_folder_id": "dovi-folder", "share_path": "第二季 DoVi"}
        for ep in range(1, 15)
    ]


class ExactFolderScanTests(unittest.TestCase):
    def setUp(self):
        self.folder = FolderInventory()
        self.storage = SimpleNamespace(library=lambda: self.folder,
                                       client=SimpleNamespace(drive_id="resource-drive"))

    def test_real_e14_does_not_block_missing_e01_to_e13(self):
        report = repair.scan_exact_season_episodes(
            self.storage, "season-folder", 2, list(range(1, 15)))
        self.assertEqual(report["present"], [14])
        self.assertEqual(report["missing"], list(range(1, 14)))
        self.assertEqual(self.folder.writes, 0)

    def test_unclear_video_or_wrong_season_fails_closed(self):
        for filename in ("4K.mp4", "狂王 - S01E05.mp4"):
            with self.subTest(filename=filename):
                self.folder.files.append({"id": "unexpected", "name": filename,
                                          "size": 100, "is_dir": False})
                with self.assertRaisesRegex(ValueError, "核对"):
                    repair.scan_exact_season_episodes(
                        self.storage, "season-folder", 2, list(range(1, 14)))
                self.folder.files.pop()

    def test_mixed_source_versions_cannot_be_silently_merged(self):
        parsed = ParsedResource(is_media=True, title="狂王", season=2,
                                media_type="anime", episodes=list(range(1, 14)))
        listing = source_files() + [
            {**file, "id": f"hdr-{ep}", "share_folder_id": "hdr-folder",
             "share_path": "第二季 HDR.Vivid"}
            for ep, file in enumerate(source_files(), 1)
        ]
        with self.assertRaises(NeedsManual):
            _select_video_items(listing, parsed, only_episodes=list(range(1, 14)))
        selected = _select_video_items(
            listing, parsed, only_episodes=list(range(1, 14)),
            share_folder_id="dovi-folder", share_folder_path="第二季 DoVi",
        )
        self.assertEqual([f["id"] for f in selected["files"]],
                         [f"dovi-{ep}" for ep in range(1, 14)])
        self.assertEqual(selected["pairs"], [[2, ep] for ep in range(1, 14)])


class BatchMissingApiTests(WebCase):
    CONFIG = {
        "save_dirs": {"默认": "aliyun:season-folder"},
        "defaults": {"storage": "aliyun", "save_to": "默认",
                     "approval_mode": "manual", "parser": "regex"},
        "sources": [],
    }

    def setUp(self):
        super().setUp()
        self.folder = FolderInventory()
        self.ctx = PipelineContext(RawMessage(
            "@shareAliyun", "old-14", "狂王 第二季 E14 https://www.alipan.com/s/gsQB8YFoLZQ",
        ))
        self.ctx.parsed = ParsedResource(
            is_media=True, title="狂王", media_type="anime", year=2026,
            season=2, episodes=[14], share_code="gsQB8YFoLZQ",
            raw_fields={"share_provider": "aliyun"},
        )
        self.ctx.state, self.ctx.decision = TaskState.SKIPPED, Decision.SKIP
        self.ctx.source_rule = {
            "storage": "aliyun", "save_to": "默认",
            "approval_mode": "manual", "episodes_from": "share",
        }
        self.ctx.ns("task")["skip"] = {"kind": "duplicate", "reason": "历史记录说 14 集均已入库"}
        self.app.state.store.save_task(self.ctx)
        self.url = f"/api/tasks/{self.ctx.task_id}"
        self.storage = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        patchers = [
            patch.object(self.storage, "ready", return_value=True),
            patch.object(self.storage, "_share_files", return_value=source_files()),
            patch.object(self.storage, "client", SimpleNamespace(drive_id="resource-drive")),
            patch.object(self.storage, "library", return_value=self.folder),
            patch.object(self.storage, "dir_path",
                         return_value="/动漫/Dad/国产动漫/狂王 (2026)/Season 02"),
        ]
        for mocker in patchers:
            mocker.start()
            self.addCleanup(mocker.stop)

    def launch(self, episodes=None):
        return self.client.post(
            self.url + "/force-missing-range",
            json={"episodes": episodes if episodes is not None else list(range(1, 14))},
        )

    def choose_and_approve(self):
        self.assertEqual(self.launch().status_code, 200)
        preview = self.client.get(self.url + "/share-folders")
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertIn("dovi-folder", [x["folder_id"] for x in preview.json()["folders"]])
        choice = self.client.post(self.url + "/share-folder",
                                  json={"folder_id": "dovi-folder", "remember": False})
        self.assertEqual(choice.status_code, 200, choice.text)
        approve = self.client.post(self.url + "/approve", json={
            "existing_folder_id": "season-folder",
            "existing_drive_id": "resource-drive",
            "destination_mode": "exact",
        })
        self.assertEqual(approve.status_code, 200, approve.text)
        return self.app.state.store.get_task(self.ctx.task_id)

    def test_batch_force_needs_explicit_source_version_and_final_folder(self):
        detail = self.client.get(self.url).json()
        self.assertIn("force_missing_range", detail["actions"])
        self.assertEqual(self.launch([]).status_code, 422)
        self.assertEqual(self.launch(list(range(1, 102))).status_code, 422)
        started = self.launch()
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["state"], "pending_approval")
        self.assertNotIn("select_episodes", started.json()["actions"])
        self.assertEqual(self.client.post(self.url + "/approve", json={}).status_code, 409)
        self.assertEqual(self.client.post(self.url + "/approve", json={
            "existing_folder_id": "season-folder",
            "existing_drive_id": "resource-drive", "destination_mode": "exact",
        }).status_code, 409)
        self.assertEqual(self.client.post(self.url + "/force-missing-range",
                                           json={"episodes": [1]}).status_code, 409)
        self.assertEqual(self.app.state.store.get_task(self.ctx.task_id).state,
                         TaskState.PENDING_APPROVAL)

    def test_full_approval_keeps_original_history_and_never_overwrites_e14(self):
        queued = self.choose_and_approve()
        self.assertEqual(queued.state, TaskState.QUEUED)
        self.assertEqual(queued.parsed.episodes, list(range(1, 14)))
        self.assertEqual(queued.ns("share_selection")["folder_id"], "dovi-folder")
        self.assertEqual(queued.ns("task")["force_missing_range"]["episodes"], list(range(1, 14)))
        self.assertEqual(queued.ns("library")["direct_existing"]["mode"], "exact")
        self.assertFalse(queued.ns("storage").get("landed"))
        self.assertFalse(queued.result)
        self.assertEqual([x["id"] for x in self.folder.files], ["saved14"])
        self.assertEqual(self.folder.writes, 0)

    def test_unknown_cloud_file_blocks_approval_and_does_not_queue(self):
        self.folder.files.append({"id": "unlabeled", "name": "4K.mp4",
                                  "size": 10000, "is_dir": False})
        self.assertEqual(self.launch().status_code, 200)
        self.assertEqual(self.client.post(self.url + "/share-folder",
                          json={"folder_id": "dovi-folder", "remember": False}).status_code, 200)
        result = self.client.post(self.url + "/approve", json={
            "existing_folder_id": "season-folder",
            "existing_drive_id": "resource-drive",
            "destination_mode": "exact",
        })
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.app.state.store.get_task(self.ctx.task_id).state,
                         TaskState.PENDING_APPROVAL)

    def test_worker_ignores_old_dedup_but_excludes_real_cloud_file(self):
        queued = self.choose_and_approve()
        # E09 appeared after approval; queued job must now omit it. The
        # historical ledger still falsely marks all E01-E14 as stored.
        self.folder.files.append({
            "id": "saved09", "name": "狂王 - S02E09 - 第9集 - 2160p.mp4",
            "parent_id": "season-folder", "size": 700, "is_dir": False,
        })
        drive_scope = hashlib.sha256(b"resource-drive").hexdigest()
        dedup_cid = "resource_" + drive_scope + "_season-folder"
        work = works.of(queued.parsed, queued.meta.get("tmdb"))
        for episode in range(1, 15):
            key = f"aliyun:{dedup_cid}:{self.app.state.store.episode_key(work, 2, episode)}"
            self.app.state.store.mark(DEDUP_EPISODE, key, "old-done")
        observed = {}

        async def no_copy(ctx):
            observed["only"] = list(ctx.ns("share_selection")["only_episodes"])
            observed["stored"] = set(map(tuple, ctx.ns("storage")["stored_episodes"]))
            raise NeedsManual("test stops before write")

        with patch.object(self.storage, "prepare", new=no_copy), patch.object(
                self.storage, "save", new_callable=AsyncMock) as real_copy:
            asyncio.run(self.app.state.pipeline.execute_save(queued))
        self.assertEqual(real_copy.call_count, 0)
        self.assertEqual(observed["only"], [*range(1, 9), *range(10, 14)])
        self.assertNotIn((2, 1), observed["stored"])
        self.assertIn((2, 9), observed["stored"])
        self.assertIn((2, 14), observed["stored"])
        self.assertEqual(self.app.state.store.get_task(self.ctx.task_id).state,
                         TaskState.FAILED)

    def test_done_episode14_creates_separate_missing_range_draft(self):
        done = self.app.state.store.get_task(self.ctx.task_id)
        done.state, done.decision = TaskState.DONE, Decision.SAVE
        done.result = {"storage_verified": True, "landed": [
            {"id": "saved14", "name": "狂王 - S02E14 - 2160p.mp4"},
        ]}
        done.ns("storage")["provider"] = "aliyun"
        done.ns("task").pop("skip", None)
        self.app.state.store.save_task(done)
        self.assertIn("force_missing_range", self.client.get(self.url).json()["actions"])
        created = self.launch()
        self.assertEqual(created.status_code, 200, created.text)
        new_id = created.json()["task_id"]
        self.assertNotEqual(new_id, done.task_id)
        draft = self.app.state.store.get_task(new_id)
        self.assertEqual(draft.state, TaskState.PENDING_APPROVAL)
        self.assertEqual(draft.parsed.episodes, list(range(1, 14)))
        self.assertFalse(draft.ns("storage").get("manifest"))
        self.assertEqual(draft.ns("task")["force_missing_range"]["season"], 2)
        parent = self.app.state.store.get_task(done.task_id)
        self.assertEqual(parent.state, TaskState.DONE)
        self.assertTrue(parent.result["storage_verified"])
        self.assertEqual(parent.ns("task")["bulk_missing_children"], [new_id])
        self.assertEqual(self.launch().status_code, 409)  # prevent second active draft
        draft_url = f"/api/tasks/{new_id}"
        selected = self.client.post(draft_url + "/share-folder", json={
            "folder_id": "dovi-folder", "remember": False,
        })
        self.assertEqual(selected.status_code, 200, selected.text)
        approve = self.client.post(draft_url + "/approve", json={
            "existing_folder_id": "season-folder",
            "existing_drive_id": "resource-drive", "destination_mode": "exact",
        })
        self.assertEqual(approve.status_code, 200, approve.text)
        self.assertEqual(self.app.state.store.get_task(done.task_id).state, TaskState.DONE)
        self.assertEqual(self.app.state.store.get_task(new_id).state, TaskState.QUEUED)

    def test_prepared_subset_survives_worker_restart(self):
        queued = self.choose_and_approve()
        # The live folder gains E09 before preparation. A valid persisted
        # manifest therefore contains only twelve of the thirteen requested
        # episodes. Worker restart must NOT reject that legitimate subset.
        effective = [*range(1, 9), *range(10, 14)]
        queued.parsed.episodes = effective
        queued.ns("share_selection")["only_episodes"] = effective
        queued.ns("task")["force_missing_range"]["effective_episodes"] = effective
        storage_ns = queued.ns("storage")
        storage_ns.update({
            "provider": "aliyun",
            "phase": "ready", "resource_key": "test:resource:bulk",
            "manifest": {
                "drive_id": "resource-drive",
                "staging_cid": "stage-bulk",
                "files": [
                    {"id": f"dovi-{ep}", "name": f"狂王.S02E{ep:02d}.2160p.mp4",
                     "size": 500000000 + ep, "season": 2, "episode": ep}
                    for ep in effective
                ],
                "season": 2, "episodes": effective,
            },
        })
        self.app.state.store.save_task(queued)
        with patch.object(self.app.state.store, "claim_resource", return_value=False) as claim:
            asyncio.run(self.app.state.pipeline.execute_save(queued))
        self.assertTrue(claim.called, "valid recovery should reach the resource claim")
        self.assertEqual(self.app.state.store.get_task(queued.task_id).state,
                         TaskState.QUEUED)

    def test_wrong_season_or_wrong_provider_cannot_use_force(self):
        wrong = self.app.state.store.get_task(self.ctx.task_id)
        wrong.source_rule["storage"] = "p115"
        self.app.state.store.save_task(wrong)
        self.assertNotIn("force_missing_range", self.client.get(self.url).json()["actions"])
        self.assertEqual(self.launch().status_code, 409)


if __name__ == "__main__":
    unittest.main()
