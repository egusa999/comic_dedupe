"""ロガーの構築。

既定ではコンソールにしか出さない。`--log` が指定されたときだけファイルハンドラを足す
(ユーザー要件: ログファイルは log オプションがある場合のみ出力)。
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Iterable, Optional

from . import constants as C


class QueueHandler(logging.Handler):
    """GUI 側のログペインへ流すためのハンドラ(worker thread から安全に渡す)。"""

    def __init__(self, queue: "Queue[tuple[str, object]]") -> None:
        super().__init__()
        self._queue = queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._queue.put(("log", self.format(record)))
        except Exception:  # pragma: no cover - キューへの投入失敗は致命的ではない
            self.handleError(record)


def timestamp() -> str:
    return datetime.now().strftime(C.LOG_TIMESTAMP_FORMAT)


def default_log_dir() -> Path:
    """ログの既定出力先: アプリ本体(このパッケージ)のフォルダ直下の logs/。入力の場所は汚さない。"""

    return Path(__file__).resolve().parent / C.LOG_DIR_NAME


def resolve_log_paths(log_dir: Path, stamp: Optional[str] = None) -> tuple[Path, Path]:
    """ログファイルと CSV のパスを決める。"""

    stamp = stamp or timestamp()
    log_path = log_dir / C.LOG_FILENAME_TEMPLATE.format(timestamp=stamp)
    csv_path = log_dir / C.CSV_FILENAME_TEMPLATE.format(timestamp=stamp)
    return log_path, csv_path


def setup_logger(
    log_path: Optional[Path] = None,
    verbose: bool = False,
    gui_queue: "Optional[Queue[tuple[str, object]]]" = None,
    quiet_console: bool = False,
) -> logging.Logger:
    """アプリ共通のロガーを構築して返す。多重呼び出しでもハンドラを重複させない。"""

    logger = logging.getLogger(C.LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    console_level = logging.DEBUG if verbose else logging.INFO
    if not quiet_console:
        console = logging.StreamHandler()
        console.setLevel(console_level)
        console.setFormatter(logging.Formatter(C.CONSOLE_LINE_FORMAT))
        logger.addHandler(console)

    if gui_queue is not None:
        gui_handler = QueueHandler(gui_queue)
        gui_handler.setLevel(console_level)
        gui_handler.setFormatter(logging.Formatter(C.CONSOLE_LINE_FORMAT))
        logger.addHandler(gui_handler)

    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(C.LOG_LINE_FORMAT))
        logger.addHandler(file_handler)
        logger.info("ログファイル: %s", log_path)

    if not logger.handlers:
        # ハンドラが 1 つも無いと logging の lastResort が標準エラーへ出してしまう
        logger.addHandler(logging.NullHandler())

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(C.LOGGER_NAME)


def write_csv(csv_path: Path, rows: Iterable[Iterable[object]]) -> None:
    """判定根拠の CSV を書き出す(Excel で開けるよう BOM 付き UTF-8)。"""

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(C.CSV_HEADER)
        for row in rows:
            writer.writerow(list(row))
