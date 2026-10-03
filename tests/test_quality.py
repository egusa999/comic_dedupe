"""画質比較・勝者選定のテスト。"""

from __future__ import annotations

import unittest
from pathlib import Path

from comic_dedupe import constants as C
from comic_dedupe import naming, quality
from comic_dedupe.models import DupGroup, PageInfo, VolumeItem


def make_item(
    name: str,
    pages: int,
    width: int,
    height: int,
    page_bytes: int,
    kind: str = "zip",
) -> VolumeItem:
    page_infos = [
        PageInfo(name=f"{i:03d}.jpg", width=width, height=height, size_bytes=page_bytes)
        for i in range(1, min(pages, C.SAMPLE_PAGES) + 1)
    ]
    return VolumeItem(
        path=Path(name),
        kind=kind,
        title=naming.parse_title(name),
        rel_path=name,
        display_name=name,
        page_count=pages,
        pages=page_infos,
        total_bytes=page_bytes * pages,
    )


class CompareTest(unittest.TestCase):
    def test_page_count_is_ignored(self) -> None:
        more = make_item("第8巻.zip", 20, 800, 1100, 100_000)
        fewer = make_item("08.zip", 15, 1600, 2200, 400_000)
        sign, reason = quality.compare_items(more, fewer)
        self.assertLess(sign, 0)
        self.assertIn("解像度", reason)

    def test_resolution_then_bytes_per_pixel(self) -> None:
        same_pixels_high_bpp = make_item("a.zip", 10, 1000, 1000, 200_000)
        same_pixels_low_bpp = make_item("b.zip", 10, 1000, 1000, 100_000)
        sign, reason = quality.compare_items(same_pixels_high_bpp, same_pixels_low_bpp)
        self.assertGreater(sign, 0)
        self.assertIn("bytes/pixel", reason)

    def test_tiebreak_by_source_kind(self) -> None:
        as_zip = make_item("a.zip", 10, 1000, 1000, 100_000, kind="zip")
        as_folder = make_item("a", 10, 1000, 1000, 100_000, kind="folder")
        sign, reason = quality.compare_items(as_zip, as_folder)
        self.assertGreater(sign, 0)
        self.assertIn("書庫種別", reason)


class SelectWinnerTest(unittest.TestCase):
    def test_higher_resolution_wins(self) -> None:
        high = make_item("第7巻", 20, 1600, 2200, 400_000, kind="folder")
        low = make_item("07.zip", 20, 800, 1100, 100_000)
        decision = quality.select_winner(DupGroup(key="g", items=[high, low]))
        self.assertIs(decision.winner, high)
        self.assertEqual(decision.losers, [low])
        self.assertTrue(decision.decided)

    def test_different_page_count_is_still_decided(self) -> None:
        full = make_item("第7巻.zip", 20, 1600, 2200, 400_000)
        partial = make_item("07.zip", 5, 1600, 2200, 400_000)
        decision = quality.select_winner(DupGroup(key="g", items=[full, partial]))
        self.assertIsNotNone(decision.winner)
        self.assertEqual(len(decision.losers), 1)

    def test_unmeasurable_item_is_pending(self) -> None:
        good = make_item("第7巻.zip", 20, 1600, 2200, 400_000)
        broken = make_item("07.rar", 20, 1600, 2200, 400_000, kind="rar")
        broken.measure_error = "読めない"
        broken.pages = ()
        decision = quality.select_winner(DupGroup(key="g", items=[good, broken]))
        self.assertIs(decision.winner, good)
        self.assertIn(broken, decision.undecided)
        self.assertEqual(decision.losers, [])

    def test_content_verification_splits_mismatch(self) -> None:
        left = make_item("第7巻.zip", 10, 1600, 2200, 400_000)
        right = make_item("07.zip", 10, 800, 1100, 100_000)
        left.pages = [
            PageInfo(name=f"{i}.jpg", width=1600, height=2200, size_bytes=400_000, dhash=0)
            for i in range(10)
        ]
        right.pages = [
            PageInfo(
                name=f"{i}.jpg",
                width=800,
                height=1100,
                size_bytes=100_000,
                dhash=0xFFFFFFFFFFFFFFFF,
            )
            for i in range(10)
        ]
        decision = quality.select_winner(
            DupGroup(key="g", items=[left, right]), verify_content=True
        )
        self.assertIs(decision.winner, left)
        self.assertIn(right, decision.undecided)
        self.assertEqual(decision.losers, [])


