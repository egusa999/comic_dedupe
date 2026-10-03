"""タイトル正規化と巻数抽出のテスト。"""

from __future__ import annotations

import unittest

from comic_dedupe import constants as C
from comic_dedupe import naming


class ParseVolumeTest(unittest.TestCase):
    def test_explicit_volume_patterns(self) -> None:
        cases = {
            "作品名 第07巻.zip": "7",
            "作品名 第7巻": "7",
            "作品名 vol.7.cbz": "7",
            "作品名 v07.rar": "7",
            "作品名 (7).zip": "7",
            "作品名 [7].zip": "7",
            "作品名 #7.zip": "7",
            "作品名 7巻.zip": "7",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(naming.parse_title(name).volume, expected)

    def test_zero_padding_and_fullwidth_are_equivalent(self) -> None:
        for name in ("07.zip", "7.zip", "０７.zip", "第０７巻.zip"):
            with self.subTest(name=name):
                self.assertEqual(naming.parse_title(name).volume, "7")

    def test_bare_trailing_number(self) -> None:
        self.assertEqual(naming.parse_title("作品名09.zip").volume, "9")
        self.assertEqual(naming.parse_title("作品名 09").volume, "9")

    def test_part_words(self) -> None:
        key = naming.parse_title("作品名 下巻.zip")
        self.assertEqual(key.volume, "3")
        self.assertEqual(key.volume_kind, C.VOLUME_KIND_PART)

    def test_range_is_separate_kind(self) -> None:
        key = naming.parse_title("作品名 第1-10巻.zip")
        self.assertEqual(key.volume, "1-10")
        self.assertEqual(key.volume_kind, C.VOLUME_KIND_RANGE)

    def test_no_volume(self) -> None:
        self.assertIsNone(naming.parse_title("作品名.zip").volume)


class NoiseAndSeriesTest(unittest.TestCase):
    def test_noise_is_stripped(self) -> None:
        left = naming.parse_title("[作者] 作品名 第09巻 (DL版).zip")
        right = naming.parse_title("作品名09.zip")
        self.assertEqual(left.series, right.series)
        self.assertEqual(left.volume, right.volume)

    def test_resolution_tag_is_noise(self) -> None:
        key = naming.parse_title("作品名 第3巻 1600x2300 高画質.zip")
        self.assertEqual(key.volume, "3")
        self.assertEqual(key.series, naming.parse_title("作品名 第3巻.zip").series)

    def test_katakana_and_hiragana_are_unified(self) -> None:
        self.assertEqual(
            naming.normalize_series("テスト"), naming.normalize_series("てすと")
        )

    def test_edition_words_are_detected(self) -> None:
        key = naming.parse_title("作品名 完全版 第10巻.zip")
        self.assertIn("完全版", key.editions)
        self.assertEqual(naming.parse_title("作品名 第10巻.zip").editions, frozenset())

    def test_volume_only_names_have_empty_series(self) -> None:
        self.assertEqual(naming.parse_title("第7巻").series, "")
        self.assertEqual(naming.parse_title("07.zip").series, "")


if __name__ == "__main__":
    unittest.main()


class FilesFolderAndCopySuffixTest(unittest.TestCase):
    def test_files_folder_is_a_volume(self) -> None:
        for name, volume in (("8_files", "8"), ("16_files", "16"), ("08_files", "8")):
            key = naming.parse_title(name)
            self.assertEqual((key.series, key.volume), ("", volume), name)
        self.assertFalse(naming.parse_title("cover").has_volume)

    def test_windows_copy_suffix_is_not_a_volume(self) -> None:
        self.assertEqual(naming.parse_title("8_files (2)").volume, "8")
        self.assertEqual(naming.parse_title("怪物事変 第01巻 (2)").volume, "1")
        self.assertEqual(naming.parse_title("作品名 07 (2)").volume, "7")
        # 唯一の巻数表記なら巻数として残す
        self.assertEqual(naming.parse_title("作品名 (2)").volume, "2")


class ChapterNotationTest(unittest.TestCase):
    def test_chapter_is_separate_from_volume(self) -> None:
        for name, volume in (
            ("DLRAW.TO_Kingdom ch658-671", "658-671"),
            ("DLRAW.TO_Kingdom ch700", "700"),
            ("作品 第12話", "12"),
            ("作品 第12-15話", "12-15"),
            ("作品 Chapter 5", "5"),
        ):
            key = naming.parse_title(name)
            self.assertEqual(key.volume_kind, C.VOLUME_KIND_CHAPTER, name)
            self.assertEqual(key.volume, volume, name)
        # 巻とは別のグループキーになる(ch12 と 12巻は混ざらない)
        self.assertNotEqual(
            naming.volume_group_key(naming.parse_title("作品 ch12")),
            naming.volume_group_key(naming.parse_title("作品 第12巻")),
        )
        self.assertEqual(naming.parse_title("Church 05").volume_kind, C.VOLUME_KIND_NUMBER)

    def test_impossible_ranges_are_not_volumes(self) -> None:
        for name in ("Kingdom 04-00", "Kingdom 00-01", "Kingdom 00-00", "Kingdom v05-03"):
            key = naming.parse_title(name)
            self.assertFalse(key.has_volume, name)
        self.assertEqual(naming.parse_title("Kingdom ovl 01-75").volume, "1-75")

    def test_garbled_names_are_recovered(self) -> None:
        # CP932 の名前が CP437 で解釈された文字化けは元に戻す
        self.assertEqual(naming.repair_mojibake("âLâôâOâ_âÇ30è¬"), "キングダム30巻")
        self.assertEqual(naming.parse_title("DLRAW.TO_âLâôâOâ_âÇ30è¬").volume, "30")
        # 正しい名前・ASCII の名前は触らない
        self.assertEqual(naming.repair_mojibake("キングダム 第01巻"), "キングダム 第01巻")

    def test_repair_cp1252_and_partial_mojibake(self):
        self.assertIn("第01巻", naming.repair_mojibake("[ƒ€ƒ‰ƒ^ƒRƒEƒW] ‚—ä‚Ìƒnƒi‚³‚ñ ‘æ01Šª"))
        self.assertIn("第01巻", naming.repair_mojibake("[è¦ÄRé¡é¬é-ü~É[ÄRâtâMâô] âIü[âoü[âìü[âh æµ01è¬"))

    def test_site_prefix_noise(self):
        for raw in ("DLRAW.APP_ガチャ", "13DL.ME_ガチャ", "MANGA-ZIP.APP_ガチャ"):
            self.assertEqual(naming.strip_noise(naming.basic_normalize(raw)), "ガチャ")
        self.assertEqual(naming.repair_mojibake("Kingdom v12"), "Kingdom v12")
        # 「第04巻」が `_` に置き換わった名前も巻数だけは拾う
        self.assertEqual(naming.parse_title("[___] _____ -KINGDOM- _04_").volume, "4")

    def test_site_prefix_and_v_range(self) -> None:
        a = naming.parse_title("DLRAW.TO_Kingdom v01-04")
        b = naming.parse_title("DLRAW.TO_Kingdom v12")
        self.assertEqual((a.series, a.volume, a.volume_kind), ("kingdom", "1-4", C.VOLUME_KIND_RANGE))
        self.assertEqual(a.series, b.series)
        c = naming.parse_title("DLRAW.TO_Kingdom v01-10b")
        self.assertEqual((c.volume, c.volume_kind), ("1-10", C.VOLUME_KIND_RANGE))


class DefaultLogDirTest(unittest.TestCase):
    def test_default_log_dir_is_inside_app_folder(self) -> None:
        from pathlib import Path

        from comic_dedupe import cli, constants as C
        from comic_dedupe.logging_setup import default_log_dir

        app_dir = Path(cli.__file__).resolve().parent
        self.assertEqual(default_log_dir(), app_dir / C.LOG_DIR_NAME)
        self.assertEqual(cli._log_directory(cli._LOG_SENTINEL), default_log_dir())
        self.assertIsNone(cli._log_directory(None))
