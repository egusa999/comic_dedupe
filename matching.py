"""タイトルのあいまい照合(fuzzy matching)。

同じ巻数・同じ版のアイテム同士について作品名の類似度を測り、
- しきい値以上 → 同一巻として同じグループへ
- グレーゾーン → 保留(既定では動かさず、GUI/CLI で人が確認)
- それ未満 → 別物
に 3 分類する。
"""

from __future__ import annotations

import json
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Optional, Sequence

from . import constants as C
from . import naming
from .logging_setup import get_logger
from .models import DupGroup, ReviewPair, TitleKey, VolumeItem

METHOD_EXACT = "完全一致"
METHOD_ALIAS = "エイリアス一致"
METHOD_SUBSTRING = "部分一致"
METHOD_SEQUENCE = "文字列類似"
METHOD_BIGRAM = "bigram一致"
METHOD_EDITION_DIFF = "版違い"
METHOD_VOLUME_ONLY = "巻数のみ一致(作品名なし)"
METHOD_ONE_SIDED = "片方のみ作品名あり"


class UnionFind:
    """グループ化用の素集合データ構造。"""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, index: int) -> int:
        while self._parent[index] != index:
            self._parent[index] = self._parent[self._parent[index]]
            index = self._parent[index]
        return index

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[right_root] = left_root


def load_aliases(path: Optional[Path]) -> dict[str, str]:
    """作品名エイリアス表(JSON)を読む。{"代表名": ["別表記", ...]} 形式。"""

    if path is None:
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"エイリアスファイルを読めない: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"エイリアスファイルの形式が不正(辞書ではない): {path}")

    table: dict[str, str] = {}
    for canonical, variants in raw.items():
        canonical_key = naming.normalize_series(naming.basic_normalize(str(canonical)))
        if not canonical_key:
            continue
        table[canonical_key] = canonical_key
        if isinstance(variants, str):
            variants = [variants]
        if not isinstance(variants, Iterable):
            raise ValueError(f"エイリアスの値が不正: {canonical}")
        for variant in variants:
            variant_key = naming.normalize_series(naming.basic_normalize(str(variant)))
            if variant_key:
                table[variant_key] = canonical_key
    return table


def canonical_series(series: str, aliases: dict[str, str]) -> str:
    return aliases.get(series, series)


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i: i + 2] for i in range(len(text) - 1)}


def _dice(left: str, right: str) -> float:
    left_grams, right_grams = _bigrams(left), _bigrams(right)
    if not left_grams or not right_grams:
        return 0.0
    overlap = len(left_grams & right_grams)
    return (2 * overlap) / (len(left_grams) + len(right_grams))


def similarity(
    left: TitleKey,
    right: TitleKey,
    aliases: Optional[dict[str, str]] = None,
) -> tuple[float, str]:
    """作品名の類似度と採用した指標名を返す。"""

    aliases = aliases or {}
    if left.editions != right.editions:
        return 0.0, METHOD_EDITION_DIFF

    left_series = canonical_series(left.series, aliases)
    right_series = canonical_series(right.series, aliases)
    if not left_series and not right_series:
        # 「第7巻」と「07」のように巻数しか書かれていないケース
        return C.EMPTY_SERIES_SIMILARITY, METHOD_VOLUME_ONLY
    if not left_series or not right_series:
        # 片方だけ作品名がある → 自動判定せずグレーゾーンに落とす
        return C.ONE_SIDED_SERIES_SIMILARITY, METHOD_ONE_SIDED
    if left_series == right_series:
        method = METHOD_ALIAS if left.series != right.series else METHOD_EXACT
        return 1.0, method

    scores: list[tuple[float, str]] = []
    shorter, longer = sorted((left_series, right_series), key=len)
    if len(shorter) >= C.SUBSTRING_MIN_LENGTH and shorter in longer:
        scores.append((C.SUBSTRING_SIMILARITY, METHOD_SUBSTRING))
    scores.append((SequenceMatcher(None, left_series, right_series).ratio(), METHOD_SEQUENCE))
    scores.append((_dice(left_series, right_series), METHOD_BIGRAM))
    return max(scores, key=lambda pair: pair[0])


def pair_key(left: VolumeItem, right: VolumeItem) -> frozenset:
    """グレーゾーンのペアを識別するキー(GUI での個別承認に使う)。"""

    return frozenset({left.rel_path or left.label(), right.rel_path or right.label()})


