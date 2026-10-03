"""あいまい照合(自動一致 / グレーゾーン / 別物)のテスト。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from comic_dedupe import constants as C
from comic_dedupe import matching, naming
from comic_dedupe.models import VolumeItem


def make_item(name: str, kind: str = "zip") -> VolumeItem:
    return VolumeItem(
        path=Path(name),
        kind=kind,
        title=naming.parse_title(name),
        rel_path=name,
        display_name=name,
    )


class SimilarityTest(unittest.TestCase):
    def test_same_series_is_exact(self) -> None:
        score, method = matching.similarity(
            naming.parse_title("作品名 第1巻.zip"), naming.parse_title("作品名 01.zip")
        )
        self.assertEqual(score, 1.0)
        self.assertEqual(method, matching.METHOD_EXACT)

    def test_volume_only_names_match(self) -> None:
        score, method = matching.similarity(
            naming.parse_title("第7巻"), naming.parse_title("07.zip")
        )
        self.assertEqual(score, C.EMPTY_SERIES_SIMILARITY)
        self.assertEqual(method, matching.METHOD_VOLUME_ONLY)

    def test_one_sided_series_is_review_zone(self) -> None:
        score, method = matching.similarity(
            naming.parse_title("第7巻"), naming.parse_title("作品名 第7巻.zip")
        )
        self.assertEqual(method, matching.METHOD_ONE_SIDED)
        self.assertLess(score, C.SERIES_SIMILARITY_MIN)
        self.assertGreaterEqual(score, C.SERIES_SIMILARITY_REVIEW)

    def test_edition_difference_is_rejected(self) -> None:
        score, method = matching.similarity(
            naming.parse_title("作品名 第10巻.zip"),
            naming.parse_title("作品名 完全版 第10巻.zip"),
        )
        self.assertEqual(score, 0.0)
        self.assertEqual(method, matching.METHOD_EDITION_DIFF)

    def test_substring_relation(self) -> None:
        score, _ = matching.similarity(
            naming.parse_title("あいうえおかきくけこ 第2巻.zip"),
            naming.parse_title("あいうえおかきくけこ さしすせそ 第2巻.zip"),
        )
        self.assertGreaterEqual(score, C.SUBSTRING_SIMILARITY)

    def test_different_series_do_not_match(self) -> None:
        score, _ = matching.similarity(
            naming.parse_title("まったく違う作品 第1巻.zip"),
            naming.parse_title("別の物語 第1巻.zip"),
        )
        self.assertLess(score, C.SERIES_SIMILARITY_REVIEW)


class GroupItemsTest(unittest.TestCase):
    def test_same_volume_is_grouped(self) -> None:
        items = [make_item("第7巻.zip"), make_item("07.zip"), make_item("第8巻.zip")]
        groups, reviews, no_volume = matching.group_items(items)
        duplicates = [group for group in groups if group.is_duplicate]
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(len(duplicates[0].items), 2)
        self.assertEqual(reviews, [])
        self.assertEqual(no_volume, [])

    def test_gray_zone_goes_to_review_not_group(self) -> None:
        # 作品名が複数混在するフォルダでは、作品名の無い巻を勝手に引き継がせず保留にする
        items = [
            make_item("第7巻.zip"),
            make_item("作品名 第7巻.zip"),
            make_item("まったく別の本 第1巻.zip"),
        ]
        groups, reviews, _ = matching.group_items(items)
        self.assertEqual([group for group in groups if group.is_duplicate], [])
        self.assertEqual(len(reviews), 1)

    def test_nameless_volume_adopts_single_series_of_folder(self) -> None:
        # 作品名のある巻がすべて同じ作品名のフォルダなら、作品名の無い巻は同じ作品として結合する
        items = [make_item("第7巻.zip"), make_item("作品名 第7巻.zip"), make_item("作品名 第8巻.zip")]
        groups, reviews, _ = matching.group_items(items)
        duplicates = [group for group in groups if group.is_duplicate]
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(len(duplicates[0].items), 2)
        self.assertEqual(reviews, [])

    def test_approved_pair_is_grouped(self) -> None:
        items = [make_item("第7巻.zip"), make_item("作品名 第7巻.zip")]
        approved = {matching.pair_key(items[0], items[1])}
        groups, reviews, _ = matching.group_items(items, approved_pairs=approved)
        self.assertEqual(len([g for g in groups if g.is_duplicate]), 1)
        self.assertEqual(reviews, [])

    def test_accept_review_option(self) -> None:
        items = [make_item("第7巻.zip"), make_item("作品名 第7巻.zip")]
        groups, reviews, _ = matching.group_items(items, accept_review=True)
        self.assertEqual(len([g for g in groups if g.is_duplicate]), 1)
        self.assertEqual(reviews, [])

    def test_items_without_volume_are_pending(self) -> None:
        items = [make_item("作品名.zip"), make_item("別作品.zip")]
        groups, reviews, no_volume = matching.group_items(items)
        self.assertEqual(groups, [])
        self.assertEqual(reviews, [])
        self.assertEqual(len(no_volume), 2)

    def test_different_volumes_are_not_grouped(self) -> None:
        items = [make_item("第7巻.zip"), make_item("第8巻.zip")]
        groups, _, _ = matching.group_items(items)
        self.assertEqual([g for g in groups if g.is_duplicate], [])


class AliasTest(unittest.TestCase):
    def test_alias_file_merges_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            alias_path = Path(tmp) / "alias.json"
            alias_path.write_text(
                json.dumps({"ワンピース": ["one piece"]}, ensure_ascii=False),
                encoding="utf-8",
            )
            aliases = matching.load_aliases(alias_path)
        score, method = matching.similarity(
            naming.parse_title("ワンピース 第7巻.zip"),
            naming.parse_title("ONE PIECE 07.zip"),
            aliases,
        )
        self.assertEqual(score, 1.0)
        self.assertEqual(method, matching.METHOD_ALIAS)

    def test_broken_alias_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            alias_path = Path(tmp) / "alias.json"
            alias_path.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertRaises(ValueError):
                matching.load_aliases(alias_path)


if __name__ == "__main__":
    unittest.main()


class OverflowRetryTest(unittest.TestCase):
    def _run(self, names):
        items = [make_item(n) for n in names]
        groups, reviews, _ = matching.group_items(items)
        return matching.retry_overflowing_scopes(groups, reviews)

    def test_overflow_merges_across_series_names(self) -> None:
        names = ["Hyoken v01.zip", "Hyoken v02.zip", "氷剣の魔術師 第01巻.zip", "氷剣の魔術師 第02巻.zip"]
        groups, _, notes = self._run(names)
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(len(g.items) == 2 for g in groups))
        self.assertTrue(notes)

    def test_overflow_detected_through_parent_when_volumes_in_separate_folders(self) -> None:
        names = ["Hyoken v01", "Hyoken v02", "氷剣の魔術師 第01巻", "氷剣の魔術師 第02巻"]
        items = []
        for index, name in enumerate(names):
            item = make_item(name)
            item.rel_path = f"root/w{index}/{name}"
            items.append(item)
        groups, reviews, _ = matching.group_items(items)
        groups, _, notes = matching.retry_overflowing_scopes(groups, reviews)
        self.assertEqual(len(groups), 2)
        self.assertTrue(notes)

    def test_no_merge_when_count_within_max(self) -> None:
        names = ["作品A 第01巻.zip", "作品B 第02巻.zip", "作品C 第03巻.zip"]
        groups, _, notes = self._run(names)
        self.assertEqual(len(groups), 3)
        self.assertEqual(notes, [])


class PageNumberAndSuffixTest(unittest.TestCase):
    def test_suffix_letters_after_volume(self) -> None:
        for name, vol in [("x v01s.zip", "1"), ("x v12ss.zip", "12"), ("x 05w.zip", "5"),
                          ("x_v16.zip", "16"), ("作品 第06巻s.zip", "6")]:
            with self.subTest(name=name):
                self.assertEqual(naming.parse_title(name).volume, vol)

    def test_no_fallback_ignores_bare_numbers(self) -> None:
        self.assertIsNone(naming.parse_title("001.jpg", use_fallback=False).volume)
