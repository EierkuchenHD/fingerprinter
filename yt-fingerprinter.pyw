"""
Fingerprinter (Windows only)
----------------------------
Pipeline:
  1. List the entries behind a link via yt-dlp: a channel, playlist or single
     page on YouTube, Archive.org, Mixcloud, SoundCloud or any other site
     yt-dlp supports
  2. Log how many entries were found and a rough size/time estimate, then
     start without asking (so a list can run unattended)
  3. If a previous run left files in this link's download subfolder, offer
     to clear it
  4. Download each entry as native m4a/opus (no transcode), several at once,
     into <output_dir>/<name>_subfolder
  5. Probe each file with ffprobe; split anything over 12:00 in place into
     6:00 pieces, the last one taking the remainder (audfprint has less to
     match on the shorter a piece is)
  6. Empty the program's work/ folder, moving any .pklz an interrupted run
     left there to the fingerprints folder
  7. Scan that folder for audio and fingerprint it with audfprint, several
     batches at a time
  8. Move the renamed .pklz files to the folder they are kept in (pklz-files/
     by default), list it and optionally open it

Requirements (dependencies.py checks them and installs what is missing):
  - Python 3.10+ with the packages in requirements.txt
  - yt-dlp
  - ffmpeg + ffprobe
  - Node.js (yt-dlp runs it for YouTube via --js-runtimes node)
  - audfprint from WerZatSong (github.com/Nel80s/WerZatSong, libs/audfprint)
    in <program folder>/audfprint. Upstream dpwe/audfprint does not work:
    see dependencies.py for why.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import queue
import re
import shlex
import shutil
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import dependencies

try:
    import psutil   # in requirements.txt; Pause uses it to suspend running work
except ImportError:  # pragma: no cover - Check setup offers to install it
    psutil = None

__version__ = "1.0.0-beta.4"


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


def check_dependency(cmd: str | list[str]) -> bool:
    """True if the program starts at all. A list is a full command, such as
    ["python", "-m", "yt_dlp"]."""
    argv = [cmd] if isinstance(cmd, str) else list(cmd)
    try:
        subprocess.run(
            [*argv, "--version"],
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
# Raised when the meaning of a saved setting changes (see _apply_config).
CONFIG_VERSION = 3
# Items already fingerprinted, one "<extractor> <id>" per line: the format of
# yt-dlp's --download-archive, so the file also works with yt-dlp itself.
DONE_FILE = SCRIPT_DIR / "fingerprinted-items.txt"
# A list run in progress: its links, which finished, and which one is running.
# Written as each link starts and ends and deleted when the list completes, so
# finding it at start-up means the last run stopped part-way (Stop, a crash,
# the PC shutting down) and can be continued (see _offer_resume).
LIST_STATE_FILE = SCRIPT_DIR / "unfinished-list.json"


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


# Folders inside the program folder. downloads\ and pklz-files\ are the user's:
# the default working folder and the default place finished fingerprints are
# kept, and nothing in pklz-files\ is ever deleted. work\ is the program's own
# scratch space (audfprint's file lists, and the .pklz files it is writing
# before they are renamed and moved out), emptied whenever a run needs it.
# yt-dlp writes to a pipe in the Windows code page (cp1252 and the like),
# while everything here reads UTF-8, so every non-ASCII letter in a title
# (ä, ö, ü, ß, Japanese...) arrived as a replacement character and showed as
# "?". yt-dlp's own --encoding makes it write UTF-8, whether it is the pip
# script, python -m yt_dlp or the standalone exe.
YTDLP_UTF8 = ("--encoding", "utf-8")

# Settings, General: the window's colours. "Follow Windows" reads whether
# Windows apps are set to dark, and keeps following it while the program runs.
THEME_CHOICES = ("Follow Windows", "Light", "Dark")
# Colours for everything the ttk theme does not draw: the list and Now rows,
# the console and its colour tags, borders, dividers, tooltips, and the text
# colours of help lines and links. Light matches the look before dark mode:
# menus, drop-down lists and selected text keep Windows' own colours there.
PALETTES = {
    "light": {
        "bg": "#f0f0f0", "fg": "#000000", "field": "#ffffff", "button": "#e1e1e1",
        "hover": "#e5f1fb", "pressed": "#cce4f7", "disabled_fg": "#8a8a8a",
        "row_bg": "#ffffff", "row_selected": "#cce8ff", "link": "#0b57d0",
        "muted": "#707070", "help": "#5f6b7a", "warn": "#b02a37", "remove_hover": "#c0392b",
        "border": "#c8c8c8", "sash": "#f0f0f0", "accent": "#06b025",
        "console_bg": "SystemWindow", "console_fg": "SystemWindowText", "select": "#cce8ff",
        "menu": "SystemMenu", "menu_fg": "SystemMenuText", "list": "SystemWindow",
        "list_fg": "SystemWindowText", "highlight": "SystemHighlight", "highlight_fg": "SystemHighlightText",
        "tip_bg": "#ffffe1", "tip_fg": "#000000",
        "tags": {"ytdlp": "#1565c0", "bat": "#e67e22", "warning": "#c0392b",
                 "splitter": "#16a085", "ts": "#888888"},
    },
    "dark": {
        "bg": "#202124", "fg": "#e8eaed", "field": "#2b2c30", "button": "#35363a",
        "hover": "#3f4146", "pressed": "#4a4c52", "disabled_fg": "#76797e",
        "row_bg": "#1b1c1f", "row_selected": "#264f78", "link": "#8ab4f8",
        "muted": "#9aa0a6", "help": "#9aa0a6", "warn": "#f28b82", "remove_hover": "#f28b82",
        "border": "#3c4043", "sash": "#3c4043", "accent": "#5bb974",
        "console_bg": "#16171a", "console_fg": "#e8eaed", "select": "#264f78",
        "menu": "#2b2c30", "menu_fg": "#e8eaed", "list": "#2b2c30",
        "list_fg": "#e8eaed", "highlight": "#264f78", "highlight_fg": "#e8eaed",
        "tip_bg": "#303134", "tip_fg": "#e8eaed",
        "tags": {"ytdlp": "#8ab4f8", "bat": "#fbbc04", "warning": "#f28b82",
                 "splitter": "#81c995", "ts": "#8c8f94"},
    },
}

# Browsers yt-dlp can take cookies from (Settings, Downloads); "None" means
# no --cookies-from-browser.
COOKIE_BROWSERS = ("None", "Firefox", "Chrome", "Edge", "Brave", "Opera", "Vivaldi", "Chromium")

DOWNLOADS_DIR = "downloads"
PKLZ_DIR = "pklz-files"
# Where downloaded audio is kept when Settings asks for it (off by default).
KEEP_AUDIO_DIR = "audio"
WORK_DIR = "work"
WORK_TEXTS = Path(WORK_DIR, "texts")
WORK_PKLZ = Path(WORK_DIR, "pklz")


def norm_path(text: str) -> str:
    """A folder path in Windows form, or "" if blank. Tk's folder picker hands
    back forward slashes, which look like a mistake next to the backslashes
    of every path the program formats itself."""
    text = text.strip()
    return os.path.normpath(text) if text else ""


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


class Tooltip:
    """A small note that appears when the pointer rests on a widget, and goes
    on leaving or clicking. `text` may be a function, for notes that change
    (a folder, a count's date). These replace the grey line that used to sit
    under every control and cost a line of height each."""

    DELAY_MS = 600
    # Set by the app from the current theme (see _apply_theme).
    colours = {"bg": "#ffffe1", "fg": "#000000"}

    def __init__(self, widget: tk.Misc, text: str | Callable[[], str]) -> None:
        self.widget = widget
        self.text = text
        self._after: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: object = None) -> None:
        self._cancel()
        self._after = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self) -> None:
        self._after = None
        text = self.text() if callable(self.text) else self.text
        if self._tip is not None or not text:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=text, justify="left", bg=Tooltip.colours["bg"], fg=Tooltip.colours["fg"],
                 relief="solid", bd=1, wraplength=380, padx=6, pady=3).pack()
        tip.update_idletasks()
        # Below the widget, kept on screen at the right-hand edge.
        x = min(self.widget.winfo_rootx() + 10,
                self.widget.winfo_screenwidth() - tip.winfo_reqwidth() - 8)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        tip.wm_geometry(f"+{max(0, x)}+{y}")
        self._tip = tip

    def _hide(self, _event: object = None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


# ---------------------------- main app ----------------------------------------

class FingerprinterApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"Fingerprinter {__version__}")
        # The fingerprint icon, for this window, Settings and the taskbar
        # (main() gives the program a taskbar button of its own for it).
        self._set_window_icon()
        # Sized against the actual screen rather than a fixed guess, and clamped
        # so a 1366x768 laptop still gets a window that fits with nothing to
        # scroll (see _build_ui). Wide enough for the list and Now side by side.
        want_w = min(1280, max(900, self.root.winfo_screenwidth() - 80))
        want_h = min(1000, max(600, self.root.winfo_screenheight() - 80))
        self.root.geometry(f"{want_w}x{want_h}")
        self.root.minsize(900, 600)

        # ffmpeg and Node.js installed by Check setup live in tools\; put them
        # on PATH for this process and everything it starts.
        dependencies.add_tools_to_path()
        # How to run yt-dlp: the exe on PATH, or `python -m yt_dlp` when pip put
        # it somewhere PATH does not reach. Resolved at startup (off the UI
        # thread) and again after anything is installed.
        self.ytdlp: list[str] = ["yt-dlp"]

        self.log_queue: queue.Queue[tuple[str, str, str | None]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        # The running list's record (_run_queue), and whether the unfinished
        # list has been offered this session (_offer_resume).
        self._list_state: dict | None = None
        self._resume_asked = False
        # Which job is running, for what the Stop question says (_cancel).
        self._job_kind = ""
        # Whether the main window carries the app ID (_set_window_app_id).
        self._window_app_id = False
        # The record of finished batches per work\pklz, this session (_load_fingerprinted).
        self._fp_records: dict[str, dict[str, str]] = {}
        self.cancel_flag = threading.Event()
        # EVERY live child process, so Stop and Quit can actually end them.
        # This used to hold audfprint batches only, which meant Stop during the
        # download stage killed nothing at all: yt-dlp was noticed only when it
        # next wrote a line, and it writes nothing for the whole remux.
        self._fp_procs: list[subprocess.Popen] = []
        self._fp_procs_lock = threading.Lock()
        # batch id -> (files done, files in batch), for the aggregate progress line
        self._fp_progress: dict[int, tuple[int, int]] = {}
        # (batch id, part) -> files done, summed per batch into _fp_progress;
        # and when each batch last printed its progress line
        self._fp_part_done: dict[tuple[int, int], int] = {}
        self._fp_last_report: dict[int, float] = {}
        self._fp_progress_lock = threading.Lock()
        self._fp_status_base = ""
        # Set to skip just the current channel in a queue run (vs cancel_flag
        # which aborts the entire batch).
        self.skip_flag = threading.Event()
        # Set while paused: running children are suspended and nothing new
        # starts until it clears (see _pause).
        self.pause_flag = threading.Event()
        self._pause_lock = threading.Lock()
        self._suspended: dict[int, list] = {}   # pid -> [psutil.Process, times suspended, CPU seconds]
        # Tracks where the most recent channel's pklz files landed, so a queue
        # run can open that folder once at the end.
        self._last_report_dir: Path | None = None
        # This link's items that downloaded (see _download_parallel)
        self._downloaded_ok: list[dict] = []

        self._build_ui()
        self._apply_config(load_config())
        self._fill_default_folders()
        # The ttk theme main() picked: light mode goes back to it (_apply_theme).
        self._native_theme = ttk.Style().theme_use()
        self._apply_console_font()
        self._apply_theme()
        for window in (self.root, self.settings_win):
            window.bind("<Map>", self._on_map_title_bar, add="+")
        self.root.after(10_000, self._follow_windows_theme)
        self._poll_log_queue()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Quick look for anything missing, so a first run offers to install it
        # instead of failing halfway through the first link. Started from the
        # event loop, and handed the folder rather than reading the Tk variable
        # itself: a worker that touches Tk before mainloop is running dies with
        # "main thread is not in main loop", silently under pythonw.
        self.root.after(200, lambda: threading.Thread(
            target=self._startup_check,
            args=(self.bat_dir_var.get().strip() or str(dependencies.APP_DIR),),
            daemon=True,
        ).start())

    # ------------------------- UI ---------------------------------------------
    #
    # One screen with nothing to scroll. Two toolbar rows hold what you do
    # (add links; start, pause, skip, stop), the list and the live progress sit
    # side by side under them, and the console runs the full width along the
    # bottom, where long file names fit on one line. What is set once (folders,
    # fingerprinting and download options) lives in Settings. Explanations are
    # tooltips: the grey line that used to sit under every control cost a line
    # of height each, and together they pushed the controls off the screen.

    # Parallel yt-dlp downloads. Each is one yt-dlp process (roughly 50-100 MB)
    # plus a brief ffmpeg remux, so the machine is rarely the limit; the site is.
    # Push a single host hard enough and it answers HTTP 429, which is why the
    # default stays moderate and the ceiling finite.
    DEFAULT_PARALLEL = 8
    MAX_PARALLEL = 32

    # audfprint processes side by side, and when a batch is worth splitting
    # into parts (see _fingerprint_all).
    DEFAULT_FP_JOBS = 4
    MIN_FILES_TO_SPLIT = 40
    MIN_FILES_PER_PART = 10

    HELP_FONT = ("Segoe UI", 8)
    HELP_GREY = "#5f6b7a"
    HELP_WARN = "#b02a37"

    # The console keeps at least this much of the window, and at least this
    # share of it; the list and Now share the rest side by side, the list
    # taking LIST_SHARE of the width.
    CONSOLE_MIN_HEIGHT = 260
    CONSOLE_MIN_SHARE = 0.4
    LIST_SHARE = 0.55
    # The console's text size (Settings, General), in points.
    CONSOLE_FONT_SIZE = 9
    CONSOLE_FONT_RANGE = (7, 20)

    def _help(
        self, parent: tk.Misc, text: str,
        colour: str | None = None, wrap: int = 980,
    ) -> ttk.Label:
        """One small grey explanation line, to sit under the control it explains."""
        label = ttk.Label(
            parent, text=text, font=self.HELP_FONT,
            foreground=colour or self.HELP_GREY,
            wraplength=wrap, justify="left",
        )
        # Its colour follows the theme (see _apply_theme).
        self._themed_labels.append((label, "warn" if colour == self.HELP_WARN else "help"))
        return label

    def _folder_row(
        self, parent: ttk.Frame, row: int, label: str,
        var: tk.StringVar, help_text: str, wrap: int = 560,
    ) -> ttk.Label:
        """Label + entry + Browse, with an explanation line beneath it.

        Returns the explanation label."""
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", padx=4, pady=(6, 0),
        )
        entry = ttk.Entry(parent, textvariable=var, width=58)
        entry.grid(row=row, column=1, sticky="we", padx=4, pady=(6, 0))
        # A typed or pasted path is tidied into Windows form on leaving the box.
        entry.bind("<FocusOut>", lambda _e: self._normalize_var(var))
        ttk.Button(
            parent, text="Browse...", command=lambda: self._browse(var),
        ).grid(row=row, column=2, padx=4, pady=(6, 0))
        hint = self._help(parent, help_text, wrap=wrap)
        hint.grid(row=row + 1, column=1, columnspan=2, sticky="w", padx=4, pady=(1, 2))
        return hint

    def _panel_header(self, parent: tk.Misc, title_var: tk.StringVar) -> ttk.Frame:
        """A panel's title on the left, with room for small buttons on the right."""
        head = ttk.Frame(parent)
        head.pack(fill="x", pady=(0, 3))
        ttk.Label(head, textvariable=title_var, font=self._bold_font).pack(side="left")
        return head

    def _build_ui(self) -> None:
        # ttk labels whose text colour is set by hand, with the palette role it
        # comes from, so a change of theme can recolour them (_apply_theme).
        self._themed_labels: list[tuple[ttk.Label, str]] = []
        self._console_font = tkfont.Font(family="Consolas", size=self.CONSOLE_FONT_SIZE)
        self._bold_font = tkfont.nametofont("TkDefaultFont").copy()
        self._bold_font.configure(weight="bold")
        self._link_font = tkfont.nametofont("TkDefaultFont").copy()
        self._link_font.configure(underline=True)

        # Status bar first, so it keeps its place at the bottom at any size.
        # On the right, where finished fingerprints go: the one folder setting
        # worth seeing all the time, and a click opens it.
        status = ttk.Frame(self.root)
        status.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(status, textvariable=self.status_var, anchor="w").pack(
            side="left", fill="x", expand=True, padx=(8, 0), pady=3)
        self.keep_label = ttk.Label(status, cursor="hand2", foreground=self.QLINK_FG)
        self._themed_labels.append((self.keep_label, "link"))
        self.keep_label.pack(side="right", padx=8, pady=3)
        self.keep_label.bind("<Button-1>", lambda _e: self._open_keep_folder())
        Tooltip(self.keep_label, lambda: (
            f"{self._keep_dir(self._program_dir())}\n"
            "Click to open it. Change it in Settings, Folders."))
        ttk.Separator(self.root).pack(side="bottom", fill="x")

        self._build_toolbar()

        # The work area: the list and Now side by side on top, the console
        # below. The classic PanedWindow rather than ttk's: with the Windows
        # theme the ttk sash draws as blank background, so nothing says it can
        # be dragged.
        sash = {"sashrelief": "raised", "sashwidth": 8, "borderwidth": 0, "opaqueresize": True}
        self.panes = tk.PanedWindow(self.root, orient="vertical", **sash)
        self.panes.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self.hpanes = tk.PanedWindow(self.panes, orient="horizontal", **sash)
        self.panes.add(self.hpanes, stretch="always", minsize=150)

        # One mouse-wheel handler for the whole program (see _on_mousewheel):
        # these canvases scroll when the pointer is over them.
        self._wheel_targets: list[tk.Canvas] = []
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

        self._build_list_panel()
        self._build_now_panel()
        self._build_console_panel()
        self._build_settings()

        for var in (self.move_pklz_dir_var, self.bat_dir_var):
            var.trace_add("write", lambda *_a: self._update_keep_label())
        self._update_keep_label()
        # Where the dividers go can only be worked out once the window has a size.
        self.root.after(50, self._place_divider)

    def _build_toolbar(self) -> None:
        # Row 1: links in.
        bar = ttk.Frame(self.root)
        bar.pack(side="top", fill="x", padx=8, pady=(8, 4))
        ttk.Label(bar, text="Link:").pack(side="left", padx=(0, 6))
        self.url_var = tk.StringVar()
        # Cap input at 200 chars. Pasting tens of thousands of characters
        # froze the GUI on the main thread; legitimate URLs (even with
        # playlist + tracking params) stay well under this limit.
        url_validator = self.root.register(lambda s: len(s) <= 200)
        self.url_combo = ttk.Combobox(
            bar, textvariable=self.url_var, values=load_recent_urls(),
            validate="key", validatecommand=(url_validator, "%P"),
        )
        self.url_combo.pack(side="left", fill="x", expand=True, padx=(0, 6))
        # Enter in the link box adds to the list too, whenever Add could.
        self.url_combo.bind("<Return>", lambda _e: self.add_queue_btn.invoke())
        Tooltip(self.url_combo,
                "A channel, playlist or single page from YouTube, Archive.org, "
                "Mixcloud, SoundCloud or any other site yt-dlp supports. "
                "The arrow lists recent links.")
        self.add_queue_btn = ttk.Button(bar, text="Add", width=8, command=self._add_to_queue,
                                        state="disabled")
        self.add_queue_btn.pack(side="left")
        # Only clickable with something in the link box (_update_add_btn).
        self.url_var.trace_add("write", lambda *_a: self._update_add_btn())
        self.queue_import_btn = ttk.Button(
            bar, text="Import...", command=self._import_queue_from_file)
        self.queue_import_btn.pack(side="left", padx=(6, 0))
        Tooltip(self.queue_import_btn,
                "Add links from a text file: one per line, or @handles and "
                "channel IDs. Lines starting with # are skipped.")

        # Row 2: the run.
        bar = ttk.Frame(self.root)
        bar.pack(side="top", fill="x", padx=8, pady=(0, 8))
        self.start_btn = ttk.Button(bar, text="Download and fingerprint", command=self._start)
        self.start_btn.pack(side="left")
        Tooltip(self.start_btn,
                "Download, split and fingerprint every ticked link, top to bottom.")
        self.pause_btn = ttk.Button(bar, text="Pause", width=8,
                                    command=self._toggle_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=(6, 0))
        Tooltip(self.pause_btn,
                "Freeze what is running and start nothing new. Resume carries on "
                "from the same point.")
        self.skip_btn = ttk.Button(bar, text="Skip link", command=self._skip_current,
                                   state="disabled")
        self.skip_btn.pack(side="left", padx=(6, 0))
        Tooltip(self.skip_btn, "Move on to the next link once the current stage finishes.")
        self.cancel_btn = ttk.Button(bar, text="Stop", width=8, command=self._cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))
        Tooltip(self.cancel_btn, "End everything now.")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        # Download concurrency sits next to the buttons it affects rather than
        # in Settings: it is the setting most worth changing, and the one
        # people went looking for. Raised from 4 to 8 by default; the ceiling
        # is 32.
        ttk.Label(bar, text="Downloads at once:").pack(side="left", padx=(0, 4))
        self.parallel_var = tk.IntVar(value=self.DEFAULT_PARALLEL)
        self.parallel_spin = ttk.Spinbox(
            bar, from_=1, to=self.MAX_PARALLEL, textvariable=self.parallel_var, width=4,
        )
        self.parallel_spin.pack(side="left")
        Tooltip(self.parallel_spin,
                "More is faster on a good connection but uses more bandwidth and "
                "CPU. If downloads start failing (for example HTTP 429, too many "
                "requests), lower it. 1 to 32.")

        self.settings_btn = ttk.Button(bar, text="Settings", command=self._show_settings)
        self.settings_btn.pack(side="right")
        Tooltip(self.settings_btn,
                "Folders, fingerprinting and download options. Worth a look before "
                "a large job.")
        self.test_btn = ttk.Button(bar, text="Check setup", command=self._start_test_connection)
        self.test_btn.pack(side="right", padx=(0, 6))
        Tooltip(self.test_btn,
                "Check that every component works, and offer to install anything "
                "missing.")
        # The two ways of working on audio already on disk share one menu, but
        # stay two separate choices: one rewrites files in place and the other
        # never touches them, and each asks before it starts.
        self.disk_btn = ttk.Menubutton(bar, text="Audio on disk")
        disk_menu = self.disk_menu = tk.Menu(self.disk_btn, tearoff=0)
        disk_menu.add_command(
            label="Split + fingerprint the working folder",
            command=lambda: self._start_bats_only(split=True))
        disk_menu.add_command(
            label="Fingerprint only (files already split)",
            command=lambda: self._start_bats_only(split=False))
        self.disk_btn["menu"] = disk_menu
        self.disk_btn.pack(side="right", padx=(0, 6))
        Tooltip(self.disk_btn,
                "Fingerprint audio already in the working folder, without "
                "downloading. Split + fingerprint first cuts files over 12 minutes "
                "into 6-minute pieces; Fingerprint only uses them as they are.")

    def _build_list_panel(self) -> None:
        box = ttk.Frame(self.hpanes)
        self.hpanes.add(box, stretch="always", minsize=320)
        self.list_title_var = tk.StringVar(value="Your list")
        head = self._panel_header(box, self.list_title_var)
        self.queue_clear_btn = ttk.Button(head, text="Clear", style="Toolbutton",
                                          command=self._queue_clear)
        self.queue_clear_btn.pack(side="right")
        self.queue_remove_btn = ttk.Button(head, text="Remove ticked", style="Toolbutton",
                                           command=self._queue_remove)
        self.queue_remove_btn.pack(side="right")
        self.tick_all_btn = ttk.Button(head, text="Tick all", style="Toolbutton",
                                       command=self._tick_all)
        self.tick_all_btn.pack(side="right")

        # Rows of widgets in a canvas (see _build_queue_row): a Listbox cannot
        # hold tick boxes. White and outlined, like a list box.
        frame = ttk.Frame(box)
        frame.pack(fill="both", expand=True)
        self.queue_canvas = tk.Canvas(
            frame, bg=self.QROW_BG, highlightthickness=1,
            highlightbackground="#c8c8c8", highlightcolor="#c8c8c8",
        )
        q_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.queue_canvas.yview)
        self.queue_canvas.configure(yscrollcommand=q_scroll.set)
        self.queue_canvas.pack(side="left", fill="both", expand=True)
        q_scroll.pack(side="right", fill="y")
        self.queue_rows_frame = tk.Frame(self.queue_canvas, bg=self.QROW_BG)
        inner = self.queue_canvas.create_window((0, 0), window=self.queue_rows_frame, anchor="nw")
        self.queue_rows_frame.bind("<Configure>", lambda _e: self.queue_canvas.configure(
            scrollregion=self.queue_canvas.bbox("all")))
        self.queue_canvas.bind("<Configure>", lambda e: self.queue_canvas.itemconfigure(
            inner, width=e.width))
        self._wheel_targets.append(self.queue_canvas)

        # Backing state. queue_urls holds the URLs; queue_checks holds a
        # BooleanVar per URL (checked = include in the run); queue_active is the
        # index last clicked. queue_rows holds each row's widgets, in order.
        self.queue_urls: list[str] = []
        self.queue_checks: list[tk.BooleanVar] = []
        self.queue_active: int | None = None
        self.queue_rows: list[dict] = []
        self._queue_editable = True
        self._drag: dict | None = None
        # How many entries each link has: url -> {"n", "single", "date"},
        # saved with the list. _count_state holds counts in progress or failed.
        self.queue_counts: dict[str, dict] = {}
        self._count_state: dict[str, dict] = {}
        self._count_queue: queue.Queue[str] = queue.Queue()
        self._count_pending: set[str] = set()
        self._count_threads: list[threading.Thread] = []
        self._count_procs: list[subprocess.Popen] = []
        self._count_lock = threading.Lock()
        self._closing = False

    def _build_now_panel(self) -> None:
        box = ttk.Frame(self.hpanes)
        self.hpanes.add(box, stretch="always", minsize=260)
        self.now_title_var = tk.StringVar(value="Now")
        head = self._panel_header(box, self.now_title_var)
        self.now_detail_var = tk.StringVar(value="Idle")
        detail = ttk.Label(head, textvariable=self.now_detail_var, foreground=self.HELP_GREY)
        detail.pack(side="right")
        self._themed_labels.append((detail, "help"))
        self.now_bar = ttk.Progressbar(box, mode="determinate", maximum=100)
        self.now_bar.pack(fill="x", pady=(0, 4))
        self._progress_stage: tuple[str, float, int] | None = None

        # One row per download slot, or per fingerprint batch while those run.
        # They scroll when there are more of them (up to MAX_PARALLEL) than fit.
        frame = ttk.Frame(box)
        frame.pack(fill="both", expand=True)
        self.now_canvas = tk.Canvas(
            frame, bg=self.QROW_BG, highlightthickness=1,
            highlightbackground="#c8c8c8", highlightcolor="#c8c8c8",
        )
        now_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.now_canvas.yview)
        self.now_canvas.configure(yscrollcommand=now_scroll.set)
        self.now_canvas.pack(side="left", fill="both", expand=True)
        now_scroll.pack(side="right", fill="y")
        self.active_frame = tk.Frame(self.now_canvas, bg=self.QROW_BG)
        inner = self.now_canvas.create_window((0, 0), window=self.active_frame, anchor="nw")
        self.active_frame.bind("<Configure>", lambda _e: self.now_canvas.configure(
            scrollregion=self.now_canvas.bbox("all")))
        self.now_canvas.bind("<Configure>", lambda e: self.now_canvas.itemconfigure(
            inner, width=e.width))
        self._wheel_targets.append(self.now_canvas)
        self.slot_vars: list[tk.StringVar] = []
        self._init_slots(0)

    def _build_console_panel(self) -> None:
        box = ttk.Frame(self.panes)
        self.panes.add(box, stretch="always", minsize=120)
        # Kept on self: a StringVar nothing holds on to is collected, and
        # the label it feeds goes blank.
        self.console_title_var = tk.StringVar(value="Console")
        head = self._panel_header(box, self.console_title_var)
        self.clear_btn = ttk.Button(head, text="Clear", style="Toolbutton", command=self._clear_log)
        self.clear_btn.pack(side="right")
        self.copy_btn = ttk.Button(head, text="Copy", style="Toolbutton", command=self._copy_log)
        self.copy_btn.pack(side="right")
        # Follow: keep the newest line in view (_poll_log_queue). Off, the view
        # stays where it was scrolled to while lines are added below.
        self.console_follow_var = tk.BooleanVar(value=True)
        follow = ttk.Checkbutton(head, text="Follow", variable=self.console_follow_var,
                                 command=self._console_follow_changed)
        follow.pack(side="right", padx=(0, 8))
        Tooltip(follow, "Scroll to each new line as it is added. Turn off to read "
                        "earlier lines while a job runs.")
        # wrap="word": long file names wrap onto the next line whole instead of
        # breaking mid-word at the edge of the console. A plain Text with a ttk
        # scrollbar rather than ScrolledText, whose classic Windows scrollbar
        # cannot be coloured for dark mode.
        frame = ttk.Frame(box)
        frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(frame, height=12, font=self._console_font, wrap="word",
                                borderwidth=1, relief="solid", highlightthickness=0)
        log_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        # Ctrl and the wheel change the text size, like in a browser.
        self.log_text.bind("<Control-MouseWheel>", self._console_zoom)
        self.log_text.configure(state="disabled")
        self.log_text.tag_configure("ytdlp", foreground="#1565c0")
        self.log_text.tag_configure("bat", foreground="#e67e22")
        self.log_text.tag_configure("warning", foreground="#c0392b")
        self.log_text.tag_configure("splitter", foreground="#16a085")
        self.log_text.tag_configure("ts", foreground="#888888")

    def _build_settings(self) -> None:
        """Everything set once, in its own window: folders, then download and
        fingerprinting options. Built once and only hidden, never destroyed,
        so every widget reference here stays valid for the code that locks
        some of them during a run. Changes apply at once; there is nothing to
        save or cancel."""
        self.settings_win = tk.Toplevel(self.root)
        self.settings_win.title("Settings")
        self.settings_win.transient(self.root)
        self.settings_win.resizable(False, False)
        self.settings_win.protocol("WM_DELETE_WINDOW", self._hide_settings)
        self.settings_win.withdraw()
        self.settings_tabs = ttk.Notebook(self.settings_win)
        self.settings_tabs.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        # ---- General ----
        tab = ttk.Frame(self.settings_tabs, padding=8)
        self.settings_tabs.add(tab, text="General")
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Label(row, text="Appearance:").pack(side="left", padx=(0, 4))
        self.theme_var = tk.StringVar(value=THEME_CHOICES[0])
        self.theme_combo = ttk.Combobox(row, textvariable=self.theme_var, values=THEME_CHOICES,
                                        state="readonly", width=16)
        self.theme_combo.pack(side="left")
        self.theme_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_theme())
        self._help(
            tab,
            "Follow Windows goes dark when Windows apps are set to dark (Windows Settings, "
            "Personalisation, Colours), and changes along with it.",
            wrap=560,
        ).pack(anchor="w", padx=4, pady=(1, 8))

        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Label(row, text="Console text size:").pack(side="left", padx=(0, 4))
        self.console_font_var = tk.IntVar(value=self.CONSOLE_FONT_SIZE)
        self.console_font_spin = ttk.Spinbox(
            row, from_=self.CONSOLE_FONT_RANGE[0], to=self.CONSOLE_FONT_RANGE[1],
            textvariable=self.console_font_var, width=4, command=self._apply_console_font)
        self.console_font_spin.pack(side="left")
        self.console_font_spin.bind("<FocusOut>", lambda _e: self._apply_console_font())
        self.console_font_spin.bind("<Return>", lambda _e: self._apply_console_font())
        self._help(tab, "Ctrl and the mouse wheel over the console change it too.",
                   wrap=560).pack(anchor="w", padx=4, pady=(1, 8))

        self.keep_awake_var = tk.BooleanVar(value=True)
        self.confirm_stop_var = tk.BooleanVar(value=True)
        self.offer_resume_var = tk.BooleanVar(value=True)
        for var, label, explanation in (
            (self.keep_awake_var, "Keep the PC awake while a job runs",
             "Windows does not go to sleep until the job has finished. The screen can "
             "still turn off."),
            (self.confirm_stop_var, "Ask before Stop",
             "Stop ends everything at once, so this asks first."),
            (self.offer_resume_var, "Offer to continue an unfinished list",
             "If a list stopped part-way (Stop, a crash, the PC shutting down), the program "
             "offers to carry on where it stopped: when it starts, and when you press "
             "Download and fingerprint. Links that finished are not run again; the one it "
             "stopped in starts over."),
        ):
            ttk.Checkbutton(tab, text=label, variable=var).pack(anchor="w", pady=1)
            self._help(tab, explanation, wrap=560).pack(anchor="w", padx=22, pady=(0, 6))

        # ---- Folders ----
        tab = ttk.Frame(self.settings_tabs, padding=8)
        self.settings_tabs.add(tab, text="Folders")
        self.output_dir_var = tk.StringVar()
        # The program's own folder, which holds audfprint\, tools\ and work\.
        # Always where this file is; not a setting (it was one, from the days
        # of the .bat scripts, and a stale value could only point elsewhere).
        self.bat_dir_var = tk.StringVar(value=str(SCRIPT_DIR))
        self.move_pklz_dir_var = tk.StringVar()
        self._folder_row(
            tab, 0, "Working folder for audio:", self.output_dir_var,
            "Downloads land here and are deleted once each link is fingerprinted, "
            "so keep nothing else in it. Default: downloads in this program's folder.",
        )
        self._folder_row(
            tab, 2, "Keep finished fingerprints in:", self.move_pklz_dir_var,
            "Where finished .pklz files are collected. Nothing here is ever deleted. "
            "Default: pklz-files in this program's folder.",
        )
        # Keeping the downloads: off by default, and the folder only matters
        # (and can only be changed) while it is on.
        self.keep_audio_var = tk.BooleanVar(value=False)
        self.keep_audio_dir_var = tk.StringVar()
        self.keep_audio_check = ttk.Checkbutton(
            tab, text="Keep downloaded audio in:", variable=self.keep_audio_var)
        self.keep_audio_check.grid(row=4, column=0, sticky="w", padx=4, pady=(10, 0))
        self.keep_audio_entry = ttk.Entry(tab, textvariable=self.keep_audio_dir_var, width=58)
        self.keep_audio_entry.grid(row=4, column=1, sticky="we", padx=4, pady=(10, 0))
        self.keep_audio_entry.bind("<FocusOut>", lambda _e: self._normalize_var(self.keep_audio_dir_var))
        self.keep_audio_browse = ttk.Button(
            tab, text="Browse...", command=lambda: self._browse(self.keep_audio_dir_var))
        self.keep_audio_browse.grid(row=4, column=2, padx=4, pady=(10, 0))
        self._help(
            tab,
            "Off by default: downloaded audio is deleted once it has been fingerprinted. "
            "When on, each link's downloads are also kept here, in a folder named after "
            "the channel, as they were downloaded, before splitting. Default: audio in "
            "this program's folder.",
            wrap=560,
        ).grid(row=5, column=1, columnspan=2, sticky="w", padx=4, pady=(1, 2))
        self.keep_audio_var.trace_add("write", lambda *_a: self._update_keep_audio_row())
        self._update_keep_audio_row()
        tab.columnconfigure(1, weight=1)

        # ---- Downloads ----
        tab = ttk.Frame(self.settings_tabs, padding=8)
        self.settings_tabs.add(tab, text="Downloads")
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Label(row, text="Name downloaded files:").pack(side="left", padx=(0, 4))
        self.filename_template_var = tk.StringVar(value="%(title)s [%(id)s].%(ext)s")
        self.filename_template_entry = ttk.Entry(row, textvariable=self.filename_template_var)
        self.filename_template_entry.pack(side="left", fill="x", expand=True)
        self._help(tab, "A yt-dlp output template. The default gives “Title [id].m4a”.",
                   wrap=560).pack(anchor="w", padx=4, pady=(1, 8))
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Label(row, text="Extra download options:").pack(side="left", padx=(0, 4))
        self.extra_args_var = tk.StringVar(value="")
        self.extra_args_entry = ttk.Entry(row, textvariable=self.extra_args_var)
        self.extra_args_entry.pack(side="left", fill="x", expand=True)
        self._help(
            tab,
            "Passed to yt-dlp as they are, for anything the settings below do not cover "
            "(--limit-rate 2M, for example, caps each download at 2 MB/s).",
            wrap=560,
        ).pack(anchor="w", padx=4, pady=(1, 8))
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Label(row, text="Sign in with cookies from:").pack(side="left", padx=(0, 4))
        self.cookies_browser_var = tk.StringVar(value=COOKIE_BROWSERS[0])
        self.cookies_combo = ttk.Combobox(
            row, textvariable=self.cookies_browser_var, values=COOKIE_BROWSERS,
            state="readonly", width=12)
        self.cookies_combo.pack(side="left")
        self._help(
            tab,
            "For private, members-only or age-restricted items: yt-dlp uses your login "
            "from that browser. Close the browser first; Firefox works most reliably.",
            wrap=560,
        ).pack(anchor="w", padx=4, pady=(1, 8))

        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Label(row, text="Skip items shorter than").pack(side="left", padx=(0, 4))
        self.min_seconds_var = tk.IntVar(value=0)
        self.min_seconds_spin = ttk.Spinbox(
            row, from_=0, to=3600, increment=15, textvariable=self.min_seconds_var, width=6)
        self.min_seconds_spin.pack(side="left")
        ttk.Label(row, text="seconds, or longer than").pack(side="left", padx=4)
        self.max_minutes_var = tk.IntVar(value=0)
        self.max_minutes_spin = ttk.Spinbox(
            row, from_=0, to=1440, increment=10, textvariable=self.max_minutes_var, width=6)
        self.max_minutes_spin.pack(side="left")
        ttk.Label(row, text="minutes").pack(side="left", padx=(4, 0))
        self._help(
            tab,
            "0 means no limit. 60 seconds, for example, leaves out YouTube Shorts and "
            "other short clips.",
            wrap=560,
        ).pack(anchor="w", padx=4, pady=(1, 8))

        row = ttk.Frame(tab)
        row.pack(fill="x")
        self.skip_done_var = tk.BooleanVar(value=False)
        self.skip_done_check = ttk.Checkbutton(
            row, text="Skip items already fingerprinted in an earlier run",
            variable=self.skip_done_var)
        self.skip_done_check.pack(side="left")
        self.forget_done_btn = ttk.Button(row, text="Forget them", command=self._forget_done)
        self.forget_done_btn.pack(side="right")
        self.done_count_var = tk.StringVar()
        done_count = ttk.Label(row, textvariable=self.done_count_var, foreground=self.HELP_GREY)
        done_count.pack(side="right", padx=6)
        self._themed_labels.append((done_count, "help"))
        self._help(
            tab,
            "Each item is remembered once its link has been fingerprinted, in "
            f"{DONE_FILE.name} in the program folder. Running a channel again then only "
            "fetches what is new.",
            wrap=560,
        ).pack(anchor="w", padx=22, pady=(0, 8))

        self.verbose_var = tk.BooleanVar(value=False)
        self.verbose_check = ttk.Checkbutton(
            tab, text="Show every line of download output in the console",
            variable=self.verbose_var)
        self.verbose_check.pack(anchor="w", pady=1)
        self._help(
            tab,
            "Adds everything yt-dlp and audfprint print to the console: each "
            "download's progress, redirects, retries and warnings. Useful when a "
            "download fails; otherwise it floods the console. It can't be changed "
            "while a job is running.",
            wrap=560,
        ).pack(anchor="w", padx=22, pady=(0, 6))
        self.open_folder_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(tab, text="Open the audio folder when a link starts",
                        variable=self.open_folder_var).pack(anchor="w", pady=1)

        # ---- Fingerprinting ----
        tab = ttk.Frame(self.settings_tabs, padding=8)
        self.settings_tabs.add(tab, text="Fingerprinting")
        row = ttk.Frame(tab)
        row.pack(fill="x")
        # Concurrent batches is the one that matters. Measured here: the old
        # single sequential audfprint did 32 files in 117s; eight concurrent
        # batches did the same 32 in 28s. It is capped rather than opened up
        # because a 1000-file batch peaks around 5.5 GB, so 4 is roughly 22 GB
        # of this box's 64 GB and leaves room for everything else running.
        ttk.Label(row, text="Fingerprint jobs at once:").pack(side="left", padx=(0, 4))
        self.fp_concurrency_var = tk.IntVar(value=self.DEFAULT_FP_JOBS)
        self.fp_concurrency_spin = ttk.Spinbox(
            row, from_=1, to=16, textvariable=self.fp_concurrency_var, width=5,
        )
        self.fp_concurrency_spin.pack(side="left")
        # There is deliberately no control for audfprint's own --ncores. It is
        # pinned to AUDFPRINT_NCORES; see that constant for the measurements.

        # Files per .pklz. Bigger means fewer, larger shards, which matters
        # downstream: a matcher reloads every .pklz on every run, so hundreds of
        # small ones pay that cost hundreds of times.
        ttk.Label(row, text="Recordings per file:").pack(side="left", padx=(16, 4))
        self.batch_size_var = tk.IntVar(value=1000)
        self.batch_size_spin = ttk.Spinbox(
            row, from_=50, to=5000, increment=50,
            textvariable=self.batch_size_var, width=7,
        )
        self.batch_size_spin.pack(side="left")
        self._help(row, "(1000 recommended)").pack(side="left", padx=(6, 0))
        self._help(
            tab,
            "Jobs are audfprint processes working at the same time, 4 by default. A link "
            "with fewer recordings than Recordings per file is one batch; from 40 files, "
            "the jobs share it and their work is merged into one .pklz. "
            "Each job can use up to about 5.5 GB of memory, so raise jobs only if you have "
            "the RAM and CPU cores. Keep recordings per file high: a matcher reloads every "
            ".pklz on each search, so many small files slow down every search later.",
            wrap=560,
        ).pack(anchor="w", padx=4, pady=(1, 8))
        self.split_long_var = tk.BooleanVar(value=True)
        self.open_pklz_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(tab, text="Split long recordings after downloading (recommended)",
                        variable=self.split_long_var).pack(anchor="w", pady=1)
        self._help(
            tab,
            "Files over 12 minutes are split into 6-minute pieces (the last piece "
            "takes the remainder), so a match points to a 6-minute window instead of "
            "a whole recording. Applies to downloads; the Audio on disk choices "
            "decide for themselves.",
            wrap=560,
        ).pack(anchor="w", padx=22, pady=(0, 6))
        ttk.Checkbutton(tab, text="Open the fingerprints folder when a run finishes",
                        variable=self.open_pklz_var).pack(anchor="w", pady=1)
        self.notify_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(tab, text="Play a sound and flash the taskbar button when a run finishes",
                        variable=self.notify_var).pack(anchor="w", pady=1)
        # The record of finished batches that lets Audio on disk carry on
        # after a restart (_load_fingerprinted). Off, it is kept in memory.
        self.fp_record_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tab, text=f"Write {self.FINGERPRINTED_RECORD} while fingerprinting",
                        variable=self.fp_record_var).pack(anchor="w", pady=(7, 1))
        self._help(
            tab,
            f"Off by default. A record in work\\pklz of which batches are finished. Without "
            f"it, Audio on disk still carries on after Stop as long as the program stays "
            f"open; with it, also after the program was closed or crashed.",
            wrap=560,
        ).pack(anchor="w", padx=22, pady=(0, 6))

        close = ttk.Frame(self.settings_win)
        close.pack(fill="x", padx=8, pady=8)
        ttk.Button(close, text="Close", command=self._hide_settings).pack(side="right")

    def _show_settings(self, tab: int | None = None) -> None:
        self._update_done_count()
        if tab is not None:
            self.settings_tabs.select(tab)
        self.settings_win.deiconify()
        self.settings_win.lift()
        # Over the main window rather than wherever Windows feels like, so it
        # reads as belonging to the button that opened it.
        self.root.update_idletasks()
        self.settings_win.geometry(
            f"+{self.root.winfo_rootx() + 60}+{self.root.winfo_rooty() + 80}")

    def _hide_settings(self) -> None:
        # Folders typed into the boxes are tidied when the window closes too.
        for var in (self.output_dir_var, self.bat_dir_var, self.move_pklz_dir_var,
                    self.keep_audio_dir_var):
            self._normalize_var(var)
        self.settings_win.withdraw()

    def _program_dir(self) -> Path:
        return Path(norm_path(self.bat_dir_var.get()) or SCRIPT_DIR)

    def _set_window_icon(self) -> None:
        """Give every window the fingerprint icon, one image per size.

        iconbitmap with the .ico left the taskbar button blurred: Windows was
        handed one size and scaled it. iconphoto takes each size separately,
        and Windows picks the one that fits. The sizes come straight out of
        fingerprinter.ico, which stores each as PNG, a format Tk reads itself,
        so this needs neither a second copy of the icon nor Pillow."""
        try:
            data = dependencies.ICON_FILE.read_bytes()
            count = struct.unpack_from("<H", data, 4)[0]
            photos = []
            for i in range(count):
                width, _h, _c, _r, _p, _b, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
                png = data[offset:offset + size]
                if (width or 256) in (16, 24, 32, 48, 256) and png.startswith(b"\x89PNG"):
                    photos.append(tk.PhotoImage(master=self.root, data=base64.b64encode(png).decode("ascii")))
            if photos:
                self._icon_photos = photos          # Tk forgets images nothing holds on to
                self.root.iconphoto(True, *sorted(photos, key=lambda p: -p.width()))
                return
        except (OSError, struct.error, tk.TclError):
            pass
        try:
            self.root.iconbitmap(default=str(dependencies.ICON_FILE))
        except tk.TclError:
            pass                            # no icon file: Tk's own icon then

    # ---- appearance and power (Settings, General) --------------------------------

    def _apply_console_font(self) -> None:
        low, high = self.CONSOLE_FONT_RANGE
        size = min(high, max(low, self._safe_int(self.console_font_var, self.CONSOLE_FONT_SIZE)))
        self._console_font.configure(size=size)

    def _console_zoom(self, event: tk.Event) -> str:
        """Ctrl and the wheel over the console: one point bigger or smaller."""
        low, high = self.CONSOLE_FONT_RANGE
        size = int(self._console_font.cget("size")) + (1 if event.delta > 0 else -1)
        size = min(high, max(low, size))
        self.console_font_var.set(size)
        self._console_font.configure(size=size)
        return "break"

    @staticmethod
    def _windows_uses_dark() -> bool:
        """Whether Windows apps are set to dark (Settings, Personalisation, Colours)."""
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        except (OSError, ImportError):
            return False

    def _theme_mode(self) -> str:
        choice = self.theme_var.get()
        if choice in ("Light", "Dark"):
            return choice.lower()
        return "dark" if self._windows_uses_dark() else "light"

    def _follow_windows_theme(self) -> None:
        """While Appearance is Follow Windows, notice Windows changing over."""
        if self.theme_var.get() == THEME_CHOICES[0] and self._theme_mode() != self._theme_now:
            self._apply_theme()
        self.root.after(10_000, self._follow_windows_theme)

    def _apply_theme(self) -> None:
        """Colour the whole program for Settings, General, Appearance.

        ttk draws most of it: light uses the native Windows theme (as before),
        dark uses clam, the one built-in theme that takes colours at all. The
        rest is coloured by hand from PALETTES: the rows of the list and Now,
        the console and its colour tags, borders, dividers, tooltips, and the
        text colour of help lines and links. Message boxes are drawn by Windows
        and stay light."""
        mode = self._theme_mode()
        self._theme_now = mode
        p = PALETTES[mode]
        self._style_ttk(mode, p)
        self.QROW_BG, self.QROW_SELECTED = p["row_bg"], p["row_selected"]
        self.QLINK_FG, self.QMUTED_FG, self.QFG, self.QFIELD = p["link"], p["muted"], p["fg"], p["field"]
        self.QREMOVE_HOVER_FG = p["remove_hover"]
        self.HELP_GREY, self.HELP_WARN = p["help"], p["warn"]
        for window in (self.root, self.settings_win):
            window.configure(bg=p["bg"])
        for panes in (self.panes, self.hpanes):
            panes.configure(bg=p["sash"])
        for canvas in (self.queue_canvas, self.now_canvas):
            canvas.configure(bg=p["row_bg"], highlightbackground=p["border"], highlightcolor=p["border"])
        self.queue_rows_frame.configure(bg=p["row_bg"])
        self.active_frame.configure(bg=p["row_bg"])
        self.log_text.configure(bg=p["console_bg"], fg=p["console_fg"], insertbackground=p["console_fg"],
                                selectbackground=p["highlight"], selectforeground=p["highlight_fg"])
        for tag, colour in p["tags"].items():
            self.log_text.tag_configure(tag, foreground=colour)
        self._themed_labels = [(label, role) for label, role in self._themed_labels if label.winfo_exists()]
        for label, role in self._themed_labels:
            label.configure(foreground=p[role])
        Tooltip.colours = {"bg": p["tip_bg"], "fg": p["tip_fg"]}
        self.disk_menu.configure(**self._menu_colours())
        for combo in (self.url_combo, self.cookies_combo, self.theme_combo):
            self._style_combo_popdown(combo, p)
        # The rows are rebuilt in the new colours, keeping what they show.
        self._refresh_queue()
        texts = [v.get() for v in self.slot_vars]
        self._init_slots(len(texts), texts or None)
        for window in (self.root, self.settings_win):
            self._dark_title_bar(window, mode == "dark")

    def _menu_colours(self) -> dict:
        p = PALETTES[self._theme_now]
        return {"bg": p["menu"], "fg": p["menu_fg"], "activebackground": p["highlight"],
                "activeforeground": p["highlight_fg"]}

    def _on_map_title_bar(self, event: tk.Event) -> None:
        """A window's title bar can only be made dark once Windows has made
        the frame that draws it, when the window is first shown; setting it
        in _apply_theme before that does nothing. Hence again on every <Map>
        (bound on the main window, Settings and the timed Yes/No box). The
        main window's first also gives it its app ID (_set_window_app_id)."""
        window = event.widget
        if isinstance(window, (tk.Tk, tk.Toplevel)):
            self._dark_title_bar(window, self._theme_now == "dark")
        if window is self.root and not self._window_app_id:
            self._window_app_id = self._set_window_app_id(True)

    def _set_window_app_id(self, on: bool) -> bool:
        """The main window's app ID and how to start the program again, which
        Windows reads when the window's taskbar button is pinned. Without
        them, pinning the running program made a pin to a bare pythonw.exe
        that opened nothing. Set once the window has its frame (its first
        <Map>); removed again before it closes (_on_close), as Windows asks
        of a program that sets them. True if they were set."""
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            from ctypes import wintypes

            class Guid(ctypes.Structure):
                _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                            ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

            class PropertyKey(ctypes.Structure):
                _fields_ = [("fmtid", Guid), ("pid", wintypes.DWORD)]

            class PropVariant(ctypes.Structure):
                _fields_ = [("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3),
                            ("value", ctypes.c_void_p), ("unused", ctypes.c_void_p)]

            def guid(text: str) -> Guid:
                g = Guid()
                ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(g))
                return g

            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            store = ctypes.c_void_p()
            ctypes.oledll.shell32.SHGetPropertyStoreForWindow(          # IID_IPropertyStore
                hwnd, ctypes.byref(guid("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")), ctypes.byref(store))
            vtable = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0],
                                 ctypes.POINTER(ctypes.c_void_p))
            set_value = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(PropertyKey),
                                           ctypes.POINTER(PropVariant))(vtable[6])
            commit = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)(vtable[7])
            release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
            # System.AppUserModel.RelaunchCommand, RelaunchIconResource,
            # RelaunchDisplayNameResource, then ID, which is read with them.
            values = [
                (2, f'"{dependencies._shortcut_target()}" "{SCRIPT_DIR / "yt-fingerprinter.pyw"}"'),
                (3, f"{dependencies.ICON_FILE},0"),
                (4, "Fingerprinter"),
                (5, dependencies.APP_ID),
            ]
            try:
                for pid, text in (values if on else values[::-1]):
                    key = PropertyKey(guid("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), pid)
                    buf = ctypes.create_unicode_buffer(text)
                    var = PropVariant(31 if on else 0)                   # VT_LPWSTR, or VT_EMPTY
                    if on:
                        var.value = ctypes.cast(buf, ctypes.c_void_p)
                    set_value(store, ctypes.byref(key), ctypes.byref(var))
                commit(store)
            finally:
                release(store)
            return on
        except Exception:  # noqa: BLE001 - pinning is not worth an error
            return False

    def _style_ttk(self, mode: str, p: dict) -> None:
        style = ttk.Style()
        if mode == "light":
            if style.theme_use() != self._native_theme:
                style.theme_use(self._native_theme)
            return
        style.theme_use("clam")
        bg, fg, field, button = p["bg"], p["fg"], p["field"], p["button"]
        border, hover, pressed, dim = p["border"], p["hover"], p["pressed"], p["disabled_fg"]
        style.configure(".", background=bg, foreground=fg, fieldbackground=field, bordercolor=border,
                        darkcolor=bg, lightcolor=bg, troughcolor=field, selectbackground=p["select"],
                        selectforeground=fg, insertcolor=fg, arrowcolor=fg, focuscolor=border)
        # clam's own state maps win over configure, so every one that carries
        # a light colour is replaced: unfocused selections, pressed bevels, tabs.
        style.map(".", background=[("disabled", bg)], foreground=[("disabled", dim)],
                  selectbackground=[("!focus", p["select"])], selectforeground=[("!focus", fg)])
        for name in ("TButton", "TMenubutton"):
            style.configure(name, background=button, lightcolor=button, darkcolor=button)
            style.map(name, background=[("disabled", bg), ("pressed", pressed), ("active", hover)],
                      foreground=[("disabled", dim)], lightcolor=[("pressed", pressed)],
                      darkcolor=[("pressed", pressed)])
        style.configure("Toolbutton", background=bg, bordercolor=bg, lightcolor=bg, darkcolor=bg)
        style.map("Toolbutton", background=[("disabled", bg), ("pressed", pressed), ("active", hover)],
                  foreground=[("disabled", dim)], lightcolor=[("pressed", pressed)],
                  darkcolor=[("pressed", pressed)])
        for name in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(name, fieldbackground=field, foreground=fg, background=button,
                            insertcolor=fg, arrowcolor=fg, lightcolor=field, darkcolor=field)
            style.map(name, fieldbackground=[("readonly", field), ("disabled", bg)],
                      foreground=[("disabled", dim)], background=[("active", hover)],
                      selectbackground=[("readonly", field)], selectforeground=[("readonly", fg)])
        style.configure("TCheckbutton", background=bg, foreground=fg, indicatorbackground=field,
                        indicatorforeground=fg, upperbordercolor=border, lowerbordercolor=border)
        style.map("TCheckbutton", background=[("active", bg)],
                  indicatorbackground=[("disabled", bg), ("pressed", pressed)])
        self._tick_indicator(style, p)
        style.configure("TNotebook", background=bg, bordercolor=border)
        style.configure("TNotebook.Tab", background=button, foreground=fg, lightcolor=button, bordercolor=border)
        style.map("TNotebook.Tab", background=[("selected", bg), ("active", hover)],
                  lightcolor=[("selected", bg), ("!selected", button)])
        style.configure("TLabelframe", background=bg, bordercolor=border)
        style.configure("TLabelframe.Label", background=bg, foreground=fg)
        style.configure("Vertical.TScrollbar", background=button, troughcolor=bg, arrowcolor=fg,
                        lightcolor=button, darkcolor=button)
        style.map("Vertical.TScrollbar", background=[("active", hover)])
        style.configure("Horizontal.TProgressbar", background=p["accent"], troughcolor=field,
                        lightcolor=p["accent"], darkcolor=p["accent"])
        style.configure("TSeparator", background=border)

    def _tick_indicator(self, style: ttk.Style, p: dict) -> None:
        """clam, the theme dark mode is built on, marks a ticked box with a
        cross, which reads as "off" (and next to Follow, as "close"). Its
        checkbuttons get a box with a tick instead, drawn here in the
        palette's colours at the screen's scaling. The images are made, and
        the element added to clam, once; they must be kept referenced."""
        if getattr(self, "_tick_images", None) is None:
            size = max(11, round(9.5 * float(self.root.tk.call("tk", "scaling"))))
            stroke = max(2, size // 6)

            def draw(fill: str, border: str, tick: str | None = None) -> tk.PhotoImage:
                # 4 transparent pixels on the right keep the label off the box.
                img = tk.PhotoImage(master=self.root, width=size + 4, height=size)
                img.put(border, to=(0, 0, size, size))
                img.put(fill, to=(1, 1, size - 1, size - 1))
                if tick:
                    points = [(0.22, 0.52), (0.42, 0.72), (0.80, 0.30)]
                    for (x0, y0), (x1, y1) in zip(points, points[1:]):
                        for i in range(2 * size + 1):
                            x = round((x0 + (x1 - x0) * i / (2 * size)) * size) - stroke // 2
                            y = round((y0 + (y1 - y0) * i / (2 * size)) * size) - stroke // 2
                            img.put(tick, to=(x, y, x + stroke, y + stroke))
                return img

            self._tick_images = images = {
                "off": draw(p["field"], p["muted"]),
                "on": draw(p["link"], p["link"], p["bg"]),
                "off_disabled": draw(p["bg"], p["border"]),
                "on_disabled": draw(p["border"], p["border"], p["disabled_fg"]),
            }
            style.element_create("Fp.tick", "image", images["off"],
                                 ("disabled", "selected", images["on_disabled"]),
                                 ("disabled", images["off_disabled"]),
                                 ("selected", images["on"]), sticky="w")

        def swap(layout: list) -> list:
            return [("Fp.tick" if name == "Checkbutton.indicator" else name,
                     dict(opts, children=swap(opts["children"])) if "children" in opts else opts)
                    for name, opts in layout]
        style.layout("TCheckbutton", swap(style.layout("TCheckbutton")))

    @staticmethod
    def _style_combo_popdown(combo: ttk.Combobox, p: dict) -> None:
        """A combobox's drop-down list is a classic Listbox the theme misses."""
        try:
            popdown = combo.tk.eval(f"ttk::combobox::PopdownWindow {combo}")
            combo.tk.call(f"{popdown}.f.l", "configure", "-background", p["list"], "-foreground", p["list_fg"],
                          "-selectbackground", p["highlight"], "-selectforeground", p["highlight_fg"])
        except tk.TclError:
            pass

    @staticmethod
    def _dark_title_bar(window: tk.Misc, dark: bool) -> None:
        """Windows 10 (20H1 on; attribute 19 before that) and 11 can draw a
        window's title bar dark. Older Windows ignores it. The bar keeps its
        old colour until its caption is drawn again, so that is forced by
        switching the caption to its other look (active or inactive) and
        back; a plain redraw of the frame did not do it."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(window.winfo_id())
            value = ctypes.c_int(1 if dark else 0)
            for attribute in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
            active = user32.GetForegroundWindow() == hwnd
            for state in (not active, active):
                user32.SendMessageW(hwnd, 0x0086, state, 0)     # WM_NCACTIVATE
        except Exception:  # noqa: BLE001 - a light title bar is not worth an error
            pass

    def _keep_awake(self, on: bool) -> None:
        """While a job runs, ask Windows not to go to sleep (Settings, General).
        The request belongs to the thread that makes it, always the UI thread
        here, which lasts as long as the program; it is withdrawn when the job
        ends, and lapses by itself if the program closes. The screen can still
        turn off."""
        if sys.platform != "win32":
            return
        es_continuous, es_system_required = 0x80000000, 0x00000001
        flags = es_continuous | (es_system_required if on and self.keep_awake_var.get() else 0)
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
        except Exception:  # noqa: BLE001
            pass

    def _update_keep_label(self) -> None:
        """Status bar: where finished fingerprints go, shortened to fit."""
        path = str(self._keep_dir(self._program_dir()))
        parts = Path(path).parts
        if len(path) > 48 and len(parts) > 3:
            path = str(Path("…", *parts[-2:]))
        self.keep_label.configure(text=f"Fingerprints go to {path}")

    def _open_keep_folder(self) -> None:
        path = self._keep_dir(self._program_dir())
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._log(f"[!] Could not create {path}: {e}")
            return
        self._open_folder(path)

    def _place_divider(self, attempt: int = 0) -> None:
        """Share the window: the console gets CONSOLE_MIN_HEIGHT or
        CONSOLE_MIN_SHARE of the height, whichever is more, and the list gets
        LIST_SHARE of the width beside Now. Where the user dragged the dividers
        last time wins, if it still fits."""
        self.root.update_idletasks()
        total = self.panes.winfo_height()
        width = self.hpanes.winfo_width()
        if total < 200 or width < 400:
            if attempt < 20:
                self.root.after(50, self._place_divider, attempt + 1)
            return
        console = getattr(self, "_saved_console_height", 0)
        if not 120 <= console <= total - 150:
            console = max(self.CONSOLE_MIN_HEIGHT, int(total * self.CONSOLE_MIN_SHARE))
        self.panes.sash_place(0, 0, total - console)
        share = getattr(self, "_saved_list_share", 0.0)
        if not 0.2 <= share <= 0.8:
            share = self.LIST_SHARE
        self.hpanes.sash_place(0, int(width * share), 0)

    def _console_height(self) -> int:
        """Current console height, including the divider above it."""
        try:
            return int(self.panes.winfo_height() - self.panes.sash_coord(0)[1])
        except (tk.TclError, ValueError, IndexError):
            return 0

    def _list_share(self) -> float:
        """How much of the width the list has, for remembering the divider."""
        try:
            width = self.hpanes.winfo_width()
            return round(self.hpanes.sash_coord(0)[0] / width, 3) if width > 1 else 0.0
        except (tk.TclError, ValueError, IndexError):
            return 0.0

    def _on_mousewheel(self, event: tk.Event) -> str | None:
        """Scroll whatever is under the pointer.

        One handler for the whole program. Each scrollable area used to bind
        the wheel for itself while the pointer was over it, and unbind *every*
        wheel binding when it left, so moving between them could leave nothing
        scrolling at all.

        Walks up from the widget under the pointer to the nearest registered
        canvas that can still move in that direction. Text boxes, spinboxes and
        comboboxes handle the wheel themselves and are left alone."""
        try:
            widget = self.root.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):   # a Tk-internal window with no Python object
            return None
        delta = getattr(event, "delta", 0)
        if not delta:
            return None
        steps = -int(delta / 120) or (-1 if delta > 0 else 1)
        while widget is not None:
            try:
                cls = widget.winfo_class()
            except tk.TclError:
                return None
            if cls in ("Text", "TSpinbox", "TCombobox", "Listbox"):
                return None
            if widget in self._wheel_targets:
                first, last = widget.yview()
                if (steps < 0 and first > 0) or (steps > 0 and last < 1):
                    widget.yview_scroll(steps, "units")
                    return "break"
            widget = widget.master
        return None

    # ---- the Now panel ---------------------------------------------------------

    def _set_now_title(self, text: str) -> None:
        self.root.after(0, self.now_title_var.set, text)

    def _set_progress(self, label: str, done: int | None = None, total: int | None = None) -> None:
        """Thread-safe: what the job is doing, for the Now panel's header and
        bar. With done/total the bar fills and, once a few are done, a rough
        time left is shown; without them the bar only shows activity."""
        now = time.monotonic()
        stage = self._progress_stage
        if stage is None or stage[0] != label or (done or 0) < stage[2]:
            stage = self._progress_stage = (label, now, done or 0)
        if total:
            done = done or 0
            text = f"{label} {done:,} of {total:,}"
            if done - stage[2] >= 3 and done < total:
                left = (now - stage[1]) / (done - stage[2]) * (total - done)
                text += f", {fmt_time(left)} left"
            pct: float | None = min(100.0, done * 100.0 / total)
        else:
            text, pct = label, None
        self.root.after(0, self._apply_progress, text, pct)

    def _apply_progress(self, text: str, pct: float | None) -> None:
        self.now_detail_var.set(text)
        if pct is None:
            if str(self.now_bar.cget("mode")) != "indeterminate":
                self.now_bar.configure(mode="indeterminate")
                self.now_bar.start(15)
        else:
            if str(self.now_bar.cget("mode")) != "determinate":
                self.now_bar.stop()
                self.now_bar.configure(mode="determinate")
            self.now_bar.configure(value=pct)

    def _reset_progress(self) -> None:
        """UI thread, when a job ends: back to an idle Now panel."""
        self._progress_stage = None
        self.now_title_var.set("Now")
        self.now_detail_var.set("Idle")
        self.now_bar.stop()
        self.now_bar.configure(mode="determinate", value=0)
        self._init_slots(0)

    def _fill_default_folders(self) -> None:
        """Tidy the saved folder paths into Windows form, and fill an empty
        working folder or fingerprints folder with the one that ships inside
        the program folder, creating it if it is missing."""
        for var in (self.output_dir_var, self.bat_dir_var, self.move_pklz_dir_var,
                    self.keep_audio_dir_var):
            self._normalize_var(var)
        base = Path(self.bat_dir_var.get())
        if not self.keep_audio_dir_var.get():
            self.keep_audio_dir_var.set(str(base / KEEP_AUDIO_DIR))   # made when first used
        for var, name in ((self.output_dir_var, DOWNLOADS_DIR),
                          (self.move_pklz_dir_var, PKLZ_DIR)):
            default = base / name
            if not var.get():
                var.set(str(default))
            if Path(var.get()) == default:
                try:
                    default.mkdir(exist_ok=True)
                except OSError:
                    pass    # a read-only program folder; Start reports the folder

    def _normalize_var(self, var: tk.StringVar) -> None:
        tidy = norm_path(var.get())
        if tidy != var.get():
            var.set(tidy)

    def _keep_dir(self, bat_dir: Path) -> Path:
        """Where finished .pklz files go: the chosen folder, or pklz-files in the
        program folder if the box was cleared. Never work\\pklz, which is
        emptied before the next link."""
        return Path(norm_path(self.move_pklz_dir_var.get()) or bat_dir / PKLZ_DIR)

    def _folders_clash(self, bat_dir: str) -> bool:
        """True, after saying why, if a folder the user keeps things in lies
        inside the program's work folder, which runs empty."""
        work = (Path(bat_dir) / WORK_DIR).resolve()
        for label, value in (
            ("Working folder for audio", norm_path(self.output_dir_var.get())),
            ("Keep finished fingerprints in", str(self._keep_dir(Path(bat_dir)))),
        ):
            if not value:
                continue
            path = Path(value).resolve()
            if path == work or work in path.parents:
                messagebox.showerror(
                    "Pick another folder",
                    f"{label} is inside the program's work folder:\n{path}\n\n"
                    f"That folder is emptied during runs. Choose a different one.",
                    parent=self.root,
                )
                return True
        if self.keep_audio_var.get():
            kept = self._keep_dir_audio(Path(bat_dir)).resolve()
            working = Path(norm_path(self.output_dir_var.get()) or bat_dir).resolve()
            if kept == work or work in kept.parents or kept == working or working in kept.parents:
                messagebox.showerror(
                    "Pick another folder",
                    f"Keep downloaded audio in is inside the working folder or the "
                    f"program's work folder:\n{kept}\n\nAudio in those is deleted during "
                    f"runs, and Audio on disk would fingerprint it again. Choose a folder "
                    f"outside them.",
                    parent=self.root,
                )
                return True
        return False

    def _update_keep_audio_row(self) -> None:
        """The kept-audio folder can only be changed while keeping is on."""
        state = "normal" if self.keep_audio_var.get() else "disabled"
        self.keep_audio_entry.config(state=state)
        self.keep_audio_browse.config(state=state)

    def _browse(self, var: tk.StringVar) -> None:
        current = norm_path(var.get())
        options = {"initialdir": current} if current and Path(current).is_dir() else {}
        path = filedialog.askdirectory(parent=self.root, **options)
        if path:
            var.set(norm_path(path))

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
                if self.console_follow_var.get():
                    self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    def _console_follow_changed(self) -> None:
        """Turned back on: catch up with the newest line straight away."""
        if self.console_follow_var.get():
            self.log_text.see("end")

    def _set_status(self, text: str) -> None:
        self.root.after(0, self.status_var.set, text)

    # ------------------------- config load/save -------------------------------

    # (var_name, json_key, expected_type)
    _CONFIG_FIELDS: tuple = (
        ("output_dir_var", "output_dir", str),
        # "bat_dir", the program folder, is no longer a setting: an old value
        # in config.json is ignored, and dropped at the next save.
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
        ("cookies_browser_var", "cookies_browser", str),
        ("min_seconds_var", "skip_shorter_than", int),
        ("max_minutes_var", "skip_longer_than", int),
        ("skip_done_var", "skip_done", bool),
        ("notify_var", "notify", bool),
        ("keep_audio_var", "keep_audio", bool),
        ("keep_audio_dir_var", "keep_audio_dir", str),
        ("theme_var", "theme", str),
        ("console_font_var", "console_font_size", int),
        ("keep_awake_var", "keep_awake", bool),
        ("confirm_stop_var", "confirm_stop", bool),
        ("offer_resume_var", "offer_resume", bool),
        ("fp_record_var", "write_fingerprinted_json", bool),
        ("console_follow_var", "console_follow", bool),
    )

    def _apply_config(self, cfg: dict) -> None:
        """Apply a loaded config dict to the relevant Tk vars. Bad values silently skipped."""
        # Every setting is saved on close, defaults included, so a settings
        # file from before CONFIG_VERSION 3 holds "verbose": true because that
        # was the default, not because anyone chose it. The default is now off.
        try:
            version = int(cfg.get("config_version") or 0)
        except (TypeError, ValueError):
            version = 0
        if version < 3:
            cfg = {k: v for k, v in cfg.items() if k != "verbose"}
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
        self._migrate_program_folder(cfg)
        # Where the dividers were last dragged to (see _place_divider).
        for key, attr, conv in (("console_height", "_saved_console_height", int),
                                ("list_share", "_saved_list_share", float)):
            try:
                setattr(self, attr, conv(cfg.get(key) or 0))
            except (TypeError, ValueError):
                setattr(self, attr, conv(0))
        # Restore the saved channel queue (crash recovery / persistence).
        saved_queue = cfg.get("queue")
        if isinstance(saved_queue, list):
            self.queue_urls = [str(u) for u in saved_queue if u]
            self.queue_checks = [self._new_check() for _ in self.queue_urls]
            self.queue_active = None
            counts = cfg.get("queue_counts")
            if isinstance(counts, dict):
                self.queue_counts = {
                    u: c for u, c in counts.items()
                    if u in self.queue_urls and isinstance(c, dict)
                    and isinstance(c.get("n"), int)
                }
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

    def _migrate_program_folder(self, cfg: dict) -> None:
        """"This program's folder" is no longer a setting. A config that had
        it pointed at another folder read an empty working, fingerprints and
        kept-audio folder as the default one inside that folder; those stay
        where they were, set outright, rather than moving without a word. The
        next save drops "bat_dir", so this happens once."""
        old = norm_path(str(cfg.get("bat_dir") or ""))
        if not old or Path(old) == SCRIPT_DIR or not Path(old).is_dir():
            return
        kept = []
        for var, name, label in ((self.output_dir_var, DOWNLOADS_DIR, "Working folder for audio"),
                                 (self.move_pklz_dir_var, PKLZ_DIR, "Keep finished fingerprints in"),
                                 (self.keep_audio_dir_var, KEEP_AUDIO_DIR, "Keep downloaded audio in")):
            if not norm_path(var.get()):
                var.set(str(Path(old) / name))
                kept.append(label)
        self._log(f"[!] \"This program's folder\" ({old}) is no longer a setting: the program "
                  f"now works from its own folder, {SCRIPT_DIR}, and uses the audfprint there.",
                  tag="warning")
        if kept:
            self._log(f"    These stay in {old}, now set in Settings, Folders: {', '.join(kept)}.")

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
        # A folder left at its default is saved as "", so the defaults follow
        # the program if its folder is moved or copied to another computer.
        for key, default in (("output_dir", SCRIPT_DIR / DOWNLOADS_DIR),
                             ("move_pklz_dir", SCRIPT_DIR / PKLZ_DIR),
                             ("keep_audio_dir", SCRIPT_DIR / KEEP_AUDIO_DIR)):
            value = norm_path(out.get(key, ""))
            out[key] = "" if value and Path(value) == default else value
        # Persist the queue so it survives restarts.
        out["config_version"] = CONFIG_VERSION
        out["queue"] = list(self.queue_urls)
        out["queue_counts"] = {u: c for u, c in self.queue_counts.items() if u in self.queue_urls}
        console, share = self._console_height(), self._list_share()
        if console > 0 and share > 0:
            out["console_height"] = console
            out["list_share"] = share
        return out

    def _on_close(self) -> None:
        """Persist settings, then exit. Confirm first if a worker is running."""
        if self.worker_thread is not None and self.worker_thread.is_alive():
            confirm = messagebox.askyesno(
                "Quit while running?",
                "A job is still running.\n\n"
                "Quitting stops it. yt-dlp or ffmpeg may take a few seconds to exit.\n\n"
                "Quit anyway?",
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
        self._stop_counting()
        save_config(self._gather_config())
        if self._window_app_id:
            self._set_window_app_id(False)
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
                dlg = tk.Toplevel(self.root, bg=PALETTES[self._theme_now]["bg"])
                dlg.title(title)
                dlg.transient(self.root)
                dlg.resizable(False, False)
                dlg.bind("<Map>", self._on_map_title_bar, add="+")

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

    def _init_slots(self, n: int, texts: list[str] | None = None) -> None:
        """Rebuild the Now panel with n rows (UI thread). With no rows it says
        what will appear there."""
        for child in self.active_frame.winfo_children():
            child.destroy()
        self.slot_vars = []
        if n <= 0:
            tk.Label(
                self.active_frame, bg=self.QROW_BG, fg=self.QMUTED_FG, justify="left", anchor="w",
                text="Nothing running. Downloads and fingerprinting show here once "
                     "you start.",
                wraplength=360,
            ).pack(fill="x", padx=6, pady=6)
            return
        for i in range(n):
            var = tk.StringVar(value=(texts[i] if texts else f"{i + 1:>2}  idle"))
            tk.Label(self.active_frame, textvariable=var, bg=self.QROW_BG, fg=self.QFG,
                     anchor="w").pack(fill="x", padx=4)
            self.slot_vars.append(var)

    def _update_slot(self, idx: int, text: str) -> None:
        """Thread-safe slot label update."""
        def apply() -> None:
            if 0 <= idx < len(self.slot_vars):
                self.slot_vars[idx].set(f"{idx + 1:>2}  {text}")
        self.root.after(0, apply)

    def _show_rows(self, texts: list[str]) -> None:
        """UI thread: show these lines in the Now panel, one row each."""
        if len(texts) != len(self.slot_vars):
            self._init_slots(len(texts), texts)
            return
        for var, text in zip(self.slot_vars, texts):
            var.set(text)

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

        self._log(f"[+] Estimate: {n} item(s), {size_str}, {time_str} with {workers} downloads at once")
        if log_only:
            return True
        msg = (
            f"{n} item(s)\n"
            f"{size_str} estimated\n"
            f"{time_str} with {workers} downloads at once\n\n"
            f"Estimates are rough: real size and time depend on bitrate, length "
            f"and connection speed.\n\n"
            f"Start the download?"
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
                dlg.title(f"Select what to download ({len(entries)} found)")
                dlg.transient(self.root)
                dlg.geometry("760x560")
                dlg.minsize(560, 400)

                def on_close() -> None:
                    cleanup_bindings()
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
                        f"Untick anything you don't want. All {len(entries)} are selected."
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

                # The program-wide wheel handler (_on_mousewheel) scrolls this
                # list while the pointer is over it. It used to bind the wheel
                # itself and unbind_all on leaving, which also removed every
                # other wheel binding in the program.
                self._wheel_targets.append(canvas)

                def cleanup_bindings() -> None:
                    if canvas in self._wheel_targets:
                        self._wheel_targets.remove(canvas)

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
        """yt-dlp options from Settings, for every yt-dlp run that fetches from
        a site: the browser to sign in with, then Extra download options
        (parsed respecting quotes)."""
        args: list[str] = []
        browser = self.cookies_browser_var.get().strip()
        if browser and browser != COOKIE_BROWSERS[0]:
            args += ["--cookies-from-browser", browser.lower()]
        raw = self.extra_args_var.get().strip()
        if not raw:
            return args
        try:
            return args + shlex.split(raw, posix=False)
        except ValueError as e:
            self._log(f"[!] Could not parse 'Extra download options': {e}. Ignoring them.")
            return args

    # ---- what to leave out (Settings, Downloads) ----------------------------

    @staticmethod
    def _archive_key(entry: dict) -> str | None:
        """"<extractor> <id>", as yt-dlp's --download-archive writes it, or None
        when the listing did not say (such an item is never left out)."""
        extractor, item_id = entry.get("extractor"), entry.get("id")
        if not extractor or not item_id:
            return None
        return f"{str(extractor).lower()} {item_id}"

    @staticmethod
    def _load_done() -> set[str]:
        try:
            return {ln.strip() for ln in DONE_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()}
        except OSError:
            return set()

    def _filter_entries(self, entries: list[dict]) -> list[dict]:
        """Leave out items outside the length limits and, when asked, items
        fingerprinted in an earlier run. Items whose length the listing did not
        give are kept here; yt-dlp checks those itself (see _download_one)."""
        shortest = self._safe_int(self.min_seconds_var, 0, minimum=0)
        longest = self._safe_int(self.max_minutes_var, 0, minimum=0) * 60
        kept: list[dict] = []
        short = long_ = 0
        for entry in entries:
            length = entry.get("duration")
            if length and shortest and length < shortest:
                short += 1
            elif length and longest and length > longest:
                long_ += 1
            else:
                kept.append(entry)
        if short:
            self._log(f"[*] Leaving out {short} item(s) shorter than {shortest} seconds.")
        if long_:
            self._log(f"[*] Leaving out {long_} item(s) longer than {longest // 60} minutes.")
        if self.skip_done_var.get():
            done = self._load_done()
            before = len(kept)
            kept = [e for e in kept if self._archive_key(e) not in done]
            if before > len(kept):
                self._log(f"[*] Leaving out {before - len(kept)} item(s) already "
                          f"fingerprinted in an earlier run.")
        return kept

    def _remember_done(self, entries: list[dict]) -> None:
        """Worker thread, after a link has been fingerprinted: add its items to
        the done list. Only then, so a link that fails part-way leaves nothing
        marked as done that is not in a .pklz."""
        keys = [k for k in map(self._archive_key, entries) if k]
        new = [k for k in dict.fromkeys(keys) if k not in self._load_done()]
        if not new:
            return
        try:
            with open(DONE_FILE, "a", encoding="utf-8") as f:
                f.writelines(k + "\n" for k in new)
        except OSError as e:
            self._log(f"[!] Could not update {DONE_FILE.name}: {e}")
            return
        self._log(f"[+] Remembered {len(new)} item(s) as fingerprinted.")
        self.root.after(0, self._update_done_count)

    def _update_done_count(self) -> None:
        n = len(self._load_done())
        self.done_count_var.set(f"{n:,} remembered" if n else "none remembered yet")

    def _forget_done(self) -> None:
        n = len(self._load_done())
        if not n:
            return
        if not messagebox.askyesno(
            "Forget fingerprinted items?",
            f"Forget the {n:,} item(s) remembered as fingerprinted?\n\n"
            "The next run of each link then downloads and fingerprints everything "
            "again. The .pklz files you already have are not touched.",
            parent=self.settings_win,
        ):
            return
        try:
            DONE_FILE.unlink(missing_ok=True)
        except OSError as e:
            messagebox.showerror("Could not forget them", str(e), parent=self.settings_win)
        self._update_done_count()

    def _notify_done(self) -> None:
        """UI thread, when a run finishes: a sound, and the taskbar button
        flashes until the window is brought forward (Settings, Fingerprinting)."""
        if not self.notify_var.get():
            return
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:  # noqa: BLE001 - not Windows, or no sound device
            self.root.bell()
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                            ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                            ("dwTimeout", wintypes.DWORD)]

            # Tk's own window sits inside the frame Windows draws the taskbar
            # button for; that frame is its parent.
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            FLASHW_ALL, FLASHW_TIMERNOFG = 0x3, 0xC
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd,
                              FLASHW_ALL | FLASHW_TIMERNOFG, 0, 0)
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:  # noqa: BLE001 - a missed flash is not worth an error
            pass

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

    # Each link is a row of plain Tk widgets rather than ttk ones, so a whole
    # row can be coloured when it is selected: a drag handle, the tick box, its
    # number, the link itself, how many entries it has, and a remove button.
    # Dragging a row moves it. A click that does not move opens the link when
    # it lands on the link, and otherwise selects the row for Move up/down.

    QROW_BG = "#ffffff"
    QFG = "#000000"
    QFIELD = "#ffffff"
    QROW_SELECTED = "#cce8ff"
    QLINK_FG = "#0b57d0"
    QMUTED_FG = "#707070"
    QREMOVE_HOVER_FG = "#c0392b"
    # Two links are counted at a time, in the background.
    COUNT_WORKERS = 2

    def _refresh_queue(self, active_index: int | None = None) -> None:
        """Rebuild the rows from self.queue_urls / self.queue_checks."""
        self._drag = None
        for child in self.queue_rows_frame.winfo_children():
            child.destroy()
        self.queue_rows = []
        if active_index is not None:
            self.queue_active = active_index

        if not self.queue_urls:
            # The explanation the list used to carry in a permanent grey line
            # above it, shown where it is needed: while there is nothing here.
            tk.Label(
                self.queue_rows_frame, bg=self.QROW_BG, fg=self.QMUTED_FG, justify="left",
                anchor="w", wraplength=420,
                text="Your list is empty.\n\n"
                     "Paste a link to a channel, playlist or single page in the Link box "
                     "above and press Add. YouTube, Archive.org, Mixcloud, SoundCloud and "
                     "any other site yt-dlp supports work.\n\n"
                     "Links run from top to bottom. Drag a row by ≡ to move it, "
                     "click a link to open it, and right-click a row for more.",
            ).pack(fill="x", padx=8, pady=8)
        for url, var in zip(self.queue_urls, self.queue_checks):
            self.queue_rows.append(self._build_queue_row(url, var))
        self._paint_queue_rows()
        self._update_list_header()

    def _new_check(self) -> tk.BooleanVar:
        """A row's tick box, ticked, keeping the list's header count current."""
        var = tk.BooleanVar(value=True)
        var.trace_add("write", lambda *_a: self._update_list_header())
        return var

    def _update_list_header(self) -> None:
        n = len(self.queue_urls)
        ticked = sum(1 for v in self.queue_checks if v.get())
        if not n:
            title = "Your list"
        else:
            title = f"Your list · {n} link" + ("" if n == 1 else "s")
            if ticked != n:
                title += f", {ticked} ticked"
        self.list_title_var.set(title)
        self.tick_all_btn.configure(text="Untick all" if n and ticked == n else "Tick all")

    def _tick_all(self) -> None:
        """Tick every link, or untick them all when they already are."""
        value = not all(v.get() for v in self.queue_checks)
        for var in self.queue_checks:
            var.set(value)

    def _build_queue_row(self, url: str, var: tk.BooleanVar) -> dict:
        bg = self.QROW_BG
        frame = tk.Frame(self.queue_rows_frame, bg=bg)
        frame.pack(fill="x")
        handle = tk.Label(frame, text="\u2261", bg=bg, fg=self.QMUTED_FG,
                          cursor="fleur", padx=5)
        handle.pack(side="left")
        check = tk.Checkbutton(frame, variable=var, bg=bg, activebackground=bg,
                               fg=self.QFG, activeforeground=self.QFG, selectcolor=self.QFIELD,
                               highlightthickness=0, bd=0)
        check.pack(side="left")
        num = tk.Label(frame, bg=bg, fg=self.QFG, width=3, anchor="e")
        num.pack(side="left")
        # Packed from the right before the link, so a long link is cut short
        # rather than pushing them out of view.
        remove = tk.Label(frame, text="\u2715", bg=bg, fg=self.QMUTED_FG,
                          cursor="hand2", padx=6)
        remove.pack(side="right")
        count = tk.Label(frame, text=self._count_text(url), bg=bg, fg=self.QMUTED_FG)
        count.pack(side="right", padx=(8, 0))
        link = tk.Label(frame, text=url, bg=bg, fg=self.QLINK_FG, font=self._link_font,
                        cursor="hand2", anchor="w")
        link.pack(side="left", fill="x", expand=True, padx=(4, 0))

        row = {"url": url, "frame": frame, "handle": handle, "check": check,
               "num": num, "count": count, "link": link, "remove": remove}
        for widget, kind in ((frame, "row"), (handle, "row"), (num, "row"),
                             (count, "row"), (link, "link")):
            widget.bind("<ButtonPress-1>", lambda e, r=row, k=kind: self._queue_press(e, r, k))
            widget.bind("<B1-Motion>", self._queue_motion)
            widget.bind("<ButtonRelease-1>", self._queue_release)
        for widget in (frame, handle, check, num, count, link, remove):
            widget.bind("<Button-3>", lambda e, r=row: self._queue_menu(e, r))
        remove.bind("<Button-1>", lambda _e, r=row: self._queue_remove_row(r))
        remove.bind("<Enter>", lambda e: e.widget.config(
            fg=self.QREMOVE_HOVER_FG if self._queue_editable else self.QMUTED_FG))
        remove.bind("<Leave>", lambda e: e.widget.config(fg=self.QMUTED_FG))
        Tooltip(handle, "Drag to move this link up or down the list.")
        Tooltip(link, lambda: f"Open {url} in your browser.")
        Tooltip(count, lambda: self._count_tip(url))
        Tooltip(remove, "Remove from the list.")
        return row

    def _paint_queue_rows(self) -> None:
        """Number the rows and colour the selected one."""
        for i, row in enumerate(self.queue_rows):
            row["num"].config(text=f"{i + 1}.")
            bg = self.QROW_SELECTED if i == self.queue_active else self.QROW_BG
            for key in ("frame", "handle", "check", "num", "count", "link", "remove"):
                row[key].config(bg=bg)
            row["check"].config(activebackground=bg)

    def _queue_press(self, event: tk.Event, row: dict, kind: str) -> None:
        self._drag = {"row": row, "kind": kind, "y": event.y_root, "moved": False}

    def _queue_motion(self, event: tk.Event) -> None:
        drag = self._drag
        if drag is None or not self._queue_editable or drag["row"] not in self.queue_rows:
            return
        if not drag["moved"]:
            if abs(event.y_root - drag["y"]) < 5:
                return
            drag["moved"] = True
            self.queue_active = self.queue_rows.index(drag["row"])
            self._paint_queue_rows()
        # Scroll when dragged past either edge of the list.
        top = self.queue_canvas.winfo_rooty()
        if event.y_root < top + 6:
            self.queue_canvas.yview_scroll(-1, "units")
        elif event.y_root > top + self.queue_canvas.winfo_height() - 6:
            self.queue_canvas.yview_scroll(1, "units")
        # Move once the pointer passes the middle of a neighbouring row, so the
        # row does not jump back and forth while the pointer is still over it.
        y = event.y_root - self.queue_rows_frame.winfo_rooty()
        cur = target = self.queue_rows.index(drag["row"])

        def middle(i: int) -> float:
            f = self.queue_rows[i]["frame"]
            return f.winfo_y() + f.winfo_height() / 2

        while target + 1 < len(self.queue_rows) and y > middle(target + 1):
            target += 1
        if target == cur:
            while target > 0 and y < middle(target - 1):
                target -= 1
        if target != cur:
            self._queue_reorder(cur, target)

    def _queue_reorder(self, cur: int, target: int) -> None:
        """Move one link from position cur to target, keeping the selection."""
        selected = self.queue_urls[self.queue_active] if self.queue_active is not None else None
        for seq in (self.queue_urls, self.queue_checks, self.queue_rows):
            seq.insert(target, seq.pop(cur))
        self.queue_active = self.queue_urls.index(selected) if selected is not None else None
        for row in self.queue_rows:
            row["frame"].pack_forget()
        for row in self.queue_rows:
            row["frame"].pack(fill="x")
        self._paint_queue_rows()
        self.queue_rows_frame.update_idletasks()

    def _queue_release(self, _event: tk.Event) -> None:
        drag, self._drag = self._drag, None
        if drag is None or drag["row"] not in self.queue_rows:
            return
        if drag["moved"]:
            save_config(self._gather_config())
        elif drag["kind"] == "link":
            self._open_link(drag["row"]["url"])
        else:
            idx = self.queue_rows.index(drag["row"])
            self.queue_active = None if idx == self.queue_active else idx
            self._paint_queue_rows()

    def _queue_menu(self, event: tk.Event, row: dict) -> None:
        url = row["url"]
        editable = "normal" if self._queue_editable else "disabled"
        menu = tk.Menu(self.root, tearoff=0, **self._menu_colours())
        menu.add_command(label="Open link", command=lambda: self._open_link(url))
        menu.add_command(label="Copy link", command=lambda: self._copy_text(url))
        menu.add_command(label="Count again", command=lambda: self._request_counts([url], force=True))
        menu.add_separator()
        menu.add_command(label="Move to top", state=editable,
                         command=lambda: self._queue_move_row(row, 0))
        menu.add_command(label="Move to bottom", state=editable,
                         command=lambda: self._queue_move_row(row, len(self.queue_rows) - 1))
        menu.add_command(label="Remove from the list", state=editable,
                         command=lambda: self._queue_remove_row(row))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    @staticmethod
    def _open_link(url: str) -> None:
        webbrowser.open(url if "://" in url else f"https://{url}")

    def _copy_text(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _queue_remove_row(self, row: dict) -> None:
        if not self._queue_editable or row not in self.queue_rows:
            return
        i = self.queue_rows.index(row)
        del self.queue_urls[i]
        del self.queue_checks[i]
        if self.queue_active is not None:
            if self.queue_active == i:
                self.queue_active = None
            elif self.queue_active > i:
                self.queue_active -= 1
        self._refresh_queue()
        save_config(self._gather_config())

    # ---- how many entries each link has --------------------------------------
    #
    # yt-dlp cannot say how many videos a channel has without listing them (a
    # playlist it can, a channel tab it cannot), so each link is listed in the
    # background with --flat-playlist, which fetches the list pages only: about
    # 20 seconds for a channel of 500 videos. Counts are saved with the list
    # and refreshed once a day, and a run that lists a link updates its count.

    @staticmethod
    def _count_noun(url: str) -> str:
        return "video" if re.search(r"(^|[/.])(youtube\.com|youtu\.be)(/|$)", url) else "item"

    def _count_text(self, url: str) -> str:
        state = self._count_state.get(url)
        saved = self.queue_counts.get(url)
        if state is not None and state["status"] == "counting":
            return f"counting... {state['n']:,}" if state["n"] else "counting..."
        if not saved:
            # A failed recount keeps showing the last good count instead.
            return "could not count" if state is not None else ""
        noun = self._count_noun(url)
        if saved.get("single"):
            return f"single {noun}"
        n = saved["n"]
        return f"{n:,} {noun}" + ("" if n == 1 else "s")

    def _count_tip(self, url: str) -> str:
        state = self._count_state.get(url)
        saved = self.queue_counts.get(url)
        if state is not None and state["status"] == "counting":
            return "Counting in the background. A large channel takes a minute or so."
        if saved:
            return (f"How many {self._count_noun(url)}s this link has, counted on "
                    f"{saved.get('date', '?')}. Right-click to count again.")
        if state is not None:
            return "Could not count this link. Right-click to try again."
        return ""

    def _update_count_label(self, url: str) -> None:
        for row in self.queue_rows:
            if row["url"] == url:
                row["count"].config(text=self._count_text(url))

    def _request_counts(self, urls: list[str] | None = None, force: bool = False) -> None:
        """Queue links to be counted: all of them by default, skipping any
        counted today unless `force`."""
        today = time.strftime("%Y-%m-%d")
        for url in list(self.queue_urls) if urls is None else urls:
            saved = self.queue_counts.get(url)
            if url in self._count_pending or (not force and saved and saved.get("date") == today):
                continue
            self._count_pending.add(url)
            self._count_queue.put(url)
        while len(self._count_threads) < self.COUNT_WORKERS:
            t = threading.Thread(target=self._count_worker, daemon=True)
            self._count_threads.append(t)
            t.start()

    def _count_worker(self) -> None:
        while not self._closing:
            url = self._count_queue.get()
            try:
                if url in self.queue_urls and not self._closing:
                    self._count_one(url)
            except Exception as e:  # noqa: BLE001
                self._log(f"[!] Could not count {url}: {e!r}")
            finally:
                self._count_pending.discard(url)

    def _count_one(self, url: str) -> None:
        """Worker thread. List the link's entries and count them."""
        state = {"status": "counting", "n": 0}
        self._count_state[url] = state
        self.root.after(0, self._update_count_label, url)
        cmd = [*self.ytdlp, *YTDLP_UTF8, "--js-runtimes", "node", "--flat-playlist", "--no-warnings",
               "--print", "%(playlist_title)s", *self._extra_args(), url]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            state["status"] = "failed"
            self.root.after(0, self._update_count_label, url)
            return
        with self._count_lock:
            self._count_procs.append(proc)
        first = None
        last = 0.0
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if first is None:
                    first = line.strip()
                state["n"] += 1
                if time.monotonic() - last >= 0.5:
                    last = time.monotonic()
                    self.root.after(0, self._update_count_label, url)
            proc.wait()
        finally:
            with self._count_lock:
                if proc in self._count_procs:
                    self._count_procs.remove(proc)
        if self._closing:
            return
        if state["n"] == 0:
            state["status"] = "failed"
            self.root.after(0, self._update_count_label, url)
        else:
            # A single video has no playlist, which yt-dlp prints as NA.
            single = state["n"] == 1 and first in ("NA", "")
            self.root.after(0, self._record_count, url, state["n"], single)

    def _record_count(self, url: str, n: int, single: bool) -> None:
        """UI thread. Keep a finished count, from the counter or from a run."""
        self._count_state.pop(url, None)
        if url not in self.queue_urls:
            return
        self.queue_counts[url] = {"n": n, "single": single, "date": time.strftime("%Y-%m-%d")}
        self._update_count_label(url)
        save_config(self._gather_config())

    def _stop_counting(self) -> None:
        """On quit: end the counting yt-dlp processes too."""
        self._closing = True
        with self._count_lock:
            running = list(self._count_procs)
        for proc in running:
            self._kill_tree(proc)

    def _add_to_queue(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            return
        if not url.startswith(("http://", "https://", "www.")):
            messagebox.showerror(
                "Invalid link",
                "That doesn't look like a link.\n\n"
                "Paste a URL starting with http:// or https://.",
            )
            return
        if url in self.queue_urls:
            self._log(f"[!] Already in the list: {url}")
            return
        self.queue_urls.append(url)
        self.queue_checks.append(self._new_check())  # ticked by default
        self._refresh_queue(active_index=len(self.queue_urls) - 1)
        self._request_counts([url])
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
            title="Import links from a file",
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
                self.queue_checks.append(self._new_check())
                added += 1

        if added:
            self._refresh_queue(active_index=len(self.queue_urls) - 1)
            save_config(self._gather_config())
            self._request_counts([u for u in self.queue_urls if u in seen_this_import])

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

    def _queue_move_row(self, row: dict, target: int) -> None:
        """Right-click's Move to top / bottom: dragging a row across a long
        list is slow, so the ends are one click away."""
        if not self._queue_editable or row not in self.queue_rows:
            return
        cur = self.queue_rows.index(row)
        target = max(0, min(target, len(self.queue_rows) - 1))
        if target != cur:
            self.queue_active = cur
            self._queue_reorder(cur, target)
            save_config(self._gather_config())

    def _queue_clear(self) -> None:
        if not self.queue_urls:
            return
        if not messagebox.askyesno("Clear the list", "Remove every link from the list?"):
            return
        self.queue_urls.clear()
        self.queue_checks.clear()
        self.queue_active = None
        self._refresh_queue()
        save_config(self._gather_config())

    def _set_queue_controls_enabled(self, enabled: bool) -> None:
        # Rows cannot be dragged or removed during a run either. The run works
        # from a copy of the list taken at Start, so edits would not reach it.
        self._queue_editable = enabled
        state = "normal" if enabled else "disabled"
        for btn in (
            self.queue_import_btn, self.queue_remove_btn,
            self.tick_all_btn, self.queue_clear_btn,
        ):
            btn.config(state=state)
        self._update_add_btn()

    def _update_add_btn(self) -> None:
        """Add is clickable when the link box holds something and the list
        can be changed (not during a run)."""
        ready = bool(self.url_var.get().strip()) and getattr(self, "_queue_editable", True)
        self.add_queue_btn.config(state="normal" if ready else "disabled")

    # ------------------------- pipeline control -------------------------------

    def _start(self, resume: dict | None = None) -> None:
        """Download and fingerprint the ticked links, or with `resume` the links
        an unfinished list had left. Pressed while there is such a list, it
        asks first whether to continue that instead: a new run would replace
        its record."""
        if resume is None:
            # Convenience: if the URL field has something, add it to the queue first.
            if self.url_var.get().strip():
                self._add_to_queue()
            if self.offer_resume_var.get():
                state = self._unfinished_list()
                if state is not None and self._ask_to_continue(state):
                    resume = state
        if resume is not None:
            checked_urls = list(resume["remaining"])
        else:
            if not self.queue_urls:
                messagebox.showerror(
                    "The list is empty",
                    "Add at least one link to the list first.",
                )
                return

            # Only checked entries run.
            checked_urls = [
                u for u, v in zip(self.queue_urls, self.queue_checks) if v.get()
            ]
            if not checked_urls:
                messagebox.showerror(
                    "Nothing ticked",
                    "Tick at least one link in the list.",
                )
                return

        self._fill_default_folders()
        output_dir = self.output_dir_var.get()
        bat_dir = self.bat_dir_var.get()
        if not output_dir or not Path(output_dir).is_dir():
            messagebox.showerror("Missing input", "Pick a valid working folder for audio.")
            return
        if not self._audfprint_ready(bat_dir) or self._folders_clash(bat_dir):
            return

        self.cancel_flag.clear()
        self.skip_flag.clear()
        self.start_btn.config(state="disabled")
        self.disk_btn.config(state="disabled")
        self.test_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.pause_btn.config(text="Pause", state="normal")
        self.skip_btn.config(state="normal")
        self._set_inputs_locked(True)
        self._set_queue_controls_enabled(False)

        save_config(self._gather_config())
        self._keep_awake(True)
        # Any unfinished-list record from here on is this run's, which Start
        # itself asks about next time; the start-up offer is for a record an
        # earlier session left, so it must not pick this one up.
        self._resume_asked = True

        # Snapshot the checked URLs so edits mid-run don't matter.
        self._job_kind = "list"
        self.worker_thread = threading.Thread(
            target=self._run_queue,
            args=(checked_urls, Path(output_dir), Path(bat_dir), resume),
            daemon=True,
        )
        self.worker_thread.start()

    def _audfprint_ready(self, bat_dir: str) -> bool:
        """True if <bat_dir>\\audfprint is WerZatSong's audfprint; otherwise say
        what is wrong and point at Check setup. A file check only, so it is fast
        enough for the button click; Check setup does the thorough version."""
        problem = dependencies.audfprint_problem(bat_dir)
        if problem is None:
            return True
        messagebox.showerror(
            "WerZatSong's audfprint is needed",
            f"audfprint: {problem}.\n\n"
            f"The Fingerprinter needs WerZatSong's version of audfprint "
            f"({dependencies.WERZATSONG_AUDFPRINT_URL}). Upstream dpwe/audfprint "
            f"does not work here.\n\n"
            f"Press Check setup to install it.",
            parent=self.root,
        )
        return False

    def _run_queue(self, urls: list[str], output_base: Path, bat_dir: Path,
                   resume: dict | None = None) -> None:
        """Process each queued URL sequentially through the full pipeline.
        Skip-on-failure: a failed/skipped channel doesn't halt the batch.

        Its progress is kept in LIST_STATE_FILE as it goes: each link is noted
        as it starts and marked when it ends. A list that completes deletes the
        file; one that stops part-way (Stop, a crash, the PC shutting down)
        leaves it, and the next start offers to continue (_offer_resume). With
        `resume`, this is that continuation: `urls` are the links left, and the
        one it stopped in starts over from scratch."""
        results: list[tuple[str, str]] = []  # (url, status)
        state = resume or {"links": list(urls), "finished": {}}
        state = {"links": list(state["links"]), "finished": dict(state.get("finished", {})),
                 "current": state.get("current")}
        restart = state["current"] if resume else None
        # Whether the link it stopped in had begun fingerprinting: only then is
        # what work\pklz holds that link's own (_note_list_fingerprinting).
        discard = bool(resume and resume.get("fingerprinting"))
        self._list_state = state
        try:
            total = len(urls)
            if resume:
                self._log(f"[*] Continuing the unfinished list: {total} of "
                          f"{len(state['links'])} link(s) left.")
            for i, url in enumerate(urls, start=1):
                if self.cancel_flag.is_set():
                    # Mark the rest as not-run.
                    for u in urls[i - 1:]:
                        results.append((u, "cancelled"))
                    break

                self.skip_flag.clear()
                self._log("=" * 60)
                self._log(f"[*] LINK {i}/{total}: {url}")
                self._log("=" * 60)
                self._set_status(f"Link {i}/{total}: starting...")
                self._set_now_title(f"Now · link {i} of {total}")

                state["current"] = url
                if url == restart and discard:
                    # Its unfinished .pklz files are still in work\pklz until
                    # _prepare_work discards them, so the note stays: stopped
                    # again before then, the next continuation discards them too.
                    state["fingerprinting"] = True
                else:
                    state.pop("fingerprinting", None)
                self._save_list_state(state)
                try:
                    status = self._run_pipeline(
                        url, output_base, bat_dir, queue_mode=True,
                        queue_position=(i, total), restarting=(url == restart),
                        discard_work=(url == restart and discard),
                    )
                except Exception as e:  # noqa: BLE001
                    self._log(f"[X] Link failed with error: {e!r}")
                    status = "failed"
                results.append((url, status or "done"))
                # A link Stop cut short stays "current": continuing starts it
                # over. That includes one that failed because Stop killed its
                # work. A link the user declined (a question answered No) counts
                # as finished, like a skipped one.
                if not (self.cancel_flag.is_set() and status in ("cancelled", "failed")):
                    state["finished"][url] = status or "done"
                    state["current"] = None
                    state.pop("fingerprinting", None)
                    self._save_list_state(state)

            # Only a list that ran to its end has nothing left to continue.
            if not self.cancel_flag.is_set():
                self._clear_list_state()

            # Final summary.
            self._log("=" * 60)
            self._log("[+] LIST COMPLETE")
            done = sum(1 for _, s in results if s == "done")
            self._log(f"    {done}/{total} link(s) completed.")
            for u, s in results:
                if s != "done":
                    tag = "warning" if s in ("failed", "skipped") else None
                    self._log(f"    [{s}] {u}", tag=tag)
            self._log("=" * 60)
            self._set_status(f"List done: {done}/{total} completed.")
            self.root.after(0, self._notify_done)

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
            self._log(f"[X] List error: {e!r}")
            self._set_status("List error.")
        finally:
            self._list_state = None
            self.root.after(0, self._finish)

    def _start_bats_only(self, split: bool = True) -> None:
        """Fingerprint audio already on disk, skipping downloads. `split` picks
        the variant: the two buttons that reach here differ only in this flag,
        and each says in its own label which it is, so neither depends on the
        Advanced setting."""
        self._fill_default_folders()
        bat_dir = self.bat_dir_var.get()
        if not self._audfprint_ready(bat_dir) or self._folders_clash(bat_dir):
            return

        source_dir = self.output_dir_var.get()
        if not source_dir or not Path(source_dir).is_dir():
            messagebox.showerror("Missing input", "Pick a valid working folder to scan for audio.")
            return

        # Says that files get rewritten, because they do: splitting replaces a
        # long recording with its pieces and deletes the original. Agreeing to
        # "fingerprint what is on disk" should not quietly also mean "and
        # restructure it".
        splitting = split
        split_line = (
            f"Files over {SPLIT_TRIGGER // 60}:00 are first SPLIT IN PLACE into "
            f"{SPLIT_SEGMENT // 60}:00 pieces (the last piece takes the remainder), and "
            f"the originals deleted.\n\n"
            if splitting else
            "Files are fingerprinted as they are, without checking their length. Use "
            "this for audio that is already split; anything over "
            f"{SPLIT_TRIGGER // 60}:00 goes into the database whole.\n\n"
        )
        if not messagebox.askyesno(
            "Split and fingerprint existing audio?" if splitting
            else "Fingerprint existing audio?",
            f"Scan for audio under:\n{source_dir}\n\n"
            f"{split_line}"
            f"Then fingerprint it, and keep the results in:\n{self._keep_dir(Path(bat_dir))}\n\n"
            f"Nothing is downloaded.\n\nContinue?",
            parent=self.root,
        ):
            return

        self.cancel_flag.clear()
        self.start_btn.config(state="disabled")
        self.disk_btn.config(state="disabled")
        self.test_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.pause_btn.config(text="Pause", state="normal")
        self._set_inputs_locked(True)

        save_config(self._gather_config())
        self._keep_awake(True)

        self._job_kind = "disk"
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
        self.disk_btn.config(state="disabled")
        self.test_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self._set_inputs_locked(True)

        self._job_kind = "setup"
        self.worker_thread = threading.Thread(
            target=self._run_test_connection,
            daemon=True,
        )
        self.worker_thread.start()

    def _run_test_connection(self) -> None:
        statuses: list[dependencies.Status] = []
        bat_dir = self.bat_dir_var.get().strip() or str(dependencies.APP_DIR)
        try:
            self._set_status("Checking setup...")
            self._set_now_title("Now · checking setup")
            self._set_progress("Checking setup")
            self._log("=" * 60)
            self._log(f"[*] Setup check, Fingerprinter {__version__}")
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

            # 2. Every component, run for real rather than just found on PATH
            #    (dependencies.py): packages, yt-dlp, ffmpeg/ffprobe, Node.js and
            #    WerZatSong's audfprint.
            self._log("[*] Components:")
            statuses = dependencies.check_all(bat_dir)
            self._log_statuses(statuses)
            self._refresh_ytdlp()
            ytdlp_ok = any(s.key == "yt-dlp" and s.ok for s in statuses)

            if self.cancel_flag.is_set():
                return

            # 3. yt-dlp to its newest release, as setup.bat does. (`yt-dlp -U`,
            #    which this used to run, only updates the standalone exe; for
            #    the copy pip installed it just says to use pip.)
            if ytdlp_ok:
                dependencies.update_ytdlp(lambda line: self._log("[*] " + line if not line.startswith(" ") else line))
                self._refresh_ytdlp()

            if self.cancel_flag.is_set():
                return

            # 4. Test info retrieval against a known stable video.
            test_url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
            if not ytdlp_ok:
                self._log("[*] Skipping the test download: yt-dlp is not working.")
            else:
                self._run_test_fetch(test_url)

            if self.cancel_flag.is_set():
                return

            # 5. Output dir checks
            self._check_folders()

            self._log("=" * 60)
            self._log("[+] Setup check complete.")
            self._log("=" * 60)
            self._set_status("Setup check complete.")

            # 6. Offer to install or repair anything that is not working.
            if not self.cancel_flag.is_set():
                self._offer_install(statuses, bat_dir)

        except Exception as e:  # noqa: BLE001
            self._log(f"[X] Setup check error: {e!r}")
            self._set_status("Setup check error.")
        finally:
            self.root.after(0, self._finish)

    def _run_test_fetch(self, test_url: str) -> None:
        """Fetch one known video's metadata, to prove yt-dlp can reach YouTube."""
        self._log(f"[*] Fetching info for: {test_url}")
        self._log("    (YouTube's first upload, a stable test target.)")
        try:
            proc = subprocess.run(
                [*self.ytdlp, *YTDLP_UTF8, "--js-runtimes", "node",
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

    def _check_folders(self) -> None:
        """Working folder: space and write access. Program folder: its working
        subfolders. Config: where it is and whether it can be saved."""
        out_dir = self.output_dir_var.get().strip()
        self._log(f"[*] Working folder: {out_dir or '(not set)'}")
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

        # audfprint itself is covered by the component check above.
        bat_dir = self.bat_dir_var.get().strip()
        self._log(f"[*] Program folder: {bat_dir or '(not set)'}")
        if bat_dir:
            p = Path(bat_dir)
            if p.is_dir():
                for sub in (WORK_TEXTS, WORK_PKLZ):
                    d = p / sub
                    if d.is_dir():
                        self._log(f"    + {sub}: {len(list(d.iterdir()))} item(s)")
                    else:
                        self._log(f"    + {sub}: not present (created on use)")
                keep = self._keep_dir(p)
                count = len(list(keep.glob("*.pklz"))) if keep.is_dir() else 0
                self._log(f"[*] Finished fingerprints: {keep} ({count} .pklz file(s))")
            else:
                self._log("    ! Path does not exist or is not a directory.", tag="warning")

        self._log(f"[*] Config: {CONFIG_FILE}")
        self._log(
            f"    exists: {CONFIG_FILE.is_file()}, "
            f"writable: {os.access(CONFIG_FILE.parent, os.W_OK)}"
        )

    # ------------------------- dependencies -----------------------------------

    def _log_statuses(self, statuses: list[dependencies.Status]) -> None:
        for s in statuses:
            self._log("    " + dependencies.describe(s), tag=None if s.ok else "warning")

    def _refresh_ytdlp(self) -> None:
        """Re-resolve how to run yt-dlp (after a check or an install)."""
        self.ytdlp = dependencies.ytdlp_command() or ["yt-dlp"]

    def _startup_check(self, bat_dir: str) -> None:
        """Worker thread, at launch: check the components (offering to install
        whatever is missing), then offer to continue an unfinished list."""
        try:
            self._check_components_at_start(bat_dir)
            # The program folder's shortcut with the fingerprint icon, for a
            # copy that was unzipped without running setup.bat again, and made
            # again when it no longer fits: the folder was moved or copied, or
            # the Python it started is gone.
            existed = dependencies.SHORTCUT_FILE.exists()
            if not dependencies.shortcut_is_current() and dependencies.make_shortcut(lambda _m: None):
                name = dependencies.SHORTCUT_FILE.name
                self._log(f"[+] Updated {name} for this folder." if existed else
                          f"[+] Made {name} in the program folder: start the Fingerprinter from it.")
        finally:
            self.root.after(500, self._offer_resume)

    def _check_components_at_start(self, bat_dir: str) -> None:
        """Check every component and offer to install whatever is missing.
        Silent apart from one line when all is well."""
        try:
            statuses = dependencies.check_all(bat_dir)
            self._refresh_ytdlp()
        except Exception as e:  # noqa: BLE001
            self._log(f"[!] Could not check the setup: {e!r}")
            return
        # Now that yt-dlp's whereabouts are known, bring the list's counts up to date.
        if any(s.key == "yt-dlp" and s.ok for s in statuses):
            self.root.after(0, self._request_counts)
        problems = [s for s in statuses if not s.ok]
        if not problems:
            self._log("[+] Setup OK: every component is installed and working.")
            return
        self._log("[!] Some components are missing or not working:", tag="warning")
        self._log_statuses(problems)
        if self.worker_thread is not None and self.worker_thread.is_alive():
            self._log("[!] Press Check setup once the current job has finished to install them.")
            return
        # Treated like any other job while it runs: buttons off, Stop and quit
        # handled the usual way.
        self._job_kind = "setup"
        self.worker_thread = threading.current_thread()
        self.root.after(0, self._lock_for_job)
        try:
            self._offer_install(statuses, bat_dir)
        finally:
            self.root.after(0, self._finish)

    # ---- continuing an unfinished list ------------------------------------------

    @staticmethod
    def _save_list_state(state: dict, stamp: bool = True) -> None:
        """Written whole to a temporary name and then swapped in, so a crash
        mid-write cannot leave half a file. `stamp` records the time as when
        the list was last running."""
        data = dict(state, updated=time.strftime("%Y-%m-%d %H:%M")) if stamp else state
        tmp = LIST_STATE_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, LIST_STATE_FILE)
        except OSError:
            pass

    @staticmethod
    def _load_list_state() -> dict | None:
        try:
            data = json.loads(LIST_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("links"), list):
            return None
        data["finished"] = data.get("finished") if isinstance(data.get("finished"), dict) else {}
        return data

    @staticmethod
    def _clear_list_state() -> None:
        try:
            LIST_STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    def _note_list_fingerprinting(self, bat_dir: Path) -> None:
        """Worker thread: the list's current link begins fingerprinting, into
        a work\\pklz that _prepare_work has just emptied. Noted in the record,
        so that if the list stops now, what work\\pklz holds is known to be
        this link's own and a continuation deletes it (_prepare_work, discard).
        Not noted if something could not be moved out: that is not its own."""
        state = self._list_state
        if state is None or any((bat_dir / WORK_PKLZ).glob("*.pklz")):
            return
        state["fingerprinting"] = True
        self._save_list_state(state)

    def _note_list_link_done(self, url: str) -> None:
        """Worker thread: the list's current link is fingerprinted and its
        .pklz files are about to be moved out. Recorded as finished now, not
        when the link returns, so that a list stopped during the move (Quit,
        a crash, Windows restarting) is not continued by making the link
        again next to the fingerprints already moved. What is left in
        work\\pklz then goes out as recovered-* at the next link."""
        state = self._list_state
        if state is None or state.get("current") != url:
            return
        state["finished"][url] = "done"
        state["current"] = None
        state.pop("fingerprinting", None)
        self._save_list_state(state)

    def _forget_list_fingerprinting(self) -> None:
        """Another job is about to use work\\pklz, so what it holds may no longer
        be only the stopped link's: a continuation then moves it out as
        recovered-* like any other leftover, rather than deleting it."""
        state = self._load_list_state()
        if state is not None and state.pop("fingerprinting", None):
            self._save_list_state(state, stamp=False)

    def _unfinished_list(self) -> dict | None:
        """The record of a list that stopped part-way, with the links it has
        left under "remaining": those still in the list, the one it stopped in
        first (it starts over), then the rest in the list's order. None if
        there is nothing to continue, when the record is deleted."""
        state = self._load_list_state()
        if state is None:
            return None
        links = [u for u in state["links"] if isinstance(u, str)]
        finished = state["finished"]
        remaining = [u for u in links if u not in finished and u in self.queue_urls]
        if not remaining:
            self._clear_list_state()
            return None
        remaining.sort(key=lambda u: u != state.get("current"))
        return dict(state, links=links, remaining=remaining)

    def _ask_to_continue(self, state: dict) -> bool:
        """Ask whether to continue the unfinished list in `state`. No deletes
        the record: then it is not asked about again."""
        self._resume_asked = True
        links, remaining = state["links"], state["remaining"]
        done = sum(1 for u in links if u in state["finished"])
        stopped_in = state.get("current") or remaining[0]
        when = f" on {state['updated']}" if state.get("updated") else ""
        if messagebox.askyesno(
            "Continue the unfinished list?",
            f"The last run of your list stopped part-way{when}. {done} of {len(links)} "
            f"link(s) had finished, and it stopped in:\n{stopped_in}\n\n"
            f"Continue with the {len(remaining)} link(s) left now? The one it stopped "
            f"in starts over.\n\nNo forgets where it stopped; your list stays as it is.",
            parent=self.root,
        ):
            return True
        self._clear_list_state()
        return False

    def _offer_resume(self, attempt: int = 0) -> None:
        """UI thread, at start-up: if the last list stopped part-way, offer to
        carry on with the links it had left (Settings, General). Start asks the
        same when pressed while there is one, so this steps aside if that has
        already happened."""
        if self._resume_asked or not self.offer_resume_var.get():
            return
        if self.worker_thread is not None and self.worker_thread.is_alive():
            # The start-up install offer may still be running; ask after it.
            if attempt < 60:
                self.root.after(1000, self._offer_resume, attempt + 1)
            return
        state = self._unfinished_list()
        if state is not None and self._ask_to_continue(state):
            self._start(resume=state)

    def _lock_for_job(self) -> None:
        self.start_btn.config(state="disabled")
        self.disk_btn.config(state="disabled")
        self.test_btn.config(state="disabled")
        self._set_inputs_locked(True)

    def _offer_install(self, statuses: list[dependencies.Status], bat_dir: str) -> None:
        """Worker thread. List what is wrong, ask, and install on a yes.
        Only things that are missing or not working are offered."""
        problems = [s for s in statuses if not s.ok]
        if not problems:
            self._log("[+] Everything the Fingerprinter needs is installed and working.")
            return
        for s in problems:
            if not s.fix:
                self._log(f"[!] {s.name} has to be fixed by hand: {s.manual}", tag="warning")
        fixable = [s for s in problems if s.fix]
        if not fixable:
            return
        items = "\n".join(f"• {s.name} ({s.why}):\n   {s.fix}" for s in fixable)
        if not self._ask_yes_no(
            "Install missing components?",
            f"These are missing or not working:\n\n{items}\n\n"
            f"Install them now? Progress is shown in the console.",
        ):
            self._log("[!] Nothing was installed. Press Check setup to do it later.")
            return
        self._set_status("Installing...")
        self._log("=" * 60)
        self._log("[*] Installing")
        self._log("=" * 60)
        after = dependencies.install(statuses, self._log, bat_dir)
        self._refresh_ytdlp()
        self._log("[*] Components now:")
        self._log_statuses(after)
        left = [s for s in after if not s.ok]
        if left:
            self._log(f"[!] {len(left)} component(s) still need attention; see above.", tag="warning")
            self._set_status("Some components still need attention.")
        else:
            self._log("[+] Everything the Fingerprinter needs is installed and working.")
            self._set_status("Setup complete.")

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
        # Started in the moment between a worker passing its pause check and
        # Pause being pressed: suspend it with the rest.
        if self.pause_flag.is_set():
            self._apply_pause_state()

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
        # Settings, General: Ask before Stop. The job carries on meanwhile.
        # What survives depends on the job, so the question says so.
        detail = {
            "list": "\n\nLinks that have finished keep their fingerprints, and the list can "
                    "be continued later. The link running now is cut short: when it runs "
                    "again, it is downloaded and fingerprinted from the start.",
            "disk": "\n\nBatches that have finished are kept, and fingerprinting the audio on "
                    "disk again carries on from them while the program stays open (after a "
                    f"restart too, with Write {self.FINGERPRINTED_RECORD} on).",
        }.get(self._job_kind, "")
        if self.confirm_stop_var.get() and not messagebox.askyesno(
            "Stop the job?", "Stop everything that is running now?" + detail,
            icon="warning", default="no", parent=self.root,
        ):
            return
        if self.worker_thread is None or not self.worker_thread.is_alive():
            return      # the job finished while the question was open
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
        # Stop wins over Pause: waiting workers see cancel_flag and return, and
        # suspended processes are killed like running ones.
        self.pause_flag.clear()
        self.pause_btn.config(text="Pause", state="disabled")

        def kill_then_forget_suspended() -> None:
            self._kill_all_children()
            self._apply_pause_state()

        # On a worker thread, not here. This is a Tk button callback, and
        # taskkill is synchronous: walking every child on the UI thread froze
        # the window mid-Stop, which looks exactly like the hang that Stop is
        # supposed to end.
        threading.Thread(target=kill_then_forget_suspended, daemon=True).start()

    # ------------------------- pause ------------------------------------------
    #
    # Two halves. Work already running (yt-dlp, ffmpeg, audfprint and their
    # children) is suspended by the operating system, so it stops using CPU and
    # disk and continues from the same point on Resume. Work not yet started is
    # held by _wait_if_paused at the points where workers would start it.

    def _toggle_pause(self) -> None:
        if self.pause_flag.is_set():
            self.pause_flag.clear()
            self.pause_btn.config(text="Pause")
            self._log("[*] Resumed.")
            self._set_status("Resumed.")
        else:
            if self.cancel_flag.is_set():
                return
            self.pause_flag.set()
            self.pause_btn.config(text="Resume")
            if psutil is None:
                self._log("[!] Paused: nothing new will start, but work already running "
                          "finishes first (psutil is missing; Check setup installs it).")
            else:
                self._log("[!] Paused. Press Resume to continue from the same point.")
            self._set_status("Paused.")
        # psutil calls are quick, but keep the UI thread free of process work.
        # Pausing starts the watcher (which applies the state straight away);
        # resuming applies it once.
        target = self._hold_while_paused if self.pause_flag.is_set() else self._apply_pause_state
        threading.Thread(target=target, daemon=True).start()

    def _apply_pause_state(self) -> None:
        """Make the processes match pause_flag: suspend every tracked process
        tree while it is set, resume everything this suspended once it is not.

        Reads the flag under the lock, so presses in quick succession cannot
        leave the processes out of step with the button. Windows counts
        suspensions, so each process is suspended at most once and resumed
        exactly as often."""
        if psutil is None:
            return
        with self._pause_lock:
            if self.pause_flag.is_set() and not self.cancel_flag.is_set():
                with self._fp_procs_lock:
                    running = list(self._fp_procs)
                for proc in running:
                    try:
                        parent = psutil.Process(proc.pid)
                        # The parent first, so it cannot start a child after
                        # its children were listed.
                        self._suspend_one(parent)
                        for child in parent.children(recursive=True):
                            self._suspend_one(child)
                    except psutil.Error:
                        continue
            else:
                for p, times, _cpu in self._suspended.values():
                    for _ in range(times):
                        try:
                            p.resume()
                        except psutil.Error:
                            break       # exited or killed while suspended
                self._suspended.clear()

    @staticmethod
    def _cpu_seconds(p: "psutil.Process") -> float:
        try:
            t = p.cpu_times()
            return t.user + t.system
        except psutil.Error:
            return 0.0

    def _suspend_one(self, p: "psutil.Process") -> None:
        """Suspend p the first time it is seen. After that, only suspend it
        again if it has used CPU since: a thread being created at the moment
        of suspension escapes it (ffmpeg starting its workers, say), and CPU
        use is the one sure sign of that. Windows counts suspensions, so each
        is recorded and Resume undoes exactly as many."""
        entry = self._suspended.get(p.pid)
        if entry is not None and self._cpu_seconds(p) <= entry[2] + 0.02:
            return
        try:
            p.suspend()
        except psutil.Error:
            return
        if entry is None:
            self._suspended[p.pid] = [p, 1, self._cpu_seconds(p)]
        else:
            entry[1] += 1
            entry[2] = self._cpu_seconds(p)

    def _hold_while_paused(self) -> None:
        """While paused, look again every half second: for a child a process
        was starting just as it was suspended, and for an escaped thread."""
        while self.pause_flag.is_set() and not self.cancel_flag.is_set():
            self._apply_pause_state()
            time.sleep(0.5)

    def _wait_if_paused(self) -> None:
        """Hold a worker while paused. Returns at once otherwise, and as soon
        as Stop is pressed, so callers check cancel_flag straight after."""
        while self.pause_flag.is_set() and not self.cancel_flag.is_set():
            time.sleep(0.2)

    def _skip_current(self) -> None:
        self.skip_flag.set()
        self._log("[!] Skip requested. Moving to the next link at the next safe point"
                  + (", once you press Resume." if self.pause_flag.is_set() else "."))

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
        # Read for every line a running job prints, so it waits for the next job.
        self.verbose_check.config(state="disabled" if locked else "normal")
        # Read as each link is listed, downloaded and recorded.
        self.cookies_combo.config(state="disabled" if locked else "readonly")
        for widget in (self.min_seconds_spin, self.max_minutes_spin,
                       self.skip_done_check, self.forget_done_btn):
            widget.config(state="disabled" if locked else "normal")
        # Read as each link's downloads finish.
        self.keep_audio_check.config(state="disabled" if locked else "normal")
        if locked:
            self.keep_audio_entry.config(state="disabled")
            self.keep_audio_browse.config(state="disabled")
        else:
            self._update_keep_audio_row()

    def _finish(self) -> None:
        self._keep_awake(False)
        self.pause_flag.clear()
        self._reset_progress()
        self.pause_btn.config(text="Pause", state="disabled")
        self.start_btn.config(state="normal")
        self.disk_btn.config(state="normal")
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
        restarting: bool = False,
        discard_work: bool = False,
    ) -> str | None:
        """Run the full pipeline for one channel. `restarting` is the link an
        unfinished list stopped in: its download folder is cleared, not asked
        about. `discard_work` when it had begun fingerprinting, so the .pklz
        files in work\\pklz are its own unfinished ones: those are deleted.

        Returns a status string ("done", "failed", "skipped", "cancelled") when
        called in queue mode; the queue runner uses it for the summary. When not
        in queue mode it calls _finish itself and the return value is ignored.

        In queue mode, interactive dialogs (subset selection, size estimate) are
        auto-confirmed so the batch can run unattended; the prompt about a
        link's leftover download folder still appears but auto-resolves via its
        2-minute timer."""
        qp = f"[{queue_position[0]}/{queue_position[1]}] " if queue_position else ""

        def stopped() -> str | None:
            """Return a status if we should bail, else None."""
            if self.cancel_flag.is_set():
                return "cancelled"
            if self.skip_flag.is_set():
                self._log(f"[!] {qp}Skipping this link.", tag="warning")
                return "skipped"
            return None

        try:
            if not check_dependency(self.ytdlp):
                self._log("[X] yt-dlp not found. Press Check setup to install it.")
                return "failed"
            if not check_dependency("ffmpeg"):
                self._log("[X] ffmpeg not found (needed to remux and split audio). "
                          "Press Check setup to install it.")
                return "failed"

            # 1. Extract video list
            self._set_status(f"{qp}Extracting playlist info...")
            self._log(f"[*] Fetching info for: {url}")
            info = self._extract_info(url)
            if info is None:
                # Stop kills the listing, which then has nothing to return.
                return "cancelled" if self.cancel_flag.is_set() else "failed"
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
            # A single video lists as one entry with no playlist title.
            single = not entries or (len(entries) == 1 and not info.get("title"))
            if not entries:
                entries = [info]  # single video fallback
            # The listing just done is a fresh count for the list.
            self.root.after(0, self._record_count, url, len(entries), single)

            self._log(f"[+] Source: {channel_name_raw}  ({len(entries)} item(s))")
            if (s := stopped()):
                return s
            # Settings, Downloads: items outside the length limits, and items
            # fingerprinted in an earlier run, are left out here.
            entries = self._filter_entries(entries)
            if not entries:
                self._log(f"[+] {qp}Nothing new to download for this link.")
                self._set_status(f"{qp}Nothing new.")
                return "done"

            # Resolve this channel's download subfolder now (needed for the
            # pre-download cleanup check below).
            target_folder_name = f"{channel_name}_subfolder"
            initial_folder = output_base / target_folder_name

            # 1.5 Pre-download cleanup: if this channel's subfolder already has
            #     content (e.g. a leftover partial run), ask to clear it. This
            #     runs in queue mode too — it only touches THIS channel's
            #     subfolder, never other channels' folders.
            if initial_folder.is_dir():
                if restarting:
                    # Left by the run that stopped in this link; it starts over.
                    self._log("[*] Clearing what the stopped run left in this link's download folder.")
                    self._clear_download_folder(initial_folder)
                elif not self._preflight_clean(
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
                    self._log("[X] Nothing selected. Aborting.")
                    self._set_status("Aborted.")
                    return "failed"
                if len(selected) != len(entries):
                    self._log(
                        f"[+] Selection: {len(selected)} of {len(entries)} item(s) "
                        f"({len(entries) - len(selected)} skipped)."
                    )
                entries = selected

            # 1b. Size/time estimate — interactive only outside queue mode.
            # Clamped here as well: a Spinbox's range does not stop typed values.
            workers = min(self.MAX_PARALLEL,
                          self._safe_int(self.parallel_var, self.DEFAULT_PARALLEL))
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
            self._set_status(f"{qp}Downloading 0/{len(entries)} ({workers} at once)...")
            ok, fail = self._download_parallel(entries, initial_folder, workers)
            self._log(f"[+] Downloads finished: {ok} OK, {fail} failed.")

            if (s := stopped()):
                return s
            if ok == 0:
                self._log("[X] No successful downloads. Aborting before fingerprinting.")
                return "failed"

            # 4. Folder stays where it was downloaded; the scan below picks it up there.
            self._log(f"[+] Folder stays at: {initial_folder}")
            # Settings, Folders: keep a copy of the audio as it was downloaded,
            # before splitting rewrites it and the link's folder is deleted.
            if self.keep_audio_var.get():
                self._keep_audio(initial_folder, bat_dir, channel_name)

            # 4a. Audio length sanity check (right after downloads finish)
            if not self._check_long_audio(initial_folder):
                return "cancelled"

            # 4b. The work folders must start empty (nothing in them is asked about)
            self._prepare_work(bat_dir, resume=False, discard=discard_work)
            self._note_list_fingerprinting(bat_dir)

            # 5 + 6. Scan and fingerprint. Scan THIS channel's download
            # subfolder -- the same folder we downloaded into, split in place,
            # and delete further down -- not the whole output directory, which
            # in a queue run can still hold a previous channel whose cleanup
            # failed. (This read an undefined `output_dir` and raised NameError
            # on every download run.)
            pklz_dir = bat_dir / WORK_PKLZ
            pklz_before = self._snapshot_pklz(pklz_dir)
            source_dir = initial_folder
            complete = self._run_fingerprint_stage(bat_dir, source_dir, status_prefix=qp)
            if not complete:
                if (s := stopped()):
                    return s
                # Batches failed but their lists are still on disk, so the pklz
                # files that DID succeed are worth keeping and renaming below.
                self._log("[!] Fingerprinting did not complete cleanly - see the failures above.")
            if (s := stopped()):
                return s

            # 6b. Rename the newly-created pklz files using the channel handle.
            self._rename_new_pklz(pklz_dir, pklz_before, pklz_prefix)
            # From here its fingerprints start reaching the fingerprints
            # folder, so a list that stops now must not run this link again.
            if queue_mode:
                self._note_list_link_done(url)

            # 6c. Move the pklz files out of work\pklz, which the next link
            #     empties, to where finished fingerprints are kept.
            keep_dir = self._keep_dir(bat_dir)
            self._move_pklz_files(pklz_dir, keep_dir)
            if complete and self.skip_done_var.get():
                self._remember_done(self._downloaded_ok)

            # 6d. Delete this channel's download folder now that its audio has
            #     been fingerprinted and the pklz files saved. The next run
            #     recreates the folder if needed.
            self._clear_download_folder(initial_folder)
            # With the audio gone, the file lists and resume record are spent.
            self._clear_work_if_moved(bat_dir)

            # 7. Report + open the folder where the pklz files ended up.
            #    In a multi-channel queue, opening after every channel would
            #    spam identical folder windows, so queue mode reports without
            #    opening here and opens once at the end of the whole queue.
            self._report_pklz(keep_dir, label="Finished fingerprints", open_folder=not queue_mode)
            # Remember where this channel's pklz files landed so the queue
            # runner can open the final location once at the end.
            self._last_report_dir = keep_dir

            self._log(f"[+] {qp}Link done.")
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
            self._set_now_title("Now · audio on disk")

            # Only work\texts is cleared here. work\pklz is deliberately left
            # alone: a successful run moves every .pklz out to the database, so
            # anything still sitting there is the partial output of a run that
            # failed, and that is exactly what this path should be resuming from
            # rather than being made to rebuild. The fingerprint stage decides
            # per batch whether an existing .pklz still matches its file list.
            self._forget_list_fingerprinting()
            self._prepare_work(bat_dir, resume=True)

            pklz_dir = bat_dir / WORK_PKLZ
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
            complete = self._run_fingerprint_stage(bat_dir, source_dir)
            if not complete:
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

            # Move them to where finished fingerprints are kept, then list and
            # optionally open that folder.
            keep_dir = self._keep_dir(bat_dir)
            self._move_pklz_files(pklz_dir, keep_dir)
            # After a clean run nothing is left to resume. After a failed one the
            # record stays, so pressing the button again redoes only what failed.
            if complete:
                self._clear_work_if_moved(bat_dir)
            self._report_pklz(keep_dir, label="Finished fingerprints")

            self._log("[+] All done.")
            self.root.after(0, self._notify_done)
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
            items = sorted(self._fp_progress.items())
        done = sum(d for _, (d, _t) in items)
        total = sum(t for _, (_d, t) in items)
        active = len(items)
        base = self._fp_status_base
        if total:
            self._set_status(f"{base}{done:,}/{total:,} files "
                             f"({done * 100 // total}%) across {active} batch(es)")
        else:
            self._set_status(f"{base}fingerprinting...")
        # The Now panel: overall files, and a row per batch under way.
        self._set_progress("Fingerprinting", done, total or None)
        running = [f"batch {b}   {d:,} of {t:,} files ({d * 100 // t}%)"
                   for b, (d, t) in items if 0 < d < t]
        waiting = sum(1 for _, (d, _t) in items if d == 0)
        finished = sum(1 for _, (d, t) in items if t and d >= t)
        running.append(f"{waiting} batch(es) waiting, {finished} done")
        self.root.after(0, self._show_rows, running)

    def _scan_audio_files(self, source_dir: Path, texts_dir: Path, batch_size: int) -> int:
        """Walk source_dir for audio and write work/texts/<n>.txt lists of batch_size
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
        """The record is kept in memory for as long as the program runs, and
        also written to FINGERPRINTED_RECORD in work\\pklz when that is turned
        on (Settings, Fingerprinting), so it outlives the program. Without the
        file, a run that resumes after a restart has no record and makes the
        .pklz files it finds again (see _fingerprint_batches)."""
        remembered = self._fp_records.get(str(pklz_dir))
        if remembered is not None:
            return dict(remembered)
        try:
            data = json.loads((pklz_dir / self.FINGERPRINTED_RECORD).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _save_fingerprinted(self, pklz_dir: Path, record: dict[str, str]) -> None:
        self._fp_records[str(pklz_dir)] = dict(record)
        path = pklz_dir / self.FINGERPRINTED_RECORD
        if not self.fp_record_var.get():
            # Off: memory only. A file left from when it was on would fall
            # behind the record from here on, so it goes.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        try:
            path.write_text(json.dumps(record, indent=1), encoding="utf-8")
        except OSError as e:
            self._log(f"[!] Could not update {self.FINGERPRINTED_RECORD}: {e}")

    def _forget_fingerprinted(self, bat_dir: Path) -> None:
        """work\\pklz is being emptied: its record goes with it."""
        self._fp_records.pop(str(bat_dir / WORK_PKLZ), None)

    def _run_audfprint_batch(
        self,
        bat_dir: Path,
        batch_id: int,
        ncores: int,
        log_lock: threading.Lock,
        parts: int = 1,
    ) -> tuple[int, bool, str]:
        """Run audfprint over work/texts/<batch_id>.txt -> work/pklz/<batch_id>.pklz,
        as one process or, with `parts` above 1, as that many side by side
        whose results are merged. Returns (batch_id, ok, detail)."""
        # Every remaining batch is already queued in the pool, so the moment a
        # running one is killed the pool dispatches the next. Without this
        # guard, pressing Stop during batch 4 simply started batch 5.
        self._wait_if_paused()
        if self.cancel_flag.is_set():
            return batch_id, False, "cancelled before it started"

        texts_dir = bat_dir / WORK_TEXTS
        pklz_dir = bat_dir / WORK_PKLZ
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

        try:
            files = [ln for ln in list_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError:
            files = []
        total_files = len(files)
        new_args = ["new", "-C",               # -C: keep going when one file fails to read
                    "--ncores", str(max(1, ncores))]

        if parts <= 1:
            ok, detail = self._audfprint_process(
                bat_dir, batch_id, [*new_args, "--dbase", str(part_pklz), "--list", str(list_file)],
                log_lock, total_files)
        else:
            # A batch split into parts: each part's files are fingerprinted by
            # a process of its own, side by side, and the parts are merged into
            # the batch's one .pklz. Merging only combines hash buckets (which
            # hold up to 100 entries, the same cap as building in one go), so
            # the result matches what a single process would have written.
            part_lists = [texts_dir / f"{batch_id}.part{k + 1}.txt" for k in range(parts)]
            part_dbs = [pklz_dir / f"{batch_id}.part{k + 1}.pklz" for k in range(parts)]
            for k, (lst, db) in enumerate(zip(part_lists, part_dbs)):
                lst.write_text("\n".join(files[k::parts]), encoding="utf-8")
                db.unlink(missing_ok=True)
            with ThreadPoolExecutor(max_workers=parts) as pool:
                results = list(pool.map(
                    lambda k: self._audfprint_process(
                        bat_dir, batch_id,
                        [*new_args, "--dbase", str(part_dbs[k]), "--list", str(part_lists[k])],
                        log_lock, total_files, part=k),
                    range(parts)))
            failed = [d for good, d in results if not good]
            if failed or self.cancel_flag.is_set():
                ok, detail = False, (failed[0] if failed else "cancelled")
            else:
                with log_lock:
                    self._log(f"  | [batch {batch_id}] merging {parts} parts into one .pklz...",
                              tag="bat")
                ok, detail = self._audfprint_process(
                    bat_dir, batch_id,
                    ["newmerge", "--dbase", str(part_pklz), *map(str, part_dbs)], log_lock)
            for path in (*part_lists, *part_dbs):
                path.unlink(missing_ok=True)

        if self.cancel_flag.is_set():
            part_pklz.unlink(missing_ok=True)
            return batch_id, False, "cancelled"
        if not ok or not part_pklz.exists():
            part_pklz.unlink(missing_ok=True)
            return batch_id, False, detail or "audfprint wrote no .pklz"
        try:
            os.replace(part_pklz, final_pklz)
        except OSError as e:
            part_pklz.unlink(missing_ok=True)
            return batch_id, False, f"could not finalise {final_pklz.name}: {e}"
        return batch_id, True, f"{final_pklz.name} ({final_pklz.stat().st_size / 1024 / 1024:.1f} MB)"

    def _audfprint_process(
        self,
        bat_dir: Path,
        batch_id: int,
        args: list[str],
        log_lock: threading.Lock,
        total_files: int = 0,
        part: int = 0,
    ) -> tuple[bool, str]:
        """Run one audfprint process for a batch (the whole batch, one part of
        it, or the merge of its parts) and stream its output. Returns (ok,
        detail); detail says why it failed.

        Output is streamed rather than buffered: the old version used Node's
        exec(), which holds everything in memory and only hands it over at the
        end, so a batch that died mid-run reported an empty stdout and there
        was nothing to diagnose it with."""
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
        # -u matters: Python block-buffers stdout when it is a pipe rather than
        # a terminal, so audfprint's per-file lines would sit in the child's
        # buffer and arrive in one lump when the batch ended. Without it the
        # console shows a single line and then looks frozen for the twenty
        # minutes the batch actually takes.
        cmd = [sys.executable, "-u", *launcher, *args]

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
            return False, f"could not start audfprint: {e}"

        self._track_proc(proc)
        # Stop can land between the caller's guard and this spawn, in which case
        # _cancel walked a process list that did not yet contain us. Re-check
        # now that we are registered, so no batch survives by timing.
        if self.cancel_flag.is_set():
            self._kill_tree(proc)
        try:
            assert proc.stdout is not None
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
                # Reported as a counter instead, at most once a second per
                # batch, summed over its parts when it has them.
                m = _INGESTING_RE.search(line)
                if m:
                    now = time.time()
                    with self._fp_progress_lock:
                        # audfprint counts from #0, in each part separately
                        self._fp_part_done[(batch_id, part)] = int(m.group(1)) + 1
                        done = sum(n for (b, _p), n in self._fp_part_done.items() if b == batch_id)
                        self._fp_progress[batch_id] = (done, total_files)
                        report = now - self._fp_last_report.get(batch_id, 0.0) >= 1.0
                        if report:
                            self._fp_last_report[batch_id] = now
                    if report:
                        pct = f" {done * 100 // total_files}%" if total_files else ""
                        name = os.path.basename(m.group(2))
                        with log_lock:
                            self._log(f"  | [batch {batch_id}] {done}/{total_files or '?'}"
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
            return False, "cancelled"
        if proc.returncode != 0:
            detail = f"audfprint exited {proc.returncode}"
            if tail:
                detail += "\n      last output: " + "\n      ".join(tail[-6:])
            return False, detail
        return True, ""

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

        With fewer batches than jobs, though, independent batches leave jobs
        idle: a link under Recordings per file is one batch, and it used to run
        alone. So the spare jobs split each batch into parts, run as separate
        --ncores 1 processes (quiet, like any batch), and audfprint's newmerge
        combines their tables into the batch's one .pklz. Measured with 48
        files of 6:00 on the same machine, idle:

            1 process                          170.6s
            2 parts   97.3s + merge 51.4s  =  148.7s
            4 parts   60.0s + merge 58.9s  =  118.8s

        The merge costs about a minute however small the parts (it loads and
        saves 400 MB tables and walks every bucket in Python), so splitting
        only pays from about MIN_FILES_TO_SPLIT files, and pays most for the
        big batches: a full 1000-file batch goes from about 52 minutes to 14.
        The merged .pklz holds the same hashes (1,087,603 in both builds here,
        0.41% dropped in both) and matches the same way.

        The cap matters: peak memory measured at 676 MB for 120 files, which
        extrapolates to roughly 5.5 GB for a 1000-file batch, so the default of
        4 concurrent batches is about 22 GB of the 64 GB on this box. Raising it
        much further risks swapping, which would undo the gain."""
        pklz_dir = bat_dir / WORK_PKLZ
        pklz_dir.mkdir(parents=True, exist_ok=True)

        # Per batch, not max(id). A gap left by an earlier failure is work to
        # redo, not a batch to skip. A .pklz only counts as done when the batch
        # it belongs to still covers the same files (see the manifest note in
        # _scan_audio_files); anything else is stale and gets rebuilt.
        texts_dir = bat_dir / WORK_TEXTS
        record = self._load_fingerprinted(pklz_dir)
        record_lock = threading.Lock()
        pending: list[int] = []
        stale = unknown = 0
        for i in range(1, total_batches + 1):
            existing = pklz_dir / f"{i}.pklz"
            current = self._batch_list_hash(texts_dir, i)
            if existing.exists() and current and record.get(str(i)) == current:
                continue                      # genuinely already done
            if existing.exists():
                # Present but built from a different file set, or with no
                # record of what it was built from, so it may be wrong for this
                # batch number now. Rebuilt rather than trusted.
                if str(i) in record:
                    stale += 1
                else:
                    unknown += 1
                try:
                    existing.unlink()
                except OSError as e:
                    self._log(f"[!] Could not remove stale {existing.name}: {e}")
            pending.append(i)
        if stale:
            self._log(f"[!] {stale} existing pklz file(s) were built from a different "
                      f"set of files (the audio changed) and are being rebuilt.")
        if unknown:
            hint = (f" To keep that record when the program closes, turn on Write "
                    f"{self.FINGERPRINTED_RECORD} in Settings, Fingerprinting."
                    if not self.fp_record_var.get() else "")
            self._log(f"[!] {unknown} pklz file(s) from an earlier run are being made again: "
                      f"there is no record of which files they hold.{hint}")
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
            self._fp_part_done = {}
            self._fp_last_report = {}
            for i in pending:
                try:
                    n_files = sum(1 for ln in (texts_dir / f"{i}.txt").read_text(
                        encoding="utf-8").splitlines() if ln.strip())
                except OSError:
                    n_files = 0
                self._fp_progress[i] = (0, n_files)

        # "Fingerprint jobs at once" is how many audfprint processes run side
        # by side. With at least that many batches, each batch is one process.
        # With fewer (a link under Recordings per file is a single batch), the
        # spare jobs go into the batches: each is split into parts that run at
        # once and are merged into its one .pklz. Before this, a single batch
        # ran alone however many jobs were allowed. The merge costs about a
        # minute however small the parts are, so a batch is only split from
        # MIN_FILES_TO_SPLIT files, into parts of MIN_FILES_PER_PART or more.
        jobs = self._safe_int(self.fp_concurrency_var, self.DEFAULT_FP_JOBS)
        concurrency = max(1, min(jobs, len(pending)))
        spare = jobs // len(pending) if len(pending) < jobs else 1
        with self._fp_progress_lock:
            parts = {i: (max(1, min(spare, n // self.MIN_FILES_PER_PART))
                         if n >= self.MIN_FILES_TO_SPLIT else 1)
                     for i, (_d, n) in self._fp_progress.items()}
        ncores = AUDFPRINT_NCORES
        n_files_all = sum(t for _d, t in self._fp_progress.values())
        if max(parts.values()) > 1:
            self._log(f"[*] Fingerprinting {n_files_all:,} file(s) in {len(pending)} batch(es), "
                      f"each split into up to {max(parts.values())} parts that run at the "
                      f"same time and are merged into one .pklz")
        else:
            self._log(f"[*] Fingerprinting {n_files_all:,} file(s) in {len(pending)} batch(es), "
                      f"{concurrency} at a time")

        log_lock = threading.Lock()
        completed = 0
        failures: list[tuple[int, str]] = []
        started = time.time()

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(self._run_audfprint_batch, bat_dir, i, ncores, log_lock, parts[i]): i
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
        texts_dir = bat_dir / WORK_TEXTS
        batch_size = self._safe_int(self.batch_size_var, 1000)

        self._set_status(f"{status_prefix}Scanning for audio...")
        self._set_progress("Scanning for audio")
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
        for i, f in enumerate(audio_files):
            self._wait_if_paused()
            if self.cancel_flag.is_set():
                return False
            self._set_progress("Checking lengths", i, len(audio_files))
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
                f"[+] All {len(audio_files)} file(s) are {SPLIT_TRIGGER // 60}:00 "
                f"or shorter; nothing to split."
            )
            return True

        long_files.sort(key=lambda x: -x[1])

        # No "splitting is disabled" branch here any more: that case now returns
        # at the top of this method, before anything is enumerated or probed.
        self._log(
            f"[*] Splitting {len(long_files)} file(s) over {SPLIT_TRIGGER // 60}:00 into "
            f"{SPLIT_SEGMENT // 60}:00 pieces (the last piece takes the remainder)...",
            tag="splitter",
        )
        made = 0
        for i, (path, dur) in enumerate(long_files):
            self._wait_if_paused()
            if self.cancel_flag.is_set():
                return False
            self._set_progress("Splitting", i, len(long_files))
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
            # Which extractor found it, for the done list (_archive_key). Flat
            # entries carry ie_key; a single page has extractor_key instead.
            "%(ie_key,extractor_key)s",
        ))
        cmd = [
            *self.ytdlp,
            *YTDLP_UTF8,
            "--js-runtimes", "node",
            "--flat-playlist",
            "--no-warnings",
            "--print", template,
            *self._extra_args(),
            url,
        ]

        entries: list[dict] = []
        self._set_progress("Listing entries")
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
            "extractor",
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
                # pipeline only consumes id/title/duration/url/webpage_url and
                # the extractor.
                for k in ("playlist_title", "playlist_uploader",
                          "playlist_channel", "channel"):
                    obj.pop(k, None)
                entries.append(obj)

                # Throttle progress logs to ~1/sec so we don't spam the log.
                now = time.monotonic()
                if now - last_log >= 1.0:
                    self._set_status(f"Listing entries ({len(entries)} found)...")
                    self._set_progress(f"Listing entries: {len(entries):,} found")
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
        # The items that downloaded, for the done list (see _remember_done).
        self._downloaded_ok: list[dict] = []

        # Number of visible slots = min(workers, total)
        slot_count = max(1, min(workers, total))
        self.root.after(0, self._init_slots, slot_count)
        self._set_progress("Downloading", 0, total)

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
                    if not futures[fut].get("_left_out"):
                        self._downloaded_ok.append(futures[fut])
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
                self._set_progress("Downloading", done, total)
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
        self._wait_if_paused()
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
            *self.ytdlp,
            *YTDLP_UTF8,
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
        # The length limits, for items the listing gave no length for (the
        # rest were left out before downloading). "?" lets an item whose
        # length yt-dlp cannot find either through.
        limits = []
        if (shortest := self._safe_int(self.min_seconds_var, 0, minimum=0)):
            limits.append(f"duration >=? {shortest}")
        if (longest := self._safe_int(self.max_minutes_var, 0, minimum=0)):
            limits.append(f"duration <=? {longest * 60}")
        if limits:
            cmd += ["--match-filter", " & ".join(limits)]
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
            paused_for = 0.0
            while not ticker_stop.is_set():
                es = extract_start[0]
                if self.pause_flag.is_set():
                    paused_for += 1.0     # the clock stops with the process
                    if es is not None:
                        self._update_slot(slot, f"{short_title} | paused")
                elif es is not None:
                    elapsed = int(time.time() - es - paused_for)
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
                if "does not pass filter" in line:
                    entry["_left_out"] = True     # outside the length limits
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
        if success and entry.get("_left_out"):
            self._log(f"  - Left out '{title}': outside the length limits in Settings.")
        if not success and not self.cancel_flag.is_set():
            joined = " ".join(err_lines)
            reason = self._classify_yt_dlp_error(joined)
            if reason:
                self._log(
                    f"  ! Skipped '{title}': {reason}",
                    tag="warning",
                )
            elif err_lines:
                self._log(f"  ! Skipped '{title}': {err_lines[-1][:300]}", tag="warning")
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
            self._log(f"[!] Work folder doesn't exist: {pklz_dir}", tag="warning")
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

    def _prepare_work(self, bat_dir: Path, resume: bool, discard: bool = False) -> None:
        """Empty the program's scratch folders before fingerprinting, without
        asking: nothing in them is the user's to decide about. A .pklz file an
        interrupted run left in work\\pklz is a finished fingerprint file, so
        it is moved to the fingerprints folder as recovered-<name>, never
        deleted.

        `resume` (the buttons for audio already on disk) keeps work\\pklz as it
        is: its .pklz files and their record (_load_fingerprinted) are what
        that run resumes from. Only the file lists are rebuilt.

        `discard` (a continued list starting over the link it stopped in, which
        had begun fingerprinting) deletes the leftovers instead: they are that
        link's own unfinished work, and keeping them would put its audio in the
        database twice."""
        shutil.rmtree(bat_dir / WORK_TEXTS, ignore_errors=True)
        pklz_dir = bat_dir / WORK_PKLZ
        if resume:
            return
        self._forget_fingerprinted(bat_dir)
        if not pklz_dir.is_dir():
            return
        leftovers = sorted(p for p in pklz_dir.glob("*.pklz") if p.is_file())
        if discard:
            if leftovers:
                self._log(f"[*] Discarding {len(leftovers)} unfinished .pklz file(s) of the stopped "
                          f"link; it is fingerprinted again in full.")
            shutil.rmtree(pklz_dir, ignore_errors=True)
            return
        if leftovers:
            keep = self._keep_dir(bat_dir)
            keep.mkdir(parents=True, exist_ok=True)
            for src in leftovers:
                target = keep / f"recovered-{src.name}"
                n = 2
                while target.exists():
                    target = keep / f"recovered-{src.stem}_{n}{src.suffix}"
                    n += 1
                try:
                    shutil.move(str(src), str(target))
                except OSError as e:
                    self._log(f"[!] Could not move {src.name} out of the work folder: {e}",
                              tag="warning")
                    return          # leave the folder alone rather than lose it
            self._log(f"[*] Moved {len(leftovers)} .pklz file(s) left by an interrupted "
                      f"run to {keep} (named recovered-...).")
        shutil.rmtree(pklz_dir, ignore_errors=True)

    def _clear_work_if_moved(self, bat_dir: Path) -> None:
        """Empty work\\texts and work\\pklz once every .pklz has been moved out.

        Left in place, the file lists and fingerprinted.json made the next
        link stop at the "not empty" prompt, which waits two minutes before
        answering itself. A .pklz that failed to move is never deleted: then
        nothing is cleared."""
        pklz_dir = bat_dir / WORK_PKLZ
        if pklz_dir.is_dir() and any(pklz_dir.glob("*.pklz")):
            self._log(f"[!] Some .pklz files are still in {pklz_dir}; left in place.", tag="warning")
            return
        self._forget_fingerprinted(bat_dir)
        for sub in (WORK_TEXTS, WORK_PKLZ):
            shutil.rmtree(bat_dir / sub, ignore_errors=True)

    def _keep_dir_audio(self, bat_dir: Path) -> Path:
        """Where kept audio goes: the chosen folder, or audio in the program folder."""
        return Path(norm_path(self.keep_audio_dir_var.get()) or bat_dir / KEEP_AUDIO_DIR)

    def _keep_audio(self, folder: Path, bat_dir: Path, channel_name: str) -> None:
        """Worker thread: put a copy of every downloaded file in `folder` into
        <kept audio folder>\\<channel name>, before splitting rewrites them and
        the folder is deleted once fingerprinted.

        A hard link where it can be, so keeping costs no time and no space
        (splitting only removes the download's own name for the file, and
        nothing writes into a download in place); a copy where it cannot, as
        between drives. A file already kept, same name and size, is left as it
        is, so running a link again keeps nothing twice."""
        dest = self._keep_dir_audio(bat_dir) / channel_name
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._log(f"[!] Could not create {dest} to keep the audio in: {e}", tag="warning")
            return
        kept = already = 0
        for src in sorted(p for p in folder.iterdir()
                          if p.is_file() and p.suffix.lower() in self.AUDIO_EXTENSIONS):
            target = dest / src.name
            if target.exists():
                if target.stat().st_size == src.stat().st_size:
                    already += 1
                    continue
                n = 2
                while target.exists():
                    target = dest / f"{src.stem} ({n}){src.suffix}"
                    n += 1
            try:
                try:
                    os.link(src, target)
                except OSError:
                    shutil.copy2(src, target)
                kept += 1
            except OSError as e:
                self._log(f"[!] Could not keep {src.name}: {e}", tag="warning")
        if kept:
            self._log(f"[+] Kept {kept} downloaded file(s) in {dest}")
        if already:
            self._log(f"[*] {already} file(s) were already kept there.")

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

    def _report_pklz(self, pklz_dir: Path, label: str = "Fingerprints", open_folder: bool = True) -> int:
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
    # A taskbar button of the program's own, showing its fingerprint icon.
    # Without this Windows files the window under pythonw.exe and shows
    # Python's icon there. It has to be set before the first window exists.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(dependencies.APP_ID)
        except Exception:  # noqa: BLE001
            pass
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