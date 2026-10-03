"""残した巻の名前を `作品名 第NN巻`(巻フォルダ名)に統一する名前を決める(ファイルは触らない)。

- 作品名は、同じ作品と見なせる巻の元の名前から多数決で決める(表記ゆれ・作者名・タグ込みの名前を揃える)
- 巻数はゼロ埋めし、桁数は最大巻数に合わせて全巻で揃える
- 巻数が取れない書庫は元の名前を保つ(推測で巻数を付けない)
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional, Sequence

from . import constants as C
from . import matching, naming
from .models import VolumeItem


def sanitize_filename(text: str) -> str:
    """Windows で使えない文字を除き、末尾のドット・空白を落とす。"""

    cleaned = "".join("_" if ch in C.INVALID_FILENAME_CHARS else ch for ch in text)
    return cleaned.strip(" .")


def unified_extension(path: Path) -> str:
    """zip/cbz は .zip に揃え、rar・7z は元の拡張子(小文字)を保つ。"""

    suffix = path.suffix.lower()
    if suffix in C.ZIP_EXTENSIONS:
        return C.UNIFIED_ZIP_EXTENSION
    return suffix


def _is_chapter(item: VolumeItem) -> bool:
    return item.title.volume_kind == C.VOLUME_KIND_CHAPTER


def volume_digits(items: Sequence[VolumeItem], chapters: bool = False) -> int:
    """全巻(chapters=True なら全話)で揃えるゼロ埋め桁数(最大値の桁数、最小は種別ごとの下限)。"""

    widest = C.CHAPTER_MIN_DIGITS if chapters else C.VOLUME_MIN_DIGITS
    for item in items:
        if item.title.volume is None or _is_chapter(item) != chapters:
            continue
        for part in item.title.volume.split("-"):
            widest = max(widest, len(part.lstrip("0")) or 1)
    return widest


def format_volume(volume: str, digits: int) -> str:
    return "-".join(part.zfill(digits) for part in volume.split("-"))


def _cluster_series(
    items: Sequence[VolumeItem],
    linked_groups: Sequence[Sequence[VolumeItem]],
    threshold: float,
    aliases: dict,
) -> dict[int, int]:
    """作品名のあるアイテムを同じ作品ごとに束ねる(index → クラスタ番号)。

    束ねる根拠は (1) 作品名のあいまい一致、(2) 同じ重複グループ(=同じ巻と判定済み)に属すること。
    (2) により、ローマ字名と日本語名のように表記が全く違っても、同じ巻の重複を介して同一作品になる。
    """

    union = matching.UnionFind(len(items))
    position = {id(item): index for index, item in enumerate(items)}
    for group in linked_groups:
        members = [position[id(item)] for item in group if id(item) in position]
        for other in members[1:]:
            union.union(members[0], other)
    named = [i for i, item in enumerate(items) if item.title.series]
    for pos, left in enumerate(named):
        for right in named[pos + 1:]:
            score, _ = matching.similarity(items[left].title, items[right].title, aliases)
            if score >= threshold:
                union.union(left, right)
    return {i: union.find(i) for i in named}


def _has_cjk(text: str) -> bool:
    return any(C.CJK_RANGE_START <= ord(ch) <= C.CJK_RANGE_END for ch in text)


def _best_name(names: Sequence[str]) -> str:
    """作品名の代表を選ぶ。日本語を含む名前 > 多数派 > 長い(情報量が多い)名前 > 辞書順。"""

    counts = Counter(names)
    return sorted(counts, key=lambda n: (not _has_cjk(n), -counts[n], -len(n), n))[0]


def plan_names(
    survivors: Sequence[VolumeItem],
    linked_groups: Sequence[Sequence[VolumeItem]] = (),
    fallback_series: str = "",
    threshold: float = C.SERIES_SIMILARITY_MIN,
    aliases: Optional[dict] = None,
) -> dict[int, str]:
    """残す各アイテム(`id(item)`)の統一名(拡張子なし)を返す。巻数不明と、複数巻が混在する imageset は含めない。

    作品名の候補には、退避された敗者の名前も含める(同じ作品の別表記を拾うため)。
    """

    aliases = aliases or {}
    universe = list(survivors)
    known = {id(item) for item in universe}
    for group in linked_groups:
        for item in group:
            if id(item) not in known:
                known.add(id(item))
                universe.append(item)

    cluster_of = _cluster_series(universe, linked_groups, threshold, aliases)
    cluster_names: dict[int, list[str]] = {}
    for index, root in cluster_of.items():
        shown = naming.display_series(universe[index].path.name)
        if shown:
            cluster_names.setdefault(root, []).append(shown)
    cluster_label = {root: _best_name(names) for root, names in cluster_names.items()}

    # 作品名の取れない巻(07.zip など)には、最も巻数の多い作品名を当てる
    biggest = Counter(cluster_of.values()).most_common(1)
    default_series = (
        cluster_label.get(biggest[0][0], fallback_series) if biggest else fallback_series
    )

    digits = volume_digits(survivors)
    chapter_digits = volume_digits(survivors, chapters=True)
    plan: dict[int, str] = {}
    for index, item in enumerate(survivors):
        if item.title.volume is None:
            continue  # 巻数不明は元の名前のまま
        series = cluster_label.get(cluster_of.get(index, -1), default_series)
        series = sanitize_filename(series)[: C.MAX_SERIES_NAME_LENGTH].strip(" .")
        if _is_chapter(item):
            template = (
                C.UNIFIED_CHAPTER_TEMPLATE if series else C.UNIFIED_CHAPTER_TEMPLATE_NO_SERIES
            )
            stem = template.format(
                series=series, chapter=format_volume(item.title.volume, chapter_digits)
            )
        else:
            template = C.UNIFIED_NAME_TEMPLATE if series else C.UNIFIED_NAME_TEMPLATE_NO_SERIES
            stem = template.format(series=series, volume=format_volume(item.title.volume, digits))
        plan[id(item)] = stem
    return plan
