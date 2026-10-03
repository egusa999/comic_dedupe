"""RAR 読み取りのフォールバック(7-Zip / WinRAR 系コマンド)のテスト。偽コマンドで経路を検証する。"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from comic_dedupe import archives, external_tools
from comic_dedupe.archives import ArchiveError, UnrarCliSource, open_source
from comic_dedupe.logging_setup import setup_logger

# 偽の unrar: `x ... <archive> <dest>/` で、archive(中身は ZIP)を dest に展開する
FAKE_UNRAR = """#!{python}
import sys, zipfile
args = [a for a in sys.argv[1:] if not a.startswith("-")]
archive, dest = args[1], args[2]
if "BROKEN" in archive:
    sys.exit(3)
zipfile.ZipFile(archive).extractall(dest)
"""


class UnrarCliSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        setup_logger(quiet_console=True)

    def _fake_tool(self, root: Path) -> Path:
        tool = root / "unrar"
        tool.write_text(FAKE_UNRAR.format(python=os.sys.executable), encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        return tool

    def _make_archive(self, root: Path, name: str) -> Path:
        path = root / name
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("巻/001.jpg", b"x")
            z.writestr("巻/002.jpg", b"y")
        return path

    def test_extract_and_read_via_unrar_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = self._fake_tool(root)
            archive = self._make_archive(root, "a.rar")
            with mock.patch.object(external_tools, "find_unrar_cli", return_value=str(tool)):
                source = UnrarCliSource(archive)
                names = sorted(e.name for e in source.list_entries() if not e.is_dir)
                self.assertEqual(names, ["巻/001.jpg", "巻/002.jpg"])
                self.assertEqual(source.read_entry("巻/001.jpg"), b"x")
                source.extract_all(root / "out")
                staging = source._staging
                source.close()
            self.assertTrue((root / "out" / "巻" / "002.jpg").is_file())
            self.assertFalse(staging.exists(), "一時展開フォルダは close で消える")

    def test_falls_back_to_unrar_cli_after_bsdtar_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = self._fake_tool(root)
            archive = self._make_archive(root, "b.rar")

            class FailingBsdtar(archives.ArchiveSource):
                def list_entries(self):
                    raise ArchiveError("bsdtar で展開できない: unreadable filename")

                def read_entries(self, names):
                    raise ArchiveError("x")

                def extract_all(self, dest):
                    raise ArchiveError("bsdtar で展開できない: unreadable filename")

            factories = [
                ("bsdtar", lambda: FailingBsdtar(archive, "rar")),
                ("unrar", lambda: UnrarCliSource(archive)),
            ]
            with mock.patch.object(external_tools, "find_unrar_cli", return_value=str(tool)):
                with archives.FallbackSource(archive, "rar", factories) as source:
                    source.extract_all(root / "out")
            self.assertTrue((root / "out" / "巻" / "001.jpg").is_file())

    def test_all_failed_message_lists_each_backend_and_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = self._make_archive(root, "c.rar")

            class Failing(archives.ArchiveSource):
                def list_entries(self):
                    raise ArchiveError("boom")

                def read_entries(self, names):
                    raise ArchiveError("boom")

                def extract_all(self, dest):
                    raise ArchiveError("bsdtar で展開できない")

            source = archives.FallbackSource(archive, "rar", [("bsdtar", lambda: Failing(archive, "rar"))])
            with self.assertRaises(ArchiveError) as ctx:
                source.extract_all(root / "out")
            message = str(ctx.exception)
            self.assertIn("[bsdtar]", message)
            self.assertIn("7-Zip", message)  # 導入の案内

    def test_bsdtar_skipping_with_exit_zero_is_treated_as_failure(self) -> None:
        # bsdtar が「unreadable filename ... skipping」を出して終了コード 0 で戻っても、
        # 不完全な展開として次のバックエンド(WinRAR 系)へ切り替え、残骸も消える
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unrar = self._fake_tool(root)
            bsdtar = root / "bsdtar"
            bsdtar.write_text(
                f"#!{os.sys.executable}\n"
                "import sys, pathlib\n"
                "dest = sys.argv[sys.argv.index('-C') + 1]\n"
                "pathlib.Path(dest, 'partial.jpg').write_bytes(b'z')\n"
                "sys.stderr.write('tar.exe: Archive entry has empty or unreadable filename ... skipping\\n')\n",
                encoding="utf-8",
            )
            bsdtar.chmod(bsdtar.stat().st_mode | stat.S_IEXEC)
            archive = self._make_archive(root, "d.rar")
            factories = [
                ("bsdtar", lambda: archives.BsdtarSource(archive, "rar")),
                ("unrar", lambda: UnrarCliSource(archive)),
            ]
            with mock.patch.object(external_tools, "find_bsdtar", return_value=str(bsdtar)), \
                    mock.patch.object(external_tools, "find_unrar_cli", return_value=str(unrar)):
                with archives.FallbackSource(archive, "rar", factories) as source:
                    source.extract_all(root / "out")
            self.assertTrue((root / "out" / "巻" / "001.jpg").is_file())
            self.assertFalse((root / "out" / "partial.jpg").exists(), "失敗した側の残骸は消える")


if __name__ == "__main__":
    unittest.main()
