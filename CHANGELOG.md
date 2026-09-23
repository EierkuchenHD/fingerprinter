# Changelog

Versions follow [semantic versioning](https://semver.org/). Pre-releases
(`-beta.N`) are for testing and may still change.

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

[1.0.0-beta.1]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.1
