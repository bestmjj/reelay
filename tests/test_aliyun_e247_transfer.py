"""Full mock Aliyun E247 copy/readback: only source 247 4K, never wrong 4K."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from reelay.core.contracts import NeedsManual, ParsedResource, PipelineContext, RawMessage
from reelay.providers.aliyun import AliyunStorage


class FakeAliyunShareAndDrive:
    drive_id = "resource-drive-e247"

    def __init__(self):
        self.share = {
            "root": [
                {"id": "good247", "name": "247 4K.mp4",
                 "size": 1620000000, "is_dir": False},
                {"id": "wrong-dir", "name": "洛DA", "is_dir": True},
            ],
            "wrong-dir": [
                {"id": "wrong4gb", "name": "4K.mp4",
                 "size": 4040000000, "is_dir": False},
            ],
        }
        self.copy_calls = []
        self.staging = None
        self.landed = []

    def share_token(self, share, receive_code):
        return "fake-read-only-token"

    def shared_children(self, share, share_token, parent):
        return list(self.share[parent])

    def children(self, parent):
        if parent == "target":
            return ([{"id": "stage", "name": self.staging, "is_dir": True}]
                    if self.staging else [])
        if parent == "stage":
            return list(self.landed)
        raise AssertionError("Unexpected destination: " + str(parent))

    def mkdir(self, parent, name):
        if parent != "target" or self.staging:
            raise AssertionError("Duplicate or wrong-parent staging request")
        self.staging = name
        return "stage"

    def copy(self, share, token, file_id, destination):
        self.copy_calls.append((share, file_id, destination))
        assert file_id == "good247" and destination == "stage", "Wrong cloud source copied!"
        self.landed.append({"id": "landed-247", "name": "247 4K.mp4",
                            "size": 1620000000, "is_dir": False, "sha1": None})
        return {"file_id": "landed-247"}


def e247_task():
    ctx = PipelineContext(RawMessage(
        "@shareAliyun", "manual-e247",
        "https://www.alipan.com/s/gsQB8YFoLZQ 选择分享根目录中的 247",
        extra={"manual_import": True, "manual_review": True},
    ))
    ctx.parsed = ParsedResource(
        is_media=True, media_type="anime", title="吞噬星空", year=2020,
        season=1, episodes=[247], resolution="2160p",
        share_code="gsQB8YFoLZQ", raw_fields={"share_provider": "aliyun"},
    )
    ctx.source_rule = {"storage": "aliyun", "episodes_from": "message"}
    ctx.save_dir_cid = "target"
    ctx.ns("share_selection").update({
        "file_id": "good247", "file_name": "247 4K.mp4", "file_path": "",
        "file_size": 1620000000,
        "share_code": "gsQB8YFoLZQ", "provider": "aliyun",
    })
    return ctx


class TestE247RealTransferFlowWithMockedAliyun(unittest.TestCase):
    def test_single_correct_file_is_prepared_copied_once_and_verified(self):
        cloud = FakeAliyunShareAndDrive()
        storage = AliyunStorage()
        storage.client = cloud
        ctx = e247_task()
        with patch.object(storage, "ready", return_value=True):
            manifest = asyncio.run(storage.prepare(ctx))
        self.assertEqual([item["id"] for item in manifest["files"]], ["good247"])
        self.assertEqual(manifest["pairs"], [[1, 247]])
        self.assertEqual(cloud.copy_calls, [])
        ctx.ns("storage")["manifest"] = manifest
        result = asyncio.run(storage.save(ctx))
        self.assertTrue(result["storage_verified"])
        self.assertEqual(result["saved_files"], ["247 4K.mp4"])
        self.assertEqual(cloud.copy_calls, [("gsQB8YFoLZQ", "good247", "stage")])
        # Retry after a confirmed landing must verify, never send another copy.
        verified = asyncio.run(storage.save(ctx))
        self.assertTrue(verified["storage_verified"])
        self.assertEqual(len(cloud.copy_calls), 1)

    def test_remote_file_changed_or_blocked_never_sends_copy(self):
        for mutate in ("renamed", "size", "missing", "quarantined"):
            with self.subTest(mutate=mutate):
                cloud = FakeAliyunShareAndDrive()
                storage = AliyunStorage()
                storage.client = cloud
                ctx = e247_task()
                if mutate == "renamed":
                    cloud.share["root"][0]["name"] = "unrelated.mp4"
                elif mutate == "size":
                    cloud.share["root"][0]["size"] = 4040000000
                elif mutate == "missing":
                    cloud.share["root"].pop(0)
                else:
                    ctx.ns("storage")["blocked_episodes"] = [[1, 247]]
                with patch.object(storage, "ready", return_value=True):
                    with self.assertRaises(NeedsManual):
                        asyncio.run(storage.prepare(ctx))
                self.assertEqual(cloud.copy_calls, [])
                self.assertIsNone(cloud.staging)


if __name__ == "__main__":
    unittest.main()