def adopt_scope_series(items: Sequence[VolumeItem], aliases: Optional[dict[str, str]] = None) -> int:
    """作品名の無い巻(`8_files`・`07.zip` など)に、同じフォルダの作品名を引き継がせる。

    同じ親フォルダの中で、作品名のある巻がすべて同じ作品名なら、そのフォルダは 1 作品と見なせる。
    作品名が複数混在するフォルダでは何もしない(別作品への誤統合を避ける)。
    戻り値: 作品名を補った件数。
    """

    aliases = aliases or {}
    named: dict[str, list[VolumeItem]] = {}
    nameless: dict[str, list[VolumeItem]] = {}
    for item in items:
        if not item.title.has_volume:
            continue
        bucket = named if item.title.series else nameless
        bucket.setdefault(scope_key(item), []).append(item)

    adopted = 0
    for scope, members in nameless.items():
        series_names = {canonical_series(m.title.series, aliases) for m in named.get(scope, [])}
        if len(series_names) != 1:
            continue
        donor = named[scope][0].title.series
        for item in members:
            item.title = replace(item.title, series=donor)
            adopted += 1
    if adopted:
        get_logger().info("作品名の無い巻 %d 件に、同じフォルダの作品名を引き継ぎ", adopted)
    return adopted


def group_items(
    items: Sequence[VolumeItem],
    aliases: Optional[dict[str, str]] = None,
    auto_threshold: float = C.SERIES_SIMILARITY_MIN,
    review_threshold: float = C.SERIES_SIMILARITY_REVIEW,
    accept_review: bool = False,
    approved_pairs: Optional[set] = None,
) -> tuple[list[DupGroup], list[ReviewPair], list[VolumeItem]]:
    """アイテムを重複グループに分ける。

    戻り値: (重複グループ, グレーゾーンのペア, 巻数が取れなかったアイテム)
    """

    logger = get_logger()
    aliases = aliases or {}
    adopt_scope_series(items, aliases)

    no_volume = [item for item in items if not item.title.has_volume]
    for item in no_volume:
        logger.info("巻数を判定できないため保留: %s", item.label())

    targets = [item for item in items if item.title.has_volume]
    buckets: dict[str, list[VolumeItem]] = {}
    for item in targets:
        buckets.setdefault(naming.volume_group_key(item.title), []).append(item)

    groups: list[DupGroup] = []
    reviews: list[ReviewPair] = []

    for bucket_key, bucket_items in sorted(buckets.items()):
        union = UnionFind(len(bucket_items))
        pair_scores: dict[tuple[int, int], tuple[float, str]] = {}

        for i in range(len(bucket_items)):
            for j in range(i + 1, len(bucket_items)):
                score, method = similarity(
                    bucket_items[i].title, bucket_items[j].title, aliases
                )
                logger.debug(
                    "照合: %s ⇔ %s = %.3f (%s)",
                    bucket_items[i].label(),
                    bucket_items[j].label(),
                    score,
                    method,
                )
                if score >= auto_threshold:
                    union.union(i, j)
                    pair_scores[(i, j)] = (score, method)
                elif score >= review_threshold:
                    pair = ReviewPair(
                        left=bucket_items[i],
                        right=bucket_items[j],
                        similarity=score,
                        method=method,
                    )
                    approved = accept_review or (
                        approved_pairs is not None
                        and pair_key(bucket_items[i], bucket_items[j]) in approved_pairs
                    )
                    if approved:
                        union.union(i, j)
                        pair_scores[(i, j)] = (score, method)
                        logger.info("グレーゾーンを承認により結合: %s", pair.describe())
                    else:
                        reviews.append(pair)
                        logger.info("保留(要確認): %s", pair.describe())

        clustered: dict[int, list[int]] = {}
        for index in range(len(bucket_items)):
            clustered.setdefault(union.find(index), []).append(index)

        for root, indices in sorted(clustered.items()):
            members = [bucket_items[i] for i in indices]
            relevant = [
                pair_scores[(i, j)]
                for (i, j) in pair_scores
                if i in indices and j in indices
            ]
            group_similarity = min((score for score, _ in relevant), default=1.0)
            notes = [f"{method} {score:.2f}" for score, method in relevant]
            groups.append(
                DupGroup(
                    key=f"{bucket_key}#{root}",
                    items=members,
                    similarity=group_similarity,
                    match_notes=notes,
                )
            )

    return groups, reviews, no_volume


# --- 最大巻数の推測と再判定 -------------------------------------------------


