"""画質の計測と勝者選定。

「どちらを残すか」をここだけで決める。比較順は
  1. ページ画素数の中央値(解像度が高い方)
  2. bytes/pixel(圧縮が緩い=劣化が少ない方)
  3. 総バイト数
(ページ数は比較にも保留判定にも使わない)
で、相対差が QUALITY_MARGIN 以内なら「同等」として次の指標へ進む。
全指標同等なら決定論的タイブレーク(書庫種別 → 名前の長さ → パス)で決め、理由を必ず残す。
"""

from __future__ import annotations

import functools
from statistics import median
from typing import Callable, Optional, Sequence

from . import constants as C
from . import pages
from . import external_tools as tools
from .archives import ArchiveError, open_source
from .logging_setup import get_logger
from .models import Decision, DupGroup, VolumeItem


def measure_item(
    item: VolumeItem,
    sample_limit: int = C.SAMPLE_PAGES,
    want_hash: bool = False,
    rar_backend: str = "auto",
) -> VolumeItem:
    """1 巻の計測結果を item に埋める。失敗は measure_error に残して例外にしない。

    RAR は、一覧の取得とページの読み出しを別のバックエンドで行うと名前の文字コードが食い違って
    読めなくなる(bsdtar の化けた名前を UnRAR に渡す等)ため、読めなかったら
    バックエンドを 1 つずつ指定して、一覧から最初からやり直す。
    """

    logger = get_logger()
    attempts = [rar_backend]
    if item.kind == "rar" and rar_backend == "auto":
        attempts += [name for name in tools.available_rar_backends("auto")]
    error = ""
    for backend in attempts:
        item.measure_error = ""
        _measure_once(item, sample_limit, want_hash, backend)
        if item.measured:
            break
        error = item.measure_error
        if backend != attempts[-1]:
            logger.debug("バックエンド %s では計測できなかったため次を試す: %s", backend, item.label())
    if not item.measured:
        item.measure_error = error or item.measure_error
        logger.warning("計測できないため判定対象外: %s (%s)", item.label(), item.measure_error)
    return item


def _measure_once(item: VolumeItem, sample_limit: int, want_hash: bool, rar_backend: str) -> None:
    logger = get_logger()
    item.pages = ()
    try:
        files = item.member_files if item.kind == "imageset" else None
        with open_source(item.path, files=files, rar_backend=rar_backend) as source:
            entries = pages.list_page_entries(source)
            if not entries:
                item.measure_error = "画像エントリが見つからない"
                return
            item.page_count = len(entries)
            item.total_bytes = _total_bytes(item, entries)
            item.pages = pages.measure_pages(
                source, pages.sample_entries(entries, sample_limit), want_hash=want_hash
            )
        if not item.pages:
            item.measure_error = "サンプルページを計測できない"
            return
        logger.debug(
            "計測: %s ページ数=%d 中央画素=%.0f bytes/pixel=%.4f 総バイト=%d",
            item.label(),
            item.page_count,
            item.median_pixels,
            item.median_bytes_per_pixel,
            item.total_bytes,
        )
    except ArchiveError as exc:
        item.measure_error = str(exc)
    except Exception as exc:  # 想定外は握りつぶさずログに残して対象外にする
        item.measure_error = f"想定外のエラー: {exc}"
        logger.exception("計測中に想定外のエラー: %s", item.label())


def _total_bytes(item: VolumeItem, entries: Sequence) -> int:
    if item.kind in ("folder", "imageset"):
        return sum(entry.size or 0 for entry in entries)
    try:
        return item.path.stat().st_size
    except OSError:
        return sum(entry.size or 0 for entry in entries)


def _relative_difference(left: float, right: float) -> float:
    base = max(abs(left), abs(right))
    if base == 0:
        return 0.0
    return abs(left - right) / base


def compare_items(
    left: VolumeItem,
    right: VolumeItem,
    margin: float = C.QUALITY_MARGIN,
) -> tuple[int, str]:
    """left が勝つなら正、right が勝つなら負を返す。第 2 要素は理由。ページ数は考慮しない。"""

    metrics = (
        ("解像度(中央画素数)", left.median_pixels, right.median_pixels),
        ("bytes/pixel(圧縮の緩さ)", left.median_bytes_per_pixel, right.median_bytes_per_pixel),
        ("総バイト数", float(left.total_bytes), float(right.total_bytes)),
    )
    for label, left_value, right_value in metrics:
        if _relative_difference(left_value, right_value) <= margin:
            continue
        sign = 1 if left_value > right_value else -1
        better = max(left_value, right_value)
        return sign, f"{label}が大きい({better:.4g})"

    if left.source_rank != right.source_rank:
        sign = 1 if left.source_rank > right.source_rank else -1
        kind = left.kind if sign > 0 else right.kind
        return sign, f"全指標同等のため書庫種別で決定({kind})"

    left_label, right_label = left.label(), right.label()
    if len(left_label) != len(right_label):
        sign = 1 if len(left_label) > len(right_label) else -1
        return sign, "全指標同等のため情報量の多い名前を採用"

    if left_label != right_label:
        sign = 1 if left_label < right_label else -1
        return sign, "全指標同等のため名前順で決定"

    return 0, "完全に同等"


