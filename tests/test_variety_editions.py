"""Protect distinct variety-show episodes/editions from wrong dedup and overwrite."""
from __future__ import annotations

import unittest

from reelay.core import works, runtime
from reelay.core.contracts import AlreadyStored, NeedsManual, ParsedResource, PipelineContext, RawMessage
from reelay.core.store import Store
from reelay.plugins.organizer import _same_slot, build_plan
from reelay.providers.storage import _select_video_items


FILES = [
    "20261026.第3期加更.mp4",
    "20261025.第3期下.mkv",
    "20261024.第3期上.mp4",
    "20261020.第2期还有加更.mp4",
    "20261019.第2期加更.mp4",
    "20261018.第2期下.mp4",
    "20261017.第2期上.mp4",
    "20261013.第1期还有加更：王安宇脑力下线白敬亭直跺脚.mkv",
    "20261012.第1期加更：王楚然猜词变造词“气疯”沈腾.mp4",
    "20261010.纯享 第01期上·慢速目击证人纯享.mp4",
]
LISTING = [{"id": str(i + 1), "name": name, "size": 100 + i, "sha1": f"{i + 1:040X}"}
           for i, name in enumerate(FILES)]


class VarietySelectionTests(unittest.TestCase):
    def setUp(self):
        self.parsed = ParsedResource(is_media=True, media_type="tv", title="某综艺", season=1, episodes=[1, 2, 3])

    def test_real_world_variety_files_are_all_selected_as_distinct_editions(self):
        chosen = _select_video_items(LISTING, self.parsed)
        self.assertEqual(len(chosen["files"]), 10)
        self.assertEqual(chosen["episodes"], [1, 2, 3])
        self.assertEqual(chosen["pairs"], [[1, 1], [1, 2], [1, 3]])
        self.assertEqual(len(chosen["variety_parts"]), 10)
        self.assertEqual({(x["episode"], x["variant"]) for x in chosen["files"] if x["episode"] == 3},
                         {(3, "upper"), (3, "lower"), (3, "bonus")})

    def test_regular_episode_ledger_must_not_drop_any_variety_editions(self):
        chosen = _select_video_items(LISTING, self.parsed,
                                     stored=[[1, 1], [1, 2], [1, 3]])
        self.assertEqual(len(chosen["files"]), len(LISTING))

    def test_edition_ledger_only_removes_exact_matching_part(self):
        first = _select_video_items(LISTING, self.parsed)
        seen = [[x["season"], x["episode"], x["variant"]] for x in first["files"][:4]]
        next_batch = _select_video_items(LISTING, self.parsed, stored_variety_parts=seen)
        self.assertEqual(len(next_batch["files"]), 6)
        with self.assertRaisesRegex(AlreadyStored, "综艺分享"):
            _select_video_items(LISTING, self.parsed,
                                stored_variety_parts=[[x["season"], x["episode"], x["variant"]]
                                                      for x in first["files"]])

    def test_message_scope_only_picks_requested_period_with_all_its_parts(self):
        self.parsed.episodes = [3]
        picked = _select_video_items(LISTING, self.parsed, scope="message")
        self.assertEqual({x["variant"] for x in picked["files"]}, {"upper", "lower", "bonus"})
        self.assertTrue(all(x["episode"] == 3 for x in picked["files"]))

    def test_same_period_and_edition_with_two_versions_requires_review(self):
        extra = {"id": "other", "name": "20261026.第3期加更：不同片源.mp4", "size": 222}
        with self.assertRaisesRegex(NeedsManual, "多个文件或版本"):
            _select_video_items([*LISTING, extra], self.parsed)
        # AI must not silently choose one variant and discard the other.
        with self.assertRaisesRegex(NeedsManual, "多个文件或版本"):
            _select_video_items([*LISTING, extra], self.parsed, hints={"drop": [extra["name"]]})

    def test_unknown_or_mixed_file_names_require_manual_review(self):
        with self.assertRaisesRegex(NeedsManual, "无法从文件名识别集号"):
            _select_video_items([*LISTING, {"id": "u", "name": "20261026.第3期神秘版.mp4"}], self.parsed)
        with self.assertRaisesRegex(NeedsManual, "混有综艺期数"):
            _select_video_items([*LISTING, {"id": "m", "name": "某节目.S01E04.mp4"}], self.parsed)


