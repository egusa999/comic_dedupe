"""comic_dedupe で使う定数の集約先。

マジックナンバー・マジックワードはすべてこのファイルに置き、他モジュールからは
`from . import constants as C` の形で参照する(数値を直書きしない)。
しきい値を調整したいときはここだけを見ればよい状態を保つこと。
"""

from __future__ import annotations

import zipfile

# --- 対象拡張子 -------------------------------------------------------------

ZIP_EXTENSIONS = frozenset({".zip", ".cbz"})
RAR_EXTENSIONS = frozenset({".rar", ".cbr"})
SEVENZIP_EXTENSIONS = frozenset({".7z", ".cb7"})
ARCHIVE_EXTENSIONS = ZIP_EXTENSIONS | RAR_EXTENSIONS | SEVENZIP_EXTENSIONS

IMAGE_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".jpe",
        ".png",
        ".gif",
        ".bmp",
        ".webp",
        ".tif",
        ".tiff",
        ".avif",
        ".jxl",
    }
)

# 書庫/フォルダ内で無視するファイル(画像でもページでもないもの)
IGNORED_ENTRY_NAMES = frozenset({"thumbs.db", "desktop.ini", ".ds_store"})
IGNORED_ENTRY_PREFIXES = ("__MACOSX/", "__macosx/")

# --- 巻(ボリューム)の同定 -------------------------------------------------

#: フォルダを「1巻ぶんの画像フォルダ」と見なすのに必要な直下画像枚数
MIN_IMAGES_AS_VOLUME = 3

#: 1巻から画質測定のために抜き出すページ数の上限
SAMPLE_PAGES = 16

#: 画質比較で「同等」とみなす相対差(5% 以内の差は優劣なしとして次の指標へ)
QUALITY_MARGIN = 0.05

#: 全指標が同等だったときのタイブレーク順(大きいほど残したい)
SOURCE_RANK = {
    "zip": 4,
    "7z": 3,
    "rar": 2,
    "folder": 1,
    "imageset": 0,
}

# --- タイトルのあいまい照合 -------------------------------------------------

#: この類似度以上なら自動で「同一作品の同一巻」と判定する
SERIES_SIMILARITY_MIN = 0.85

#: この類似度以上かつ SERIES_SIMILARITY_MIN 未満は「保留(要確認)」にする
SERIES_SIMILARITY_REVIEW = 0.65

#: 部分文字列関係だったときに与える類似度(片方がもう片方を完全に含む場合)
SUBSTRING_SIMILARITY = 0.9

#: 部分一致を認めるために必要な短い側の最小文字数(短すぎる名前の誤爆防止)
SUBSTRING_MIN_LENGTH = 4

#: 両方とも作品名が取れなかった場合の類似度。
#: 「第7巻」と「07」のように巻数しか書かれていない同階層の書庫は同一巻として扱う。
EMPTY_SERIES_SIMILARITY = 1.0

#: 片方だけ作品名が無い場合の類似度(グレーゾーンに落として人に確認させる)
ONE_SIDED_SERIES_SIMILARITY = 0.7

#: タイトルから取り除くノイズ。正規表現(NFKC 正規化・小文字化の後に適用)。
#: 追記しやすいよう1行1パターンで並べる。
NOISE_PATTERNS = (
    r"dlraw\.to[-_ ]*",       # 配布サイト名の接頭辞(DLRAW.TO_。後続の「raw」除去より先に消す)
    r"\[[^\[\]]*\]",          # [作者名] [雑誌名] など角括弧の塊
    r"\([^()]*\)",            # (一般コミック) (完) (DL版) など丸括弧の塊
    r"【[^】]*】",
    r"dl版",
    r"(?:\d*dl(?:raw)?|manga-zip|dlraw)\.(?:app|me|cc|net|to)[-_ ]*",  # 配布サイト名の接頭辞(DLRAW.APP_ / 13DL.ME_ / MANGA-ZIP.APP_。後続の「raw」除去より先に消す)
    r"電子版",
    r"自宅スキャン",
    r"修正版",
    r"再upload",
    r"第?\d+刷",
    r"\d{3,4}\s*[x×]\s*\d{3,4}",  # 1600x2300 のような解像度表記
    r"高画質",
    r"高解像度",
    r"中国語",
    r"raw",
    r"\.?part\d+",
    r"分割\d+",
    r"jpg|jpeg|png|webp|avif",
)