def content_match_ratio(left: VolumeItem, right: VolumeItem) -> Optional[float]:
    """サンプルページの dHash 一致率(内容照合オプション用)。測れなければ None。"""

    left_hashes = [page.dhash for page in left.pages if page.dhash is not None]
    right_hashes = [page.dhash for page in right.pages if page.dhash is not None]
    if not left_hashes or not right_hashes:
        return None
    matched = sum(
        1
        for value in left_hashes
        if any(
            pages.hamming_distance(value, other) <= C.HASH_DISTANCE_MAX
            for other in right_hashes
        )
    )
    return matched / len(left_hashes)


def find_size_outliers(
    items: Sequence[VolumeItem],
    scope_of: Callable[[VolumeItem], str],
) -> dict[int, str]:
    """同じフォルダの他の単巻と比べて総バイト数が極端に小さい/大きいアイテムを返す(id(item) → 理由)。

    欠けた(途中で切れた)書庫や、別物(雑誌など)が画質指標だけで勝ってしまうのを防ぐため。
    基準にするのは単巻だけ。範囲表記は「巻数 × 単巻の中央値」と比べて小さすぎるときだけ判定する。
    話数・上下巻の書庫は判定しない。
    """

    judged = [
        item
        for item in items
        if item.measured
        and item.total_bytes > 0
        and item.title.volume_kind == C.VOLUME_KIND_NUMBER
        and item.title.has_volume
    ]
    scopes = {id(item): scope_of(item) for item in judged}

    def subtree(prefix: str) -> list[VolumeItem]:
        return [
            item
            for item in judged
            if not prefix or scopes[id(item)] == prefix or scopes[id(item)].startswith(prefix + "/")
        ]

    outliers: dict[int, str] = {}
    for item in judged:
        # 自分のフォルダから親へ遡り、単巻が十分な数ある最初のフォルダを基準にする
        # (雑誌など別物だけが入ったフォルダでも、作品全体の巻と比べて外れ値になる)
        parts = scopes[id(item)].split("/") if scopes[id(item)] else []
        members: list[VolumeItem] = []
        for depth in range(len(parts), -1, -1):
            members = subtree("/".join(parts[:depth]))
            if len(members) >= C.OUTLIER_MIN_REFERENCE_COUNT:
                break
        if len(members) < C.OUTLIER_MIN_REFERENCE_COUNT:
            continue
        reference = float(median(m.total_bytes for m in members))
        ratio = item.total_bytes / reference
        if ratio < C.OUTLIER_LOW_RATIO:
            outliers[id(item)] = f"同じフォルダの他の巻に比べ極端に小さい(中央値の {ratio:.0%})"
        elif ratio > C.OUTLIER_HIGH_RATIO:
            outliers[id(item)] = f"同じフォルダの他の巻に比べ極端に大きい(中央値の {ratio:.0%})"

    _add_job_wide_small_outliers(items, judged, outliers)
    return outliers


def _add_job_wide_small_outliers(
    items: Sequence[VolumeItem], judged: Sequence[VolumeItem], outliers: dict[int, str]
) -> None:
    """作品全体(ジョブ全体)の単巻の中央値と比べて極端に小さいものを外れ値に加える。

    フォルダ単位の判定は、雑誌が 50 冊入ったフォルダでは「雑誌の中央値」が基準になって効かない。
    範囲表記の書庫(`第33-34巻` など)は、巻数ぶんの想定サイズ(巻数 × 中央値)と比べる。
    """

    if len(judged) < C.OUTLIER_MIN_REFERENCE_COUNT:
        return
    reference = float(median(item.total_bytes for item in judged))
    for item in judged:
        ratio = item.total_bytes / reference
        if id(item) not in outliers and ratio < C.OUTLIER_LOW_RATIO:
            outliers[id(item)] = f"作品全体の巻に比べ極端に小さい(全体の中央値の {ratio:.0%})"
    for item in items:
        if (
            item.measured
            and item.total_bytes > 0
            and item.title.volume_kind == C.VOLUME_KIND_RANGE
            and item.title.volume
            and id(item) not in outliers
        ):
            first, _, last = item.title.volume.partition("-")
            span = int(last) - int(first) + 1
            ratio = item.total_bytes / (reference * span)
            if ratio < C.OUTLIER_LOW_RATIO:
                outliers[id(item)] = (
                    f"{span} 巻ぶんの想定サイズに比べ極端に小さい(想定の {ratio:.0%})"
                )


