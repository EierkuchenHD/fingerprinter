# Changelog

Versions follow [semantic versioning](https://semver.org/). Pre-releases
(`-beta.N`) are for testing and may still change.

## [1.0.0-beta.5] - 2026-09-25

### Added

- **`uninstall.bat`** removes the Fingerprinter: the program and everything it
  put in its folder (audfprint, ffmpeg and Node.js, settings, scratch files)
  and its shortcuts, also a desktop one copied there by hand. Your
  fingerprints, kept audio and working folder are asked about one by one and
  kept unless you say otherwise; deleting your fingerprints needs a second
  confirmation and removes only the `.pklz` files. The Python packages and the
  Python that setup installed are offered too, only those, each from the
  Python it went into, and only removed on a yes. Files that did not come with
  the Fingerprinter are never touched, and it works on a network share and
  without Python too.
- `setup.bat` notes what it installs outside the program folder in
  `installed-by-setup.txt`, for the uninstaller.
- **Remove links from the list once fingerprinted** (Settings, General), off
  by default. Links that failed, were skipped or stopped, or where some
  downloads or batches failed in a way that may work next time, stay in the
  list.
- **A desktop shortcut:** `setup.bat` asks once whether to put one on the
  desktop, and **Put a shortcut on the desktop** in Settings, General makes
  one at any time. It goes on the desktop Windows shows, also when OneDrive
  has moved it, is updated when the program folder moves, and leaves another
  copy's shortcut there alone.

### Changed

- A link where some batches failed, or some downloads failed in a way that
  may work next time (a network error, a rate limit, a premiere not out yet),
  ends as "incomplete" in the list's summary, rather than as done.
- Shortcuts are made through Windows' own shortcut object instead of
  WScript.Shell, which could not hold a folder name outside the system's
  code page (a Cyrillic user name on English Windows, for example): such a
  shortcut started nothing.
- `setup.bat` also works from a folder on a network share.

## [1.0.0-beta.4] - 2026-09-24

### Added

- **Keep downloaded audio** (Settings, Folders), off by default: a copy of
  each link's downloads is kept in a folder named after the channel, as they
  were downloaded, before splitting. On the same drive it is a hard link, so
  it takes no extra space or time; the working copy is still fingerprinted and
  deleted as before.
- **Continuing an unfinished list.** When a list stops part-way (Stop, a
  crash, Windows restarting), the program offers to carry on at its next start,
  or when **Download and fingerprint** is pressed. Links that finished are not
  run again. The link it stopped in starts over, and its unfinished downloads
  and `.pklz` files are thrown away, so nothing is fingerprinted twice.
- A **General** tab in Settings, the first one, which Settings opens on:
  - **Appearance**: Light, Dark, or Follow Windows, which switches along with
    Windows' own setting while the program runs.
  - **Console text size**, also Ctrl and the mouse wheel over the console.
  - **Keep the PC awake while a job runs**, on by default. The screen can
    still turn off.
  - **Ask before Stop**, on by default.
  - **Offer to continue an unfinished list**, on by default.
- A fingerprint icon for the window and its taskbar button, and a
  **Fingerprinter** shortcut with that icon in the program folder. `setup.bat`
  makes the shortcut, and the program makes it again at start when it is
  missing or no longer fits: the folder was moved or copied, or the Python it
  started is gone. Pinned to the taskbar, the shortcut or the running window
  is one button that starts the Fingerprinter. The icon is based on
  [Fingerprint](https://www.flaticon.com/free-icon/fingerprint_2313362) by
  Pixel perfect from Flaticon.
- **Follow** on the console, on by default. Untick it to read back through
  the console while new lines keep coming.
- **Write fingerprinted.json while fingerprinting** (Settings,
  Fingerprinting), off by default. See Changed.

### Changed

- `fingerprinted.json`, the record of finished batches in `work\pklz`, is no
  longer written unless that new setting is on. The record is kept in memory
  instead, so **Audio on disk** still carries on after Stop while the program
  stays open; only carrying on after the program was closed or crashed needs
  the file.
- **Add** is only clickable while the Link box holds something.
- `setup.bat` updates yt-dlp to its newest release every time it runs,
  whatever version is installed. **Check setup** does the same with pip; the
  `yt-dlp -U` it ran before only updates the standalone exe, not the copy pip
  installed. The program uses that copy, or a `yt-dlp.exe` found on PATH if
  that one is newer; before, the exe always won, however old.

### Removed

- **This program's folder** (Settings, Folders). The program always works from
  the folder it is in, where setup puts `audfprint\` and `tools\`. If it pointed
  somewhere else, the working, fingerprints and kept-audio folders that
  followed it stay where they were, now set in Settings, Folders, and the
  console says so once.

### Fixed

- `setup.bat` ignored a failed Python installation and said "Python is
  installed, but this window cannot see it yet". It now checks winget's result
  and says what went wrong. When a Windows policy blocks the installation
  ("Organization policies are preventing installation", Windows Installer code
  1625), it explains that, shows any Windows Installer policy set on the PC,
  and offers to install for all users, get Python from the Microsoft Store, or
  open python.org. A cancelled install, a missing connection, a full disk and
  a pending restart each get their own message. A policy set to allow
  installs is no longer reported as the cause.
- `setup.bat` also finds a Python installed for all users before the window's
  PATH knows about it, and starts the program even without `pythonw`.

## [1.0.0-beta.3] - 2026-09-23

### Added

- **Skip items already fingerprinted in an earlier run** (Settings,
  Downloads), with **Forget them**. Running a channel again then only fetches
  its new uploads. Items are remembered in `fingerprinted-items.txt`, in the
  format of yt-dlp's `--download-archive`, and only once their whole link has
  been fingerprinted.
- **Skip items shorter than / longer than** (Settings, Downloads): leave out
  items by length, for example YouTube Shorts under 60 seconds. Items whose
  length the listing does not give are checked by yt-dlp before downloading.
- **Sign in with cookies from** a browser (Settings, Downloads), instead of
  typing `--cookies-from-browser` into Extra download options.
- **Play a sound and flash the taskbar button when a run finishes**
  (Settings, Fingerprinting), on by default.

### Changed

- **Fingerprint jobs at once now speeds up a single batch too.** A link with
  fewer recordings than Recordings per file is one batch, and it used to run
  as one process however many jobs were allowed. Its files are now split
  across the jobs (4 by default), fingerprinted side by side, and merged into
  the same single `.pklz`, which holds the same fingerprints and matches the
  same way. 48 six-minute files took 2 minutes instead of 3; a full
  1000-file batch should take about 14 minutes instead of 52. Batches under
  40 files are not split, as merging costs about a minute.
- **Show every line of download output** is off by default, explains itself
  in Settings, and can't be changed while a job runs. Settings files from
  earlier versions saved it as on because that was the old default, so it is
  switched off once.

### Fixed

- Titles with ä, ö, ü, ß and other letters outside English showed them as "?"
  in the Now panel and the console. yt-dlp now writes UTF-8.
- The console no longer mentions audfprint's `--ncores` setting.

## [1.0.0-beta.2] - 2026-09-23

### Added

- **A redesigned window, with everything on one screen.** Two toolbar rows
  hold the link box and the run buttons; the list and a new **Now** panel sit
  side by side under them, and the console runs the full width along the
  bottom. Nothing needs scrolling on a 1366x768 laptop screen, and every
  divider can be dragged (their positions are remembered).
- **Now** shows which link is running and what it is doing, with a progress
  bar, a rough time left, and a row per download or fingerprint batch. It
  replaces the Downloading now panel, which sat empty while fingerprinting.
- **Settings** window with Folders, Downloads and Fingerprinting tabs. It
  replaces Advanced settings and the three folder rows that were always on
  screen. The status bar shows where fingerprints go; click it to open the
  folder.
- Explanations are tooltips: hover over a control to see what it does. An
  empty list says what to paste instead.
- **Pause** and **Resume.** Running downloads, splitting and fingerprinting
  are suspended where they are and continue from the same point; nothing new
  starts while paused.
- **A more useful list.** Drag a row by its `≡` handle to reorder it, click a
  link to open it, click **✕** to remove it, and right-click a row to open or
  copy the link, count again, move it to the top or bottom, or remove it. The
  header counts the links and ticks or unticks them all.
- Each link shows how many videos the channel or playlist has, counted in the
  background when it is added and refreshed once a day.

### Changed

- **The folders come ready to use.** `downloads\` and `pklz-files\` in the
  program folder are now the default working folder and the default place
  finished fingerprints are kept. They are filled in and created on first
  start, so a new install needs no folder setup.
- `pklz-files\` only ever receives finished fingerprints, and nothing in it is
  deleted. The program's own scratch files (audfprint's file lists and
  unfinished `.pklz` files) moved to `work\`.
- **No more prompt to empty the fingerprints folder.** The program empties its
  own `work\` folder without asking, and moves any `.pklz` an interrupted run
  left there to your fingerprints folder as `recovered-...`.
- The **Audio on disk** menu holds the two ways of fingerprinting audio
  already in the working folder, which used to be two buttons of their own.
  Move up and Move down gave way to dragging and the right-click menu.
- Removed "Include this output when asking for help" from under the console.
- The warning about starting a list with no fingerprints folder is gone:
  finished fingerprints can no longer be left where the next link deletes them.
- Folders left at their defaults are saved that way, so they follow the
  program if its folder is moved or copied.

### Fixed

- Fingerprinting progress lines cut file names off after 60 characters. They
  now show the whole name, and long console lines wrap between words.
- Folder paths are always shown with backslashes. **Browse** returned
  `C:/Users/...` while other paths used backslashes; typed paths and older
  saved settings are tidied too.
- Setting **Keep finished fingerprints in** to `pklz-files` in the program
  folder lost work. audfprint also wrote there, so finished files were renamed
  with `_2` and then deleted before the next link. That folder is now safe,
  and the program refuses any folder inside `work\`.
- In a list, every link after the first stopped at a "folder is not empty"
  prompt about leftover file lists, which waited two minutes before answering
  itself. The scratch files are now cleared once a link's fingerprints have
  been moved out.

Upgrading from 1.0.0-beta.1: an old `texts\` folder can be deleted. Anything
already in `pklz-files\` stays, as finished fingerprints.

## [1.0.0-beta.1] - 2026-09-23

The first numbered version. It is a pre-release: please report anything that
does not work, with the console output from **Check setup**.

### Added

- **`setup.bat`**, a first-run installer. It finds Python 3.10 or newer (and
  offers to install it with winget if there is none), then checks every other
  component and installs what is missing once you confirm.
- **Component check and repair** (`dependencies.py`), used by `setup.bat`, by
  **Check setup** and at every start. It runs each component instead of only
  looking for it on PATH, leaves working ones alone, says what it would install
  and why, asks first, and gives the manual step if an install fails. It covers
  the Python packages, yt-dlp, ffmpeg and ffprobe, Node.js and WerZatSong's
  audfprint.
- ffmpeg and Node.js are installed into `tools\` in the program folder, checked
  against their published SHA-256, with no admin rights or PATH changes.
- yt-dlp also works when it is installed as a Python package but not on PATH.
- The version number is shown in the window title and in the Check setup
  output.

### Changed

- **WerZatSong's audfprint is required, and says so.** The README, the program
  and setup name the exact source
  ([Nel80s/WerZatSong, `libs/audfprint`](https://github.com/Nel80s/WerZatSong/tree/main/libs/audfprint))
  and explain why the original dpwe/audfprint does not work on Windows. An
  upstream copy, a missing one, or one nested a folder too deep is detected
  before a run starts.
- **Larger console.** It gets at least 40% of the window, the divider above it
  can be dragged (its position is remembered), and the controls scroll when
  the window is too small for them. The section is now called Console.
- One mouse-wheel handler for the whole window: it scrolls whatever is under
  the pointer, including the new scrolling controls.
- **Downloads at once** moved from Advanced settings to Step 3. The default is
  8 (was 4) and the maximum 32 (was 16), with a note on what raising it costs.
- Links can come from any site yt-dlp supports, and the wording says so
  (YouTube, Archive.org, Mixcloud, SoundCloud and others).
- The splitting rule is described exactly everywhere: files over 12:00 become
  6:00 pieces, the last piece takes the remainder, and files of 12:00 or less
  stay whole. The rule itself is unchanged.
- Shorter wording throughout, and "list" and "link" in place of "queue" and
  "channel". The Advanced settings hint now suggests checking them before a
  large job, and each setting explains its cost.
- "Check my setup" is now **Check setup**, and offers to install or repair
  what it finds.
- README rewritten: Windows only, requirements, setup, usage, splitting,
  settings, troubleshooting and limitations.

### Fixed

- A value typed into Downloads at once above the maximum is capped at 32.

[1.0.0-beta.5]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.5
[1.0.0-beta.4]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.4
[1.0.0-beta.3]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.3
[1.0.0-beta.2]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.2
[1.0.0-beta.1]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.1
