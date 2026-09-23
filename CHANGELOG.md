# Changelog

Versions follow [semantic versioning](https://semver.org/). Pre-releases
(`-beta.N`) are for testing and may still change.

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

[1.0.0-beta.2]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.2
[1.0.0-beta.1]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.1