class SizeOutlierTest(unittest.TestCase):
    def _volumes(self) -> list:
        # 通常サイズの 2〜5 巻(各 約 6MB)
        return [make_item(f"作品 第0{n}巻.zip", 60, 800, 1100, 100_000) for n in (2, 3, 4, 5)]

    def test_outliers_are_found_only_with_enough_references(self) -> None:
        small = make_item("作品 第01巻 切れ.zip", 3, 1600, 2200, 400_000)   # 1.2MB
        items = self._volumes() + [small]
        outliers = quality.find_size_outliers(items, lambda item: "")
        self.assertEqual(list(outliers), [id(small)])
        self.assertIn("極端に小さい", outliers[id(small)])
        # 基準になる単巻が少ないフォルダでは判定しない
        self.assertEqual(quality.find_size_outliers(items[:2] + [small], lambda item: ""), {})

    def test_range_and_chapter_packs_are_not_judged(self) -> None:
        big_range = make_item("作品 v01-10.zip", 600, 800, 1100, 100_000)
        big_chapter = make_item("作品 ch658-671.zip", 600, 800, 1100, 100_000)
        items = self._volumes() + [big_range, big_chapter]
        self.assertEqual(quality.find_size_outliers(items, lambda item: ""), {})

    def test_small_outlier_cannot_win_even_with_higher_resolution(self) -> None:
        normal = make_item("作品 第01巻.zip", 60, 800, 1100, 100_000)
        truncated = make_item("作品 第01巻 (別).zip", 3, 1600, 2200, 400_000)
        group = DupGroup(key="number:1:#0", items=[normal, truncated])
        # 画像数が桁違いに少ない(3 枚 / 60 枚)ので、外れ値指定が無くても勝者候補から外れる
        self.assertIs(quality.select_winner(group).winner, normal)
        outliers = quality.find_size_outliers(
            self._volumes() + [normal, truncated], lambda item: ""
        )
        decision = quality.select_winner(group, outliers=outliers)
        self.assertIs(decision.winner, normal)
        self.assertEqual(decision.losers, [truncated])
        self.assertIn("サイズ外れ値", decision.reasons[truncated.label()])

    def test_reference_climbs_to_parent_folder(self) -> None:
        # 雑誌 1 冊だけが入った別フォルダの小さな巻も、作品全体(親フォルダ)の巻と比べて外れ値になる
        def in_folder(item, folder):
            item.rel_path = f"{folder}/{item.rel_path}"
            return item

        volumes = [in_folder(v, "全巻/v01-08") for v in self._volumes()]
        magazine = in_folder(make_item("雑誌 No.01.zip", 19, 1020, 1000, 50_000), "全巻/ch658-671")
        scope = lambda item: item.rel_path.rpartition("/")[0]
        outliers = quality.find_size_outliers(volumes + [magazine], scope)
        self.assertEqual(list(outliers), [id(magazine)])

    def test_thin_candidate_cannot_win_even_when_it_is_the_only_one_in_its_folder(self) -> None:
        # フォルダ単位の外れ値に引っかからなくても、同じグループ内で画像数が桁違いに少なければ除外する
        real = make_item("作品 第01巻.zip", 222, 1002, 1000, 360_000)       # 本物(222 ページ)
        magazine = make_item("雑誌 No.01.zip", 19, 1020, 1000, 450_000)     # 19 ページ・高 bytes/pixel
        group = DupGroup(key="number:1:#0", items=[magazine, real])
        decision = quality.select_winner(group)
        self.assertIs(decision.winner, real)
        self.assertEqual(decision.losers, [magazine])
        self.assertIn("画像数", decision.reasons[magazine.label()])

    def test_page_count_differences_within_ratio_are_still_ignored(self) -> None:
        more = make_item("第8巻.zip", 20, 800, 1100, 100_000)
        fewer = make_item("08.zip", 15, 1600, 2200, 400_000)   # 75%: 版違いで普通にある差
        decision = quality.select_winner(DupGroup(key="number:8:#0", items=[more, fewer]))
        self.assertIs(decision.winner, fewer)

    def test_range_and_lone_small_items_are_judged_against_job_wide_median(self) -> None:
        # 雑誌が同じフォルダに 4 冊以上あっても(フォルダ内の中央値が雑誌自身になっても)、
        # 作品全体の中央値と比べて外れ値になる。範囲表記の雑誌合併号(No.33-34)も同様
        def at(item, folder):
            item.rel_path = f"{folder}/{item.rel_path}"
            return item

        volumes = [at(make_item(f"作品 第{n:02d}巻.zip", 60, 800, 1100, 100_000), "v") for n in range(1, 9)]
        magazines = [at(make_item(f"雑誌 No.{n:02d}.zip", 20, 1020, 1000, 40_000), "ch") for n in range(1, 6)]
        double = at(make_item("雑誌 No.33-34.zip", 21, 1020, 1000, 40_000), "ch")
        scope = lambda item: item.rel_path.rpartition("/")[0]
        outliers = quality.find_size_outliers(volumes + magazines + [double], scope)
        self.assertEqual(set(outliers), {id(m) for m in magazines} | {id(double)})
        self.assertIn("2 巻ぶんの想定サイズ", outliers[id(double)])

    def test_all_outliers_are_kept_in_comparison(self) -> None:
        a = make_item("作品 第01巻.zip", 3, 800, 1100, 100_000)
        b = make_item("作品 第01巻 (2).zip", 3, 1600, 2200, 400_000)
        group = DupGroup(key="number:1:#0", items=[a, b])
        decision = quality.select_winner(group, outliers={id(a): "x", id(b): "y"})
        self.assertIs(decision.winner, b)   # 全員が外れ値なら通常どおり比較する