#: 「版違い」を示す語。これらの有無が違うアイテムは別作品として扱う。
EDITION_WORDS = (
    "完全版",
    "新装版",
    "愛蔵版",
    "文庫版",
    "カラー版",
    "総集編",
    "特装版",
    "新装開店",
    "リマスター",
    "合本",
)

#: 「話数」(チャプター)の抽出パターン。巻数より先に試し、巻とは別種別(CHAPTER)で扱う。
#: ch658 / ch658-671 / chapter 12 / 第12話 / 第12-15話
CHAPTER_PATTERNS = (
    r"(?<![a-z0-9])ch(?:apter|\.)?\s*(?P<vol>\d{1,4})(?:\s*[-~ー〜]\s*(?P<vol2>\d{1,4}))?(?![a-z0-9])",
    r"第\s*(?P<vol>\d{1,4})(?:\s*[-~ー〜]\s*(?P<vol2>\d{1,4}))?\s*話",
)

#: RAR などで名前が文字化けしたとき(CP932 のバイト列を CP437 で解釈した形)を直す際の、
#: 解釈し直しの対象にする文字コードの組(誤った解釈 → 本来の文字コード)
MOJIBAKE_REPAIR_ENCODINGS = (("cp437", "cp932"), ("cp850", "cp932"), ("cp1252", "cp932"))

#: 厳密な変換が失敗したとき(一部の文字だけ化け方が違う)に、置換文字を許して再試行した結果を採用する条件。
#: 巻数の目印がこれらのどれかを含むときだけ採用する(誤修復防止)。
MOJIBAKE_LENIENT_MARKERS = ("第", "巻")

#: 文字化け修復の結果として採用する文字の範囲(ひらがな〜CJK 統合漢字が含まれていれば修復成功とみなす)
MOJIBAKE_REPAIRED_RANGE = (0x3040, 0x9FFF)

#: 巻数の抽出パターン。上から順に試し、最初に当たったものを採用する。
#: 各パターンは巻数を group("vol")(範囲の場合は "vol" と "vol2")で返すこと。
VOLUME_PATTERNS = (
    # 範囲表記(第1-10巻、01-10、1~10巻、v01-04、v01-10b)
    r"(?<![a-z0-9])v(?:ol\.?)?\s*(?P<vol>\d{1,4})\s*[-~ー〜]\s*(?P<vol2>\d{1,4})(?:[a-z]{1,2})?(?![a-z0-9])",
    r"第?(?P<vol>\d{1,4})\s*[-~ー〜]\s*(?P<vol2>\d{1,4})\s*巻",
    r"(?P<vol>\d{1,4})\s*[-~ー〜]\s*(?P<vol2>\d{1,4})\s*$",
    # 単巻表記
    r"(?<![0-9])(?P<vol>\d{1,4})_files(?![a-z0-9])",   # 8_files(巻数だけのフォルダ名の定番形)
    r"第(?P<vol>\d{1,4})\s*巻(?:[a-z]{1,2}(?![a-z0-9]))?",
    r"(?P<vol>\d{1,4})\s*巻(?:[a-z]{1,2}(?![a-z0-9]))?",
    r"vol\.?\s*(?P<vol>\d{1,4})",
    # v01 / v01s / v12ss / _v16(末尾の英字 1〜2 文字は画質・版のタグ。アンダースコア直後も許可)
    r"(?<![a-z0-9])v(?P<vol>\d{1,4})(?:[a-z]{1,2})?(?![a-z0-9])",
    r"#(?P<vol>\d{1,4})",
    r"\((?P<vol>\d{1,4})\)",
    r"\[(?P<vol>\d{1,4})\]",
)

