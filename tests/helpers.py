"""テスト用の合成データ生成ヘルパ。"""

from __future__ import annotations

import zipfile
from pathlib import Path

from PIL import Image, ImageDraw


def write_pages(
    folder: Path,
    pages: int,
    width: int,
    height: int,
    quality: int = 85,
    seed: int = 0,
) -> Path:
    """JPEG のページ画像を folder に作る。"""

    folder.mkdir(parents=True, exist_ok=True)
    for index in range(1, pages + 1):
        image = Image.new("RGB", (width, height), (230, 230, 230))
        draw = ImageDraw.Draw(image)
        for position in range(0, width, max(8, width // 10)):
            draw.line(
                [(position, 0), (position + index * 3 + seed, height)],
                fill=(40 + (position % 180), 70, 120),
                width=3,
            )
        image.save(folder / f"{index:03d}.jpg", "JPEG", quality=quality)
    return folder


def zip_folder(folder: Path, out_zip: Path, compress: bool = True) -> Path:
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", mode) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(folder).as_posix())
    return out_zip
