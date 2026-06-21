"""
YouTube Channel Fingerprinter
-----------------------------
Pipeline:
  1. Extract video list from a YouTube channel/playlist URL via yt-dlp
  2. Optionally let the user untick videos they don't want
  3. Show a size/time estimate and confirm before proceeding
  4. Download each selected video as native m4a/opus (no transcode) in
     parallel into <output_dir>/<CHANNEL>_subfolder
  5. Probe each file with ffprobe; auto-split anything >= 5:00 in place
     using ffmpeg's silencedetect, or warn in red if splitting is disabled
  6. Confirm-clear the bat dir's texts/ and pklz-files/ folders if non-empty
  7. Run preparador.bat then creador.bat (both block until exit)
  8. List the resulting pklz-files folder and optionally open it

Requirements:
  - Python 3.10+
  - yt-dlp on PATH (`pip install yt-dlp`)
  - ffmpeg + ffprobe on PATH
  - node on PATH (used by yt-dlp's --js-runtimes node)
"""
from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk


# ---------------------------- helpers -----------------------------------------

def sanitize_name(name: str) -> str:
    """Make a string safe for folder names (alphanumeric + . _ -)."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return safe or "channel"


def extract_handle(url: str, fallback: str = "") -> str:
    """Pull a channel handle like '@Muzarkive' out of a YouTube URL.
    Falls back to /channel/<id> or /c/<name> or /user/<name> forms, then to
    `fallback` (a metadata-derived name), then to 'channel'. The returned
    string is filename-safe but intentionally keeps a leading '@' if present."""
    if url:
        # @handle form: youtube.com/@Muzarkive[/videos]
        m = re.search(r"/(@[^/?#]+)", url)
        if m:
            handle = m.group(1)
            # keep the @, sanitize the rest
            return "@" + sanitize_name(handle[1:])
        # /c/Name, /user/Name, /channel/UCxxxx forms
        m = re.search(r"/(?:c|user|channel)/([^/?#]+)", url)
        if m:
            return sanitize_name(m.group(1))
    if fallback:
        return sanitize_name(fallback)
    return "channel"


def check_dependency(cmd: str) -> bool:
    try:
        subprocess.run(
            [cmd, "--version"],
            capture_output=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except FileNotFoundError:
        return False


# ----- recent URLs persistence -----------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
RECENT_URLS_FILE = SCRIPT_DIR / "recent_urls.json"
RECENT_URLS_MAX = 10


def load_recent_urls() -> list[str]:
    """Load the most-recently-used URL list. Silently returns [] on any failure."""
    try:
        if RECENT_URLS_FILE.is_file():
            data = json.loads(RECENT_URLS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(x) for x in data][:RECENT_URLS_MAX]
    except Exception:
        pass
    return []


def save_recent_url(url: str) -> list[str]:
    """Push `url` to top of the recent list, dedupe, cap, persist. Returns new list."""
    urls = load_recent_urls()
    urls = [u for u in urls if u != url]
    urls.insert(0, url)
    urls = urls[:RECENT_URLS_MAX]
    try:
        RECENT_URLS_FILE.write_text(json.dumps(urls, indent=2), encoding="utf-8")
    except Exception:
        pass
    return urls


# ----- general config persistence --------------------------------------------

CONFIG_FILE = SCRIPT_DIR / "config.json"


def load_config() -> dict:
    """Load saved settings. Silently returns {} on any failure."""
    try:
        if CONFIG_FILE.is_file():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_config(cfg: dict) -> None:
    """Persist settings dict. Silently swallows write failures."""
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


# ----- splitter constants ----------------------------------------------------

SPLITTER_T7 = int(4.5 * 60)           # files shorter than 4:30 won't be split (precondition)
SPLITTER_T8 = 5 * 60                  # files this long or longer will be split
SPLITTER_MIN_SEGMENT = 90             # min seconds between cuts
SPLITTER_SILENCE_THRESHOLD = "-50dB"
SPLITTER_SILENCE_DURATION = 1
SPLITTER_EPS = 0.25                   # tolerance for duration-sum sanity check


def fmt_size(num_bytes: float) -> str:
    """Human-readable byte count."""
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def fmt_time(seconds: float) -> str:
    """Human-readable duration."""
    s = int(seconds)
    if s < 60:
        return f"~{s}s"
    if s < 3600:
        return f"~{s // 60} min"
    return f"~{s // 3600}h {(s % 3600) // 60}m"


# ---------------------------- main app ----------------------------------------

class FingerprinterApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("YouTube Channel Fingerprinter")
        self.root.geometry("1100x780")
        self.root.minsize(900, 480)

        self.log_queue: queue.Queue[tuple[str, str, str | None]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.cancel_flag = threading.Event()
        # Set to skip just the current channel in a queue run (vs cancel_flag
        # which aborts the entire batch).
        self.skip_flag = threading.Event()
        # Tracks where the most recent channel's pklz files landed, so a queue
        # run can open that folder once at the end.
        self._last_report_dir: Path | None = None

        self._build_ui()
        self._apply_config(load_config())
        self._poll_log_queue()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------- UI ---------------------------------------------

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        # ---- Source -------------------------------------------------------
        src = ttk.LabelFrame(self.root, text="Source")
        src.pack(fill="x", **pad)

        ttk.Label(src, text="YouTube URL (channel/playlist):").grid(
            row=0, column=0, sticky="w", padx=4, pady=4,
        )
        self.url_var = tk.StringVar()
        # Cap input at 200 chars. Pasting tens of thousands of characters
        # froze the GUI on the main thread; legitimate URLs (even with
        # playlist + tracking params) stay well under this limit.
        url_validator = self.root.register(
            lambda s: len(s) <= 200,
        )
        self.url_combo = ttk.Combobox(
            src, textvariable=self.url_var, values=load_recent_urls(),
            validate="key", validatecommand=(url_validator, "%P"),
        )
        self.url_combo.grid(row=0, column=1, sticky="we", padx=4, pady=4)
        self.add_queue_btn = ttk.Button(
            src, text="Add to queue", command=self._add_to_queue,
        )
        self.add_queue_btn.grid(row=0, column=2, padx=4)
        # Enter in the URL field adds to the queue too.
        self.url_combo.bind("<Return>", lambda _e: self._add_to_queue())

        ttk.Label(src, text="Output directory:").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self.output_dir_var = tk.StringVar()
        ttk.Entry(src, textvariable=self.output_dir_var).grid(row=1, column=1, sticky="we", padx=4, pady=4)
        ttk.Button(src, text="Browse...", command=lambda: self._browse(self.output_dir_var)).grid(row=1, column=2, padx=4)

        ttk.Label(src, text="Bat files directory:").grid(row=2, column=0, sticky="w", padx=4, pady=4)
        self.bat_dir_var = tk.StringVar()
        ttk.Entry(src, textvariable=self.bat_dir_var).grid(row=2, column=1, sticky="we", padx=4, pady=4)
        ttk.Button(src, text="Browse...", command=lambda: self._browse(self.bat_dir_var)).grid(row=2, column=2, padx=4)

        ttk.Label(src, text="Move pklzs to (optional):").grid(row=3, column=0, sticky="w", padx=4, pady=4)
        self.move_pklz_dir_var = tk.StringVar()
        ttk.Entry(src, textvariable=self.move_pklz_dir_var).grid(row=3, column=1, sticky="we", padx=4, pady=4)
        ttk.Button(src, text="Browse...", command=lambda: self._browse(self.move_pklz_dir_var)).grid(row=3, column=2, padx=4)

        src.columnconfigure(1, weight=1)

        # ---- Queue --------------------------------------------------------
        queue_box = ttk.LabelFrame(self.root, text="Channel queue")
        queue_box.pack(fill="x", **pad)

        q_inner = ttk.Frame(queue_box)
        q_inner.pack(fill="x", padx=4, pady=4)

        # Scrollable frame of checkbox rows (one per queued URL). The Listbox
        # widget can't host checkboxes, so we build rows manually in a canvas.
        q_list_frame = ttk.Frame(q_inner)
        q_list_frame.pack(side="left", fill="both", expand=True)
        self.queue_canvas = tk.Canvas(q_list_frame, height=96, highlightthickness=0)
        q_scroll = ttk.Scrollbar(
            q_list_frame, orient="vertical", command=self.queue_canvas.yview,
        )
        self.queue_canvas.configure(yscrollcommand=q_scroll.set)
        self.queue_canvas.pack(side="left", fill="both", expand=True)
        q_scroll.pack(side="right", fill="y")

        self.queue_rows_frame = ttk.Frame(self.queue_canvas)
        _q_inner_id = self.queue_canvas.create_window(
            (0, 0), window=self.queue_rows_frame, anchor="nw",
        )

        def _q_on_config(_e: object) -> None:
            self.queue_canvas.configure(scrollregion=self.queue_canvas.bbox("all"))
        self.queue_rows_frame.bind("<Configure>", _q_on_config)

        def _q_canvas_config(e: object) -> None:
            self.queue_canvas.itemconfigure(_q_inner_id, width=e.width)  # type: ignore[attr-defined]
        self.queue_canvas.bind("<Configure>", _q_canvas_config)

        def _q_wheel(event: object) -> None:
            delta = getattr(event, "delta", 0)
            if delta:
                self.queue_canvas.yview_scroll(int(-delta / 120), "units")
        self.queue_canvas.bind("<Enter>", lambda _e: self.queue_canvas.bind_all("<MouseWheel>", _q_wheel, add="+"))
        self.queue_canvas.bind("<Leave>", lambda _e: self.queue_canvas.unbind_all("<MouseWheel>"))

        # Queue control buttons stacked on the right.
        q_btns = ttk.Frame(q_inner)
        q_btns.pack(side="left", fill="y", padx=(8, 0))
        self.queue_import_btn = ttk.Button(
            q_btns, text="Import file...", width=14, command=self._import_queue_from_file,
        )
        self.queue_import_btn.pack(fill="x", pady=1)
        self.queue_remove_btn = ttk.Button(q_btns, text="Remove checked", width=14, command=self._queue_remove)
        self.queue_remove_btn.pack(fill="x", pady=1)
        self.queue_up_btn = ttk.Button(q_btns, text="Move up", width=14, command=lambda: self._queue_move(-1))
        self.queue_up_btn.pack(fill="x", pady=1)
        self.queue_down_btn = ttk.Button(q_btns, text="Move down", width=14, command=lambda: self._queue_move(1))
        self.queue_down_btn.pack(fill="x", pady=1)
        self.queue_clear_btn = ttk.Button(q_btns, text="Clear all", width=14, command=self._queue_clear)
        self.queue_clear_btn.pack(fill="x", pady=1)

        # Backing state. queue_urls holds the URLs; queue_checks holds a
        # BooleanVar per URL (checked = include in the run); queue_active is the
        # index last clicked, used as the target for Move up/down.
        self.queue_urls: list[str] = []
        self.queue_checks: list[tk.BooleanVar] = []
        self.queue_active: int | None = None

        # ---- Options (one labelled box, three rows) -----------------------
        opts = ttk.LabelFrame(self.root, text="Options")
        opts.pack(fill="x", **pad)

        # Row 1: parallel downloads
        opts_r1 = ttk.Frame(opts)
        opts_r1.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(opts_r1, text="Parallel downloads:").pack(side="left", padx=(0, 4))
        self.parallel_var = tk.IntVar(value=4)
        self.parallel_spin = ttk.Spinbox(
            opts_r1, from_=1, to=16, textvariable=self.parallel_var, width=5,
        )
        self.parallel_spin.pack(side="left")

        # Row 2: behavioural checkboxes
        opts_r2 = ttk.Frame(opts)
        opts_r2.pack(fill="x", padx=4, pady=2)
        self.verbose_var = tk.BooleanVar(value=True)
        self.open_folder_var = tk.BooleanVar(value=True)
        self.open_pklz_var = tk.BooleanVar(value=True)
        self.split_long_var = tk.BooleanVar(value=True)
        for label, var in (
            ("Verbose yt-dlp output", self.verbose_var),
            ("Open channel subfolder on start", self.open_folder_var),
            ("Open pklz-files folder when done", self.open_pklz_var),
            ("Split files longer than 4:59", self.split_long_var),
        ):
            ttk.Checkbutton(opts_r2, text=label, variable=var).pack(side="left", padx=(0, 14))

        # Row 3: filename template
        opts_r3 = ttk.Frame(opts)
        opts_r3.pack(fill="x", padx=4, pady=(2, 2))
        ttk.Label(opts_r3, text="Filename template:").pack(side="left", padx=(0, 4))
        self.filename_template_var = tk.StringVar(
            value="%(title)s [%(id)s].%(ext)s",
        )
        self.filename_template_entry = ttk.Entry(
            opts_r3, textvariable=self.filename_template_var,
        )
        self.filename_template_entry.pack(side="left", fill="x", expand=True)

        # Row 4: extra yt-dlp args (own row, full width)
        opts_r4 = ttk.Frame(opts)
        opts_r4.pack(fill="x", padx=4, pady=(2, 4))
        ttk.Label(opts_r4, text="Extra yt-dlp args:").pack(side="left", padx=(0, 4))
        self.extra_args_var = tk.StringVar(value="")
        self.extra_args_entry = ttk.Entry(opts_r4, textvariable=self.extra_args_var)
        self.extra_args_entry.pack(side="left", fill="x", expand=True)

        # ---- Action buttons (run controls left, diagnostic right) ---------
        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="Start queue", command=self._start)
        self.start_btn.pack(side="left", padx=(0, 4))
        self.bats_btn = ttk.Button(btns, text="Run Bats Only", command=self._start_bats_only)
        self.bats_btn.pack(side="left", padx=4)
        self.skip_btn = ttk.Button(btns, text="Skip current", command=self._skip_current, state="disabled")
        self.skip_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        # Test Connection is diagnostic — push to the right so it's visually separate.
        self.test_btn = ttk.Button(btns, text="Test Connection", command=self._start_test_connection)
        self.test_btn.pack(side="right", padx=(4, 0))

        # ---- Active downloads (fixed-height, scrollable) ------------------
        active_box = ttk.LabelFrame(self.root, text="Active downloads")
        active_box.pack(fill="x", **pad)

        # A fixed-height canvas holds the slot rows. With many parallel
        # downloads (up to 16) the rows scroll inside this fixed area instead
        # of stretching the panel and squashing the log box.
        active_canvas = tk.Canvas(active_box, height=96, highlightthickness=0)
        active_scroll = ttk.Scrollbar(
            active_box, orient="vertical", command=active_canvas.yview,
        )
        active_canvas.configure(yscrollcommand=active_scroll.set)
        active_canvas.pack(side="left", fill="x", expand=True, padx=(4, 0), pady=4)
        active_scroll.pack(side="right", fill="y", pady=4)

        # The frame that actually holds the slot labels lives inside the canvas.
        self.active_frame = ttk.Frame(active_canvas)
        active_inner_id = active_canvas.create_window(
            (0, 0), window=self.active_frame, anchor="nw",
        )

        def _active_on_config(_e: object) -> None:
            active_canvas.configure(scrollregion=active_canvas.bbox("all"))
        self.active_frame.bind("<Configure>", _active_on_config)

        def _active_canvas_config(e: object) -> None:
            active_canvas.itemconfigure(active_inner_id, width=e.width)  # type: ignore[attr-defined]
        active_canvas.bind("<Configure>", _active_canvas_config)

        # Mousewheel scrolls the panel only while the cursor is over it
        # (scoped via Enter/Leave so it never steals wheel events globally).
        def _active_wheel(event: object) -> None:
            delta = getattr(event, "delta", 0)
            if delta:
                active_canvas.yview_scroll(int(-delta / 120), "units")
        active_canvas.bind("<Enter>", lambda _e: active_canvas.bind_all("<MouseWheel>", _active_wheel, add="+"))
        active_canvas.bind("<Leave>", lambda _e: active_canvas.unbind_all("<MouseWheel>"))

        self.slot_vars: list[tk.StringVar] = []
        # placeholder line so the frame doesn't collapse before a run
        self._slot_placeholder = ttk.Label(
            self.active_frame, text="(no active downloads)", foreground="grey",
        )
        self._slot_placeholder.pack(anchor="w")

        # ---- Log -----------------------------------------------------------
        log_header = ttk.Frame(self.root)
        log_header.pack(fill="x", padx=8, pady=(4, 0))
        ttk.Label(log_header, text="Log").pack(side="left")

        # Pack the bottom-anchored widgets FIRST (status bar, then log buttons)
        # using side="bottom". Tk reserves their space before the expanding log,
        # so on small screens the log shrinks instead of pushing them off-screen.
        self.status_var = tk.StringVar(value="Idle.")
        ttk.Label(
            self.root, textvariable=self.status_var, relief="sunken", anchor="w",
        ).pack(side="bottom", fill="x")

        log_btns = ttk.Frame(self.root)
        log_btns.pack(side="bottom", fill="x", padx=8, pady=(0, 4))
        self.clear_btn = ttk.Button(log_btns, text="Clear log", command=self._clear_log)
        self.clear_btn.pack(side="left", padx=(0, 4))
        self.copy_btn = ttk.Button(log_btns, text="Copy Log", command=self._copy_log)
        self.copy_btn.pack(side="left", padx=4)

        # The log fills whatever vertical space is left between the top widgets
        # and the bottom-pinned buttons/status bar. A minimum height keeps it
        # usable; it shrinks (not the buttons) when the window is small.
        self.log_text = scrolledtext.ScrolledText(self.root, height=10, font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, padx=8, pady=4)
        self.log_text.configure(state="disabled")
        self.log_text.tag_configure("ytdlp", foreground="#1565c0")
        self.log_text.tag_configure("bat", foreground="#e67e22")
        self.log_text.tag_configure("warning", foreground="#c0392b")
        self.log_text.tag_configure("splitter", foreground="#16a085")
        self.log_text.tag_configure("ts", foreground="#888888")

    def _browse(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory()
        if path:
            var.set(path)

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _copy_log(self) -> None:
        contents = self.log_text.get("1.0", "end-1c")
        if not contents.strip():
            self._log("[!] Log is empty; nothing to copy.")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(contents)
            # Force-update the clipboard so the text persists after the app closes.
            self.root.update_idletasks()
            line_count = contents.count("\n") + (0 if contents.endswith("\n") else 1)
            self._log(f"[+] Log copied to clipboard ({line_count} line(s), {len(contents)} chars).")
        except tk.TclError as e:
            self._log(f"[!] Could not copy to clipboard: {e}")

    # ------------------------- threading bridge -------------------------------

    def _log(self, msg: str, tag: str | None = None) -> None:
        ts = time.strftime("[%H:%M:%S] ")
        # Auto-tag error lines red. Won't override an explicit tag.
        if tag is None and msg.lstrip().startswith("[X]"):
            tag = "warning"
        self.log_queue.put((ts, msg, tag))

    def _poll_log_queue(self) -> None:
        try:
            while True:
                ts, msg, tag = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", ts, "ts")
                if tag:
                    self.log_text.insert("end", msg + "\n", tag)
                else:
                    self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    def _set_status(self, text: str) -> None:
        self.root.after(0, self.status_var.set, text)

    # ------------------------- config load/save -------------------------------

    # (var_name, json_key, expected_type)
    _CONFIG_FIELDS: tuple = (
        ("output_dir_var", "output_dir", str),
        ("bat_dir_var", "bat_dir", str),
        ("move_pklz_dir_var", "move_pklz_dir", str),
        ("extra_args_var", "extra_args", str),
        ("filename_template_var", "filename_template", str),
        ("parallel_var", "parallel", int),
        ("verbose_var", "verbose", bool),
        ("open_folder_var", "open_folder", bool),
        ("split_long_var", "split_long", bool),
        ("open_pklz_var", "open_pklz", bool),
    )

    def _apply_config(self, cfg: dict) -> None:
        """Apply a loaded config dict to the relevant Tk vars. Bad values silently skipped."""
        for var_name, key, conv in self._CONFIG_FIELDS:
            if key not in cfg:
                continue
            try:
                getattr(self, var_name).set(conv(cfg[key]))
            except (ValueError, TypeError, AttributeError):
                pass
        # Restore the saved channel queue (crash recovery / persistence).
        saved_queue = cfg.get("queue")
        if isinstance(saved_queue, list):
            self.queue_urls = [str(u) for u in saved_queue if u]
            self.queue_checks = [tk.BooleanVar(value=True) for _ in self.queue_urls]
            self.queue_active = None
            self._refresh_queue()

    def _gather_config(self) -> dict:
        """Read current Tk var values into a serializable dict."""
        out: dict = {}
        for var_name, key, conv in self._CONFIG_FIELDS:
            try:
                out[key] = conv(getattr(self, var_name).get())
            except (ValueError, TypeError, AttributeError):
                pass
        # Persist the queue so it survives restarts.
        out["queue"] = list(self.queue_urls)
        return out

    def _on_close(self) -> None:
        """Persist settings, then exit. Confirm first if a worker is running."""
        if self.worker_thread is not None and self.worker_thread.is_alive():
            confirm = messagebox.askyesno(
                "Quit while running?",
                "A process is still running.\n\n"
                "Quitting now will stop the run and may leave background "
                "yt-dlp or ffmpeg processes alive briefly until they finish.\n\n"
                "Are you sure you want to quit?",
                icon="warning",
                default="no",
                parent=self.root,
            )
            if not confirm:
                return
            # Best-effort: signal the worker to stop at its next checkpoint.
            self.cancel_flag.set()
        save_config(self._gather_config())
        self.root.destroy()

    def _ask_yes_no(self, title: str, message: str) -> bool:
        """Show a yes/no messagebox from a worker thread, blocking until answered."""
        result: list[bool] = [False]
        done = threading.Event()

        def show() -> None:
            try:
                result[0] = messagebox.askyesno(title, message, parent=self.root)
            finally:
                done.set()

        self.root.after(0, show)
        done.wait()
        return result[0]

    def _ask_yes_no_timed(
        self,
        title: str,
        message: str,
        timeout_seconds: int,
        default_yes: bool = True,
    ) -> bool:
        """Yes/No dialog from a worker thread that auto-resolves after `timeout_seconds`.
        Returns `default_yes` on timeout, False if closed via the X (no auto-action)."""
        result: list[bool] = [False]
        done = threading.Event()
        timer_id: list[str | None] = [None]

        def show() -> None:
            try:
                dlg = tk.Toplevel(self.root)
                dlg.title(title)
                dlg.transient(self.root)
                dlg.resizable(False, False)

                def cancel_timer() -> None:
                    if timer_id[0] is not None:
                        try:
                            dlg.after_cancel(timer_id[0])
                        except Exception:
                            pass
                        timer_id[0] = None

                def on_close() -> None:
                    cancel_timer()
                    result[0] = False
                    done.set()
                    dlg.destroy()
                dlg.protocol("WM_DELETE_WINDOW", on_close)

                msg_frame = ttk.Frame(dlg, padding=14)
                msg_frame.pack(fill="both", expand=True)
                ttk.Label(
                    msg_frame, text=message, justify="left", wraplength=520,
                ).pack(anchor="w")

                action_word = "yes" if default_yes else "no"
                countdown_var = tk.StringVar(
                    value=f"Auto-{action_word} in {timeout_seconds // 60}:{timeout_seconds % 60:02d}..."
                )
                ttk.Label(
                    msg_frame, textvariable=countdown_var, foreground="#888",
                ).pack(anchor="w", pady=(8, 0))

                btn_frame = ttk.Frame(dlg, padding=(14, 0, 14, 14))
                btn_frame.pack(fill="x")

                def click(value: bool) -> None:
                    cancel_timer()
                    result[0] = value
                    done.set()
                    dlg.destroy()

                ttk.Button(btn_frame, text="Yes", command=lambda: click(True)).pack(side="left", padx=4)
                ttk.Button(btn_frame, text="No", command=lambda: click(False)).pack(side="left", padx=4)

                remaining = [timeout_seconds]

                def tick() -> None:
                    remaining[0] -= 1
                    if remaining[0] <= 0:
                        result[0] = default_yes
                        timer_id[0] = None
                        done.set()
                        dlg.destroy()
                    else:
                        m, s = divmod(remaining[0], 60)
                        countdown_var.set(f"Auto-{action_word} in {m}:{s:02d}...")
                        timer_id[0] = dlg.after(1000, tick)

                timer_id[0] = dlg.after(1000, tick)

                dlg.update_idletasks()
                rx, ry = self.root.winfo_x(), self.root.winfo_y()
                rw, rh = self.root.winfo_width(), self.root.winfo_height()
                dw, dh = dlg.winfo_width(), dlg.winfo_height()
                dlg.geometry(f"+{rx + (rw - dw) // 2}+{ry + (rh - dh) // 2}")
                dlg.grab_set()
            except Exception:
                done.set()

        self.root.after(0, show)
        done.wait()
        return result[0]

    # ------------------------- slot panel -------------------------------------

    def _init_slots(self, n: int) -> None:
        """Rebuild the active-downloads panel with n rows. Called from main thread."""
        for child in self.active_frame.winfo_children():
            child.destroy()
        self.slot_vars = []
        if n <= 0:
            ttk.Label(self.active_frame, text="(no active downloads)", foreground="grey").pack(anchor="w")
            return
        for i in range(n):
            var = tk.StringVar(value=f"slot {i + 1}: idle")
            ttk.Label(self.active_frame, textvariable=var, font=("Consolas", 9), anchor="w").pack(fill="x")
            self.slot_vars.append(var)

    def _update_slot(self, idx: int, text: str) -> None:
        """Thread-safe slot label update."""
        def apply() -> None:
            if 0 <= idx < len(self.slot_vars):
                self.slot_vars[idx].set(f"slot {idx + 1}: {text}")
        self.root.after(0, apply)

    def _confirm_estimate(
        self, entries: list[dict], workers: int, log_only: bool = False,
    ) -> bool:
        """Compute rough size/time estimate and ask the user to confirm.
        Returns True to continue, False to abort. If log_only is True, just
        logs the estimate and returns True without showing a dialog (used in
        queue mode so the batch runs unattended)."""
        n = len(entries)
        # `--flat-playlist` returns `duration` (seconds) for YouTube playlist/channel
        # entries most of the time. Sum what's there; missing entries don't count.
        durations = [float(e.get("duration") or 0) for e in entries]
        total_dur = sum(durations)
        known = sum(1 for d in durations if d > 0)

        # Audio at ~128 kbps = 16 KB/s ≈ ~1 MB/min.
        if total_dur > 0:
            est_bytes = total_dur * 16_000
            # Scale up if some entries had unknown duration (use known average)
            if known and known < n:
                avg = total_dur / known
                est_bytes += (n - known) * avg * 16_000
            size_str = f"~{fmt_size(est_bytes)}"
        else:
            size_str = "unknown"

        # Time heuristic: ~25s per video (network + remux), divided by workers.
        est_dl_seconds = (n * 25) / max(1, workers)
        time_str = fmt_time(est_dl_seconds)

        self._log(f"[+] Estimate: {n} videos, {size_str}, {time_str} at parallel={workers}")
        if log_only:
            return True
        msg = (
            f"{n} videos\n"
            f"{size_str} estimated\n"
            f"{time_str} at parallel={workers}\n\n"
            f"Note: estimates are rough — real size/time depends on bitrate, length, "
            f"and connection speed.\n\n"
            f"Proceed with download?"
        )
        return self._ask_yes_no("Confirm download", msg)

    def _select_subset(self, entries: list[dict]) -> list[dict] | None:
        """Show a checklist of video titles and return the subset the user kept.
        Returns None if the dialog was cancelled (X / Esc), an empty list if the
        user explicitly confirmed with everything unchecked, or a non-empty list
        on a normal confirm. Thread-safe — callable from a worker thread."""
        result: list[list[dict] | None] = [None]
        done = threading.Event()

        def show() -> None:
            try:
                dlg = tk.Toplevel(self.root)
                dlg.title(f"Select videos to download ({len(entries)} found)")
                dlg.transient(self.root)
                dlg.geometry("760x560")
                dlg.minsize(560, 400)

                def on_close() -> None:
                    result[0] = None
                    done.set()
                    dlg.destroy()
                dlg.protocol("WM_DELETE_WINDOW", on_close)

                # ---- top instructions + filter ----
                top = ttk.Frame(dlg, padding=(12, 12, 12, 6))
                top.pack(fill="x")
                ttk.Label(
                    top,
                    text=(
                        f"Untick any videos you don't want to download. "
                        f"All {len(entries)} are selected by default."
                    ),
                    wraplength=720, justify="left",
                ).pack(anchor="w")

                filter_row = ttk.Frame(dlg, padding=(12, 0, 12, 6))
                filter_row.pack(fill="x")
                ttk.Label(filter_row, text="Filter:").pack(side="left", padx=(0, 4))
                filter_var = tk.StringVar()
                filter_entry = ttk.Entry(filter_row, textvariable=filter_var)
                filter_entry.pack(side="left", fill="x", expand=True)
                count_var = tk.StringVar(value=f"{len(entries)} of {len(entries)} selected")
                ttk.Label(filter_row, textvariable=count_var, foreground="#666").pack(
                    side="right", padx=(8, 0),
                )

                # ---- selection toolbar ----
                tools = ttk.Frame(dlg, padding=(12, 0, 12, 6))
                tools.pack(fill="x")

                # ---- scrollable checkbox list ----
                list_frame = ttk.Frame(dlg, padding=(12, 0, 12, 0))
                list_frame.pack(fill="both", expand=True)

                canvas = tk.Canvas(list_frame, highlightthickness=0)
                scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
                inner = ttk.Frame(canvas)
                inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
                canvas.configure(yscrollcommand=scrollbar.set)
                canvas.pack(side="left", fill="both", expand=True)
                scrollbar.pack(side="right", fill="y")

                def on_inner_config(_e: object) -> None:
                    canvas.configure(scrollregion=canvas.bbox("all"))
                inner.bind("<Configure>", on_inner_config)

                def on_canvas_config(e: object) -> None:
                    # keep the inner frame as wide as the canvas
                    canvas.itemconfigure(inner_id, width=e.width)  # type: ignore[attr-defined]
                canvas.bind("<Configure>", on_canvas_config)

                def on_mousewheel(event: object) -> None:
                    delta = getattr(event, "delta", 0)
                    if delta:
                        canvas.yview_scroll(int(-delta / 120), "units")
                # Bind to the dialog and its scroll region only — not bind_all,
                # which would steal wheel events from the main window too.
                # Using <Enter>/<Leave> on the dialog so the wheel works only
                # while the cursor is over the selector window.
                wheel_binding: list[str | None] = [None]

                def attach_wheel(_e: object = None) -> None:
                    if wheel_binding[0] is None:
                        wheel_binding[0] = dlg.bind_all(
                            "<MouseWheel>", on_mousewheel, add="+",
                        )

                def detach_wheel(_e: object = None) -> None:
                    if wheel_binding[0] is not None:
                        try:
                            dlg.unbind_all("<MouseWheel>")
                        except Exception:
                            pass
                        wheel_binding[0] = None

                dlg.bind("<Enter>", attach_wheel)
                dlg.bind("<Leave>", detach_wheel)
                # cleanup on close — also unbinds if the user closes via X
                def cleanup_bindings() -> None:
                    detach_wheel()

                # build one row per entry
                vars_: list[tk.BooleanVar] = []
                row_widgets: list[ttk.Checkbutton] = []
                for i, entry in enumerate(entries):
                    var = tk.BooleanVar(value=True)
                    title = entry.get("title") or entry.get("id") or f"<#{i + 1}>"
                    dur = entry.get("duration")
                    if dur:
                        m, s = divmod(int(dur), 60)
                        label = f"  {m:>3}:{s:02d}   {title}"
                    else:
                        label = f"     ?:??   {title}"
                    cb = ttk.Checkbutton(inner, text=label, variable=var)
                    cb.pack(anchor="w", fill="x", padx=4, pady=1)
                    vars_.append(var)
                    row_widgets.append(cb)

                def update_count() -> None:
                    n_sel = sum(1 for v in vars_ if v.get())
                    visible = sum(1 for w in row_widgets if w.winfo_ismapped())
                    if visible == len(entries):
                        count_var.set(f"{n_sel} of {len(entries)} selected")
                    else:
                        count_var.set(
                            f"{n_sel} of {len(entries)} selected  "
                            f"(showing {visible})"
                        )
                for v in vars_:
                    v.trace_add("write", lambda *_: update_count())

                def apply_filter(*_: object) -> None:
                    needle = filter_var.get().strip().lower()
                    for entry, w in zip(entries, row_widgets):
                        title = (entry.get("title") or "").lower()
                        if not needle or needle in title:
                            w.pack(anchor="w", fill="x", padx=4, pady=1)
                        else:
                            w.pack_forget()
                    update_count()
                filter_var.trace_add("write", apply_filter)

                # toolbar buttons
                def select_all() -> None:
                    for entry, var, w in zip(entries, vars_, row_widgets):
                        if w.winfo_ismapped():
                            var.set(True)
                def deselect_all() -> None:
                    for entry, var, w in zip(entries, vars_, row_widgets):
                        if w.winfo_ismapped():
                            var.set(False)
                def invert() -> None:
                    for entry, var, w in zip(entries, vars_, row_widgets):
                        if w.winfo_ismapped():
                            var.set(not var.get())

                ttk.Button(tools, text="Select all", command=select_all).pack(side="left", padx=(0, 4))
                ttk.Button(tools, text="Deselect all", command=deselect_all).pack(side="left", padx=4)
                ttk.Button(tools, text="Invert", command=invert).pack(side="left", padx=4)

                # ---- bottom action buttons ----
                btn_row = ttk.Frame(dlg, padding=(12, 6, 12, 12))
                btn_row.pack(fill="x")

                def confirm() -> None:
                    cleanup_bindings()
                    chosen = [e for e, v in zip(entries, vars_) if v.get()]
                    result[0] = chosen
                    done.set()
                    dlg.destroy()

                def cancel() -> None:
                    cleanup_bindings()
                    result[0] = None
                    done.set()
                    dlg.destroy()

                ttk.Button(btn_row, text="Cancel", command=cancel).pack(side="right", padx=(4, 0))
                ttk.Button(btn_row, text="Download selected", command=confirm).pack(side="right")

                dlg.bind("<Escape>", lambda _e: cancel())

                dlg.update_idletasks()
                rx, ry = self.root.winfo_x(), self.root.winfo_y()
                rw, rh = self.root.winfo_width(), self.root.winfo_height()
                dw, dh = dlg.winfo_width(), dlg.winfo_height()
                dlg.geometry(f"+{rx + (rw - dw) // 2}+{ry + (rh - dh) // 2}")
                dlg.grab_set()
            except Exception:
                done.set()

        self.root.after(0, show)
        done.wait()
        return result[0]

    def _open_folder(self, path: Path) -> None:
        """Open a folder in the OS file manager. Best-effort."""
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
            self._log(f"[+] Opened folder: {path}")
        except Exception as e:  # noqa: BLE001
            self._log(f"[!] Could not open folder: {e}")

    def _extra_args(self) -> list[str]:
        """Parse the free-form extra-args field into a list, respecting quoting."""
        raw = self.extra_args_var.get().strip()
        if not raw:
            return []
        try:
            return shlex.split(raw, posix=False)
        except ValueError as e:
            self._log(f"[!] Could not parse 'Extra yt-dlp args': {e}. Ignoring.")
            return []

    @staticmethod
    def _parse_yt_dlp_line(line: str) -> tuple[str | None, str | None]:
        """Return (state, display_text) for an interesting yt-dlp output line, else (None, None)."""
        # [download]   2.4% of   25.93MiB at  349.69KiB/s ETA 01:14
        m = re.search(
            r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+~?\s*\S+\s+at\s+(\S+)\s+ETA\s+(\S+)",
            line,
        )
        if m:
            return "downloading", f"{m.group(1)}%  {m.group(2)}  ETA {m.group(3)}"
        # [download] 100% of   5.42MiB in 00:11
        m = re.search(r"\[download\]\s+100(?:\.0+)?%\s+of\s+\S+\s+in\s+(\S+)", line)
        if m:
            return "downloaded", f"100% (in {m.group(1)})"
        if "[download] Destination:" in line:
            return "downloading", "starting..."
        if "[ExtractAudio]" in line:
            return "extracting", "extracting audio..."
        if "has already been downloaded" in line:
            return "skipped", "already on disk"
        return None, None

    # ------------------------- queue management -------------------------------

    def _refresh_queue(self, active_index: int | None = None) -> None:
        """Rebuild the checkbox rows from self.queue_urls / self.queue_checks."""
        for child in self.queue_rows_frame.winfo_children():
            child.destroy()
        if active_index is not None:
            self.queue_active = active_index

        if not self.queue_urls:
            ttk.Label(
                self.queue_rows_frame, text="(queue is empty)", foreground="grey",
            ).pack(anchor="w", padx=2, pady=2)
            return

        for i, (url, var) in enumerate(zip(self.queue_urls, self.queue_checks)):
            row = ttk.Frame(self.queue_rows_frame)
            row.pack(fill="x", anchor="w")
            cb = ttk.Checkbutton(row, variable=var)
            cb.pack(side="left")
            # Highlight the active row (target for Move up/down) by prefixing it.
            marker = "\u25b6 " if i == self.queue_active else "   "
            lbl = ttk.Label(
                row, text=f"{marker}{i + 1}.  {url}", anchor="w",
            )
            lbl.pack(side="left", fill="x", expand=True)
            # Clicking the label (not the checkbox) selects the row as active.
            lbl.bind("<Button-1>", lambda _e, idx=i: self._queue_set_active(idx))

    def _queue_set_active(self, idx: int) -> None:
        self.queue_active = idx
        self._refresh_queue()

    def _add_to_queue(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            return
        if not url.startswith(("http://", "https://", "www.")):
            messagebox.showerror(
                "Invalid URL",
                "That doesn't look like a YouTube URL.\n\n"
                "Paste a channel, playlist, or video link.",
            )
            return
        if url in self.queue_urls:
            self._log(f"[!] Already in queue: {url}")
            return
        self.queue_urls.append(url)
        self.queue_checks.append(tk.BooleanVar(value=True))  # checked by default
        self._refresh_queue(active_index=len(self.queue_urls) - 1)
        # Remember this URL for the dropdown's recent list, and refresh it.
        recent = save_recent_url(url)
        self.url_combo.configure(values=recent)
        self.url_var.set("")  # clear the field for the next paste
        save_config(self._gather_config())

    # Recognizes a bare YouTube channel ID, e.g. UCxN0K3hMnvgtsoz9NN0Iq_Q.
    _CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
    # Recognizes a bare @handle, e.g. @Muzarkive.
    _HANDLE_RE = re.compile(r"^@[A-Za-z0-9._-]{2,30}$")

    @classmethod
    def _normalize_queue_entry(cls, token: str) -> str | None:
        """Turn one imported token into a usable queue URL, or None if it's
        not recognized. Accepts full http(s)/www URLs as-is, bare channel IDs
        (UCxxxxxxxxxxxxxxxxxxxxxx -> .../channel/<id>), and bare @handles
        (-> .../<handle>)."""
        token = token.strip()
        if not token or len(token) > 200:
            return None
        if token.startswith(("http://", "https://", "www.")):
            return token
        if cls._CHANNEL_ID_RE.match(token):
            return f"https://www.youtube.com/channel/{token}"
        if cls._HANDLE_RE.match(token):
            return f"https://www.youtube.com/{token}"
        return None

    def _import_queue_from_file(self) -> None:
        """Bulk-add channels to the queue from a text file. Accepts one entry
        per line (or multiple, separated by commas/semicolons/whitespace):
        full YouTube URLs, bare channel IDs (UCxxxxxxxxxxxxxxxxxxxxxx), or
        @handles. Blank lines and lines starting with '#' are ignored.
        Duplicates (already queued, or repeated in the file) are skipped."""
        path = filedialog.askopenfilename(
            title="Import channel queue from file",
            filetypes=[
                ("Text files", "*.txt"),
                ("CSV files", "*.csv"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        try:
            raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Could not read file", f"Failed to read:\n{path}\n\n{e}")
            return

        added = 0
        dupes = 0
        invalid: list[str] = []
        seen_this_import: set[str] = set()

        for lineno, raw_line in enumerate(raw.splitlines(), start=1):
            line = raw_line.strip().strip("\"'")
            if not line or line.startswith("#"):
                continue
            for token in re.split(r"[,;\s]+", line):
                if not token:
                    continue
                url = self._normalize_queue_entry(token)
                if url is None:
                    invalid.append(f"line {lineno}: {token!r}")
                    continue
                if url in self.queue_urls or url in seen_this_import:
                    dupes += 1
                    continue
                seen_this_import.add(url)
                self.queue_urls.append(url)
                self.queue_checks.append(tk.BooleanVar(value=True))
                added += 1

        if added:
            self._refresh_queue(active_index=len(self.queue_urls) - 1)
            save_config(self._gather_config())

        summary = f"[+] Imported {added} url(s) from {Path(path).name}"
        if dupes:
            summary += f", {dupes} duplicate(s) skipped"
        if invalid:
            summary += f", {len(invalid)} unrecognized line(s) skipped"
        self._log(summary)
        for entry in invalid[:20]:
            self._log(f"    not recognized - {entry}", tag="warning")
        if len(invalid) > 20:
            self._log(f"    ...and {len(invalid) - 20} more.", tag="warning")
        if not added and not invalid and not dupes:
            self._log("[!] File was empty; nothing imported.", tag="warning")

    def _queue_remove(self) -> None:
        """Remove all checked entries. If none are checked, remove the active row."""
        checked = [i for i, v in enumerate(self.queue_checks) if v.get()]
        if not checked:
            if self.queue_active is None:
                self._log("[!] Nothing checked to remove. Tick a box or click a row first.")
                return
            checked = [self.queue_active]
        # Delete from the end so indices stay valid.
        for i in sorted(checked, reverse=True):
            del self.queue_urls[i]
            del self.queue_checks[i]
        self.queue_active = None
        self._refresh_queue()
        save_config(self._gather_config())

    def _queue_move(self, delta: int) -> None:
        if self.queue_active is None:
            self._log("[!] Click a queue row first to select what to move.")
            return
        idx = self.queue_active
        new_idx = idx + delta
        if not (0 <= new_idx < len(self.queue_urls)):
            return
        self.queue_urls[idx], self.queue_urls[new_idx] = (
            self.queue_urls[new_idx], self.queue_urls[idx],
        )
        self.queue_checks[idx], self.queue_checks[new_idx] = (
            self.queue_checks[new_idx], self.queue_checks[idx],
        )
        self._refresh_queue(active_index=new_idx)
        save_config(self._gather_config())

    def _queue_clear(self) -> None:
        if not self.queue_urls:
            return
        if not messagebox.askyesno("Clear queue", "Remove all channels from the queue?"):
            return
        self.queue_urls.clear()
        self.queue_checks.clear()
        self.queue_active = None
        self._refresh_queue()
        save_config(self._gather_config())

    def _set_queue_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for btn in (
            self.add_queue_btn, self.queue_import_btn, self.queue_remove_btn,
            self.queue_up_btn, self.queue_down_btn, self.queue_clear_btn,
        ):
            btn.config(state=state)

    # ------------------------- pipeline control -------------------------------

    def _start(self) -> None:
        # Convenience: if the URL field has something, add it to the queue first.
        if self.url_var.get().strip():
            self._add_to_queue()

        if not self.queue_urls:
            messagebox.showerror(
                "Empty queue",
                "Add at least one YouTube channel/playlist URL to the queue first.",
            )
            return

        # Only checked entries run.
        checked_urls = [
            u for u, v in zip(self.queue_urls, self.queue_checks) if v.get()
        ]
        if not checked_urls:
            messagebox.showerror(
                "Nothing checked",
                "Tick the checkbox next to at least one channel to run it.",
            )
            return

        output_dir = self.output_dir_var.get().strip()
        bat_dir = self.bat_dir_var.get().strip()
        if not output_dir or not Path(output_dir).is_dir():
            messagebox.showerror("Missing input", "Pick a valid output directory.")
            return
        if not bat_dir or not Path(bat_dir).is_dir():
            messagebox.showerror("Missing input", "Pick a valid bat files directory.")
            return

        prep = Path(bat_dir) / "preparador.bat"
        creador = Path(bat_dir) / "creador.bat"
        if not prep.is_file() or not creador.is_file():
            messagebox.showerror(
                "Bat files not found",
                f"Could not find preparador.bat and/or creador.bat in:\n{bat_dir}",
            )
            return

        # Warn if running >1 channel without auto-move, since the pklz-files
        # folder is shared and each channel's output must be evacuated between
        # runs (the preflight clear would otherwise delete the prior channel's
        # results, or block on the not-empty prompt).
        move_dest = self.move_pklz_dir_var.get().strip()
        if len(checked_urls) > 1 and not move_dest:
            proceed = messagebox.askyesno(
                "No 'Move pklzs to' set",
                "You're queueing multiple channels but haven't set a "
                "'Move pklzs to' directory.\n\n"
                "Between channels the pklz-files folder is cleared, so each "
                "channel's renamed pklz files will be DELETED before the next "
                "channel runs unless they're moved out first.\n\n"
                "Set a destination directory to keep every channel's output.\n\n"
                "Continue anyway (only the last channel's pklz files will survive)?",
                icon="warning", default="no",
            )
            if not proceed:
                return

        self.cancel_flag.clear()
        self.skip_flag.clear()
        self.start_btn.config(state="disabled")
        self.bats_btn.config(state="disabled")
        self.test_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.skip_btn.config(state="normal")
        self._set_inputs_locked(True)
        self._set_queue_controls_enabled(False)

        save_config(self._gather_config())

        # Snapshot the checked URLs so edits mid-run don't matter.
        self.worker_thread = threading.Thread(
            target=self._run_queue,
            args=(checked_urls, Path(output_dir), Path(bat_dir)),
            daemon=True,
        )
        self.worker_thread.start()

    def _run_queue(self, urls: list[str], output_base: Path, bat_dir: Path) -> None:
        """Process each queued URL sequentially through the full pipeline.
        Skip-on-failure: a failed/skipped channel doesn't halt the batch."""
        results: list[tuple[str, str]] = []  # (url, status)
        try:
            total = len(urls)
            for i, url in enumerate(urls, start=1):
                if self.cancel_flag.is_set():
                    # Mark the rest as not-run.
                    for u in urls[i - 1:]:
                        results.append((u, "cancelled"))
                    break

                self.skip_flag.clear()
                self._log("=" * 60)
                self._log(f"[*] QUEUE {i}/{total}: {url}")
                self._log("=" * 60)
                self._set_status(f"Queue {i}/{total}: starting...")

                try:
                    status = self._run_pipeline(
                        url, output_base, bat_dir, queue_mode=True,
                        queue_position=(i, total),
                    )
                except Exception as e:  # noqa: BLE001
                    self._log(f"[X] Channel failed with error: {e!r}")
                    status = "failed"
                results.append((url, status or "done"))

            # Final summary.
            self._log("=" * 60)
            self._log("[+] QUEUE COMPLETE")
            done = sum(1 for _, s in results if s == "done")
            self._log(f"    {done}/{total} channel(s) completed.")
            for u, s in results:
                if s != "done":
                    tag = "warning" if s in ("failed", "skipped") else None
                    self._log(f"    [{s}] {u}", tag=tag)
            self._log("=" * 60)
            self._set_status(f"Queue done — {done}/{total} completed.")

            # Open the folder where pklz files ended up, once, at the very end
            # (per-channel opening was suppressed to avoid window spam). Honors
            # the "open pklz-files folder when done" checkbox.
            if (
                done > 0
                and not self.cancel_flag.is_set()
                and self.open_pklz_var.get()
                and getattr(self, "_last_report_dir", None) is not None
                and self._last_report_dir.is_dir()
            ):
                self._open_folder(self._last_report_dir)
        except Exception as e:  # noqa: BLE001
            self._log(f"[X] Queue error: {e!r}")
            self._set_status("Queue error.")
        finally:
            self.root.after(0, self._finish)

    def _start_bats_only(self) -> None:
        """Skip downloads entirely; run just preparador.bat + creador.bat + monitor."""
        bat_dir = self.bat_dir_var.get().strip()
        if not bat_dir or not Path(bat_dir).is_dir():
            messagebox.showerror("Missing input", "Pick a valid bat files directory.")
            return

        prep = Path(bat_dir) / "preparador.bat"
        creador = Path(bat_dir) / "creador.bat"
        if not prep.is_file() or not creador.is_file():
            messagebox.showerror(
                "Bat files not found",
                f"Could not find preparador.bat and/or creador.bat in:\n{bat_dir}",
            )
            return

        if not messagebox.askyesno(
            "Run bats only?",
            f"Run preparador.bat and creador.bat in:\n{bat_dir}\n\n"
            f"This skips the YouTube download step entirely. Make sure the audio "
            f"files preparador.bat needs are already in place.\n\nContinue?",
            parent=self.root,
        ):
            return

        self.cancel_flag.clear()
        self.start_btn.config(state="disabled")
        self.bats_btn.config(state="disabled")
        self.test_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self._set_inputs_locked(True)

        save_config(self._gather_config())

        self.worker_thread = threading.Thread(
            target=self._run_bats_only_pipeline,
            args=(Path(bat_dir),),
            daemon=True,
        )
        self.worker_thread.start()

    def _start_test_connection(self) -> None:
        """Run a battery of diagnostic checks: tools, versions, network."""
        self.cancel_flag.clear()
        self.start_btn.config(state="disabled")
        self.bats_btn.config(state="disabled")
        self.test_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self._set_inputs_locked(True)

        self.worker_thread = threading.Thread(
            target=self._run_test_connection,
            daemon=True,
        )
        self.worker_thread.start()

    def _run_test_connection(self) -> None:
        try:
            self._set_status("Running connection test...")
            self._log("=" * 60)
            self._log("[*] Connection / dependency test")
            self._log("=" * 60)

            # 1. Python + platform
            import platform
            self._log(
                f"[*] Python {platform.python_version()} on "
                f"{platform.system()} {platform.release()}"
            )
            try:
                self._log(f"    Tk version: {self.root.tk.call('info', 'patchlevel')}")
            except Exception:
                pass

            if self.cancel_flag.is_set():
                return

            # 2. yt-dlp version + update check
            self._log("[*] yt-dlp:")
            if check_dependency("yt-dlp"):
                self._run_diagnostic(["yt-dlp", "--version"], prefix="    version: ")
                if self.cancel_flag.is_set():
                    return
                self._log("    running 'yt-dlp -U' (update check, may take a moment)...")
                self._run_diagnostic(["yt-dlp", "-U"], prefix="      ", tag="ytdlp", timeout=90)
            else:
                self._log("    ! NOT FOUND on PATH", tag="warning")

            if self.cancel_flag.is_set():
                return

            # 3. ffmpeg / ffprobe
            for tool in ("ffmpeg", "ffprobe"):
                self._log(f"[*] {tool}:")
                if check_dependency(tool):
                    self._run_diagnostic(
                        [tool, "-version"], prefix="    ", first_line_only=True,
                    )
                else:
                    self._log("    ! NOT FOUND on PATH", tag="warning")
                if self.cancel_flag.is_set():
                    return

            # 4. node (for --js-runtimes node)
            self._log("[*] node (used by yt-dlp's --js-runtimes node):")
            if check_dependency("node"):
                self._run_diagnostic(["node", "--version"], prefix="    version: ")
            else:
                self._log(
                    "    ! NOT FOUND on PATH (yt-dlp may fail on JS-required extractors).",
                    tag="warning",
                )

            if self.cancel_flag.is_set():
                return

            # 5. Test info retrieval against a known stable video.
            test_url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
            self._log(f"[*] Fetching info for: {test_url}")
            self._log("    (YouTube's first-ever upload — the most stable test target.)")
            try:
                proc = subprocess.run(
                    ["yt-dlp", "--js-runtimes", "node",
                     "--dump-single-json", "--no-warnings", "--no-playlist",
                     test_url],
                    capture_output=True, text=True, check=False,
                    encoding="utf-8", errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=45,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    info = json.loads(proc.stdout)
                    self._log(f"    + Title:       {info.get('title')}")
                    self._log(f"    + Uploader:    {info.get('uploader')}")
                    self._log(f"    + Upload date: {info.get('upload_date')}")
                    dur = info.get("duration")
                    if dur:
                        m, s = divmod(int(dur), 60)
                        self._log(f"    + Duration:    {m}:{s:02d}")
                    vc = info.get("view_count")
                    if isinstance(vc, int):
                        self._log(f"    + Views:       {vc:,}")
                    fmts = info.get("formats") or []
                    audio_fmts = [
                        f for f in fmts
                        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
                    ]
                    self._log(f"    + Audio formats available: {len(audio_fmts)}")
                else:
                    err = (proc.stderr or "").strip()
                    self._log(f"    ! Info retrieval failed (exit {proc.returncode}).", tag="warning")
                    if err:
                        for line in err.splitlines()[:3]:
                            self._log(f"      {line[:200]}", tag="warning")
            except subprocess.TimeoutExpired:
                self._log("    ! Info retrieval timed out (network issue?).", tag="warning")
            except json.JSONDecodeError as e:
                self._log(f"    ! Could not parse yt-dlp JSON: {e}", tag="warning")
            except Exception as e:  # noqa: BLE001
                self._log(f"    ! Test failed: {e!r}", tag="warning")

            if self.cancel_flag.is_set():
                return

            # 6. Output dir checks
            out_dir = self.output_dir_var.get().strip()
            self._log(f"[*] Output directory: {out_dir or '(not set)'}")
            if out_dir:
                p = Path(out_dir)
                if p.is_dir():
                    try:
                        free = shutil.disk_usage(p).free
                        self._log(f"    + Free disk space: {fmt_size(free)}")
                    except Exception as e:  # noqa: BLE001
                        self._log(f"    ! Disk usage check failed: {e}", tag="warning")
                    test_file = p / ".write_test_yt_fingerprinter"
                    try:
                        test_file.write_text("ok", encoding="utf-8")
                        test_file.unlink()
                        self._log("    + Writable: yes")
                    except Exception as e:  # noqa: BLE001
                        self._log(f"    ! Not writable: {e}", tag="warning")
                else:
                    self._log("    ! Path does not exist or is not a directory.", tag="warning")

            # 7. Bat dir + bat presence
            bat_dir = self.bat_dir_var.get().strip()
            self._log(f"[*] Bat directory: {bat_dir or '(not set)'}")
            if bat_dir:
                p = Path(bat_dir)
                if p.is_dir():
                    for name in ("preparador.bat", "creador.bat"):
                        bp = p / name
                        if bp.is_file():
                            self._log(f"    + {name}: found ({bp.stat().st_size} bytes)")
                        else:
                            self._log(f"    ! {name}: NOT FOUND", tag="warning")
                else:
                    self._log("    ! Path does not exist or is not a directory.", tag="warning")

            # 8. Config file status
            self._log(f"[*] Config: {CONFIG_FILE}")
            self._log(
                f"    exists: {CONFIG_FILE.is_file()}, "
                f"writable: {os.access(CONFIG_FILE.parent, os.W_OK)}"
            )

            self._log("=" * 60)
            self._log("[+] Test complete.")
            self._log("=" * 60)
            self._set_status("Test complete.")

        except Exception as e:  # noqa: BLE001
            self._log(f"[X] Test error: {e!r}")
            self._set_status("Test error.")
        finally:
            self.root.after(0, self._finish)

    def _run_diagnostic(
        self,
        cmd: list[str],
        prefix: str = "",
        tag: str | None = None,
        timeout: int = 30,
        first_line_only: bool = False,
    ) -> None:
        """Run `cmd`, log its combined output line-by-line. Used by the test runner."""
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=timeout,
            )
        except FileNotFoundError:
            self._log(f"{prefix}! command not found: {cmd[0]}", tag="warning")
            return
        except subprocess.TimeoutExpired:
            self._log(f"{prefix}! timed out after {timeout}s", tag="warning")
            return

        out = (proc.stdout or "") + (proc.stderr or "")
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if first_line_only and lines:
            lines = lines[:1]
        if not lines:
            self._log(f"{prefix}(no output, exit code {proc.returncode})")
            return
        for ln in lines:
            self._log(f"{prefix}{ln}", tag=tag)

    def _cancel(self) -> None:
        self.cancel_flag.set()
        self._log("[!] Cancellation requested. The entire queue will stop at the next safe checkpoint.")

    def _skip_current(self) -> None:
        self.skip_flag.set()
        self._log("[!] Skip requested. Moving to the next channel at the next safe checkpoint.")

    def _set_inputs_locked(self, locked: bool) -> None:
        """Lock/unlock fields whose values are read mid-run, so the user can't
        change them after the pipeline has started using them.
        - parallel_spin: locked because workers count drives slot pool sizing
        - extra_args_entry / filename_template_entry: locked because they're
          baked into every yt-dlp invocation"""
        # Spinbox uses "disabled" (not "readonly", which still allows arrow keys)
        self.parallel_spin.config(state="disabled" if locked else "normal")
        # Entries support "readonly" — content stays visible but isn't editable
        entry_state = "readonly" if locked else "normal"
        self.extra_args_entry.config(state=entry_state)
        self.filename_template_entry.config(state=entry_state)

    def _finish(self) -> None:
        self.start_btn.config(state="normal")
        self.bats_btn.config(state="normal")
        self.test_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        self.skip_btn.config(state="disabled")
        self._set_inputs_locked(False)
        self._set_queue_controls_enabled(True)

    # ------------------------- pipeline ---------------------------------------

    def _run_pipeline(
        self,
        url: str,
        output_base: Path,
        bat_dir: Path,
        queue_mode: bool = False,
        queue_position: tuple[int, int] | None = None,
    ) -> str | None:
        """Run the full pipeline for one channel.

        Returns a status string ("done", "failed", "skipped", "cancelled") when
        called in queue mode; the queue runner uses it for the summary. When not
        in queue mode it calls _finish itself and the return value is ignored.

        In queue mode, interactive dialogs (subset selection, size estimate) are
        auto-confirmed so the batch can run unattended; the preflight clear
        prompts still appear but auto-resolve via their 2-minute timers."""
        qp = f"[{queue_position[0]}/{queue_position[1]}] " if queue_position else ""

        def stopped() -> str | None:
            """Return a status if we should bail, else None."""
            if self.cancel_flag.is_set():
                return "cancelled"
            if self.skip_flag.is_set():
                self._log(f"[!] {qp}Skipping this channel.", tag="warning")
                return "skipped"
            return None

        try:
            if not check_dependency("yt-dlp"):
                self._log("[X] yt-dlp not found on PATH. Install with: pip install yt-dlp")
                return "failed"
            if not check_dependency("ffmpeg"):
                self._log("[X] ffmpeg not found on PATH. Required for audio remux and splitting.")
                return "failed"

            # 1. Extract video list
            self._set_status(f"{qp}Extracting playlist info...")
            self._log(f"[*] Fetching info for: {url}")
            info = self._extract_info(url)
            if info is None:
                return "failed"
            if (s := stopped()):
                return s

            channel_name_raw = (
                info.get("channel")
                or info.get("uploader")
                or info.get("title")
                or "channel"
            )
            channel_name = sanitize_name(channel_name_raw)
            # Handle for naming pklz files — prefer the @handle from the URL,
            # fall back to the channel name yt-dlp reported.
            pklz_prefix = extract_handle(url, fallback=channel_name_raw)
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                entries = [info]  # single video fallback

            self._log(f"[+] Source: {channel_name_raw}  ({len(entries)} videos)")
            if (s := stopped()):
                return s

            # Resolve this channel's download subfolder now (needed for the
            # pre-download cleanup check below).
            target_folder_name = f"{channel_name}_subfolder"
            initial_folder = output_base / target_folder_name

            # 1.5 Pre-download cleanup: if this channel's subfolder already has
            #     content (e.g. a leftover partial run), ask to clear it. This
            #     runs in queue mode too — it only touches THIS channel's
            #     subfolder, never other channels' folders.
            if initial_folder.is_dir():
                if not self._preflight_clean(
                    initial_folder, ("",), label_for_root="download folder",
                ):
                    return "cancelled"

            # 1a. Subset selection — interactive only outside queue mode.
            #     In queue mode we download everything (can't cherry-pick
            #     unattended across many channels).
            if not queue_mode and len(entries) > 1:
                selected = self._select_subset(entries)
                if selected is None:
                    self._log("[X] Selection cancelled. Aborting.")
                    self._set_status("Aborted.")
                    return "cancelled"
                if not selected:
                    self._log("[X] No videos selected. Aborting.")
                    self._set_status("Aborted.")
                    return "failed"
                if len(selected) != len(entries):
                    self._log(
                        f"[+] Selection: {len(selected)} of {len(entries)} videos "
                        f"({len(entries) - len(selected)} skipped)."
                    )
                entries = selected

            # 1b. Size/time estimate — interactive only outside queue mode.
            workers = max(1, int(self.parallel_var.get()))
            if queue_mode:
                self._confirm_estimate(entries, workers, log_only=True)
            else:
                if not self._confirm_estimate(entries, workers):
                    self._log("[X] User declined the estimate. Aborting.")
                    self._set_status("Aborted.")
                    return "cancelled"

            # 2. Make download folder (path was resolved above for the cleanup check)
            initial_folder.mkdir(parents=True, exist_ok=True)
            self._log(f"[+] Download folder: {initial_folder}")

            self._log("[+] Audio mode: native m4a/opus (no transcode, fast)")

            # Open the channel's subfolder on start if requested. This applies
            # in queue mode too — it opens whichever channel is downloading now.
            if self.open_folder_var.get():
                self._open_folder(initial_folder)

            # 3. Parallel download (uses the same `workers` value shown in the estimate)
            self._set_status(f"{qp}Downloading 0/{len(entries)} (parallel={workers})...")
            ok, fail = self._download_parallel(entries, initial_folder, workers)
            self._log(f"[+] Downloads finished: {ok} OK, {fail} failed.")

            if (s := stopped()):
                return s
            if ok == 0:
                self._log("[X] No successful downloads. Aborting before bat scripts.")
                return "failed"

            # 4. Folder stays where it was downloaded; preparador.bat reads it from there.
            self._log(f"[+] Folder stays at: {initial_folder}")

            # 4a. Audio length sanity check (right after downloads finish)
            if not self._check_long_audio(initial_folder):
                return "cancelled"

            # 4b. Pre-flight: check that bat output folders are empty
            if not self._preflight_clean(bat_dir, ("texts", "pklz-files")):
                return "cancelled"

            # 5. preparador.bat
            self._set_status(f"{qp}Running preparador.bat...")
            self._log("[*] Running preparador.bat...")
            self._run_bat(bat_dir / "preparador.bat", bat_dir)
            if (s := stopped()):
                return s

            # 6. creador.bat (runs immediately — preparador has already exited)
            pklz_dir = bat_dir / "pklz-files"
            pklz_before = self._snapshot_pklz(pklz_dir)
            self._set_status(f"{qp}Running creador.bat...")
            self._log("[*] Running creador.bat...")
            self._run_bat(bat_dir / "creador.bat", bat_dir)

            # 6b. Rename the newly-created pklz files using the channel handle.
            self._rename_new_pklz(pklz_dir, pklz_before, pklz_prefix)

            # 6c. Optionally move the pklz files to a destination directory.
            move_dest = self.move_pklz_dir_var.get().strip()
            if move_dest:
                self._move_pklz_files(pklz_dir, Path(move_dest))

            # 6d. Delete this channel's download folder now that its audio has
            #     been fingerprinted and the pklz files saved. The next run
            #     recreates the folder if needed.
            self._clear_download_folder(initial_folder)

            # 7. Report + open the folder where the pklz files ended up:
            #    the move destination if one was set, otherwise pklz-files.
            #    In a multi-channel queue, opening after every channel would
            #    spam identical folder windows, so queue mode reports without
            #    opening here and opens once at the end of the whole queue.
            report_dir = Path(move_dest) if move_dest else pklz_dir
            report_label = "moved pklz files" if move_dest else "pklz-files"
            self._report_pklz(report_dir, label=report_label, open_folder=not queue_mode)
            # Remember where this channel's pklz files landed so the queue
            # runner can open the final location once at the end.
            self._last_report_dir = report_dir

            self._log(f"[+] {qp}Channel done.")
            self._set_status(f"{qp}Done.")
            return "done"
        except Exception as e:  # noqa: BLE001
            self._log(f"[X] Error: {e!r}")
            self._set_status("Error.")
            return "failed"
        finally:
            # In queue mode the queue runner owns the button state; only reset
            # here for standalone (non-queue) runs.
            if not queue_mode:
                self.root.after(0, self._finish)

    def _run_bats_only_pipeline(self, bat_dir: Path) -> None:
        """Skip downloads. Run preparador.bat -> creador.bat -> done."""
        try:
            self._log("[*] Running bats only (no download)...")

            # Pre-flight: texts/ and pklz-files/ must be empty
            if not self._preflight_clean(bat_dir, ("texts", "pklz-files")):
                return

            # preparador.bat
            self._set_status("Running preparador.bat...")
            self._log("[*] Running preparador.bat...")
            self._run_bat(bat_dir / "preparador.bat", bat_dir)
            if self.cancel_flag.is_set():
                return

            # creador.bat (runs immediately — preparador has already exited)
            pklz_dir = bat_dir / "pklz-files"
            pklz_before = self._snapshot_pklz(pklz_dir)
            self._set_status("Running creador.bat...")
            self._log("[*] Running creador.bat...")
            self._run_bat(bat_dir / "creador.bat", bat_dir)

            # Rename new pklz files. No URL here, so derive the prefix from a
            # *_subfolder in the bat dir if present, else use a generic name.
            prefix = self._derive_prefix_from_subfolder(bat_dir)
            self._rename_new_pklz(pklz_dir, pklz_before, prefix)

            # Optionally move the pklz files to a destination directory.
            move_dest = self.move_pklz_dir_var.get().strip()
            if move_dest:
                self._move_pklz_files(pklz_dir, Path(move_dest))

            # List + optionally open the relevant folder (destination if moved).
            if move_dest:
                self._report_pklz(Path(move_dest), label="moved pklz files")
            else:
                self._report_pklz(pklz_dir)

            self._log("[+] All done.")
            self._set_status("Done.")
        except Exception as e:  # noqa: BLE001
            self._log(f"[X] Error: {e!r}")
            self._set_status("Error.")
        finally:
            self.root.after(0, self._finish)

    # ------------------------- yt-dlp wrappers --------------------------------

    def _get_audio_duration(self, path: Path) -> float | None:
        """Return audio duration in seconds via ffprobe, or None if it can't be read."""
        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1",
                 str(path)],
                capture_output=True, text=True, check=True,
                encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return float(proc.stdout.strip())
        except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
            return None

    def _check_long_audio(self, folder: Path, threshold_seconds: int = 300) -> bool:
        """Probe every audio file in `folder`. If any is `threshold_seconds` or longer,
        either auto-split (if the checkbox is on) or just log a red warning. Always
        returns True so the pipeline continues; only returns False on cancel."""
        self._set_status("Checking audio durations...")
        self._log("[*] Checking audio file durations...")

        if not check_dependency("ffprobe"):
            self._log("[!] ffprobe not on PATH; skipping length check.")
            return True

        audio_exts = {".m4a", ".opus", ".mp3", ".webm", ".ogg", ".oga", ".aac", ".wav", ".flac"}
        audio_files = sorted(
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in audio_exts
        )
        if not audio_files:
            self._log("[!] No audio files found to check.")
            return True

        long_files: list[tuple[Path, float]] = []
        unreadable = 0
        for f in audio_files:
            if self.cancel_flag.is_set():
                return False
            dur = self._get_audio_duration(f)
            if dur is None:
                unreadable += 1
                continue
            if dur >= threshold_seconds:
                long_files.append((f, dur))

        if unreadable:
            self._log(f"[!] Could not read duration of {unreadable} file(s).")
        if not long_files:
            self._log(f"[+] All {len(audio_files)} files are under 5:00.")
            return True

        long_files.sort(key=lambda x: -x[1])  # longest first

        if not self.split_long_var.get():
            # Splitting disabled — just warn in red and continue.
            self._log(
                f"[!] WARNING: {len(long_files)} file(s) are 5:00 or longer. "
                f"Splitting is disabled — pklz creation may run into issues.",
                tag="warning",
            )
            for path, dur in long_files[:10]:
                mins, secs = divmod(int(dur), 60)
                self._log(f"    {mins:>3}:{secs:02d}  {path.name}", tag="warning")
            if len(long_files) > 10:
                self._log(f"    ... (+{len(long_files) - 10} more)", tag="warning")
            return True

        # Splitting enabled — split each long file in place.
        self._log(f"[*] Splitting {len(long_files)} file(s) longer than 5:00...", tag="splitter")
        for path, dur in long_files:
            if self.cancel_flag.is_set():
                return False
            self._split_file_in_place(path, dur)
        self._log("[+] Splitting complete.", tag="splitter")
        return True

    # ---------- splitter (ported from FILE_SPLITTER.py) -----------------------

    def _detect_silences(self, file: Path) -> list[tuple[float, float]]:
        """Return list of (start, end) silence ranges via ffmpeg's silencedetect filter."""
        try:
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-i", str(file),
                 "-af", f"silencedetect=noise={SPLITTER_SILENCE_THRESHOLD}:d={SPLITTER_SILENCE_DURATION}",
                 "-f", "null", "-"],
                capture_output=True, text=True, check=False,
                encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            self._log("[!] ffmpeg missing during silence detection.", tag="warning")
            return []

        silences: list[tuple[float, float]] = []
        start: float | None = None
        for line in (proc.stderr or "").splitlines():
            line = line.strip()
            if "silence_start:" in line:
                try:
                    start = float(line.split("silence_start:")[-1].strip())
                except ValueError:
                    start = None
            elif "silence_end:" in line and start is not None:
                try:
                    end = float(line.split("silence_end:")[-1].split("|")[0].strip())
                    silences.append((start, end))
                except ValueError:
                    pass
                start = None
        return silences

    @staticmethod
    def _choose_cut_points(
        raw_silences: list[tuple[float, float]],
        seg_start: float,
        seg_end: float,
        min_len: float = SPLITTER_MIN_SEGMENT,
    ) -> list[float]:
        """Select silence-start times inside (seg_start, seg_end) such that no
        resulting piece is shorter than `min_len`."""
        candidates = sorted(s[0] for s in raw_silences if seg_start < s[0] < seg_end)
        cuts: list[float] = []
        last_boundary = seg_start
        for i, t in enumerate(candidates):
            next_boundary = candidates[i + 1] if i + 1 < len(candidates) else seg_end
            if (t - last_boundary) >= min_len and (next_boundary - t) >= min_len:
                cuts.append(t)
                last_boundary = t
        return cuts

    def _split_segment(
        self,
        seg_start: float,
        seg_end: float,
        raw_silences: list[tuple[float, float]],
    ) -> list[tuple[float, float]]:
        """Recursively pick cut points until every piece is <= 5:00.
        Falls back to halfway-cut when no usable silences exist."""
        duration = seg_end - seg_start
        if duration <= SPLITTER_T8:
            return [(seg_start, seg_end)]

        cuts = self._choose_cut_points(raw_silences, seg_start, seg_end)
        if cuts:
            pieces: list[tuple[float, float]] = []
            last = seg_start
            for t in cuts:
                pieces.extend(self._split_segment(last, t, raw_silences))
                last = t
            pieces.extend(self._split_segment(last, seg_end, raw_silences))
            return pieces

        # No usable silences and piece is still > 5:00 — bisect.
        mid = (seg_start + seg_end) / 2.0
        self._log(
            f"  No usable silences in [{seg_start:.1f}s, {seg_end:.1f}s] "
            f"(dur={duration:.1f}s); cutting halfway at {mid:.1f}s.",
            tag="splitter",
        )
        left = self._split_segment(seg_start, mid, raw_silences)
        right = self._split_segment(mid, seg_end, raw_silences)
        return left + right

    def _ffmpeg_slice(self, src: Path, start: float, end: float, dst: Path) -> bool:
        """Stream-copy a slice [start, end] of src into dst. Returns True on success."""
        try:
            proc = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-i", str(src),
                 "-ss", f"{start:.6f}",
                 "-to", f"{end:.6f}",
                 "-c", "copy",
                 str(dst)],
                capture_output=True, text=True, check=False,
                encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode != 0:
                err = (proc.stderr or "").strip()[:200]
                self._log(f"  ! ffmpeg slice failed: {err}", tag="warning")
                return False
            return True
        except FileNotFoundError:
            self._log("[!] ffmpeg missing during slicing.", tag="warning")
            return False

    def _split_file_in_place(self, file_path: Path, duration: float) -> None:
        """Split one file using silence detection. Pieces land in the same folder
        with `_1`, `_2`, ... suffixes; the original is deleted on success."""
        mins, secs = divmod(int(duration), 60)
        self._log(
            f"[*] Splitting: {file_path.name}  ({mins}:{secs:02d})",
            tag="splitter",
        )

        if duration < SPLITTER_T7:
            # Shouldn't happen given the caller's filter, but mirror the original guard.
            self._log("  Skipping: shorter than 4:30.", tag="splitter")
            return

        self._set_status(f"Splitting {file_path.name}...")
        silences = self._detect_silences(file_path)
        self._log(f"  Found {len(silences)} silence region(s).", tag="splitter")

        if self.cancel_flag.is_set():
            return

        ranges = self._split_segment(0.0, duration, silences)
        ranges.sort(key=lambda r: r[0])

        total_out = sum(e - s for s, e in ranges)
        if abs(duration - total_out) > SPLITTER_EPS:
            self._log(
                f"  ! Sum of pieces ({total_out:.2f}s) differs from original "
                f"({duration:.2f}s) by {total_out - duration:+.2f}s.",
                tag="warning",
            )

        self._log(f"  Will produce {len(ranges)} piece(s):", tag="splitter")
        base = file_path.stem
        ext = file_path.suffix
        out_dir = file_path.parent

        written: list[Path] = []
        used_names: set[str] = {file_path.name}  # never collide with the source
        for i, (start, end) in enumerate(ranges, start=1):
            if self.cancel_flag.is_set():
                return
            # Build a piece name, bumping the index if it would collide with the
            # source file or an existing/already-written piece (ffmpeg's -y would
            # otherwise silently overwrite them).
            n = i
            out_path = out_dir / f"{base}_{n}{ext}"
            while out_path.name in used_names or (
                out_path.exists() and out_path.name != file_path.name
            ):
                n += 1
                out_path = out_dir / f"{base}_{n}{ext}"
            used_names.add(out_path.name)
            ms, ss = divmod(int(start), 60)
            me, se = divmod(int(end), 60)
            if self._ffmpeg_slice(file_path, start, end, out_path):
                written.append(out_path)
                self._log(
                    f"    + {out_path.name}  [{ms}:{ss:02d} \u2192 {me}:{se:02d}, "
                    f"dur {(end - start):.1f}s]",
                    tag="splitter",
                )

        if written:
            try:
                file_path.unlink()
                self._log(f"  - deleted original: {file_path.name}", tag="splitter")
            except Exception as e:  # noqa: BLE001
                self._log(f"[!] Could not delete original {file_path.name}: {e}", tag="warning")
        else:
            self._log(
                f"[!] No pieces produced for {file_path.name}; original kept.",
                tag="warning",
            )

    def _preflight_clean(
        self,
        bat_dir: Path,
        folder_names: tuple[str, ...],
        label_for_root: str = "",
    ) -> bool:
        """Check each given subfolder of bat_dir; if non-empty, ask user to clear it.
        Returns True if pipeline should continue, False to abort.

        If a name is "" (empty string) the base path itself is checked. In that
        case `label_for_root` overrides the displayed name (e.g. "output directory")."""
        for name in folder_names:
            if self.cancel_flag.is_set():
                return False
            folder = bat_dir if name == "" else bat_dir / name
            display_name = label_for_root if name == "" else f"'{name}'"
            if not folder.is_dir():
                continue
            contents = list(folder.iterdir())
            if not contents:
                continue

            self._log(
                f"[!] {display_name.capitalize() if name == '' else display_name} "
                f"already contains {len(contents)} item(s)."
            )
            preview = ", ".join(p.name for p in contents[:5])
            if len(contents) > 5:
                preview += f", ... (+{len(contents) - 5} more)"
            msg = (
                f"The {display_name} is not empty:\n"
                f"{folder}\n\n"
                f"Contents ({len(contents)}): {preview}\n\n"
                f"Delete all contents and continue?\n"
                f"(Click No to abort the run instead.)"
            )
            title = (
                "Output directory is not empty"
                if name == "" else f"'{name}' is not empty"
            )
            ok = self._ask_yes_no_timed(
                title, msg, timeout_seconds=120, default_yes=True,
            )
            if not ok:
                self._log(f"[X] User declined to clear {display_name}. Aborting.")
                self._set_status("Aborted.")
                return False
            self._log(f"[*] Clearing {display_name}...")

            # Re-read the folder right before deletion so any files added
            # while the user was deciding still get cleaned up.
            failed = 0
            for item in list(folder.iterdir()):
                try:
                    if item.is_dir() and not item.is_symlink():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                except Exception as e:  # noqa: BLE001
                    self._log(f"[!] Could not delete {item.name}: {e}")
                    failed += 1
            if failed:
                self._log(f"[!] Cleared {display_name} with {failed} failure(s).")
            else:
                self._log(f"[+] Cleared {display_name}.")
        return True

    def _extract_info(self, url: str) -> dict | None:
        # Stream one entry per line on stdout instead of buffering the whole
        # playlist into a single JSON dump. This lets us log a running count
        # so the user knows the script isn't frozen on huge channels.
        # Tab-separated fields rather than JSON-encoded — simpler and avoids
        # version-dependent format-template features.
        sep = "\x1f"  # ASCII unit separator, won't appear in titles
        template = sep.join((
            "%(id)s",
            "%(title)s",
            "%(duration)s",
            "%(url)s",
            "%(webpage_url)s",
            "%(playlist_title)s",
            "%(playlist_uploader)s",
            "%(playlist_channel)s",
            "%(channel)s",
        ))
        cmd = [
            "yt-dlp",
            "--js-runtimes", "node",
            "--flat-playlist",
            "--no-warnings",
            "--print", template,
            *self._extra_args(),
            url,
        ]

        entries: list[dict] = []
        meta: dict = {}
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as e:
            self._log(f"[X] yt-dlp launch failed: {e}")
            return None

        assert proc.stdout is not None
        last_log = time.monotonic()
        keys = (
            "id", "title", "duration", "url", "webpage_url",
            "playlist_title", "playlist_uploader", "playlist_channel", "channel",
        )
        try:
            for line in proc.stdout:
                if self.cancel_flag.is_set():
                    proc.terminate()
                    break
                line = line.rstrip("\n")
                if not line:
                    continue
                parts = line.split(sep)
                if len(parts) != len(keys):
                    continue  # malformed line, skip
                obj: dict = dict(zip(keys, parts))
                # yt-dlp prints the literal "NA" for missing fields.
                for k, v in list(obj.items()):
                    if v == "NA":
                        obj[k] = None
                # Coerce duration to a number where possible.
                if obj.get("duration") is not None:
                    try:
                        obj["duration"] = float(obj["duration"])
                    except (TypeError, ValueError):
                        obj["duration"] = None

                # First entry seeds the channel-level metadata.
                if not meta:
                    meta = {
                        "channel": obj.get("playlist_channel")
                                  or obj.get("channel")
                                  or obj.get("playlist_uploader"),
                        "uploader": obj.get("playlist_uploader") or obj.get("channel"),
                        "title": obj.get("playlist_title"),
                    }
                # Strip the playlist-level keys from each entry; the rest of the
                # pipeline only consumes id/title/duration/url/webpage_url.
                for k in ("playlist_title", "playlist_uploader",
                          "playlist_channel", "channel"):
                    obj.pop(k, None)
                entries.append(obj)

                # Throttle progress logs to ~1/sec so we don't spam the log.
                now = time.monotonic()
                if now - last_log >= 1.0:
                    self._set_status(f"Fetching channel info ({len(entries)} videos)...")
                    self._log(f"[*] Fetched {len(entries)} entries...")
                    last_log = now
        finally:
            proc.wait()

        if self.cancel_flag.is_set():
            return None
        if proc.returncode != 0 and not entries:
            err = (proc.stderr.read() if proc.stderr else "").strip()
            self._log(f"[X] yt-dlp info failed: {err[:500]}")
            return None

        # Log the final count if the throttled log missed it.
        self._log(f"[*] Done fetching: {len(entries)} entries total.")

        # Single-video URLs return one entry without playlist metadata.
        if not meta:
            single = entries[0] if entries else {}
            return {
                "channel": single.get("uploader") or single.get("channel"),
                "title": single.get("title"),
                "entries": entries,
            }
        meta["entries"] = entries
        return meta

    def _download_parallel(
        self,
        entries: list[dict],
        target_folder: Path,
        workers: int,
    ) -> tuple[int, int]:
        ok = 0
        fail = 0
        total = len(entries)
        done = 0

        # Number of visible slots = min(workers, total)
        slot_count = max(1, min(workers, total))
        self.root.after(0, self._init_slots, slot_count)

        # Slot pool: each worker grabs an index, returns it when finished.
        slot_pool: queue.Queue[int] = queue.Queue()
        for i in range(slot_count):
            slot_pool.put(i)

        def worker(entry: dict) -> bool:
            if self.cancel_flag.is_set():
                return False
            slot = slot_pool.get()
            try:
                return self._download_one(entry, slot, target_folder)
            finally:
                self._update_slot(slot, "idle")
                slot_pool.put(slot)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(worker, e): e for e in entries}
            for fut in as_completed(futures):
                if self.cancel_flag.is_set():
                    break
                try:
                    success = fut.result()
                except Exception as e:  # noqa: BLE001
                    # A worker raised (e.g. proc.terminate() mid-read on cancel).
                    # Count it as a failure rather than crashing the channel.
                    success = False
                    self._log(f"  ! Download worker error: {e!r}", tag="warning")
                if success:
                    ok += 1
                else:
                    fail += 1
                done += 1
                title = futures[fut].get("title", "?")
                tag = "OK" if success else "FAIL"
                self._log(f"  [{done}/{total}] {tag}: {title}")
                self._set_status(f"Downloading {done}/{total}...")
        return ok, fail

    def _download_one(
        self,
        entry: dict,
        slot: int,
        target_folder: Path,
    ) -> bool:
        video_url = entry.get("webpage_url") or entry.get("url") or entry.get("id", "")
        video_url = str(video_url)
        # If the entry only gave us a bare 11-character YouTube video ID
        # (typical for flat-playlist results), build the full URL. Anything
        # that already looks like a URL (has :// or starts with a known host)
        # passes through untouched.
        if video_url and "://" not in video_url and not video_url.startswith(
            ("youtu.be/", "www.", "youtube.com/")
        ):
            video_url = f"https://www.youtube.com/watch?v={video_url}"
        elif video_url.startswith(("youtu.be/", "www.", "youtube.com/")):
            video_url = f"https://{video_url}"
        title = entry.get("title", str(video_url))
        short_title = title if len(title) <= 40 else (title[:37] + "...")

        self._update_slot(slot, f"{short_title} | queued")
        self._log(f"[*] Starting: {title}")

        verbose = self.verbose_var.get()
        template = self.filename_template_var.get().strip() or "%(title)s [%(id)s].%(ext)s"
        cmd = [
            "yt-dlp",
            "--js-runtimes", "node",
            "-o", str(target_folder / template),
            "--no-playlist",
            "--ignore-errors",
            "--newline",  # one progress line per update instead of \r-rewriting
            "-f", "bestaudio[ext=m4a]/bestaudio",
            "--remux-video", "webm>opus",
        ]
        if not verbose:
            cmd.append("--no-warnings")
        cmd.extend(self._extra_args())
        cmd.append(video_url)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as e:
            self._log(f"[!] {title}: {e!r}")
            return False

        err_lines: list[str] = []
        assert proc.stdout is not None

        # Ticker thread: ffmpeg runs silently during ExtractAudio, so without this
        # the slot label appears frozen for the whole encode. We update with elapsed
        # seconds once per second from the moment we see the [ExtractAudio] line.
        extract_start: list[float | None] = [None]
        ticker_stop = threading.Event()

        def ticker() -> None:
            while not ticker_stop.is_set():
                es = extract_start[0]
                if es is not None:
                    elapsed = int(time.time() - es)
                    self._update_slot(slot, f"{short_title} | extracting audio... ({elapsed}s)")
                ticker_stop.wait(1.0)

        ticker_thread = threading.Thread(target=ticker, daemon=True)
        ticker_thread.start()

        try:
            for raw in proc.stdout:
                if self.cancel_flag.is_set():
                    proc.terminate()
                    break
                line = raw.rstrip()
                if not line:
                    continue
                if verbose:
                    self._log(f"  [s{slot + 1}] {line}", tag="ytdlp")
                if "[ExtractAudio]" in line and extract_start[0] is None:
                    extract_start[0] = time.time()
                _, display = self._parse_yt_dlp_line(line)
                if display is not None:
                    self._update_slot(slot, f"{short_title} | {display}")
                # accumulate error/warning lines so we can classify the failure later
                low = line.lower()
                if line.startswith("ERROR") or low.startswith("warning") or "error:" in low[:30]:
                    err_lines.append(line)
        finally:
            ticker_stop.set()
            ticker_thread.join(timeout=2.0)

        proc.wait()
        success = proc.returncode == 0 and not self.cancel_flag.is_set()
        if not success and not self.cancel_flag.is_set():
            joined = " ".join(err_lines)
            reason = self._classify_yt_dlp_error(joined)
            if reason:
                self._log(
                    f"  ! WARNING: skipping '{title}' — {reason}",
                    tag="warning",
                )
            elif err_lines:
                self._log(f"  ! Skipped '{title}' — {err_lines[-1][:300]}", tag="warning")
            else:
                self._log(f"  ! Skipped '{title}' (no error detail captured).", tag="warning")
        return success

    @staticmethod
    def _classify_yt_dlp_error(text: str) -> str | None:
        """Return a short human-readable reason if `text` matches a known yt-dlp
        failure mode, else None. Match is case-insensitive."""
        t = text.lower()
        # Order matters: more specific patterns first.
        patterns: list[tuple[str, str]] = [
            ("sign in to confirm your age", "age-restricted (login required)"),
            ("inappropriate for some users", "age-restricted"),
            ("age-restricted", "age-restricted"),
            ("members-only content", "members-only"),
            ("members only", "members-only"),
            ("this video is private", "private video"),
            ("video is private", "private video"),
            ("removed by the uploader", "removed by uploader"),
            ("account associated with this video has been terminated",
             "uploader account terminated"),
            ("violated youtube's", "removed for terms violation"),
            ("not available in your country", "geo-blocked"),
            ("blocked it in your country", "geo-blocked"),
            ("not made this video available in your country", "geo-blocked"),
            ("blocked on copyright grounds", "copyright takedown"),
            ("copyright claim", "copyright takedown"),
            ("this live event will begin", "scheduled livestream (not started)"),
            ("premieres in", "scheduled premiere (not aired)"),
            ("live event ended", "live event ended (no recording)"),
            ("this live stream recording is not available",
             "live stream recording unavailable"),
            ("video unavailable", "unavailable"),
            ("this video is unavailable", "unavailable"),
            ("video has been removed", "removed"),
            ("private video", "private video"),
            ("requested format is not available", "no audio format available"),
            ("unable to download webpage", "network error"),
            ("http error 403", "HTTP 403 forbidden"),
            ("http error 429", "HTTP 429 rate-limited"),
        ]
        for pat, label in patterns:
            if pat in t:
                return label
        return None

    # ------------------------- bat runner -------------------------------------

    def _run_bat(self, bat_path: Path, cwd: Path) -> None:
        try:
            proc = subprocess.Popen(
                ["cmd", "/c", str(bat_path)],
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                # Force UTF-8 decoding with replacement. Without this Python
                # picks the system codepage (cp1252 on most Windows installs)
                # which crashes on filenames containing non-Latin characters.
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self._log(f"  | {line}", tag="bat")
            proc.wait()
            self._log(f"[+] {bat_path.name} exited with code {proc.returncode}")
        except FileNotFoundError as e:
            self._log(f"[X] Could not run {bat_path.name}: {e}")

    # ------------------------- pklz reporting ---------------------------------

    @staticmethod
    def _snapshot_pklz(pklz_dir: Path) -> set[str]:
        """Return the set of .pklz filenames currently in pklz_dir (empty if none)."""
        if not pklz_dir.is_dir():
            return set()
        return {p.name for p in pklz_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pklz"}

    @staticmethod
    def _derive_prefix_from_subfolder(bat_dir: Path) -> str:
        """For the bats-only run (no URL), look for a '<name>_subfolder' folder
        in bat_dir and use <name> as the pklz prefix. Falls back to 'channel'."""
        try:
            for p in bat_dir.iterdir():
                if p.is_dir() and p.name.endswith("_subfolder"):
                    base = p.name[: -len("_subfolder")]
                    if base:
                        return base
        except Exception:
            pass
        return "channel"

    def _rename_new_pklz(
        self,
        pklz_dir: Path,
        before: set[str],
        prefix: str,
    ) -> None:
        """Rename only the .pklz files created since `before` to
        '<prefix>-1.pklz', '<prefix>-2.pklz', ... in sorted name order.
        Pre-existing files (those in `before`) are left untouched."""
        if not pklz_dir.is_dir():
            return
        current = {
            p.name for p in pklz_dir.iterdir()
            if p.is_file() and p.suffix.lower() == ".pklz"
        }
        new_names = sorted(current - before)
        if not new_names:
            self._log("[!] No new pklz files to rename.", tag="warning")
            return

        self._log(f"[*] Renaming {len(new_names)} new pklz file(s) to '{prefix}-N.pklz'...")

        # Two-phase rename to avoid collisions: first move everything to unique
        # temp names, then to the final names. This prevents a new file named
        # e.g. '@Muzarkive-1.pklz' (coincidentally) from clobbering a target.
        temp_paths: list[Path] = []
        for i, name in enumerate(new_names):
            src = pklz_dir / name
            tmp = pklz_dir / f".__renaming_{i}__.pklz.tmp"
            try:
                src.rename(tmp)
                temp_paths.append(tmp)
            except Exception as e:  # noqa: BLE001
                self._log(f"[!] Could not stage {name}: {e}", tag="warning")
                temp_paths.append(src)  # leave it where it is

        renamed = 0
        for i, tmp in enumerate(temp_paths, start=1):
            final = pklz_dir / f"{prefix}-{i}.pklz"
            # If a final name somehow already exists (pre-existing file with the
            # same scheme), bump until free.
            bump = i
            while final.exists() and final.name not in before:
                bump += 1
                final = pklz_dir / f"{prefix}-{bump}.pklz"
            try:
                tmp.rename(final)
                self._log(f"    {tmp.name} -> {final.name}")
                renamed += 1
            except Exception as e:  # noqa: BLE001
                self._log(f"[!] Could not finalize {tmp.name}: {e}", tag="warning")

        self._log(f"[+] Renamed {renamed} pklz file(s) with prefix '{prefix}'.")

    def _move_pklz_files(self, pklz_dir: Path, dest_dir: Path) -> None:
        """Move every .pklz file from pklz_dir into dest_dir. Creates dest_dir
        if needed. On a name collision in the destination, appends _2, _3, ..."""
        if not pklz_dir.is_dir():
            self._log(f"[!] pklz-files folder doesn't exist: {pklz_dir}", tag="warning")
            return
        files = [
            p for p in pklz_dir.iterdir()
            if p.is_file() and p.suffix.lower() == ".pklz"
        ]
        if not files:
            self._log("[!] No pklz files to move.", tag="warning")
            return

        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            self._log(f"[X] Could not create destination '{dest_dir}': {e}")
            return

        self._log(f"[*] Moving {len(files)} pklz file(s) to: {dest_dir}")
        moved = 0
        for src in sorted(files, key=lambda p: p.name):
            if self.cancel_flag.is_set():
                self._log("[!] Move cancelled partway through.", tag="warning")
                return
            target = dest_dir / src.name
            # Avoid clobbering an existing file in the destination.
            if target.exists():
                stem, suffix = src.stem, src.suffix
                n = 2
                while target.exists():
                    target = dest_dir / f"{stem}_{n}{suffix}"
                    n += 1
            try:
                shutil.move(str(src), str(target))
                self._log(f"    {src.name} -> {target.name}")
                moved += 1
            except Exception as e:  # noqa: BLE001
                self._log(f"[!] Could not move {src.name}: {e}", tag="warning")
        self._log(f"[+] Moved {moved} pklz file(s) to {dest_dir}.")

    def _clear_download_folder(self, folder: Path) -> None:
        """Delete the channel's download subfolder entirely after its audio has
        been fingerprinted. The next run recreates it, so removing the whole
        folder is cleaner than leaving an empty one behind."""
        if not folder.is_dir():
            return
        self._log(f"[*] Deleting download folder: {folder}")
        try:
            shutil.rmtree(folder)
            self._log(f"[+] Deleted download folder: {folder.name}")
        except Exception as e:  # noqa: BLE001
            self._log(f"[!] Could not delete download folder {folder.name}: {e}", tag="warning")

    def _report_pklz(self, pklz_dir: Path, label: str = "pklz-files", open_folder: bool = True) -> int:
        """One-shot inventory of a pklz folder. Lists contents, then optionally
        opens the folder. Returns the file count. `label` is just for the log."""
        if not pklz_dir.is_dir():
            self._log(f"[!] {label} folder doesn't exist: {pklz_dir}", tag="warning")
            return 0
        files = sorted(p.name for p in pklz_dir.iterdir() if p.is_file())
        self._log(f"[+] {label}: {len(files)} file(s) in {pklz_dir}")
        for name in files:
            self._log(f"    - {name}")
        if files and open_folder and self.open_pklz_var.get():
            self._open_folder(pklz_dir)
        return len(files)


# ---------------------------- entry point -------------------------------------

def main() -> None:
    root = tk.Tk()
    try:
        style = ttk.Style()
        names = style.theme_names()
        if "vista" in names:
            style.theme_use("vista")
        elif "clam" in names:
            style.theme_use("clam")
    except Exception:
        pass
    FingerprinterApp(root)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main() or 0)