#: 明示パターンで巻数が取れなかったときのフォールバック(ノイズ除去後に適用)。
#: 末尾の裸の数字(`作品名 07` / `07`)を巻数とみなす。
VOLUME_PATTERNS_FALLBACK = (
    # 日本語の「第04巻」が文字化けで `_` に置き換わって `_04_` になった名前(末尾の `_数字_`)
    r"_(?P<vol>\d{1,4})_\s*$",
    r"(?P<vol>\d{1,4})\s*[-~ー〜]\s*(?P<vol2>\d{1,4})\s*$",
    r"(?P<vol>\d{1,4})\s*$",
    # 05w / 07s のように巻数の直後に英字 1〜2 文字のタグが付く形(末尾のみ)
    r"(?<![a-z0-9])(?P<vol>\d{1,4})[a-z]{1,2}\s*$",
)

#: Windows が重複コピーに付ける ` (2)` のような末尾の連番。巻数 `(2)` と区別するため、
#: これを除いた残りに別の巻数が取れる場合だけコピー連番として無視する
COPY_SUFFIX_PATTERN = r"\s\(\d{1,3}\)$"

#: 上中下・前後編などの区分語 → 内部巻番号
PART_WORDS = {
    "上巻": 1,
    "中巻": 2,
    "下巻": 3,
    "前編": 1,
    "中編": 2,
    "後編": 3,
    "前巻": 1,
    "後巻": 2,
}

VOLUME_KIND_NUMBER = "number"
VOLUME_KIND_PART = "part"
VOLUME_KIND_RANGE = "range"
VOLUME_KIND_INVALID = "invalid"   # 範囲として成立しない表記(04-00 / 00-01 など)
VOLUME_KIND_CHAPTER = "chapter"   # 話数(巻とは別物。範囲表記も含む)

#: 話数の統一名: ゼロ埋めの最小桁数と書式({chapter} は 658 や 658-671)
CHAPTER_MIN_DIGITS = 3
UNIFIED_CHAPTER_TEMPLATE = "{series} ch{chapter}"
UNIFIED_CHAPTER_TEMPLATE_NO_SERIES = "ch{chapter}"

#: 範囲表記(v01-05 等)の書庫を展開して中身を判定する入れ子の最大段数
MAX_RANGE_EXPAND_DEPTH = 5

# --- 最大巻数の推測と再判定 -------------------------------------------------

#: 巻数として妥当とみなす上限(西暦 2021 などを巻数と誤認して最大巻数を水増ししないため)
MAX_PLAUSIBLE_VOLUME = 999

# --- サイズ外れ値の除外 -----------------------------------------------------

#: 同じフォルダの他の単巻と比べ、総バイト数が中央値のこの倍率未満なら「極端に小さい」(勝者候補から外す)
OUTLIER_LOW_RATIO = 0.25

#: 同じく、中央値のこの倍率を超えたら「極端に大きい」(勝者候補から外す)
OUTLIER_HIGH_RATIO = 4.0

#: 外れ値判定に必要な、同じフォルダの単巻の最小件数(少ないと中央値が当てにならない)
OUTLIER_MIN_REFERENCE_COUNT = 4

#: 外れ値(別物・欠け)として勝者候補から外された、または重複が無くても極端に小さい巻の置き場所
#: (出力のラップ書庫の中。原本の名前のまま入れ、巻としては並べない)
EXCLUDED_DIR_NAME = "_除外"

#: 判定できず保留になった巻(計測不能で勝敗が付かなかった・統一名が他と衝突した)の置き場所。
#: 原本の名前のまま入れ、同じ名前の巻フォルダが `(2)` 付きで並ぶのを防ぐ
PENDING_DIR_NAME = "_保留"

#: 同じ重複グループの中で、最大のページ数のこの割合未満のアイテムは「中身が少なすぎる(欠け・別物)」として
#: 勝者候補から外す(雑誌 19 ページが、本物の 1 巻 222 ページを押しのけた実例への対処)
GROUP_PAGE_RATIO_MIN = 0.3

