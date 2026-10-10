# Changelog

Versions follow [semantic versioning](https://semver.org/). Pre-releases
(`-beta.N`) are for testing and may still change.

## [1.1.0] - 2026-10-10

### Added

- **Only items published on or after a date** (Settings, Downloads), for a
  channel's new uploads: older items are left out and listed under
  `N skipped` with the reason. Where the site's list gives no date (YouTube's
  never does), yt-dlp looks each one up just before downloading; the dates of
  those left out are remembered in `published-dates.txt`, so the next run
  does not look them up again. Items with no date at all are downloaded, and
  a run with the box ticked but no proper date is refused.

### Fixed

- **An archive.org item of several files could fail for every file** with
  "no audio format available": archive.org gives each file the item's page as
  its link, so each download fetched the whole item, whose files yt-dlp lists
  with no codec the format choice accepts. Each file now downloads by its own
  link, which is also the link **Copy link and its videos** and the skipped
  list show for it.

## [1.0.0] - 2026-10-10

The first full release.

### Added

- **See which videos were skipped:** a row whose last run left videos out
  shows `N skipped` beside its count. Click it, or right-click the row, for
  which ones and why (outside the length limits, failed to download, or
  could not be read for fingerprinting), grouped by reason, each with its
  link, and a button to copy the list. The summary at the end of a list says
  which links have some, and names them for a link taken off the list once
  fingerprinted.
- **Search the list:** a box above the list shows only the links with every
  word typed, in the link, the channel's name or `@handle`, or how the last
  run ended. Ctrl+F in the list goes there, Esc clears it. Tick all and
  Ctrl+A work on the links shown.
