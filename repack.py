"""ZIP の作成と検証。

- 巻フォルダ → `<巻名>.zip`(ページが ZIP 直下に来る一般的なコミック ZIP の形)。**圧縮(DEFLATE)**
- 無圧縮の巻 ZIP → 同じ内容で圧縮 ZIP に作り直す(`recompress_stored_zip`)
- 作業ツリー全体 → `<入力名>_整理済み.zip`(巻 ZIP と `_重複/` をそのまま格納)。**無圧縮(ZIP_STORED)**
  (中身の巻 ZIP は既に圧縮済みなので、外側は無圧縮にして二重圧縮を避ける)

作成後に必ず「エントリ数と各サイズ」を元と突き合わせ、検証に通ってから元フォルダを消す。
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path
from typing import Iterable, Optional

from . import constants as C
from . import external_tools
from .logging_setup import get_logger


class RepackError(Exception):
    """ZIP の作成・検証に失敗した。"""


def _iter_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def create_stored_zip(
    source_root: Path,
    out_zip: Path,
    files: Optional[Iterable[Path]] = None,
    compression: int = zipfile.ZIP_STORED,
) -> Path:
    """source_root 配下を ZIP にまとめる(既定は無圧縮。アーカイブ名は source_root からの相対パス)。"""

    logger = get_logger()
    targets = list(files) if files is not None else _iter_files(source_root)
    if not targets:
        raise RepackError(f"ZIP に入れるファイルが無い: {source_root}")

    out_zip.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(out_zip, "w", compression, allowZip64=True) as archive:
            for file_path in targets:
                arcname = file_path.relative_to(source_root).as_posix()
                _warn_long_path(arcname, out_zip)
                archive.write(file_path, arcname)
    except (OSError, ValueError) as exc:
        raise RepackError(f"ZIP を作成できない: {out_zip}: {exc}") from exc

    verify_stored_zip(out_zip, source_root, targets, compression)
    logger.debug("ZIP を作成: %s (%d ファイル)", out_zip, len(targets))
    return out_zip


def verify_stored_zip(
    out_zip: Path,
    source_root: Path,
    files: Optional[Iterable[Path]] = None,
    compression: int = zipfile.ZIP_STORED,
) -> None:
    """エントリ数・サイズ・格納方式(compression)を元ファイルと突き合わせる。"""

    expected = {
        path.relative_to(source_root).as_posix(): path.stat().st_size
        for path in (list(files) if files is not None else _iter_files(source_root))
    }
    try:
        with zipfile.ZipFile(out_zip) as archive:
            actual = {info.filename: info.file_size for info in archive.infolist()}
            wrong_method = {
                info.filename
                for info in archive.infolist()
                if info.compress_type != compression and info.file_size > 0
            }
    except (OSError, zipfile.BadZipFile) as exc:
        raise RepackError(f"作成した ZIP を検証できない: {out_zip}: {exc}") from exc

    if wrong_method:
        raise RepackError(
            f"想定の格納方式(compression={compression})でないエントリがある: "
            f"{out_zip}: {sorted(wrong_method)[:3]}"
        )
    missing = sorted(set(expected) - set(actual))
    if missing:
        raise RepackError(f"ZIP に欠落がある: {out_zip}: {missing[:3]}")
    mismatched = sorted(
        name for name, size in expected.items() if actual.get(name) != size
    )
    if mismatched:
        raise RepackError(f"ZIP 内のサイズが一致しない: {out_zip}: {mismatched[:3]}")


def zip_volume_folder(folder: Path, remove_source: bool = True) -> Path:
    """巻フォルダを同じ場所の圧縮 `<フォルダ名>.zip` にする。"""

    out_zip = folder.parent / f"{folder.name}.zip"
    if out_zip.exists():
        raise RepackError(f"出力先 ZIP が既に存在する: {out_zip}")
    create_stored_zip(folder, out_zip, compression=C.VOLUME_ZIP_COMPRESSION)
    if remove_source:
        _remove_tree(folder)
    return out_zip


def is_stored_zip(path: Path) -> bool:
    """中身のあるエントリがすべて無圧縮(ZIP_STORED)の ZIP か。読めない・空なら False。"""

    try:
        with zipfile.ZipFile(path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
    except (OSError, zipfile.BadZipFile):
        return False
    return bool(infos) and all(info.compress_type == zipfile.ZIP_STORED for info in infos)


def recompress_zip(source_zip: Path, staging: Path, extract) -> Path:
    """無圧縮 ZIP を、同じ内容の圧縮 ZIP に作り直して同じパスへ置き換える。

    extract(zip_path, dest_dir) は展開関数(書庫バックエンドを pipeline 側から渡す)。
    作り直した ZIP の検証に通ってから元を置き換える。失敗したら元は触らない。
    """

    temporary = source_zip.with_name(f"{C.WORK_DIR_PREFIX}{source_zip.name}")
    try:
        extract(source_zip, staging)
        create_stored_zip(staging, temporary, compression=C.VOLUME_ZIP_COMPRESSION)
        temporary.replace(source_zip)
    finally:
        if temporary.exists():
            temporary.unlink()
        if staging.exists():
            _remove_tree(staging)
    return source_zip


def create_rar(
    source_root: Path,
    out_rar: Path,
    recovery_percent: int = C.RAR_RECOVERY_PERCENT,
    compression_level: int = C.RAR_COMPRESSION_LEVEL,
) -> Path:
    """source_root 配下を圧縮(-m<level>)した RAR5 にし、リカバリーレコードを付加する。

    ソリッド圧縮は使わない(一部破損の影響範囲を広げず、個別展開も速いため)。

    RAR の作成は WinRAR の Rar.exe(または rar)だけが可能。見つからない・失敗したら RepackError。
    作成後に `t`(テスト)で書庫の整合を検証する。
    """

    writer = external_tools.find_rar_writer()
    if writer is None:
        raise RepackError("RAR を作成できるコマンド(WinRAR の Rar.exe / rar)が見つからない(--rar-exe か環境変数 COMIC_DEDUPE_RAR で指定可)")
    if not any(source_root.rglob("*")):
        raise RepackError(f"RAR に入れるファイルが無い: {source_root}")

    command = [writer, "a", "-r", f"-m{compression_level}", "-ma5", "-ep1", "-idq", "-y", "-s-"]
    if recovery_percent > 0:
        command.append(f"-rr{recovery_percent}p")
    command += [str(out_rar), "*"]
    out_rar.parent.mkdir(parents=True, exist_ok=True)
    try:
        created = external_tools.run_tool_in(command, source_root)
        if created.returncode != 0 or not out_rar.exists():
            raise RepackError(f"RAR の作成に失敗: {_tool_message(created)}")
        tested = external_tools.run_tool_in([writer, "t", "-idq", str(out_rar)], source_root)
        if tested.returncode != 0:
            raise RepackError(f"作成した RAR の検証に失敗: {_tool_message(tested)}")
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepackError(f"RAR を作成できない: {out_rar}: {exc}") from exc
    except RepackError:
        out_rar.unlink(missing_ok=True)
        raise
    get_logger().debug("RAR を作成: %s (リカバリー %d%%)", out_rar, recovery_percent)
    return out_rar


def _tool_message(completed) -> str:
    text = (completed.stderr or completed.stdout or b"").decode("utf-8", errors="replace").strip()
    return f"終了コード {completed.returncode} {text[-300:]}"


def zip_work_tree(work_root: Path, out_zip: Path) -> Path:
    """作業ツリー全体を 1 つの無圧縮 ZIP にまとめる。"""

    return create_stored_zip(work_root, out_zip)


def _remove_tree(folder: Path) -> None:
    import shutil

    try:
        shutil.rmtree(folder)
    except OSError as exc:
        raise RepackError(f"ZIP 化後の元フォルダを削除できない: {folder}: {exc}") from exc


def _warn_long_path(arcname: str, out_zip: Path) -> None:
    total = len(str(out_zip)) + len(arcname)
    if total > C.MAX_PATH_WARN:
        get_logger().warning(
            "パスが長いため Windows で展開に失敗する可能性がある(%d 文字): %s", total, arcname
        )
