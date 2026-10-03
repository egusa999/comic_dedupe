"""出力のフォルダ名・巻名の英語表記(`folder_lang="en"`)のテスト。"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from comic_dedupe import constants as C
from comic_dedupe import pipeline
from comic_dedupe.logging_setup import setup_logger
from comic_dedupe.tests.test_pipeline import build_input_tree


class FolderLangTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        setup_logger(quiet_console=True)

    def _names(self, **options) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            content = build_input_tree(Path(tmp))
            result = pipeline.Pipeline(pipeline.JobOptions(wrap_format="zip", **options)).run(content)
            with zipfile.ZipFile(result.output_path) as archive:
                return [result.output_path.name] + archive.namelist()

    def test_english_names(self) -> None:
        names = self._names(folder_lang=C.FOLDER_LANG_EN)
        joined = "\n".join(names)
        self.assertIn("_organized", names[0])
        self.assertIn("Vol 07/", joined)
        self.assertIn("_duplicates/", joined)
        for japanese in ("_重複", "第07巻", "_整理済み"):
            self.assertNotIn(japanese, joined)

    def test_default_stays_japanese(self) -> None:
        names = self._names()
        joined = "\n".join(names)
        self.assertIn("_整理済み", names[0])
        self.assertIn("第07巻/", joined)
        self.assertIn("_重複/", joined)

    def test_invalid_language_rejected(self) -> None:
        with self.assertRaises(ValueError):
            pipeline.JobOptions(folder_lang="fr").validate()
