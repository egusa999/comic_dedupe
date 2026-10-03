"""テストパッケージ。テスト中はコンソールへのログ出力を抑止する。"""

from comic_dedupe.logging_setup import setup_logger

setup_logger(quiet_console=True)