def _thin_candidates(candidates: Sequence[VolumeItem]) -> dict[int, str]:
    """同じグループの最大に比べ、ページ数または総バイト数が極端に少ない候補(id(item) → 理由)。

    ページ数の多少は版違いでも起きるので普段は無視するが、桁違いに少ないものは
    欠けた書庫や別物(雑誌など)が混ざっている可能性が高い。最大のアイテム自身は決して外さない。
    """

    if len(candidates) < 2:
        return {}
    max_pages = max(item.page_count for item in candidates)
    max_bytes = max(item.total_bytes for item in candidates)
    thin: dict[int, str] = {}
    for item in candidates:
        if max_pages and item.page_count < max_pages * C.GROUP_PAGE_RATIO_MIN:
            thin[id(item)] = (
                f"画像数が他の候補に比べ極端に少ない({item.page_count} 枚 / 最大 {max_pages} 枚)"
            )
        elif max_bytes and item.total_bytes < max_bytes * C.GROUP_SIZE_RATIO_MIN:
            thin[id(item)] = (
                f"総サイズが他の候補に比べ極端に小さい({item.total_bytes / max_bytes:.0%})"
            )
    return thin


def select_winner(
    group: DupGroup,
    margin: float = C.QUALITY_MARGIN,
    verify_content: bool = False,
    outliers: Optional[dict[int, str]] = None,
) -> Decision:
    """グループ内の勝者と敗者を決める。危険な組は undecided に入れて動かさない。

    outliers に入っているアイテム(サイズ外れ値)と、グループ内で画像数・サイズが桁違いに少ないアイテムは、他に候補が残る限り勝者候補から外して敗者にする。
    候補が全部外れ値なら外さない(比較相手が無いのに消さないため)。
    """

    logger = get_logger()
    decision = Decision(group=group)
    outliers = dict(outliers or {})

    measurable = [item for item in group.items if item.measured]
    outliers.update(_thin_candidates(measurable))
    excluded = [item for item in measurable if id(item) in outliers]
    if len(excluded) == len(measurable):
        excluded = []
    measurable = [item for item in measurable if item not in excluded]
    try:
        return _select_among(decision, group, measurable, margin, verify_content, logger)
    finally:
        for item in excluded:
            decision.losers.append(item)
            decision.reasons[item.label()] = f"サイズ外れ値のため除外: {outliers[id(item)]}"
            if not item.outlier_logged:
                item.outlier_logged = True
                logger.warning("サイズ外れ値のため勝者候補から除外: %s (%s)", item.label(), outliers[id(item)])


def _select_among(
    decision: Decision,
    group: DupGroup,
    measurable: list,
    margin: float,
    verify_content: bool,
    logger,
) -> Decision:
    for item in group.items:
        if not item.measured:
            decision.undecided.append(item)
            decision.reasons[item.label()] = f"計測不能のため保留({item.measure_error})"

    if len(measurable) < 2:
        if len(measurable) == 1:
            decision.winner = measurable[0]
            decision.reasons[measurable[0].label()] = "比較相手がいないためそのまま残す"
        decision.note = "比較できるアイテムが 1 件以下"
        return decision

    ordered = sorted(
        measurable,
        key=functools.cmp_to_key(lambda a, b: -compare_items(a, b, margin)[0]),
    )
    winner = ordered[0]

    if verify_content:
        mismatched = []
        for other in ordered[1:]:
            ratio = content_match_ratio(winner, other)
            if ratio is not None and ratio < C.CONTENT_MATCH_RATIO_MIN:
                mismatched.append((other, ratio))
        for other, ratio in mismatched:
            ordered.remove(other)
            decision.undecided.append(other)
            decision.reasons[other.label()] = (
                f"内容照合でページ一致率が低いため保留(一致率 {ratio:.2f})"
            )
            logger.warning(
                "内容照合で不一致: %s ⇔ %s (一致率 %.2f)", winner.label(), other.label(), ratio
            )
        if len(ordered) < 2:
            decision.winner = winner
            decision.reasons[winner.label()] = "内容照合により比較相手が無くなったため残す"
            decision.note = "内容照合で分離"
            return decision

    decision.winner = winner
    decision.reasons[winner.label()] = _winner_reason(winner, ordered[1:], margin)
    for loser in ordered[1:]:
        _, reason = compare_items(winner, loser, margin)
        decision.losers.append(loser)
        decision.reasons[loser.label()] = f"{winner.label()} の方が{reason}"
    logger.info(
        "判定: %s を残す(%s)", winner.label(), decision.reasons[winner.label()]
    )
    return decision


def _winner_reason(winner: VolumeItem, losers: Sequence[VolumeItem], margin: float) -> str:
    reasons = {compare_items(winner, loser, margin)[1] for loser in losers}
    return " / ".join(sorted(reasons)) if reasons else "比較相手なし"
