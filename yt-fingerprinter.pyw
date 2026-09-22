"""
YouTube Channel Fingerprinter
-----------------------------
Pipeline:
  1. Extract video list from a YouTube channel/playlist URL via yt-dlp
  2. Optionally let the user untick videos they don't want
  3. Show a size/time estimate and confirm before proceeding
  4. Download each selected video as native m4a/opus (no transcode) in
     parallel into <output_dir>/<CHANNEL>_subfolder
  5. Probe each file with ffprobe; split anything over 12:00 in place into
     pieces of at least 6:00 (audfprint has less to match on the shorter a
     piece is), or warn in red if splitting is disabled
  6. Confirm-clear the bat dir's texts/ and pklz-files/ folders if non-empty
  7. Scan that folder for audio and fingerprint it with audfprint, several
     batches at a time (in-process: no .bat files and no node involved)
  8. List the resulting pklz-files folder and optionally open it

Requirements:
  - Python 3.10+
  - yt-dlp on PATH (`pip install yt-dlp`)
  - ffmpeg + ffprobe on PATH
  - node on PATH (used by yt-dlp's --js-runtimes node; no longer needed for
    fingerprinting, which now drives audfprint directly)
"""
from __future__ import annotations

import hashlib
import json
import math
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

# The floor every produced piece must clear. This is the whole point of the
# step, so it is one constant and the rest are derived from it. It replaced a
# ceiling of 5:00 with a minimum gap of 90s between silence-aligned cuts, which
# could leave pieces a minute and a half long; audfprint has less to work with
# the shorter a piece is, so six minutes is the floor used throughout.
# audfprint's own --ncores, pinned rather than exposed. It splits one batch's
# file list across processes and then merges their hash tables back through
# pipes, and that merge is serial, so it is the worst place to spend
# parallelism. Measured on this machine, 32 files, identical total worker
# count in every row:
#
#     1 job  x --ncores 8    59.2s
#     2 jobs x --ncores 4    42.1s
#     4 jobs x --ncores 2    36.9s
#     8 jobs x --ncores 1    28.1s
#
# Independent batches have nothing to merge, so "Fingerprint jobs at once" is
# the control worth having and this one only ever made things slower while
# multiplying peak memory. It used to be a spinbox; there is no setting now.
AUDFPRINT_NCORES = 1

SPLIT_MIN_CHUNK = 6 * 60

# The nominal body of a segment. Equal to the floor: a full body already clears
# it, and the tail merge handles the only piece that could come up short.
SPLIT_SEGMENT = SPLIT_MIN_CHUNK

# Below two segments' worth there is no cut that leaves two legal pieces, so a
# file shorter than this is left exactly as it is.
SPLIT_TRIGGER = 2 * SPLIT_MIN_CHUNK

SPLIT_AUDIO_EXTENSIONS = {
    ".m4a", ".opus", ".mp3", ".webm", ".ogg", ".oga", ".aac", ".wav", ".flac",
}

# ffmpeg infers its muxer from the output extension, and slices are written to a
# ".part" scratch name first, so the format has to be named explicitly. The
# format name equals the extension for most of these; the rest are mapped.
SPLIT_MUXERS = {
    ".m4a": "ipod", ".aac": "adts", ".oga": "ogg", ".opus": "opus",
}