def scope_key(item: VolumeItem) -> str:
    """「同じ作品が置かれているはずの範囲」= 巻アイテムの親フォルダ(相対パス)。"""

    rel = item.rel_path.split("#", 1)[0].rstrip("/")
    if item.kind == "imageset":
        return rel  # 画像が直に置かれたフォルダ自体が、同居する書庫の親でもある
    return rel.rpartition("/")[0]


def _is_under(scope_dir: str, ancestor: str) -> bool:
    return not ancestor or scope_dir == ancestor or scope_dir.startswith(ancestor + "/")


def _single_volume_number(group: DupGroup) -> Optional[int]:
    """単巻(範囲・上下巻でない)グループの巻数。判定できなければ None。"""

    first = group.items[0].title
    if first.volume_kind != C.VOLUME_KIND_NUMBER or first.volume is None:
        return None
    number = int(first.volume)
    return number if number <= C.MAX_PLAUSIBLE_VOLUME else None


def retry_overflowing_scopes(
    groups: Sequence[DupGroup],
    reviews: Sequence[ReviewPair],
    aliases: Optional[dict[str, str]] = None,
) -> tuple[list[DupGroup], list[ReviewPair], list[str]]:
    """残った巻の数が推定最大巻数を超えるフォルダだけ、重複判定をやり直す。

    最大巻数 = フォルダ内の単巻の最大巻数。巻数は 1..最大 の範囲に収まるので、
    残り件数がそれを超えるなら同じ巻が別グループに残っている(作品名の表記が違う等)。
    その場合に限り、作品名を無視して「同じ巻数・同じ版」を同一巻として統合する。
    戻り値: (新しいグループ, 残すグループ以外の保留ペア, 再判定の説明文のリスト)
    """

    logger = get_logger()
    single_groups = [group for group in groups if _single_volume_number(group) is not None]
    scopes: set[str] = set()
    for group in single_groups:
        parts = scope_key(group.items[0]).split("/") if scope_key(group.items[0]) else []
        scopes.update("/".join(parts[:depth]) for depth in range(len(parts) + 1))

    retried_scopes: set[str] = set()
    replaced: set[int] = set()
    new_groups: list[DupGroup] = []
    notes: list[str] = []

    # 深いフォルダから順に、その配下(サブフォルダ含む)で判定する。
    # 書庫が巻ごとに別フォルダへ入っていても、親フォルダ側で超過を検出できる。
    for scope in sorted(scopes, key=lambda value: (-value.count("/") - (1 if value else 0), value)):
        scope_groups = [
            group
            for group in single_groups
            if id(group) not in replaced
            and all(_is_under(scope_key(item), scope) for item in group.items)
        ]
        if not scope_groups:
            continue
        numbers = [_single_volume_number(group) for group in scope_groups]
        max_volume = max(number for number in numbers if number is not None)
        if len(scope_groups) <= max_volume:
            continue

        buckets: dict[str, list[DupGroup]] = {}
        for group in scope_groups:
            buckets.setdefault(naming.volume_group_key(group.items[0].title), []).append(group)

        merged_any = False
        for bucket_key, members in sorted(buckets.items()):
            if len(members) < 2:
                continue
            merged_any = True
            items = [item for group in members for item in group.items]
            scores = [
                similarity(a.items[0].title, b.items[0].title, aliases)[0]
                for index, a in enumerate(members)
                for b in members[index + 1:]
            ]
            new_groups.append(
                DupGroup(
                    key=f"{bucket_key}#relaxed:{scope or '.'}",
                    items=items,
                    similarity=min(scores, default=1.0),
                    match_notes=["最大巻数超過の再判定: 作品名を無視し巻数一致で統合"],
                )
            )
            replaced.update(id(group) for group in members)

        if merged_any:
            retried_scopes.add(scope)
            count = len(scope_groups)
            message = (
                f"再判定: {scope or '(最上位)'} は残り {count} 件 > 推定最大巻数 {max_volume} "
                f"のため、作品名を無視して巻数一致で統合"
            )
            notes.append(message)
            logger.warning(message)
        else:
            logger.warning(
                "最大巻数超過だが同一巻数のグループが無く統合できない: %s (残り %d 件 / 最大 %d)",
                scope or "(最上位)",
                len(scope_groups),
                max_volume,
            )

    if not replaced:
        return list(groups), list(reviews), notes

    kept = [group for group in groups if id(group) not in replaced]
    kept_reviews = [
        pair
        for pair in reviews
        if not any(
            _is_under(scope_key(pair.left), scope) and _is_under(scope_key(pair.right), scope)
            for scope in retried_scopes
        )
    ]
    return kept + new_groups, kept_reviews, notes
