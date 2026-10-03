"""モジュール間で受け渡すデータ構造(振る舞いを持たない値オブジェクト)。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Optional, Sequence

from . import constants as C


@dataclass(frozen=True)
class EntryInfo:
    """書庫/フォルダ内の 1 エントリ。"""

    name: str                 # 書庫内のパス(読み出しに使う内部名)
    size: Optional[int] = None  # 不明なバックエンドでは None(読み出し時に確定する)
    is_dir: bool = False


@dataclass(frozen=True)
class TitleKey:
    """タイトルから抽出した作品名と巻数。"""

    series: str                  # 正規化済みの作品名(照合に使う)
    volume: Optional[str]        # "7" / "7-10" など。取れなければ None
    volume_kind: str = C.VOLUME_KIND_NUMBER
    editions: frozenset = frozenset()  # 完全版/新装版などの版違いワード
    raw: str = ""                # 元の名前(表示用)

    @property
    def has_volume(self) -> bool:
        return self.volume is not None

    def describe(self) -> str:
        vol = self.volume if self.volume is not None else "?"
        if self.volume_kind == C.VOLUME_KIND_CHAPTER:
            return f"{self.series or '(作品名不明)'} ch{vol}"
        edition = ("+" + ",".join(sorted(self.editions))) if self.editions else ""
        return f"{self.series or '(作品名不明)'}{edition} 第{vol}巻"


@dataclass(frozen=True)
class PageInfo:
    """1 ページ(画像)の計測結果。"""

    name: str
    width: int
    height: int
    size_bytes: int
    image_format: str = ""
    dhash: Optional[int] = None

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def bytes_per_pixel(self) -> float:
        return (self.size_bytes / self.pixels) if self.pixels else 0.0


@dataclass
class VolumeItem:
    """重複判定の単位となる「1 巻」。書庫ファイル・画像フォルダ・画像の集合のいずれか。"""

    path: Path                     # 書庫ファイル or フォルダのパス(作業領域内)
    kind: str                      # "zip" / "rar" / "7z" / "folder" / "imageset"
    title: TitleKey
    rel_path: str = ""             # 作業領域ルートからの相対パス(ログ表示用)
    member_files: Sequence[Path] = ()   # kind == "imageset" のときの実ファイル群
    display_name: str = ""         # 一覧表示に使う名前

    # 計測結果(quality.measure_item が埋める)
    page_count: int = 0
    pages: Sequence[PageInfo] = ()
    total_bytes: int = 0
    measure_error: str = ""        # 計測に失敗した理由(空なら成功)
    outlier_logged: bool = False   # サイズ外れ値の警告を出力済みか(同じ警告の重複防止)

    @property
    def measured(self) -> bool:
        return bool(self.pages) and not self.measure_error

    @property
    def median_pixels(self) -> float:
        if not self.pages:
            return 0.0
        return float(median(p.pixels for p in self.pages))

    @property
    def median_bytes_per_pixel(self) -> float:
        if not self.pages:
            return 0.0
        return float(median(p.bytes_per_pixel for p in self.pages))

    @property
    def source_rank(self) -> int:
        return C.SOURCE_RANK.get(self.kind, 0)

    def label(self) -> str:
        return self.display_name or self.rel_path or self.path.name


@dataclass
class ReviewPair:
    """グレーゾーン(類似度が中間)のペア。既定では自動処理しない。"""

    left: VolumeItem
    right: VolumeItem
    similarity: float
    method: str

    def describe(self) -> str:
        return (
            f"{self.left.label()} ⇔ {self.right.label()} "
            f"(類似度 {self.similarity:.2f} / {self.method})"
        )


@dataclass
class DupGroup:
    """同一巻と判定されたアイテムの集合。"""

    key: str
    items: list[VolumeItem] = field(default_factory=list)
    similarity: float = 1.0
    match_notes: list[str] = field(default_factory=list)

    @property
    def is_duplicate(self) -> bool:
        return len(self.items) > 1


@dataclass
class Decision:
    """1 グループの勝敗判定結果。"""

    group: DupGroup
    winner: Optional[VolumeItem] = None
    losers: list[VolumeItem] = field(default_factory=list)
    undecided: list[VolumeItem] = field(default_factory=list)
    reasons: dict = field(default_factory=dict)   # item.label() -> 理由文
    note: str = ""

    @property
    def decided(self) -> bool:
        return self.winner is not None and not self.undecided


@dataclass
class Action:
    """実際に行った(または行う予定の)ファイル操作。"""

    item_label: str
    kind: str            # "move" / "delete" / "zip" / "skip"
    source: str
    destination: str = ""
    result: str = C.RESULT_PLANNED
    detail: str = ""


@dataclass
class JobResult:
    """1 入力ぶんの処理結果。"""

    input_path: Path
    output_path: Optional[Path] = None
    volume_count: int = 0
    group_count: int = 0
    moved_count: int = 0
    deleted_count: int = 0
    pending_count: int = 0
    skipped_count: int = 0
    input_bytes: int = 0
    output_bytes: int = 0
    decisions: list[Decision] = field(default_factory=list)
    reviews: list[ReviewPair] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def summary_line(self) -> str:
        if self.error:
            return f"[失敗] {self.input_path}: {self.error}"
        size_part = ""
        if self.input_bytes and self.output_bytes:
            size_part = (
                f" / {self.input_bytes / 1048576:.1f}MB → {self.output_bytes / 1048576:.1f}MB"
            )
        return (
            f"[完了] {self.input_path.name}: 巻 {self.volume_count} 件 / "
            f"重複グループ {self.group_count} 件 / 退避 {self.moved_count} 件 / "
            f"削除 {self.deleted_count} 件 / 保留 {self.pending_count} 件 / "
            f"スキップ {self.skipped_count} 件{size_part}"
        )
