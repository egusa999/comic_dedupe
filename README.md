# comic_dedupe — Deduplicate volumes inside comic archives

**English** | [日本語](README.ja.md)

Tidies up a single archive (zip/cbz/rar/cbr/7z) or folder in which **the same volume appears under different names**
(e.g. `Vol 7/` and `07.zip`, or `Vol 8.zip` and `08/`).

1. Recursively scans the input and finds "volume" items
2. Detects duplicates by **fuzzy title matching (series name + volume number)**
3. **Keeps the higher-quality copy** and moves the loser to `_重複/` (the "duplicates" folder)
4. Renames what is left to `<Series> 第NN巻/` (**volume folders with the images directly inside**; archives are extracted, needless nesting is flattened) and wraps everything into **one archive** (default: compressed RAR with a 5% recovery record; uncompressed ZIP if RAR cannot be created). No per-volume ZIPs are produced.
5. Writes a log (.log) and a decision-evidence CSV only when `--log` is given

**Originals are never modified** (output is `<input name>_整理済み.rar`, or `.zip` where RAR cannot be created).
Destructive behavior requires explicit options.

> Note: folder names produced by the tool (`_重複`, `_除外`, `_保留`, `_その他`, `第NN巻`) and the log/decision text are in Japanese.
> The GUI labels can be switched to English (see below).

---

## 1. Setup (Windows)

```bat
git clone https://github.com/egusa999/comic_dedupe.git comic_dedupe
py -m pip install -r comic_dedupe\requirements.txt
```

> Keep the folder name `comic_dedupe` (the tool is started with `py -m comic_dedupe.gui`).

- **Pillow** is the only hard requirement (used for quality judgment).
- Install `py7zr` to read **7z** with no extra tools.
- Reading **rar** needs one of the following. They are auto-detected in this order:

| Priority | Backend | What you need |
|---|---|---|
| 1 | `libarchive-c` | Native libarchive (`archive.dll` on Windows via conda-forge/vcpkg/MSYS2). **Not installable with pip alone** |
| 2 | `bsdtar` | **`C:\Windows\System32\tar.exe`, bundled with Windows 10 1803+.** Windows 11 23H2+ bundles libarchive and can often read rar (bsdtar is detected via `--version` at startup) |
| 3 | `rarfile` + external tool | Install 7-Zip or WinRAR (and `pip install rarfile`) |
| 4 | WinRAR tools (`UnRAR.exe` / `Rar.exe`) directly | **Just install it.** The official RAR implementation, most compatible. The WinRAR installed for wrapping is reused |
| 5 | 7-Zip (`7z.exe`) directly | **Just install it** (standard locations are auto-detected) |

Check:

```bat
tar -tf "target.rar"          :: if the contents are listed, the bundled tar.exe can read it
py -m comic_dedupe.cli --help
```

If no backend can read RAR, **RAR items are passed through untouched** (so nothing is removed by mistake).

> Note: when `rarfile` ends up using `bsdtar`, reading individual entries can fail for some archives; the tool then falls back to the next backend automatically.
> To handle RAR reliably, install 7-Zip or WinRAR (they can read RARs that bsdtar cannot, e.g. `tar.exe: Archive entry has empty or unreadable filename`). On failure, the error lists the reason for every backend that was tried.

## 2. GUI

```bat
py -m comic_dedupe.gui
```

- Use **Language / 言語** at the bottom right to switch the display between Japanese and English (the choice is saved in `~/.comic_dedupe/settings.json`).
  Only GUI labels are translated; log text and decision reasons stay in Japanese. To add strings, put both `ja` and `en` entries in `STRINGS` in `i18n.py`.
- **"Add archives…"** selects multiple archive files and **"Add folder…"** selects a folder (mixed lists are fine). Each input is processed as an independent job, in order.
- **"Analyze"** shows detected duplicates and planned winners without changing any file.
- Gray-zone pairs (medium similarity) are shown with `☐`. Only those you click to `☑` are treated as the same volume on the next **"Run"** (this is where you stop fuzzy-match false positives).
- Log files are created only if "Write logs (.log/.csv)" is checked (an empty folder field means `logs/` next to the app).
- Settings are stored in `%USERPROFILE%\.comic_dedupe\settings.json`.

## 3. CLI

```bat
py -m comic_dedupe.cli "D:\comics\Series.zip" --log
py -m comic_dedupe.cli "D:\comics\Series" --dry-run
py -m comic_dedupe.cli "D:\comics\A.zip" "D:\comics\B" --log "D:\logs"
```

