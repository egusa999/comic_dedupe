"""コマンドラインインターフェース。

例:
    python -m comic_dedupe.cli "D:\\comics\\作品名.zip" --log
    python -m comic_dedupe.cli "D:\\comics\\作品名" --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import constants as C
from . import external_tools, matching, pipeline
from .logging_setup import default_log_dir, resolve_log_paths, setup_logger, timestamp, write_csv

_LOG_SENTINEL = "__INPUT_DIR__"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="comic_dedupe",
        description=(
            "書庫/フォルダの中にある同じ巻の重複を、タイトルのあいまい一致で検出し、"
            "高画質な方を残して他を重複フォルダへ退避し、無圧縮 ZIP にまとめ直します。"
        ),
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="処理対象の書庫ファイル(zip/cbz/rar/cbr/7z)またはフォルダ(複数指定可)",
    )

    output = parser.add_argument_group("出力・ログ")
    output.add_argument(
        "--log",
        nargs="?",
        const=_LOG_SENTINEL,
        default=None,
        metavar="DIR",
        help="ログ(.log)と判定根拠(.csv)を出力する。DIR 省略時はアプリ本体フォルダ内の logs/",
    )
    output.add_argument("--verbose", action="store_true", help="詳細ログ(各ページの計測値まで)")
    output.add_argument(
        "--output-suffix",
        default=None,
        help=f"出力のサフィックス(既定: {C.OUTPUT_SUFFIX}、--folder-lang en なら {C.FOLDER_NAMES['en']['suffix']})",
    )

    behaviour = parser.add_argument_group("動作")
    behaviour.add_argument(
        "--dry-run", action="store_true", help="判定だけ行い、ファイルを一切変更しない"
    )
    behaviour.add_argument(
        "--delete-losers",
        action="store_true",
        help="負けた巻を重複フォルダへ移動せず削除する(復元不可)",
    )
    behaviour.add_argument(
        "--replace",
        action="store_true",
        help="検証成功後に原本を出力で置き換える(既定は原本を残す)",
    )
    behaviour.add_argument(
        "--wrap-format",
        choices=(C.WRAP_FORMAT_RAR, C.WRAP_FORMAT_ZIP),
        default=C.WRAP_FORMAT_DEFAULT,
        help="全体を包む書庫の形式(既定 rar=圧縮+リカバリーレコード。要 WinRAR の Rar.exe。"
        "無い場合は警告して無圧縮 zip で出力)",
    )
    behaviour.add_argument(
        "--rar-exe",
        type=Path,
        default=None,
        metavar="PATH",
        help="RAR 作成に使う Rar.exe のパス(自動検出できないとき用。環境変数 "
        f"{C.RAR_WRITER_ENV_VAR} でも指定可)",
    )
    behaviour.add_argument(
        "--rar-level",
        type=int,
        default=C.RAR_COMPRESSION_LEVEL,
        metavar="N",
        help=f"RAR の圧縮レベル(0=無圧縮 〜 {C.RAR_COMPRESSION_LEVEL_MAX}=最大、既定 {C.RAR_COMPRESSION_LEVEL})",
    )
    behaviour.add_argument(
        "--recovery-percent",
        type=int,
        default=C.RAR_RECOVERY_PERCENT,
        metavar="N",
        help=f"RAR のリカバリーレコード割合(%%、0 で付加しない、既定 {C.RAR_RECOVERY_PERCENT})",
    )
    behaviour.add_argument(
        "--wrap-zip",
        action="store_true",
        help="従来の出力形式: 名前を統一せず、元の階層のまま全体を無圧縮 ZIP でラップする"
        "(既定は統一名の巻 ZIP を平坦に並べて全体を無圧縮 ZIP でラップ)",
    )
    behaviour.add_argument(
        "--in-place",
        action="store_true",
        help="フォルダ入力を作業領域へコピーせず直接操作する(高速だが原本を変更)",
    )
    behaviour.add_argument(
        "--rar-to-zip", action="store_true", help="残した rar も圧縮 ZIP に作り直す(--wrap-zip 時のみ有効)"
    )
    behaviour.add_argument(
        "--verify-content",
        action="store_true",
        help="同一巻と判定した組の内容(ページの dHash)も照合し、一致率が低ければ保留にする",
    )
    behaviour.add_argument(
        "--keep-work-dir", action="store_true", help="作業領域を削除せず残す(調査用)"
    )

    tuning = parser.add_argument_group("しきい値")
    tuning.add_argument(
        "--sample", type=int, default=C.SAMPLE_PAGES, help=f"画質計測に使うページ数(既定 {C.SAMPLE_PAGES})"
    )
    tuning.add_argument(
        "--quality-margin",
        type=float,
        default=C.QUALITY_MARGIN,
        help=f"この相対差以内は同等とみなす(既定 {C.QUALITY_MARGIN})",
    )
    tuning.add_argument(
        "--series-similarity",
        type=float,
        default=C.SERIES_SIMILARITY_MIN,
        help=f"自動で同一作品と判定する類似度(既定 {C.SERIES_SIMILARITY_MIN})",
    )
    tuning.add_argument(
        "--series-review",
        type=float,
        default=C.SERIES_SIMILARITY_REVIEW,
        help=f"保留(要確認)にする類似度の下限(既定 {C.SERIES_SIMILARITY_REVIEW})",
    )
    tuning.add_argument(
        "--accept-review",
        action="store_true",
        help="グレーゾーン(保留)のペアも自動で同一巻として処理する",
    )

    tuning.add_argument(
        "--no-retry-overflow",
        action="store_true",
        help="残り件数が推定最大巻数を超えたときの再判定(作品名を無視して巻数一致で統合)を無効にする",
    )

    paths = parser.add_argument_group("パス・バックエンド")
    paths.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="作業領域を作る親フォルダ(既定: RAMディスク → ローカル一時フォルダ → 入力と同じ場所の順)",
    )
    paths.add_argument(
        "--dup-dir-name",
        default=None,
        help=f"負けた巻をまとめるフォルダ名(既定: {C.DUP_DIR_NAME}、--folder-lang en なら {C.FOLDER_NAMES['en']['dup']})",
    )
    paths.add_argument(
        "--folder-lang",
        choices=sorted(C.FOLDER_NAMES),
        default=C.FOLDER_LANG_DEFAULT,
        help="出力のフォルダ名・巻名の言語(ja: _重複 / 第NN巻、en: _duplicates / Vol NN。既定 ja)",
    )
    paths.add_argument(
        "--alias-file", type=Path, default=None, help='作品名エイリアス JSON({"代表名": ["別表記"]})'
    )
    paths.add_argument(
        "--rar-backend",
        choices=("auto", C.BACKEND_LIBARCHIVE, C.BACKEND_BSDTAR, C.BACKEND_RARFILE),
        default="auto",
        help="RAR を読むバックエンドを固定する(既定 auto = 使えるものを自動選択)",
    )
    return parser


def _log_directory(log_option: Optional[str]) -> Optional[Path]:
    if log_option is None:
        return None
    if log_option == _LOG_SENTINEL:
        return default_log_dir()
    return Path(log_option)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    stamp = timestamp()
    log_dir = _log_directory(args.log)
    log_path = csv_path = None
    if log_dir is not None:
        log_path, csv_path = resolve_log_paths(log_dir, stamp)

    logger = setup_logger(log_path=log_path, verbose=args.verbose)
    logger.info("comic_dedupe 開始 / %s", external_tools.describe_backends())

    if args.rar_exe is not None:
        os.environ[C.RAR_WRITER_ENV_VAR] = str(args.rar_exe)
        external_tools.find_rar_writer.cache_clear()

    try:
        aliases = matching.load_aliases(args.alias_file)
        options = pipeline.JobOptions(
            dry_run=args.dry_run,
            delete_losers=args.delete_losers,
            replace=args.replace,
            in_place=args.in_place,
            rar_to_zip=args.rar_to_zip,
            verify_content=args.verify_content,
            work_dir=args.work_dir,
            dup_dir_name=args.dup_dir_name,
            folder_lang=args.folder_lang,
            aliases=aliases,
            rar_backend=args.rar_backend,
            sample_pages=args.sample,
            quality_margin=args.quality_margin,
            series_similarity=args.series_similarity,
            series_review=args.series_review,
            accept_review=args.accept_review,
            output_suffix=args.output_suffix,
            keep_work_dir=args.keep_work_dir,
            retry_overflow=not args.no_retry_overflow,
            flatten_output=not args.wrap_zip,
            wrap_format=args.wrap_format,
            recovery_percent=args.recovery_percent,
            rar_level=args.rar_level,
        )
    except ValueError as exc:
        logger.error("オプションが不正: %s", exc)
        return C.EXIT_INPUT_ERROR

    results = []
    for input_path in args.inputs:
        logger.info("=" * 60)
        logger.info("対象: %s", input_path)
        job = pipeline.Pipeline(options)
        results.append(job.run(Path(input_path)))

    _report(results, logger)

    if csv_path is not None:
        rows: list[list[object]] = []
        for result in results:
            rows.extend(pipeline.build_csv_rows(result))
        write_csv(csv_path, rows)
        logger.info("判定根拠 CSV: %s", csv_path)

    if any(not result.ok for result in results):
        return C.EXIT_INPUT_ERROR
    if any(result.pending_count or result.skipped_count for result in results):
        return C.EXIT_PARTIAL
    return C.EXIT_OK


def _report(results: Sequence, logger) -> None:
    logger.info("=" * 60)
    for result in results:
        logger.info(result.summary_line())
        for review in result.reviews:
            logger.info("  保留(要確認): %s", review.describe())
        for warning in result.warnings:
            logger.warning("  %s", warning)
        if result.output_path:
            logger.info("  出力: %s", result.output_path)


if __name__ == "__main__":
    sys.exit(main())
