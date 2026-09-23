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

    A yt-dlp.exe on PATH wins if it works. Otherwise the copy pip installed into
    this Python, which works even when Python's Scripts folder is not on PATH."""
    exe = shutil.which("yt-dlp")
    if exe:
        code, out = _run([exe, "--version"], timeout=30)
        if code == 0:
            return [exe], _last_line(out)
    module = [console_python(), "-m", "yt_dlp"]
    code, out = _run([*module, "--version"], timeout=30)
    if code == 0:
        return module, _last_line(out)
    return None, ""


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
        ok = _pip(missing, log) and ok
    if broken:
        ok = _pip(["--force-reinstall", *broken], log) and ok
    return ok


def install_ytdlp(status: Status, log: Log) -> bool:
    return _pip(["--force-reinstall", "yt-dlp"] if status.state == "not working" else ["yt-dlp"], log)


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

def main(argv: list[str]) -> int:
    if sys.platform != "win32":
        print("The Fingerprinter runs on Windows only.")
        return 1
    check_only = "--check" in argv
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