class MeasureRetryTest(unittest.TestCase):
    def test_rar_measurement_retries_with_each_backend_from_the_listing(self) -> None:
        from unittest import mock

        from comic_dedupe import external_tools
        from comic_dedupe.models import EntryInfo

        item = VolumeItem(
            path=Path("巻.rar"), kind="rar", title=naming.parse_title("巻.rar"), rel_path="巻.rar"
        )

        class FakeSource:
            def __init__(self, backend: str) -> None:
                self.backend = backend

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                return None

        calls: list[str] = []

        def fake_open(path, files=None, rar_backend="auto"):
            calls.append(rar_backend)
            return FakeSource(rar_backend)

        def fake_measure(source, entries, want_hash=False):
            # auto と bsdtar では読めず、unrar なら読める(名前の文字コード食い違いの再現)
            if source.backend in ("auto", "bsdtar"):
                return []
            return [PageInfo(name="001.jpg", width=100, height=140, size_bytes=1000)]

        entries = [EntryInfo(name="001.jpg", size=1000)]
        with mock.patch.object(quality, "open_source", fake_open), \
                mock.patch.object(quality.pages, "list_page_entries", return_value=entries), \
                mock.patch.object(quality.pages, "sample_entries", return_value=entries), \
                mock.patch.object(quality.pages, "measure_pages", fake_measure), \
                mock.patch.object(external_tools, "available_rar_backends", return_value=("bsdtar", "unrar")):
            quality.measure_item(item)
        self.assertTrue(item.measured)
        self.assertEqual(calls, ["auto", "bsdtar", "unrar"])


if __name__ == "__main__":
    unittest.main()