- **Copy link and its videos**, in a row's right-click menu (and
  Ctrl+Shift+C): the link, then each of its videos on a line of its own,
  title and link separated by a tab, and why when its last run skipped it.
  With several rows selected, each link with its videos. The background
  count now keeps what it lists, in `listings\`, so the copy is instant; a
  link not listed yet is listed first, ahead of the daily counts, and what
  you copy meanwhile is not overwritten.
- **Keyboard shortcuts in the list:** Ctrl+C copies the selected links, Ctrl+V
  adds the links on the clipboard, the arrow keys, Page Up/Down, Home and End
  select rows (with Shift, a range), Space ticks or unticks, Enter opens,
  Ctrl+Up/Down/Home/End move the selected rows (Undo puts a move to the top
  or bottom back), F5 counts them again, and Shift+F10 opens the right-click
  menu. The right-click menu shows the keys.

### Fixed

- **The same channel could be added twice:** `youtube.com/@name` and
  `www.youtube.com/@name/videos` counted as two links. They are one now, while
  the Shorts, Live and Home tabs stay links of their own; as the bare channel
  runs its Shorts and Live too, the console says how to get those when the row
  already there is the Videos tab. A `channel/UC...` link and the `@name` of the
  same channel count as one too, once the channel pictures know whose channel
  the `@name` is. A saved list that already holds a link twice is named in the
  console at start.
- **The program crashed with a very long list:** each row takes some of the
  10,000 windows Windows lets a program have. The list now holds at most 1,000
  links; Add, Ctrl+V, Import and Undo say how many did not fit. A saved list
  of more keeps its first 1,000 and writes the rest to `links-left-out.txt`,
  to import once there is room. A row also takes one window fewer than
  before: the channel picture now shares its label with the result mark.
- **Copy link and its videos** says when it can mark only some of a link's
  skipped videos (its list of them was deleted), instead of marking fewer
  without a word.

## [1.0.0-beta.9] - 2026-10-06

### Changed

- **Settings opens where you last left it**, also after a restart, instead
  of always at the same place over the main window. Where it would not fit
  on the screen in full there (pushed partly past an edge, or a smaller
  screen than last time), it is moved just enough; if that place is on no
  screen any more (a second monitor unplugged), it opens over the main
  window as before.
- **Questions and messages are dark in dark mode**: Windows draws its own
  message boxes light whatever the program looks like, so in dark mode the
  program now draws them itself, with a dark title bar. They work as before:
  Enter, Y and N, Esc where there is a Cancel, Ctrl+C to copy the text. In
  light mode they are still Windows' own. **Select what to download** is
  dark too now. The windows for picking a file or folder are Windows' own
  and stay as Windows draws them.

### Fixed

- **"No audio format available" on YouTube videos that do have sound**: when
  YouTube offers no audio-only format, the sound is now copied out of its
  360p video (format 18) into an .m4a, without re-encoding, instead of the
  video being skipped. That happens:
  - on age-restricted videos when the account signed in with (Sign in with
    cookies from) is not age-verified;
  - now and then on any video, while YouTube's player for signed-in users
    answers "The page needs to be reloaded".

  The console says so for each video it happens to. A video with no sound at
  all is still skipped. Audio-only downloads are kept as they were, except
  that one that comes as an .mp4 (YouTube live recordings, some other sites)
  is now saved as .m4a, with the same sound.
- When an age-restricted video still fails that way, the reason says so,
  "age-restricted (account not age-verified)", with what to do about it.
- A download that failed after writing its file (for example when its sound
  could not be converted) left that file in the link's folder, where it was
  fingerprinted with the rest; when it could not be read, none of the link's
  items were remembered as fingerprinted. What a failed download wrote is
  now deleted.

## [1.0.0-beta.8] - 2026-10-04

### Added

- **How far each link has got, in the list**: the running link counts up as
  its videos download ("27/74 videos"), and after a run each link shows how
  many of its videos are done: fingerprinted in its last full run, left out by
  the length limits, or, with **Skip items already fingerprinted in an earlier
  run** on, fingerprinted before. A run stopped or skipped before its
  fingerprints were made keeps the count from before. A channel that has
  grown since shows its new videos as not done yet. Hover for what the numbers
  mean.
- **It tells you when a new Fingerprinter is out**: at start it asks GitHub
  for the newest release (betas too, while you use a beta) and, when there is
  a newer one, says so in the console and with a link in the status bar that
  opens its release page. Nothing is downloaded or installed. **Tell me when a
  new Fingerprinter is out** (Settings, General) turns it off.
- **Cancel in "Continue the unfinished list?"**: it starts nothing and
  forgets nothing; the question comes again at the next Download and
  fingerprint, or the next time the program starts. Esc and the question's X
  now do the same (before, it had only Yes and No and could not be closed any
  other way).

### Changed

- **A failed download says what yt-dlp said**: with the short reason ("no
  audio format available") the console now shows yt-dlp's own error and up to
  three of its warnings for that video, which usually name the real cause.
  yt-dlp's numbered retries count as one warning.
- **Five links are counted at a time** in the background, instead of two. The
  link that is running is not counted on the side: its run lists it anyway.
- Quitting while links are being counted no longer freezes the window for a
  few seconds.

### Fixed

- **A click beside a link in the list** opened the link in the browser: the
  link reached across the empty part of the row. Now only the link itself
  does, and a click beside it selects the row.
- The Settings window could run off the bottom of a small screen.

## [1.0.0-beta.7] - 2026-09-28

### Added

- **Channel pictures in the list**: a YouTube link shows its channel's picture
  (for a playlist or a video, that of the channel it is from), with the
  channel's name on hover. Fetched in the background once a month as a small
  round PNG, so without Pillow, and kept in `avatars\`, which uninstall removes
  too. Count again fetches them again. **Show channel pictures in the list**
  (Settings, General) turns them off, and then nothing is fetched. They go
  through the proxy yt-dlp uses (Extra download options or yt-dlp's own config
  file); with a SOCKS proxy, which they cannot use, nothing is fetched.
- **Search in the console** (or Ctrl+F): every match highlighted as you type,
  in any case, new lines included; Enter and Shift+Enter go from match to
  match, with a count, and Esc clears it. Going to a match turns Follow off
  until the search is cleared.
- **A right-click menu in the console**: Copy, Copy all, Select all, Search,
  Save as, Clear and Follow, and Open link or Copy link on a link.
- **Ctrl+Backspace and Ctrl+Delete** remove a word at a time in the link box
  and every other box, as elsewhere in Windows. Tk has neither.
- **Several rows selected at once** in the list: Ctrl+click, Shift+click, and
  Ctrl+A with the list clicked. Dragging one moves them all; right-click copies,
  counts again, ticks, unticks, moves or removes them all; Delete removes them
  and Esc clears the selection. Remove ticked with nothing ticked removes the
  selected rows.
- **Sort** above the list: most or fewest videos first, or by the link, A to Z
  or Z to A. Undo puts the old order back.
- **Show them**, beside Forget them (Settings, Downloads), opens the list of
  items remembered as fingerprinted.

### Changed

- **The Now panel** shows how each download or batch is going in a column of
  its own, instead of after a `|` at the end of its title.

### Fixed

- **Titles in the Now panel** were cut at 40 characters however wide the
  panel was. They now use the room there is, end in … where they are cut, and
  show in full on hover.
- **`fingerprinted-items.txt` edited by hand**: when its last line had no line
  break, the next item remembered ran on into it, and both were fetched again;
  a byte-order mark from Notepad hid the first item. Neither happens now.

## [1.0.0-beta.6] - 2026-09-27

### Added

- **Several links at once** in the link box: one per line, or separated by
  spaces or commas. `@handles`, channel IDs and links without `https://`
  work there too, as in Import.