# audfprint prints one of these per file it reads: "ingesting #12: C:\path\x.mp3 ..."
_INGESTING_RE = re.compile(r"ingesting #(\d+)\s*:\s*(.+?)\s*\.\.\.\s*$")


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
        # Sized against the actual screen rather than a fixed guess. The
        # step-by-step layout carries an explanation line under most controls,
        # which is worth roughly 170px of height over the old dense version; at
        # a hardcoded 780 the log box at the bottom got squeezed to nothing.
        # Clamped so a laptop at 1366x768 still gets a usable window instead of
        # one running off the bottom of the screen.
        want_w = min(1100, max(900, self.root.winfo_screenwidth() - 80))
        want_h = min(900, max(560, self.root.winfo_screenheight() - 70))
        self.root.geometry(f"{want_w}x{want_h}")
        self.root.minsize(900, 560)

        self.log_queue: queue.Queue[tuple[str, str, str | None]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.cancel_flag = threading.Event()
        # EVERY live child process, so Stop and Quit can actually end them.
        # This used to hold audfprint batches only, which meant Stop during the
        # download stage killed nothing at all: yt-dlp was noticed only when it
        # next wrote a line, and it writes nothing for the whole remux.
        self._fp_procs: list[subprocess.Popen] = []
        self._fp_procs_lock = threading.Lock()
        # batch id -> (files done, files in batch), for the aggregate progress line
        self._fp_progress: dict[int, tuple[int, int]] = {}
        self._fp_progress_lock = threading.Lock()
        self._fp_status_base = ""
        # Set to skip just the current channel in a queue run (vs cancel_flag
        # which aborts the entire batch).
        self.skip_flag = threading.Event()
        # Tracks where the most recent channel's pklz files landed, so a queue
        # run can open that folder once at the end.
        self._last_report_dir: Path | None = None

        self._build_ui()
        self._apply_config(load_config())
        # First run, or a config that predates this: the program already knows
        # where it lives, and audfprint sits next to it, so fill that in rather
        # than presenting an empty box the user has to guess at (and then
        # refusing to start until they do).
        if not self.bat_dir_var.get().strip():
            self.bat_dir_var.set(str(Path(__file__).resolve().parent))
        self._poll_log_queue()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------- UI ---------------------------------------------
    #
    # The people who use this are not necessarily technical, so the window is
    # laid out as the three things you actually do, in that order, and every
    # control that has a sensible default is folded away behind "Advanced
    # settings" instead of competing for attention with the ones that matter.
    # Each field carries a plain-language line underneath saying what it is
    # for; that is deliberately visible text rather than a tooltip, because a
    # tooltip only helps someone who already suspects there is something to
    # hover over.

    HELP_FONT = ("Segoe UI", 8)
    HELP_GREY = "#5f6b7a"
    HELP_WARN = "#b02a37"

    def _help(
        self, parent: tk.Misc, text: str,
        colour: str | None = None, wrap: int = 980,
    ) -> ttk.Label:
        """One small grey explanation line, to sit under the control it explains."""
        return ttk.Label(
            parent, text=text, font=self.HELP_FONT,
            foreground=colour or self.HELP_GREY,
            wraplength=wrap, justify="left",
        )

    def _folder_row(
        self, parent: ttk.Frame, row: int, label: str,
        var: tk.StringVar, help_text: str,
    ) -> ttk.Label:
        """Label + entry + Browse, with an explanation line beneath it.

        Returns the explanation label so callers can keep a reference and
        rewrite it later (the fingerprints folder does this to warn when it is
        needed but empty)."""
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", padx=4, pady=(6, 0),
        )
        ttk.Entry(parent, textvariable=var).grid(
            row=row, column=1, sticky="we", padx=4, pady=(6, 0),
        )
        ttk.Button(
            parent, text="Browse...", command=lambda: self._browse(var),
        ).grid(row=row, column=2, padx=4, pady=(6, 0))
        hint = self._help(parent, help_text)
        hint.grid(row=row + 1, column=1, columnspan=2, sticky="w", padx=4, pady=(1, 2))
        return hint

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        # ---- Step 1: what to fingerprint ----------------------------------
        src = ttk.LabelFrame(self.root, text="  Step 1   What do you want to fingerprint?  ")
        src.pack(fill="x", **pad)

        top = ttk.Frame(src)
        top.pack(fill="x", padx=4, pady=(6, 0))
        ttk.Label(top, text="Link:").pack(side="left", padx=(0, 4))
        self.url_var = tk.StringVar()
        # Cap input at 200 chars. Pasting tens of thousands of characters
        # froze the GUI on the main thread; legitimate URLs (even with
        # playlist + tracking params) stay well under this limit.
        url_validator = self.root.register(lambda s: len(s) <= 200)
        self.url_combo = ttk.Combobox(
            top, textvariable=self.url_var, values=load_recent_urls(),
            validate="key", validatecommand=(url_validator, "%P"),
        )
        self.url_combo.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.add_queue_btn = ttk.Button(
            top, text="Add to list", command=self._add_to_queue,
        )
        self.add_queue_btn.pack(side="left")
        # Enter in the URL field adds to the list too.
        self.url_combo.bind("<Return>", lambda _e: self._add_to_queue())

        self._help(
            src,
            "A YouTube channel or playlist, or an archive.org page. Press Add to "
            "list (or hit Enter). Add as many as you like — they are worked "
            "through from top to bottom.",
        ).pack(anchor="w", padx=8, pady=(2, 4))

        ttk.Label(src, text="Your list:").pack(anchor="w", padx=8, pady=(2, 0))

        q_inner = ttk.Frame(src)
        q_inner.pack(fill="x", padx=4, pady=(2, 6))

        # Scrollable frame of checkbox rows (one per queued URL). The Listbox
        # widget can't host checkboxes, so we build rows manually in a canvas.
        q_list_frame = ttk.Frame(q_inner)
        q_list_frame.pack(side="left", fill="both", expand=True)
        self.queue_canvas = tk.Canvas(q_list_frame, height=72, highlightthickness=0)
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

        # List control buttons stacked on the right.
        q_btns = ttk.Frame(q_inner)
        q_btns.pack(side="left", fill="y", padx=(8, 0))
        self.queue_import_btn = ttk.Button(
            q_btns, text="Import from file...", width=18, command=self._import_queue_from_file,
        )
        self.queue_import_btn.pack(fill="x", pady=1)
        self.queue_remove_btn = ttk.Button(q_btns, text="Remove ticked", width=18, command=self._queue_remove)
        self.queue_remove_btn.pack(fill="x", pady=1)
        self.queue_up_btn = ttk.Button(q_btns, text="Move up", width=18, command=lambda: self._queue_move(-1))
        self.queue_up_btn.pack(fill="x", pady=1)
        self.queue_down_btn = ttk.Button(q_btns, text="Move down", width=18, command=lambda: self._queue_move(1))
        self.queue_down_btn.pack(fill="x", pady=1)
        self.queue_clear_btn = ttk.Button(q_btns, text="Clear the list", width=18, command=self._queue_clear)
        self.queue_clear_btn.pack(fill="x", pady=1)

        # Backing state. queue_urls holds the URLs; queue_checks holds a
        # BooleanVar per URL (checked = include in the run); queue_active is the
        # index last clicked, used as the target for Move up/down.
        self.queue_urls: list[str] = []
        self.queue_checks: list[tk.BooleanVar] = []
        self.queue_active: int | None = None

        # ---- Step 2: where things go --------------------------------------
        dirs = ttk.LabelFrame(self.root, text="  Step 2   Where should the files go?  ")
        dirs.pack(fill="x", **pad)

        self.output_dir_var = tk.StringVar()
        self.bat_dir_var = tk.StringVar()
        self.move_pklz_dir_var = tk.StringVar()

        self._folder_row(
            dirs, 0, "Working folder for audio:", self.output_dir_var,
            "Audio is downloaded here while it works — and deleted again once a "
            "link has been fingerprinted. Nothing you want to keep should live here.",
        )
        self._folder_row(
            dirs, 2, "This program's folder:", self.bat_dir_var,
            "The folder holding this program and audfprint. Filled in for you; "
            "change it only if you moved things around.",
        )
        self.move_hint = self._folder_row(
            dirs, 4, "Keep finished fingerprints in:", self.move_pklz_dir_var,
            "Where your finished .pklz files are collected.",
        )
        dirs.columnconfigure(1, weight=1)

        # The hint above turns into a warning when the list holds more than one
        # link and no destination is set, because that is the case where the
        # results of every link but the last are quietly thrown away.
        self.move_pklz_dir_var.trace_add("write", lambda *_a: self._update_move_hint())

        # ---- Step 3: go ----------------------------------------------------
        go = ttk.LabelFrame(self.root, text="  Step 3   Start  ")
        go.pack(fill="x", **pad)

        btns = ttk.Frame(go)
        btns.pack(fill="x", padx=4, pady=(6, 0))
        self.start_btn = ttk.Button(btns, text="Download and fingerprint", command=self._start)
        self.start_btn.pack(side="left", padx=(0, 4))
        self.skip_btn = ttk.Button(btns, text="Skip this link", command=self._skip_current, state="disabled")
        self.skip_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(btns, text="Stop", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        # Diagnostic rather than part of the run — pushed right, visually apart.
        self.test_btn = ttk.Button(btns, text="Check my setup", command=self._start_test_connection)
        self.test_btn.pack(side="right", padx=(4, 0))

        # Second row: the two ways of working on audio that is already on disk.
        # They are separate buttons rather than one button plus a setting
        # because the difference is one you do not want to get wrong by
        # accident -- one rewrites your files in place, the other never touches
        # them -- and because a setting buried under Advanced is not something
        # you would find when you needed it.
        disk = ttk.Frame(go)
        disk.pack(fill="x", padx=4, pady=(6, 0))
        ttk.Label(disk, text="Audio already on disk:").pack(side="left", padx=(0, 8))
        self.bats_btn = ttk.Button(
            disk, text="Split + fingerprint",
            command=lambda: self._start_bats_only(split=True),
        )
        self.bats_btn.pack(side="left", padx=4)
        self.fp_only_btn = ttk.Button(
            disk, text="Fingerprint only (already split)",
            command=lambda: self._start_bats_only(split=False),
        )
        self.fp_only_btn.pack(side="left", padx=4)

        self._help(
            go,
            "Download and fingerprint does the whole job for every ticked link. "
            "For audio you already have, Split + fingerprint cuts anything over "
            "12 minutes first, while Fingerprint only skips the length check "
            "altogether — far quicker over a collection that is already in "
            "pieces, since nothing has to be examined. Not sure everything is "
            "installed? Press Check my setup.",
        ).pack(anchor="w", padx=8, pady=(3, 6))

        # ---- Advanced (folded away) ----------------------------------------
        self.adv_holder = ttk.Frame(self.root)
        self.adv_holder.pack(fill="x", padx=8, pady=(2, 0))
        self.adv_open = False
        self.adv_btn = ttk.Button(
            self.adv_holder, text="▸  Advanced settings",
            width=24, command=self._toggle_advanced,
        )
        self.adv_btn.pack(side="left")
        self._help(
            self.adv_holder,
            "Everything here already has a sensible default.",
        ).pack(side="left", padx=8)

        # Advanced settings get their own small window instead of folding out
        # inside the main one. Expanding them inline pushed the progress panel,
        # the log and the status bar off the bottom of the screen, and the log
        # is the thing you actually watch while it runs. The window is built
        # once and only hidden, never destroyed, so every widget reference in
        # here stays valid for the code that disables these controls mid-run.
        self.adv_win = tk.Toplevel(self.root)
        self.adv_win.title("Advanced settings")
        self.adv_win.transient(self.root)
        self.adv_win.resizable(False, False)
        self.adv_win.protocol("WM_DELETE_WINDOW", self._hide_advanced)
        self.adv_win.withdraw()
        self.adv_frame = ttk.Frame(self.adv_win)
        self.adv_frame.pack(fill="both", expand=True)

        # Row 1: how hard to work the machine
        opts_r1 = ttk.Frame(self.adv_frame)
        opts_r1.pack(fill="x", padx=4, pady=(6, 2))
        ttk.Label(opts_r1, text="Downloads at once:").pack(side="left", padx=(0, 4))
        self.parallel_var = tk.IntVar(value=4)
        self.parallel_spin = ttk.Spinbox(
            opts_r1, from_=1, to=16, textvariable=self.parallel_var, width=5,
        )
        self.parallel_spin.pack(side="left")

        # Concurrent batches is the one that matters. Measured here: the old
        # single sequential audfprint did 32 files in 117s; eight concurrent
        # batches did the same 32 in 28s. It is capped rather than opened up
        # because a 1000-file batch peaks around 5.5 GB, so 4 is roughly 22 GB
        # of this box's 64 GB and leaves room for everything else running.
        ttk.Label(opts_r1, text="   Fingerprint jobs at once:").pack(side="left", padx=(12, 4))
        self.fp_concurrency_var = tk.IntVar(value=4)
        self.fp_concurrency_spin = ttk.Spinbox(
            opts_r1, from_=1, to=16, textvariable=self.fp_concurrency_var, width=5,
        )
        self.fp_concurrency_spin.pack(side="left")

        # There is deliberately no control for audfprint's own --ncores. It is
        # pinned to AUDFPRINT_NCORES; see that constant for the measurements.

        # Files per .pklz. Bigger means fewer, larger shards, which matters
        # downstream: a matcher reloads every .pklz on every run, so hundreds of
        # small ones pay that cost hundreds of times.
        ttk.Label(opts_r1, text="   Recordings per file:").pack(side="left", padx=(12, 4))
        self.batch_size_var = tk.IntVar(value=1000)
        self.batch_size_spin = ttk.Spinbox(
            opts_r1, from_=50, to=5000, increment=50,
            textvariable=self.batch_size_var, width=7,
        )
        self.batch_size_spin.pack(side="left")
        self._help(opts_r1, "(1000 recommended)").pack(side="left", padx=(6, 0))

        self._help(
            self.adv_frame,
            "Raise the first two to use more of the machine; lower them if "
            "downloads start failing or memory runs short. Recordings per file "
            "is best left alone: a matching tool reloads every .pklz each time "
            "it runs, so many small ones slow every future search.",
        ).pack(anchor="w", padx=8, pady=(0, 4))

        # Row 2: behavioural checkboxes
        opts_r2 = ttk.Frame(self.adv_frame)
        opts_r2.pack(fill="x", padx=4, pady=2)
        self.verbose_var = tk.BooleanVar(value=True)
        self.open_folder_var = tk.BooleanVar(value=True)
        self.open_pklz_var = tk.BooleanVar(value=True)
        self.split_long_var = tk.BooleanVar(value=True)
        for label, var in (
            ("Split long recordings after downloading (recommended)", self.split_long_var),
            ("Show every line of download output", self.verbose_var),
            ("Open the audio folder when a link starts", self.open_folder_var),
            ("Open the results folder when it finishes", self.open_pklz_var),
        ):
            ttk.Checkbutton(opts_r2, text=label, variable=var).pack(anchor="w", pady=1)

        self._help(
            self.adv_frame,
            "Splitting matters more than it sounds: a match against a three-hour "
            "mix only tells you it is somewhere in three hours, while a match "
            "against a 6-minute piece points straight at it. This applies to "
            "downloads; for audio already on disk the two buttons in Step 3 "
            "decide it instead.",
        ).pack(anchor="w", padx=8, pady=(0, 4))

        # Row 3: filename template
        opts_r3 = ttk.Frame(self.adv_frame)
        opts_r3.pack(fill="x", padx=4, pady=(2, 0))
        ttk.Label(opts_r3, text="Name downloaded files:").pack(side="left", padx=(0, 4))
        self.filename_template_var = tk.StringVar(value="%(title)s [%(id)s].%(ext)s")
        self.filename_template_entry = ttk.Entry(
            opts_r3, textvariable=self.filename_template_var,
        )
        self.filename_template_entry.pack(side="left", fill="x", expand=True)
        self._help(
            self.adv_frame,
            "yt-dlp naming. The default gives “Title [videoid].m4a”. "
            "Safe to ignore.",
        ).pack(anchor="w", padx=8, pady=(1, 4))

        # Row 4: extra yt-dlp args
        opts_r4 = ttk.Frame(self.adv_frame)
        opts_r4.pack(fill="x", padx=4, pady=(2, 0))
        ttk.Label(opts_r4, text="Extra download options:").pack(side="left", padx=(0, 4))
        self.extra_args_var = tk.StringVar(value="")
        self.extra_args_entry = ttk.Entry(opts_r4, textvariable=self.extra_args_var)
        self.extra_args_entry.pack(side="left", fill="x", expand=True)
        self._help(
            self.adv_frame,
            "Passed straight to yt-dlp. For videos that need you signed in, try "
            "--cookies-from-browser firefox (or chrome, edge, brave), with that "
            "browser closed.",
        ).pack(anchor="w", padx=8, pady=(1, 6))

        # An explicit way out, so the window does not rely on the title-bar X.
        # Changes take effect immediately; there is nothing to apply or cancel.
        adv_close = ttk.Frame(self.adv_frame)
        adv_close.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(adv_close, text="Close", command=self._hide_advanced).pack(side="right")

        # ---- Progress ------------------------------------------------------
        active_box = ttk.LabelFrame(self.root, text="  Downloading now  ")
        active_box.pack(fill="x", **pad)

        # A fixed-height canvas holds the slot rows. With many parallel
        # downloads (up to 16) the rows scroll inside this fixed area instead
        # of stretching the panel and squashing the log box.
        active_canvas = tk.Canvas(active_box, height=60, highlightthickness=0)
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
            self.active_frame, text="Nothing downloading yet.", foreground="grey",
        )
        self._slot_placeholder.pack(anchor="w")

        # ---- Log -----------------------------------------------------------
        log_header = ttk.Frame(self.root)
        log_header.pack(fill="x", padx=8, pady=(4, 0))
        ttk.Label(log_header, text="What it is doing").pack(side="left")

        # Pack the bottom-anchored widgets FIRST (status bar, then log buttons)
        # using side="bottom". Tk reserves their space before the expanding log,
        # so on small screens the log shrinks instead of pushing them off-screen.
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(
            self.root, textvariable=self.status_var, relief="sunken", anchor="w",
        ).pack(side="bottom", fill="x")

        log_btns = ttk.Frame(self.root)
        log_btns.pack(side="bottom", fill="x", padx=8, pady=(0, 4))
        self.clear_btn = ttk.Button(log_btns, text="Clear", command=self._clear_log)
        self.clear_btn.pack(side="left", padx=(0, 4))
        self.copy_btn = ttk.Button(log_btns, text="Copy", command=self._copy_log)
        self.copy_btn.pack(side="left", padx=4)
        self._help(
            log_btns,
            "Copy this and include it if you need to ask someone for help.",
        ).pack(side="left", padx=8)

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

        self._update_move_hint()

    def _toggle_advanced(self) -> None:
        (self._hide_advanced if self.adv_open else self._show_advanced)()

    def _show_advanced(self) -> None:
        self.adv_open = True
        self.adv_btn.configure(text="▾  Advanced settings")
        self.adv_win.deiconify()
        self.adv_win.lift()
        # Open it over the main window rather than wherever Windows feels like,
        # so it reads as belonging to the button that opened it.
        self.root.update_idletasks()
        self.adv_win.geometry(
            f"+{self.root.winfo_rootx() + 40}+{self.root.winfo_rooty() + 120}"
        )

    def _hide_advanced(self) -> None:
        self.adv_open = False
        self.adv_btn.configure(text="▸  Advanced settings")
        self.adv_win.withdraw()

    def _update_move_hint(self) -> None:
        """Keep the fingerprints-folder line honest about whether it is needed.

        With one link it genuinely is optional. With several it is not: the
        results folder is emptied before each link, so without somewhere to
        move them to, every link but the last is thrown away. Saying
        "(optional)" in that situation is how people lose an overnight run."""
        hint = getattr(self, "move_hint", None)
        if hint is None:
            return
        many = len(getattr(self, "queue_urls", [])) > 1
        if many and not self.move_pklz_dir_var.get().strip():
            hint.configure(
                text="Needed here — you have more than one link. The results folder "
                     "is emptied before each one, so without a destination you "
                     "will keep only the last link's fingerprints.",
                foreground=self.HELP_WARN,
            )
        else:
            hint.configure(
                text="Where your finished .pklz files are collected. Optional for a "
                     "single link; required once the list has more than one.",
                foreground=self.HELP_GREY,
            )

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
        ("batch_size_var", "batch_size", int),
        ("fp_concurrency_var", "fp_concurrency", int),
    )

    def _apply_config(self, cfg: dict) -> None:
        """Apply a loaded config dict to the relevant Tk vars. Bad values silently skipped."""
        for var_name, key, conv in self._CONFIG_FIELDS:
            if key not in cfg:
                continue
            try:
                getattr(self, var_name).set(conv(cfg[key]))
            except (ValueError, TypeError, AttributeError, tk.TclError):
                # tk.TclError matters: a Spinbox the user cleared leaves its
                # IntVar holding "", and IntVar.get() raises TclError - which is
                # not a ValueError. Uncaught it escaped into Tk's callback
                # handler, invisible under .pyw, after _start had already
                # disabled every button and before any worker existed, leaving
                # the window dead until restart.
                pass
        # Restore the saved channel queue (crash recovery / persistence).
        saved_queue = cfg.get("queue")
        if isinstance(saved_queue, list):
            self.queue_urls = [str(u) for u in saved_queue if u]
            self.queue_checks = [tk.BooleanVar(value=True) for _ in self.queue_urls]
            self.queue_active = None
            self._refresh_queue()

    @staticmethod
    def _safe_int(var: tk.IntVar, default: int, minimum: int = 1) -> int:
        """Read an IntVar that the user may have emptied.

        Same hazard as _gather_config: a cleared Spinbox makes IntVar.get()
        raise TclError, and these are read on the worker thread mid-run where
        that surfaces as an unexplained failed channel."""
        try:
            return max(minimum, int(var.get()))
        except (tk.TclError, ValueError, TypeError):
            return default

    def _gather_config(self) -> dict:
        """Read current Tk var values into a serializable dict."""
        out: dict = {}
        for var_name, key, conv in self._CONFIG_FIELDS:
            try:
                out[key] = conv(getattr(self, var_name).get())
            except (ValueError, TypeError, AttributeError, tk.TclError):
                # tk.TclError matters: a Spinbox the user cleared leaves its
                # IntVar holding "", and IntVar.get() raises TclError - which is
                # not a ValueError. Uncaught it escaped into Tk's callback
                # handler, invisible under .pyw, after _start had already
                # disabled every button and before any worker existed, leaving
                # the window dead until restart.
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
            # Signal the worker to stop, then actually kill what is running.
            # Setting the flag alone left audfprint, yt-dlp and their ffmpeg
            # children running after the window had gone, with no UI left to
            # stop them from.
            self.cancel_flag.set()
            killed = self._kill_all_children()
            if killed:
                self._log(f"[!] Killed {killed} running process(es) on exit.")
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

        # The "keep fingerprints in" hint changes meaning as soon as there is
        # more than one link in the list, so it is refreshed alongside it.
        self._update_move_hint()

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
            messagebox.showerror("Missing input", "Pick a valid working folder for audio.")
            return
        if not bat_dir or not Path(bat_dir).is_dir():
            messagebox.showerror("Missing input", "Pick a valid program folder — the one containing audfprint.")
            return

        audfprint = Path(bat_dir) / "audfprint" / "audfprint.py"
        if not audfprint.is_file():
            messagebox.showerror(
                "audfprint not found",
                f"Could not find audfprint\\audfprint.py under:\n{bat_dir}\n\n"
                f"Fingerprinting is run directly now, so this is the only script "
                f"the pipeline needs.",
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
        self.fp_only_btn.config(state="disabled")
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

    def _start_bats_only(self, split: bool = True) -> None:
        """Fingerprint audio already on disk. `split` picks the variant: the
        two buttons that reach here differ only in this flag, and each says in
        its own label which it is, so neither depends on the Advanced setting."""
        """Skip downloads entirely; scan + fingerprint whatever is already on disk."""
        bat_dir = self.bat_dir_var.get().strip()
        if not bat_dir or not Path(bat_dir).is_dir():
            messagebox.showerror("Missing input", "Pick a valid program folder — the one containing audfprint.")
            return

        audfprint = Path(bat_dir) / "audfprint" / "audfprint.py"
        if not audfprint.is_file():
            messagebox.showerror(
                "audfprint not found",
                f"Could not find audfprint\\audfprint.py under:\n{bat_dir}",
            )
            return

        source_dir = self.output_dir_var.get().strip()
        if not source_dir or not Path(source_dir).is_dir():
            messagebox.showerror("Missing input", "Pick a valid working folder to scan for audio.")
            return

        # Says that files get rewritten, because they do: splitting replaces a
        # long recording with its pieces and deletes the original. Agreeing to
        # "fingerprint what is on disk" should not quietly also mean "and
        # restructure it".
        splitting = split
        split_line = (
            f"Anything longer than {SPLIT_TRIGGER // 60}:00 will first be SPLIT IN PLACE "
            f"into pieces of at least {SPLIT_MIN_CHUNK // 60}:00, and the original file "
            f"deleted.\n\n"
            if splitting else
            "Nothing will be split, and no file is examined for length. Use this "
            "when the audio has already been split; anything still over "
            f"{SPLIT_TRIGGER // 60}:00 goes into the database whole.\n\n"
        )
        if not messagebox.askyesno(
            "Split and fingerprint existing audio?" if splitting
            else "Fingerprint existing audio?",
            f"Scan for audio under:\n{source_dir}\n\n"
            f"{split_line}"
            f"Then fingerprint it into:\n{Path(bat_dir) / 'pklz-files'}\n\n"
            f"This skips the YouTube download step entirely.\n\nContinue?",
            parent=self.root,
        ):
            return

        self.cancel_flag.clear()
        self.start_btn.config(state="disabled")
        self.bats_btn.config(state="disabled")
        self.fp_only_btn.config(state="disabled")
        self.test_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self._set_inputs_locked(True)

        save_config(self._gather_config())

        self.worker_thread = threading.Thread(
            target=self._run_bats_only_pipeline,
            args=(Path(bat_dir), splitting),
            daemon=True,
        )
        self.worker_thread.start()

    def _start_test_connection(self) -> None:
        """Run a battery of diagnostic checks: tools, versions, network."""
        self.cancel_flag.clear()
        self.start_btn.config(state="disabled")
        self.bats_btn.config(state="disabled")
        self.fp_only_btn.config(state="disabled")
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

            # 7. Fingerprinter dir + audfprint presence
            bat_dir = self.bat_dir_var.get().strip()
            self._log(f"[*] Fingerprinter directory: {bat_dir or '(not set)'}")
            if bat_dir:
                p = Path(bat_dir)
                if p.is_dir():
                    afp = p / "audfprint" / "audfprint.py"
                    if afp.is_file():
                        self._log(f"    + audfprint.py: found ({afp.stat().st_size} bytes)")
                    else:
                        self._log("    ! audfprint/audfprint.py: NOT FOUND", tag="warning")
                    for sub_name in ("texts", "pklz-files"):
                        d = p / sub_name
                        if d.is_dir():
                            self._log(f"    + {sub_name}/: {len(list(d.iterdir()))} item(s)")
                        else:
                            self._log(f"    + {sub_name}/: not present (created on use)")
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

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """Kill a child process and everything it started.

        proc.terminate() only kills the process we launched, which is not the
        thing actually doing the work: audfprint spawns an ffmpeg per file,
        yt-dlp spawns ffmpeg to remux, and where ffmpeg or aria2c came from a
        package manager the name on PATH is often a small shim that runs the
        real binary as a further child. Terminating the top of that chain
        leaves the rest running, which is why Stop used to be something you had
        to press and then wait out. taskkill /T walks the whole tree."""
        if proc.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=10,
                )
            else:
                proc.terminate()
        except Exception:  # noqa: BLE001
            # taskkill can lose a race with a process that just exited; falling
            # back costs nothing and never leaves the tree alive on purpose.
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    def _track_proc(self, proc: subprocess.Popen) -> None:
        with self._fp_procs_lock:
            self._fp_procs.append(proc)

    def _untrack_proc(self, proc: subprocess.Popen) -> None:
        with self._fp_procs_lock:
            if proc in self._fp_procs:
                self._fp_procs.remove(proc)

    def _kill_all_children(self) -> int:
        """Kill every child we know about, and their children. Returns the count."""
        with self._fp_procs_lock:
            running = list(self._fp_procs)
        for proc in running:
            self._kill_tree(proc)
        return len(running)

    def _cancel(self) -> None:
        self.cancel_flag.set()
        self._log("[!] Stopping. Running work is being killed now; "
                  "nothing further will start.")
        # Safe to kill: an audfprint batch writes to a .part file that is only
        # renamed into place on a clean exit, and a killed download is simply
        # downloaded again.
        with self._fp_procs_lock:
            n = len(self._fp_procs)
        if n:
            self._log(f"[!] Stopping {n} running process(es)...")
        # On a worker thread, not here. This is a Tk button callback, and
        # taskkill is synchronous: walking every child on the UI thread froze
        # the window mid-Stop, which looks exactly like the hang that Stop is
        # supposed to end.
        threading.Thread(target=self._kill_all_children, daemon=True).start()

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
        self.fp_only_btn.config(state="normal")
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
            workers = self._safe_int(self.parallel_var, 4)
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
                self._log("[X] No successful downloads. Aborting before fingerprinting.")
                return "failed"

            # 4. Folder stays where it was downloaded; the scan below picks it up there.
            self._log(f"[+] Folder stays at: {initial_folder}")

            # 4a. Audio length sanity check (right after downloads finish)
            if not self._check_long_audio(initial_folder):
                return "cancelled"

            # 4b. Pre-flight: check that bat output folders are empty
            if not self._preflight_clean(bat_dir, ("texts", "pklz-files")):
                return "cancelled"

            # 5 + 6. Scan and fingerprint. Scan THIS channel's download
            # subfolder -- the same folder we downloaded into, split in place,
            # and delete further down -- not the whole output directory, which
            # in a queue run can still hold a previous channel whose cleanup
            # failed. (This read an undefined `output_dir` and raised NameError
            # on every download run.)
            pklz_dir = bat_dir / "pklz-files"
            pklz_before = self._snapshot_pklz(pklz_dir)
            source_dir = initial_folder
            if not self._run_fingerprint_stage(bat_dir, source_dir, status_prefix=qp):
                if (s := stopped()):
                    return s
                # Batches failed but their lists are still on disk, so the pklz
                # files that DID succeed are worth keeping and renaming below.
                self._log("[!] Fingerprinting did not complete cleanly - see the failures above.")
            if (s := stopped()):
                return s

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

    def _run_bats_only_pipeline(self, bat_dir: Path, split: bool = True) -> None:
        """Skip downloads. Scan the output directory, fingerprint it, done."""
        try:
            self._log("[*] Fingerprinting existing audio (no download)...")

            # Only texts/ is cleared here. pklz-files/ is deliberately left
            # alone: a successful run moves every .pklz out to the database, so
            # anything still sitting there is the partial output of a run that
            # failed, and that is exactly what this path should be resuming from
            # rather than being made to rebuild. The fingerprint stage decides
            # per batch whether an existing .pklz still matches its file list.
            if not self._preflight_clean(bat_dir, ("texts",)):
                return

            pklz_dir = bat_dir / "pklz-files"
            source_dir = Path(self.output_dir_var.get().strip() or bat_dir)

            # Split first, same as the download pipeline does. Without this the
            # two buttons produced different databases from the same audio: a
            # downloaded channel went in as >=6:00 pieces while anything
            # fingerprinted from disk went in whole.
            #
            # Recursive here, unlike the download path: that one is handed a
            # single channel folder, while this is pointed at the whole output
            # directory, which is normally a folder per channel or per year.
            if not self._check_long_audio(source_dir, recursive=True, split=split):
                return
            if not self._run_fingerprint_stage(bat_dir, source_dir):
                if self.cancel_flag.is_set():
                    return
                self._log("[!] Fingerprinting did not complete cleanly - see the failures above.")
            if self.cancel_flag.is_set():
                return

            # Every numbered .pklz here belongs to the scan that just ran, including
            # any carried over from a previous attempt, so all of them are renamed
            # rather than only the ones created this time round. Otherwise a resumed
            # run ships a mix of "channel_3.pklz" and bare "3.pklz" to the database.
            prefix = self._derive_prefix_from_subfolder(bat_dir)
            self._rename_new_pklz(pklz_dir, set(), prefix)

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

    # ------------------- fingerprinting (native, no .bat / no node) -----------
    #
    # Replaces preparador.bat -> setup.js and creador.bat -> fingerprinter.js.
    # Those were four processes deep (cmd -> bat -> node -> python) to do two
    # things Python does directly: walk a folder, and run audfprint on batches
    # of what it found. Doing it here removes the Node dependency, the console
    # windows, and the Spanish-language logs, and makes the work cancellable
    # and streamable into this console like everything else.
    #
    # Three behaviours are deliberately different from the scripts they replace,
    # each fixing something that was losing work:
    #
    #  1. setup.js scanned a hard-coded "C:\fingerprints\download" and ignored
    #     the configured Output directory entirely. Point the GUI somewhere else
    #     and it would fingerprint whatever happened to be at the old path. The
    #     scan now follows the configured directory.
    #
    #  2. fingerprinter.js resumed from max(existing pklz id), so a failed batch
    #     followed by a successful one was never retried: the run on 2026-09-15
    #     lost batches 6 and 7 that way, and the next run started at 9. Each
    #     batch is now checked for its own output, so gaps are picked back up.
    #
    #  3. A .pklz is written under a .part name and renamed only once audfprint
    #     exits 0, so a crash or Cancel mid-write cannot leave a truncated file
    #     that later looks like a finished batch.

    AUDIO_EXTENSIONS = {
        ".mp3", ".mp4", ".wav", ".flac", ".m4a",
        ".wma", ".webm", ".ogg", ".aac", ".opus",
    }

    def _publish_fp_progress(self) -> None:
        """Roll the per-batch counters into one status-bar line.

        The batches run concurrently and finish out of order, so a single
        'files done / files total' across all of them is the only number that
        means anything while they are in flight."""
        with self._fp_progress_lock:
            done = sum(d for d, _ in self._fp_progress.values())
            total = sum(t for _, t in self._fp_progress.values())
            active = len(self._fp_progress)
        base = self._fp_status_base
        if total:
            self._set_status(f"{base}{done:,}/{total:,} files "
                             f"({done * 100 // total}%) across {active} batch(es)")
        else:
            self._set_status(f"{base}fingerprinting...")

    def _scan_audio_files(self, source_dir: Path, texts_dir: Path, batch_size: int) -> int:
        """Walk source_dir for audio and write texts/<n>.txt lists of batch_size
        paths each. Returns the number of batches written (0 if no audio found).

        os.scandir rather than Path.rglob: at tens of thousands of files the
        difference is seconds, and this runs on the UI's worker thread."""
        texts_dir.mkdir(parents=True, exist_ok=True)
        found: list[str] = []
        stack = [str(source_dir)]
        while stack:
            if self.cancel_flag.is_set():
                return 0
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            elif os.path.splitext(entry.name)[1].lower() in self.AUDIO_EXTENSIONS:
                                found.append(entry.path)
                        except OSError:
                            continue
            except OSError as e:
                self._log(f"[!] Could not read {current}: {e}")

        if not found:
            return 0

        # Sorted so a given folder always batches the same way: a rerun after a
        # failure then reproduces the same <n>.txt contents, which is what makes
        # "batch 6 is missing its pklz" mean the same thing on the second run.
        found.sort()
        batches = 0
        for start in range(0, len(found), batch_size):
            batches += 1
            chunk = found[start:start + batch_size]
            (texts_dir / f"{batches}.txt").write_text("\n".join(chunk), encoding="utf-8")
        self._log(f"[+] Found {len(found):,} audio file(s) -> {batches} batch(es) of up to {batch_size}")
        return batches

    # The record of what was actually fingerprinted, kept next to the .pklz files
    # it describes rather than next to the lists, because it has to outlive any
    # rescan. It maps batch id -> hash of the file list that produced that batch's
    # .pklz. Resume compares the current list against it, so "batch 3 is done" is
    # only true while batch 3 still means the same files: add or remove audio and
    # the batching reshuffles under the old numbers, and without this check a
    # resumed run would keep .pklz files whose contents no longer match.
    FINGERPRINTED_RECORD = "fingerprinted.json"

    @staticmethod
    def _batch_list_hash(texts_dir: Path, batch_id: int) -> str | None:
        try:
            raw = (texts_dir / f"{batch_id}.txt").read_text(encoding="utf-8")
        except OSError:
            return None
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _load_fingerprinted(self, pklz_dir: Path) -> dict[str, str]:
        try:
            data = json.loads((pklz_dir / self.FINGERPRINTED_RECORD).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _save_fingerprinted(self, pklz_dir: Path, record: dict[str, str]) -> None:
        try:
            (pklz_dir / self.FINGERPRINTED_RECORD).write_text(
                json.dumps(record, indent=1), encoding="utf-8")
        except OSError as e:
            self._log(f"[!] Could not update {self.FINGERPRINTED_RECORD}: {e}")

    def _run_audfprint_batch(
        self,
        bat_dir: Path,
        batch_id: int,
        ncores: int,
        log_lock: threading.Lock,
    ) -> tuple[int, bool, str]:
        """Run audfprint over texts/<batch_id>.txt -> pklz-files/<batch_id>.pklz.

        Returns (batch_id, ok, detail). Output is streamed rather than buffered:
        the old version used Node's exec(), which holds everything in memory and
        only hands it over at the end, so a batch that died mid-run reported an
        empty stdout and there was nothing to diagnose it with."""
        # Every remaining batch is already queued in the pool, so the moment a
        # running one is killed the pool dispatches the next. Without this
        # guard, pressing Stop during batch 4 simply started batch 5.
        if self.cancel_flag.is_set():
            return batch_id, False, "cancelled before it started"

        texts_dir = bat_dir / "texts"
        pklz_dir = bat_dir / "pklz-files"
        list_file = texts_dir / f"{batch_id}.txt"
        final_pklz = pklz_dir / f"{batch_id}.pklz"
        part_pklz = pklz_dir / f"{batch_id}.pklz.part"

        if not list_file.is_file():
            return batch_id, False, f"{list_file.name} is missing"

        if part_pklz.exists():
            try:
                part_pklz.unlink()      # leftover from an interrupted attempt
            except OSError:
                pass

        script = bat_dir / "audfprint" / "audfprint.py"

        # Go through audfprint_quiet.py so that audfprint's own ffmpeg children
        # are spawned hidden. CREATE_NO_WINDOW below only covers this child: a
        # process without a console cannot lend one to its children, so every
        # ffmpeg audfprint starts would otherwise be handed a fresh console
        # window, which is the black box that blinks once per file. The wrapper
        # sets the flag process-wide inside audfprint instead of us patching
        # audfprint's source, which is a third-party checkout users download
        # themselves. If it is missing, fall back to invoking audfprint
        # directly: everything still works, it just flickers again.
        wrapper = Path(__file__).with_name("audfprint_quiet.py")
        launcher = [str(wrapper), str(script)] if wrapper.is_file() else [str(script)]
        cmd = [
            # -u matters: Python block-buffers stdout when it is a pipe rather
            # than a terminal, so audfprint's per-file lines would sit in the
            # child's buffer and arrive in one lump when the batch ended. Without
            # it the console shows a single line and then looks frozen for the
            # twenty minutes the batch actually takes.
            sys.executable, "-u", *launcher, "new",
            "-C",                        # keep going when one file fails to read
            "--dbase", str(part_pklz),
            "--list", str(list_file),
            "--ncores", str(max(1, ncores)),
        ]

        try:
            total_files = sum(1 for ln in list_file.read_text(encoding="utf-8").splitlines() if ln.strip())
        except OSError:
            total_files = 0

        tail: list[str] = []
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(bat_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            return batch_id, False, f"could not start audfprint: {e}"

        self._track_proc(proc)
        # Stop can land between the guard above and this spawn, in which case
        # _cancel walked a process list that did not yet contain us. Re-check
        # now that we are registered, so no batch survives by timing.
        if self.cancel_flag.is_set():
            self._kill_tree(proc)
        try:
            assert proc.stdout is not None
            last_report = 0.0
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                # Keep the last lines regardless of verbosity: when a batch dies
                # this is the only record of where it got to.
                tail.append(line)
                if len(tail) > 25:
                    del tail[0]

                # audfprint prints one "ingesting #N: <path>" per file. Echoing
                # every one would be thousands of lines of noise across four
                # concurrent batches, and dropping them (as this first did) left
                # the console looking frozen for the twenty minutes a batch runs.
                # Reported as a counter instead, at most once a second per batch.
                m = _INGESTING_RE.search(line)
                if m:
                    done_here = int(m.group(1)) + 1   # audfprint counts from #0
                    with self._fp_progress_lock:
                        self._fp_progress[batch_id] = (done_here, total_files)
                    now = time.time()
                    if now - last_report >= 1.0:
                        last_report = now
                        pct = f" {done_here * 100 // total_files}%" if total_files else ""
                        name = os.path.basename(m.group(2))[:60]
                        with log_lock:
                            self._log(f"  | [batch {batch_id}] {done_here}/{total_files or '?'}"
                                      f"{pct}  {name}", tag="bat")
                        self._publish_fp_progress()
                    continue

                if self.verbose_var.get():
                    with log_lock:
                        self._log(f"  | [batch {batch_id}] {line}", tag="bat")
            proc.wait()
        finally:
            self._untrack_proc(proc)

        if self.cancel_flag.is_set():
            part_pklz.unlink(missing_ok=True)
            return batch_id, False, "cancelled"

        if proc.returncode != 0 or not part_pklz.exists():
            part_pklz.unlink(missing_ok=True)
            detail = f"audfprint exited {proc.returncode}"
            if tail:
                detail += "\n      last output: " + "\n      ".join(tail[-6:])
            return batch_id, False, detail

        try:
            os.replace(part_pklz, final_pklz)
        except OSError as e:
            part_pklz.unlink(missing_ok=True)
            return batch_id, False, f"could not finalise {final_pklz.name}: {e}"
        return batch_id, True, f"{final_pklz.name} ({final_pklz.stat().st_size / 1024 / 1024:.1f} MB)"

    def _fingerprint_all(self, bat_dir: Path, total_batches: int) -> bool:
        """Run every batch that does not already have its .pklz, concurrently.

        Concurrency is across batches rather than inside one. Measured on this
        machine (50 cores, 32 real files per trial), with the same total number
        of audfprint worker processes each time:

            1 process  --ncores 8    59.2s
            2 processes --ncores 4   42.1s
            4 processes --ncores 2   36.9s
            8 processes --ncores 1   28.1s

        against 117.1s for the old single process at --ncores 1. audfprint's own
        --ncores splits one file list across processes and then merges their hash
        tables back through pipes, and that merge is serial, so it stops paying
        off around 8 and got slower at 16. Independent batches have nothing to
        merge.

        The cap matters: peak memory measured at 676 MB for 120 files, which
        extrapolates to roughly 5.5 GB for a 1000-file batch, so the default of
        4 concurrent batches is about 22 GB of the 64 GB on this box. Raising it
        much further risks swapping, which would undo the gain."""
        pklz_dir = bat_dir / "pklz-files"
        pklz_dir.mkdir(parents=True, exist_ok=True)

        # Per batch, not max(id). A gap left by an earlier failure is work to
        # redo, not a batch to skip. A .pklz only counts as done when the batch
        # it belongs to still covers the same files (see the manifest note in
        # _scan_audio_files); anything else is stale and gets rebuilt.
        texts_dir = bat_dir / "texts"
        record = self._load_fingerprinted(pklz_dir)
        record_lock = threading.Lock()
        pending: list[int] = []
        stale = 0
        for i in range(1, total_batches + 1):
            existing = pklz_dir / f"{i}.pklz"
            current = self._batch_list_hash(texts_dir, i)
            if existing.exists() and current and record.get(str(i)) == current:
                continue                      # genuinely already done
            if existing.exists():
                # Present but built from a different file set, so it is wrong
                # for this batch number now. Rebuilt rather than trusted.
                stale += 1
                try:
                    existing.unlink()
                except OSError as e:
                    self._log(f"[!] Could not remove stale {existing.name}: {e}")
            pending.append(i)
        if stale:
            self._log(f"[!] {stale} existing pklz file(s) were built from a different "
                      f"set of files (the audio changed) and are being rebuilt.")
        # Drop records for batches that no longer exist, so the file cannot grow
        # forever across runs with different batch counts.
        for key in [k for k in record if not k.isdigit() or int(k) > total_batches]:
            record.pop(key, None)

        # A run over less audio than last time produces fewer batches, leaving
        # higher-numbered .pklz files behind that nothing will refresh. Named
        # rather than deleted: they may be a previous channel's output that has
        # not been moved to the database yet, and that is not this step's to throw
        # away.
        orphans = sorted(
            p.name for p in pklz_dir.glob("*.pklz")
            if p.stem.isdigit() and int(p.stem) > total_batches
        )
        if orphans:
            self._log(f"[!] {len(orphans)} pklz file(s) left over from a previous, larger "
                      f"run and not covered by this one: {', '.join(orphans)}")
            self._log("[!] They are untouched. Move or delete them if they are no longer wanted.")

        done_already = total_batches - len(pending)
        if done_already:
            self._log(f"[+] {done_already} batch(es) already fingerprinted, {len(pending)} to do")
        if not pending:
            self._log("[+] Nothing to fingerprint: every batch already has its pklz.")
            return True

        # Seeded with every pending batch at 0 so the total is the real total
        # from the first line printed, rather than climbing as batches start.
        with self._fp_progress_lock:
            self._fp_progress = {}
            for i in pending:
                try:
                    n_files = sum(1 for ln in (texts_dir / f"{i}.txt").read_text(
                        encoding="utf-8").splitlines() if ln.strip())
                except OSError:
                    n_files = 0
                self._fp_progress[i] = (0, n_files)

        concurrency = max(1, min(self._safe_int(self.fp_concurrency_var, 4), len(pending)))
        ncores = AUDFPRINT_NCORES
        self._log(f"[*] Fingerprinting {len(pending)} batch(es), "
                  f"{concurrency} at a time, audfprint --ncores {ncores}")

        log_lock = threading.Lock()
        completed = 0
        failures: list[tuple[int, str]] = []
        started = time.time()

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(self._run_audfprint_batch, bat_dir, i, ncores, log_lock): i
                for i in pending
            }
            for fut in as_completed(futures):
                batch_id, ok, detail = fut.result()
                completed += 1
                if ok:
                    # Recorded the moment the batch lands, not at the end of the
                    # run: a crash after batch 3 should still leave 1-3 known-good
                    # rather than throwing away work that is already on disk.
                    with record_lock:
                        h = self._batch_list_hash(texts_dir, batch_id)
                        if h:
                            record[str(batch_id)] = h
                            self._save_fingerprinted(pklz_dir, record)
                    self._log(f"[+] Batch {batch_id} done -> {detail}  "
                              f"({completed}/{len(pending)})")
                else:
                    failures.append((batch_id, detail))
                    self._log(f"[X] Batch {batch_id} failed: {detail}")
                # A finished batch counts as fully done, so the aggregate line
                # does not stall at whatever its last "ingesting" line reported.
                with self._fp_progress_lock:
                    if batch_id in self._fp_progress:
                        _, tot = self._fp_progress[batch_id]
                        self._fp_progress[batch_id] = (tot, tot)
                elapsed = time.time() - started
                rate = completed / elapsed if elapsed else 0
                left = (len(pending) - completed) / rate if rate else 0
                self._fp_status_base = (f"Batch {completed}/{len(pending)} done, "
                                        f"{fmt_time(left)} left - ")
                self._publish_fp_progress()
                if self.cancel_flag.is_set():
                    # cancel_futures drops everything still queued in one go.
                    # Cancelling them individually (as this did) races the pool,
                    # which dispatches the next batch as soon as a worker frees
                    # up -- which killing the running batches does immediately.
                    pool.shutdown(wait=False, cancel_futures=True)
                    break

        if self.cancel_flag.is_set():
            self._log("[!] Fingerprinting cancelled.")
            return False

        took = fmt_time(time.time() - started)
        if failures:
            self._log(f"[!] Fingerprinting finished in {took} with "
                      f"{len(failures)} failed batch(es): "
                      + ", ".join(str(b) for b, _ in failures))
            self._log("[!] Their .pklz files were not written, so running this "
                      "again retries exactly those batches.")
            return False
        self._log(f"[+] All {len(pending)} batch(es) fingerprinted in {took}.")
        return True

    def _run_fingerprint_stage(self, bat_dir: Path, source_dir: Path, status_prefix: str = "") -> bool:
        """Scan + fingerprint: the whole of what preparador.bat and creador.bat
        used to do. Returns True only if every batch produced a .pklz."""
        texts_dir = bat_dir / "texts"
        batch_size = self._safe_int(self.batch_size_var, 1000)

        self._set_status(f"{status_prefix}Scanning for audio...")
        self._log(f"[*] Scanning for audio under: {source_dir}")
        total_batches = self._scan_audio_files(source_dir, texts_dir, batch_size)
        if self.cancel_flag.is_set():
            return False
        if total_batches == 0:
            self._log(f"[X] No audio files found under {source_dir}. Nothing to fingerprint.")
            return False

        self._set_status(f"{status_prefix}Fingerprinting...")
        return self._fingerprint_all(bat_dir, total_batches)

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

    def _check_long_audio(
        self, folder: Path, recursive: bool = False, split: bool | None = None,
    ) -> bool:
        """Probe every audio file under `folder` and split anything long enough to
        be split. Always returns True so the pipeline continues; returns False only
        on cancel.

        `recursive` because the two callers see different shapes: a channel
        download lands one flat folder, while the disk paths are pointed at the
        whole working folder, which is usually a folder per channel or per year.

        `split` overrides the checkbox for one run: the two disk buttons say in
        their own labels whether they split, so they pass it explicitly rather
        than depending on a setting the user would have to go and find. None
        means "use the checkbox", which is what the download path does.

        When splitting is off this returns before enumerating anything. That is
        the whole point of the no-split path: the probe costs one ffprobe per
        file, so on an already-split collection of thousands of pieces it used
        to spend a long time reading durations only to announce it had nothing
        to do.
        """
        if split is None:
            split = self.split_long_var.get()
        if not split:
            self._log(
                "[*] Splitting is off - skipping the duration check entirely "
                "(no files are probed)."
            )
            return True

        self._set_status("Checking audio durations...")
        self._log("[*] Checking audio file durations...")

        if not check_dependency("ffprobe"):
            self._log("[!] ffprobe not on PATH; skipping length check.")
            return True

        if recursive:
            audio_files = sorted(
                p for p in folder.rglob("*")
                if p.is_file() and p.suffix.lower() in SPLIT_AUDIO_EXTENSIONS
            )
        else:
            audio_files = sorted(
                p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in SPLIT_AUDIO_EXTENSIONS
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
            if dur > SPLIT_TRIGGER:
                long_files.append((f, dur))

        if unreadable:
            self._log(f"[!] Could not read duration of {unreadable} file(s).")
        if not long_files:
            self._log(
                f"[+] All {len(audio_files)} file(s) are under "
                f"{SPLIT_TRIGGER // 60}:00 — nothing to split."
            )
            return True

        long_files.sort(key=lambda x: -x[1])

        # No "splitting is disabled" branch here any more: that case now returns
        # at the top of this method, before anything is enumerated or probed.
        self._log(
            f"[*] Splitting {len(long_files)} file(s) into pieces of at least "
            f"{SPLIT_MIN_CHUNK // 60}:00...",
            tag="splitter",
        )
        made = 0
        for path, dur in long_files:
            if self.cancel_flag.is_set():
                return False
            made += self._split_file_in_place(path, dur)
        self._log(f"[+] Splitting complete: {made} piece(s) written.", tag="splitter")
        return True

    # ---------- splitter ------------------------------------------------------
    #
    # Every piece is at least SPLIT_MIN_CHUNK long. That is a floor, not a
    # ceiling, and it replaced the opposite rule: this used to cut on detected
    # silences until no piece was longer than 5:00, which meant pieces as short
    # as 90 seconds. Audfprint has less to match on the shorter a piece is, and
    # holding every collection to the same floor means a database fed from
    # several sources has one granularity rather than several.
    #
    # The arithmetic: cut at fixed
    # SPLIT_SEGMENT boundaries, and if the last piece would come up short,
    # drop the segment count by one so the remainder joins the piece before it.
    # That final piece is then SPLIT_SEGMENT + remainder, which is over the
    # floor by construction — so no cut can produce a short piece.

    @staticmethod
    def _build_segments(duration: float) -> list[tuple[float, float]]:
        """(start, end) pairs, or [] when the file should be left whole.

        Below two segments' worth there is no cut that leaves two legal pieces,
        so a 9-minute video stays a 9-minute video."""
        if duration <= SPLIT_TRIGGER:
            return []

        count = int(math.ceil(duration / float(SPLIT_SEGMENT)))
        if count >= 2:
            tail = duration - (count - 1) * SPLIT_SEGMENT
            if 0 < tail < SPLIT_MIN_CHUNK:
                count -= 1
        if count < 2:
            return []

        segments = [
            (i * SPLIT_SEGMENT,
             duration if i == count - 1 else min((i + 1) * SPLIT_SEGMENT, duration))
            for i in range(count)
        ]
        segments = [(s, e) for s, e in segments if e > s]
        # Asserted rather than trusted: this runs unattended over whole channels,
        # and a short piece would only show up later as a weak fingerprint.
        for s, e in segments:
            assert e - s >= SPLIT_MIN_CHUNK - 0.5, (
                f"produced a {e - s:.1f}s piece from a {duration:.1f}s file")
        return segments

    @staticmethod
    def _split_piece_name(base: str, start: float, end: float, ext: str) -> str:
        """`song_split_000m-006m.m4a`, naming each piece by its position.

        Self-describing on purpose: a bare `_1`, `_2` says nothing about which
        part of the recording it is, which matters when a match comes back
        against one piece of a long set."""
        def label(seconds: float) -> str:
            total = max(0, int(round(seconds)))
            m, s = divmod(total, 60)
            return f"{m:03d}m" if s == 0 else f"{m:03d}m{s:02d}s"
        return f"{base}_split_{label(start)}-{label(end)}{ext}"

    def _ffmpeg_slice(self, src: Path, start: float, end: float, dst: Path) -> bool:
        """Stream-copy [start, end) of src into dst. Returns True on success.

        Written to a scratch name and moved into place only once ffmpeg exits
        cleanly, so a cancel or a crash mid-write cannot leave a truncated piece
        sitting at the real name looking like finished work.
        """
        length = end - start
        if length <= 0:
            return False
        tmp = dst.with_suffix(dst.suffix + ".part")
        # ffmpeg picks its muxer from the output extension, and ".part" tells it
        # nothing — without -f every single slice fails with "Unable to find a
        # suitable output format".
        fmt = SPLIT_MUXERS.get(dst.suffix.lower(), dst.suffix.lower().lstrip("."))
        try:
            proc = subprocess.run(
                # -ss before -i is an input seek: fast, and accurate enough here
                # because these are all frame-independent audio codecs. Duration
                # is given as -t after the input rather than -to before it,
                # because -to as an input option has meant different things
                # across ffmpeg versions.
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-ss", f"{start:.6f}", "-i", str(src), "-t", f"{length:.6f}",
                 "-vn", "-c", "copy", "-f", fmt, str(tmp)],
                capture_output=True, text=True, check=False,
                encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            ok = proc.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0
            if not ok:
                err = (proc.stderr or "").strip()[:200]
                self._log(f"  ! ffmpeg slice failed: {err}", tag="warning")
                tmp.unlink(missing_ok=True)
                return False
            os.replace(tmp, dst)
            return True
        except FileNotFoundError:
            self._log("[!] ffmpeg missing during slicing.", tag="warning")
            tmp.unlink(missing_ok=True)
            return False

    def _split_file_in_place(self, file_path: Path, duration: float) -> int:
        """Split one file into >= SPLIT_MIN_CHUNK pieces beside it, then delete the
        original. Returns how many pieces were written."""
        mins, secs = divmod(int(duration), 60)
        segments = self._build_segments(duration)
        if not segments:
            self._log(f"  {file_path.name} ({mins}:{secs:02d}) is too short to split; left whole.",
                      tag="splitter")
            return 0

        self._log(f"[*] Splitting: {file_path.name}  ({mins}:{secs:02d}) "
                  f"-> {len(segments)} piece(s)", tag="splitter")
        self._set_status(f"Splitting {file_path.name}...")

        base = file_path.stem
        ext = file_path.suffix
        out_dir = file_path.parent
        written: list[Path] = []

        for start, end in segments:
            if self.cancel_flag.is_set():
                # Leave the original alone: a half-split file that lost its
                # source would be unrecoverable.
                for p in written:
                    p.unlink(missing_ok=True)
                self._log("  cancelled; pieces removed and original kept.", tag="warning")
                return 0
            out_path = out_dir / self._split_piece_name(base, start, end, ext)
            if out_path.exists() and out_path != file_path:
                out_path.unlink(missing_ok=True)   # leftover from an earlier attempt
            if self._ffmpeg_slice(file_path, start, end, out_path):
                written.append(out_path)
                self._log(f"    + {out_path.name}  [{(end - start):.0f}s]", tag="splitter")

        if len(written) == len(segments):
            try:
                file_path.unlink()
            except Exception as e:  # noqa: BLE001
                self._log(f"[!] Could not delete original {file_path.name}: {e}", tag="warning")
        else:
            # Some pieces failed, so the original is still the only complete copy
            # of the parts that did not get written.
            for p in written:
                p.unlink(missing_ok=True)
            self._log(
                f"[!] Only {len(written)}/{len(segments)} piece(s) written for "
                f"{file_path.name}; pieces discarded and original kept.",
                tag="warning",
            )
            return 0
        return len(written)

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

        self._track_proc(proc)

        # Drain stderr on a thread. It was piped and never read, so as soon as
        # yt-dlp wrote more than one pipe buffer there (a few KB - trivial with
        # --verbose in Extra download options, or a playlist full of warnings)
        # the child blocked writing while we sat in the stdout loop below
        # waiting for a line that could never arrive. Neither side could move.
        stderr_tail: list[str] = []

        def _drain_stderr() -> None:
            if proc.stderr is None:
                return
            try:
                for errline in proc.stderr:
                    errline = errline.rstrip()
                    if errline:
                        stderr_tail.append(errline)
                        del stderr_tail[:-20]   # keep only the last few
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_drain_stderr, daemon=True).start()

        assert proc.stdout is not None
        last_log = time.monotonic()
        keys = (
            "id", "title", "duration", "url", "webpage_url",
            "playlist_title", "playlist_uploader", "playlist_channel", "channel",
        )
        try:
            for line in proc.stdout:
                if self.cancel_flag.is_set():
                    self._kill_tree(proc)
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
            self._untrack_proc(proc)

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
                    # Drop everything still queued. Without this the `with`
                    # block's shutdown(wait=True) also blocked the whole
                    # pipeline until the remaining downloads finished on their
                    # own, so the UI stayed "running" long after Stop.
                    ex.shutdown(wait=False, cancel_futures=True)
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
                # See the note in _download_one: .get(key, default) doesn't
                # fall back when the key is present with an explicit None
                # value, which happens for extractors that leave title blank.
                title = futures[fut].get("title") or "?"
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
        # Same shape as the fingerprint batches: every remaining video is
        # already queued in the pool, so without this a Stop during download 4
        # simply started download 5.
        if self.cancel_flag.is_set():
            return False
        video_url = entry.get("webpage_url") or entry.get("url") or entry.get("id") or ""
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
        # NOTE: entry.get("title", default) would NOT fall back here if the key
        # is present with an explicit None value — which happens whenever
        # yt-dlp printed its "NA" marker for a missing title (common on
        # non-YouTube extractors like Mixcloud, SoundCloud, etc., where flat
        # playlist enumeration doesn't always populate every field). `.get()`
        # only uses its default when the key is ABSENT, not when its value is
        # None. Using `or` instead correctly falls back in both cases.
        title = entry.get("title") or str(video_url) or "(untitled)"
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

        # Registered so Stop can kill it outright. Relying on the cancel check
        # inside the stdout loop below was not enough: yt-dlp prints nothing
        # for the whole remux, so the loop sits in a blocking read and the
        # download outlived Stop.
        self._track_proc(proc)

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
                    self._kill_tree(proc)
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
            self._untrack_proc(proc)
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