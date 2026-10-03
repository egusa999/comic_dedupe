"""書庫を読むためのバックエンド検出。

RAR には pure Python の解凍器が存在しないため、次の順に「使えるもの」を探す:

1. libarchive-c (`import libarchive`) + ネイティブ libarchive(archive.dll / libarchive.so)
2. bsdtar (Windows 同梱の `C:\\Windows\\System32\\tar.exe` は libarchive ベース)
3. rarfile + 外部ツール(UnRAR.exe / 7z.exe / unar など)

検出結果はプロセス内でキャッシュする。実際に読めるかどうかは書庫ごとに差があるため、
最終的な可否は `archives.py` 側の「失敗したら次のバックエンドへ降格」で担保する。
"""

from __future__ import annotations

import ctypes.util
import functools
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import constants as C
from .logging_setup import get_logger


def _is_windows() -> bool:
    return sys.platform.startswith("win")


@functools.lru_cache(maxsize=1)
def find_libarchive() -> Optional[str]:
    """ネイティブ libarchive の場所を返す(見つからなければ None)。"""

    explicit = os.environ.get(C.LIBARCHIVE_ENV_VAR)
    if explicit and Path(explicit).exists():
        return explicit
    found = ctypes.util.find_library("archive")
    if found:
        return found
    if _is_windows():
        for name in ("archive.dll", "libarchive.dll"):
            located = shutil.which(name)
            if located:
                return located
    return None


@functools.lru_cache(maxsize=1)
def load_libarchive_module():
    """libarchive-c モジュールを読み込む(使えなければ None)。"""

    if find_libarchive() is None:
        return None
    try:
        import libarchive  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - 環境依存
        get_logger().debug("libarchive-c の読み込みに失敗: %s", exc)
        return None
    return libarchive


@functools.lru_cache(maxsize=1)
def load_rarfile_module():
    """rarfile を読み込み、外部バックエンドを設定して返す(使えなければ None)。"""

    try:
        import rarfile  # type: ignore[import-not-found]
    except Exception as exc:
        get_logger().debug("rarfile の読み込みに失敗: %s", exc)
        return None
    _configure_rarfile_tool(rarfile, find_unrar_tool())
    try:
        # rarfile は使えるツールを自分で選ぶ。1 つも無ければ例外になる。
        rarfile.tool_setup()
    except Exception as exc:
        get_logger().debug("rarfile のバックエンドが見つからない: %s", exc)
        return None
    return rarfile


def _configure_rarfile_tool(rarfile_module, tool: Optional[str]) -> None:
    """検出した外部ツールを rarfile の適切な設定項目へ割り当てる。

    rarfile は unrar / unar / bsdtar / 7z を別の設定として持つため、
    すべてを UNRAR_TOOL に入れてはいけない(PATH 上に無い Windows の固定パス対策)。
    """

    if tool is None:
        return
    name = Path(tool).name.lower()
    if name.startswith(("unrar", "rar")):
        rarfile_module.UNRAR_TOOL = tool
    elif name.startswith("7z"):
        rarfile_module.SEVENZIP_TOOL = tool
    elif name.startswith("unar"):
        rarfile_module.UNAR_TOOL = tool
    elif "bsdtar" in name or name in ("tar", "tar.exe"):
        rarfile_module.BSDTAR_TOOL = tool


@functools.lru_cache(maxsize=1)
def load_py7zr_module():
    try:
        import py7zr  # type: ignore[import-not-found]
    except Exception as exc:
        get_logger().debug("py7zr の読み込みに失敗: %s", exc)
        return None
    return py7zr


def _which_any(names: tuple[str, ...]) -> Optional[str]:
    for name in names:
        located = shutil.which(name)
        if located:
            return located
    return None


@functools.lru_cache(maxsize=1)
def find_unrar_tool() -> Optional[str]:
    """rarfile のバックエンドに使える外部コマンドを探す。"""

    if _is_windows():
        for candidate in C.WINDOWS_TOOL_PATHS:
            if Path(candidate).exists():
                return candidate
    return _which_any(C.UNRAR_TOOL_NAMES)


@functools.lru_cache(maxsize=1)
def find_rar_writer() -> Optional[str]:
    """RAR 書庫を「作成」できる外部コマンド(WinRAR の Rar.exe / rar)を探す。

    環境変数 COMIC_DEDUPE_RAR > Windows の標準インストール先 > PATH の順。
    7-Zip と unrar は RAR を作れないので対象外。WinRAR は PATH を通さないので、PATH に無くても正常。
    """

    explicit = os.environ.get(C.RAR_WRITER_ENV_VAR)
    if explicit and Path(explicit).is_file():
        return explicit
    if _is_windows():
        candidates = list(C.WINDOWS_RAR_WRITER_PATHS)
        for var in C.WINDOWS_PROGRAM_FILES_ENV_VARS:
            base = os.environ.get(var)
            if base:
                candidates.append(str(Path(base) / C.WINRAR_RAR_RELATIVE_PATH))
        for candidate in candidates:
            if Path(candidate).is_file():
                return candidate
    return _which_any(C.RAR_WRITER_NAMES)