| Option | Meaning |
|---|---|
| `--log [DIR]` | Write the log and evidence CSV (`logs/` next to the app if DIR is omitted). **Off by default** |
| `--verbose` | Detailed log including per-page measurements |
| `--dry-run` | Judge only; change no files |
| `--delete-losers` | Delete losing volumes instead of setting them aside (irreversible) |
| `--wrap-format rar\|zip` | Wrapper archive format. Default `rar` (compressed + recovery record). **Only WinRAR's `Rar.exe` (or `rar`) can create RAR**, not 7-Zip. If it is missing or fails, a warning is shown and an uncompressed ZIP is written |
| `--rar-level N` | RAR compression level (0 = store … 5 = max, default 3). Images are already compressed, so higher levels save little |
| `--recovery-percent N` | RAR recovery record percentage (0–10, 0 = none, default 5) |
| `--wrap-zip` | Legacy layout: keep the original hierarchy and names, zip each volume (compressed), and wrap everything in an uncompressed ZIP |
| `--replace` | After successful verification, replace the original with the output |
| `--in-place` | Operate on a folder input directly instead of copying it to the work area (fast, modifies the original) |
| `--rar-to-zip` | (with `--wrap-zip` only) rebuild kept rar files as compressed ZIP |
| `--verify-content` | Also compare the content (page dHash) of pairs judged the same volume; hold them if the match rate is low |
| `--sample N` | Pages sampled for quality measurement (default 16) |
| `--quality-margin F` | Relative difference within which two copies count as equal (default 0.05) |
| `--series-similarity F` | Similarity at which two series are auto-judged the same (default 0.85) |
| `--series-review F` | Lower bound of similarity for "needs review" (default 0.65) |
| `--accept-review` | Also auto-process gray-zone pairs |
| `--alias-file PATH` | Series-name alias JSON (`{"ワンピース": ["ONE PIECE"]}`) |
| `--rar-backend` | `auto` / `libarchive` / `bsdtar` / `rarfile` |
| `--work-dir PATH` | Parent folder for the work area. Default order: RAM disk (`/dev/shm`, Linux only) → local temp folder → next to the input. On Windows, give a RAM-disk drive to work in memory |
| `--no-retry-overflow` | Disable the re-judgment triggered by the max-volume check |
| `--dup-dir-name NAME` | Name of the folder for losers (default `_重複`) |
| `--keep-work-dir` | Keep the work area (for investigation) |

Exit codes: `0` OK / `1` input error / `2` finished, but some items were held or skipped.

## 4. Judgment rules

### Duplicate detection (fuzzy title match)

1. `NFKC` normalization, lowercasing, katakana → hiragana, removal of symbols and spaces
2. Noise removal: distribution-site prefixes (`DLRAW.TO_`, `13DL.APP-`, `DLRAW.APP_`, `13DL.ME_`, `MANGA-ZIP.APP_`), `[author]`, `(DL版)`, `(完)`, resolution tags (`1600x2300`), `第1刷`, etc.
3. **Chapters are separate from volumes**: `ch658`, `ch658-671`, `chapter 12`, `第12話`, `第12-15話` are recognized as chapters and never mixed with volumes (only identical chapters/ranges are compared; output name `<Series> ch658-671`). Partially overlapping ranges (`ch669-680` and `ch680-689`) are different and both are kept.
4. Volume number extraction (a trailing ` (2)` is ignored as a Windows copy counter when another number is found): `第07巻`, `第7巻`, `7巻`, `vol.7`, `v07`, `(7)`, `[7]`, `#7`, `上巻/下巻`, `前編/後編`, a bare trailing number (`Series 07`, `07`), `8_files` (`N_files`), `1-10` / `v01-04` / `v01-10b` (ranges are distinct from single volumes). Ranges that cannot be valid (`04-00`, `00-01`) are treated as "no volume number".
5. Only pairs with the same volume number are compared by series-name similarity: the maximum of `SequenceMatcher`, character-bigram Dice coefficient and substring match
   - `>= 0.85` → automatically the same volume
   - `0.65 – 0.85` → **held** (approve in the GUI, or `--accept-review`)
   - neither has a series name (`第7巻` vs `07`) → treated as the same volume
   - only one has a series name → held; but if every series name in the same folder is identical (one series), nameless volumes (`8_files`, `07.zip`) inherit it and count as the same volume
   - pairs that differ in **edition words** (`完全版`, `新装版`, `カラー版`, …) are different works
