"""パイプラインのエンドツーエンドテスト(合成データ)。"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from comic_dedupe import constants as C
from comic_dedupe import pipeline
from comic_dedupe.archives import ArchiveError, open_source
from comic_dedupe.logging_setup import setup_logger
from comic_dedupe.tests.helpers import write_pages, zip_folder


def build_input_tree(root: Path) -> Path:
    """以下を含むテスト入力フォルダを作る。

    - 第7巻/(高画質) と 07.zip(低画質)        → 高画質が勝つ
    - 第8巻.zip(10p) と 08/(4p)              → ページ数は無視して通常判定
    - [作者] 作品名 第09巻 (DL版).zip と 作品名09.zip → あいまい一致で同一巻
    - 作品名 第10巻.zip と 作品名 完全版 第10巻.zip   → 版違いで別物
    - 第11巻.zip                              → 重複なし
    - その他/さらに下/第12巻/                 → 入れ子でも検出
    """

    content = root / "入力"
    staging = root / "staging"
    content.mkdir(parents=True)

    write_pages(content / "第7巻", 6, 120, 170, quality=92)
    zip_folder(write_pages(staging / "07", 6, 60, 85, quality=50), content / "07.zip")

    zip_folder(write_pages(staging / "v8", 10, 100, 140), content / "第8巻.zip")
    write_pages(content / "08", 4, 100, 140)

    zip_folder(
        write_pages(staging / "v9a", 5, 110, 155, quality=92),
        content / "[作者] 作品名 第09巻 (DL版).zip",
    )
    zip_folder(write_pages(staging / "v9b", 5, 60, 85, quality=50), content / "作品名09.zip")

    zip_folder(write_pages(staging / "v10a", 4, 90, 127), content / "作品名 第10巻.zip")
    zip_folder(
        write_pages(staging / "v10b", 4, 90, 127, seed=3),
        content / "作品名 完全版 第10巻.zip",
    )

    zip_folder(write_pages(staging / "v11", 3, 80, 113), content / "第11巻.zip")
    write_pages(content / "その他" / "さらに下" / "第12巻", 3, 85, 120)
    return content


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        setup_logger(quiet_console=True)

    def test_folder_input_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = build_input_tree(root)
            before = sorted(path.name for path in content.iterdir())

            options = pipeline.JobOptions(wrap_format="zip", flatten_output=False)
            result = pipeline.Pipeline(options).run(content)

            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.volume_count, 10)
            self.assertIsNotNone(result.output_path)
            # 原本は無傷
            self.assertEqual(sorted(path.name for path in content.iterdir()), before)

            with zipfile.ZipFile(result.output_path) as archive:
                infos = archive.infolist()
            names = sorted(info.filename for info in infos)

            # すべて無圧縮で格納されている
            self.assertTrue(all(info.compress_type == zipfile.ZIP_STORED for info in infos))
            # 勝者は巻ごとの ZIP になっている
            self.assertIn("第7巻.zip", names)
            self.assertIn("[作者] 作品名 第09巻 (DL版).zip", names)
            # 敗者は _重複/ 配下
            self.assertIn(f"{C.DUP_DIR_NAME}/07.zip", names)
            self.assertIn(f"{C.DUP_DIR_NAME}/作品名09.zip", names)
            # 版違いは両方残る
            self.assertIn("作品名 第10巻.zip", names)
            self.assertIn("作品名 完全版 第10巻.zip", names)
            # 重複なしの巻と入れ子の巻
            self.assertIn("第11巻.zip", names)
            self.assertIn("その他/さらに下/第12巻.zip", names)
            # ページ数は考慮しない: 総バイト数の大きい第8巻.zip が勝つ
            self.assertIn("第8巻.zip", names)
            self.assertIn(f"{C.DUP_DIR_NAME}/08.zip", names)

    def test_range_archive_is_expanded_and_judged_per_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = root / "入力"
            staging = root / "staging"
            inner = staging / "inner"
            # 範囲書庫(v01-03)の中に 1,2 巻(低画質)と、さらに範囲書庫 v04-05 が入っている
            zip_folder(write_pages(staging / "a1", 4, 60, 85, quality=40), inner / "作品 v01.zip")
            zip_folder(write_pages(staging / "a2", 4, 60, 85, quality=40), inner / "作品 v02.zip")
            deep = staging / "deep"
            zip_folder(write_pages(staging / "a4", 4, 60, 85), deep / "作品 v04.zip")
            zip_folder(write_pages(staging / "a5", 4, 60, 85), deep / "作品 v05.zip")
            zip_folder(deep, inner / "作品 v04-05.zip")
            zip_folder(inner, content / "作品 v01-05.zip")
            # 範囲書庫の外にある高画質の 1 巻
            write_pages(content / "作品 v01", 4, 120, 170, quality=92)

            result = pipeline.Pipeline(pipeline.JobOptions(wrap_format="zip", flatten_output=False)).run(content)

            self.assertTrue(result.ok, result.error)
            with zipfile.ZipFile(result.output_path) as archive:
                names = sorted(info.filename for info in archive.infolist())
            joined = "\n".join(names)
            self.assertNotIn("v01-05.zip", joined, "範囲書庫そのものは残らない")
            self.assertNotIn("v04-05.zip", joined)
            self.assertIn("作品 v01.zip", names, "高画質の v01 が勝つ")
            losers = [n for n in names if n.startswith(C.DUP_DIR_NAME)]
            self.assertTrue(any(n.endswith("作品 v01.zip") for n in losers), names)
            self.assertEqual(
                sum(1 for n in names if n.endswith("作品 v02.zip")), 1
            )
            self.assertTrue(any(n.endswith("作品 v04.zip") for n in names))
            self.assertTrue(any(n.endswith("作品 v05.zip") for n in names))

    def test_archive_input_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = build_input_tree(root)
            input_zip = zip_folder(content, root / "作品名まとめ.zip")

            result = pipeline.Pipeline(pipeline.JobOptions(wrap_format="zip", flatten_output=False)).run(input_zip)

            self.assertTrue(result.ok, result.error)
            self.assertTrue(input_zip.exists(), "原本の書庫は残る")
            self.assertEqual(result.output_path.name, f"作品名まとめ{C.OUTPUT_SUFFIX}.zip")
            with zipfile.ZipFile(result.output_path) as archive:
                names = sorted(info.filename for info in archive.infolist())
            self.assertIn("第7巻.zip", names)
            self.assertIn(f"{C.DUP_DIR_NAME}/07.zip", names)

    def test_dry_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = build_input_tree(root)
            snapshot = sorted(
                (path.relative_to(content).as_posix(), path.stat().st_size)
                for path in content.rglob("*")
                if path.is_file()
            )

            result = pipeline.Pipeline(pipeline.JobOptions(wrap_format="zip", dry_run=True)).run(content)

            self.assertTrue(result.ok, result.error)
            self.assertIsNone(result.output_path)
            self.assertEqual(result.moved_count, 0)
            after = sorted(
                (path.relative_to(content).as_posix(), path.stat().st_size)
                for path in content.rglob("*")
                if path.is_file()
            )
            self.assertEqual(snapshot, after)
            self.assertEqual(list(root.glob("*_整理済み.zip")), [])
            # 判定計画は立っている
            self.assertTrue(any(action.kind == "move" for action in result.actions))

    def test_delete_losers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = build_input_tree(root)
            result = pipeline.Pipeline(
                pipeline.JobOptions(wrap_format="zip", delete_losers=True, flatten_output=False)
            ).run(content)
            self.assertTrue(result.ok, result.error)
            with zipfile.ZipFile(result.output_path) as archive:
                names = [info.filename for info in archive.infolist()]
            self.assertFalse(any(name.startswith(C.DUP_DIR_NAME) for name in names))
            self.assertGreater(result.deleted_count, 0)

    def test_replace_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = build_input_tree(root)
            input_zip = zip_folder(content, root / "まとめ.zip")
            result = pipeline.Pipeline(pipeline.JobOptions(wrap_format="zip", replace=True, flatten_output=False)).run(input_zip)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.output_path.name, "まとめ.zip")
            self.assertEqual(sorted(p.name for p in root.glob("まとめ*.zip")), ["まとめ.zip"])

    def test_invalid_input_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "存在しない.zip"
            result = pipeline.Pipeline(pipeline.JobOptions(wrap_format="zip", )).run(missing)
            self.assertFalse(result.ok)
            self.assertIn("入力が存在しない", result.error)

    def test_unsupported_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            text = Path(tmp) / "memo.txt"
            text.write_text("x", encoding="utf-8")
            result = pipeline.Pipeline(pipeline.JobOptions(wrap_format="zip", )).run(text)
            self.assertFalse(result.ok)
            self.assertIn("対応していない入力形式", result.error)

    def test_work_dir_is_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = build_input_tree(root)
            pipeline.Pipeline(pipeline.JobOptions(wrap_format="zip", flatten_output=False)).run(content)
            leftovers = [p for p in root.iterdir() if p.name.startswith(C.WORK_DIR_PREFIX)]
            self.assertEqual(leftovers, [])

    def test_invalid_options_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            pipeline.JobOptions(wrap_format="zip", sample_pages=0).validate()
        with self.assertRaises(ValueError):
            pipeline.JobOptions(wrap_format="zip", series_review=0.99, series_similarity=0.8).validate()


class ConvertAndEncodingTest(unittest.TestCase):
    def test_archive_to_zip_conversion(self) -> None:
        """書庫 → 無圧縮 ZIP 変換の経路(--rar-to-zip)を検証する。

        この環境では rar を作れないため、deflate 圧縮の zip を rar 相当のアイテムに見立てて
        「展開 → 無圧縮 ZIP 化 → 元ファイル削除」の流れだけを確認する。
        """

        from comic_dedupe import naming
        from comic_dedupe.models import VolumeItem

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = zip_folder(write_pages(root / "src", 3, 50, 70), root / "第3巻.zip")
            item = VolumeItem(
                path=source,
                kind="rar",
                title=naming.parse_title(source.name),
                rel_path=source.name,
                display_name=source.name,
            )
            job = pipeline.Pipeline(pipeline.JobOptions(wrap_format="zip", ))
            action = job._convert_archive_to_zip(item)

            self.assertEqual(action.result, C.RESULT_KEPT)
            self.assertEqual(item.kind, "zip")
            with zipfile.ZipFile(item.path) as archive:
                infos = archive.infolist()
            self.assertEqual(len(infos), 3)
            self.assertTrue(all(info.compress_type == zipfile.ZIP_DEFLATED for info in infos))
            self.assertEqual(sorted(p.name for p in root.glob("*.zip")), ["第3巻.zip"])

    def test_cp932_entry_names_are_decoded(self) -> None:
        """UTF-8 フラグの無い(cp932 で書かれた)ZIP エントリ名を復元できること。

        zipfile は UTF-8 フラグが無いエントリ名を cp437 として decode するため、
        日本語名は化けた文字列として現れる。それを元に戻すのが decode_zip_name。
        """

        from comic_dedupe.archives import decode_zip_name

        original = "第5巻/001.jpg"
        legacy = zipfile.ZipInfo(original.encode("cp932").decode("cp437"))
        legacy.flag_bits = 0  # UTF-8 フラグ無し = 昔の Windows が作る ZIP
        self.assertEqual(decode_zip_name(legacy), original)

        modern = zipfile.ZipInfo(original)
        modern.flag_bits = 0x800
        self.assertEqual(decode_zip_name(modern), original)


class ArchiveSourceTest(unittest.TestCase):
    def test_unsupported_extension_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.txt"
            path.write_text("x", encoding="utf-8")
            with self.assertRaises(ArchiveError):
                open_source(path)

    def test_broken_zip_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.zip"
            path.write_bytes(b"not a zip")
            with self.assertRaises(ArchiveError):
                open_source(path)


if __name__ == "__main__":
    unittest.main()


class WorkBaseTest(unittest.TestCase):
    def test_explicit_work_dir_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            options = pipeline.JobOptions(wrap_format="zip", work_dir=Path(tmp))
            base = pipeline.Pipeline(options)._choose_work_base(Path("/nonexistent/in.zip"), 1)
            self.assertEqual(base, Path(tmp))

    def test_default_avoids_input_parent(self) -> None:
        base = pipeline.Pipeline(pipeline.JobOptions(wrap_format="zip", ))._choose_work_base(Path("/nonexistent/in.zip"), 1)
        self.assertNotEqual(base, Path("/nonexistent"))
