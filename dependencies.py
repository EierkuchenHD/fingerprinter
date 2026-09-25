"""Check, and on request install, everything the Fingerprinter needs. Windows only.

Used in two places:

  * yt-fingerprinter.pyw checks at startup and from "Check setup", and offers
    to install whatever is missing or broken;
  * setup.bat runs `python dependencies.py` in a console, which is how a first
    install gets the Python packages in place before the window can open.

Nothing is installed without asking, and nothing that already works is touched:
every component is checked by running it, not just by looking for a file.

ffmpeg and Node.js are installed into this folder's tools\\ subfolder instead
of system-wide. That needs no admin rights and no PATH edits, and deleting the
folder removes them again. add_tools_to_path() puts them first on PATH for this
program and everything it starts.

audfprint must be WerZatSong's version (Nel80s/WerZatSong, libs/audfprint),
not upstream dpwe/audfprint. On Windows the upstream version crashes on file
names outside the console code page (it prints every name it reads), misreads
the UTF-8 file lists this program writes, and rejects audio that carries cover
art. WerZatSong's copy fixes all three.

    python dependencies.py           check, then offer to install what is missing
    python dependencies.py --check   check only; exit code 1 if anything is wrong
    --update-ytdlp                   first bring yt-dlp to its newest release (setup.bat)
    --shortcut                       make Fingerprinter.lnk, and at the end offer one on the
                                     desktop (setup.bat)
    python dependencies.py --uninstall <file>
                                     remove the Fingerprinter, asking first (uninstall.bat,
                                     which runs the command left in <file> afterwards)
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

APP_DIR = Path(__file__).resolve().parent
TOOLS_DIR = APP_DIR / "tools"
FFMPEG_BIN = TOOLS_DIR / "ffmpeg" / "bin"
NODE_DIR = TOOLS_DIR / "node"

MIN_PYTHON = (3, 10)

# The audfprint this program is built for. Upstream dpwe/audfprint is not a
# substitute; see the module docstring.
WERZATSONG_AUDFPRINT_URL = "https://github.com/Nel80s/WerZatSong/tree/main/libs/audfprint"
_WERZATSONG_RAW = "https://raw.githubusercontent.com/Nel80s/WerZatSong/main/libs/audfprint/"
AUDFPRINT_FILES = (
    "__init__.py", "audfprint.py", "audfprint_analyze.py", "audfprint_match.py",
    "audio_read.py", "hash_table.py", "stft.py",
)
# Lines only WerZatSong's copy has. Upstream prints in the console code page
# and chokes on video streams (cover art) in ffmpeg's output.
_WERZATSONG_MARKERS = {
    "audfprint.py": 'reconfigure(encoding="utf-8")',
    "audio_read.py": "mjpeg",
}

FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
NODE_DIST_URL = "https://nodejs.org/dist/"

# What audfprint imports. Read from requirements.txt so the two cannot drift.
_FALLBACK_PACKAGES = ("numpy", "scipy", "docopt", "joblib", "psutil")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_USER_AGENT = "Fingerprinter-setup (+https://github.com/EierkuchenHD/fingerprinter)"

Log = Callable[[str], None]


@dataclass
class Status:
    key: str            # "python", "packages", "yt-dlp", "ffmpeg", "node", "audfprint"
    name: str           # what the user sees
    why: str            # what it is needed for
    ok: bool
    state: str          # "ok", "missing", "not working", "wrong version"
    detail: str         # version when ok, otherwise what is wrong
    fix: str = ""       # what installing would do; "" if it cannot be done from here
    manual: str = ""    # how to fix it by hand


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def add_tools_to_path() -> None:
    """Put tools\\ffmpeg\\bin and tools\\node first on PATH for this process and
    its children, if they exist. Safe to call repeatedly."""
    current = os.environ.get("PATH", "").split(os.pathsep)
    for folder in (NODE_DIR, FFMPEG_BIN):
        if folder.is_dir() and str(folder) not in current:
            current.insert(0, str(folder))
    os.environ["PATH"] = os.pathsep.join(current)


def console_python() -> str:
    """python.exe rather than pythonw.exe: pip and audfprint write to stdout."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.is_file():
            return str(candidate)
    return str(exe)


def _run(cmd: list[str], timeout: float = 60, cwd: str | None = None) -> tuple[int, str]:
    """Run a command hidden and return (exit code, combined output).
    -1 means it could not be started at all."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=cwd, creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        return -1, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return -2, f"{Path(cmd[0]).name} did not answer within {timeout:.0f}s"
    except OSError as e:
        return -1, str(e)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1][:200] if lines else "no output"


def required_packages() -> list[str]:
    try:
        lines = (APP_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
    except OSError:
        return list(_FALLBACK_PACKAGES)
    names = []
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line.split("=")[0].split("<")[0].split(">")[0].split("[")[0].strip())
    return names or list(_FALLBACK_PACKAGES)


# ----------------------------------------------------------------------------
# checks
# ----------------------------------------------------------------------------

def check_python() -> Status:
    version = platform.python_version()
    ok = sys.version_info >= MIN_PYTHON
    return Status(
        "python", "Python", "runs this program and audfprint", ok,
        "ok" if ok else "wrong version",
        version if ok else f"{version} is too old; 3.10 or newer is needed",
        manual="Install Python 3.10 or newer from python.org (tick \"Add python.exe to PATH\"), "
               "or run setup.bat.",
    )


_IMPORT_PROBE = """
import importlib, json, sys
result = {}
for name in sys.argv[1:]:
    try:
        importlib.import_module(name)
        result[name] = "ok"
    except ModuleNotFoundError as e:
        result[name] = "missing" if e.name == name else "broken: " + str(e)
    except Exception as e:
        result[name] = "broken: " + type(e).__name__ + ": " + str(e)