6. Mojibake names (CP932 mis-decoded as CP437/CP850/CP1252) are repaired before judging.

### Re-judgment (max-volume check)

If the number of remaining volume groups in a folder exceeds the estimated maximum volume number, the same volume is assumed to remain in separate groups because of different spellings (romaji vs. Japanese, etc.). The series name is then **ignored and volumes with the same number and edition are merged**. Because this can wrongly merge different series that share a folder, `--no-retry-overflow` disables it (originals are kept, so it is non-destructive).

### Quality judgment (which copy to keep)

1. **Median pixel count per page** (higher resolution wins)
2. **bytes/pixel** (looser compression = less degradation)
3. **Total bytes**

Page count is not used (different editions normally differ in page count).

**Excluding implausibly small items**: (1) within a duplicate group, candidates below 30% of the largest page count or 25% of total bytes are removed from winner candidates and moved to `_重複/`. (2) Items below 25% (or above 4×) of the median single-volume size of the whole folder/series (4+ volumes) are also excluded; ranges (`第33-34巻`) are compared against "volumes × median". (3) Outliers without a duplicate are not placed as volumes but go to `_除外/` under their original name (magazines or truncated files; a warning is shown). Chapter archives are exempt. Thresholds: `OUTLIER_*` / `GROUP_*` in `constants.py`.

If the relative difference is within `--quality-margin` (5%), the copies are equal and the next metric is used.
If all metrics tie, the decision falls back to "archive type (zip > 7z > rar > folder) → informativeness of the name → name order", and the reason is recorded.

### Name unification and flat output (default)

- The wrapper is RAR5 (`rar a -r -m3 -ma5 -ep1 -s- -rr5p`, non-solid) and is verified right afterwards with `rar t`. The recovery record is redundant data that lets WinRAR's "Repair" fix a partly damaged archive.
- The wrapper is built and verified in the local work area and moved to the destination once (building directly on a NAS was slow).
- A volume is named `<Series> 第NN巻` (folder). The series name is decided by majority vote among the original names of volumes judged to be the same series (author names, `(DL版)`, resolution tags are removed); the volume number is zero-padded to the maximum volume (minimum 2 digits).
- Volumes without a series name (`07.zip`) take the series name with the most volumes; archives without a volume number keep their original name.
- `_保留/` ("held") receives items that are duplicates but could not be ranked because the RAR was unreadable, and items whose unified name collides with another volume — under their original names.
- The top level of the wrapper contains only volume folders (plus `_重複/` `_除外/` `_保留/` `_その他/`), with images directly inside each. Only volumes that cannot be extracted stay as archives with the unified name + original extension. Losers go to `_重複/`; non-volume files (covers, text, …) are collected flat in `_その他/`.

### Cases that are not touched (held)

- The archive cannot be read / contains no images / quality cannot be measured
- The volume number cannot be determined
- Series-name similarity is in the gray zone (not approved)
- With `--verify-content`, the content match rate is below 0.6

## 5. Module layout

| File | Responsibility |
|---|---|
| `constants.py` | Thresholds, extensions, folder names (**numbers live only here**) |
| `models.py` | Data structures passed between modules |
| `logging_setup.py` | Logger setup (file output only with `--log`) and CSV writer |
| `external_tools.py` | Backend detection (libarchive / bsdtar / unrar, 7z / py7zr) |
| `archives.py` | Reads archives and folders through one interface (falls back to the next backend on failure) |
| `discovery.py` | Identifies volume items in the work tree (nested aware) |
| `naming.py` | Title normalization, volume-number extraction, mojibake repair |
| `matching.py` | Fuzzy matching and grouping (Union-Find) |
| `pages.py` | Page listing, sampling, measurement, dHash |
| `quality.py` | Quality scoring and winner selection |
| `layout.py` | Moving / deleting losers |
| `repack.py` | Creating and verifying the wrapper archive (RAR / ZIP) and `--wrap-zip` volume ZIPs |
| `unify.py` | Deciding unified names (`<Series> 第NN巻`) |
| `i18n.py` | Japanese / English switching for GUI labels |
| `pipeline.py` | Orchestration (one input = one job) |
| `cli.py` / `gui.py` | Front ends (no judgment logic) |

## 6. Tests

```bash
python3 -m unittest discover -s comic_dedupe/tests -t .
```

Synthetic data (generated with Pillow) is used to verify, end to end, fuzzy matching, quality judgment, moving losers, ZIP creation, the non-destructiveness of `--dry-run`, and that originals stay intact.
