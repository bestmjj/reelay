"""Shared-link HDR.Vivid/DoVi selection: no cloud writes, no mixed-version transfers."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from reelay.core.contracts import AlreadyStored, NeedsManual, ParsedResource, PipelineContext, RawMessage, TaskState
from reelay.core.media import episode_of
from reelay.providers.aliyun import AliyunStorage
from reelay.providers.storage import (P115Storage, SHARE_ROOT_DIRECT, _announced_latest,
                                      _select_video_items, share_folder_choices, share_video_choices)
from tests.fakes import WebCase


def two_versions():
    out = []
    for branch, name in (("hdr-folder", "Season 1 (HDR.Vivid)"),
                         ("dovi-folder", "Season 1 (DoVi)")):
        for ep in (13, 14):
            out.append({"id": f"{branch}-{ep}", "name": f"小城良方.S01E{ep:02d}.2160p.mkv",
                        "size": 200 + ep, "sha1": None, "is_dir": False,
                        "share_folder_id": branch, "share_path": name})
    return out


class FolderChoiceTests(unittest.TestCase):
    def setUp(self):
        self.parsed = ParsedResource(is_media=True, media_type="tv", title="小城良方",
                                     season=1, episodes=[14])

    def test_no_choice_blocks_duplicate_versions_in_different_folders(self):
        with self.assertRaisesRegex(NeedsManual, "选择一个分享版本"):
            _select_video_items(two_versions(), self.parsed, scope="message")
        with self.assertRaisesRegex(NeedsManual, "选择一个分享版本"):
            _select_video_items(two_versions(), self.parsed,
                                hints={"drop": [two_versions()[0]["name"]]})

    def test_all_old_do_vi_hdr_versions_are_skipped_before_source_choice(self):
        # Production regression: 尚公主 E11 already saved. A new share has
        # two season folders (DoVi / HDR.Vivid.60fps), but neither should
        # require human selection or create a FAILED task.
        files = two_versions()
        with self.assertRaisesRegex(AlreadyStored, "均已入库"):
            _select_video_items(files, self.parsed, scope="share",
                                stored=[[1, 13], [1, 14]])
        # The logic is identical for the message announcing only E14.
        with self.assertRaisesRegex(AlreadyStored, "均已入库"):
            _select_video_items(files, self.parsed, scope="message",
                                stored=[[1, 14]])

    def test_missing_episode_single_version_is_incrementally_copied(self):
        # E13/E14 have two old versions; E15 is available only in DoVi.
        # Do not let the old branches veto automatic filling of missing E15.
        files = [*two_versions(), {
            "id": "dovi-folder-15", "name": "小城良方.S01E15.2160p.mkv",
            "size": 215, "is_dir": False, "share_folder_id": "dovi-folder",
            "share_path": "Season 1 (DoVi)",
        }]
        selected = _select_video_items(
            files, self.parsed, scope="share", stored=[[1, 13], [1, 14]],
        )
        self.assertEqual(selected["pairs"], [[1, 15]])
        self.assertEqual([f["id"] for f in selected["files"]], ["dovi-folder-15"])

    def test_only_ambiguous_missing_episode_needs_a_version_choice(self):
        files = [*two_versions(),
                 *[{"id": f"{branch}-15", "name": "小城良方.S01E15.2160p.mkv",
                    "size": 215, "is_dir": False, "share_folder_id": branch,
                    "share_path": path}
                   for branch, path in (("dovi-folder", "Season 1 (DoVi)"),
                                        ("hdr-folder", "Season 1 (HDR.Vivid)"))]]
        with self.assertRaisesRegex(NeedsManual, "S01E015"):
            _select_video_items(files, self.parsed, scope="share",
                                stored=[[1, 13], [1, 14]])
        chosen = _select_video_items(
            files, self.parsed, scope="share",
            share_folder_id="dovi-folder", stored=[[1, 13], [1, 14]],
        )
        self.assertEqual(chosen["pairs"], [[1, 15]])
        self.assertEqual([f["id"] for f in chosen["files"]], ["dovi-folder-15"])

    def test_quarantined_episode_cannot_be_hidden_by_old_dedup(self):
        files = [*two_versions()]
        with self.assertRaisesRegex(NeedsManual, "曾被标记为错片"):
            _select_video_items(files, self.parsed, scope="share",
                                stored=[[1, 13], [1, 14]],
                                blocked_episodes=[[1, 14]])

    def test_unnumbered_video_never_silently_becomes_duplicate(self):
        files = [*two_versions(), {"id": "unknown-15", "name": "4K.mp4",
                 "size": 215, "is_dir": False, "share_folder_id": "unknown-folder",
                 "share_path": "Season 1 (unknown)"}]
        with self.assertRaises(NeedsManual):
            _select_video_items(files, self.parsed, scope="share",
                                stored=[[1, 13], [1, 14]])

    def test_dovi_or_hdr_choice_selects_exact_episodes_not_both(self):
        listing = two_versions()
        folders = share_folder_choices(listing)
        self.assertEqual([x["count"] for x in folders], [2, 2])
        self.assertEqual(len({x["folder_id"] for x in folders}), 2)
        for branch in ("dovi-folder", "hdr-folder"):
            with self.subTest(branch=branch):
                selected = _select_video_items(listing, self.parsed, scope="message",
                                               share_folder_id=branch)
                self.assertEqual([file["id"] for file in selected["files"]], [f"{branch}-14"])
                self.assertEqual(selected["pairs"], [[1, 14]])

    def test_missing_episode_in_selected_version_never_falls_back(self):
        listing = [file for file in two_versions() if file["id"] != "dovi-folder-14"]
        with self.assertRaisesRegex(NeedsManual, "14"):
            _select_video_items(listing, self.parsed, scope="message",
                                share_folder_id="dovi-folder")
        with self.assertRaisesRegex(NeedsManual, "已不存在"):
            _select_video_items(two_versions(), self.parsed, scope="message",
                                share_folder_id="removed-folder")

    def test_latest_announcement_must_exist_in_chosen_folder(self):
        listing = [file for file in two_versions() if file["id"] != "dovi-folder-14"]
        ctx = PipelineContext(RawMessage("@shareAliyun", "latest", "小城良方 [更新14集]"))
        ctx.parsed = self.parsed
        ctx.ns("share_selection")["folder_id"] = "dovi-folder"
        self.assertEqual(_announced_latest(ctx), 14)
        with self.assertRaisesRegex(NeedsManual, "选中版本缺少.*E14"):
            _select_video_items(listing, self.parsed, scope="share",
                                share_folder_id="dovi-folder",
                                required_latest=_announced_latest(ctx))
        with self.assertRaisesRegex(NeedsManual, "名称已改变"):
            _select_video_items(two_versions(), self.parsed, scope="message",
                                share_folder_id="dovi-folder",
                                share_folder_path="Season 1 (HDR.Vivid)")

    def test_duplicate_versions_within_the_chosen_folder_require_review(self):
        listing = [*two_versions(), {**two_versions()[-1], "id": "second-dovi-14",
                                    "name": "小城良方.S01E14.1080p.mkv"}]
        with self.assertRaisesRegex(NeedsManual, "多个文件"):
            _select_video_items(listing, self.parsed, scope="message", share_folder_id="dovi-folder")

    def test_distinct_seasons_in_distinct_folders_do_not_force_version_choice(self):
        files = [
            {"id": "s1", "name": "Show.S01E01.mkv", "share_folder_id": "season1",
             "share_path": "Season 1"},
            {"id": "s2", "name": "Show.S02E01.mkv", "share_folder_id": "season2",
             "share_path": "Season 2"},
        ]
        selection = _select_video_items(files, ParsedResource(is_media=True, media_type="tv"))
        self.assertEqual(selection["pairs"], [[1, 1], [2, 1]])

    def test_movie_versions_are_never_automatically_picked(self):
        files = [{**x, "name": f"Movie.2026.2160p.{x['share_folder_id']}.mkv"}
                 for x in two_versions() if x["id"].endswith("-13")]
        with self.assertRaisesRegex(NeedsManual, "选择一个分享版本"):
            _select_video_items(files, ParsedResource(media_type="movie"))
        selected = _select_video_items(files, ParsedResource(media_type="movie"),
                                       share_folder_id="dovi-folder")
        self.assertEqual(len(selected["files"]), 1)

    def test_aliyun_recursive_listing_preserves_top_level_branch_ids(self):
        class FakeAliyun:
            def share_token(self, share, pwd):
                return "readonly-share-token"

            def shared_children(self, share, token, parent):
                if parent == "root":
                    return [
                        {"id": "hdr-folder", "name": "Season 1 (HDR.Vivid)", "is_dir": True},
                        {"id": "dovi-folder", "name": "Season 1 (DoVi)", "is_dir": True},
                    ]
                return [{x["id"]: x["id"]}.get(x["id"]) and {
                    "id": x["id"], "name": x["name"], "is_dir": False, "size": x["size"],
                } for x in two_versions() if x["share_folder_id"] == parent]

        cloud = AliyunStorage()
        cloud.client = FakeAliyun()
        parsed = self.parsed
        ctx = PipelineContext(RawMessage("@shareAliyun", "folder", "小城良方"))
        ctx.parsed = parsed
        files = cloud._share_files(ctx)
        self.assertEqual(len(files), 4)
        self.assertEqual({x["share_folder_id"] for x in files}, {"hdr-folder", "dovi-folder"})
        self.assertEqual({x["share_path"] for x in files},
                         {"Season 1 (HDR.Vivid)", "Season 1 (DoVi)"})

    def test_storage_adapter_uses_chosen_branch_instead_of_merging(self):
        context = PipelineContext(RawMessage("@shareAliyun", "t", "小城良方 更新14集"))
        context.parsed = self.parsed
        context.ns("share_selection").update(folder_id="dovi-folder",
                                              path="Season 1 (DoVi)")
        # A share-scoped first import may take both E13 and E14 from DoVi.
        selected = AliyunStorage._select(context, two_versions())
        self.assertEqual([f["id"] for f in selected["files"]],
                         ["dovi-folder-13", "dovi-folder-14"])
        # A message-scoped latest-episode subscription must take E14 alone.
        context.source_rule["episodes_from"] = "message"
        selected = AliyunStorage._select(context, two_versions())
        self.assertEqual([f["id"] for f in selected["files"]], ["dovi-folder-14"])
        self.assertEqual(selected["pairs"], [[1, 14]])

    def test_p115_recursive_listing_also_preserves_folder_id(self):
        class Fake115:
            def iterdir(self, folder):
                if folder == 0:
                    return [{"id": 10, "name": "Season 1 HDR", "is_dir": True},
                            {"id": 20, "name": "Season 1 DV", "is_dir": True}]
                return [{"id": folder + 1, "name": "Show.S01E14.mkv",
                         "is_dir": False, "size": 200}]
        listing = P115Storage._collect_share_videos(Fake115())
        self.assertEqual({x["share_folder_id"] for x in listing}, {"10", "20"})



class WrongEpisodeAndNestedDirectoryTests(unittest.TestCase):
    """Real E247: root 247 4K (1.62GB), unrelated nested 4K (4.04GB)."""

    def setUp(self):
        self.parsed = ParsedResource(is_media=True, media_type="tv",
                                     title="吞噬星空", year=2020,
                                     season=1, episodes=[247])

    @staticmethod
    def listing():
        return [
            {"id": "correct-247", "name": "247 4K.mp4", "size": 1620000000,
             "is_dir": False, "share_path": "",
             "share_folder_id": "root", "share_ancestor_ids": []},
            {"id": "unrelated-4k", "name": "4K.mp4", "size": 4040000000,
             "is_dir": False, "share_path": "洛DA",
             "share_folder_id": "unrelated-folder",
             "share_ancestor_ids": ["unrelated-folder"]},
        ]

    def test_never_selects_wrong_unlabeled_four_gigabyte_file(self):
        listing = self.listing()
        self.assertEqual(episode_of(listing[0]["name"]), (None, 247))
        self.assertEqual(episode_of(listing[1]["name"]), (None, None))
        with self.assertRaisesRegex(NeedsManual, "分享不同目录混有"):
            _select_video_items(listing, self.parsed, scope="message")
        with self.assertRaisesRegex(NeedsManual, "分享不同目录混有"):
            _select_video_items(
                listing, self.parsed, scope="message",
                hints={"episodes": {"4K.mp4": (1, 247)}, "drop": ["247 4K.mp4"]})
        choices = share_folder_choices(listing)
        root = next(x for x in choices if x["folder_id"] == SHARE_ROOT_DIRECT)
        self.assertEqual(root["count"], 1)
        self.assertEqual(root["samples"][0]["name"], "247 4K.mp4")
        chosen = _select_video_items(listing, self.parsed, scope="message",
                                     share_folder_id=SHARE_ROOT_DIRECT)
        self.assertEqual([x["id"] for x in chosen["files"]], ["correct-247"])
        self.assertEqual(chosen["pairs"], [[1, 247]])
        with self.assertRaisesRegex(NeedsManual, "无法从文件名识别集号"):
            _select_video_items(listing, self.parsed, scope="message",
                                share_folder_id="unrelated-folder")
        with self.assertRaisesRegex(NeedsManual, "AI 不能"):
            _select_video_items(listing, self.parsed, scope="message",
                                share_folder_id="unrelated-folder",
                                hints={"episodes": {"4K.mp4": (1, 247)}})

    def test_exact_file_id_e247_never_copies_the_other_four_gigabyte_video(self):
        files = self.listing()
        choices = share_video_choices(files, self.parsed)
        valid = [choice for choice in choices if choice["selectable"]]
        self.assertEqual([choice["file_id"] for choice in valid], ["correct-247"])
        self.assertFalse(next(choice for choice in choices
                              if choice["file_id"] == "unrelated-4k")["selectable"])
        selection = _select_video_items(
            files, self.parsed, scope="message",
            share_file_id="correct-247", share_file_name="247 4K.mp4",
            share_file_path="", share_file_size=1620000000,
        )
        self.assertEqual([item["id"] for item in selection["files"]], ["correct-247"])
        self.assertEqual(selection["pairs"], [[1, 247]])
        for changed in (
                {**files[0], "size": 4040000000},
                {**files[0], "name": "wrong.mp4"},
                {**files[0], "share_path": "other-folder"},
        ):
            listing = [changed, files[1]]
            with self.assertRaisesRegex(NeedsManual, "已变化"):
                _select_video_items(
                    listing, self.parsed, scope="message",
                    share_file_id="correct-247", share_file_name="247 4K.mp4",
                    share_file_path="", share_file_size=1620000000)
        with self.assertRaisesRegex(NeedsManual, "集数"):
            _select_video_items(
                files, self.parsed, scope="message",
                share_file_id="unrelated-4k", share_file_name="4K.mp4",
                share_file_path="洛DA", share_file_size=4040000000)
        with self.assertRaisesRegex(NeedsManual, "曾被标记为错片"):
            _select_video_items(
                files, self.parsed, scope="message",
                share_file_id="correct-247", share_file_name="247 4K.mp4",
                share_file_path="", share_file_size=1620000000,
                blocked_episodes=[[1, 247]])

    def test_nested_wrapper_lists_both_hdr_versions_and_empty_directories(self):
        listing = [
            {"id": "wrapper", "name": "X)小城良方", "is_dir": True,
             "share_path": "X)小城良方", "share_ancestor_ids": ["wrapper"]},
            {"id": "hdr", "name": "Season 1 (HDR.Vivid)", "is_dir": True,
             "share_path": "X)小城良方/Season 1 (HDR.Vivid)",
             "share_ancestor_ids": ["wrapper", "hdr"]},
            {"id": "dovi", "name": "Season 1 (DoVi)", "is_dir": True,
             "share_path": "X)小城良方/Season 1 (DoVi)",
             "share_ancestor_ids": ["wrapper", "dovi"]},
            {"id": "empty", "name": "Extra", "is_dir": True,
             "share_path": "X)小城良方/Extra",
             "share_ancestor_ids": ["wrapper", "empty"]},
        ]
        for leaf, dirname in (("hdr", "HDR.Vivid"), ("dovi", "DoVi")):
            for ep in (13, 14):
                listing.append({
                    "id": f"{leaf}-{ep}", "name": f"小城良方.S01E{ep:02d}.2160p.mkv",
                    "size": 1000000000, "is_dir": False,
                    "share_folder_id": leaf, "share_ancestor_ids": ["wrapper", leaf],
                    "share_path": f"X)小城良方/Season 1 ({dirname})",
                })
        choices = share_folder_choices(listing)
        self.assertEqual(len(choices), 4)
        self.assertEqual({x["path"] for x in choices},
                         {"X)小城良方", "X)小城良方/Season 1 (HDR.Vivid)",
                          "X)小城良方/Season 1 (DoVi)", "X)小城良方/Extra"})
        self.assertEqual(next(x["count"] for x in choices if x["folder_id"] == "wrapper"), 4)
        self.assertEqual(next(x["count"] for x in choices if x["folder_id"] == "dovi"), 2)
        self.assertFalse(next(x["selectable"] for x in choices if x["folder_id"] == "empty"))
        parsed = ParsedResource(is_media=True, media_type="tv",
                                title="小城良方", season=1, episodes=[14])
        with self.assertRaisesRegex(NeedsManual, "更具体"):
            _select_video_items(listing, parsed, scope="message",
                                share_folder_id="wrapper")
        selected = _select_video_items(
            listing, parsed, scope="message", share_folder_id="dovi",
            share_folder_path="X)小城良方/Season 1 (DoVi)")
        self.assertEqual([f["id"] for f in selected["files"]], ["dovi-14"])
        with self.assertRaisesRegex(NeedsManual, "名称已改变"):
            _select_video_items(
                listing, parsed, scope="message", share_folder_id="dovi",
                share_folder_path="X)小城良方/Season 1 (HDR.Vivid)")

    def test_aliyun_listing_returns_all_nested_directory_rows_and_files(self):
        class Fake:
            def share_token(self, share, code):
                return "token"

            def shared_children(self, share, token, parent):
                dirs = {
                    "root": [
                        {"id": "wrapper", "name": "X)小城良方", "is_dir": True},
                        {"id": "root247", "name": "247 4K.mp4", "is_dir": False,
                         "size": 1620000000},
                    ],
                    "wrapper": [
                        {"id": "hdr", "name": "Season 1 (HDR.Vivid)", "is_dir": True},
                        {"id": "dovi", "name": "Season 1 (DoVi)", "is_dir": True},
                        {"id": "empty", "name": "Some empty folder", "is_dir": True},
                    ],
                    "hdr": [{"id": "hdr14", "name": "小城良方.S01E14.mkv",
                             "is_dir": False, "size": 111}],
                    "dovi": [{"id": "dv14", "name": "小城良方.S01E14.mkv",
                              "is_dir": False, "size": 222}],
                    "empty": [],
                }
                return dirs[parent]
        cloud = AliyunStorage()
        cloud.client = Fake()
        ctx = PipelineContext(RawMessage("@share", "123", "小城良方"))
        ctx.parsed = self.parsed
        videos = cloud._share_files(ctx)
        self.assertEqual(len(videos), 3)
        self.assertEqual(next(x for x in videos if x["id"] == "dv14")["share_ancestor_ids"],
                         ["wrapper", "dovi"])
        all_rows = cloud._share_files(ctx, include_dirs=True)
        self.assertEqual(len(all_rows), 7)
        choices = share_folder_choices(all_rows)
        self.assertEqual({x["folder_id"] for x in choices},
                         {"wrapper", "hdr", "dovi", "empty", SHARE_ROOT_DIRECT})
        self.assertEqual(next(x["count"] for x in choices if x["folder_id"] == "hdr"), 1)
        self.assertEqual(next(x["count"] for x in choices if x["folder_id"] == "empty"), 0)

class ManualE247FileChoiceAPITests(WebCase):
    CONFIG = {
        "save_dirs": {"动漫": "aliyun:existing-destination"},
        "defaults": {"storage": "aliyun", "parser": "regex", "save_to": "动漫",
                     "approval_mode": "manual"},
        "sources": [{"bot": "@shareAliyun", "storage": "aliyun", "parser": "regex",
                     "approval_mode": "manual"}],
    }

    def setUp(self):
        super().setUp()
        raw = RawMessage("@shareAliyun", "manual-e247",
                         "https://www.alipan.com/s/gsQB8YFoLZQ 选择分享根目录中的 247",
                         extra={"manual_import": True, "manual_unlabeled": True,
                                "manual_review": True})
        self.ctx = PipelineContext(raw)
        self.ctx.state = TaskState.PENDING_APPROVAL
        self.ctx.parsed = ParsedResource(
            is_media=True, share_code="gsQB8YFoLZQ",
            raw_fields={"share_provider": "aliyun",
                        "share_url": "https://www.alipan.com/s/gsQB8YFoLZQ"},
        )
        self.ctx.source_rule = {"storage": "aliyun", "approval_mode": "manual",
                                "episodes_from": "share"}
        self.app.state.store.save_task(self.ctx)
        storage = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        self._ready_patch = patch.object(storage, "ready", return_value=True)
        self._listing_patch = patch.object(storage, "_share_files",
                                           return_value=WrongEpisodeAndNestedDirectoryTests.listing())
        self._ready_patch.start()
        self._listing_patch.start()
        self.addCleanup(self._ready_patch.stop)
        self.addCleanup(self._listing_patch.stop)

    def test_review_correction_exact_file_and_approval_never_copy_4gb_video(self):
        url = f"/api/tasks/{self.ctx.task_id}"
        no_title = self.client.post(url + "/approve")
        self.assertEqual(no_title.status_code, 409)
        self.assertIn("真实片名", no_title.text)
        with patch.object(self.app.state.pipeline, "refresh_task", return_value=False):
            corrected = self.client.patch(url + "/parsed", json={"values": {
                "title": "吞噬星空", "media_type": "anime", "year": 2020,
                "season": 1, "episodes": [247], "resolution": "2160p",
            }})
        self.assertEqual(corrected.status_code, 200, corrected.text)
        saved = self.app.state.store.get_task(self.ctx.task_id)
        self.assertEqual(saved.source_rule["episodes_from"], "message")
        self.assertEqual(saved.parsed.episodes, [247])
        before_file = self.client.post(url + "/approve")
        self.assertEqual(before_file.status_code, 409)
        self.assertIn("源文件", before_file.text)
        preview = self.client.get(url + "/share-folders")
        self.assertEqual(preview.status_code, 200, preview.text)
        choices = preview.json()["files"]
        self.assertEqual([f["file_id"] for f in choices if f["selectable"]],
                         ["correct-247"])
        self.assertEqual(self.client.post(url + "/share-file",
                            json={"file_id": "unrelated-4k"}).status_code, 409)
        selected = self.client.post(url + "/share-file",
                                    json={"file_id": "correct-247"})
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual(selected.json()["selected_file_id"], "correct-247")
        chosen = self.app.state.store.get_task(self.ctx.task_id)
        receipt = AliyunStorage._select(chosen,
                                        WrongEpisodeAndNestedDirectoryTests.listing())
        self.assertEqual([file["id"] for file in receipt["files"]], ["correct-247"])
        self.assertEqual(receipt["pairs"], [[1, 247]])
        # Link-only manual submissions must explicitly choose the Aliyun
        # target even after the exact E247 source file has been selected.
        self.assertEqual(self.client.post(url + "/approve").status_code, 409)
        approve = self.client.post(url + "/approve", json={"save_dir_alias": "动漫"})
        self.assertEqual(approve.status_code, 200, approve.text)
        queued = self.app.state.store.get_task(self.ctx.task_id)
        self.assertEqual(queued.save_dir_alias, "动漫")
        self.assertEqual(queued.state, TaskState.QUEUED)
        self.assertFalse(queued.ns("storage").get("manifest"))
        self.assertIsNone(queued.result)

    def test_existing_aliyun_episode_folder_is_verified_and_no_nested_series_created(self):
        """The already saved episodes' *final* folder is the destination for E247."""
        url = f"/api/tasks/{self.ctx.task_id}"
        with patch.object(self.app.state.pipeline, "refresh_task", return_value=False):
            corrected = self.client.patch(url + "/parsed", json={"values": {
                "title": "吞噬星空", "media_type": "anime", "year": 2020,
                "season": 1, "episodes": [247], "resolution": "2160p",
            }})
        self.assertEqual(corrected.status_code, 200, corrected.text)
        selected = self.client.post(url + "/share-file", json={"file_id": "correct-247"})
        self.assertEqual(selected.status_code, 200, selected.text)
        storage = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        folder = "existing-season-01"
        actual = "/影视/动漫/Dad/吞噬星空/Season 01"

        def attrs(fid):
            return {"id": fid, "name": "Season 01", "is_dir": True, "parent_id": "existing-show"}

        with patch.object(storage, "client", SimpleNamespace(drive_id="resource-drive-e247")), \
                patch.object(storage, "dir_path", return_value=actual) as directory, \
                patch.object(storage, "library", return_value=SimpleNamespace(
                    get_attr=attrs, iterdir=lambda fid: [
                        {"id": "old-246", "name": "吞噬星空 - S01E246.mp4", "is_dir": False},
                    ])):
            no_drive = self.client.post(url + "/approve", json={
                "existing_folder_id": folder,
            })
            self.assertEqual(no_drive.status_code, 422, no_drive.text)
            wrong_drive = self.client.post(url + "/approve", json={
                "existing_folder_id": folder, "existing_drive_id": "backup-drive",
            })
            self.assertEqual(wrong_drive.status_code, 409, wrong_drive.text)
            with patch.object(storage, "dir_path", return_value="/影视/动漫/牧神记/Season 01"):
                wrong_work = self.client.post(url + "/approve", json={
                    "existing_folder_id": folder, "existing_drive_id": "resource-drive-e247",
                })
            self.assertEqual(wrong_work.status_code, 409, wrong_work.text)
            # A show root containing only season subfolders is not the
            # final directory where the earlier episode videos live.
            with patch.object(storage, "library", return_value=SimpleNamespace(
                    get_attr=attrs, iterdir=lambda fid: [
                        {"id": "child-season", "name": "Season 01", "is_dir": True},
                    ])):
                wrong_leaf = self.client.post(url + "/approve", json={
                    "existing_folder_id": folder, "existing_drive_id": "resource-drive-e247",
                })
            self.assertEqual(wrong_leaf.status_code, 409, wrong_leaf.text)
            self.assertIn("没有已有视频", wrong_leaf.text)
            self.assertEqual(self.app.state.store.get_task(self.ctx.task_id).state,
                             TaskState.PENDING_APPROVAL)

            approved = self.client.post(url + "/approve", json={
                "existing_folder_id": folder, "existing_drive_id": "resource-drive-e247",
            })
            self.assertEqual(approved.status_code, 200, approved.text)
            ctx = self.app.state.store.get_task(self.ctx.task_id)
            self.assertEqual(ctx.state, TaskState.QUEUED)
            self.assertEqual(ctx.ns("library")["direct_existing"]["path"], actual)
            self.assertFalse(ctx.ns("storage").get("landed"))
            # Worker resolves the same, still-existing resource-drive folder
            # rather than the category alias, and must fail if it is renamed.
            self.app.state.pipeline._resolve_directory(ctx)
            self.assertEqual(ctx.save_dir_cid, folder)
            self.assertEqual(directory.call_count >= 2, True)
            with patch.object(storage, "dir_path", return_value="/影视/动漫/Dad/已移动"):
                with self.assertRaisesRegex(ValueError, "无法再次核验"):
                    self.app.state.pipeline._resolve_directory(ctx)
            # The organizer retains the E247 filename but NEVER creates
            # another "吞噬星空 (2020)/Season 01" inside the selected folder.
            from reelay.plugins.organizer import LibraryRouter, Organizer
            ctx.ns("storage")["landed"] = [{
                "id": "landed247", "name": "247 4K.mp4", "size": 1620000000, "is_dir": False,
            }]
            ctx.ns("storage")["manifest"] = {
                "files": [{"name": "247 4K.mp4", "season": 1, "episode": 247}]
            }
            with self.app.state.pipeline.scope(ctx):
                asyncio.run(LibraryRouter().run(ctx))
                asyncio.run(Organizer().prepare(ctx))
            plan = ctx.ns("organize")["plan"]
            self.assertEqual(len(plan), 1)
            self.assertEqual(plan[0]["target_dir"], "")
            self.assertIn("S01E247", plan[0]["target_name"])