print(json.dumps(result))
"""


def _probe_imports(names: list[str]) -> dict[str, str]:
    """Import each package in a fresh interpreter: {name: "ok" | "missing" | "broken: ..."}."""
    _, out = _run([console_python(), "-c", _IMPORT_PROBE, *names], timeout=120)
    # Warnings from an import land in the same output; the result is the JSON line.
    for line in reversed(out.splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    return {n: f"broken: {_last_line(out)}" for n in names}


def check_packages() -> Status:
    names = required_packages()
    result = _probe_imports(names)
    missing = [n for n in names if result.get(n) == "missing"]
    broken = [n for n in names if str(result.get(n, "")).startswith("broken")]
    why = "audfprint's Python packages (" + ", ".join(names) + ")"
    if not missing and not broken:
        return Status("packages", "Python packages", why, True, "ok", "all installed")
    parts = []
    if missing:
        parts.append("missing: " + ", ".join(missing))
    if broken:
        parts.append("not working: " + "; ".join(f"{n} ({result[n][8:][:80]})" for n in broken))
    fix = []
    if missing:
        fix.append("pip install " + " ".join(missing))
    if broken:
        fix.append("pip install --force-reinstall " + " ".join(broken))
    return Status(
        "packages", "Python packages", why, False,
        "not working" if broken else "missing", "; ".join(parts),
        fix=" and ".join(fix) + " (from PyPI)",
        manual="pip install -r requirements.txt",
    )


def _find_ytdlp() -> tuple[list[str] | None, str]:
    """(command that runs a working yt-dlp, its version), or (None, "").

    The newer of two: the copy pip installed into this Python, and a
    yt-dlp.exe on PATH. The pip copy wins a tie: it is the one setup.bat and
    Check setup bring to the newest release (update_ytdlp), and it works even
    when Python's Scripts folder is not on PATH. The exe may have come from
    anywhere (winget, Chocolatey, a download) and pip cannot update it, but
    when it is the newer one, as after updating from a version that used it
    without running setup again, switching to an older pip copy would only
    make downloads fail."""
    found: list[tuple[list[str], str]] = []
    module = [console_python(), "-m", "yt_dlp"]
    code, out = _run([*module, "--version"], timeout=30)
    if code == 0:
        found.append((module, _last_line(out)))
    exe = shutil.which("yt-dlp")
    if exe:
        code, out = _run([exe, "--version"], timeout=30)
        if code == 0:
            found.append(([exe], _last_line(out)))
    if not found:
        return None, ""
    return max(found, key=lambda f: _version_key(f[1]))     # the first, the pip copy, on a tie


def _ytdlp_module_version() -> str:
    """The version of the yt-dlp installed into this Python, or "" if none."""
    code, out = _run([console_python(), "-m", "yt_dlp", "--version"], timeout=30)
    return _last_line(out) if code == 0 else ""


def _newest_ytdlp_on_pypi() -> str:
    """The newest yt-dlp release on PyPI, or "" when PyPI cannot be reached."""
    try:
        return str(json.loads(_fetch_text("https://pypi.org/pypi/yt-dlp/json"))["info"]["version"])
    except Exception:  # noqa: BLE001 - offline, blocked, or an unexpected reply
        return ""


def _version_key(version: str) -> tuple[int, ...]:
    """2026.08.19 and PyPI's 2026.8.19 compare equal; nightlies sort after."""
    return tuple(int(part) for part in re.findall(r"\d+", version))


def update_ytdlp(log: Log) -> bool:
    """Install the newest yt-dlp release into this Python, whatever version is
    there now. Sites change what they send all the time, and an old yt-dlp is
    the commonest reason downloads fail. pip installs the newest release, or
    installs one when there is none; the program runs this copy (_find_ytdlp).

    PyPI is asked for the newest version number first, because pip cannot be
    taken at its word: with no connection it still says "Requirement already
    satisfied" and succeeds, which would read as "already the newest"."""
    before = _ytdlp_module_version()
    newest = _newest_ytdlp_on_pypi()
    log("Updating yt-dlp to the newest release" + (f" (now {before})..." if before else "..."))
    if not _pip(["--upgrade", "yt-dlp"], log):
        log("    ! yt-dlp could not be updated" +
            (f"; version {before} stays in use." if before else "."))
        return False
    after = _ytdlp_module_version()
    if not after:
        log("    ! pip finished, but yt-dlp does not start.")
        return False
    if not before:
        _record_packages(["yt-dlp"])
    if newest and _version_key(after) < _version_key(newest):
        log(f"    ! yt-dlp {newest} is the newest release, but pip left {after} in place.")
        return False
    if not newest and after == before:
        log(f"    ! PyPI could not be reached to look for a newer yt-dlp; version {after} "
            f"stays in use.")
        return False
    if after == before:
        log(f"    yt-dlp {after} is already the newest release.")
    elif before:
        log(f"    yt-dlp updated from {before} to {after}.")
    else:
        log(f"    yt-dlp {after} installed.")
    return True


def ytdlp_command() -> list[str] | None:
    """The command that runs a working yt-dlp, or None."""
    return _find_ytdlp()[0]


def check_ytdlp() -> Status:
    why = "downloads the audio"
    cmd, version = _find_ytdlp()
    if cmd:
        where = "on PATH" if len(cmd) == 1 else "Python module"
        return Status("yt-dlp", "yt-dlp", why, True, "ok", f"{version} ({where})")
    exe = shutil.which("yt-dlp")
    if exe:
        _, out = _run([exe, "--version"], timeout=30)
        return Status(
            "yt-dlp", "yt-dlp", why, False, "not working", f"{exe}: {_last_line(out)}",
            fix="pip install --force-reinstall yt-dlp (from PyPI); the program then uses that copy",
            manual="pip install --force-reinstall yt-dlp",
        )
    return Status(
        "yt-dlp", "yt-dlp", why, False, "missing", "not found",
        fix="pip install yt-dlp (from PyPI)", manual="pip install yt-dlp",
    )


def _tool_status(key: str, name: str, why: str, commands: list[list[str]],
                 fix: str, manual: str) -> Status:
    problems = []
    version = ""
    for cmd in commands:
        exe = shutil.which(cmd[0])
        if not exe:
            problems.append(f"{cmd[0]} not found")
            continue
        code, out = _run([exe, *cmd[1:]], timeout=30)
        if code != 0:
            problems.append(f"{cmd[0]} does not run ({_last_line(out)})")
        elif not version:
            version = out.splitlines()[0].split(" Copyright")[0][:80] if out else "installed"
    if not problems:
        return Status(key, name, why, True, "ok", version)
    state = "missing" if all(p.endswith("not found") for p in problems) else "not working"
    return Status(key, name, why, False, state, "; ".join(problems), fix=fix, manual=manual)


def check_ffmpeg() -> Status:
    return _tool_status(
        "ffmpeg", "ffmpeg and ffprobe",
        "read audio lengths, split long files, and decode audio for audfprint",
        [["ffmpeg", "-version"], ["ffprobe", "-version"]],
        fix=f"download the ffmpeg essentials build from gyan.dev (about 100 MB) into {FFMPEG_BIN}",
        manual="Install ffmpeg from gyan.dev/ffmpeg/builds and put its bin folder on PATH, "
               "or let Check setup install it.",
    )


def check_node() -> Status:
    return _tool_status(
        "node", "Node.js", "yt-dlp needs it to read YouTube pages",
        [["node", "--version"]],
        fix=f"download the current Node.js LTS from nodejs.org (about 30 MB) into {NODE_DIR}",
        manual="Install Node.js LTS from nodejs.org, or let Check setup install it.",
    )