#: 同じく、グループ内の最大の総バイト数のこの割合未満は勝者候補から外す
GROUP_SIZE_RATIO_MIN = 0.25

# --- 内容照合(--verify-content) -------------------------------------------

#: dHash の一辺(8 → 64bit)
HASH_SIZE = 8

#: 同一ページと見なすハミング距離の上限
HASH_DISTANCE_MAX = 6

#: グループを内容的に同一と認めるページ一致率の下限
CONTENT_MATCH_RATIO_MIN = 0.6

# --- 入出力のレイアウト -----------------------------------------------------

#: 負けた巻を集めるフォルダ名(最終 ZIP の中に残る)
DUP_DIR_NAME = "_重複"

#: 作業用フォルダ名のプレフィックス(入力と同じドライブに作る)
WORK_DIR_PREFIX = "_dedupe_work_"

#: 出力 ZIP のファイル名サフィックス(原本は残す)
OUTPUT_SUFFIX = "_整理済み"

#: 平坦出力で、巻として扱えなかったファイルを集めるフォルダ名
OTHER_DIR_NAME = "_その他"

#: 統一名の巻数の最小桁数(最大巻数がこれを超える場合はその桁数に揃える)
VOLUME_MIN_DIGITS = 2

#: 統一名に使う作品名の最大文字数(長パス対策)
MAX_SERIES_NAME_LENGTH = 60

#: 日本語(ひらがな〜CJK統合漢字)の文字コード範囲。作品名の代表に日本語表記を優先するために使う
CJK_RANGE_START = 0x3040
CJK_RANGE_END = 0x9FFF

#: 統一名の書式。{series} は作品名、{volume} はゼロ埋めした巻数(範囲は「01-05」)
UNIFIED_NAME_TEMPLATE = "{series} 第{volume}巻"
UNIFIED_NAME_TEMPLATE_NO_SERIES = "第{volume}巻"

#: 出力のフォルダ・ファイル名の言語。既定は日本語。`--folder-lang en` / GUI で英語表記にできる
FOLDER_LANG_JA = "ja"
FOLDER_LANG_EN = "en"
FOLDER_LANG_DEFAULT = FOLDER_LANG_JA

#: 言語ごとの出力フォルダ名・サフィックス・統一名の書式
#: (キー: dup=負けた巻 / excluded=外れ値 / pending=保留 / other=巻でないファイル / suffix=出力名 / volume・volume_no_series=巻の書式)
FOLDER_NAMES = {
    FOLDER_LANG_JA: {
        "dup": DUP_DIR_NAME,
        "excluded": EXCLUDED_DIR_NAME,
        "pending": PENDING_DIR_NAME,
        "other": OTHER_DIR_NAME,
        "suffix": OUTPUT_SUFFIX,
        "volume": UNIFIED_NAME_TEMPLATE,
        "volume_no_series": UNIFIED_NAME_TEMPLATE_NO_SERIES,
    },
    FOLDER_LANG_EN: {
        "dup": "_duplicates",
        "excluded": "_excluded",
        "pending": "_held",
        "other": "_others",
        "suffix": "_organized",
        "volume": "{series} Vol {volume}",
        "volume_no_series": "Vol {volume}",
    },
}

#: ファイル名に使えない文字(Windows 基準)
INVALID_FILENAME_CHARS = '<>:"/\\|?*'

#: 作品名の前後から落とす区切り文字
SERIES_EDGE_CHARS = " 　-_–—・,、."

#: 統一名の拡張子(zip と cbz は zip に揃える。rar/7z は元の拡張子を維持)
UNIFIED_ZIP_EXTENSION = ".zip"

#: 巻フォルダ内の「サブフォルダ 1 つだけ」の入れ子を引き上げる最大段数
MAX_HOIST_DEPTH = 10

#: 巻 ZIP の格納方式(DEFLATE=圧縮)。外側のラップ ZIP は無圧縮のまま
VOLUME_ZIP_COMPRESSION = zipfile.ZIP_DEFLATED