- **Undo** (in the status bar, or Ctrl+Z) puts removed links back where they
  were. **Remove ticked** asks first when it would empty the list or take more
  than five links; every link is ticked when added, so it used to empty a new
  list without a word.
- **How each link's last run ended**, as a mark on its row (done, incomplete,
  failed, skipped, stopped; running now), kept with the list, and **Tick
  unfinished** to run the rest again. Ticks are kept between sessions too.
- **The run in the window title**, so it can be read from the taskbar, and the
  Now panel names the row when ticked links and rows differ.
- **Keep yt-dlp up to date** (Settings, General), on by default: once a day at
  start, a newer yt-dlp is installed. Before, only setup.bat and Check setup
  updated it.
- **When a job finishes: Sleep or Shut down** (Settings, General), after a
  minute's warning. Back to Do nothing at every start.
- **Pausing by itself** when ten downloads in a row fail in a way that says the
  site is refusing them (HTTP 429 or 403, YouTube's bot check, no connection),
  resuming after 10 minutes and longer each time; a full disk pauses until
  Resume. A warning before downloading more than the drive has free.
- The window opens where it was, at the same size, maximised or not.

### Changed

- **Skip link** stops a link's listing and downloads at once. A link that is
  already being fingerprinted finishes first: skipping it then threw away its
  finished fingerprints, which went out nameless as `recovered-*` at the next
  link while the link's audio was fingerprinted again in full next time.
- **"Open the working folder when a list starts"** opens it once per list,
  instead of a window for every link on a folder deleted when the link is done.
- After each link the console lists **only that link's new `.pklz` files**, and
  one line for the whole folder, instead of every file in it.
- A link already in the list is recognised however it is written: tracking
  parameters (`?si=`), `www.`, `m.`, a trailing slash, `youtu.be` links.
- **Audio on disk** asks for the name of its `.pklz` files, suggesting the
  channel of the one download folder in the working folder, and renames only
  its own batch files.
- The listing is read as one JSON object per item, so a title with a line
  break in it no longer loses the item.

### Fixed

- **A file audfprint could not read** (a cut-off download, say) was left out
  of the fingerprints without a word while its link ended as done, and with
  Skip items already fingerprinted on it was marked as fingerprinted, so never
  tried again. It is now named in the console, the link ends incomplete, and
  the item is not remembered.
- **A failed listing** said only "yt-dlp info failed:", with nothing after it.
  It now shows yt-dlp's error. A listing yt-dlp stopped part-way (a rate limit
  on page 30 of a large channel) ran as if complete and could end the link as
  done; the link is now incomplete, and the short count is not kept.
- **Audio on disk** named every file `channel-N.pklz`: it looked for the
  channel's folder in the program folder, not the working folder.
- **Quotes in Extra download options** reached yt-dlp as part of the value, so
  a quoted path with spaces was not found.
- YouTube's "confirm you're not a bot", a full disk and an outdated yt-dlp are
  recognised, with advice; a 404 or 429 on a page is no longer called a
  network error.

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

[1.1.0]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.1.0
[1.0.0]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0
[1.0.0-beta.9]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.9
[1.0.0-beta.8]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.8
[1.0.0-beta.7]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.7
[1.0.0-beta.6]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.6
[1.0.0-beta.5]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.5
[1.0.0-beta.4]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.4
[1.0.0-beta.3]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.3
[1.0.0-beta.2]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.2
[1.0.0-beta.1]: https://github.com/EierkuchenHD/fingerprinter/releases/tag/v1.0.0-beta.1
