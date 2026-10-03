"""書庫/フォルダを同じインターフェースで読むための層。

上位(discovery / quality / repack)にバックエンドの差異を漏らさないため、
すべて `ArchiveSource`(list_entries / read_entries / open_entry / extract_all)に揃える。
RAR と 7z は複数バックエンドを優先順に試し、失敗したら次へ自動降格する。
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import zipfile
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Optional, Sequence

from . import constants as C
from . import external_tools as tools
from .logging_setup import get_logger
from .models import EntryInfo


class ArchiveError(Exception):
    """書庫を読めなかったときに投げる(上位は「触らずスキップ」に落とす)。"""


def kind_of(path: Path) -> str:
    """パスから種別("zip"/"rar"/"7z"/"folder")を判定する。"""

    if path.is_dir():
        return "folder"
    suffix = path.suffix.lower()
    if suffix in C.ZIP_EXTENSIONS:
        return "zip"
    if suffix in C.RAR_EXTENSIONS:
        return "rar"
    if suffix in C.SEVENZIP_EXTENSIONS:
        return "7z"
    return ""


def is_archive(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in C.ARCHIVE_EXTENSIONS


def sanitize_member_name(name: str) -> str:
    """書庫内のエントリ名を安全な相対パスに直す(ディレクトリトラバーサル対策)。"""

    normalized = name.replace("\\", "/")
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in ("", ".", ".."):
            continue
        parts.append(part)
    return "/".join(parts)


def decode_zip_name(info: zipfile.ZipInfo) -> str:
    """UTF-8 フラグの無い ZIP エントリ名を cp932 として解釈し直す。

    zipfile は非 UTF-8 のエントリ名を cp437 として decode するため、日本語名が化ける。
    cp437 へ戻してから cp932 で読み直し、失敗したら元の文字列をそのまま使う。
    """

    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp932")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


class ArchiveSource(ABC):
    """書庫 1 つ(またはフォルダ 1 つ)への読み取りアクセス。"""

    def __init__(self, path: Path, kind: str) -> None:
        self.path = path
        self.kind = kind

    # --- サブクラスが実装する ---------------------------------------------

    @abstractmethod
    def list_entries(self) -> list[EntryInfo]:
        """エントリ一覧を返す。size が不明なバックエンドでは None を入れる。"""

    @abstractmethod
    def read_entries(self, names: Sequence[str]) -> dict[str, bytes]:
        """指定エントリの内容をまとめて読む(1 パスで済むバックエンドのため)。"""

    @abstractmethod
    def extract_all(self, dest: Path) -> None:
        """全エントリを dest 配下へ展開する。"""

    # --- 共通実装 ----------------------------------------------------------

    def read_entry(self, name: str) -> bytes:
        data = self.read_entries([name])
        if name not in data:
            raise ArchiveError(f"エントリを読めない: {name} ({self.path})")
        return data[name]

    def open_entry(self, name: str) -> BinaryIO:
        return io.BytesIO(self.read_entry(name))

    def close(self) -> None:
        return None

    def __enter__(self) -> "ArchiveSource":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


# --- フォルダ ---------------------------------------------------------------


class FolderSource(ArchiveSource):
    """フォルダを書庫と同じように扱う。"""

    def __init__(self, path: Path, files: Optional[Sequence[Path]] = None) -> None:
        super().__init__(path, "folder")
        self._files = list(files) if files is not None else None

    def _iter_files(self) -> Iterable[Path]:
        if self._files is not None:
            return list(self._files)
        return [p for p in sorted(self.path.rglob("*")) if p.is_file()]

    def list_entries(self) -> list[EntryInfo]:
        entries: list[EntryInfo] = []
        for file_path in self._iter_files():
            try:
                size = file_path.stat().st_size
            except OSError as exc:
                raise ArchiveError(f"ファイル情報を取得できない: {file_path}: {exc}") from exc
            entries.append(
                EntryInfo(name=file_path.relative_to(self.path).as_posix(), size=size)
            )
        return entries

    def read_entries(self, names: Sequence[str]) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for name in names:
            target = self.path / name
            try:
                result[name] = target.read_bytes()
            except OSError as exc:
                raise ArchiveError(f"ファイルを読めない: {target}: {exc}") from exc
        return result

    def open_entry(self, name: str) -> BinaryIO:
        target = self.path / name
        try:
            return target.open("rb")
        except OSError as exc:
            raise ArchiveError(f"ファイルを開けない: {target}: {exc}") from exc

    def extract_all(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        for file_path in self._iter_files():
            relative = file_path.relative_to(self.path)
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, target)


# --- ZIP --------------------------------------------------------------------


class ZipSource(ArchiveSource):
    def __init__(self, path: Path) -> None:
        super().__init__(path, "zip")
        try:
            self._zip = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ArchiveError(f"ZIP を開けない: {path}: {exc}") from exc

    def list_entries(self) -> list[EntryInfo]:
        return [
            EntryInfo(name=info.filename, size=info.file_size, is_dir=info.is_dir())
            for info in self._zip.infolist()
        ]

    def read_entries(self, names: Sequence[str]) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for name in names:
            try:
                result[name] = self._zip.read(name)
            except (KeyError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
                raise ArchiveError(f"ZIP エントリを読めない: {name} ({self.path}): {exc}") from exc
        return result

    def open_entry(self, name: str) -> BinaryIO:
        try:
            return self._zip.open(name)
        except (KeyError, OSError, RuntimeError) as exc:
            raise ArchiveError(f"ZIP エントリを開けない: {name} ({self.path}): {exc}") from exc

    def extract_all(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        for info in self._zip.infolist():
            member = sanitize_member_name(decode_zip_name(info))
            if not member:
                continue
            target = dest / member
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self._zip.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise ArchiveError(
                    f"ZIP の展開に失敗: {info.filename} ({self.path}): {exc}"
                ) from exc

    def close(self) -> None:
        self._zip.close()


# --- libarchive -------------------------------------------------------------


@contextmanager
def _in_directory(directory: Path):
    previous = Path.cwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(previous)


class LibarchiveSource(ArchiveSource):
    """libarchive-c 経由。RAR v4 / RAR5 / 7z などをライブラリ内で読める。

    libarchive はストリーミング前提でランダムアクセスできないため、
    読み取りは「1 パスで全エントリを走査しながら欲しいものだけ拾う」方式にする。
    """

    def __init__(self, path: Path, kind: str) -> None:
        super().__init__(path, kind)
        self._module = tools.load_libarchive_module()
        if self._module is None:
            raise ArchiveError("libarchive が利用できない")

    def list_entries(self) -> list[EntryInfo]:
        entries: list[EntryInfo] = []
        try:
            with self._module.file_reader(str(self.path)) as reader:
                for entry in reader:
                    entries.append(
                        EntryInfo(
                            name=str(entry.pathname),
                            size=int(entry.size) if entry.size is not None else None,
                            is_dir=bool(entry.isdir),
                        )
                    )
        except Exception as exc:  # libarchive は多様な例外を投げる
            raise ArchiveError(f"libarchive で読めない: {self.path}: {exc}") from exc
        return entries

    def read_entries(self, names: Sequence[str]) -> dict[str, bytes]:
        wanted = set(names)
        result: dict[str, bytes] = {}
        try:
            with self._module.file_reader(str(self.path)) as reader:
                for entry in reader:
                    name = str(entry.pathname)
                    if name not in wanted:
                        continue
                    result[name] = b"".join(entry.get_blocks())
                    if len(result) == len(wanted):
                        break
        except Exception as exc:
            raise ArchiveError(f"libarchive で読めない: {self.path}: {exc}") from exc
        return result

    def extract_all(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        flags = getattr(self._module.extract, "SECURE_NODOTDOT", 0)
        try:
            with _in_directory(dest):
                self._module.extract_file(str(self.path), flags)
        except Exception as exc:
            raise ArchiveError(f"libarchive で展開できない: {self.path}: {exc}") from exc


# --- bsdtar (libarchive の CLI。Windows 同梱の tar.exe を含む) --------------


class BsdtarSource(ArchiveSource):
    def __init__(self, path: Path, kind: str) -> None:
        super().__init__(path, kind)
        self._tool = tools.find_bsdtar()
        if self._tool is None:
            raise ArchiveError("bsdtar が利用できない")

    def list_entries(self) -> list[EntryInfo]:
        completed = tools.run_tool([self._tool, "-tf", str(self.path)])
        if completed.returncode != 0:
            raise ArchiveError(
                f"bsdtar で一覧できない: {self.path}: "
                f"{completed.stderr.decode('utf-8', 'replace').strip()}"
            )
        entries: list[EntryInfo] = []
        for line in completed.stdout.decode("utf-8", "replace").splitlines():
            name = line.rstrip("\n")
            if not name:
                continue
            entries.append(EntryInfo(name=name.rstrip("/"), size=None, is_dir=name.endswith("/")))
        return entries

    def read_entries(self, names: Sequence[str]) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for name in names:
            completed = tools.run_tool([self._tool, "-xOf", str(self.path), name])
            if completed.returncode != 0:
                raise ArchiveError(
                    f"bsdtar でエントリを読めない: {name} ({self.path}): "
                    f"{completed.stderr.decode('utf-8', 'replace').strip()}"
                )
            result[name] = completed.stdout
        return result

    def extract_all(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        completed = tools.run_tool([self._tool, "-xf", str(self.path), "-C", str(dest)])
        message = completed.stderr.decode("utf-8", "replace").strip()
        # 名前を読めないエントリを読み飛ばしても終了コード 0 のことがある。不完全な展開は失敗扱いにする
        incomplete = any(marker in message.lower() for marker in C.BSDTAR_INCOMPLETE_MARKERS)
        if completed.returncode != 0 or incomplete:
            raise ArchiveError(f"bsdtar で展開できない(一部を読み飛ばした): {self.path}: {message[-400:]}")


# --- rarfile ----------------------------------------------------------------


class RarfileSource(ArchiveSource):
    def __init__(self, path: Path) -> None:
        super().__init__(path, "rar")
        module = tools.load_rarfile_module()
        if module is None:
            raise ArchiveError("rarfile(+外部ツール)が利用できない")
        self._module = module
        try:
            self._rar = module.RarFile(str(path))
        except Exception as exc:
            raise ArchiveError(f"RAR を開けない: {path}: {exc}") from exc

    def list_entries(self) -> list[EntryInfo]:
        try:
            return [
                EntryInfo(name=info.filename, size=info.file_size, is_dir=info.is_dir())
                for info in self._rar.infolist()
            ]
        except Exception as exc:
            raise ArchiveError(f"RAR を一覧できない: {self.path}: {exc}") from exc

    def read_entries(self, names: Sequence[str]) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for name in names:
            try:
                result[name] = self._rar.read(name)
            except Exception as exc:
                raise ArchiveError(f"RAR エントリを読めない: {name} ({self.path}): {exc}") from exc
        return result

    def extract_all(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        try:
            self._rar.extractall(path=str(dest))
        except Exception as exc:
            raise ArchiveError(f"RAR を展開できない: {self.path}: {exc}") from exc

    def close(self) -> None:
        try:
            self._rar.close()
        except Exception:  # pragma: no cover - クローズ失敗は無害
            get_logger().debug("RAR のクローズに失敗: %s", self.path, exc_info=True)


# --- py7zr / 7z CLI ---------------------------------------------------------


class Py7zrSource(ArchiveSource):
    def __init__(self, path: Path) -> None:
        super().__init__(path, "7z")
        self._module = tools.load_py7zr_module()
        if self._module is None:
            raise ArchiveError("py7zr が利用できない")

    def list_entries(self) -> list[EntryInfo]:
        try:
            with self._module.SevenZipFile(str(self.path)) as archive:
                return [
                    EntryInfo(
                        name=info.filename,
                        size=int(info.uncompressed) if getattr(info, "uncompressed", None) else None,
                        is_dir=bool(info.is_directory),
                    )
                    for info in archive.list()
                ]
        except Exception as exc:
            raise ArchiveError(f"7z を一覧できない: {self.path}: {exc}") from exc

    def read_entries(self, names: Sequence[str]) -> dict[str, bytes]:
        """py7zr 0.x の read() と 1.x の extract(targets=...) の両方に対応する。"""

        try:
            with self._module.SevenZipFile(str(self.path)) as archive:
                if hasattr(archive, "read"):
                    extracted = archive.read(list(names)) or {}
                    return {name: buffer.read() for name, buffer in extracted.items()}
                return self._read_via_extract(archive, names)
        except ArchiveError:
            raise
        except Exception as exc:
            raise ArchiveError(f"7z を読めない: {self.path}: {exc}") from exc

    def _read_via_extract(self, archive, names: Sequence[str]) -> dict[str, bytes]:
        import tempfile

        result: dict[str, bytes] = {}
        with tempfile.TemporaryDirectory(prefix="comic_dedupe_7z_") as staging:
            staging_path = Path(staging)
            archive.extract(path=staging_path, targets=list(names))
            for name in names:
                target = staging_path / sanitize_member_name(name)
                if not target.is_file():
                    raise ArchiveError(f"7z エントリを取り出せない: {name} ({self.path})")
                result[name] = target.read_bytes()
        return result

    def extract_all(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        try:
            with self._module.SevenZipFile(str(self.path)) as archive:
                archive.extractall(path=str(dest))
        except Exception as exc:
            raise ArchiveError(f"7z を展開できない: {self.path}: {exc}") from exc


class SevenZipCliSource(ArchiveSource):
    def __init__(self, path: Path, kind: str) -> None:
        super().__init__(path, kind)
        self._tool = tools.find_sevenzip_cli()
        if self._tool is None:
            raise ArchiveError("7z コマンドが利用できない")

    def list_entries(self) -> list[EntryInfo]:
        completed = tools.run_tool(
            [self._tool, "l", "-slt", "-ba", C.SEVENZIP_UTF8_SWITCH, str(self.path)]
        )
        if completed.returncode != 0:
            raise ArchiveError(f"7z で一覧できない: {self.path}")
        entries: list[EntryInfo] = []
        name: Optional[str] = None
        size: Optional[int] = None
        is_dir = False
        for line in completed.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("Path = "):
                if name:
                    entries.append(EntryInfo(name=name, size=size, is_dir=is_dir))
                name, size, is_dir = line[len("Path = "):], None, False
            elif line.startswith("Size = "):
                raw = line[len("Size = "):].strip()
                size = int(raw) if raw.isdigit() else None
            elif line.startswith("Attributes = "):
                is_dir = "D" in line[len("Attributes = "):]
        if name:
            entries.append(EntryInfo(name=name, size=size, is_dir=is_dir))
        return entries

    def read_entries(self, names: Sequence[str]) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for name in names:
            completed = tools.run_tool([self._tool, "x", "-so", str(self.path), name])
            if completed.returncode != 0:
                raise ArchiveError(f"7z でエントリを読めない: {name} ({self.path})")
            result[name] = completed.stdout
        return result

    def extract_all(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        completed = tools.run_tool([self._tool, "x", "-y", f"-o{dest}", str(self.path)])
        if completed.returncode != 0:
            raise ArchiveError(f"7z で展開できない: {self.path}")


class UnrarCliSource(ArchiveSource):
    """WinRAR 系コマンド(UnRAR.exe / Rar.exe / unrar)で RAR を展開する。

    名前の文字コードを一覧出力から読み取らずに済むよう、一覧・個別読み出しは
    いったん一時フォルダへ全展開して、その中身を読む。
    """

    def __init__(self, path: Path) -> None:
        super().__init__(path, "rar")
        self._tool = tools.find_unrar_cli()
        if self._tool is None:
            raise ArchiveError("WinRAR 系コマンド(UnRAR.exe / Rar.exe)が利用できない")
        self._staging: Optional[Path] = None

    def _extract_to(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        completed = tools.run_tool(
            [self._tool, "x", "-y", "-idq", "-o+", str(self.path), str(dest) + os.sep]
        )
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout).decode("utf-8", "replace").strip()
            raise ArchiveError(f"{Path(self._tool).name} で展開できない: {self.path}: {message[-300:]}")

    def _staged(self) -> FolderSource:
        if self._staging is None:
            self._staging = Path(tempfile.mkdtemp(prefix=C.WORK_DIR_PREFIX + "unrar_"))
            self._extract_to(self._staging)
        return FolderSource(self._staging)

    def list_entries(self) -> list[EntryInfo]:
        return self._staged().list_entries()

    def read_entries(self, names: Sequence[str]) -> dict[str, bytes]:
        return self._staged().read_entries(names)

    def extract_all(self, dest: Path) -> None:
        self._extract_to(dest)

    def close(self) -> None:
        if self._staging is not None:
            shutil.rmtree(self._staging, ignore_errors=True)
            self._staging = None


# --- バックエンド降格ラッパ -------------------------------------------------


class FallbackSource(ArchiveSource):
    """複数バックエンドを優先順に試し、失敗したら次へ降格するラッパ。"""

    def __init__(
        self,
        path: Path,
        kind: str,
        factories: Sequence[tuple[str, Callable[[], ArchiveSource]]],
    ) -> None:
        super().__init__(path, kind)
        if not factories:
            raise ArchiveError(
                f"{kind} を読めるバックエンドが無い: {path} "
                f"({tools.describe_backends()})"
            )
        self._factories = list(factories)
        self._index = 0
        self._current: Optional[ArchiveSource] = None
        self._errors: list[str] = []

    @property
    def backend_name(self) -> str:
        return self._factories[min(self._index, len(self._factories) - 1)][0]

    def _ensure(self) -> ArchiveSource:
        while self._current is None:
            if self._index >= len(self._factories):
                raise ArchiveError(f"すべてのバックエンドで失敗: {self.path}")
            name, factory = self._factories[self._index]
            try:
                self._current = factory()
                get_logger().debug("%s を %s で開いた", self.path.name, name)
            except ArchiveError as exc:
                get_logger().debug("バックエンド %s 不可(%s)", name, exc)
                self._index += 1
        return self._current

    def _degrade(self, exc: Exception) -> None:
        name = self._factories[self._index][0]
        get_logger().warning(
            "バックエンド %s で失敗したため次を試す: %s (%s)", name, self.path.name, exc
        )
        if self._current is not None:
            self._current.close()
        self._current = None
        self._index += 1

    def _attempt(self, action: Callable[[ArchiveSource], object]) -> object:
        last_error: Optional[Exception] = None
        while self._index < len(self._factories):
            reader = self._ensure()
            try:
                return action(reader)
            except ArchiveError as exc:
                last_error = exc
                self._errors.append(f"[{self._factories[self._index][0]}] {exc}")
                self._degrade(exc)
        raise ArchiveError(
            f"すべてのバックエンドで失敗: {self.path}\n"
            + "\n".join(self._errors or [str(last_error)])
            + self._install_hint()
        )

    def _install_hint(self) -> str:
        """試せたのが標準ツールだけのとき、確実に読める外部ツールの導入を案内する。"""

        if self.kind != "rar":
            return ""
        names = {name for name, _ in self._factories}
        if names & {C.BACKEND_SEVENZIP_CLI, C.BACKEND_UNRAR_CLI, C.BACKEND_RARFILE}:
            return ""
        return "\n→ 7-Zip(7z.exe)か WinRAR(UnRAR.exe / Rar.exe)をインストールすると読めるようになる可能性が高い"

    def list_entries(self) -> list[EntryInfo]:
        return self._attempt(lambda reader: reader.list_entries())  # type: ignore[return-value]

    def read_entries(self, names: Sequence[str]) -> dict[str, bytes]:
        return self._attempt(lambda reader: reader.read_entries(names))  # type: ignore[return-value]

    def extract_all(self, dest: Path) -> None:
        # 空の展開先なら、失敗したバックエンドが残した中途半端なファイルを消して次へ進む
        was_empty = not dest.exists() or not any(dest.iterdir())

        def action(reader: ArchiveSource) -> None:
            try:
                reader.extract_all(dest)
            except ArchiveError:
                if was_empty and dest.exists():
                    for child in dest.iterdir():
                        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
                raise

        self._attempt(action)

    def close(self) -> None:
        if self._current is not None:
            self._current.close()
            self._current = None


# --- ファクトリ -------------------------------------------------------------


def _rar_factories(path: Path, preferred: str) -> list[tuple[str, Callable[[], ArchiveSource]]]:
    factories: list[tuple[str, Callable[[], ArchiveSource]]] = []
    for name in tools.available_rar_backends(preferred):
        if name == C.BACKEND_LIBARCHIVE:
            factories.append((name, lambda p=path: LibarchiveSource(p, "rar")))
        elif name == C.BACKEND_BSDTAR:
            factories.append((name, lambda p=path: BsdtarSource(p, "rar")))
        elif name == C.BACKEND_RARFILE:
            factories.append((name, lambda p=path: RarfileSource(p)))
        elif name == C.BACKEND_SEVENZIP_CLI:
            factories.append((name, lambda p=path: SevenZipCliSource(p, "rar")))
        elif name == C.BACKEND_UNRAR_CLI:
            factories.append((name, lambda p=path: UnrarCliSource(p)))
    return factories


def _sevenzip_factories(path: Path, preferred: str) -> list[tuple[str, Callable[[], ArchiveSource]]]:
    factories: list[tuple[str, Callable[[], ArchiveSource]]] = []
    for name in tools.available_sevenzip_backends(preferred):
        if name == C.BACKEND_PY7ZR:
            factories.append((name, lambda p=path: Py7zrSource(p)))
        elif name == C.BACKEND_LIBARCHIVE:
            factories.append((name, lambda p=path: LibarchiveSource(p, "7z")))
        elif name == C.BACKEND_BSDTAR:
            factories.append((name, lambda p=path: BsdtarSource(p, "7z")))
        elif name == C.BACKEND_SEVENZIP_CLI:
            factories.append((name, lambda p=path: SevenZipCliSource(p, "7z")))
    return factories


def open_source(
    path: Path,
    files: Optional[Sequence[Path]] = None,
    rar_backend: str = "auto",
) -> ArchiveSource:
    """パスに応じた ArchiveSource を返す。`files` 指定時はその集合だけを対象にする。"""

    if files is not None:
        base = path if path.is_dir() else path.parent
        return FolderSource(base, files)
    if path.is_dir():
        return FolderSource(path)
    kind = kind_of(path)
    if kind == "zip":
        return ZipSource(path)
    if kind == "rar":
        return FallbackSource(path, "rar", _rar_factories(path, rar_backend))
    if kind == "7z":
        return FallbackSource(path, "7z", _sevenzip_factories(path, "auto"))
    raise ArchiveError(f"対応していない形式: {path}")
