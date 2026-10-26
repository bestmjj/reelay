"""Resumable read-only scan must never infer that a transfer was completed."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from reelay.core import library_scan, library_scan_progress
from tests.test_library_scan import FakeCloud, item


class ScanProgressTests(unittest.TestCase):
    CFG = {"save_dirs": {"动漫": "aliyun:shows"}, "defaults": {"storage": "aliyun", "save_to": "动漫"}}

    def test_resumes_after_folder_budget_and_keeps_real_file_ids(self):
        cloud = FakeCloud({
            "shows": [item("series", "牧神记 (2024) [tmdbid=236534]", folder=True)],
            "series": [item("first", "牧神记.S01E105.mp4", size=100),
                       item("second", "牧神记.S01E106.mp4", size=200)],
        })
        with patch.object(library_scan, "MAX_FOLDERS", 1):
            first, cursor = library_scan_progress.step(self.CFG, lambda _: cloud)
            self.assertTrue(first["has_more"])
            self.assertFalse(first["complete"])
            self.assertEqual(first["file_count"], 0)
            final, cursor = library_scan_progress.step(self.CFG, lambda _: cloud, cursor)
        self.assertTrue(final["complete"], final["errors"])
        self.assertFalse(final["has_more"])
        self.assertEqual(final["scanned_roots"], 1)
        self.assertEqual(final["file_count"], 2)
        self.assertEqual(final["groups"][0]["episode_pairs"], [[1, 105], [1, 106]])
        self.assertEqual({r["id"] for r in cursor["records"]}, {"first", "second"})
        self.assertEqual({r["tmdbid"] for r in cursor["records"]}, {236534})
        self.assertEqual(cloud.fs.calls.count("shows"), 1)
        self.assertEqual(cloud.fs.calls.count("series"), 1)

    def test_more_than_one_page_in_a_single_directory_resumes_without_data_loss(self):
        cloud = FakeCloud({"shows": [item("f" + str(i), "动漫.S01E%03d.mkv" % i) for i in range(1, 7)]})
        with patch.object(library_scan, "MAX_ITEMS_PER_FOLDER", 2):
            first, cursor = library_scan_progress.step(self.CFG, lambda _: cloud)
            self.assertEqual(first["file_count"], 6)
            self.assertTrue(first["complete"])
            self.assertEqual(cursor["records"][0]["id"], "f1")
            self.assertEqual(len({r["id"] for r in cursor["records"]}), 6)
            self.assertGreaterEqual(cloud.fs.calls.count("shows"), 3)

    def test_configuration_change_invalidates_old_cursor(self):
        cloud = FakeCloud({
            "shows": [item("folder", "sub", folder=True)],
            "folder": [item("ep", "剧.S01E01.mp4")],
            "another": [item("ep2", "新剧.S01E02.mp4")],
        })
        with patch.object(library_scan, "MAX_FOLDERS", 1):
            _, cursor = library_scan_progress.step(self.CFG, lambda _: cloud)
        cfg = {**self.CFG, "save_dirs": {"动漫": "aliyun:another"}}
        snapshot, fresh = library_scan_progress.step(cfg, lambda _: cloud, cursor)
        self.assertEqual(snapshot["file_count"], 1)
        self.assertEqual([x["id"] for x in fresh["records"]], ["ep2"])

    def test_missing_root_is_not_turned_into_false_empty_healthy_scan(self):
        snapshot, state = library_scan_progress.step(
            self.CFG, lambda _: FakeCloud({}))
        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["scanned_roots"], 0)
        self.assertTrue(snapshot["errors"])
        self.assertEqual(state["records"], [])


if __name__ == "__main__":
    unittest.main()
