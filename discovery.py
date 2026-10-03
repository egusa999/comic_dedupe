"""作業ツリーから「巻」単位のアイテムを見つける。

判定ルール:
- 書庫ファイル(zip/cbz/rar/cbr/7z)        → 1 巻アイテム
- 直下に画像が MIN_IMAGES_AS_VOLUME 枚以上 → そのフォルダが 1 巻アイテム(配下へは潜らない)
- ただし直下画像のファイル名が複数の巻に分かれている場合は、巻ごとの imageset アイテムに分割
- それ以外のフォルダ                       → さらに再帰する
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import constants as C
from . import naming
from . import pages
from .archives import is_archive, kind_of
from .logging_setup import get_logger
from .models import TitleKey, VolumeItem


def discover_volumes(root: Path, dup_dir_name: str = C.DUP_DIR_NAME) -> list[VolumeItem]:
    """root 配下の巻アイテムを列挙する。"""

    items: list[VolumeItem] = []
    _walk(root, root, dup_dir_name, items)
    items.sort(key=lambda item: pages.natural_key(item.rel_path))
    return items


def _walk(
    directory: Path,
    root: Path,
    dup_dir_name: str,
    items: list[VolumeItem],
) -> None:
    logger = get_logger()
    try:
        children = sorted(directory.iterdir())
    except OSError as exc:
        logger.warning("フォルダを読めないのでスキップ: %s (%s)", directory, exc)
        return

    image_files = [
        child
        for child in children
        if child.is_file()
        and pages.is_image_name(child.name)
        and not pages.is_ignored_name(child.name)
    ]
    archive_files = [child for child in children if is_archive(child)]
    subdirectories = [child for child in children if child.is_dir()]

    for archive in archive_files:
        items.append(_make_archive_item(archive, root))

    if len(image_files) >= C.MIN_IMAGES_AS_VOLUME:
        grouped = _group_images_by_volume(image_files)
        if len(grouped) >= 2:
            logger.info(
                "%s 直下の画像をファイル名から %d 巻に分割", _relative(directory, root), len(grouped)
            )
            for volume, files in grouped.items():
                items.append(_make_imageset_item(directory, root, volume, files))
        else:
            items.append(_make_folder_item(directory, root))
        return  # 巻フォルダとして確定したので配下へは潜らない

    if image_files:
        logger.debug(
            "画像が %d 枚しかないため巻として扱わない: %s",
            len(image_files),
            _relative(directory, root),
        )

    for subdirectory in subdirectories:
        if subdirectory.name == dup_dir_name:
            logger.info("重複フォルダなので探索対象から除外: %s", _relative(subdirectory, root))
            continue
        _walk(subdirectory, root, dup_dir_name, items)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix() or "."
    except ValueError:
        return str(path)


def _make_archive_item(path: Path, root: Path) -> VolumeItem:
    return VolumeItem(
        path=path,
        kind=kind_of(path) or "zip",
        title=naming.parse_title(path.name),
        rel_path=_relative(path, root),
        display_name=path.name,
    )


def _make_folder_item(path: Path, root: Path) -> VolumeItem:
    title = naming.parse_title(path.name)
    if not title.has_volume:
        title = _title_from_children(path, title)
    return VolumeItem(
        path=path,
        kind="folder",
        title=title,
        rel_path=_relative(path, root),
        display_name=path.name + "/",
    )


def _make_imageset_item(
    directory: Path,
    root: Path,
    volume: str,
    files: list[Path],
) -> VolumeItem:
    directory_title = naming.parse_title(directory.name)
    series = directory_title.series or naming.parse_title(files[0].name).series
    title = TitleKey(
        series=series,
        volume=volume,
        volume_kind=C.VOLUME_KIND_NUMBER,
        editions=directory_title.editions,
        raw=f"{directory.name}/{volume}",
    )
    return VolumeItem(
        path=directory,
        kind="imageset",
        title=title,
        rel_path=f"{_relative(directory, root)}#第{volume}巻",
        member_files=tuple(files),
        display_name=f"{directory.name}/ (第{volume}巻の画像 {len(files)} 枚)",
    )


def _title_from_children(directory: Path, fallback: TitleKey) -> TitleKey:
    """フォルダ名から巻数が取れないとき、中の画像名から巻数を推定する。"""

    for child in sorted(directory.iterdir()):
        if not child.is_file() or not pages.is_image_name(child.name):
            continue
        # 「001.jpg」のようなページ番号を巻数と誤認しないよう、明示的な巻表記のみ採用する
        child_title = naming.parse_title(child.name, use_fallback=False)
        if child_title.has_volume:
            return TitleKey(
                series=fallback.series or child_title.series,
                volume=child_title.volume,
                volume_kind=child_title.volume_kind,
                editions=fallback.editions,
                raw=fallback.raw,
            )
    return fallback


def _group_images_by_volume(image_files: list[Path]) -> dict[str, list[Path]]:
    """画像ファイル名から巻ごとにまとめる(巻が 1 つなら分割しない)。"""

    groups: dict[str, list[Path]] = {}
    for image in image_files:
        volume: Optional[str] = naming.parse_title(image.name).volume
        if volume is None:
            return {}
        groups.setdefault(volume, []).append(image)

    if len(groups) < 2:
        return {}
    if any(len(files) < C.MIN_IMAGES_AS_VOLUME for files in groups.values()):
        return {}
    return groups
