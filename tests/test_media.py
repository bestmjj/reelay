"""core/media.py：文件名季集识别、中文季号、集号区间、清晰度。"""
from __future__ import annotations

import unittest

from reelay.core.media import (cn_number, episode_list, episode_of, format_episodes, resolution_label,
                               resolution_name, resolution_rank, season_of, variety_episode)


class EpisodeOfTest(unittest.TestCase):
    def test_common_file_names(self):
        cases = {
            "繁花.S01E05.2160p.mkv": (1, 5),
            "Show.S02.EP03.mkv": (2, 3),
            "Show S01 E07 1080p.mp4": (1, 7),
            "某剧 第2季 第05集.mp4": (2, 5),
            "某剧 第二季 第12话.mkv": (2, 12),
            "第05集.mp4": (None, 5),
            "Squid Game 2021 E05.mkv": (None, 5),
            "[字幕组] 某番 - 05 [1080p].mkv": (None, 5),
            "[字幕组] 某番 S2 - 11v2 (1080p).mkv": (2, 11),
            "[字幕组][某番][07][1080p].mp4": (None, 7),
            "05.mkv": (None, 5),
            "41 4K.mp4": (None, 41),
            "106 4K.mp4": (None, 106),
            "106.2160p.mkv": (None, 106),
            "106 - 4K HDR.mp4": (None, 106),
            "106_1080p_HEVC.mp4": (None, 106),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(episode_of(name), expected)

    def test_years_and_resolutions_are_not_episodes(self):
        for name in ("House.1080p.03.mkv", "Movie.2023.2160p.mkv", "2021.mkv", "1080.mkv",
                     "Movie.mkv", "2024 4K.mp4", "1080p.mp4", "Season 4K.mp4",
                     "41 travel 4K.mp4", "4K HDR.mp4", "42 4K movie.mp4"):
            with self.subTest(name=name):
                self.assertIsNone(episode_of(name)[1])

    def test_variety_periods_dates_and_editions(self):
        sample = {
            "20261026.第3期加更.mp4": (3, "bonus"),
            "20261025.第3期下.mkv": (3, "lower"),
            "20261024.第3期上.mp4": (3, "upper"),
            "20261020.第2期还有加更.mp4": (2, "bonus_more"),
            "20261019.第2期加更.mp4": (2, "bonus"),
            "20261018.第2期下.mp4": (2, "lower"),
            "20261017.第2期上.mp4": (2, "upper"),
            "20261013.第1期还有加更：王安宇脑力下线白敬亭直跺脚.mkv": (1, "bonus_more"),
            "20261012.第1期加更：王楚然猜词变造词“气疯”沈腾.mp4": (1, "bonus"),
            "20261010.纯享 第01期上·慢速目击证人纯享.mp4": (1, "pure_upper"),
            "某节目 - S01E03 - 第 3 期（上） - 2160p.mkv": (3, "upper"),
            "某节目 - S01E03 - 第 3 期（下）.mp4": (3, "lower"),
            "20261026.第3期.mp4": (3, "main"),
        }
        for name, expected in sample.items():
            with self.subTest(name=name):
                self.assertEqual(variety_episode(name), expected)
                self.assertEqual(episode_of(name), (1, expected[0]) if name.startswith("某节目") else (None, expected[0]))

    def test_variety_periods_fail_closed_for_unknown_editions_or_date_only(self):
        for name in ("20261026.mp4", "20261026.加更.mp4", "第03期未知版.mkv",
                     "第3期（删减版）.mp4", "第3期 第4期加更.mkv",
                     "第0期加更.mp4", "第3期上和下.mp4"):
            with self.subTest(name=name):
                self.assertIsNone(variety_episode(name))
                self.assertIsNone(episode_of(name)[1])

    def test_season_marker_without_episode(self):
        self.assertEqual(episode_of("某剧 第二季 合集.mkv"), (2, None))


class NumbersTest(unittest.TestCase):
    def test_chinese_numbers(self):
        for text, value in {"一": 1, "两": 2, "十": 10, "十二": 12, "二十": 20, "二十三": 23, "7": 7}.items():
            self.assertEqual(cn_number(text), value)
        self.assertIsNone(cn_number("百"))
        self.assertIsNone(cn_number("一二"))

    def test_season_of(self):
        self.assertEqual(season_of("漫长的季节 第二季 更新至 5 集"), 2)
        self.assertEqual(season_of("Show S03 1080p"), 3)
        self.assertEqual(season_of("Show Season 4"), 4)
        self.assertIsNone(season_of("Show 2023 1080p"))

    def test_episode_list(self):
        self.assertEqual(episode_list("1-3,5、E07-E08"), [1, 2, 3, 5, 7, 8])
        with self.assertRaises(ValueError):
            episode_list("5-1")
        with self.assertRaises(ValueError):
            episode_list("第五集")

    def test_format_episodes(self):
        self.assertEqual(format_episodes([1, 2, 3, 5]), "E01-E03、E05")


class ResolutionTest(unittest.TestCase):
    def test_names_and_ranks(self):
        self.assertEqual(resolution_name("✨4K HDR"), "2160p")
        self.assertEqual(resolution_name("1080i"), "1080p")
        self.assertEqual(resolution_rank("WEB-DL 720p"), 2)
        self.assertEqual(resolution_label("576p"), "标清")
        self.assertIsNone(resolution_name("HDR WEB-DL"))
