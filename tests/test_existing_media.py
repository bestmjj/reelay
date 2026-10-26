"""Existing cloud media dedup regressions."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from reelay.core import existing_media
from reelay.core.contracts import ParsedResource

class ExistingMediaTests(unittest.TestCase):
    def setUp(self):
        self.parsed=ParsedResource(is_media=True,title="牧神记",media_type="anime",season=1,episodes=[106],year=2024,raw_fields={"tmdbid":236534})
        self.record={"provider":"aliyun","drive_id":"resource1","id":"file106","parent_id":"anime","title":"牧神记","name":"牧神记.S01E106.mkv","media_type":"anime","season":1,"episode":106,"tmdbid":236534,"year":2024,"size":800,"sha1":"ABC"}
        fs=SimpleNamespace(get_attr=Mock(return_value={"id":"file106","parent_id":"anime","name":"牧神记.S01E106.mkv","is_dir":False,"size":800,"sha1":"ABC"}))
        self.fs=fs
        self.cloud=SimpleNamespace(client=SimpleNamespace(drive_id="resource1"),library=Mock(return_value=fs))

    def test_confirmed_existing_file_is_verified_before_skipping(self):
        self.assertEqual(existing_media.check(self.cloud,[self.record],self.parsed,{},provider="aliyun"),{"covered":[[1,106]],"uncertain":""})
        self.fs.get_attr.assert_called_once_with("file106")

    def test_same_title_without_tmdb_or_year_is_uncertain_not_silently_skipped(self):
        weak={**self.record,"tmdbid":None,"year":None}
        result=existing_media.check(self.cloud,[weak],self.parsed,{},provider="aliyun")
        self.assertEqual(result["covered"],[])
        self.assertTrue(result["uncertain"])

    def test_wrong_drive_or_wrong_work_is_not_duplicated(self):
        self.assertFalse(existing_media.check(self.cloud,[{**self.record,"drive_id":"backup"}],self.parsed,{},provider="aliyun")["covered"])
        self.assertFalse(existing_media.check(self.cloud,[{**self.record,"tmdbid":999}],self.parsed,{},provider="aliyun")["covered"])

    def test_same_tmdb_id_and_episode_is_reused_even_after_filename_renamed(self):
        different = {**self.record, "title": "Ancient Tale"}
        self.assertEqual(existing_media.check(
            self.cloud, [different], self.parsed, {}, provider="aliyun")["covered"], [[1,106]])

    def test_conflicting_year_with_matching_tmdb_id_requires_review(self):
        different = {**self.record, "title": "Another Title", "year": 2025}
        result = existing_media.check(self.cloud, [different], self.parsed, {}, provider="aliyun")
        self.assertFalse(result["covered"])
        self.assertTrue(result["uncertain"])

    def test_observed_for_subscription_is_only_a_hint_not_verified_transfer(self):
        sub = {"title": "牧神记", "season": 1, "tmdbid": 236534}
        observed = [
            {**self.record, "episode": 105},
            self.record,
            {**self.record, "episode": 107, "tmdbid": 77777},
            {**self.record, "episode": 108, "season": 2},
            {**self.record, "episode": 110, "provider": "aliyun", "title": "Other"},
        ]
        self.assertEqual(existing_media.observed_for_subscription(observed, sub), [105, 106, 110])
        self.assertEqual(existing_media.observed_for_subscription(observed,
            {**sub, "tmdbid": 77777}), [107])

    def test_stale_file_causes_manual_review(self):
        self.fs.get_attr.return_value["parent_id"]="moved"
        result=existing_media.check(self.cloud,[self.record],self.parsed,{},provider="aliyun")
        self.assertEqual(result["covered"],[])
        self.assertTrue(result["uncertain"])

class CrossFolderTest(unittest.TestCase):
    def test_verified_entries_same_drive_different_folders_share_coverage(self):
        import hashlib
        from reelay.core import works
        from reelay.core.store import Store
        from reelay.core.store.base import DEDUP_EPISODE
        store=Store(":memory:")
        self.addCleanup(store.close)
        work=works.identity("牧神记",2024,236534,"tv")
        digest=hashlib.sha256(b"resource-1").hexdigest()
        other=hashlib.sha256(b"backup").hexdigest()
        store.mark(DEDUP_EPISODE,f"aliyun:resource_{digest}_first:{store.episode_key(work,1,106)}")
        store.mark(DEDUP_EPISODE,f"aliyun:resource_{other}_second:{store.episode_key(work,1,107)}")
        self.assertEqual(store.stored_episode_pairs_in_drive(work,"aliyun","resource-1"),[[1,106]])
        self.assertEqual(store.stored_episode_pairs_in_drive(work,"aliyun","nope"),[])
        self.assertEqual(store.stored_episode_pairs_in_drive(work,"p115"),[])

if __name__=="__main__":
    unittest.main()