@functools.lru_cache(maxsize=1)
def find_bsdtar() -> Optional[str]:
    """libarchive ベースの bsdtar を探す。

    Windows 10 1803 以降は `C:\\Windows\\System32\\tar.exe` が bsdtar であり、
    Windows 11 23H2 以降は RAR/7z リーダーを含む libarchive が同梱されている。
    GNU tar は RAR を読めないので、`--version` 出力に bsdtar と書かれているものだけ採用する。
    """

    candidates: list[str] = []
    if _is_windows() and Path(C.WINDOWS_BSDTAR_PATH).exists():
        candidates.append(C.WINDOWS_BSDTAR_PATH)
    for name in C.BSDTAR_TOOL_NAMES:
        located = shutil.which(name)
        if located:
            candidates.append(located)

    for candidate in candidates:
        try:
            completed = subprocess.run(
                [candidate, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            get_logger().debug("%s の --version 実行に失敗: %s", candidate, exc)
            continue
        banner = f"{completed.stdout} {completed.stderr}".lower()
        if "bsdtar" in banner or "libarchive" in banner:
            return candidate
    return None


def _program_files_dirs() -> list[Path]:
    """ProgramFiles 系の環境変数が指すフォルダ(重複なし)。"""

    seen: list[Path] = []
    for var in C.WINDOWS_PROGRAM_FILES_ENV_VARS:
        base = os.environ.get(var)
        if base and Path(base) not in seen:
            seen.append(Path(base))
    return seen


@functools.lru_cache(maxsize=1)
def find_sevenzip_cli() -> Optional[str]:
    if _is_windows():
        for candidate in C.WINDOWS_TOOL_PATHS:
            if Path(candidate).name.lower().startswith("7z") and Path(candidate).exists():
                return candidate
        for base in _program_files_dirs():
            candidate = base / C.SEVENZIP_RELATIVE_PATH
            if candidate.is_file():
                return str(candidate)
    return _which_any(("7z", "7zz", "7za"))


@functools.lru_cache(maxsize=1)
def find_unrar_cli() -> Optional[str]:
    """RAR を展開できる WinRAR 系コマンド(UnRAR.exe / Rar.exe / unrar / rar)を探す。

    Rar.exe も `x` で展開できる(RAR を作るために入れた WinRAR をそのまま使える)。
    """

    explicit = os.environ.get(C.RAR_WRITER_ENV_VAR)
    if explicit and Path(explicit).is_file():
        return explicit
    if _is_windows():
        folders = [Path(p).parent for p in C.WINDOWS_TOOL_PATHS if "WinRAR" in p]
        folders += [base / "WinRAR" for base in _program_files_dirs()]
        for folder in folders:
            for filename in C.WINRAR_CLI_FILENAMES:
                if (folder / filename).is_file():
                    return str(folder / filename)
    return _which_any(C.UNRAR_CLI_NAMES)


def available_rar_backends(preferred: str = "auto") -> tuple[str, ...]:
    """RAR 用に実際に使えるバックエンド名を優先順で返す。"""

    order = C.RAR_BACKEND_PRIORITY if preferred == "auto" else (preferred,)
    usable: list[str] = []
    for name in order:
        if name == C.BACKEND_LIBARCHIVE and load_libarchive_module() is not None:
            usable.append(name)
        elif name == C.BACKEND_BSDTAR and find_bsdtar() is not None:
            usable.append(name)
        elif name == C.BACKEND_RARFILE and load_rarfile_module() is not None:
            usable.append(name)
        elif name == C.BACKEND_SEVENZIP_CLI and find_sevenzip_cli() is not None:
            usable.append(name)
        elif name == C.BACKEND_UNRAR_CLI and find_unrar_cli() is not None:
            usable.append(name)
    return tuple(usable)


def available_sevenzip_backends(preferred: str = "auto") -> tuple[str, ...]:
    order = C.SEVENZIP_BACKEND_PRIORITY if preferred == "auto" else (preferred,)
    usable: list[str] = []
    for name in order:
        if name == C.BACKEND_PY7ZR and load_py7zr_module() is not None:
            usable.append(name)
        elif name == C.BACKEND_LIBARCHIVE and load_libarchive_module() is not None:
            usable.append(name)
        elif name == C.BACKEND_BSDTAR and find_bsdtar() is not None:
            usable.append(name)
        elif name == C.BACKEND_SEVENZIP_CLI and find_sevenzip_cli() is not None:
            usable.append(name)
    return tuple(usable)


def describe_backends() -> str:
    """起動時に表示する検出結果の 1 行サマリ。"""

    rar = available_rar_backends() or ("なし",)
    seven = available_sevenzip_backends() or ("なし",)
    return (
        f"RAR バックエンド: {', '.join(rar)} / 7z バックエンド: {', '.join(seven)}"
        f" / libarchive: {find_libarchive() or '未検出'}"
        f" / bsdtar: {find_bsdtar() or '未検出'}"
        f" / unrar系: {find_unrar_tool() or '未検出'}"
        f" / 7z: {find_sevenzip_cli() or '未検出'}"
        f" / WinRAR系: {find_unrar_cli() or '未検出'}"
        f" / RAR作成: {find_rar_writer() or '未検出'}"
    )


def run_tool(command: list[str]) -> subprocess.CompletedProcess:
    """外部ツールを実行する。失敗はそのまま返し、呼び出し側で降格判断させる。"""

    get_logger().debug("外部コマンド実行: %s", " ".join(command))
    return subprocess.run(
        command,
        capture_output=True,
        timeout=C.SUBPROCESS_TIMEOUT,
        check=False,
    )


def run_tool_in(command: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """作業ディレクトリを指定して外部ツールを実行する(RAR 作成で相対パス名にするため)。"""

    get_logger().debug("外部コマンド実行(cwd=%s): %s", cwd, " ".join(command))
    return subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        timeout=C.SUBPROCESS_TIMEOUT,
        check=False,
    )