# --- ラップ書庫の形式(RAR + リカバリーレコード) -----------------------------

WRAP_FORMAT_RAR = "rar"
WRAP_FORMAT_ZIP = "zip"

#: 既定のラップ形式。RAR を作れない環境(Rar.exe が無い)では警告を出して ZIP にフォールバックする
WRAP_FORMAT_DEFAULT = WRAP_FORMAT_RAR

#: リカバリーレコードの割合(%)。0 で付加しない。RAR 本体の `-rr<N>p` に渡す
RAR_RECOVERY_PERCENT = 5
RAR_RECOVERY_PERCENT_MAX = 10

#: RAR の圧縮レベル(`-m<N>`: 0=無圧縮 1=高速 3=標準 5=最大)。既定は標準
RAR_COMPRESSION_LEVEL = 3
RAR_COMPRESSION_LEVEL_MAX = 5

#: RAR を「作る」ことができる外部コマンド(WinRAR / rar。7-Zip と unrar は作れない)
RAR_WRITER_NAMES = ("rar", "Rar")

#: Rar.exe の場所を明示する環境変数(PATH に WinRAR が無い・標準外の場所にあるとき用)
RAR_WRITER_ENV_VAR = "COMIC_DEDUPE_RAR"

#: Windows で RAR 作成コマンドを探す固定パス(ProgramFiles 系の環境変数配下も別途探す)
WINDOWS_RAR_WRITER_PATHS = (
    r"C:\Program Files\WinRAR\Rar.exe",
    r"C:\Program Files (x86)\WinRAR\Rar.exe",
)

#: ProgramFiles 系の環境変数名(WinRAR の標準インストール先を環境変数から組み立てる)
WINDOWS_PROGRAM_FILES_ENV_VARS = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")

#: ProgramFiles 配下の WinRAR の Rar.exe の相対パス
WINRAR_RAR_RELATIVE_PATH = r"WinRAR\Rar.exe"

#: 作業に必要な空き容量の見積り係数(入力サイズ × この値)
WORKSPACE_SIZE_FACTOR = 2.2

#: 名前衝突時のリネーム試行回数の上限
MAX_RENAME_ATTEMPTS = 1000

#: Windows の MAX_PATH 対策で警告を出すパス長
MAX_PATH_WARN = 250

# --- 作業領域の置き場所 -----------------------------------------------------

#: メモリ上のファイルシステム(RAM ディスク)として使える既知のフォルダ(あれば最優先)
RAM_DISK_CANDIDATES = ("/dev/shm",)

#: メモリ上の作業領域は、空き容量のこの割合までしか使わない(メモリ枯渇の防止)
RAM_WORKSPACE_MAX_FREE_RATIO = 0.5

# --- ログ -------------------------------------------------------------------

#: ログの既定出力先(アプリ本体=このパッケージのフォルダ直下のサブフォルダ名)
LOG_DIR_NAME = "logs"

LOGGER_NAME = "comic_dedupe"
LOG_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
LOG_FILENAME_TEMPLATE = "dedupe_{timestamp}.log"
CSV_FILENAME_TEMPLATE = "dedupe_{timestamp}.csv"
LOG_LINE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
CONSOLE_LINE_FORMAT = "%(levelname)-7s %(message)s"

CSV_HEADER = (
    "group",
    "role",
    "path",
    "kind",
    "pages",
    "median_pixels",
    "bytes_per_pixel",
    "total_bytes",
    "similarity",
    "reason",
    "result",
)

# --- 役割・結果の表示名 -----------------------------------------------------

ROLE_WINNER = "勝ち(残す)"
ROLE_LOSER = "負け(退避)"
ROLE_PENDING = "保留(要確認)"
ROLE_SINGLE = "単独(重複なし)"

RESULT_KEPT = "keep"
RESULT_MOVED = "moved"
RESULT_DELETED = "deleted"
RESULT_SKIPPED = "skipped"
RESULT_PLANNED = "planned"

