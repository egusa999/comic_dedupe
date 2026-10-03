"""タイトル(ファイル名/フォルダ名)から作品名と巻数を取り出す。

重複検出はここで作る `TitleKey` を `matching.py` があいまい照合する形で行う。
「第7巻」と「07」「vol.7」「(7)」を同じ巻として扱えるようにするのがこのモジュールの責務。
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Optional

from . import constants as C
from .models import TitleKey

_NOISE_REGEXES = tuple(re.compile(pattern) for pattern in C.NOISE_PATTERNS)
_VOLUME_REGEXES = tuple(re.compile(pattern) for pattern in C.VOLUME_PATTERNS)
_CHAPTER_REGEXES = tuple(re.compile(pattern) for pattern in C.CHAPTER_PATTERNS)
_VOLUME_FALLBACK_REGEXES = tuple(re.compile(pattern) for pattern in C.VOLUME_PATTERNS_FALLBACK)

#: 比較に使わない文字(記号・空白)を落とすための許可文字パターン
_KEEP_CHARS = re.compile(r"[0-9a-z぀-ヿ㐀-䶿一-鿿豈-﫿]+")

_KATAKANA_START = 0x30A1
_KATAKANA_END = 0x30F6
_HIRAGANA_OFFSET = 0x3041 - 0x30A1


def repair_mojibake(name: str) -> str:
    """CP932(日本語 Windows)の名前が CP437 などで誤解釈された文字化けを元に戻す。

    例: `âLâôâOâ_âÇ30è¬` → `キングダム30巻`。日本語を含む結果が得られたときだけ採用し、
    もともと正しい名前や直せない名前はそのまま返す。
    """

    low, high = C.MOJIBAKE_REPAIRED_RANGE
    if all(ord(ch) < 0x80 for ch in name) or any(low <= ord(ch) <= high for ch in name):
        return name  # ASCII だけ、または既に日本語が入っている名前は直さない
    for wrong, right in C.MOJIBAKE_REPAIR_ENCODINGS:
        try:
            repaired = name.encode(wrong).decode(right)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if any(low <= ord(ch) <= high for ch in repaired):
            return repaired
    for wrong, right in C.MOJIBAKE_REPAIR_ENCODINGS:
        repaired = name.encode(wrong, errors="replace").decode(right, errors="replace")
        if any(marker in repaired for marker in C.MOJIBAKE_LENIENT_MARKERS):
            return repaired
    return name


def basic_normalize(text: str) -> str:
    """NFKC 正規化 + 小文字化(全角英数・全角記号を半角に寄せる)。"""

    return unicodedata.normalize("NFKC", text).lower().strip()


def strip_extension(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in C.ARCHIVE_EXTENSIONS or suffix in C.IMAGE_EXTENSIONS:
        return name[: -len(suffix)]
    return name


def extract_editions(text: str) -> frozenset:
    """完全版・新装版などの「版違い」ワードを拾う(別作品として分離するため)。"""

    found = {word for word in C.EDITION_WORDS if word in text}
    return frozenset(found)


def strip_noise(text: str) -> str:
    """作者名・タグ・解像度表記などのノイズを取り除く。"""

    result = text
    for regex in _NOISE_REGEXES:
        result = regex.sub(" ", result)
    return re.sub(r"\s+", " ", result).strip()


def _katakana_to_hiragana(text: str) -> str:
    return "".join(
        chr(ord(ch) + _HIRAGANA_OFFSET) if _KATAKANA_START <= ord(ch) <= _KATAKANA_END else ch
        for ch in text
    )


def normalize_series(text: str) -> str:
    """作品名を比較用に正規化する(記号・空白を落とし、カタカナをひらがなへ寄せる)。"""

    kana = _katakana_to_hiragana(text)
    joined = "".join(_KEEP_CHARS.findall(kana))
    # 長音・波ダッシュの揺れを吸収
    return joined.replace("ー", "").replace("〜", "").replace("~", "")


def _apply_volume_regexes(
    text: str, regexes, chapter: bool = False
) -> tuple[Optional[str], str, str]:
    """巻数パターンを順に試す。戻り値は (巻数, 種別, 巻数部分を除いたテキスト)。

    chapter=True のときは話数として扱い、範囲表記も含めて種別を CHAPTER にする。
    """

    for regex in regexes:
        match = regex.search(text)
        if match is None:
            continue
        first = str(int(match.group("vol")))
        second = match.groupdict().get("vol2")
        if second:
            if int(first) < 1 or int(second) <= int(first):
                # 「04-00」「00-01」のように範囲として成立しない表記は、巻数を推測せず不明にする
                return None, C.VOLUME_KIND_INVALID, text
            volume = f"{int(first)}-{int(second)}"
            kind = C.VOLUME_KIND_CHAPTER if chapter else C.VOLUME_KIND_RANGE
        else:
            volume = first
            kind = C.VOLUME_KIND_CHAPTER if chapter else C.VOLUME_KIND_NUMBER
        remaining = (text[: match.start()] + " " + text[match.end():]).strip()
        return volume, kind, remaining
    return None, C.VOLUME_KIND_NUMBER, text


def _apply_part_words(text: str) -> tuple[Optional[str], str]:
    """上巻/前編などの区分語を巻数に変換する。"""

    for word, number in C.PART_WORDS.items():
        if word in text:
            return str(number), text.replace(word, " ")
    return None, text


def parse_volume(text: str, use_fallback: bool = True) -> tuple[Optional[str], str, str]:
    """正規化済みテキストから巻数を取り出す。戻り値は (巻数, 種別, 残りテキスト)。

    use_fallback=False のときは「末尾の裸の数字」を巻数とみなさない(ページ番号の誤認防止)。
    """

    volume, kind, remaining = _apply_volume_regexes(text, _CHAPTER_REGEXES, chapter=True)
    if volume is not None:
        return volume, kind, remaining
    if kind == C.VOLUME_KIND_INVALID:
        return None, C.VOLUME_KIND_NUMBER, text

    volume, kind, remaining = _apply_volume_regexes(text, _VOLUME_REGEXES)
    if volume is not None:
        return volume, kind, remaining
    if kind == C.VOLUME_KIND_INVALID:
        return None, C.VOLUME_KIND_NUMBER, text

    part_volume, remaining_after_part = _apply_part_words(text)
    if part_volume is not None:
        return part_volume, C.VOLUME_KIND_PART, remaining_after_part

    if not use_fallback:
        return None, C.VOLUME_KIND_NUMBER, text
    cleaned = strip_noise(remaining)
    volume, kind, remaining = _apply_volume_regexes(cleaned, _VOLUME_FALLBACK_REGEXES)
    if kind == C.VOLUME_KIND_INVALID:
        return None, C.VOLUME_KIND_NUMBER, text
    return volume, kind, remaining


_COPY_SUFFIX = re.compile(C.COPY_SUFFIX_PATTERN)


def strip_copy_suffix(text: str, use_fallback: bool = True) -> str:
    """末尾の ` (2)` が Windows のコピー連番なら取り除く(他に巻数が取れる場合に限る)。

    `作品名 (2)` のように、それが唯一の巻数表記なら巻数として残す。
    """

    match = _COPY_SUFFIX.search(text)
    if match is None:
        return text
    stripped = text[: match.start()]
    volume, _, _ = parse_volume(stripped, use_fallback=use_fallback)
    return stripped if volume is not None else text


def parse_title(name: str, use_fallback: bool = True) -> TitleKey:
    """ファイル名/フォルダ名を TitleKey(作品名 + 巻数)に変換する。"""

    raw = name
    text = strip_copy_suffix(basic_normalize(strip_extension(repair_mojibake(name))), use_fallback)
    editions = extract_editions(text)
    volume, kind, remaining = parse_volume(text, use_fallback=use_fallback)
    series = normalize_series(strip_noise(remaining))
    return TitleKey(
        series=series,
        volume=volume,
        volume_kind=kind,
        editions=editions,
        raw=raw,
    )


def volume_group_key(key: TitleKey) -> str:
    """同じ巻数・同じ版かどうかを粗く分けるためのキー。"""

    editions = ",".join(sorted(key.editions))
    return f"{key.volume_kind}:{key.volume}:{editions}"


# --- 表示用の作品名(統一名の生成に使う。大文字小文字・記号は元のまま) ----------

_NOISE_REGEXES_CI = tuple(re.compile(pattern, re.IGNORECASE) for pattern in C.NOISE_PATTERNS)
_VOLUME_REGEXES_CI = tuple(re.compile(pattern, re.IGNORECASE) for pattern in C.VOLUME_PATTERNS)
_CHAPTER_REGEXES_CI = tuple(re.compile(pattern, re.IGNORECASE) for pattern in C.CHAPTER_PATTERNS)
_VOLUME_FALLBACK_REGEXES_CI = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in C.VOLUME_PATTERNS_FALLBACK
)


#: 範囲表記(v01-05)から巻数だけ取れて残る「v」「vol」の取り残し
_TRAILING_VOLUME_MARK = re.compile(r"(?<![a-z0-9])(?:vol\.?|v)$", re.IGNORECASE)


def _strip_noise_ci(text: str) -> str:
    for regex in _NOISE_REGEXES_CI:
        text = regex.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def display_series(name: str) -> str:
    """ファイル名/フォルダ名から、巻数とノイズを除いた読める作品名を取り出す。

    比較用の `TitleKey.series`(小文字・記号なし)と違い、新しい名前にそのまま使える形で返す。
    """

    text = strip_copy_suffix(
        unicodedata.normalize("NFKC", strip_extension(repair_mojibake(name))).strip()
    )
    volume, _, remaining = _apply_volume_regexes(text, _CHAPTER_REGEXES_CI, chapter=True)
    if volume is None:
        _, _, remaining = _apply_volume_regexes(text, _VOLUME_REGEXES_CI)
    if remaining == text:
        part_volume, remaining = _apply_part_words(text)
        if part_volume is None:
            cleaned = _strip_noise_ci(text)
            _, _, remaining = _apply_volume_regexes(cleaned, _VOLUME_FALLBACK_REGEXES_CI)
    series = _strip_noise_ci(remaining).strip(C.SERIES_EDGE_CHARS)
    return _TRAILING_VOLUME_MARK.sub("", series).strip(C.SERIES_EDGE_CHARS)
