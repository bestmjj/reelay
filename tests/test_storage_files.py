"""Cloud file-manager guardrails: no delete, explicit provider, safe names, stale reads."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.fakes import WebCase
from reelay.providers.storage import browse_dirs


class FakeDrive:
    def __init__(self):
        self.mkdir = Mock(return_value="created-folder-1")
        self.rename = Mock()
        self.move = Mock()
        self.delete = Mock()
        self.get_attr = Mock(return_value={
            "id": "file_1", "parent_id": "parent_1",
            "name": "before.mkv", "is_dir": False,
        })


class FileManagerWebTests(WebCase):
    def test_aliyun_file_listing_does_not_need_115(self):
        cloud = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        listing = {
            "items": [{"id": "folder_1", "name": "动漫", "is_dir": True, "size": None},
                      {"id": "file_1", "name": "movie.mkv", "is_dir": False, "size": 123}],
            "cid": "0", "drive_id": "resource_1", "total_entries": 2,
            "path": [{"id": "0", "name": "根目录"}], "next_offset": None,
        }
        with patch.object(cloud, "browse", return_value=listing) as browse:
            response = self.client.get("/api/storage/items", params={
                "provider": "aliyun", "cid": "0"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"][1]["id"], "file_1")
        self.assertFalse(response.json()["items"][1]["is_dir"])
        browse.assert_called_once_with("0", 0, 100, include_files=True)

    def test_create_folder_uses_selected_drive_and_prevents_duplicate(self):
        cloud = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        fs = FakeDrive()
        listing = {"items": [], "drive_id": "resource_1", "next_offset": None, "total_entries": 0}
        with patch.object(cloud, "client", SimpleNamespace(drive_id="resource_1")), \
                patch.object(cloud, "browse", return_value=listing), \
                patch.object(cloud, "library", return_value=fs):
            body = {"provider": "aliyun", "cid": "parent_1",
                    "drive_id": "resource_1", "name": "动漫电影"}
            result = self.client.post("/api/storage/folders", json=body)
            self.assertEqual(result.status_code, 200, result.text)
            fs.mkdir.assert_called_once_with("parent_1", "动漫电影")
            listing["items"] = [{"id": "already", "name": "动漫电影", "is_dir": True}]
            self.assertEqual(self.client.post("/api/storage/folders", json=body).status_code, 409)
            self.assertEqual(fs.mkdir.call_count, 1)
            self.assertEqual(self.client.post("/api/storage/folders", json={
                **body, "drive_id": "old_resource"}).status_code, 409)
            self.assertEqual(self.client.post("/api/storage/folders", json={
                **body, "name": "../escape"}).status_code, 422)
            self.assertEqual(self.client.post("/api/storage/folders", json={
                **body, "name": ".reelay-manifest"}).status_code, 409)
            fs.delete.assert_not_called()

    def test_rename_checks_parent_name_type_and_drive(self):
        cloud = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        fs = FakeDrive()
        listing = {"items": [{"id": "file_1", "name": "before.mkv", "is_dir": False}],
                   "drive_id": "resource_1", "next_offset": None, "total_entries": 1}
        body = {"provider": "aliyun", "drive_id": "resource_1",
                "cid": "parent_1", "fid": "file_1",
                "old_name": "before.mkv", "name": "after.mkv", "is_dir": False}
        with patch.object(cloud, "client", SimpleNamespace(drive_id="resource_1")), \
                patch.object(cloud, "browse", return_value=listing), \
                patch.object(cloud, "library", return_value=fs):
            result = self.client.patch("/api/storage/names", json=body)
            self.assertEqual(result.status_code, 200, result.text)
            fs.rename.assert_called_once_with("file_1", "after.mkv")
            fs.get_attr.return_value["parent_id"] = "other"
            self.assertEqual(self.client.patch("/api/storage/names", json=body).status_code, 409)
            fs.get_attr.return_value["parent_id"] = "parent_1"
            self.assertEqual(self.client.patch("/api/storage/names", json={
                **body, "old_name": "stale.mkv"}).status_code, 409)
            self.assertEqual(fs.rename.call_count, 1)
            self.assertEqual(self.client.patch("/api/storage/names", json={
                **body, "name": "forbidden/name.mkv"}).status_code, 422)
            self.assertEqual(self.client.patch("/api/storage/names", json={
                **body, "name": ".reelay-private"}).status_code, 409)
            fs.delete.assert_not_called()

    def test_same_drive_file_move_without_delete_or_overwrite(self):
        cloud = self.app.state.pipeline.reg.get_provider("storage", "aliyun")
        fs = FakeDrive()
        file = {"id": "file_1", "parent_id": "parent_1", "name": "before.mkv", "is_dir": False}
        target = {"id": "folder_2", "parent_id": "root", "name": "新目录", "is_dir": True}
        values = {"file_1": file, "folder_2": target}
        fs.get_attr.side_effect = lambda fid: dict(values[fid])
        fs.move.side_effect = lambda fid, cid: file.update(parent_id=cid)
        source = {"items": [{"id": "file_1", "name": "before.mkv", "is_dir": False}],
                  "total_entries": 1, "drive_id": "resource_1", "next_offset": None}
        dest = {"items": [], "total_entries": 0, "drive_id": "resource_1", "next_offset": None}
        def browse(cid, offset, limit, include_files):
            return source if cid == "parent_1" else dest
        body = {"provider": "aliyun", "cid": "parent_1", "drive_id": "resource_1",
                "fid": "file_1", "name": "before.mkv", "destination_cid": "folder_2"}
        with patch.object(cloud, "client", SimpleNamespace(drive_id="resource_1")), \
                patch.object(cloud, "browse", side_effect=browse), \
                patch.object(cloud, "library", return_value=fs):
            result = self.client.post("/api/storage/move", json=body)
            self.assertEqual(result.status_code, 200, result.text)
            fs.move.assert_called_once_with("file_1", "folder_2")
            self.assertEqual(self.client.post("/api/storage/move", json=body).status_code, 409)
            file["parent_id"] = "parent_1"
            dest["items"] = [{"id": "other", "name": "before.mkv"}]
            self.assertEqual(self.client.post("/api/storage/move", json=body).status_code, 409)
            self.assertEqual(self.client.post("/api/storage/move", json={
                **body, "drive_id": "backup-drive"}).status_code, 409)
            self.assertEqual(self.client.post("/api/storage/move", json={
                **body, "destination_cid": "parent_1"}).status_code, 422)
            fs.delete.assert_not_called()
            self.assertEqual(fs.move.call_count, 1)

    def test_disallows_delete_and_remote_cross_origin_write(self):
        for url in ("/api/storage/folders", "/api/storage/names", "/api/storage/items", "/api/storage/move"):
            self.assertEqual(self.client.delete(url).status_code, 405)
        response = self.client.post("/api/storage/folders",
            headers={"Origin": "https://attacker.example"},
            json={"provider": "aliyun", "cid": "0", "name": "wrong"})
        self.assertEqual(response.status_code, 403)


class Browser115FileTests(unittest.TestCase):
    def test_cookie_list_keeps_file_fid_not_parent_cid(self):
        class Client:
            def fs_files(self, payload, *, timeout):
                return {"data": [
                    {"cid": "101", "pid": "0", "n": "动漫电影"},
                    {"fid": "456", "cid": "0", "n": "before.mkv", "s": "4096"},
                ], "count": 2}
        auth = SimpleNamespace(has_oauth=False, cookie_client=lambda: Client())
        with patch("p115client.check_response", side_effect=lambda result: result):
            result = browse_dirs(auth, "0", include_files=True)
        self.assertEqual(result["items"][0]["id"], "101")
        self.assertTrue(result["items"][0]["is_dir"])
        self.assertEqual(result["items"][1]["id"], "456")
        self.assertEqual(result["items"][1]["pid"], "0")
        self.assertEqual(result["items"][1]["size"], 4096)
        with patch("p115client.check_response", side_effect=lambda result: result):
            picker = browse_dirs(auth, "0", include_files=False)
        self.assertEqual(len(picker["items"]), 1)


if __name__ == "__main__":
    unittest.main()
