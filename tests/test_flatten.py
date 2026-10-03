"""統一名・巻フォルダ出力(外側のみ無圧縮ラップ ZIP)のテスト(合成データ)。"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from comic_dedupe import constants as C
from comic_dedupe import naming, pipeline
from comic_dedupe.logging_setup import setup_logger
from comic_dedupe.tests.helpers import write_pages, zip_folder
from comic_dedupe.tests.test_pipeline import build_input_tree


def volume_folders(names: list[str]) -> dict[str, list[str]]:
    """ラップ ZIP のエントリを「最上位の名前 → その直下のファイル名」にまとめる。"""

    result: dict[str, list[str]] = {}
    for name in names:
        if name.endswith("/"):
            continue
        parts = name.split("/")
        result.setdefault(parts[0], []).append("/".join(parts[1:]))
    return result


class DisplaySeriesTest(unittest.TestCase):
    def test_display_series(self) -> None:
        cases = {
            "[作者] 作品名 第09巻 (DL版).zip": "作品名",
            "作品名09.zip": "作品名",
            "07.zip": "",
            "ONE PIECE v01s.rar": "ONE PIECE",
            "作品名 完全版 第10巻.zip": "作品名 完全版",
            "作品名 v01-05.zip": "作品名",
            "13DL.APP-Hyouken_no_Majutsushi_ga_Sekai v01.zip": "Hyouken_no_Majutsushi_ga_Sekai",
        }
        for name, expected in cases.items():
            self.assertEqual(naming.display_series(name), expected, name)


class FlattenOutputTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        setup_logger(quiet_console=True)

    def _run(self, path: Path, **options) -> tuple:
        result = pipeline.Pipeline(pipeline.JobOptions(wrap_format="zip", **options)).run(path)
        self.assertTrue(result.ok, result.error)
        with zipfile.ZipFile(result.output_path) as wrapper:
            infos = wrapper.infolist()
            names = [info.filename for info in infos]
            # 外側のラップ ZIP は無圧縮
            self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED for i in infos))
        return result, names

    def test_volume_folders_with_unified_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = build_input_tree(root)
            result, names = self._run(content)

            self.assertEqual(result.output_path.name, f"入力{C.OUTPUT_SUFFIX}.zip")
            folders = volume_folders(names)
            expected_volumes = {
                "作品名 第07巻", "作品名 第08巻", "作品名 第09巻", "作品名 第10巻",
                "作品名 第11巻", "作品名 第12巻", "作品名 完全版 第10巻",
            }
            self.assertEqual(set(folders) - {C.DUP_DIR_NAME}, expected_volumes)
            # 巻ごとの ZIP は作らない。画像は巻フォルダ直下(入れ子なし)
            for volume in expected_volumes:
                self.assertTrue(all("/" not in f and f.endswith(".jpg") for f in folders[volume]), volume)
            self.assertFalse(any(n.endswith(".zip") and not n.startswith(C.DUP_DIR_NAME) for n in names))
            # 敗者は _重複/ にそのまま
            self.assertIn(f"{C.DUP_DIR_NAME}/07.zip", names)
            # 原本は無傷
            self.assertTrue((content / "第7巻").is_dir())

    def test_archive_input_and_leftovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = build_input_tree(root)
            (content / "メモ.txt").write_text("memo", encoding="utf-8")
            input_zip = zip_folder(content, root / "まとめ.zip")

            result, names = self._run(input_zip)

            self.assertEqual(result.output_path.name, f"まとめ{C.OUTPUT_SUFFIX}.zip")
            self.assertIn(f"{C.OTHER_DIR_NAME}/メモ.txt", names)
            self.assertIn("作品名 第07巻/001.jpg", names)

    def test_zip_volume_is_extracted_and_single_child_folder_hoisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = root / "入力"
            # ZIP 内に余計なフォルダが 1 段入っている書庫
            zip_folder(write_pages(root / "s" / "Foo v1" / "pages", 3, 60, 85), root / "s" / "Foo v1.zip")
            (content).mkdir()
            zip_folder(root / "s" / "Foo v1", content / "Foo v1.zip")

            result, names = self._run(content)

            self.assertEqual(
                sorted(n for n in names if not n.endswith("/")),
                ["Foo 第01巻/001.jpg", "Foo 第01巻/002.jpg", "Foo 第01巻/003.jpg"],
            )

    def test_digits_follow_max_volume_and_unknown_volume_keeps_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = root / "入力"
            staging = root / "staging"
            zip_folder(write_pages(staging / "a", 3, 60, 85), content / "Foo v1.zip")
            zip_folder(write_pages(staging / "b", 3, 60, 85), content / "[x] Foo v100.zip")
            zip_folder(write_pages(staging / "c", 3, 60, 85), content / "おまけ.zip")

            _, names = self._run(content)

            self.assertEqual(
                sorted(volume_folders(names)), ["Foo 第001巻", "Foo 第100巻", "おまけ"]
            )

    def test_romaji_and_japanese_names_are_unified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = root / "入力"
            staging = root / "staging"
            names_widths = {
                "13DL.APP-Hyouken_no_Majutsushi_ga_Sekai v01.zip": 120,
                "13DL.APP-Hyouken_no_Majutsushi_ga_Sekai v02.zip": 120,
                "13DL.APP-冰剣の魔術師が世界を統べる v01.zip": 60,
                "13DL.APP-冰剣の魔術師が世界を統べる v03.zip": 120,
            }
            for index, (name, width) in enumerate(names_widths.items()):
                zip_folder(write_pages(staging / str(index), 3, width, width + 40), content / name)

            _, names = self._run(content)

            volumes = sorted(v for v in volume_folders(names) if v != C.DUP_DIR_NAME)
            self.assertEqual(volumes, [f"冰剣の魔術師が世界を統べる 第0{n}巻" for n in (1, 2, 3)])

    def test_files_folders_merge_with_named_volumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = root / "入力"
            for n in (8, 9, 10):
                write_pages(content / f"{n}_files", 3, 100 + n, 140)          # 巻数だけの名前
                write_pages(content / f"怪物事変 第{n:02d}巻", 3, 100 + n, 140)  # 作品名付き
            write_pages(content / "8_files (2)", 3, 108, 140)               # Windows のコピー
            write_pages(content / "cover", 3, 80, 100)                      # 巻数なし

            result, names = self._run(content)

            folders = volume_folders(names)
            self.assertEqual(
                sorted(f for f in folders if f != C.DUP_DIR_NAME),
                ["cover", "怪物事変 第08巻", "怪物事変 第09巻", "怪物事変 第10巻"],
            )
            dup_folders = {n.split("/")[1] for n in names if n.startswith(C.DUP_DIR_NAME + "/")}
            self.assertEqual(len(dup_folders), 4)  # 8 巻の 3 つ中 2 つ + 9・10 巻の各 1 つ
            self.assertEqual(result.pending_count, 1)  # cover だけが保留

    def test_chapter_packs_stay_separate_from_volumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = root / "入力"
            staging = root / "staging"
            names = [
                "DLRAW.TO_Kingdom ch658-671.zip",
                "DLRAW.TO_Kingdom ch669-680.zip",
                "DLRAW.TO_Kingdom ch658-671 (2).zip",   # コピー(重複)
                "DLRAW.TO_Kingdom v12.zip",
            ]
            for index, name in enumerate(names):
                zip_folder(write_pages(staging / str(index), 3, 100 + index, 140), content / name)

            result, names_in_zip = self._run(content)

            folders = volume_folders(names_in_zip)
            self.assertEqual(
                sorted(f for f in folders if f != C.DUP_DIR_NAME),
                ["Kingdom ch658-671", "Kingdom ch669-680", "Kingdom 第12巻"],
            )
            # 同じ話数範囲のコピーだけが重複として退避される
            self.assertEqual(len({n.split("/")[1] for n in names_in_zip if n.startswith(C.DUP_DIR_NAME + "/")}), 1)

    def test_tiny_magazine_folders_are_excluded_from_volumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = root / "入力"
            for n in range(1, 7):                       # 本物の巻(各 12 ページ)
                write_pages(content / "巻" / f"作品 第{n:02d}巻", 12, 200, 280)
            for n in (33, 35):                          # 雑誌(各 3 ページ・小さい)。名前は巻数に見える
                write_pages(content / "雑誌" / f"週刊誌 2021 No.{n}", 3, 60, 85, quality=30)
            write_pages(content / "雑誌" / "週刊誌 2021 No.33-34", 3, 60, 85, quality=30)
            write_pages(content / "雑誌" / "週刊誌 2021 No.36", 3, 60, 85, quality=30)

            result, names = self._run(content)

            folders = volume_folders(names)
            volumes = sorted(f for f in folders if f.startswith("作品"))
            self.assertEqual(volumes, [f"作品 第0{n}巻" for n in range(1, 7)])
            self.assertFalse(any("33-34" in f or "33_34" in f for f in folders if f != C.EXCLUDED_DIR_NAME))
            excluded = {n.split("/")[1] for n in names if n.startswith(C.EXCLUDED_DIR_NAME + "/")}
            self.assertEqual(
                excluded,
                {"週刊誌 2021 No.33", "週刊誌 2021 No.35", "週刊誌 2021 No.33-34", "週刊誌 2021 No.36"},
            )

    def test_unmeasurable_duplicate_goes_to_pending_not_a_second_volume_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = root / "入力"
            write_pages(content / "作品 第01巻", 4, 100, 140)
            (content / "作品 v01.zip").write_bytes(b"not a zip at all")   # 読めない同じ巻
            write_pages(content / "作品 第02巻", 4, 100, 140)

            result, names = self._run(content)

            folders = volume_folders(names)
            self.assertEqual(sorted(f for f in folders if not f.startswith("_")), ["作品 第01巻", "作品 第02巻"])
            self.assertIn(f"{C.PENDING_DIR_NAME}/作品 v01.zip", names)
            self.assertFalse(any("(2)" in f for f in folders))

    def test_replace_original_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = build_input_tree(root)
            result = pipeline.Pipeline(pipeline.JobOptions(wrap_format="zip", replace=True)).run(content)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.output_path, root / "入力.zip")
            self.assertFalse(content.exists())
            with zipfile.ZipFile(result.output_path) as wrapper:
                self.assertIn("作品名 第07巻/001.jpg", wrapper.namelist())

    def test_workspace_is_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = build_input_tree(root)
            self._run(content)
            self.assertEqual(list(root.glob("**/_dedupe_work_*")), [])

    def test_staging_and_workspace_removed_on_failure_and_cancel(self) -> None:
        import threading
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = build_input_tree(root)
            # 失敗: ラップ書庫の作成で例外 → 組み立て用フォルダは残さない(作業領域は調査用に残る仕様)
            work = root / "work"
            with mock.patch("comic_dedupe.repack.zip_work_tree", side_effect=OSError("boom")):
                result = pipeline.Pipeline(
                    pipeline.JobOptions(wrap_format="zip", work_dir=work)
                ).run(content)
            self.assertFalse(result.ok)
            self.assertEqual(list(work.glob("_dedupe_work_flat_*")), [])
            self.assertEqual(len(list(work.glob("_dedupe_work_*"))), 1, "失敗時の作業領域は調査用に残る")
            # 中止: 作業領域も残さない
            work2 = root / "work2"
            cancel = threading.Event()
            cancel.set()
            result = pipeline.Pipeline(
                pipeline.JobOptions(work_dir=work2), cancel_event=cancel
            ).run(content)
            self.assertFalse(result.ok)
            self.assertEqual(list(work2.glob("_dedupe_work_*")), [])


if __name__ == "__main__":
    unittest.main()
