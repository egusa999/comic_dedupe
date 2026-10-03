"""退避(layout)と無圧縮 ZIP 化(repack)のテスト。"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from comic_dedupe import constants as C
from comic_dedupe import layout, naming, repack
from comic_dedupe.models import Decision, DupGroup, VolumeItem
from comic_dedupe.tests.helpers import write_pages, zip_folder


def make_item(path: Path, kind: str) -> VolumeItem:
    return VolumeItem(
        path=path,
        kind=kind,
        title=naming.parse_title(path.name),
        rel_path=path.name,
        display_name=path.name,
    )


class UniqueDestinationTest(unittest.TestCase):
    def test_renames_on_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "a.zip"
            base.write_bytes(b"x")
            self.assertEqual(layout.unique_destination(base).name, "a (2).zip")
            (Path(tmp) / "a (2).zip").write_bytes(b"x")
            self.assertEqual(layout.unique_destination(base).name, "a (3).zip")

    def test_returns_same_path_when_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "free.zip"
            self.assertEqual(layout.unique_destination(target), target)


class DisposeTest(unittest.TestCase):
    def test_dry_run_moves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            loser = work / "07.zip"
            loser.write_bytes(b"x")
            decision = Decision(group=DupGroup(key="g"), losers=[make_item(loser, "zip")])
            actions = layout.dispose_losers([decision], work, dry_run=True)
            self.assertTrue(loser.exists())
            self.assertEqual(actions[0].result, C.RESULT_PLANNED)

    def test_move_to_duplicate_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            loser = work / "07.zip"
            loser.write_bytes(b"x")
            item = make_item(loser, "zip")
            decision = Decision(group=DupGroup(key="g"), losers=[item])
            actions = layout.dispose_losers([decision], work)
            moved = work / C.DUP_DIR_NAME / "07.zip"
            self.assertFalse(loser.exists())
            self.assertTrue(moved.exists())
            self.assertEqual(actions[0].result, C.RESULT_MOVED)
            self.assertEqual(item.path, moved)

    def test_move_keeps_relative_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            nested = work / "その他" / "さらに下"
            nested.mkdir(parents=True)
            loser = nested / "07.zip"
            loser.write_bytes(b"x")
            decision = Decision(group=DupGroup(key="g"), losers=[make_item(loser, "zip")])
            layout.dispose_losers([decision], work)
            self.assertTrue((work / C.DUP_DIR_NAME / "その他" / "さらに下" / "07.zip").exists())

    def test_delete_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            loser = work / "07.zip"
            loser.write_bytes(b"x")
            decision = Decision(group=DupGroup(key="g"), losers=[make_item(loser, "zip")])
            actions = layout.dispose_losers([decision], work, delete=True)
            self.assertFalse(loser.exists())
            self.assertEqual(actions[0].result, C.RESULT_DELETED)


class RepackTest(unittest.TestCase):
    def test_volume_folder_becomes_compressed_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = write_pages(Path(tmp) / "第7巻", 3, 60, 80)
            out_zip = repack.zip_volume_folder(folder)
            self.assertFalse(folder.exists())
            with zipfile.ZipFile(out_zip) as archive:
                infos = archive.infolist()
            self.assertEqual(len(infos), 3)
            self.assertTrue(all(info.compress_type == zipfile.ZIP_DEFLATED for info in infos))
            self.assertEqual(infos[0].filename, "001.jpg")

    def test_work_tree_zip_keeps_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            write_pages(work / "第7巻", 2, 40, 50)
            zip_folder(write_pages(work / "src08", 2, 40, 50), work / "第8巻.zip")
            out_zip = repack.zip_work_tree(work, Path(tmp) / "out.zip")
            with zipfile.ZipFile(out_zip) as archive:
                names = sorted(info.filename for info in archive.infolist())
            self.assertIn("第7巻/001.jpg", names)
            self.assertIn("第8巻.zip", names)

    def test_verification_detects_missing_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = write_pages(Path(tmp) / "vol", 2, 40, 50)
            out_zip = Path(tmp) / "broken.zip"
            with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_STORED) as archive:
                archive.write(folder / "001.jpg", "001.jpg")
            with self.assertRaises(repack.RepackError):
                repack.verify_stored_zip(out_zip, folder)

    def test_verification_rejects_compressed_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = write_pages(Path(tmp) / "vol", 1, 40, 50)
            out_zip = zip_folder(folder, Path(tmp) / "deflated.zip", compress=True)
            with self.assertRaises(repack.RepackError):
                repack.verify_stored_zip(out_zip, folder)

    def test_empty_folder_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            with self.assertRaises(repack.RepackError):
                repack.create_stored_zip(empty, Path(tmp) / "out.zip")


if __name__ == "__main__":
    unittest.main()
