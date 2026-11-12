"""Regression tests for explicit read-only cloud inventory (no network)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from reelay.core import library_scan
from tests.fakes import WebCase


def item(fid, name, *, folder=False, size=1024):
    return {"id": fid, "name": name, "is_dir": folder, "size": size,
            "parent_id": "root", "sha1": None}


class ReadOnlyLibrary:
    def __init__(self, listings):
        self.listings = listings
        self.calls = []

    def iterdir(self, cid):
        self.calls.append(str(cid))
        if str(cid) not in self.listings:
            raise FileNotFoundError("folder missing")
        return list(self.listings[str(cid)])

    def mkdir(self, *args, **kwargs):
        raise AssertionError("Inventory must never create directories")

    def rename(self, *args, **kwargs):
        raise AssertionError("Inventory must never rename files")

    def move(self, *args, **kwargs):
        raise AssertionError("Inventory must never move files")

    def delete(self, *args, **kwargs):
        raise AssertionError("Inventory must never delete files")


class FakeCloud:
    def __init__(self, listings, *, disk="resource-drive"):
        self.fs = ReadOnlyLibrary(listings)
        self.client = SimpleNamespace(drive_id=disk)

    def ready(self):
        return True

    def library(self):
        return self.fs


class InventoryTests(unittest.TestCase):
    CONFIG = {
        "save_dirs": {
            "电影": "aliyun:movie_root",
            "剧集": "aliyun:show_root",
            "默认": "aliyun:movie_root",  # same physical path; should only count once
        },
        "defaults": {"storage": "p115", "save_to": "默认"},
        "sources": [{"bot": "@shareAliyun", "storage": "aliyun", "save_to": "剧集"}],
    }

    def test_new_anime_movie_folder_is_scanned_as_movie_not_new_media_type(self):
        cfg = {"save_dirs": {"动漫电影": "aliyun:animeMovieFolder"},
               "defaults": {"storage": "aliyun", "save_to": "动漫电影"}}
        cloud = FakeCloud({"animeMovieFolder": [item("clip_a", "My.Movie.2026.mkv", size=4096)]})
        snapshot = library_scan.inventory(cfg, lambda _: cloud)
        self.assertEqual(snapshot["file_count"], 1)
        self.assertEqual(snapshot["groups"][0]["save_dir_alias"], "动漫电影")
        self.assertEqual(snapshot["groups"][0]["media_type"], "movie")
        self.assertTrue(snapshot["complete"], snapshot["errors"])

    def test_scans_named_roots_and_deduplicates_overlapping_aliases(self):
        cloud = FakeCloud({
            "movie_root": [item("movie1", "春天 (2025).mkv", size=2000)],
            "show_root": [item("dir1", "长夜", folder=True)],
            "dir1": [
                item("ep1", "长夜.S01E01.mkv", size=1000),
                item("ep2", "长夜.S01E02.mkv", size=1200),
                item("ignore1", "picture.jpg"),
            ],
        })
        snapshot = library_scan.inventory(self.CONFIG, lambda name: cloud)
        self.assertEqual(snapshot["file_count"], 3)
        self.assertEqual(snapshot["scanned_roots"], 3)
        self.assertEqual(len(snapshot["groups"]), 2)
        self.assertTrue(snapshot["complete"], snapshot["errors"])
        self.assertEqual(snapshot["folder_count"], 3)
        self.assertEqual(cloud.fs.calls.count("movie_root"), 1)
        tv = next(x for x in snapshot["groups"] if x["media_type"] == "tv")
        self.assertEqual(tv["episodes"], [1, 2])
        self.assertEqual(tv["origin"], "scanned")
        self.assertEqual(tv["tasks"], 0)
        self.assertEqual(tv["file_count"], 2)
        self.assertIn("长夜/长夜.S01E01.mkv", tv["files"])
        self.assertEqual(snapshot["groups"][0]["save_dir_alias"], "剧集")

    def test_same_file_id_in_different_clouds_not_conflated(self):
        cfg = {
            "save_dirs": {"电影": "101", "动画": "aliyun:anime_root"},
            "defaults": {"storage": "p115", "save_to": "电影"},
            "sources": [],
        }
        p115 = FakeCloud({"101": [item("identical", "Alpha (2023).mp4")]})
        aliyun = FakeCloud({"anime_root": [item("identical", "Beta.S01E01.mkv")]})
        snapshot = library_scan.inventory(cfg, lambda name: aliyun if name == "aliyun" else p115)
        self.assertEqual(snapshot["file_count"], 2)
        self.assertEqual({g["provider"] for g in snapshot["groups"]}, {"aliyun", "p115"})

    def test_skips_whole_cloud_root_and_mixed_provider_alias(self):
        cfg = {"save_dirs": {"默认": "0"},
               "defaults": {"storage": "p115", "save_to": "默认"},
               "sources": [{"storage": "aliyun", "save_to": "默认"}, {"storage": "p115", "save_to": "默认"}]}
        snapshot = library_scan.inventory(cfg, lambda name: self.fail("must not access provider"))
        self.assertEqual(snapshot["scanned_roots"], 0)
        self.assertTrue(snapshot["errors"])
        self.assertFalse(snapshot["complete"])

    def test_depth_limit_does_not_claim_complete_inventory(self):
        cfg = {"save_dirs": {"剧集": "aliyun:root1"},
               "defaults": {"storage": "aliyun", "save_to": "剧集"}}
        cloud = FakeCloud({
            "root1": [item("folder2", "Season 1", folder=True)],
            "folder2": [item("folder3", "Next", folder=True)],
            "folder3": [item("video", "Next.S01E01.mkv")],
        })
        with patch.object(library_scan, "MAX_DEPTH", 1):
            snapshot = library_scan.inventory(cfg, lambda _: cloud)
        self.assertTrue(snapshot["truncated"])
        self.assertEqual(snapshot["file_count"], 0)
        self.assertNotIn("folder3", cloud.fs.calls)

    def test_failed_listing_is_not_a_false_empty_inventory(self):
        cfg = {"save_dirs": {"电影": "aliyun:missing"},
               "defaults": {"storage": "aliyun", "save_to": "电影"}}
        snapshot = library_scan.inventory(cfg, lambda _: FakeCloud({}))
        self.assertEqual(snapshot["scanned_roots"], 0)
        self.assertTrue(snapshot["errors"])


class LibraryScanWebTests(WebCase):
    CONFIG = {
        "save_dirs": {"剧集": "aliyun:show_root"},
        "defaults": {"save_to": "剧集", "storage": "aliyun"},
    }

    def test_read_only_scan_is_independent_from_successful_transfer_ledger(self):
        cloud = FakeCloud({
            "show_root": [item("dir1", "长夜", folder=True)],
            "dir1": [item("episode1", "长夜.S01E01.mkv", size=123456)],
        })
        provider = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        with patch.object(provider, "library", return_value=cloud.fs), \
                patch.object(provider, "ready", return_value=True), \
                patch.object(provider, "client", cloud.client):
            first = self.client.post("/api/library/scan")
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["file_count"], 1)
            second = self.client.post("/api/library/scan")
            self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["file_count"], 1)
        existing = self.client.get("/api/library/scan").json()
        self.assertEqual(existing["groups"][0]["origin"], "scanned")
        self.assertEqual(existing["groups"][0]["tasks"], 0)
        self.assertEqual(existing["total_done"], 0)
        self.assertEqual(self.client.get("/api/library").json()["groups"], [])
        self.assertEqual(self.app.state.store.counts_by_state(), {})
        self.assertFalse(self.app.state.store.saved_share("some-share", "show_root", "aliyun"))

    def test_failed_scan_preserves_last_completed_inventory(self):
        cloud = FakeCloud({"show_root": [item("episode1", "剧名.S01E01.mp4")]})
        provider = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        with patch.object(provider, "library", return_value=cloud.fs), \
                patch.object(provider, "ready", return_value=True), \
                patch.object(provider, "client", cloud.client):
            self.assertEqual(self.client.post("/api/library/scan").status_code, 200)
        with patch.object(provider, "library", side_effect=ConnectionError("backend unavailable")), \
                patch.object(provider, "ready", return_value=True), \
                patch.object(provider, "client", cloud.client):
            failed = self.client.post("/api/library/scan")
            self.assertEqual(failed.status_code, 422, failed.text)
        old = self.client.get("/api/library/scan").json()
        self.assertEqual(old["file_count"], 1)
        self.assertEqual(old["scanned_roots"], 1)


if __name__ == "__main__":
    unittest.main()