def audfprint_problem(bat_dir: Path | str) -> str | None:
    """Quick, file-only check of <bat_dir>\\audfprint. None if it looks right,
    otherwise a sentence saying what is wrong. Does not run anything."""
    folder = Path(bat_dir) / "audfprint"
    if not (folder / "audfprint.py").is_file():
        nested = next(folder.glob("*/audfprint.py"), None) if folder.is_dir() else None
        if nested is not None:
            return (f"audfprint.py is one folder too deep ({nested.parent.name}\\audfprint.py); "
                    f"it must be directly in {folder}")
        return f"not found in {folder}"
    for filename, marker in _WERZATSONG_MARKERS.items():
        try:
            text = (folder / filename).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return f"{filename} is missing from {folder}"
        if marker not in text:
            return ("this is upstream audfprint (dpwe/audfprint); the Fingerprinter needs "
                    "WerZatSong's version")
    return None


def check_audfprint(bat_dir: Path | str) -> Status:
    folder = Path(bat_dir) / "audfprint"
    why = "makes the fingerprints; must be WerZatSong's version"
    fix = (f"download WerZatSong's audfprint ({len(AUDFPRINT_FILES)} files) from GitHub into {folder}"
           + (" and keep the current folder as audfprint.old" if folder.exists() else ""))
    manual = f"Get the libs\\audfprint folder from {WERZATSONG_AUDFPRINT_URL} and put it at {folder}."
    problem = audfprint_problem(bat_dir)
    if problem:
        state = "wrong version" if "upstream" in problem else "missing"
        return Status("audfprint", "audfprint (WerZatSong version)", why, False, state, problem,
                      fix=fix, manual=manual)
    # Runs it for real: --version imports every module and package it needs.
    code, out = _run([console_python(), str(folder / "audfprint.py"), "--version"],
                     timeout=120, cwd=str(folder))
    if code == 0:
        return Status("audfprint", "audfprint (WerZatSong version)", why, True, "ok",
                      f"version {_last_line(out)}, in {folder}")
    # A missing numpy/scipy/... stops it too, but its files are fine: that is
    # the packages item's to fix, and downloading audfprint again would not help.
    missing = re.search(r"No module named '([^'.]+)", out)
    if missing and missing.group(1) in required_packages():
        return Status("audfprint", "audfprint (WerZatSong version)", why, False, "not working",
                      f"needs the Python package {missing.group(1)} (see Python packages)")
    return Status("audfprint", "audfprint (WerZatSong version)", why, False, "not working",
                  _last_line(out), fix=fix, manual=manual)


def check_all(bat_dir: Path | str = APP_DIR) -> list[Status]:
    add_tools_to_path()
    return [check_python(), check_packages(), check_ytdlp(), check_ffmpeg(),
            check_node(), check_audfprint(bat_dir)]


# ----------------------------------------------------------------------------
# installers
# ----------------------------------------------------------------------------

def _download(url: str, dest: Path, log: Log, label: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response, open(dest, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        next_report = 0.1
        while True:
            block = response.read(1 << 20)
            if not block:
                break
            out.write(block)
            done += len(block)
            if total and done / total >= next_report:
                log(f"    {label}: {done * 100 // total}% of {total / 1e6:.0f} MB")
                next_report += 0.1
    if total and done != total:
        raise OSError(f"download incomplete ({done:,} of {total:,} bytes)")


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _pip(args: list[str], log: Log) -> bool:
    python = console_python()
    if _run([python, "-m", "pip", "--version"], timeout=60)[0] != 0:
        log("    pip is missing; setting it up with ensurepip...")
        code, out = _run([python, "-m", "ensurepip", "--upgrade"], timeout=300)
        if code != 0:
            log(f"    ! ensurepip failed: {_last_line(out)}")
            return False
    cmd = [python, "-m", "pip", "install", "--disable-pip-version-check", *args]
    log("    " + " ".join(["pip", "install", *args]))
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
        )
    except OSError as e:
        log(f"    ! could not start pip: {e}")
        return False
    assert proc.stdout is not None
    tail = []
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            tail = (tail + [line])[-8:]
            if line.startswith(("Collecting", "Successfully", "Requirement already", "ERROR")):
                log(f"      {line[:160]}")
    proc.wait()
    if proc.returncode != 0:
        log("    ! pip failed. Last output:")
        for line in tail:
            log(f"      {line[:160]}")
        return False
    return True


def install_packages(status: Status, log: Log) -> bool:
    names = required_packages()
    result = _probe_imports(names)
    missing = [n for n in names if result.get(n, "missing") == "missing"]
    broken = [n for n in names if str(result.get(n, "")).startswith("broken")]
    ok = True
    if missing:
        if _pip(missing, log):
            _record_packages(missing)
        else:
            ok = False
    if broken:
        ok = _pip(["--force-reinstall", *broken], log) and ok
    return ok


def install_ytdlp(status: Status, log: Log) -> bool:
    had_module = bool(_ytdlp_module_version())
    ok = _pip(["--force-reinstall", "yt-dlp"] if status.state == "not working" else ["yt-dlp"], log)
    if ok and not had_module:
        _record_packages(["yt-dlp"])
    return ok


def install_ffmpeg(status: Status, log: Log) -> bool:
    FFMPEG_BIN.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TOOLS_DIR) as tmp:
        archive = Path(tmp) / "ffmpeg.zip"
        log(f"    downloading {FFMPEG_ZIP_URL}")
        _download(FFMPEG_ZIP_URL, archive, log, "ffmpeg")
        expected = _fetch_text(FFMPEG_ZIP_URL + ".sha256").split()[0].lower()
        if _sha256(archive) != expected:
            log("    ! checksum does not match gyan.dev's published SHA-256; not installed")
            return False
        log("    checksum verified")
        with zipfile.ZipFile(archive) as zf:
            wanted = {"ffmpeg.exe", "ffprobe.exe"}
            for member in zf.namelist():
                name = member.rsplit("/", 1)[-1]
                if name in wanted and member.rsplit("/", 2)[-2] == "bin":
                    with zf.open(member) as src, open(FFMPEG_BIN / name, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    wanted.discard(name)
            if wanted:
                log(f"    ! the archive did not contain {', '.join(sorted(wanted))}")
                return False
    log(f"    installed into {FFMPEG_BIN}")
    return True


def install_node(status: Status, log: Log) -> bool:
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    releases = json.loads(_fetch_text(NODE_DIST_URL + "index.json"))
    release = next((r for r in releases if r.get("lts") and f"win-{arch}-zip" in r.get("files", [])), None)
    if release is None:
        log("    ! could not find a Node.js LTS release for Windows on nodejs.org")
        return False
    version = release["version"]
    name = f"node-{version}-win-{arch}"
    url = f"{NODE_DIST_URL}{version}/{name}.zip"
    NODE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TOOLS_DIR) as tmp:
        archive = Path(tmp) / f"{name}.zip"
        log(f"    downloading Node.js {version} (LTS) from nodejs.org")
        _download(url, archive, log, "Node.js")
        sums = _fetch_text(f"{NODE_DIST_URL}{version}/SHASUMS256.txt")
        expected = next((ln.split()[0] for ln in sums.splitlines() if ln.endswith(f"{name}.zip")), "")
        if not expected or _sha256(archive) != expected.lower():
            log("    ! checksum does not match nodejs.org's SHASUMS256.txt; not installed")
            return False
        log("    checksum verified")
        with zipfile.ZipFile(archive) as zf, zf.open(f"{name}/node.exe") as src, \
                open(NODE_DIR / "node.exe", "wb") as dst:
            shutil.copyfileobj(src, dst)
    log(f"    installed into {NODE_DIR}")
    return True


