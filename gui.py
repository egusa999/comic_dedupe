"""tkinter フロントエンド。

書庫ファイルの複数選択、またはフォルダ単位の指定ができ、積んだ入力を 1 件ずつ処理する。
判定ロジックはここには書かず、すべて `pipeline.Pipeline` に委譲する。

起動: `python -m comic_dedupe.gui`
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import constants as C
from . import external_tools, i18n, matching, pipeline
from .i18n import t
from .logging_setup import default_log_dir, resolve_log_paths, setup_logger, timestamp, write_csv
from .models import JobResult

CHECKED = "☑"
UNCHECKED = "☐"


def settings_path() -> Path:
    return Path.home() / ".comic_dedupe" / C.GUI_SETTINGS_FILENAME


class DedupeApp(ttk.Frame):
    """メインウィンドウ。"""

    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=8)
        self.master.geometry(C.GUI_WINDOW_SIZE)
        self.grid(sticky="nsew")
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)

        self._queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._cancel = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._inputs: list[Path] = []
        self._review_rows: dict[str, tuple[frozenset, bool]] = {}
        self._last_log_path: Optional[Path] = None
        self._last_output_dir: Optional[Path] = None
        self._results: list[JobResult] = []

        self._build_variables()
        self._load_settings()
        i18n.set_language(self.var_language.get())
        self._build_widgets()
        self._append_log(external_tools.describe_backends())
        if not external_tools.available_rar_backends():
            self._append_log(t("warn.no_rar"))
        self.after(C.GUI_POLL_INTERVAL_MS, self._poll_queue)

    # --- 画面構築 -----------------------------------------------------------

    def _build_variables(self) -> None:
        self.var_dry_run = tk.BooleanVar(value=False)
        self.var_write_log = tk.BooleanVar(value=False)
        self.var_log_dir = tk.StringVar(value="")
        self.var_delete_losers = tk.BooleanVar(value=False)
        self.var_replace = tk.BooleanVar(value=False)
        self.var_rar_to_zip = tk.BooleanVar(value=False)
        self.var_verify_content = tk.BooleanVar(value=False)
        self.var_accept_review = tk.BooleanVar(value=False)
        self.var_sample = tk.IntVar(value=C.SAMPLE_PAGES)
        self.var_rar_level = tk.IntVar(value=C.RAR_COMPRESSION_LEVEL)
        self.var_quality_margin = tk.DoubleVar(value=C.QUALITY_MARGIN)
        self.var_similarity = tk.DoubleVar(value=C.SERIES_SIMILARITY_MIN)
        self.var_review = tk.DoubleVar(value=C.SERIES_SIMILARITY_REVIEW)
        self.var_status = tk.StringVar(value=t("status.idle"))
        self.var_language = tk.StringVar(value=i18n.DEFAULT_LANGUAGE)

    def _build_widgets(self) -> None:
        self.master.title(t("title"))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.rowconfigure(3, weight=2)
        self.rowconfigure(5, weight=1)

        self._build_input_panel()
        self._build_option_panel()
        self._build_result_panel()
        self._build_log_panel()
        self._build_action_panel()

    def _build_input_panel(self) -> None:
        frame = ttk.LabelFrame(self, text=t("input.title"), padding=6)
        frame.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        frame.columnconfigure(0, weight=1)

        buttons = ttk.Frame(frame)
        buttons.grid(row=0, column=0, sticky="w")
        ttk.Button(buttons, text=t("input.add_archives"), command=self._add_archives).pack(side="left")
        ttk.Button(buttons, text=t("input.add_folder"), command=self._add_folder).pack(side="left", padx=4)
        ttk.Button(buttons, text=t("input.remove"), command=self._remove_selected).pack(side="left")
        ttk.Button(buttons, text=t("input.clear"), command=self._clear_inputs).pack(side="left", padx=4)

        self.input_tree = ttk.Treeview(
            frame, columns=("kind", "size"), show="tree headings", height=5
        )
        self.input_tree.heading("#0", text=t("input.col_path"))
        self.input_tree.heading("kind", text=t("input.col_kind"))
        self.input_tree.heading("size", text=t("input.col_size"))
        # 処理対象欄はパスを広く取り、種別・サイズは必要最小限の固定幅にする
        self.input_tree.column("#0", width=C.GUI_INPUT_PATH_WIDTH, stretch=True)
        self.input_tree.column("kind", width=C.GUI_INPUT_KIND_WIDTH, stretch=False, anchor="center")
        self.input_tree.column("size", width=C.GUI_INPUT_SIZE_WIDTH, stretch=False, anchor="e")
        self.input_tree.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        frame.rowconfigure(1, weight=1)

    def _build_option_panel(self) -> None:
        frame = ttk.LabelFrame(self, text=t("opt.title"), padding=6)
        frame.grid(row=2, column=0, sticky="ew", pady=(0, 6))

        left = ttk.Frame(frame)
        left.grid(row=0, column=0, sticky="nw", padx=(0, 16))
        ttk.Checkbutton(
            left, text=t("opt.dry_run"), variable=self.var_dry_run
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            left, text=t("opt.write_log"), variable=self.var_write_log
        ).grid(row=1, column=0, sticky="w")
        log_row = ttk.Frame(left)
        log_row.grid(row=2, column=0, sticky="w")
        ttk.Entry(log_row, textvariable=self.var_log_dir, width=32).pack(side="left")
        ttk.Button(log_row, text=t("opt.log_dir"), command=self._choose_log_dir).pack(side="left", padx=4)

        middle = ttk.Frame(frame)
        middle.grid(row=0, column=1, sticky="nw", padx=(0, 16))
        ttk.Checkbutton(
            middle, text=t("opt.delete_losers"), variable=self.var_delete_losers
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            middle, text=t("opt.replace"), variable=self.var_replace
        ).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(
            middle, text=t("opt.rar_to_zip"), variable=self.var_rar_to_zip
        ).grid(row=2, column=0, sticky="w")
        ttk.Checkbutton(
            middle, text=t("opt.verify_content"), variable=self.var_verify_content
        ).grid(row=3, column=0, sticky="w")
        ttk.Checkbutton(
            middle, text=t("opt.accept_review"), variable=self.var_accept_review
        ).grid(row=4, column=0, sticky="w")

        right = ttk.Frame(frame)
        right.grid(row=0, column=2, sticky="nw")
        self._add_spin(right, 0, t("opt.sample"), self.var_sample, 1, 200, 1)
        self._add_spin(right, 1, t("opt.margin"), self.var_quality_margin, 0.0, 0.9, 0.01)
        self._add_spin(right, 2, t("opt.similarity"), self.var_similarity, 0.1, 1.0, 0.01)
        self._add_spin(right, 3, t("opt.review"), self.var_review, 0.1, 1.0, 0.01)
        self._add_spin(
            right, 4, t("opt.rar_level"), self.var_rar_level, 0, C.RAR_COMPRESSION_LEVEL_MAX, 1
        )

    def _add_spin(self, parent, row, label, variable, minimum, maximum, step) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
        ttk.Spinbox(
            parent,
            from_=minimum,
            to=maximum,
            increment=step,
            textvariable=variable,
            width=8,
        ).grid(row=row, column=1, sticky="w", padx=4, pady=1)

    def _build_result_panel(self) -> None:
        frame = ttk.LabelFrame(self, text=t("result.title"), padding=6)
        frame.grid(row=3, column=0, sticky="nsew", pady=(0, 6))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.result_tree = ttk.Treeview(
            frame,
            columns=("role", "pages", "pixels", "reason"),
            show="tree headings",
            height=10,
        )
        self.result_tree.heading("#0", text=t("result.col_tree"))
        self.result_tree.heading("role", text=t("result.col_role"))
        self.result_tree.heading("pages", text=t("result.col_pages"))
        self.result_tree.heading("pixels", text=t("result.col_pixels"))
        self.result_tree.heading("reason", text=t("result.col_reason"))
        self.result_tree.column("role", width=110, anchor="center")
        self.result_tree.column("pages", width=60, anchor="e")
        self.result_tree.column("pixels", width=100, anchor="e")
        self.result_tree.column("reason", width=460, anchor="w")
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.result_tree.yview)
        self.result_tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")
        self.result_tree.bind("<Button-1>", self._on_result_click)

    def _build_log_panel(self) -> None:
        frame = ttk.LabelFrame(self, text=t("log.title"), padding=6)
        frame.grid(row=5, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(frame, height=8, wrap="none")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set, state="disabled")
        scroll.grid(row=0, column=1, sticky="ns")

    def _build_action_panel(self) -> None:
        frame = ttk.Frame(self)
        frame.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        frame.columnconfigure(3, weight=1)

        self.button_analyze = ttk.Button(frame, text=t("btn.analyze"), command=self._run_analyze)
        self.button_analyze.grid(row=0, column=0)
        self.button_execute = ttk.Button(frame, text=t("btn.execute"), command=self._run_execute)
        self.button_execute.grid(row=0, column=1, padx=4)
        self.button_cancel = ttk.Button(
            frame, text=t("btn.cancel"), command=self._cancel_job, state="disabled"
        )
        self.button_cancel.grid(row=0, column=2)

        self.progress = ttk.Progressbar(frame, mode="determinate")
        self.progress.grid(row=0, column=3, sticky="ew", padx=8)
        ttk.Label(frame, textvariable=self.var_status).grid(row=0, column=4)
        ttk.Button(frame, text=t("btn.open_log"), command=self._open_log).grid(row=0, column=5, padx=4)
        ttk.Button(frame, text=t("btn.open_output"), command=self._open_output_dir).grid(row=0, column=6)
        ttk.Label(frame, text=t("language")).grid(row=0, column=7, padx=(12, 2))
        combo = ttk.Combobox(
            frame,
            state="readonly",
            width=9,
            values=list(i18n.LANGUAGES.values()),
        )
        combo.set(i18n.LANGUAGES[i18n.get_language()])
        combo.grid(row=0, column=8)
        combo.bind("<<ComboboxSelected>>", lambda _event: self._switch_language(combo.get()))

    def _switch_language(self, label: str) -> None:
        """言語を切り替え、画面を作り直す(入力・ログ・判定結果は引き継ぐ)。"""

        code = next((c for c, name in i18n.LANGUAGES.items() if name == label), i18n.DEFAULT_LANGUAGE)
        if code == i18n.get_language():
            return
        running = self._worker is not None and self._worker.is_alive()
        log_content = self.log_text.get("1.0", "end-1c")
        i18n.set_language(code)
        self.var_language.set(code)
        for child in self.winfo_children():
            child.destroy()
        self._review_rows.clear()
        self._build_widgets()
        self.log_text.configure(state="normal")
        self.log_text.insert("end", log_content + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        for path in self._inputs:
            kind = t("input.kind_folder") if path.is_dir() else path.suffix.lower().lstrip(".")
            self.input_tree.insert(
                "", "end", iid=str(path), text=str(path), values=(kind, self._format_size(path))
            )
        for result in self._results:
            self._show_result(result, remember=False)
        self._set_running(running)

    # --- 入力リスト ---------------------------------------------------------

    def _add_archives(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(C.ARCHIVE_EXTENSIONS))
        selected = filedialog.askopenfilenames(
            title=t("dlg.pick_archives"),
            filetypes=[(t("dlg.archives"), patterns), (t("dlg.all_files"), "*.*")],
        )
        for path in selected:
            self._add_input(Path(path))

    def _add_folder(self) -> None:
        selected = filedialog.askdirectory(title=t("dlg.pick_folder"))
        if selected:
            self._add_input(Path(selected))

    def _add_input(self, path: Path) -> None:
        if path in self._inputs:
            return
        self._inputs.append(path)
        kind = t("input.kind_folder") if path.is_dir() else path.suffix.lower().lstrip(".")
        self.input_tree.insert(
            "", "end", iid=str(path), text=str(path), values=(kind, self._format_size(path))
        )

    def _remove_selected(self) -> None:
        for item in self.input_tree.selection():
            self.input_tree.delete(item)
            path = Path(item)
            if path in self._inputs:
                self._inputs.remove(path)

    def _clear_inputs(self) -> None:
        self.input_tree.delete(*self.input_tree.get_children())
        self._inputs.clear()

    def _format_size(self, path: Path) -> str:
        try:
            if path.is_file():
                total = path.stat().st_size
            else:
                total = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        except OSError:
            return "?"
        return f"{total / 1048576:.1f} MB"

    def _choose_log_dir(self) -> None:
        selected = filedialog.askdirectory(title=t("dlg.pick_log_dir"))
        if selected:
            self.var_log_dir.set(selected)
            self.var_write_log.set(True)

    # --- 実行 ---------------------------------------------------------------

    def _run_analyze(self) -> None:
        self._start_job(dry_run=True)

    def _run_execute(self) -> None:
        if self.var_delete_losers.get():
            if not messagebox.askyesno(
                t("dlg.confirm"), t("dlg.confirm_delete")
            ):
                return
        if self.var_replace.get():
            if not messagebox.askyesno(
                t("dlg.confirm"), t("dlg.confirm_replace")
            ):
                return
        self._start_job(dry_run=False)

    def _start_job(self, dry_run: bool) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo(t("dlg.busy_title"), t("dlg.busy"))
            return
        if not self._inputs:
            messagebox.showwarning(t("dlg.no_input_title"), t("dlg.no_input"))
            return

        try:
            options = self._build_options(dry_run)
        except ValueError as exc:
            messagebox.showerror(t("dlg.bad_options"), str(exc))
            return

        self._cancel.clear()
        self.result_tree.delete(*self.result_tree.get_children())
        self._review_rows.clear()
        self._results.clear()
        self._set_running(True)

        log_path = csv_path = None
        if self.var_write_log.get():
            log_dir = Path(self.var_log_dir.get()) if self.var_log_dir.get() else default_log_dir()
            log_path, csv_path = resolve_log_paths(log_dir, timestamp())
            self._last_log_path = log_path
        setup_logger(
            log_path=log_path,
            verbose=False,
            gui_queue=self._queue,
            quiet_console=True,
        )

        inputs = list(self._inputs)
        self._worker = threading.Thread(
            target=self._worker_main,
            args=(inputs, options, csv_path),
            daemon=True,
        )
        self._worker.start()

    def _build_options(self, dry_run: bool) -> pipeline.JobOptions:
        approved = {
            pair for pair, checked in self._review_rows.values() if checked
        } if not dry_run else set()
        options = pipeline.JobOptions(
            dry_run=dry_run,
            delete_losers=self.var_delete_losers.get(),
            replace=self.var_replace.get(),
            rar_to_zip=self.var_rar_to_zip.get(),
            verify_content=self.var_verify_content.get(),
            sample_pages=int(self.var_sample.get()),
            rar_level=int(self.var_rar_level.get()),
            quality_margin=float(self.var_quality_margin.get()),
            series_similarity=float(self.var_similarity.get()),
            series_review=float(self.var_review.get()),
            accept_review=self.var_accept_review.get(),
            approved_pairs=approved,
        )
        options.validate()
        return options

    def _worker_main(self, inputs, options: pipeline.JobOptions, csv_path: Optional[Path]) -> None:
        rows: list[list[object]] = []
        try:
            for index, input_path in enumerate(inputs, start=1):
                self._queue.put(("log", f"=== ({index}/{len(inputs)}) {input_path}"))
                job = pipeline.Pipeline(
                    options,
                    progress=lambda stage, current, total: self._queue.put(
                        ("progress", (stage, current, total))
                    ),
                    cancel_event=self._cancel,
                )
                result = job.run(input_path)
                rows.extend(pipeline.build_csv_rows(result))
                self._queue.put(("result", result))
                if self._cancel.is_set():
                    break
            if csv_path is not None:
                write_csv(csv_path, rows)
                self._queue.put(("log", t("log.csv", path=csv_path)))
        except Exception as exc:  # worker の例外も GUI に見せる
            self._queue.put(("log", t("log.error", error=exc)))
        finally:
            self._queue.put(("done", None))

    def _cancel_job(self) -> None:
        self._cancel.set()
        self._append_log(t("log.cancel_requested"))

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.button_analyze.configure(state=state)
        self.button_execute.configure(state=state)
        self.button_cancel.configure(state="normal" if running else "disabled")
        self.var_status.set(t("status.running") if running else t("status.idle"))

    # --- キュー処理 ---------------------------------------------------------

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "progress":
                    stage, current, total = payload  # type: ignore[misc]
                    self.var_status.set(f"{stage} {current}/{total}")
                    self.progress.configure(maximum=max(total, 1), value=current)
                elif kind == "result":
                    self._show_result(payload)  # type: ignore[arg-type]
                elif kind == "done":
                    self._set_running(False)
                    self.progress.configure(value=0)
        except queue.Empty:
            pass
        self.after(C.GUI_POLL_INTERVAL_MS, self._poll_queue)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > C.GUI_LOG_MAX_LINES:
            self.log_text.delete("1.0", f"{line_count - C.GUI_LOG_MAX_LINES}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _show_result(self, result: JobResult, remember: bool = True) -> None:
        if remember:
            self._results.append(result)
        if remember:
            self._append_log(result.summary_line())
        if result.output_path:
            self._last_output_dir = result.output_path.parent

        root_id = self.result_tree.insert(
            "", "end", text=str(result.input_path), values=(t("result.input"), "", "", result.summary_line())
        )
        for decision in result.decisions:
            group_id = self.result_tree.insert(
                root_id,
                "end",
                text=t("result.group", key=decision.group.key),
                values=(
                    t("result.duplicate"),
                    "",
                    "",
                    t(
                        "result.similarity",
                        value=decision.group.similarity,
                        notes=" ".join(decision.group.match_notes),
                    ),
                ),
            )
            members = []
            if decision.winner is not None:
                members.append((C.ROLE_WINNER, decision.winner))
            members += [(C.ROLE_LOSER, item) for item in decision.losers]
            members += [(C.ROLE_PENDING, item) for item in decision.undecided]
            for role, item in members:
                self.result_tree.insert(
                    group_id,
                    "end",
                    text=item.label(),
                    values=(
                        i18n.role_text(role),
                        item.page_count,
                        f"{item.median_pixels:.0f}",
                        decision.reasons.get(item.label(), decision.note),
                    ),
                )
            self.result_tree.item(group_id, open=True)

        for review in result.reviews:
            row_id = self.result_tree.insert(
                root_id,
                "end",
                text=f"{UNCHECKED} {review.describe()}",
                values=(i18n.role_text(C.ROLE_PENDING), "", "", t("result.approve_hint")),
            )
            self._review_rows[row_id] = (matching.pair_key(review.left, review.right), False)

        for warning in result.warnings if remember else ():
            self._append_log(t("log.warning", warning=warning))
        self.result_tree.item(root_id, open=True)

    def _on_result_click(self, event) -> None:
        row_id = self.result_tree.identify_row(event.y)
        if row_id not in self._review_rows:
            return
        pair, checked = self._review_rows[row_id]
        checked = not checked
        self._review_rows[row_id] = (pair, checked)
        text = self.result_tree.item(row_id, "text")
        marker = CHECKED if checked else UNCHECKED
        self.result_tree.item(row_id, text=marker + text[1:])

    # --- 補助 ---------------------------------------------------------------

    def _open_log(self) -> None:
        if self._last_log_path is None or not self._last_log_path.exists():
            messagebox.showinfo(t("dlg.no_log_title"), t("dlg.no_log"))
            return
        self._open_path(self._last_log_path)

    def _open_output_dir(self) -> None:
        if self._last_output_dir is None:
            messagebox.showinfo(t("dlg.no_output_title"), t("dlg.no_output"))
            return
        self._open_path(self._last_output_dir)

    def _open_path(self, path: Path) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except OSError as exc:
            messagebox.showerror(t("dlg.cannot_open"), f"{path}\n{exc}")

    def _load_settings(self) -> None:
        path = settings_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.var_write_log.set(bool(data.get("write_log", False)))
        self.var_log_dir.set(str(data.get("log_dir", "")))
        self.var_rar_to_zip.set(bool(data.get("rar_to_zip", False)))
        self.var_verify_content.set(bool(data.get("verify_content", False)))
        self.var_sample.set(int(data.get("sample", C.SAMPLE_PAGES)))
        self.var_rar_level.set(int(data.get("rar_level", C.RAR_COMPRESSION_LEVEL)))
        self.var_quality_margin.set(float(data.get("quality_margin", C.QUALITY_MARGIN)))
        self.var_similarity.set(float(data.get("similarity", C.SERIES_SIMILARITY_MIN)))
        self.var_review.set(float(data.get("review", C.SERIES_SIMILARITY_REVIEW)))
        self.var_language.set(str(data.get("language", i18n.DEFAULT_LANGUAGE)))

    def save_settings(self) -> None:
        path = settings_path()
        data = {
            "write_log": self.var_write_log.get(),
            "log_dir": self.var_log_dir.get(),
            "rar_to_zip": self.var_rar_to_zip.get(),
            "verify_content": self.var_verify_content.get(),
            "sample": int(self.var_sample.get()),
            "rar_level": int(self.var_rar_level.get()),
            "quality_margin": float(self.var_quality_margin.get()),
            "similarity": float(self.var_similarity.get()),
            "review": float(self.var_review.get()),
            "language": self.var_language.get(),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass


def main() -> int:
    root = tk.Tk()
    app = DedupeApp(root)

    def on_close() -> None:
        app.save_settings()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    return C.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
