"""GUI 表示文言の多言語化(日本語 / English)。

文言は `STRINGS` に `キー: {言語コード: 文}` で持ち、`t(キー, **書式引数)` で現在の言語の文を返す。
英語が未定義のキーは日本語にフォールバックする。ログ・判定結果の本文など `pipeline` が生成する文言は対象外(日本語のまま)。
"""

from __future__ import annotations

from . import constants as C

STRINGS: dict[str, dict[str, str]] = {
    "title": {"ja": C.GUI_TITLE, "en": "Archive Duplicate Organizer (comic_dedupe)"},
    "language": {"ja": "言語", "en": "Language"},
    "status.idle": {"ja": "待機中", "en": "Idle"},
    "status.running": {"ja": "処理中…", "en": "Working…"},
    "warn.no_rar": {
        "ja": "警告: RAR を読めるバックエンドがありません。7-Zip か WinRAR を入れると rar も判定できます(未検出の rar は触らずに通します)。",
        "en": "Warning: no backend that can read RAR was found. Install 7-Zip or WinRAR to judge RAR files too (undetected RARs are passed through untouched).",
    },
    # 入力パネル
    "input.title": {"ja": "処理対象(書庫ファイルは複数選択、フォルダは単位指定)", "en": "Inputs (select multiple archives, or a folder)"},
    "input.add_archives": {"ja": "書庫を追加…", "en": "Add archives…"},
    "input.add_folder": {"ja": "フォルダを追加…", "en": "Add folder…"},
    "input.remove": {"ja": "選択を除外", "en": "Remove selected"},
    "input.clear": {"ja": "クリア", "en": "Clear"},
    "input.col_path": {"ja": "パス", "en": "Path"},
    "input.col_kind": {"ja": "種別", "en": "Type"},
    "input.col_size": {"ja": "サイズ", "en": "Size"},
    "input.kind_folder": {"ja": "フォルダ", "en": "Folder"},
    # オプション
    "opt.title": {"ja": "オプション", "en": "Options"},
    "opt.dry_run": {"ja": "解析のみ(ファイルを変更しない)", "en": "Analyze only (do not change files)"},
    "opt.write_log": {"ja": "ログ(.log/.csv)を出力する", "en": "Write logs (.log/.csv)"},
    "opt.log_dir": {"ja": "出力先…", "en": "Folder…"},
    "opt.delete_losers": {"ja": "負けた巻を削除する(復元不可)", "en": "Delete losing volumes (irreversible)"},
    "opt.replace": {"ja": "原本を出力 ZIP で置き換える", "en": "Replace originals with the output ZIP"},
    "opt.rar_to_zip": {"ja": "rar も無圧縮 ZIP に作り直す", "en": "Rebuild RARs as uncompressed ZIP too"},
    "opt.verify_content": {"ja": "内容(ページ画像)も照合する", "en": "Also compare content (page images)"},
    "opt.accept_review": {"ja": "グレーゾーンも自動処理する", "en": "Auto-process gray-zone pairs too"},
    "opt.sample": {"ja": "画質計測ページ数", "en": "Pages sampled for quality"},
    "opt.margin": {"ja": "画質マージン", "en": "Quality margin"},
    "opt.similarity": {"ja": "作品名類似度(自動)", "en": "Title similarity (auto)"},
    "opt.review": {"ja": "保留しきい値", "en": "Review threshold"},
    "opt.rar_level": {"ja": "RAR圧縮レベル(0=無圧縮)", "en": "RAR level (0 = store)"},
    # 結果パネル
    "result.title": {"ja": "判定結果(グレーゾーンは ☐ をクリックして承認)", "en": "Results (click ☐ to approve gray-zone pairs)"},
    "result.col_tree": {"ja": "グループ / 巻", "en": "Group / Volume"},
    "result.col_role": {"ja": "役割", "en": "Role"},
    "result.col_pages": {"ja": "ページ", "en": "Pages"},
    "result.col_pixels": {"ja": "中央画素数", "en": "Median pixels"},
    "result.col_reason": {"ja": "判定理由", "en": "Reason"},
    "result.input": {"ja": "入力", "en": "Input"},
    "result.group": {"ja": "グループ {key}", "en": "Group {key}"},
    "result.duplicate": {"ja": "重複", "en": "Duplicate"},
    "result.similarity": {"ja": "類似度 {value:.2f} {notes}", "en": "Similarity {value:.2f} {notes}"},
    "result.approve_hint": {"ja": "☐ をクリックして承認すると次回の実行で処理します", "en": "Click ☐ to approve; it will be processed on the next run"},
    "role.winner": {"ja": C.ROLE_WINNER, "en": "Keep"},
    "role.loser": {"ja": C.ROLE_LOSER, "en": "Set aside"},
    "role.pending": {"ja": C.ROLE_PENDING, "en": "Needs review"},
    "role.single": {"ja": C.ROLE_SINGLE, "en": "Single (no duplicate)"},
    # ログ・操作
    "log.title": {"ja": "ログ", "en": "Log"},
    "btn.analyze": {"ja": "解析のみ", "en": "Analyze"},
    "btn.execute": {"ja": "実行", "en": "Run"},
    "btn.cancel": {"ja": "中止", "en": "Cancel"},
    "btn.open_log": {"ja": "ログを開く", "en": "Open log"},
    "btn.open_output": {"ja": "出力先を開く", "en": "Open output"},
    "log.csv": {"ja": "判定根拠 CSV: {path}", "en": "Evidence CSV: {path}"},
    "log.warning": {"ja": "警告: {warning}", "en": "Warning: {warning}"},
    "log.error": {"ja": "想定外のエラー: {error}", "en": "Unexpected error: {error}"},
    "log.cancel_requested": {"ja": "中止を要求しました(現在の処理の切れ目で停止します)", "en": "Cancel requested (will stop at the next safe point)"},
    # ダイアログ
    "dlg.pick_archives": {"ja": "書庫ファイルを選択(複数選択できます)", "en": "Select archive files (multiple allowed)"},
    "dlg.archives": {"ja": "書庫ファイル", "en": "Archive files"},
    "dlg.all_files": {"ja": "すべてのファイル", "en": "All files"},
    "dlg.pick_folder": {"ja": "フォルダを選択", "en": "Select a folder"},
    "dlg.pick_log_dir": {"ja": "ログの出力先", "en": "Log output folder"},
    "dlg.confirm": {"ja": "確認", "en": "Confirm"},
    "dlg.confirm_delete": {"ja": "負けた巻を『削除』します(復元できません)。続行しますか?", "en": "Losing volumes will be DELETED (irreversible). Continue?"},
    "dlg.confirm_replace": {"ja": "検証後に原本を出力 ZIP で置き換えます。続行しますか?", "en": "After verification the originals will be replaced by the output ZIP. Continue?"},
    "dlg.busy_title": {"ja": "実行中", "en": "Busy"},
    "dlg.busy": {"ja": "処理が実行中です。完了か中止をお待ちください。", "en": "A job is running. Wait for it to finish or cancel it."},
    "dlg.no_input_title": {"ja": "対象なし", "en": "No input"},
    "dlg.no_input": {"ja": "処理対象を追加してください。", "en": "Please add something to process."},
    "dlg.bad_options": {"ja": "オプションが不正", "en": "Invalid options"},
    "dlg.no_log_title": {"ja": "ログなし", "en": "No log"},
    "dlg.no_log": {"ja": "ログ出力を有効にして実行するとここから開けます。", "en": "Enable log output and run to open it here."},
    "dlg.no_output_title": {"ja": "出力なし", "en": "No output"},
    "dlg.no_output": {"ja": "まだ出力がありません。", "en": "There is no output yet."},
    "dlg.cannot_open": {"ja": "開けません", "en": "Cannot open"},
}

LANGUAGES = {"ja": "日本語", "en": "English"}
DEFAULT_LANGUAGE = "ja"

_current = DEFAULT_LANGUAGE


def set_language(code: str) -> None:
    """表示言語を切り替える。未対応のコードは既定(日本語)にする。"""

    global _current
    _current = code if code in LANGUAGES else DEFAULT_LANGUAGE


def get_language() -> str:
    return _current


def t(key: str, /, **kwargs) -> str:
    """現在の言語の文言を返す。英語が無ければ日本語、キーが無ければキー名を返す。"""

    entry = STRINGS.get(key)
    if entry is None:
        return key
    text = entry.get(_current) or entry[DEFAULT_LANGUAGE]
    return text.format(**kwargs) if kwargs else text


def role_text(role: str) -> str:
    """`constants.ROLE_*`(内部値)を表示用の文言にする。"""

    keys = {
        C.ROLE_WINNER: "role.winner",
        C.ROLE_LOSER: "role.loser",
        C.ROLE_PENDING: "role.pending",
        C.ROLE_SINGLE: "role.single",
    }
    return t(keys[role]) if role in keys else role
