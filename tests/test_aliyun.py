"""Aliyun storage safety regressions; no live cloud credentials or requests."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from reelay.core import secret_store
from reelay.core.contracts import ParsedResource, PipelineContext, RawMessage, TransferUncertain
from reelay.core.parse_rules import parse_share_url, share_provider
from reelay.providers.aliyun import AliyunClient, AliyunStorage


def context():
    parsed = ParsedResource(is_media=True, media_type="movie", title="Example",
                            share_code="abcDEF", raw_fields={"share_provider": "aliyun"})
    ctx = PipelineContext(raw=RawMessage(source="@sample", msg_id="1", text="Example"))
    ctx.parsed = parsed
    ctx.ns("storage")["manifest"] = {
        "to_cid": "folderAAA0000000001", "staging_cid": "stageAAA0000000001",
        "files": [{"id": "share-file-1", "name": "Example.mp4", "size": 123, "sha1": None}],
        "episodes": [],
    }
    return ctx


class FakeClient:
    def __init__(self):
        self.entries = []
        self.copies = []
        self.fail_copy = False

    def children(self, parent):
        return list(self.entries)

    def share_token(self, share, pwd):
        return "token"

    def copy(self, share, token, file_id, parent):
        self.copies.append((share, file_id, parent))
        if self.fail_copy:
            raise TimeoutError("ambiguous")
        self.entries.append({"id": "landed-file-1", "name": "Example.mp4",
                             "size": 123, "sha1": None, "is_dir": False,
                             "parent_id": parent})
        return {"file_id": "landed-file-1"}


class AliyunTests(unittest.TestCase):
    def test_links_and_provider(self):
        url, code, pwd = parse_share_url("提取码 abcd https://www.alipan.com/s/abcDEF",
                                         ["https://www.alipan.com/s/abcDEF"])
        self.assertEqual((code, pwd, share_provider(url)), ("abcDEF", "abcd", "aliyun"))
        self.assertEqual(share_provider(parse_share_url("https://115.com/s/foo", [])[0]), "p115")

    def test_token_rotation_persists_before_use(self):
        client = AliyunClient()
        with secret_store.override(aliyun_refresh_token="old") as secrets:
            with patch.object(client, "_send", return_value={
                "access_token": "access", "refresh_token": "new",
                "default_drive_id": "drive1", "expires_in": 7200}):
                self.assertEqual(client.drive_id, "drive1")
            self.assertEqual(secrets.get("aliyun_refresh_token"), "new")

    def test_partial_stage_reconciles_without_copy(self):
        provider = AliyunStorage()
        provider.client = FakeClient()
        ctx = context()
        provider.client.entries = [{"id": "other", "name": "Another.mp4",
                                    "size": 3, "sha1": None, "is_dir": False}]
        with self.assertRaises(TransferUncertain):
            asyncio.run(provider.save(ctx))
        self.assertEqual(provider.client.copies, [])

    def test_one_copy_only_on_unknown_reply(self):
        provider = AliyunStorage()
        provider.client = FakeClient()
        provider.client.fail_copy = True
        ctx = context()
        with self.assertRaises(TransferUncertain):
            asyncio.run(provider.save(ctx))
        self.assertEqual(len(provider.client.copies), 1)
        with self.assertRaises(TransferUncertain):
            asyncio.run(provider.reconcile(ctx))
        self.assertEqual(len(provider.client.copies), 1)


    def test_browse_allows_opaque_directory_id(self):
        provider = AliyunStorage()
        provider.client = FakeClient()
        provider.client.entries = [
            {"id": "abcdef0123456789abcdef", "name": "影视", "is_dir": True,
             "size": 0, "sha1": None, "parent_id": "root"}
        ]
        result = provider.browse("0")
        self.assertEqual(result["path"], [{"id": "0", "name": "根目录"}])
        self.assertEqual(result["items"][0]["id"], "abcdef0123456789abcdef")

    def test_expired_access_token_retries_only_reads(self):
        from reelay.providers.aliyun import AliyunAPIError
        client = AliyunClient()
        with secret_store.override(aliyun_refresh_token="old-refresh-token-123456"):
            results = [
                {"access_token": "access1", "refresh_token": "rotated-refresh-token-123456",
                 "default_drive_id": "drive123"},
                AliyunAPIError(200, "AccessTokenInvalid"),
                {"access_token": "access2", "refresh_token": "rotated2-refresh-token-123456",
                 "default_drive_id": "drive123"},
                {"items": []},
            ]
            with patch.object(client, "_send", side_effect=results) as sent:
                self.assertEqual(client.post("/adrive/v3/file/list", {}, read=True), {"items": []})
                self.assertEqual(sent.call_count, 4)
        client = AliyunClient()
        with secret_store.override(aliyun_refresh_token="old-refresh-token-123456"):
            with patch.object(client, "_send", side_effect=[
                {"access_token": "access1", "refresh_token": "rotated-refresh-token-123456",
                 "default_drive_id": "drive123"},
                AliyunAPIError(401, "AccessTokenInvalid"),
            ]) as sent:
                with self.assertRaises(AliyunAPIError):
                    client.post("/v2/file/copy", {}, read=False)
                self.assertEqual(sent.call_count, 2)

    def test_verified_landing(self):
        provider = AliyunStorage()
        provider.client = FakeClient()
        result = asyncio.run(provider.save(context()))
        self.assertTrue(result["storage_verified"])
        self.assertEqual(result["landed"][0]["id"], "landed-file-1")


if __name__ == "__main__":
    unittest.main()
