"""Run audfprint with every console window it spawns suppressed.

audfprint shells out to ffmpeg once per file it reads. We already start
audfprint itself with CREATE_NO_WINDOW, and that is precisely why the flashing
happens: a process with no console of its own cannot lend one to its children,
so Windows hands each console child - ffmpeg included - a brand new console
window. That is the black box that blinks on screen once per file.

The flag has to be set on the ffmpeg spawn itself, which happens inside
audfprint's audio_read.py. Editing that file works but is the wrong place to
put the fix: audfprint is a third-party checkout that each user downloads
themselves, so a hand edit is easy to get wrong, easy to forget, and lost the
moment anyone updates it.

So instead this wrapper installs the flag process-wide, by making every
subprocess.Popen in this interpreter default to a hidden window, and then runs
audfprint unmodified. Stock audfprint, no windows.

Usage (argv[1] is the audfprint script, the rest is passed through untouched):

    python -u audfprint_quiet.py <path-to-audfprint.py> new --dbase ... --list ...
"""
from __future__ import annotations

import os
import runpy
import subprocess
import sys

if sys.platform == "win32":
    CREATE_NO_WINDOW = 0x08000000

    class _QuietPopen(subprocess.Popen):
        """Popen that always asks Windows for an invisible child.

        Both halves are needed. CREATE_NO_WINDOW covers console programs, and
        STARTF_USESHOWWINDOW/SW_HIDE covers anything that would otherwise show
        a top-level window. Existing flags are preserved rather than replaced,
        so a caller that passes its own creationflags still gets them.
        """

        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
            startupinfo = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0            # SW_HIDE
            kwargs["startupinfo"] = startupinfo
            super().__init__(*args, **kwargs)

    subprocess.Popen = _QuietPopen

if len(sys.argv) < 2:
    sys.exit("usage: audfprint_quiet.py <path-to-audfprint.py> [audfprint args...]")

script = sys.argv.pop(1)

# Reproduce what `python audfprint.py ...` would have set up, so audfprint
# cannot tell the difference:
#   - argv[0] is the script itself, and docopt sees the same argv[1:]
#   - the script's own directory leads sys.path, which is how audfprint finds
#     its siblings (audio_read, hash_table, ...). runpy.run_path does not do
#     this for a plain file the way direct execution does, and without it
#     audfprint dies on `import audio_read`.
sys.argv[0] = script
sys.path.insert(0, os.path.dirname(os.path.abspath(script)))

runpy.run_path(script, run_name="__main__")
