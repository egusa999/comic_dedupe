"""RAR ラップ(リカバリーレコード付き)のテスト。実 Rar.exe の代わりに偽コマンドで引数と流れを検証する。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from comic_dedupe import constants as C
from comic_dedupe import external_tools, pipeline
from comic_dedupe.logging_setup import setup_logger
from comic_dedupe.tests.test_pipeline import build_input_tree

FAKE_RAR = """#!{python}
import json, os, sys, zipfile
args = sys.argv[1:]
log = os.environ["FAKE_RAR_LOG"]
with open(log, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({{"args": args, "cwd": os.getcwd()}}) + "\\n")
if args[0] == "a":
    out = [a for a in args if a.endswith(".rar")][0]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for root, _, files in os.walk("."):
            for name in files:
                path = os.path.join(root, name)
                z.write(path, os.path.relpath(path, "."))
sys.exit(int(os.environ.get("FAKE_RAR_EXIT", "0")))
"""


class RarWrapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        setup_logger(quiet_console=True)

    def _install_fake(self, root: Path) -> Path:
        script = root / "bin" / "rar"
        script.parent.mkdir()
        script.write_text(FAKE_RAR.format(python=os.sys.executable), encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script

    def _run(self, root: Path, env: dict, **options):
        content = build_input_tree(root)
        external_tools.find_rar_writer.cache_clear()
        patched = {"PATH": f"{root / 'bin'}{os.pathsep}{os.environ['PATH']}", **env}
        with mock.patch.dict(os.environ, patched):
            result = pipeline.Pipeline(pipeline.JobOptions(**options)).run(content)
        external_tools.find_rar_writer.cache_clear()
        return result

    def test_rar_wrapper_with_recovery_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._install_fake(root)
            log = root / "calls.jsonl"
            result = self._run(root, {"FAKE_RAR_LOG": str(log)})

            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.output_path.name, f"入力{C.OUTPUT_SUFFIX}.rar")
            self.assertEqual(result.warnings, [])
            calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            add = calls[0]["args"]
            self.assertEqual(add[0], "a")
            self.assertIn(f"-m{C.RAR_COMPRESSION_LEVEL}", add)  # 圧縮(既定は標準)
            self.assertIn("-s-", add)  # ソリッドにしない
            self.assertIn(f"-rr{C.RAR_RECOVERY_PERCENT}p", add)  # リカバリーレコード
            self.assertEqual(calls[1]["args"][0], "t")  # 作成後に検証
            self.assertEqual(list(root.glob("**/_dedupe_work_*")), [])

    def test_rar_is_built_locally_then_moved_to_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._install_fake(root)
            log = root / "calls.jsonl"
            result = self._run(root, {"FAKE_RAR_LOG": str(log)}, work_dir=root / "work")
            add = json.loads(log.read_text(encoding="utf-8").splitlines()[0])["args"]
            built = Path([a for a in add if a.endswith(".rar")][0])
            # 作成・検証は出力先(入力の親)ではなく作業側で行い、完成品だけを移す
            self.assertEqual(built.parent, root / "work")
            self.assertNotEqual(built.parent, result.output_path.parent)
            self.assertFalse(built.exists(), "移動後に作業側の書庫は残らない")
            self.assertTrue(result.output_path.is_file())

    def test_recovery_percent_zero_omits_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._install_fake(root)
            log = root / "calls.jsonl"
            self._run(root, {"FAKE_RAR_LOG": str(log)}, recovery_percent=0)
            add = json.loads(log.read_text(encoding="utf-8").splitlines()[0])["args"]
            self.assertFalse(any(a.startswith("-rr") for a in add))

    def test_rar_level_zero_stores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._install_fake(root)
            log = root / "calls.jsonl"
            self._run(root, {"FAKE_RAR_LOG": str(log)}, rar_level=0)
            add = json.loads(log.read_text(encoding="utf-8").splitlines()[0])["args"]
            self.assertIn("-m0", add)

    def test_falls_back_to_zip_when_rar_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._install_fake(root)
            result = self._run(
                root, {"FAKE_RAR_LOG": str(root / "calls.jsonl"), "FAKE_RAR_EXIT": "2"}
            )
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.output_path.suffix, ".zip")
            self.assertTrue(any("RAR で出力できない" in w for w in result.warnings))
            self.assertEqual(list(result.output_path.parent.glob("*.rar")), [])

    def test_falls_back_to_zip_when_no_rar_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = build_input_tree(root)
            external_tools.find_rar_writer.cache_clear()
            with mock.patch.object(external_tools, "find_rar_writer", return_value=None):
                result = pipeline.Pipeline(pipeline.JobOptions()).run(content)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.output_path.suffix, ".zip")
            self.assertTrue(any("RAR で出力できない" in w for w in result.warnings))

    def test_option_validation(self) -> None:
        with self.assertRaises(ValueError):
            pipeline.JobOptions(recovery_percent=C.RAR_RECOVERY_PERCENT_MAX + 1).validate()
        with self.assertRaises(ValueError):
            pipeline.JobOptions(wrap_format="7z").validate()
        with self.assertRaises(ValueError):
            pipeline.JobOptions(rar_level=C.RAR_COMPRESSION_LEVEL_MAX + 1).validate()


if __name__ == "__main__":
    unittest.main()