class FolderChoiceAPITests(WebCase):
    CONFIG = {"save_dirs": {"默认": "aliyun:library"},
              "defaults": {"storage": "aliyun", "save_to": "默认"},
              "sources": []}

    def setUp(self):
        super().setUp()
        self.ctx = PipelineContext(RawMessage("@shareAliyun", "readonly", "小城良方"))
        self.ctx.parsed = ParsedResource(is_media=True, media_type="tv",
                                         title="小城良方", year=2026, season=1,
                                         episodes=[14], share_code="share-abc",
                                         raw_fields={"share_provider": "aliyun"})
        self.ctx.state = TaskState.FAILED
        self.ctx.source_rule = {"storage": "aliyun", "episodes_from": "message"}
        self.app.state.store.save_task(self.ctx)
        cloud = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        patch_ready = patch.object(cloud, "ready", return_value=True)
        patch_share = patch.object(cloud, "_share_files", return_value=two_versions())
        patch_ready.start()
        patch_share.start()
        self.addCleanup(patch_ready.stop)
        self.addCleanup(patch_share.stop)

    def test_choose_dovi_and_remember_only_for_same_share(self):
        view = self.client.get(f"/api/tasks/{self.ctx.task_id}/share-folders")
        self.assertEqual(view.status_code, 200, view.text)
        self.assertEqual({x["path"] for x in view.json()["folders"]},
                         {"Season 1 (HDR.Vivid)", "Season 1 (DoVi)"})
        selected = self.client.post(f"/api/tasks/{self.ctx.task_id}/share-folder",
                                    json={"folder_id": "dovi-folder", "remember": True})
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual(selected.json()["selected_folder_id"], "dovi-folder")
        snapshot = self.app.state.store.get_task(self.ctx.task_id)
        self.assertEqual(snapshot.meta["share_selection"]["folder_id"], "dovi-folder")
        preferences = self.app.state.store.state_get("share_folder_preferences", "items", {})
        self.assertEqual(preferences["aliyun:share-abc"]["folder_id"], "dovi-folder")
        self.assertEqual(snapshot.state, TaskState.FAILED)
        self.assertFalse(snapshot.meta.get("storage", {}).get("manifest"))
        self.assertFalse(snapshot.meta.get("storage", {}).get("landed"))
        # Different task using the SAME shared link sees the remembered folder.
        later = PipelineContext(RawMessage("@shareAliyun", "later", "小城良方 更新14集"))
        later.parsed = self.ctx.parsed
        later.state = TaskState.FAILED
        later.source_rule = dict(self.ctx.source_rule)
        self.app.state.store.save_task(later)
        preview = self.client.get(f"/api/tasks/{later.task_id}/share-folders")
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["selected_folder_id"], "dovi-folder")
        self.assertTrue(preview.json()["saved_preference"])

    def test_invalid_or_stale_folder_and_transfer_checkpoint_are_rejected(self):
        url = f"/api/tasks/{self.ctx.task_id}/share-folder"
        self.assertEqual(self.client.post(url, json={"folder_id": "unknown"}).status_code, 409)
        self.assertEqual(self.client.post(url, json={"folder_id": ""}).status_code, 422)
        self.assertEqual(self.app.state.store.state_get("share_folder_preferences", "items", {}), {})
        self.ctx.ns("storage")["manifest"] = {"staging_cid": "already-writing"}
        self.app.state.store.save_task(self.ctx)
        self.assertEqual(self.client.post(url, json={"folder_id": "dovi-folder"}).status_code, 409)
        self.assertEqual(self.client.get(f"/api/tasks/{self.ctx.task_id}/share-folders").status_code, 409)


if __name__ == "__main__":
    unittest.main()