# --- 終了コード -------------------------------------------------------------

EXIT_OK = 0
EXIT_INPUT_ERROR = 1
EXIT_PARTIAL = 2

# --- RAR/7z バックエンド ----------------------------------------------------

BACKEND_LIBARCHIVE = "libarchive"
BACKEND_BSDTAR = "bsdtar"
BACKEND_RARFILE = "rarfile"
BACKEND_PY7ZR = "py7zr"
BACKEND_SEVENZIP_CLI = "7z"
BACKEND_UNRAR_CLI = "unrar"

#: RAR を読むバックエンドの優先順(前から順に試し、失敗したら次へ降格)
RAR_BACKEND_PRIORITY = (
    BACKEND_LIBARCHIVE,
    BACKEND_BSDTAR,
    BACKEND_RARFILE,
    BACKEND_UNRAR_CLI,
    BACKEND_SEVENZIP_CLI,
)

#: 7z を読むバックエンドの優先順
SEVENZIP_BACKEND_PRIORITY = (BACKEND_PY7ZR, BACKEND_LIBARCHIVE, BACKEND_BSDTAR, BACKEND_SEVENZIP_CLI)

#: rarfile のバックエンドとして使える外部コマンド(PATH 上を探す順)
UNRAR_TOOL_NAMES = ("unrar", "UnRAR", "7z", "7zz", "7za", "bsdtar", "unar")

#: Windows で外部ツールを探す固定パス
WINDOWS_TOOL_PATHS = (
    r"C:\Program Files\WinRAR\UnRAR.exe",
    r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
    r"C:\Program Files\WinRAR\Rar.exe",
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    r"C:\Program Files\NanaZip\7z.exe",
)

#: Windows 同梱の bsdtar(libarchive ベース)
WINDOWS_BSDTAR_PATH = r"C:\Windows\System32\tar.exe"

#: bsdtar として使える外部コマンド名
BSDTAR_TOOL_NAMES = ("bsdtar", "tar")

#: RAR を展開できる WinRAR 系コマンド(UnRAR.exe / Rar.exe / unrar / rar)を PATH 上で探す名前
UNRAR_CLI_NAMES = ("unrar", "UnRAR", "rar", "Rar")

#: Windows で WinRAR 系コマンドを探す固定ファイル名(WinRAR フォルダ内)
WINRAR_CLI_FILENAMES = ("UnRAR.exe", "Rar.exe")

#: 7-Zip の Windows 標準インストール先(ProgramFiles 配下の相対パス)
SEVENZIP_RELATIVE_PATH = r"7-Zip\7z.exe"

#: 7z の一覧出力を UTF-8 で受け取るためのスイッチ(Windows の既定コードページでの文字化け対策)
SEVENZIP_UTF8_SWITCH = "-sccUTF-8"

#: bsdtar が「終了コード 0 でも一部を読み飛ばした」ことを示す標準エラーの文言(小文字で比較)。
#: これが出たら展開は不完全なので失敗扱いにして、次の解凍バックエンドへ切り替える。
BSDTAR_INCOMPLETE_MARKERS = ("unreadable filename", "skipping")

#: 外部コマンド実行のタイムアウト秒
SUBPROCESS_TIMEOUT = 600

#: libarchive の共有ライブラリを明示指定する環境変数(libarchive-c の仕様)
LIBARCHIVE_ENV_VAR = "LIBARCHIVE"

# --- GUI --------------------------------------------------------------------

GUI_TITLE = "書庫重複整理ツール (comic_dedupe)"
GUI_SETTINGS_FILENAME = "settings.json"
GUI_POLL_INTERVAL_MS = 150
GUI_LOG_MAX_LINES = 5000
GUI_WINDOW_SIZE = "1100x720"

#: 処理対象欄の列幅(px)。パスを広く、種別とサイズは最小限にする
GUI_INPUT_PATH_WIDTH = 800
GUI_INPUT_KIND_WIDTH = 48
GUI_INPUT_SIZE_WIDTH = 72