def install_audfprint(status: Status, log: Log, bat_dir: Path | str = APP_DIR) -> bool:
    folder = Path(bat_dir) / "audfprint"
    staging = Path(bat_dir) / "audfprint.download"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    log(f"    downloading WerZatSong's audfprint from {WERZATSONG_AUDFPRINT_URL}")
    try:
        for filename in AUDFPRINT_FILES:
            _download(_WERZATSONG_RAW + filename, staging / filename, log, filename)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if folder.exists():
        backup = Path(bat_dir) / "audfprint.old"
        n = 2
        while backup.exists():
            backup = Path(bat_dir) / f"audfprint.old{n}"
            n += 1
        folder.rename(backup)
        log(f"    previous folder kept as {backup.name}")
    staging.rename(folder)
    log(f"    installed into {folder}")
    return True


def install(statuses: list[Status], log: Log, bat_dir: Path | str = APP_DIR) -> list[Status]:
    """Install or repair everything in `statuses` that is not ok and can be
    fixed from here, then check again and return the new statuses."""
    installers = {
        "packages": install_packages,
        "yt-dlp": install_ytdlp,
        "ffmpeg": install_ffmpeg,
        "node": install_node,
        "audfprint": lambda s, lg: install_audfprint(s, lg, bat_dir),
    }
    # Packages first: audfprint's own check runs it, which needs them.
    for status in statuses:
        if status.key == "audfprint" and not status.ok and not status.fix:
            # It was only failing for want of packages; see whether it now runs.
            status = check_audfprint(bat_dir)
        if status.ok or not status.fix or status.key not in installers:
            continue
        log(f"[*] Installing {status.name}: {status.fix}")
        try:
            done = installers[status.key](status, log)
        except Exception as e:  # noqa: BLE001 - network, disk, archive errors
            done = False
            log(f"    ! {type(e).__name__}: {e}")
        if not done:
            log(f"[X] {status.name} was not installed. By hand: {status.manual}")
    add_tools_to_path()
    return check_all(bat_dir)


def describe(status: Status) -> str:
    mark = "+" if status.ok else "!"
    text = f"{mark} {status.name}: {status.detail}" if status.ok else \
        f"{mark} {status.name}: {status.state}, {status.detail}"
    return text


# ----------------------------------------------------------------------------
# console entry point (setup.bat)
# ----------------------------------------------------------------------------

ICON_FILE = APP_DIR / "fingerprinter.ico"
SHORTCUT_FILE = APP_DIR / "Fingerprinter.lnk"
SCRIPT_FILE = APP_DIR / "yt-fingerprinter.pyw"
# The program's AppUserModelID. The running program takes it (main() in
# yt-fingerprinter.pyw) so its taskbar button has its own icon rather than
# Python's; the shortcut carries it too, so that a pinned shortcut and the
# window it opens are one taskbar button, not two.
APP_ID = "EierkuchenHD.Fingerprinter"

# Makes a shortcut through the shell's own IShellLinkW, with the app ID set on
# it before it is saved. WScript.Shell, used before, cannot store characters
# outside the ANSI code page: a folder like C:\Users\Андрей gave a shortcut
# that started nothing.
_SHORTCUT_CS = """
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
public static class FpShortcut {
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    class ShellLink {}
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder file, int size, IntPtr data, int flags);
        void GetIDList(out IntPtr idList);
        void SetIDList(IntPtr idList);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder name, int size);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string name);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder dir, int size);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string dir);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder args, int size);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
        void GetHotkey(out short hotkey);
        void SetHotkey(short hotkey);
        void GetShowCmd(out int showCmd);
        void SetShowCmd(int showCmd);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, int reserved);
        void Resolve(IntPtr hwnd, int flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string file);
    }
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PropertyKey { public Guid Fmtid; public uint Pid; }
    [StructLayout(LayoutKind.Explicit)]
    public struct PropVariant {
        [FieldOffset(0)] public ushort Vt;
        [FieldOffset(8)] public IntPtr Value;
        [FieldOffset(16)] public IntPtr Unused;
    }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore {
        void GetCount(out uint count);
        void GetAt(uint index, out PropertyKey key);
        void GetValue(ref PropertyKey key, out PropVariant value);
        void SetValue(ref PropertyKey key, ref PropVariant value);
        void Commit();
    }
    public static void Make(string lnk, string target, string arguments, string dir,
                            string icon, string appId) {
        IShellLinkW link = (IShellLinkW)new ShellLink();
        link.SetPath(target);
        link.SetArguments(arguments);
        link.SetWorkingDirectory(dir);
        link.SetIconLocation(icon, 0);
        link.SetDescription("Fingerprinter");
        IPropertyStore store = (IPropertyStore)link;
        PropertyKey key = new PropertyKey();
        key.Fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");      // System.AppUserModel.ID
        key.Pid = 5;
        PropVariant value = new PropVariant();
        value.Vt = 31;                                                      // VT_LPWSTR
        value.Value = Marshal.StringToCoTaskMemUni(appId);
        try {
            store.SetValue(ref key, ref value);
            store.Commit();
        } finally {
            Marshal.FreeCoTaskMem(value.Value);
        }
        ((IPersistFile)link).Save(lnk, true);
        Marshal.ReleaseComObject(link);
    }
}
"""