class VarietyOrganizerTests(unittest.TestCase):
    def test_plan_uses_unique_names_for_each_edition_and_keeps_extension(self):
        runtime.set_process_config({})
        parsed = ParsedResource(is_media=True, media_type="tv", title="某综艺",
                                year=2026, season=1, episodes=[1, 2, 3])
        landed = [{**file, "is_dir": False} for file in LISTING]
        plan = build_plan(parsed, landed)
        self.assertEqual(len(plan), 10)
        self.assertEqual(len({x["target_name"] for x in plan}), 10)
        self.assertTrue(all(x["target_dir"] == "某综艺 (2026)/Season 01" for x in plan))
        upper = next(x for x in plan if x["src_name"] == "20261024.第3期上.mp4")
        lower = next(x for x in plan if x["src_name"] == "20261025.第3期下.mkv")
        bonus = next(x for x in plan if x["src_name"] == "20261026.第3期加更.mp4")
        self.assertIn("第 3 期（上）", upper["target_name"])
        self.assertIn("第 3 期（下）", lower["target_name"])
        self.assertIn("第 3 期（加更）", bonus["target_name"])
        self.assertTrue(upper["target_name"].endswith(".mp4"))
        self.assertTrue(lower["target_name"].endswith(".mkv"))
        self.assertFalse(_same_slot({"name": upper["target_name"], "is_dir": False},
                                    lower["target_name"], "某综艺", True))
        self.assertTrue(_same_slot({"name": upper["target_name"], "is_dir": False},
                                   upper["target_name"], "某综艺", True))

    def test_custom_tv_template_still_keeps_edition_label(self):
        with runtime.scope({"plugins": {"organizer": {"naming": {
                "tv": "{{title}}/Season {{season|pad2}}/{{title}} - {{season_episode}}{{fileExt}}"}}}}):
            parsed = ParsedResource(is_media=True, media_type="tv", title="某综艺", season=1, episodes=[3])
            plan = build_plan(parsed, [LISTING[0], LISTING[1], LISTING[2]])
        self.assertEqual(len({x["target_name"] for x in plan}), 3)
        self.assertIn("第 3 期（加更）", plan[0]["target_name"] + plan[1]["target_name"] + plan[2]["target_name"])


class VarietyLedgerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_transfer_marks_only_verified_editions_not_complete_episodes(self):
        ctx = PipelineContext(RawMessage("@bot", "variety-multipart", "综艺第3期"))
        ctx.parsed = ParsedResource(is_media=True, media_type="tv", title="某综艺",
                                    season=1, episodes=[3], share_code="varietyShare")
        ctx.ns("storage")["identity"] = {
            "provider": "p115", "cid": "1", "share_code": "varietyShare",
            "title": "某综艺", "season": 1, "episodes": [3],
            "work": works.identity("某综艺"),
            "variety_parts": [{"season": 1, "episode": 3, "variant": "upper"},
                              {"season": 1, "episode": 3, "variant": "lower"}],
        }
        ctx.result = {"storage_verified": True, "landed": [{"id": "1"}, {"id": "2"}]}
        self.store.save_task(ctx)
        self.store.finish_transfer(ctx)
        self.assertEqual(self.store.stored_episode_pairs(works.identity("某综艺"), "1"), [])
        self.assertEqual(self.store.stored_variety_parts_in_drive(works.identity("某综艺"), "p115"),
                         [[1, 3, "lower"], [1, 3, "upper"]])
        self.assertEqual(self.store.stored_variety_parts_in_drive(works.identity("另一作品"), "p115"), [])
        self.assertEqual(self.store.stored_variety_parts_in_drive(works.identity("某综艺"), "aliyun", "resource-drive"), [])
        self.assertFalse(self.store.stored_series())


if __name__ == "__main__":
    unittest.main()
