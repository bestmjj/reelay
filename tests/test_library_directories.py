"""Media types and configured folder aliases are independent library filters."""
from __future__ import annotations

from unittest.mock import patch

from tests.fakes import WebCase


class LibraryDirectoryWebTests(WebCase):
    CONFIG = {
        "save_dirs": {
            "默认": "0",
            "动漫": "aliyun:AnimeFolder123",
            "动漫电影": "aliyun:MovieFolder123",
        },
        "defaults": {"storage": "aliyun", "save_to": "默认"},
    }

    @staticmethod
    def snap(alias: str, fid: str) -> dict:
        return {
            "parsed": {
                "title": "同名作品", "media_type": "movie", "year": 2026,
                "raw_fields": {"tmdbid": "10001"},
            },
            "meta": {"storage": {"landed": [
                {"id": fid, "name": "同名作品.mkv", "size": 1024},
            ]}},
            "save_dir_alias": alias,
            "updated_at": 100,
        }

    def test_new_alias_is_configuration_even_with_no_media(self):
        config = self.client.get("/api/config").json()
        self.assertIn("动漫电影", config["config"]["save_dirs"])
        empty_verified = self.client.get("/api/library", params={
            "save_dir_alias": "动漫电影"})
        empty_scanned = self.client.get("/api/library/scan", params={
            "save_dir_alias": "动漫电影"})
        self.assertEqual(empty_verified.status_code, 200)
        self.assertEqual(empty_scanned.status_code, 200)
        self.assertEqual(empty_verified.json()["groups"], [])
        self.assertEqual(empty_scanned.json()["groups"], [])

    def test_verified_filter_before_merging_same_tmdb_id(self):
        store = self.app.state.store
        snaps = [
            self.snap("动漫电影", "video-movie-id"),
            self.snap("动漫", "video-anime-id"),
        ]
        with patch.object(store, "done_snapshots", return_value=snaps):
            both = self.client.get("/api/library")
            only_movie_dir = self.client.get("/api/library", params={
                "save_dir_alias": "动漫电影", "media_type": "movie"})
            only_anime_dir = self.client.get("/api/library", params={
                "save_dir_alias": "动漫"})
        self.assertEqual(both.status_code, 200, both.text)
        self.assertEqual(both.json()["groups"][0]["tasks"], 2)
        self.assertEqual(only_movie_dir.status_code, 200, only_movie_dir.text)
        self.assertEqual(len(only_movie_dir.json()["groups"]), 1)
        self.assertEqual(only_movie_dir.json()["groups"][0]["tasks"], 1)
        self.assertEqual(only_movie_dir.json()["groups"][0]["save_dir_alias"], "动漫电影")
        self.assertEqual(only_anime_dir.json()["groups"][0]["save_dir_alias"], "动漫")

    def test_scanned_filter_is_independent_of_media_type(self):
        store = self.app.state.store
        store.state_set("library_scan", "snapshot", {
            "groups": [
                {"title": "动画电影A", "media_type": "movie", "save_dir_alias": "动漫电影",
                 "bytes": 2048},
                {"title": "动画剧B", "media_type": "anime", "save_dir_alias": "动漫",
                 "bytes": 1024},
            ],
            "scanned_at": 100, "file_count": 2, "folder_count": 2,
            "scanned_roots": 2, "errors": [], "complete": True, "truncated": False,
        })
        both = self.client.get("/api/library/scan")
        selected = self.client.get("/api/library/scan", params={
            "save_dir_alias": "动漫电影", "media_type": "movie"})
        wrong_type = self.client.get("/api/library/scan", params={
            "save_dir_alias": "动漫电影", "media_type": "anime"})
        self.assertEqual(len(both.json()["groups"]), 2)
        self.assertEqual([g["title"] for g in selected.json()["groups"]], ["动画电影A"])
        self.assertEqual(wrong_type.json()["groups"], [])
        self.assertEqual(selected.json()["total_done"], 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
