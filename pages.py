"""ページ画像の列挙・サンプリング・計測。

画質比較のために「どのページを何枚見るか」と「寸法とバイト数をどう取るか」だけを担当する。
全ページを展開せず、Pillow の遅延読み込み(ヘッダだけで size が分かる)を利用する。
"""

from __future__ import annotations

import io
import re
from typing import Iterable, Optional, Sequence

from PIL import Image, UnidentifiedImageError

from . import constants as C
from .archives import ArchiveError, ArchiveSource
from .logging_setup import get_logger
from .models import EntryInfo, PageInfo

_DIGITS = re.compile(r"(\d+)")

# 1ピクセルあたりの巨大画像対策(デコード爆弾の回避は Pillow 既定の上限に任せる)
Image.MAX_IMAGE_PIXELS = None


def is_image_name(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(ext) for ext in C.IMAGE_EXTENSIONS)


def is_ignored_name(name: str) -> bool:
    lowered = name.lower()
    if any(lowered.startswith(prefix.lower()) for prefix in C.IGNORED_ENTRY_PREFIXES):
        return True
    basename = lowered.rsplit("/", 1)[-1]
    return basename in C.IGNORED_ENTRY_NAMES


def natural_key(name: str) -> tuple:
    """1.jpg, 2.jpg, 10.jpg が数値順に並ぶキー。"""

    parts = _DIGITS.split(name.lower())
    return tuple(int(part) if part.isdigit() else part for part in parts)


def list_page_entries(source: ArchiveSource) -> list[EntryInfo]:
    """書庫/フォルダ内の画像エントリを自然順で返す。"""

    entries = [
        entry
        for entry in source.list_entries()
        if not entry.is_dir and is_image_name(entry.name) and not is_ignored_name(entry.name)
    ]
    entries.sort(key=lambda entry: natural_key(entry.name))
    return entries


def sample_entries(entries: Sequence[EntryInfo], limit: int = C.SAMPLE_PAGES) -> list[EntryInfo]:
    """等間隔に最大 limit 件を抜き出す(先頭と末尾を必ず含む)。"""

    if limit <= 0 or len(entries) <= limit:
        return list(entries)
    step = (len(entries) - 1) / (limit - 1)
    indices = sorted({int(round(i * step)) for i in range(limit)})
    return [entries[index] for index in indices]


def dhash(image: Image.Image, hash_size: int = C.HASH_SIZE) -> int:
    """差分ハッシュ(内容照合オプション用)。"""

    resized = image.convert("L").resize((hash_size + 1, hash_size), Image.BILINEAR)
    pixels = list(resized.getdata())
    bits = 0
    for row in range(hash_size):
        offset = row * (hash_size + 1)
        for col in range(hash_size):
            left = pixels[offset + col]
            right = pixels[offset + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def hamming_distance(left: int, right: int) -> int:
    return bin(left ^ right).count("1")


def measure_pages(
    source: ArchiveSource,
    entries: Iterable[EntryInfo],
    want_hash: bool = False,
) -> list[PageInfo]:
    """サンプルページの寸法・バイト数(必要なら dHash)を測る。"""

    logger = get_logger()
    results: list[PageInfo] = []
    for entry in entries:
        try:
            stream, size_bytes = _open_measured(source, entry)
        except ArchiveError as exc:
            logger.debug("ページを読めないので除外: %s (%s)", entry.name, exc)
            continue
        try:
            with stream:
                with Image.open(stream) as image:
                    width, height = image.size
                    image_format = image.format or ""
                    page_hash: Optional[int] = None
                    if want_hash:
                        image.draft("L", (C.HASH_SIZE * 4, C.HASH_SIZE * 4))
                        page_hash = dhash(image)
            results.append(
                PageInfo(
                    name=entry.name,
                    width=width,
                    height=height,
                    size_bytes=size_bytes,
                    image_format=image_format,
                    dhash=page_hash,
                )
            )
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            logger.debug("画像として解釈できないので除外: %s (%s)", entry.name, exc)
    return results


def _open_measured(source: ArchiveSource, entry: EntryInfo) -> tuple[io.IOBase, int]:
    """エントリをストリームとして開き、バイト数も確定させる。"""

    if entry.size is None:
        data = source.read_entry(entry.name)
        return io.BytesIO(data), len(data)
    return source.open_entry(entry.name), entry.size  # type: ignore[return-value]
