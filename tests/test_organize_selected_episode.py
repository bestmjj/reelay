"""Regression for E18-only organization after a full E01-E18 share was copied.

No real cloud account and no new copy request: the subset is selected
from the immutable, already verified Aliyun transfer receipt.
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from reelay.core.contracts import Decision, ParsedResource, PipelineContext, RawMessage, TaskState
from reelay.plugins.organizer import Organizer
from tests.fakes import WebCase


class FakeExistingSeason:
    def __init__(self, landed):
        self.items = {str(f["id"]): dict(f) for f in landed}
        self.writes = []
        self.deleted = []
        self.items["existing-e18"] = {
            "id": "existing-e18", "name": "李熊猫 - S01E17 - 2160p.mp4",
            "size": 999, "parent_id": "season-dir", "is_dir": False,
        }
        self.items["season-dir"] = {
            "id": "season-dir", "name": "Season 01", "is_dir": True,
            "size": 0, "parent_id": "show-dir",
        }
        self.items["wrong-season-dir"] = {
            "id": "wrong-season-dir", "name": "Season 01", "is_dir": True,
            "size": 0, "parent_id": "other-dir",
        }

    def get_attr(self, file_id):
        return dict(self.items[str(file_id)])

    def iterdir(self, folder_id):
        return [dict(f) for f in self.items.values() if f["parent_id"] == folder_id]

    def rename(self, file_id, name):
        self.writes.append(("rename", str(file_id), name))
        self.items[str(file_id)]["name"] = name

    def move(self, file_id, target):
        self.writes.append(("move", str(file_id), str(target)))
        self.items[str(file_id)]["parent_id"] = str(target)

    def mkdir(self, parent, name):
        raise AssertionError("Existing Season directory must be reused")

    def delete(self, files):
        self.deleted += list(files)
        raise AssertionError("No cloud file may be deleted")


class DictState:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, val):
        self.data[key] = val

    def delete(self, key):
        self.data.pop(key, None)


class OnlyE18FromCopiedSeason(WebCase):
    CONFIG = {
        "save_dirs": {"动漫": "aliyun:season-dir"},
        "defaults": {"save_to": "动漫", "storage": "aliyun",
                     "parser": "regex", "approval_mode": "manual"},
        "sources": [],
    }

    def setUp(self):
        super().setUp()
        self.ctx = PipelineContext(RawMessage(
            "@shareAliyun", "e18-all-copied", "李熊猫（2026）4K 全18集",
        ))
        self.ctx.parsed = ParsedResource(
            is_media=True, media_type="tv", title="李熊猫", year=2026,
            season=1, episodes=[18], resolution="2160p",
            share_code="gsQB8YFoLZQ",
            raw_fields={"share_provider": "aliyun"},
        )
        self.ctx.source_rule = {"storage": "aliyun", "save_to": "动漫",
                                "approval_mode": "manual", "episodes_from": "message"}
        self.ctx.save_dir_alias, self.ctx.save_dir_cid = "动漫", "season-dir"
        self.ctx.state, self.ctx.decision = TaskState.NEED_MANUAL, Decision.SAVE
        files = [
            {"id": f"share-{ep}", "name": f"S01E{ep:02d}.4K.mp4",
             "size": 1_000_000 + ep, "season": 1, "episode": ep}
            for ep in range(1, 19)
        ]
        landed = [
            {"id": f"landed-{ep}", "name": item["name"], "size": item["size"],
             "sha1": None, "parent_id": "stage-dir", "is_dir": False}
            for ep, item in enumerate(files, 1)
        ]
        self.ctx.result = {"storage_verified": True, "landed": list(landed),
                           "verified": False}
        self.ctx.ns("storage").update({
            "provider": "aliyun", "phase": "stored", "landed": landed,
            "manifest": {"drive_id": "drive-resource", "staging_cid": "stage-dir",
                         "files": files, "season": 1, "episodes": list(range(1, 19))},
        })
        self.ctx.ns("organize")["error"] = (
            "文件集号 E01 不在已识别集数中：S01E01.4K.mp4"
        )
        self.ctx.ns("library")["direct_existing"] = {
            "id": "season-dir", "provider": "aliyun", "drive_id": "drive-resource",
            "path": "/李熊猫 (2026)/Season 01",
            "title": "李熊猫", "media_type": "tv",
            "pairs": [[1, 18]], "share_code": "gsQB8YFoLZQ", "mode": "exact",
        }
        self.app.state.store.save_task(self.ctx)
        self.url = f"/api/tasks/{self.ctx.task_id}"
        self.drive = FakeExistingSeason(landed)
        provider = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        for mocker in (
            patch.object(provider, "_assert_manifest_drive", return_value=None),
            patch.object(provider, "client", SimpleNamespace(drive_id="drive-resource")),
            patch.object(provider, "library", return_value=self.drive),
            patch.object(provider, "dir_path", side_effect=lambda cid: {
                "stage-dir": "/.reelay-test", "season-dir": "/李熊猫 (2026)/Season 01",
                "wrong-season-dir": "/其他剧 (2026)/Season 01",
            }.get(str(cid))),
        ):
            mocker.start()
            self.addCleanup(mocker.stop)

    def queue_e18(self):
        response = self.client.post(self.url + "/organize-single-episode", json={
            "existing_folder_id": "season-dir", "existing_drive_id": "drive-resource",
            "destination_mode": "exact",
        })
        self.assertEqual(response.status_code, 200, response.text)
        queued = self.app.state.store.get_task(self.ctx.task_id)
        self.assertEqual(queued.state, TaskState.QUEUED)
        return queued

    def test_only_e18_is_allowed_and_is_queued_without_copy(self):
        detail = self.client.get(self.url).json()
        self.assertIn("organize_single_episode", detail["actions"])
        self.assertEqual(self.queue_e18().ns("organize")["single_episode"]["source_id"],
                         "landed-18")
        self.assertEqual(len(self.drive.iterdir("stage-dir")), 18)
        self.assertEqual(self.drive.writes, [])
        self.assertEqual(self.client.post(self.url + "/organize-single-episode", json={
            "existing_folder_id": "season-dir", "existing_drive_id": "drive-resource",
        }).status_code, 409)

    def test_only_existing_e18_is_planned_and_other_17_untouched(self):
        queued = self.queue_e18()
        organizer = Organizer()
        asyncio.run(organizer.prepare(queued))
        plan = queued.ns("organize")["plan"]
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["src_id"], "landed-18")
        self.assertEqual(plan[0]["target_dir"], "")
        self.drive.items["existing-e18"]["name"] = plan[0]["target_name"]
        state = DictState()
        services = SimpleNamespace(
            state=lambda _: state,
            storage=lambda _: SimpleNamespace(library=lambda: self.drive),
        )
        with patch("reelay.plugins.organizer.runtime.services", return_value=services):
            self.assertTrue(asyncio.run(organizer.run(queued)))
        self.assertFalse(queued.result["verified"], "whole season must not be marked done")
        self.assertTrue(queued.result["storage_verified"])
        completed = queued.ns("organize")["partial_completed"]
        self.assertEqual((completed["season"], completed["episode"]), (1, 18))
        self.assertEqual(completed["remaining"], 17)
        self.assertEqual(completed["target_path"], "/李熊猫 (2026)/Season 01")
        self.assertEqual(completed["target_id"], "season-dir")
        self.assertEqual(len(self.drive.iterdir("stage-dir")), 17)
        self.assertFalse(any(action[1] != "landed-18" for action in self.drive.writes))
        self.assertNotEqual(self.drive.get_attr("landed-18")["name"], plan[0]["target_name"])
        self.assertTrue(self.drive.get_attr("landed-18")["name"].endswith(" (2).mp4"))
        self.assertEqual(self.drive.get_attr("existing-e18")["name"], plan[0]["target_name"])
        self.assertEqual(self.drive.deleted, [])

    def test_e18_missing_or_ambiguous_prevents_reorganization(self):
        for problem in ("missing", "ambiguous", "bad-season"):
            with self.subTest(problem=problem):
                original = self.app.state.store.get_task(self.ctx.task_id)
                items = original.ns("storage")["manifest"]["files"]
                if problem == "missing":
                    items.pop()
                elif problem == "ambiguous":
                    items.append(dict(items[-1], id="alternate-e18"))
                else:
                    items[-1]["season"] = 2
                self.app.state.store.save_task(original)
                detail = self.client.get(self.url).json()
                self.assertNotIn("organize_single_episode", detail["actions"])
                response = self.client.post(self.url + "/organize-single-episode", json={
                    "existing_folder_id": "season-dir", "existing_drive_id": "drive-resource",
                })
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.app.state.store.get_task(self.ctx.task_id).state,
                                 TaskState.NEED_MANUAL)
                self.app.state.store.save_task(self.ctx)

    def test_previous_success_moved_e18_to_wrong_folder_is_located_and_requeued(self):
        # Existing tasks could say "E18 organized" without recording which
        # Season folder was actually used. Verify the live file ID, not status.
        original = self.app.state.store.get_task(self.ctx.task_id)
        original.ns("organize")["partial_completed"] = {
            "source_id": "landed-18", "season": 1, "episode": 18,
            "remaining": 17,
        }
        self.app.state.store.save_task(original)
        self.drive.items["landed-18"]["parent_id"] = "wrong-season-dir"
        self.drive.items["landed-18"]["name"] = "李熊猫 - S01E18 - 第 18 集 - 2160p.mp4"
        located = self.client.get(self.url + "/single-episode-location")
        self.assertEqual(located.status_code, 200, located.text)
        self.assertEqual(located.json()["parent_path"], "/其他剧 (2026)/Season 01")
        self.assertIn("李熊猫 - S01E18", located.json()["file_path"])
        self.assertIn("organize_single_episode", self.client.get(self.url).json()["actions"])
        self.assertEqual(self.client.post(self.url + "/organize-single-episode", json={
            "existing_folder_id": "wrong-season-dir",
            "existing_drive_id": "drive-resource",
        }).status_code, 409)
        queued = self.queue_e18()
        self.assertEqual(queued.ns("library")["direct_existing"]["path"],
                         "/李熊猫 (2026)/Season 01")
        self.assertEqual(queued.ns("organize")["single_episode"]["source_id"], "landed-18")
        organizer = Organizer()
        state = DictState()
        services = SimpleNamespace(
            state=lambda _: state,
            storage=lambda _: SimpleNamespace(library=lambda: self.drive),
        )
        with patch("reelay.plugins.organizer.runtime.services", return_value=services):
            self.assertTrue(asyncio.run(organizer.run(queued)))
        self.assertEqual(self.drive.get_attr("landed-18")["parent_id"], "season-dir")
        self.assertEqual(len(self.drive.iterdir("stage-dir")), 17)
        self.assertEqual(queued.ns("organize")["partial_completed"]["target_path"],
                         "/李熊猫 (2026)/Season 01")
        self.assertEqual(self.drive.deleted, [])

    def test_explicit_target_and_live_id_required_before_reorganizing(self):
        self.assertEqual(self.client.post(self.url + "/organize-single-episode",
                         json={}).status_code, 422)
        self.assertEqual(self.client.post(self.url + "/organize-single-episode", json={
            "existing_folder_id": "season-dir", "existing_drive_id": "other-drive",
        }).status_code, 409)
        self.drive.items["landed-18"]["size"] = 0
        result = self.client.get(self.url + "/single-episode-location")
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.client.post(self.url + "/organize-single-episode", json={
            "existing_folder_id": "season-dir",
            "existing_drive_id": "drive-resource",
        }).status_code, 409)
        self.assertEqual(self.app.state.store.get_task(self.ctx.task_id).state,
                         TaskState.NEED_MANUAL)
        self.assertEqual(self.drive.writes, [])

    def test_ignore_stuck_transferred_pack_preserves_all_cloud_files(self):
        # This is exactly the previously unescapable state: E01-E18 are
        # copied and verified, but only E18 was identified for organization.
        self.assertIn("dismiss", self.client.get(self.url).json()["actions"])
        ctx = self.app.state.store.get_task(self.ctx.task_id)
        ctx.ns("storage")["identity"] = {
            "share_code": "gsQB8YFoLZQ", "provider": "aliyun",
            "cid": "resource-target", "title": "李熊猫", "season": 1,
            "episodes": list(range(1, 19)),
        }
        self.app.state.store.finish_transfer(ctx)
        store = self.app.state.store
        # Another valid task's same-episode entry is NEVER ours to erase.
        other_key = "other-target:other-show|S01|E01"
        from reelay.core.store.base import DEDUP_EPISODE, DEDUP_MESSAGE, DEDUP_SHARE
        store.mark(DEDUP_EPISODE, other_key, "another-task")
        store.mark(DEDUP_MESSAGE, "@shareAliyun:e18-all-copied", ctx.task_id)
        with store._lock:
            current = store._conn.execute(
                "SELECT COUNT(*) FROM dedup WHERE task_id=? AND kind IN (?,?)",
                (ctx.task_id, DEDUP_SHARE, DEDUP_EPISODE)).fetchone()[0]
        self.assertEqual(current, 19)
        answer = self.client.post(self.url + "/reject")
        self.assertEqual(answer.status_code, 200, answer.text)
        closed = store.get_task(ctx.task_id)
        self.assertEqual(closed.state, TaskState.SKIPPED)
        self.assertEqual(closed.ns("task")["skip"]["kind"], "preserved_transfer")
        self.assertNotIn("organize_single_episode", self.client.get(self.url).json()["actions"])
        # Read-only location stays available after dismissal; no new writes.
        location = self.client.get(self.url + "/single-episode-location")
        self.assertEqual(location.status_code, 200, location.text)
        self.assertEqual(location.json()["file_id"], "landed-18")
        self.assertTrue(closed.result["storage_verified"])
        self.assertEqual(len(self.drive.iterdir("stage-dir")), 18)
        self.assertEqual(self.drive.writes, [])
        self.assertEqual(self.drive.deleted, [])
        with store._lock:
            remaining = store._conn.execute(
                "SELECT kind,key FROM dedup WHERE task_id=?",
                (ctx.task_id,)).fetchall()
            unrelated = store._conn.execute(
                "SELECT COUNT(*) FROM dedup WHERE kind=? AND key=?",
                (DEDUP_EPISODE, other_key)).fetchone()[0]
        self.assertEqual({row["kind"] for row in remaining}, {DEDUP_MESSAGE})
        self.assertEqual(unrelated, 1)
        self.assertEqual(self.client.post(self.url + "/reject").status_code, 409)

    def test_cannot_ignore_unknown_or_unverified_cloud_transfer(self):
        original = self.app.state.store.get_task(self.ctx.task_id)
        for phase, verified in (("receiving", True), ("stored", False)):
            with self.subTest(phase=phase, verified=verified):
                ctx = self.app.state.store.get_task(self.ctx.task_id)
                ctx.ns("storage")["phase"] = phase
                ctx.result["storage_verified"] = verified
                self.app.state.store.save_task(ctx)
                self.assertNotIn("dismiss", self.client.get(self.url).json()["actions"])
                self.assertEqual(self.client.post(self.url + "/reject").status_code, 409)
                self.assertEqual(self.app.state.store.get_task(ctx.task_id).state,
                                 TaskState.NEED_MANUAL)
        self.app.state.store.save_task(original)

    def test_post_organize_flow_does_not_call_storage_save(self):
        queued = self.queue_e18()
        provider = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        async def record(ctx):
            self.assertEqual(ctx.ns("organize")["single_episode"]["source_id"], "landed-18")
            ctx.result["verified"] = False
        with patch.object(provider, "_assert_manifest_drive", return_value=None), patch.object(
                self.app.state.pipeline, "_resolve_directory", return_value=None), patch.object(
                self.app.state.pipeline, "_organize", new=AsyncMock(side_effect=record)
        ) as org, patch.object(provider, "save", new_callable=AsyncMock) as copy:
            asyncio.run(self.app.state.pipeline.execute_save(queued))
        self.assertEqual(copy.call_count, 0)
        self.assertEqual(org.call_count, 1)


if __name__ == "__main__":
    unittest.main()
