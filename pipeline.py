"""1 入力(書庫ファイル or フォルダ)= 1 ジョブの処理フロー。

  作業領域の用意 → 巻の同定 → あいまい照合 → 画質判定 → 敗者の退避
  → 名前を統一した巻フォルダとして並べ全体を無圧縮 ZIP でラップ(flatten_output=False なら
    巻ごとに ZIP 化して元の階層のまま全体をラップ) → 後片付け

CLI と GUI はこのモジュールだけを呼ぶ(判定ロジックを各フロントエンドに書かない)。
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import constants as C
from . import discovery, layout, matching, naming, quality, repack, unify
from .archives import ArchiveError, is_archive, open_source
from .logging_setup import get_logger, timestamp
from .models import Action, Decision, JobResult, ReviewPair, VolumeItem

ProgressCallback = Callable[[str, int, int], None]


class JobCancelled(Exception):
    """GUI の中止ボタンで協調的に停止したときに投げる。"""


@dataclass
class JobOptions:
    """1 ジョブの振る舞いを決めるオプション(CLI/GUI 共通)。"""

    dry_run: bool = False
    delete_losers: bool = False
    replace: bool = False
    in_place: bool = False
    rar_to_zip: bool = False
    verify_content: bool = False
    work_dir: Optional[Path] = None
    dup_dir_name: Optional[str] = None  # None なら folder_lang の既定名
    aliases: dict = field(default_factory=dict)
    rar_backend: str = "auto"
    sample_pages: int = C.SAMPLE_PAGES
    quality_margin: float = C.QUALITY_MARGIN
    series_similarity: float = C.SERIES_SIMILARITY_MIN
    series_review: float = C.SERIES_SIMILARITY_REVIEW
    accept_review: bool = False
    approved_pairs: set = field(default_factory=set)
    output_suffix: Optional[str] = None  # None なら folder_lang の既定名
    folder_lang: str = C.FOLDER_LANG_DEFAULT
    keep_work_dir: bool = False
    retry_overflow: bool = True
    flatten_output: bool = True
    wrap_format: str = C.WRAP_FORMAT_DEFAULT
    recovery_percent: int = C.RAR_RECOVERY_PERCENT
    rar_level: int = C.RAR_COMPRESSION_LEVEL

    def __post_init__(self) -> None:
        names = C.FOLDER_NAMES.get(self.folder_lang)
        if names is not None:
            if self.dup_dir_name is None:
                self.dup_dir_name = names["dup"]
            if self.output_suffix is None:
                self.output_suffix = names["suffix"]

    @property
    def names(self) -> dict:
        """出力フォルダ名などの表記(`folder_lang` に対応)。"""

        return C.FOLDER_NAMES[self.folder_lang]

    def validate(self) -> None:
        if self.folder_lang not in C.FOLDER_NAMES:
            raise ValueError(f"--folder-lang は {' / '.join(C.FOLDER_NAMES)} のいずれかを指定してください")
        if self.sample_pages < 1:
            raise ValueError("--sample は 1 以上を指定してください")
        if not 0.0 <= self.quality_margin < 1.0:
            raise ValueError("--quality-margin は 0 以上 1 未満で指定してください")
        if not 0.0 < self.series_similarity <= 1.0:
            raise ValueError("--series-similarity は 0 より大きく 1 以下で指定してください")
        if not 0.0 < self.series_review <= self.series_similarity:
            raise ValueError("--series-review は 0 より大きく --series-similarity 以下で指定してください")
        if self.wrap_format not in (C.WRAP_FORMAT_RAR, C.WRAP_FORMAT_ZIP):
            raise ValueError(f"--wrap-format は {C.WRAP_FORMAT_RAR} か {C.WRAP_FORMAT_ZIP} を指定してください")
        if not 0 <= self.recovery_percent <= C.RAR_RECOVERY_PERCENT_MAX:
            raise ValueError(
                f"--recovery-percent は 0〜{C.RAR_RECOVERY_PERCENT_MAX} で指定してください"
            )
        if not 0 <= self.rar_level <= C.RAR_COMPRESSION_LEVEL_MAX:
            raise ValueError(f"--rar-level は 0〜{C.RAR_COMPRESSION_LEVEL_MAX} で指定してください")
        if self.delete_losers and self.dry_run:
            get_logger().info("--dry-run のため --delete-losers は適用されません")


class Pipeline:
    """1 入力を処理する。インスタンスは 1 ジョブ 1 回だけ使う。"""

    def __init__(
        self,
        options: JobOptions,
        progress: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        options.validate()
        self.options = options
        self.logger = get_logger()
        self._progress = progress
        self._cancel = cancel_event or threading.Event()
        self._work_root: Optional[Path] = None
        self._owns_work_root = False
        self._warnings: list[str] = []
        self._wrap_notes: list[str] = []
        self._flat_dir: Optional[Path] = None
        self._staged_output: Optional[Path] = None
        self._outliers: dict[int, str] = {}
        self._cancelled = False

    # --- 公開 API -----------------------------------------------------------

    def run(self, input_path: Path) -> JobResult:
        input_path = Path(input_path)
        result = JobResult(input_path=input_path)
        try:
            self._validate_input(input_path)
            result.input_bytes = self._input_size(input_path)
            self._prepare_workspace(input_path, result.input_bytes)
            assert self._work_root is not None

            self._expand_range_containers()
            items = self._discover(result)
            self._measure(items)
            groups, reviews, no_volume = self._group(items)
            result.warnings.extend(self._warnings)
            result.reviews = list(reviews)
            result.group_count = sum(1 for group in groups if group.is_duplicate)
            warned = len(self._warnings)
            decisions = self._decide(groups)
            result.warnings.extend(self._warnings[warned:])
            result.decisions = decisions
            result.pending_count = (
                len(no_volume)
                + len(reviews)
                + sum(len(decision.undecided) for decision in decisions)
            )
            result.skipped_count = sum(
                1 for item in items if item.measure_error
            )

            if self.options.dry_run:
                result.actions = layout.dispose_losers(
                    decisions,
                    self._work_root,
                    self.options.dup_dir_name,
                    delete=self.options.delete_losers,
                    dry_run=True,
                )
                self.logger.info("[dry-run] ファイルは変更していません")
                return result

            actions = layout.dispose_losers(
                decisions,
                self._work_root,
                self.options.dup_dir_name,
                delete=self.options.delete_losers,
                dry_run=False,
            )
            result.moved_count = sum(1 for a in actions if a.result == C.RESULT_MOVED)
            result.deleted_count = sum(1 for a in actions if a.result == C.RESULT_DELETED)

            if self.options.flatten_output:
                output_path = self._flatten(input_path, items, decisions, actions)
            else:
                actions.extend(self._normalize_volumes(items, decisions))
                output_path = self._wrap(input_path)
                actions.append(
                    Action(
                        item_label=output_path.name,
                        kind="zip",
                        source=str(self._work_root),
                        destination=str(output_path),
                        result=C.RESULT_KEPT,
                        detail="全体を無圧縮 ZIP でラップ",
                    )
                )
            result.warnings.extend(self._wrap_notes)
            result.output_path = output_path
            result.output_bytes = self._input_size(output_path)
            result.actions = actions
            if self.options.replace:
                self._replace_original(input_path, output_path, result)
            return result
        except JobCancelled:
            self._cancelled = True
            result.error = "ユーザー操作により中止"
            self.logger.warning("中止しました: %s", input_path)
            return result
        except (ValueError, OSError, ArchiveError, repack.RepackError) as exc:
            result.error = str(exc)
            self.logger.error("処理に失敗: %s (%s)", input_path, exc)
            return result
        except Exception as exc:  # 想定外も握りつぶさずログに残す
            result.error = f"想定外のエラー: {exc}"
            self.logger.exception("想定外のエラー: %s", input_path)
            return result
        finally:
            # 失敗時は調査用に作業領域を残すが、ユーザーの中止では残さない(巨大な作業領域の放置防止)
            self._cleanup(
                keep=(bool(result.error) and not self._cancelled) or self.options.keep_work_dir
            )

    # --- 各ステップ ---------------------------------------------------------

    def _validate_input(self, input_path: Path) -> None:
        if not input_path.exists():
            raise ValueError(f"入力が存在しない: {input_path}")
        if input_path.is_file() and not is_archive(input_path):
            raise ValueError(
                f"対応していない入力形式(zip/cbz/rar/cbr/7z かフォルダを指定): {input_path}"
            )
        if input_path.is_dir() and input_path.name.startswith(C.WORK_DIR_PREFIX):
            raise ValueError(f"作業用フォルダは入力にできない: {input_path}")

    def _input_size(self, input_path: Path) -> int:
        """ファイルならそのサイズ、フォルダなら配下の合計サイズ。"""

        if input_path.is_file():
            return input_path.stat().st_size
        return sum(path.stat().st_size for path in input_path.rglob("*") if path.is_file())

    def _prepare_workspace(self, input_path: Path, input_bytes: int) -> None:
        self._notify("作業領域の準備", 0, 1)
        if self.options.in_place and input_path.is_dir():
            self._work_root = input_path
            self._owns_work_root = False
            self.logger.info("--in-place: 入力フォルダを直接操作します: %s", input_path)
            return

        base = self._choose_work_base(input_path, input_bytes)
        base.mkdir(parents=True, exist_ok=True)
        self._check_free_space(base, input_bytes)
        self._work_root = Path(
            tempfile.mkdtemp(prefix=f"{C.WORK_DIR_PREFIX}{timestamp()}_", dir=str(base))
        )
        self._owns_work_root = True
        self.logger.info("作業領域: %s", self._work_root)

        if input_path.is_dir():
            self.logger.info("入力フォルダを作業領域へコピー中(原本は変更しません)")
            shutil.copytree(input_path, self._work_root, dirs_exist_ok=True)
        else:
            self.logger.info("入力書庫を作業領域へ展開中(内部の書庫はファイルのまま保持)")
            with open_source(input_path, rar_backend=self.options.rar_backend) as source:
                source.extract_all(self._work_root)
        self._notify("作業領域の準備", 1, 1)

    def _choose_work_base(self, input_path: Path, input_bytes: int) -> Path:
        """作業領域の親フォルダを決める。

        明示指定 > RAM ディスク(収まる場合) > ローカルの一時フォルダ(収まる場合) > 入力と同じ場所。
        入力が NAS/クラウド同期フォルダにあっても、展開・ZIP 化の I/O をそこへ流さないための順序。
        """

        if self.options.work_dir is not None:
            return self.options.work_dir
        required = int(input_bytes * C.WORKSPACE_SIZE_FACTOR)
        for ram_dir in C.RAM_DISK_CANDIDATES:
            candidate = Path(ram_dir)
            if candidate.is_dir() and required <= self._free_bytes(candidate) * C.RAM_WORKSPACE_MAX_FREE_RATIO:
                self.logger.info("作業領域をメモリ上(%s)に作ります", candidate)
                return candidate
        local_tmp = Path(tempfile.gettempdir())
        if local_tmp.is_dir() and required <= self._free_bytes(local_tmp):
            self.logger.info("作業領域をローカルの一時フォルダ(%s)に作ります", local_tmp)
            return local_tmp
        self.logger.info("一時フォルダの空きが足りないため、入力と同じ場所に作業領域を作ります")
        return input_path.parent

    @staticmethod
    def _free_bytes(path: Path) -> int:
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return 0

    def _check_free_space(self, base: Path, input_bytes: int) -> None:
        required = int(input_bytes * C.WORKSPACE_SIZE_FACTOR)
        free = shutil.disk_usage(base).free
        if free < required:
            raise OSError(
                f"空き容量が不足(必要 {required / 1048576:.0f}MB / 空き {free / 1048576:.0f}MB): {base}"
            )

    def _expand_range_containers(self) -> None:
        """`v01-05` のような範囲表記の書庫は複数巻の集合なので、展開して中身を個別の巻として扱う。

        展開後の中にさらに範囲表記の書庫があれば繰り返す(入れ子)。展開に失敗した書庫はそのまま残す。
        """

        assert self._work_root is not None
        if self.options.in_place and self.options.dry_run:
            return  # 入力フォルダを直接触る設定で dry-run のときは何も変更しない
        failed: set[Path] = set()
        for _ in range(C.MAX_RANGE_EXPAND_DEPTH):
            containers = [
                path
                for path in self._work_root.rglob("*")
                if path.is_file()
                and path not in failed
                and is_archive(path)
                and self._is_range_title(path.name)
                and self.options.dup_dir_name not in path.relative_to(self._work_root).parts
            ]
            if not containers:
                return
            for archive in containers:
                self._raise_if_cancelled()
                destination = layout.unique_destination(archive.parent / archive.stem)
                try:
                    with open_source(archive, rar_backend=self.options.rar_backend) as source:
                        source.extract_all(destination)
                except (ArchiveError, OSError) as exc:
                    failed.add(archive)
                    shutil.rmtree(destination, ignore_errors=True)
                    self.logger.warning("範囲表記の書庫を展開できないため 1 巻として扱う: %s (%s)", archive.name, exc)
                    continue
                archive.unlink()
                self.logger.info("範囲表記の書庫を展開して中身を判定: %s", archive.name)

    @staticmethod
    def _is_range_title(name: str) -> bool:
        key = naming.parse_title(name)
        return key.has_volume and key.volume_kind == C.VOLUME_KIND_RANGE

    def _discover(self, result: JobResult) -> list[VolumeItem]:
        assert self._work_root is not None
        self._notify("巻の同定", 0, 1)
        items = discovery.discover_volumes(self._work_root, self.options.dup_dir_name)
        result.volume_count = len(items)
        self.logger.info("巻を %d 件検出", len(items))
        for item in items:
            self.logger.debug("検出: %s → %s", item.label(), item.title.describe())
        if not items:
            result.warnings.append("巻として扱えるフォルダ/書庫が見つかりませんでした")
        self._notify("巻の同定", 1, 1)
        return items

    def _measure(self, items: Sequence[VolumeItem]) -> None:
        total = len(items)
        for index, item in enumerate(items, start=1):
            self._raise_if_cancelled()
            self._notify("画質の計測", index, total)
            quality.measure_item(
                item,
                sample_limit=self.options.sample_pages,
                want_hash=self.options.verify_content,
                rar_backend=self.options.rar_backend,
            )

    def _group(
        self, items: Sequence[VolumeItem]
    ) -> tuple[list, list[ReviewPair], list[VolumeItem]]:
        self._notify("あいまい照合", 0, 1)
        groups, reviews, no_volume = matching.group_items(
            items,
            aliases=self.options.aliases,
            auto_threshold=self.options.series_similarity,
            review_threshold=self.options.series_review,
            accept_review=self.options.accept_review,
            approved_pairs=self.options.approved_pairs,
        )
        if self.options.retry_overflow:
            groups, reviews, retry_notes = matching.retry_overflowing_scopes(
                groups, reviews, aliases=self.options.aliases
            )
            self._warnings.extend(retry_notes)
        duplicates = [group for group in groups if group.is_duplicate]
        self.logger.info(
            "重複グループ %d 件 / 保留ペア %d 件 / 巻数不明 %d 件",
            len(duplicates),
            len(reviews),
            len(no_volume),
        )
        self._notify("あいまい照合", 1, 1)
        return groups, reviews, no_volume

    def _decide(self, groups: Sequence) -> list[Decision]:
        decisions: list[Decision] = []
        duplicates = [group for group in groups if group.is_duplicate]
        all_items = [item for group in groups for item in group.items]
        outliers = quality.find_size_outliers(all_items, matching.scope_key)
        self._outliers = outliers
        for group in groups:
            if not group.is_duplicate:
                for item in group.items:
                    if id(item) in outliers:
                        where = (
                            f"出力では {self.options.names["excluded"]}/ に入れます"
                            if self.options.flatten_output
                            else "そのまま残します"
                        )
                        self._warnings.append(
                            f"{item.label()}: {outliers[id(item)]}(重複が無く、{where}。欠けや別物でないか確認してください)"
                        )
        for index, group in enumerate(duplicates, start=1):
            self._raise_if_cancelled()
            self._notify("勝敗の判定", index, len(duplicates))
            decision = quality.select_winner(
                group,
                margin=self.options.quality_margin,
                verify_content=self.options.verify_content,
                outliers=outliers,
            )
            decisions.append(decision)
        return decisions

    def _normalize_volumes(
        self, items: Sequence[VolumeItem], decisions: Sequence[Decision]
    ) -> list[Action]:
        """フォルダ形式の巻を圧縮 ZIP にし、無圧縮の巻 ZIP は圧縮し直し、必要なら rar も ZIP に作り直す。"""

        actions: list[Action] = []
        targets = [item for item in items if item.path.exists()]
        for index, item in enumerate(targets, start=1):
            self._raise_if_cancelled()
            self._notify("巻ごとの ZIP 化", index, len(targets))
            if item.kind in ("folder", "imageset"):
                actions.append(self._zip_folder_volume(item))
            elif item.kind == "rar" and self.options.rar_to_zip:
                actions.append(self._convert_archive_to_zip(item))
            elif item.kind == "zip" and repack.is_stored_zip(item.path):
                actions.append(self._compress_stored_zip(item))
        return actions

    def _compress_stored_zip(self, item: VolumeItem) -> Action:
        """無圧縮の巻 ZIP を圧縮 ZIP に作り直す(失敗したら元の ZIP のまま)。"""

        action = Action(
            item_label=item.label(),
            kind="zip",
            source=str(item.path),
            detail="無圧縮 ZIP を圧縮 ZIP に作り直し",
        )
        staging = item.path.parent / f"{C.WORK_DIR_PREFIX}{item.path.stem}"

        def extract(zip_path: Path, dest: Path) -> None:
            with open_source(zip_path, rar_backend=self.options.rar_backend) as source:
                source.extract_all(dest)

        try:
            repack.recompress_zip(item.path, staging, extract)
            action.destination = str(item.path)
            action.result = C.RESULT_KEPT
            self.logger.info("無圧縮 ZIP を圧縮: %s", item.path.name)
        except (ArchiveError, repack.RepackError, OSError) as exc:
            action.result = C.RESULT_SKIPPED
            action.detail = f"圧縮に失敗したため元の ZIP のまま: {exc}"
            self.logger.warning("ZIP の圧縮に失敗: %s (%s)", item.label(), exc)
        return action

    def _zip_folder_volume(self, item: VolumeItem) -> Action:
        action = Action(
            item_label=item.label(),
            kind="zip",
            source=str(item.path),
            detail="フォルダを圧縮 ZIP 化",
        )
        try:
            if item.kind == "imageset" and item.path.is_dir():
                # 退避前の imageset(同じフォルダに複数巻が混在)は ZIP 化しない
                files = [path for path in item.member_files if path.exists()]
                if len(files) != len(list(item.path.glob("*"))):
                    action.result = C.RESULT_SKIPPED
                    action.detail = "同一フォルダに複数巻が混在するため ZIP 化しない"
                    return action
            out_zip = repack.zip_volume_folder(item.path)
            item.path = out_zip
            item.kind = "zip"
            action.destination = str(out_zip)
            action.result = C.RESULT_KEPT
            self.logger.info("巻を ZIP 化: %s", out_zip.name)
        except repack.RepackError as exc:
            action.result = C.RESULT_SKIPPED
            action.detail = f"ZIP 化に失敗: {exc}"
            self.logger.error("ZIP 化に失敗: %s (%s)", item.label(), exc)
        return action

    def _convert_archive_to_zip(self, item: VolumeItem) -> Action:
        action = Action(
            item_label=item.label(),
            kind="zip",
            source=str(item.path),
            detail="rar を圧縮 ZIP へ変換",
        )
        staging = item.path.parent / f"{C.WORK_DIR_PREFIX}{item.path.stem}"
        temporary_zip = item.path.parent / f"{C.WORK_DIR_PREFIX}{item.path.stem}.zip"
        try:
            source_path = item.path
            with open_source(source_path, rar_backend=self.options.rar_backend) as source:
                source.extract_all(staging)
            # 先に一時名で作って検証し、成功してから元を消して正式名へ付け替える
            repack.create_stored_zip(staging, temporary_zip, compression=C.VOLUME_ZIP_COMPRESSION)
            source_path.unlink()
            out_zip = source_path.with_suffix(".zip")
            if out_zip.exists():
                out_zip = layout.unique_destination(out_zip)
            temporary_zip.rename(out_zip)
            item.path = out_zip
            item.kind = "zip"
            action.destination = str(out_zip)
            action.result = C.RESULT_KEPT
        except (ArchiveError, repack.RepackError, OSError) as exc:
            action.result = C.RESULT_SKIPPED
            action.detail = f"変換に失敗したため rar のまま: {exc}"
            self.logger.warning("rar → zip 変換に失敗: %s (%s)", item.label(), exc)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if temporary_zip.exists():
                temporary_zip.unlink()
        return action

    def _wrap(self, input_path: Path) -> Path:
        assert self._work_root is not None
        self._notify("全体の ZIP 化", 0, 1)
        out_path = self._write_wrapper(self._work_root, input_path)
        self._notify("全体の ZIP 化", 1, 1)
        return out_path

    def _write_wrapper(self, source_dir: Path, input_path: Path) -> Path:
        """source_dir 全体をラップ書庫(既定 RAR=圧縮+リカバリーレコード、不可なら無圧縮 ZIP)にする。

        書庫はまずローカルの作業フォルダ側に作って検証し、できあがりを出力先へ 1 回だけ移す。
        出力先が NAS のとき、圧縮・リカバリーレコード付加・検証でネットワークを往復しないため。
        """

        stem = input_path.stem if input_path.is_file() else input_path.name
        base = input_path.parent / f"{stem}{self.options.output_suffix}"
        staging_base = source_dir.parent / f"{C.WORK_DIR_PREFIX}out_{source_dir.name}"

        built: Optional[Path] = None
        suffix = ".zip"
        if self.options.wrap_format == C.WRAP_FORMAT_RAR:
            candidate = staging_base.with_suffix(".rar")
            self._staged_output = candidate
            self._notify("RAR の作成(圧縮・リカバリーレコード・検証)", 0, 1)
            started = time.monotonic()
            try:
                repack.create_rar(
                    source_dir, candidate, self.options.recovery_percent, self.options.rar_level
                )
                built, suffix = candidate, ".rar"
                self.logger.info(
                    "RAR を作成(圧縮レベル %d / リカバリーレコード %d%% / %.0f 秒)",
                    self.options.rar_level,
                    self.options.recovery_percent,
                    time.monotonic() - started,
                )
            except repack.RepackError as exc:
                note = f"RAR で出力できないため ZIP にしました: {exc}"
                self._wrap_notes.append(note)
                self.logger.warning(note)
        if built is None:
            built = staging_base.with_suffix(".zip")
            self._staged_output = built
            self._notify("ZIP の作成", 0, 1)
            repack.zip_work_tree(source_dir, built)

        final = layout.unique_destination(base.with_name(base.name + suffix))
        self._notify("出力先へ移動", 0, 1)
        started = time.monotonic()
        shutil.move(str(built), str(final))
        self._staged_output = None
        self.logger.info("出力: %s (移動 %.0f 秒)", final, time.monotonic() - started)
        return final

    def _replace_original(self, input_path: Path, output_path: Path, result: JobResult) -> None:
        """--replace: 検証済みの出力で原本を置き換える。"""

        try:
            if input_path.is_dir():
                shutil.rmtree(input_path)
                final = input_path.parent / f"{input_path.name}{output_path.suffix}"
            else:
                input_path.unlink()
                final = input_path.with_suffix(output_path.suffix)
            if final.exists():
                final = layout.unique_destination(final)
            output_path.rename(final)
            result.output_path = final
            self.logger.info("--replace: 原本を置き換えました → %s", final)
        except OSError as exc:
            result.warnings.append(f"原本の置き換えに失敗(出力はそのまま残しています): {exc}")
            self.logger.exception("原本の置き換えに失敗: %s", input_path)

    def _flatten(
        self,
        input_path: Path,
        items: Sequence[VolumeItem],
        decisions: Sequence[Decision],
        actions: list,
    ) -> Path:
        """残した巻を統一名の巻フォルダ(画像が直下)として並べ、全体を 1 つの無圧縮 ZIP にして出力する。
        巻ごとの ZIP は作らない(書庫は展開、巻フォルダ内の余計な入れ子は解消)。

        敗者は `_重複/`、巻として扱えなかったファイルは `_その他/` に、それぞれ階層なしで退避する
        (--replace で原本を消しても取りこぼしが出ないようにするため)。
        """

        assert self._work_root is not None
        self._notify("名前の統一と平坦化", 0, 1)
        stem = input_path.stem if input_path.is_file() else input_path.name
        out_dir = self._work_root.parent / f"{C.WORK_DIR_PREFIX}flat_{self._work_root.name}"
        out_dir.mkdir(parents=True)
        self._flat_dir = out_dir

        survivors = self._survivors(items)
        plan = unify.plan_names(
            survivors,
            linked_groups=[decision.group.items for decision in decisions],
            fallback_series=naming.display_series(stem),
            threshold=self.options.series_similarity,
            aliases=self.options.aliases,
            folder_lang=self.options.folder_lang,
        )
        undecided_ids = {id(item) for decision in decisions for item in decision.undecided}
        used_names: set[str] = set()
        for index, item in enumerate(survivors, start=1):
            self._raise_if_cancelled()
            self._notify("巻フォルダの作成", index, len(survivors))
            if id(item) in self._outliers:
                reason = f"サイズ外れ値のため除外: {self._outliers[id(item)]}"
                actions.append(self._place_aside(item, out_dir / self.options.names["excluded"], reason))
                continue
            stem_name = plan.get(id(item))
            if id(item) in undecided_ids:
                reason = "同じ巻の重複だが計測できず勝敗が付かなかったため保留"
                actions.append(self._place_aside(item, out_dir / self.options.names["pending"], reason))
                continue
            if stem_name and stem_name.casefold() in used_names:
                reason = f"統一名「{stem_name}」が他の巻と衝突(同じ巻の未判定の重複)のため保留"
                actions.append(self._place_aside(item, out_dir / self.options.names["pending"], reason))
                continue
            if stem_name:
                used_names.add(stem_name.casefold())
            actions.append(self._place_volume(item, out_dir, stem_name))

        self._flatten_directory(self._work_root / self.options.dup_dir_name, out_dir / self.options.dup_dir_name)
        self._flatten_leftovers(self._work_root, out_dir / self.options.names["other"])
        try:
            out_path = self._write_wrapper(out_dir, input_path)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
        self._notify("名前の統一と平坦化", 1, 1)
        return out_path

    def _survivors(self, items: Sequence[VolumeItem]) -> list[VolumeItem]:
        """残る巻(退避・削除されていないもの)。同じ実体を指す重複は 1 件にまとめる。"""

        assert self._work_root is not None
        dup_root = self._work_root / self.options.dup_dir_name
        seen: set = set()
        result: list[VolumeItem] = []
        for item in items:
            if item.kind == "imageset":
                members = tuple(path for path in item.member_files if path.exists())
                if not members or dup_root in members[0].parents:
                    continue
                key: object = members
            else:
                if not item.path.exists() or dup_root in item.path.parents:
                    continue
                key = item.path
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def _place_volume(self, item: VolumeItem, out_dir: Path, stem_name: Optional[str]) -> Action:
        """1 巻を出力先に `統一名/`(画像が直下のフォルダ)として置く。書庫は展開する。

        統一名が無い(巻数不明)ときは元の名前を保つ。書庫を展開できないときは、
        書庫のまま統一名+元の拡張子で置く(原本の中身を失わない)。
        """

        action = Action(
            item_label=item.label(),
            kind="rename",
            source=item.rel_path or item.path.name,
            result=C.RESULT_KEPT,
            detail="統一名" if stem_name else "巻数不明のため元の名前を維持",
        )
        folder_name = stem_name or (
            unify.sanitize_filename(naming.strip_extension(item.path.name)) or item.path.name
        )
        destination = layout.unique_destination(out_dir / folder_name)
        try:
            if item.kind == "imageset":
                destination.mkdir(parents=True)
                for member in item.member_files:
                    if member.exists():
                        shutil.move(str(member), str(destination / member.name))
            elif item.path.is_dir():
                shutil.move(str(item.path), str(destination))
            else:
                try:
                    with open_source(item.path, rar_backend=self.options.rar_backend) as source:
                        source.extract_all(destination)
                    item.path.unlink()  # 展開済み(作業領域内の複製なので原本は残る)
                except (ArchiveError, OSError) as exc:
                    shutil.rmtree(destination, ignore_errors=True)
                    kept = layout.unique_destination(
                        out_dir / f"{folder_name}{unify.unified_extension(item.path)}"
                    )
                    shutil.move(str(item.path), str(kept))
                    destination = kept
                    action.detail = f"展開できないため書庫のまま配置: {exc}"
                    action.result = C.RESULT_SKIPPED
                    self.logger.warning("書庫を展開できない: %s (%s)", item.label(), exc)
            if destination.is_dir():
                self._hoist_single_child_directories(destination)
        except OSError as exc:
            action.result = C.RESULT_SKIPPED
            action.detail = f"配置に失敗: {exc}"
            self.logger.error("巻の配置に失敗: %s (%s)", item.label(), exc)
            return action
        action.destination = str(destination)
        item.path = destination
        return action

    def _place_aside(self, item: VolumeItem, aside_dir: Path, reason: str) -> Action:
        """巻としては並べず、原本の名前のまま除外/保留フォルダへ移す(中身は展開しない)。"""

        action = Action(
            item_label=item.label(),
            kind="move",
            source=item.rel_path or item.path.name,
            result=C.RESULT_MOVED,
            detail=reason,
        )
        try:
            aside_dir.mkdir(parents=True, exist_ok=True)
            if item.kind == "imageset":
                destination = layout.unique_destination(aside_dir / self._safe_name(item.path))
                destination.mkdir(parents=True)
                for member in item.member_files:
                    if member.exists():
                        shutil.move(str(member), str(destination / member.name))
            else:
                destination = layout.unique_destination(aside_dir / item.path.name)
                shutil.move(str(item.path), str(destination))
            action.destination = str(destination)
            item.path = destination
            self.logger.warning("%s に退避: %s (%s)", aside_dir.name, item.label(), reason)
        except OSError as exc:
            action.result = C.RESULT_SKIPPED
            action.detail = f"{aside_dir.name} への移動に失敗: {exc}"
            self.logger.error("退避に失敗: %s (%s)", item.label(), exc)
        return action

    @staticmethod
    def _hoist_single_child_directories(folder: Path) -> None:
        """フォルダの中身が「サブフォルダ 1 つだけ」なら、その中身を 1 段上へ引き上げる(入れ子の解消)。"""

        for _ in range(C.MAX_HOIST_DEPTH):
            children = list(folder.iterdir())
            if len(children) != 1 or not children[0].is_dir():
                return
            inner = children[0]
            holder = folder / f"{C.WORK_DIR_PREFIX}hoist"
            inner.rename(holder)
            for child in holder.iterdir():
                shutil.move(str(child), str(folder / child.name))
            holder.rmdir()

    @staticmethod
    def _safe_name(path: Path) -> str:
        return unify.sanitize_filename(path.name) or path.name

    def _flatten_directory(self, source: Path, destination: Path) -> None:
        """source 配下の書庫・巻フォルダを階層なしで destination へ移す。"""

        if not source.is_dir():
            return
        destination.mkdir(parents=True, exist_ok=True)
        for child in sorted(source.iterdir()):
            if child.is_dir() and not any(p.is_file() for p in child.iterdir()):
                self._flatten_directory(child, destination)   # 画像を直下に持たない中間フォルダ
                continue
            shutil.move(str(child), str(layout.unique_destination(destination / child.name)))

    def _flatten_leftovers(self, work_root: Path, destination: Path) -> None:
        """巻として出力されなかったファイル(表紙画像・テキスト等)を `_その他/` へ集める。"""

        leftovers = [path for path in sorted(work_root.rglob("*")) if path.is_file()]
        if not leftovers:
            return
        destination.mkdir(parents=True, exist_ok=True)
        for path in leftovers:
            shutil.move(str(path), str(layout.unique_destination(destination / path.name)))
        self.logger.info("巻として扱えなかった %d 件を %s へ集めました", len(leftovers), destination.name)

    def _cleanup(self, keep: bool) -> None:
        # 出力用の一時フォルダ(平坦出力の組み立て先)は、失敗時も含め常に消す(作業領域より大きくなり得るため)
        if self._flat_dir is not None and not self.options.keep_work_dir:
            shutil.rmtree(self._flat_dir, ignore_errors=True)
        if self._staged_output is not None:
            self._staged_output.unlink(missing_ok=True)  # 出力先へ移す前に失敗・中止した書庫
        if self._work_root is None or not self._owns_work_root:
            return
        if keep:
            self.logger.warning("作業領域を残しました: %s", self._work_root)
            return
        shutil.rmtree(self._work_root, ignore_errors=True)
        self.logger.debug("作業領域を削除: %s", self._work_root)

    # --- 補助 ---------------------------------------------------------------

    def _notify(self, stage: str, current: int, total: int) -> None:
        if self._progress is not None:
            self._progress(stage, current, total)

    def _raise_if_cancelled(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled()


def build_csv_rows(result: JobResult) -> list[list[object]]:
    """判定根拠 CSV の行を作る(--log 指定時のみ書き出される)。"""

    rows: list[list[object]] = []
    action_by_label = {action.item_label: action for action in result.actions}

    for decision in result.decisions:
        group_key = decision.group.key
        members = [(C.ROLE_WINNER, decision.winner)] if decision.winner else []
        members += [(C.ROLE_LOSER, item) for item in decision.losers]
        members += [(C.ROLE_PENDING, item) for item in decision.undecided]
        for role, item in members:
            if item is None:
                continue
            action = action_by_label.get(item.label())
            rows.append(
                [
                    group_key,
                    role,
                    item.rel_path or str(item.path),
                    item.kind,
                    item.page_count,
                    f"{item.median_pixels:.0f}",
                    f"{item.median_bytes_per_pixel:.4f}",
                    item.total_bytes,
                    f"{decision.group.similarity:.2f}",
                    decision.reasons.get(item.label(), decision.note),
                    action.result if action else C.RESULT_KEPT,
                ]
            )

    for review in result.reviews:
        for item in (review.left, review.right):
            rows.append(
                [
                    "review",
                    C.ROLE_PENDING,
                    item.rel_path or str(item.path),
                    item.kind,
                    item.page_count,
                    f"{item.median_pixels:.0f}",
                    f"{item.median_bytes_per_pixel:.4f}",
                    item.total_bytes,
                    f"{review.similarity:.2f}",
                    f"グレーゾーン({review.method}) 相手: {review.right.label() if item is review.left else review.left.label()}",
                    C.RESULT_SKIPPED,
                ]
            )
    return rows
