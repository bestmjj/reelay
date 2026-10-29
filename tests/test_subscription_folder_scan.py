"""Choosing an existing cloud folder must not require editing save_dirs or copying files."""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from reelay.core import existing_media, subscriptions, library_scan
from tests.fakes import WebCase
from tests.test_library_scan import FakeCloud, item


class ChosenFolderTests(WebCase):
    CONFIG = {
        "save_dirs": {"默认": "aliyun:new_downloads"},
        "defaults": {"storage": "aliyun", "save_to": "默认"},
        "sources": [],
    }

    def setUp(self):
        super().setUp()
        entry = subscriptions.normalize({"title": "牧神记", "tmdbid": 236534,
                                         "season": 1, "start_episode": 106})
        self.show = self.app.state.store.add_subscription(subscriptions.key(entry), entry)
        self.cloud = FakeCloud({
            "existing": [
                item("f104", "104 4K.mp4"),
                item("f105", "105 4K.mp4"),
                item("notes", "readme.txt"),
            ]
        }, disk="resource-drive")
        self.cloud.browse = Mock(return_value={
            "path": [{"id": "0", "name": "根目录"},
                     {"id": "existing", "name": "牧神记"}],
            "drive_id": "resource-drive", "items": [],
        })
        self.cloud.fs.get_attr = Mock(side_effect=lambda fid: {
            **next(file for file in self.cloud.fs.listings["existing"] if file["id"] == fid),
            "parent_id": "existing",
        })
        reg = self.app.state.pipeline.reg
        original = reg.get_provider
        def provider(kind, name):
            return self.cloud if kind == "storage" and name == "aliyun" else original(kind, name)
        self.mock_provider = patch.object(reg, "get_provider", side_effect=provider)
        self.mock_provider.start()
        self.addCleanup(self.mock_provider.stop)

    def scan(self, provider="aliyun", cid="existing"):
        return self.client.post("/api/subscriptions/existing/scan-folder",
                                json={"id": self.show["id"], "provider": provider, "cid": cid})

    def test_select_folder_scans_existing_episodes_without_modifying_destination(self):
        response = self.scan()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["file_count"], result["eligible"]), (2, 2))
        self.assertTrue(result["complete"], result["errors"])
        preview = self.client.get("/api/subscriptions/existing",
                                  params={"id": self.show["id"]}).json()
        self.assertEqual(preview["total"], 2)
        self.assertEqual([x["episode"] for x in preview["items"]], [104, 105])
        self.assertEqual(preview["folder"]["path"], "牧神记")
        self.assertEqual(self.app.state.pipeline.cfg["save_dirs"], {"默认": "aliyun:new_downloads"})
        self.assertEqual(self.app.state.store.state_get("library_scan", "files", []), [])
        self.assertEqual(self.app.state.store.state_get("media_associations", "confirmed", {}), {})
        self.assertFalse(any("E106" in x["name"] for x in preview["items"]))
        self.assertEqual(existing_media.scan_observations(self.app.state.store)[0]["provider"], "aliyun")

    def test_association_requires_explicit_choice_and_readback(self):
        self.assertEqual(self.scan().status_code, 200)
        preview = self.client.get("/api/subscriptions/existing",
                                  params={"id": self.show["id"]}).json()
        key = preview["items"][0]["file_key"]
        response = self.client.post("/api/subscriptions/existing/associate",
                                    json={"id": self.show["id"], "file_keys": [key]})
        self.assertEqual(response.status_code, 200, response.text)
        self.cloud.fs.get_attr.assert_called_once_with(preview["items"][0]["file_key"].split(":")[-1])
        matched = self.client.get("/api/subscriptions/existing",
                                  params={"id": self.show["id"]}).json()
        self.assertTrue(matched["items"][0]["associated"])
        self.assertFalse(matched["items"][1]["associated"])
        observed = existing_media.apply_associations(
            existing_media.scan_observations(self.app.state.store),
            self.app.state.store.state_get("media_associations", "confirmed", {}))
        self.assertEqual(len([x for x in observed if x.get("tmdbid") == 236534]), 1)
        # Reading/associating is not a successful transfer.
        self.assertEqual(self.app.state.store.done_snapshots(), [])

    def test_root_invalid_and_missing_folder_never_changes_config(self):
        for cid in ("0", "root", "../outside", "a" * 129):
            with self.subTest(cid=cid):
                self.assertIn(self.scan(cid=cid).status_code, (400, 422))
        self.assertEqual(self.app.state.pipeline.cfg["save_dirs"], {"默认": "aliyun:new_downloads"})

    def test_scope_rejects_cloud_account_switch(self):
        self.cloud.browse.return_value["drive_id"] = "another-drive"
        response = self.scan()
        self.assertEqual(response.status_code, 409, response.text)

    def test_folder_resume_collects_subfolders_and_avoids_completed_transfer_claim(self):
        self.cloud.fs.listings["existing"] = [item("season", "Season 01", folder=True)]
        self.cloud.fs.listings["season"] = [item("old", "105 4K.mp4"),
                                            item("new", "106 4K.mp4")]
        with patch.object(library_scan, "MAX_FOLDERS", 1):
            first = self.scan()
            self.assertEqual(first.status_code, 200, first.text)
            self.assertTrue(first.json()["has_more"])
            second = self.scan()
            self.assertEqual(second.status_code, 200, second.text)
            self.assertFalse(second.json()["has_more"])
        preview = self.client.get("/api/subscriptions/existing",
                                  params={"id": self.show["id"]}).json()
        self.assertEqual({x["episode"] for x in preview["items"]}, {105, 106})
        self.assertFalse(any(x["associated"] for x in preview["items"]))


if __name__ == "__main__":
    unittest.main()