def _same_path(a: Path | str, b: Path | str) -> bool:
    """The same file or folder, however either is spelled (8.3 names, a
    junction, a mapped drive); for paths that do not exist, the same name."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return Path(a) == Path(b)


def _shortcut_target() -> Path:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return pythonw if pythonw.is_file() else Path(sys.executable)


def _shortcut_arguments(data: bytes) -> str | None:
    """A shortcut's arguments, read from the .lnk file itself (the layout is
    Microsoft's MS-SHLLINK), so looking needs no PowerShell. None if it is
    not a shortcut this can read."""
    try:
        if data[:4] != b"\x4c\x00\x00\x00":
            return None
        flags = int.from_bytes(data[20:24], "little")
        pos = 76                                                # the header
        if flags & 0x01:                                        # a target ID list
            pos += 2 + int.from_bytes(data[pos:pos + 2], "little")
        if flags & 0x02:                                        # link info
            pos += int.from_bytes(data[pos:pos + 4], "little")
        wide = bool(flags & 0x80)
        for bit in (0x04, 0x08, 0x10, 0x20):                    # name, relative path, folder, arguments
            if flags & bit:
                count = int.from_bytes(data[pos:pos + 2], "little")
                size = count * 2 if wide else count
                text = data[pos + 2:pos + 2 + size]
                pos += 2 + size
                if bit == 0x20:
                    return text.decode("utf-16-le" if wide else "mbcs", errors="replace")
        return ""
    except (IndexError, ValueError, LookupError):
        return None


def _shortcut_script(path: Path) -> str:
    """The program a shortcut starts: its argument, unquoted. "" if unknown."""
    try:
        args = _shortcut_arguments(path.read_bytes())
    except OSError:
        return ""
    return (args or "").strip().strip('"')


def _points_here(path: Path) -> bool:
    """Whether the shortcut at `path` starts this copy of the program."""
    script = _shortcut_script(path)
    return bool(script) and _same_path(script, SCRIPT_FILE)


def shortcut_is_current(path: Path | None = None) -> bool:
    """Whether the shortcut at `path` (Fingerprinter.lnk in the program folder
    by default) exists and was made for this folder, this Python and the app
    ID. A .lnk holds absolute paths, so one that came along when the folder
    was moved or copied, or that outlived the Python it starts, would start
    the old copy or nothing at all."""
    lnk = path or SHORTCUT_FILE
    try:
        data = lnk.read_bytes()
    except OSError:
        return False
    if not _points_here(lnk):
        return False
    lowered = data.lower()

    def holds(text: str) -> bool:
        # As UTF-16, or in the ANSI code page, which is how a shortcut may
        # store its target. Both sides lowered the same way (ASCII only).
        forms = [text.encode("utf-16-le")]
        try:
            forms.append(text.encode("mbcs"))
        except (UnicodeEncodeError, LookupError):
            pass
        return any(form.lower() in lowered for form in forms)

    return holds(str(_shortcut_target())) and holds(APP_ID)


def make_shortcut(log: Log = print, path: Path | None = None) -> bool:
    """Fingerprinter.lnk in the program folder, or the shortcut at `path`:
    starts the program without a console window, and shows the fingerprint
    icon, which the .pyw file itself cannot (Windows gives every .pyw file
    Python's icon). Made with the shell's own shortcut object from a few lines
    of C# in PowerShell (_SHORTCUT_CS), with the app ID for pinning; paths go
    in as environment variables so no quoting can break them. Checked once
    saved: False, with the reason logged, unless it starts this copy."""
    lnk = path or SHORTCUT_FILE
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "try { Add-Type -TypeDefinition $env:FP_CS; "
        "[FpShortcut]::Make($env:FP_LNK, $env:FP_TARGET, '\"' + $env:FP_SCRIPT + '\"', "
        "$env:FP_DIR, $env:FP_ICON, $env:FP_APPID) "
        "} catch { $e = $_.Exception; if ($e.InnerException) { $e = $e.InnerException }; "
        "[Console]::Error.WriteLine($e.Message); exit 1 }"
    )
    env = dict(os.environ, FP_LNK=str(lnk), FP_TARGET=str(_shortcut_target()),
               FP_SCRIPT=str(SCRIPT_FILE), FP_DIR=str(APP_DIR), FP_ICON=str(ICON_FILE),
               FP_CS=_SHORTCUT_CS, FP_APPID=APP_ID)
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=120, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"    ! Could not create the shortcut: {e}")
        return False
    if proc.returncode != 0 or not lnk.is_file():
        log(f"    ! Could not create the shortcut {lnk}: {_last_line(proc.stderr or proc.stdout)}")
        return False
    if not shortcut_is_current(lnk):
        log(f"    ! Made {lnk}, but it does not start this copy of the program.")
        return False
    return True


# What setup did outside the program folder, one "kind value" line each, so
# uninstall.bat can offer to undo exactly that and no more: "python <winget id>
# <scope>" when setup.bat installed Python, "package <name> <python.exe>" for
# each Python package it installed because it was missing, "desktop <path>"
# or "desktop no" for the desktop shortcut, and "folder <path>", the program
# folder it was written in: a copied folder takes the record along, and must
# not then claim what setup did for the original. setup.bat writes the Python
# line itself.
RECORD_FILE = APP_DIR / "installed-by-setup.txt"


def recorded_lines() -> list[str]:
    try:
        text = RECORD_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def recorded(kind: str) -> list[str]:
    """The values of the record's lines of one kind, oldest first."""
    return [line.split(" ", 1)[1].strip() for line in recorded_lines()
            if " " in line and line.split(" ", 1)[0] == kind]


def _append_record(lines: list[str]) -> None:
    try:
        with open(RECORD_FILE, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
    except OSError:
        pass


def record(kind: str, value: str) -> None:
    lines = recorded_lines()
    new = [] if recorded("folder") else [f"folder {APP_DIR}"]
    if f"{kind} {value}" not in lines:
        new.append(f"{kind} {value}")
    if new:
        _append_record(new)


def _stamp_record() -> None:
    """A record setup.bat started (its Python line) gets this folder's name."""
    if RECORD_FILE.exists() and not recorded("folder"):
        _append_record([f"folder {APP_DIR}"])


def _record_is_ours() -> bool:
    """Whether the record describes this copy: written in this folder, or in
    one that is gone (the folder was moved). A copy whose original still
    exists has the original's record, and what it lists belongs there."""
    folders = recorded("folder")
    if not folders or any(_same_path(folder, APP_DIR) for folder in folders):
        return True
    if not any(Path(folder).exists() for folder in folders):
        _append_record([f"folder {APP_DIR}"])           # moved: from now on, this is where it is
        return True
    return False


def _record_packages(names: list[str]) -> None:
    for name in names:
        record("package", f"{name} {console_python()}")


def _recorded_packages() -> dict[str, list[str]]:
    """{python.exe: [packages setup installed into it]}, for those still there."""
    groups: dict[str, list[str]] = {}
    for value in recorded("package"):
        name, _, python = value.partition(" ")
        if python and Path(python).is_file() and name not in groups.get(python, []):
            groups.setdefault(python, []).append(name)
    return groups


# The desktop, as Windows knows it (FOLDERID_Desktop).
_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"


def desktop_dir() -> Path | None:
    """The desktop folder Windows shows, which OneDrive backup or a group
    policy often moves away from %USERPROFILE%\\Desktop. FINGERPRINTER_DESKTOP
    stands in for it, for tests."""
    override = os.environ.get("FINGERPRINTER_DESKTOP")
    if override:
        return Path(override)
    try:
        import ctypes

        class Guid(ctypes.Structure):
            _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                        ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

        guid = Guid()
        ctypes.oledll.ole32.CLSIDFromString(_FOLDERID_DESKTOP, ctypes.byref(guid))
        found = ctypes.c_wchar_p()
        ctypes.oledll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(found))
        try:
            return Path(found.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(found)
    except Exception:  # noqa: BLE001 - no desktop to be found
        return None


def _desktop_shortcuts_here() -> list[Path]:
    """Every shortcut on the desktop that starts this copy, whatever it is
    called: the one setup made, or one copied there by hand, as the README of
    1.0.0-beta.4 suggested."""
    desktop = desktop_dir()
    if desktop is None or not desktop.is_dir():
        return []
    return [path for path in sorted(desktop.glob("*.lnk")) if _points_here(path)]


def _free_to_use(path: Path) -> bool:
    """Whether a desktop shortcut may be written at `path`: nothing there, or
    one for this copy, or one whose program is gone. Another copy's working
    shortcut is not overwritten."""
    if not path.exists() or _points_here(path):
        return True
    script = _shortcut_script(path)
    return bool(script) and not Path(script).exists()


def make_desktop_shortcut(log: Log = print) -> Path | None:
    """A shortcut to this copy on the desktop: the one there already, made
    again, or Fingerprinter.lnk (Fingerprinter 2.lnk and so on beside another
    copy's). Its path, or None."""
    desktop = desktop_dir()
    if desktop is None or not desktop.is_dir():
        log("    ! Could not find the desktop folder.")
        return None
    existing = _desktop_shortcuts_here()
    path = existing[0] if existing else desktop / SHORTCUT_FILE.name
    n = 2
    while not _free_to_use(path):
        path = desktop / f"Fingerprinter {n}.lnk"
        n += 1
    if not make_shortcut(log, path):
        return None
    record("desktop", str(path))
    return path


def repair_desktop_shortcut() -> Path | None:
    """The desktop shortcut this copy made, made again when it no longer fits:
    the program folder was moved, so it starts a place that is gone, or the
    Python it starts changed. One that starts another copy that still exists
    is left to that copy. Reads the shortcut itself, so a shortcut that fits
    costs no PowerShell. The path if remade."""
    if not _record_is_ours():
        return None
    for value in recorded("desktop"):
        path = Path(value)
        if value == "no" or not path.is_file() or shortcut_is_current(path):
            continue
        script = _shortcut_script(path)
        if script and (_same_path(script, SCRIPT_FILE) or not Path(script).exists()):
            if make_shortcut(lambda _m: None, path):
                return path
    return None


def _ask(question: str) -> str | None:
    """The answer, or None when there is no more input (end of file)."""
    try:
        return input(question).strip()
    except EOFError:
        return None


def _yes(question: str) -> bool:
    return (_ask(question) or "").lower() in ("y", "yes")


def offer_desktop_shortcut() -> None:
    """setup.bat, at the end: ask once whether to put a shortcut on the
    desktop. One this copy has there already (made by setup, or copied there
    by hand) is kept up to date instead, and a No is remembered; Settings,
    General can make one later."""
    ours = _record_is_ours()
    if ours and any(value != "no" for value in recorded("desktop")):
        repair_desktop_shortcut()
        return
    found = _desktop_shortcuts_here()
    if found:
        for path in found:
            record("desktop", str(path))
            if not shortcut_is_current(path):
                make_shortcut(print, path)
        return
    if ours and "no" in recorded("desktop"):
        return
    if _yes("\nPut a Fingerprinter shortcut on your desktop? [y/N] "):
        path = make_desktop_shortcut()
        if path:
            print(f"Made {path}.")
    else:
        record("desktop", "no")
        print("No desktop shortcut. Settings, General in the program can make one later.")


# ----------------------------------------------------------------------------
# uninstall (uninstall.bat)
# ----------------------------------------------------------------------------

# What the Fingerprinter keeps in its folder. Only these are ever deleted, so
# anything else someone put there stays; in tools and work only the parts it
# makes, the folder itself only once nothing else is left in it. uninstall.bat
# has the same lists for when Python is gone. The folder names match
# DOWNLOADS_DIR, PKLZ_DIR, KEEP_AUDIO_DIR and WORK_DIR in yt-fingerprinter.pyw.
PROGRAM_FILES = (
    "yt-fingerprinter.pyw", "dependencies.py", "audfprint_quiet.py", "requirements.txt",
    "config.example.json", "README.md", "CHANGELOG.md", "LICENSE", "fingerprinter.ico",
    "setup.bat", SHORTCUT_FILE.name, "config.json", "recent_urls.json",
    "fingerprinted-items.txt", "unfinished-list.json", "unfinished-list.tmp", RECORD_FILE.name,
)
PROGRAM_FOLDERS = ("audfprint", "audfprint.download", "texts", "__pycache__")
PROGRAM_SUBFOLDERS = {"tools": ("ffmpeg", "node"), "work": ("texts",)}
# The user's own files: asked about one by one, and kept unless they say so.
FINGERPRINTS_DIR, KEPT_AUDIO_DIR, WORKING_DIR = "pklz-files", "audio", "downloads"


def _running_copies() -> list[int]:
    """Processes running this copy of the program, however its path was
    spelled: an 8.3 name, a mapped drive, or a relative path from its folder.
    Asked in a separate process, so that psutil is not loaded here when pip
    may be about to remove it."""
    probe = ("import json, psutil\n"
             "out = []\n"
             "for p in psutil.process_iter(['name', 'cmdline', 'cwd']):\n"
             "    if 'python' in (p.info['name'] or '').lower():\n"
             "        out.append([p.pid, p.info['cwd'] or '', p.info['cmdline'] or []])\n"
             "print(json.dumps(out))\n")
    procs = None
    code, out = _run([console_python(), "-c", probe], timeout=60)
    if code == 0:
        try:
            procs = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            procs = None
    if procs is None:
        # No psutil: command lines from Windows, split the way programs do.
        query = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                 "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                 "ForEach-Object { [string]$_.ProcessId + ' ' + $_.CommandLine }")
        _, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", query], timeout=60)
        procs = []
        for line in out.splitlines():
            pid, _, cmdline = line.strip().partition(" ")
            if pid.isdigit():
                procs.append([int(pid), "", _split_command_line(cmdline)])
    pids = []
    for pid, cwd, args in procs:
        for arg in args:
            # Any .pyw: its short name (YT-FIN~1.PYW) does not end in yt-fingerprinter.pyw.
            if str(arg).lower().endswith(".pyw"):
                path = Path(arg)
                if not path.is_absolute():
                    path = Path(cwd) / path if cwd else Path.cwd() / path
                if _same_path(path, SCRIPT_FILE):
                    pids.append(pid)
                    break
    return pids


def _split_command_line(cmdline: str) -> list[str]:
    try:
        import ctypes
        from ctypes import wintypes
        split = ctypes.windll.shell32.CommandLineToArgvW
        split.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        split.restype = ctypes.POINTER(wintypes.LPWSTR)
        count = ctypes.c_int()
        argv = split(cmdline, ctypes.byref(count))
        try:
            return [argv[i] for i in range(count.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(argv)
    except Exception:  # noqa: BLE001
        return cmdline.split()


def _files_in(folder: Path, pattern: str = "*") -> tuple[int, int]:
    """(how many files, how many bytes) under `folder`."""
    count = size = 0
    if folder.is_dir():
        for f in folder.rglob(pattern):
            try:
                if f.is_file():
                    count += 1
                    size += f.stat().st_size
            except OSError:
                pass
    return count, size


def _size_text(size: int) -> str:
    for unit, factor in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if size >= factor:
            return f"{size / factor:.1f} {unit}"
    return f"{size} bytes"


def _outside_folders() -> list[tuple[str, Path]]:
    """Folders chosen in Settings that lie outside the program folder, which
    uninstall leaves alone."""
    try:
        cfg = json.loads((APP_DIR / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found = []
    for key, label in (("move_pklz_dir", "fingerprints"), ("output_dir", "working folder"),
                       ("keep_audio_dir", "kept audio")):
        value = str(cfg.get(key) or "").strip()
        if not value:
            continue
        folder = Path(value)
        try:
            inside = folder.resolve() == APP_DIR.resolve() or APP_DIR.resolve() in folder.resolve().parents
        except OSError:
            inside = False
        if not inside:
            found.append((label, folder))
    return found


def _remove(path: Path, failed: list[str]) -> bool:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True
    except OSError as e:
        failed.append(f"{path.name}: {e.strerror or e}")
        return False


def _remove_empty(folder: Path) -> None:
    """`folder` and the folders in it, as far as they are empty."""
    if not folder.is_dir():
        return
    for sub in sorted((p for p in folder.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            sub.rmdir()
        except OSError:
            pass
    try:
        folder.rmdir()
    except OSError:
        pass


def uninstall(then_file: Path | None) -> int:
    """uninstall.bat: remove the Fingerprinter from its folder, and, when the
    user says so, their fingerprints and audio, and the Python packages and
    Python that setup installed for it (only those the record lists, and only
    when the record is this copy's). Python itself is removed by uninstall.bat
    once this has exited, from the command written to `then_file`. Returns 0
    when done, 1 when nothing was removed, 2 when some of it could not be."""
    print("Uninstall the Fingerprinter")
    print("=" * 27)
    print(f"Program folder: {APP_DIR}\n")

    # Not while it runs: it would write on, and its window holds the folder.
    while running := _running_copies():
        print(f"The Fingerprinter is open (process {', '.join(map(str, running))}). Close it first.")
        answer = _ask("Press Enter once it is closed, or type q to stop: ")
        if answer is None or answer.lower() == "q":
            print("\nNothing was removed.")
            return 1

    ours = _record_is_ours()
    program = [APP_DIR / name for name in (*PROGRAM_FILES, *PROGRAM_FOLDERS) if (APP_DIR / name).exists()]
    program += sorted(p for p in APP_DIR.iterdir()
                      if p.is_dir() and re.fullmatch(r"audfprint\.old\d*", p.name, re.IGNORECASE))
    for parent, subs in PROGRAM_SUBFOLDERS.items():
        program += [APP_DIR / parent / sub for sub in subs if (APP_DIR / parent / sub).exists()]
    tools = APP_DIR / "tools"
    if tools.is_dir():                              # left by an interrupted install
        program += sorted(p for p in tools.iterdir() if p.is_dir() and p.name.startswith("tmp"))
    desktop = [Path(value) for value in (recorded("desktop") if ours else [])
               if value != "no" and Path(value).is_file() and _points_here(Path(value))]
    desktop += [path for path in _desktop_shortcuts_here() if path not in desktop]
    print("This removes the program and everything it installed in its folder:")
    print("  audfprint, ffmpeg and Node.js (tools), its settings, list and scratch files.")
    for path in desktop:
        print(f"It also removes the shortcut on your desktop: {path}")
    for label, folder in _outside_folders():
        print(f"Your {label} folder chosen in Settings, {folder}, is left as it is.")
    if not ours and (recorded("package") or recorded("python")):
        print(f"{RECORD_FILE.name} came from {recorded('folder')[0]}, which is still there: what setup "
              f"installed belongs to that copy, so it is not offered here.")
    print()

    keep: list[str] = []
    delete: list[Path] = []
    fingerprints = APP_DIR / FINGERPRINTS_DIR
    unfinished = sorted((APP_DIR / "work" / "pklz").glob("*.pklz"))
    delete_fingerprints = False
    n, size = _files_in(fingerprints, "*.pklz")
    others, _ = _files_in(fingerprints)
    others -= n
    if n or unfinished:
        extra = f", and work\\pklz {len(unfinished)} unfinished one(s)" if unfinished else ""
        print(f"Your fingerprints: {FINGERPRINTS_DIR} holds {n} .pklz file(s), {_size_text(size)}{extra}.")
        if others:
            print(f"It also holds {others} other file(s); those stay either way.")
        print("Making them again takes hours.")
        if _yes("Delete them too? [y/N] ") and \
                _ask("Type DELETE to confirm, or press Enter to keep them: ") == "DELETE":
            delete_fingerprints = True
        else:
            keep.append(f"your fingerprints in {fingerprints}")
        print()
    for name, what, question, kept in (
            (KEPT_AUDIO_DIR, "Kept audio", "Delete it too? [y/N] ", "your kept audio in"),
            (WORKING_DIR, "The working folder", "Delete what is in it too? [y/N] ", "the working folder")):
        folder = APP_DIR / name
        n, size = _files_in(folder)
        if n:
            print(f"{what}: {name} holds {n} file(s), {_size_text(size)}.")
            if _yes(question):
                delete.append(folder)
            else:
                keep.append(f"{kept} {folder}")
            print()
        elif folder.is_dir():
            delete.append(folder)

    packages = _recorded_packages() if ours else {}
    remove_packages = False
    if packages:
        names = sorted({name for group in packages.values() for name in group})
        print(f"setup.bat installed these Python packages for the Fingerprinter: {', '.join(names)}.")
        print("Other programs on this PC may use them too.")
        remove_packages = _yes("Remove them? [y/N] ")
        print()
    python = next((value.split() for value in (recorded("python") if ours else [])
                   if len(value.split()) == 2), None)
    remove_python = False
    if python:
        scope = "for all users" if python[1] == "machine" else "for this user"
        print(f"setup.bat installed Python ({python[0]}, {scope}) with winget. Other programs may use it.")
        remove_python = _yes("Remove it? [y/N] ")
        print()

    print("Ready to remove the Fingerprinter" + (", its desktop shortcut" if desktop else "")
          + (", the Python packages" if remove_packages else "") + (" and Python" if remove_python else "")
          + ".")
    for item in keep:
        print(f"  Kept: {item}")
    if not _yes("Go ahead? [y/N] "):
        print("\nNothing was removed.")
        return 1
    print()

    failed: list[str] = []
    work_pklz = APP_DIR / "work" / "pklz"
    if delete_fingerprints:
        # Only the fingerprints themselves: other files in the folder stay.
        for f in [*sorted(fingerprints.rglob("*.pklz")), *unfinished]:
            _remove(f, failed)
        _remove_empty(fingerprints)
    elif unfinished:
        # Finished batches of an interrupted run are fingerprints too: kept
        # with the rest, as the program itself keeps them (recovered-...).
        fingerprints.mkdir(exist_ok=True)
        for src in unfinished:
            target = fingerprints / f"recovered-{src.name}"
            n = 2
            while target.exists():
                target = fingerprints / f"recovered-{src.stem}_{n}{src.suffix}"
                n += 1
            try:
                shutil.move(str(src), str(target))
                print(f"Kept {src.name} from work\\pklz as {target.name}.")
            except OSError as e:
                failed.append(f"{src.name}, an unfinished fingerprint file: it could not be moved "
                              f"out of work\\pklz ({e.strerror or e}), so work stays")
    if not any(work_pklz.glob("*.pklz")):
        program.append(work_pklz)
    for path in desktop:
        if _remove(path, failed):
            print(f"Removed the desktop shortcut, {path}")
    removed = [str(path.relative_to(APP_DIR)) for path in (*program, *delete)
               if path.exists() and _remove(path, failed)]
    for parent in PROGRAM_SUBFOLDERS:           # tools and work, once empty
        try:
            (APP_DIR / parent).rmdir()
        except OSError:
            pass
    if removed:
        print(f"Removed from the program folder: {', '.join(removed)}")
    if remove_packages:
        for python_exe, names in packages.items():
            print(f"\nRemoving {', '.join(names)} from {python_exe}...")
            code, out = _run([python_exe, "-m", "pip", "uninstall", "-y", *names], timeout=600)
            for line in out.splitlines():
                if line.startswith(("Successfully", "WARNING", "ERROR", "Found existing")):
                    print(f"  {line.strip()}")
            if code != 0:
                failed.append(f"Python packages ({', '.join(names)}): pip reported an error, see above")
    if remove_python and then_file:
        try:
            then_file.write_text(
                f"@echo.\r\n@echo Removing Python with winget...\r\n"
                f"winget uninstall --exact --id {python[0]} --scope {python[1]}\r\n", encoding="ascii")
        except (OSError, UnicodeEncodeError) as e:
            failed.append(f"Python: {e}")

    print()
    for item in keep:
        print(f"Kept: {item}")
    others = sorted(p.name for p in APP_DIR.iterdir()
                    if p.name.lower() != "uninstall.bat"
                    and p.name not in (FINGERPRINTS_DIR, KEPT_AUDIO_DIR, WORKING_DIR)) \
        if APP_DIR.is_dir() else []
    if others:
        print(f"Left as they are, as they did not come with the Fingerprinter: {', '.join(others[:10])}"
              + (" ..." if len(others) > 10 else ""))
    for item in failed:
        print(f"! Could not remove {item}")
    return 2 if failed else 0


def main(argv: list[str]) -> int:
    if sys.platform != "win32":
        print("The Fingerprinter runs on Windows only.")
        return 1
    _stamp_record()
    if "--uninstall" in argv:
        i = argv.index("--uninstall")
        return uninstall(Path(argv[i + 1]) if i + 1 < len(argv) else None)
    check_only = "--check" in argv
    if "--shortcut" in argv and make_shortcut():
        # setup.bat asks for this: the program's shortcut, with its icon.
        print(f"Made {SHORTCUT_FILE.name} in the program folder: start the Fingerprinter from it.\n")
    if "--update-ytdlp" in argv:
        # setup.bat asks for this every time it runs (see update_ytdlp).
        update_ytdlp(print)
        print()
    result = _check_and_install(check_only)
    if "--shortcut" in argv and result == 0:
        offer_desktop_shortcut()
    return result


def _check_and_install(check_only: bool) -> int:
    print("Checking what the Fingerprinter needs...\n")
    statuses = check_all(APP_DIR)
    for s in statuses:
        print("  " + describe(s))
    problems = [s for s in statuses if not s.ok]
    if not problems:
        print("\nEverything is installed and working.")
        return 0
    fixable = [s for s in problems if s.fix]
    manual = [s for s in problems if not s.fix]
    if check_only:
        return 1
    for s in manual:
        print(f"\n{s.name} has to be fixed by hand: {s.manual}")
    if not fixable:
        return 1
    print("\nThis would install or repair:")
    for s in fixable:
        print(f"  - {s.name} ({s.why}): {s.fix}")
    try:
        answer = input("\nInstall now? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        print("Nothing was installed.")
        return 1
    print()
    statuses = install(statuses, print, APP_DIR)
    print()
    for s in statuses:
        print("  " + describe(s))
    left = [s for s in statuses if not s.ok]
    print("\nEverything is installed and working." if not left else
          f"\n{len(left)} item(s) still need attention; see above.")
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
