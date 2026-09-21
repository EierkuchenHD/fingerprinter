# YouTube Channel Fingerprinter

[![Stars](https://img.shields.io/github/stars/EierkuchenHD/fingerprinter?style=flat-square&logo=github&label=stars)](https://github.com/EierkuchenHD/fingerprinter/stargazers)
[![Forks](https://img.shields.io/github/forks/EierkuchenHD/fingerprinter?style=flat-square&logo=github&label=forks)](https://github.com/EierkuchenHD/fingerprinter/network/members)
[![Last update](https://img.shields.io/github/last-commit/EierkuchenHD/fingerprinter?style=flat-square&label=last%20update)](https://github.com/EierkuchenHD/fingerprinter/commits/main)
[![Commits](https://img.shields.io/github/commit-activity/t/EierkuchenHD/fingerprinter?style=flat-square&label=commits)](https://github.com/EierkuchenHD/fingerprinter/commits/main)
![Code size](https://img.shields.io/github/languages/code-size/EierkuchenHD/fingerprinter?style=flat-square)
[![License](https://img.shields.io/github/license/EierkuchenHD/fingerprinter?style=flat-square)](LICENSE)

Point it at a YouTube channel, playlist, or archive.org page. It downloads the
audio, cuts long recordings into shorter pieces, and turns every piece into an
audio **fingerprint** — a small `.pklz` file that
[audfprint](https://github.com/dpwe/audfprint) can later use to recognise that
audio when it turns up somewhere else.

It is a normal desktop window with buttons — once it is set up, you never touch
a command line to use it. Setup itself needs the command line exactly twice, to
install some Python packages; step 3 shows exactly what to type.

![The Fingerprinter window](screenshot.png)

The window is three numbered steps in the order you actually do them: say what
you want fingerprinted, say where things should go, press the button. Every box
has a line underneath explaining what it is for, and anything with a sensible
default is tucked away under **Advanced settings** so it is not in your way.

## What is a fingerprint, and why would I want one?

A fingerprint is a compact summary of what a recording *sounds like* — a few
thousand landmarks taken from the audio, not the audio itself. Two things make
that useful:

- **It is small.** A fingerprint is a tiny fraction of the size of the audio.
- **It still matches.** Re-encoded, quieter, noisier, or cut down to a clip, the
  same recording still lines up against its fingerprint.

So once a channel is fingerprinted, you can take some unidentified audio, ask
audfprint what it matches, and get an answer — which is the usual reason people
build these collections (identifying "lostwave" tracks, for instance).

This tool handles the tedious part: fetching the audio and producing the
`.pklz` files in bulk, without you babysitting it.

## Two things to know before your first run

Neither is a bug, but both surprise people, and both can lose work.

> **1. The downloaded audio is deleted when a link finishes.**
> The audio is treated as working material, not as a result — once a link has
> been fingerprinted, its download folder is removed to stop a long list
> filling your disk. **The `.pklz` files are the output you keep.** If you also
> want the audio, copy it out while the run is going, or use a separate
> downloader.

> **2. Fill in *Keep finished fingerprints in* before adding more than one link.**
> The results folder is emptied before each link is fingerprinted. With a single
> link that does not matter. With a list of five it very much does: without a
> destination to move finished fingerprints to, **you end up with only the last
> link's results.** The program turns that line red to remind you, and asks
> before starting anyway.

## What you need before you start

Five things. All free, all on Windows.

| What | Why it is needed | Where to get it |
|---|---|---|
| **Python 3.10 or newer** | Runs this program. During install, tick **"Add Python to PATH"**. | [python.org/downloads](https://www.python.org/downloads/) |
| **ffmpeg** (includes ffprobe) | Reads audio lengths and does the cutting | [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/) — under *release builds*, take **ffmpeg-release-full.7z** (or the `.zip`) |
| **Node.js** | yt-dlp needs it to read YouTube pages; downloads fail without it | [nodejs.org](https://nodejs.org/) — the LTS installer, all defaults |
| **yt-dlp** | Does the actual downloading | Nothing to fetch — step 3 installs it |
| **audfprint** | Makes the fingerprints | [github.com/dpwe/audfprint](https://github.com/dpwe/audfprint) |

> **"On PATH" — what that means.** Windows needs to know where a program lives
> before you can call it by name. The Python and Node.js installers do this for
> you (tick the Python box). ffmpeg does not, so you do it yourself:
>
> 1. Unzip ffmpeg somewhere permanent. It unpacks into a folder with a version
>    in the name, like `ffmpeg-7.1-full_build` — **move or rename it so that
>    `C:\ffmpeg\bin` genuinely exists** and has `ffmpeg.exe` in it.
> 2. Press Start, type *"Edit the system environment variables"*, open it.
> 3. **Environment Variables** → under *System variables* select **Path** →
>    **Edit** → **New** → paste `C:\ffmpeg\bin` → **OK** on all three windows.
>
> **To check:** open a *new* Command Prompt (Start → type `cmd` → Enter) and
> type `ffmpeg -version`. Version information means it worked; "not recognized"
> means the path is wrong. Already-open Command Prompts keep the old PATH, so
> always open a fresh one after changing it.

## Setting it up

**1. Download this project.** Green **Code** button at the top of this page →
**Download ZIP** → unzip it somewhere sensible, for example
`C:\fingerprints\Fingerprinter`.

**2. Put audfprint inside it.** Download audfprint the same way (Code →
Download ZIP on [its page](https://github.com/dpwe/audfprint)).

> **Watch out:** GitHub's ZIP unpacks into a folder called `audfprint-master`,
> with the real files one level down. You need to rename it to `audfprint`, or
> move its contents up — whichever you do, aim for this exact layout:

```
C:\fingerprints\Fingerprinter\
├── yt-fingerprinter.pyw
├── audfprint_quiet.py
├── requirements.txt
└── audfprint\
    └── audfprint.py      <-- this file must be right here
```

If `audfprint\audfprint-master\audfprint.py` is what you ended up with, it is
one level too deep and the program will not find it.

**3. Install the Python packages.** Open File Explorer in that folder, click the
address bar, type `cmd`, press Enter — a Command Prompt opens there. Then run
these two lines, pressing Enter after each:

```bat
pip install -r requirements.txt
pip install yt-dlp
```

(If you get *"'pip' is not recognized"*, Python was installed without the
**Add Python to PATH** box ticked. Re-run the Python installer, choose
**Modify**, and tick it.)

**4. Start it.** Double-click `yt-fingerprinter.pyw`.

> Windows hides file extensions by default, so in Explorer it may appear simply
> as **yt-fingerprinter**. If double-clicking opens a text editor instead of the
> program, right-click it → **Open with** → **Python**.

**5. Set your working folder.** In **Step 2**, use **Browse...** next to
**Working folder for audio** and pick somewhere with room to spare, for example
`C:\fingerprints\download`. *This program's folder* fills itself in, so you can
normally leave it alone.

Everything is remembered from then on: the program writes a `config.json` when
you close it and reads it back next time. (`config.example.json` in this
repository shows what that file looks like.)

## Using it

### The short version

1. **Step 1** — paste a link and press **Add to list**.
2. **Step 2** — check the folders. If your list has more than one link, fill in
   **Keep finished fingerprints in**.
3. **Step 3** — press **Download and fingerprint**, and watch *What it is doing*
   at the bottom.

**It does not stop to ask you anything.** It lists the link, prints a size and
an estimated download time to the log, and gets going. Read those numbers as
they appear, and press **Stop** if they are bigger than you bargained for.

When it finishes, your `.pklz` files are wherever **Keep finished fingerprints
in** points (or in the `pklz-files` folder), and that folder opens for you.

### The buttons in Step 3

**Download and fingerprint** — the whole job for every ticked link: download,
cut, fingerprint, then delete the downloaded audio. This is the one you want
almost always.

**Split + fingerprint files I already have** — skips downloading and works on
audio already sitting in your working folder. Use it for audio of your own, or
to pick up after a crash. It is also the gentler of the two: it does *not*
clear the results folder, so fingerprints you already have survive.

**Skip this link** — moves on to the next one in the list. It takes effect
between stages rather than instantly, so a download already in flight finishes
first.

**Stop** — stops everything.

**Check my setup** — the thing to press when something seems wrong. It checks
Python, ffmpeg, ffprobe, node, yt-dlp and audfprint, plus free disk space and
whether your working folder is writable. Note it also runs `yt-dlp -U`, which
tries to **update yt-dlp over the internet** and can take up to 90 seconds.

### Doing several links in one go

**Your list** in Step 1 holds as many as you like. **Add to list** adds whatever
is in the Link box; **Import from file...** reads a plain text file with one URL
per line. Reorder with **Move up** / **Move down**, tidy up with **Remove
ticked** or **Clear the list**, then press **Download and fingerprint**. It
works from top to bottom, and the list is saved when you close the program, so
an interrupted batch is still there in the morning.

Untick a row to leave it in the list but skip it this time.

## Advanced settings

The **Advanced settings** button opens a separate little window. Nothing in it
needs changing to get a good result.

![The Advanced settings window](screenshot-advanced.png)

| Setting | What it does |
|---|---|
| **Downloads at once** (4) | How many downloads run in parallel. Raise it on a fast connection; lower it if downloads start failing. |
| **Fingerprint jobs at once** (4) | How many fingerprinting jobs run side by side. The main speed control once downloading is done. |
| **Cores per job** (1) | Best left at 1. audfprint's own multi-core mode is slower in practice than simply running more jobs at once. |
| **Recordings per file** (1000) | How many recordings go into one `.pklz`. Keep it high: a matching tool reloads *every* `.pklz` each time it runs, so many small ones make every future search slower. |
| **Split anything over 12 minutes** (on) | **Leave this on.** Explained below. |
| **Show every line of download output** | Useful when diagnosing a failure, noisy otherwise. |
| **Open the audio folder when a link starts** | Opens it so you can watch files arrive. |
| **Open the results folder when it finishes** | Opens your fingerprints at the end. |
| **Name downloaded files** | yt-dlp [output notation](https://github.com/yt-dlp/yt-dlp#output-template). The default gives `Title [videoid].m4a` — or `.opus`, depending on what the site offers. Safe to ignore. |
| **Extra download options** | Passed straight to yt-dlp. Usually empty — see *Private or age-restricted videos*. |

### Why the splitting matters

Anything longer than 12 minutes is cut into pieces of at least 6 minutes each.
This is not about file size — it is about how matching works.

A fingerprint tells you *that* a match happened, but a match against a
three-hour mix only tells you "it is somewhere in this three-hour mix". Cutting
long uploads into 6-minute pieces means a hit points at a 6-minute window
instead, which is a far more useful answer. Six minutes is the floor because
shorter pieces give audfprint too little to work with.

The cutting copies the audio rather than re-encoding it, so nothing is lost and
it takes seconds rather than minutes.

## Where everything ends up

Inside the program's own folder:

- **`pklz-files\`** — your results, unless *Keep finished fingerprints in* is set.
- **`texts\`** — working lists written for audfprint. Housekeeping.
- **`logs\`** — a record of each run, worth keeping if you need to ask for help.

Once the downloading stage is done, these are cleared before fingerprinting
begins — `texts\` and `pklz-files\` for **Download and fingerprint**, only
`texts\` for **Split + fingerprint files I already have**.

> If they are not already empty you get a prompt first — **but that prompt
> answers itself with "yes, delete" after two minutes** if nobody is at the
> keyboard, so an unattended list is never blocked by it. Move anything you want
> to keep out of the way before starting, rather than relying on the prompt.

## If something goes wrong

**Press *Check my setup* first.** It checks each requirement in turn and names
whichever is missing, which usually answers the question on its own.

| Problem | What is going on |
|---|---|
| *"'pip' is not recognized"* | Python was installed without **Add Python to PATH**. Re-run its installer → **Modify** → tick the box. |
| *"Could not find audfprint\audfprint.py"* | audfprint is missing, or one folder too deep — see the warning in step 2. |
| *"ffmpeg is not recognized"* | ffmpeg is not on PATH, or `C:\ffmpeg\bin` does not really exist because of the versioned folder. See *What you need*. |
| Downloads fail, or nothing downloads at all | Usually Node.js missing, or YouTube rate-limiting. Check node in *Check my setup*, then lower **Downloads at once**. |
| Some videos are skipped | Members-only, private, or region-blocked. See below. |
| A list produced far fewer results than expected | *Keep finished fingerprints in* was empty, so each link overwrote the previous one. |
| My downloaded audio vanished | Expected — it is deleted once a link is fingerprinted. See the warnings near the top. |
| A run seems frozen | Big channels take a while to list before anything visible happens. Turn on **Show every line of download output** to confirm it is still working. |

### Private or age-restricted videos

If videos need you to be signed in, put this in **Extra download options** under
Advanced settings:

```
--cookies-from-browser firefox
```

replacing `firefox` with `chrome`, `edge`, or `brave` as appropriate. yt-dlp
then borrows the login from that browser. Close the browser first — it may lock
its own cookie database while running.

One catch: this reads the cookies of *the Windows account the program runs
under*. If you run it as a scheduled task or a service, that account has no
browser profile, and it fails with a "file not found" error mentioning
`systemprofile`.

## Good to know

- **Audio is never re-encoded.** It is downloaded in its original form and
  split by copying, so quality is exactly what the site served.
- **Nothing pops up while it works.** The helper programs it runs, and the ones
  audfprint runs in turn, are all started hidden, so a long run does not
  flicker black windows across your screen.
- **Closing the window saves your settings**, including your list.
- **It does not fingerprint the same batch twice.** Finished work is recorded in
  `fingerprinted.json` beside the `.pklz` files. To resume after a crash, press
  **Split + fingerprint files I already have** — not **Download and
  fingerprint**, which clears the results folder and takes that record with it.

## Credits

Fingerprinting is done by **[audfprint](https://github.com/dpwe/audfprint)** by
Dan Ellis, and downloading by **[yt-dlp](https://github.com/yt-dlp/yt-dlp)**.
This project is the desktop window around them; credit for the hard parts
belongs to those projects.

Only download and store material you have the right to. This tool does not
decide that for you.

## License

[MIT](LICENSE) — do what you like with it, including commercially, as long as
the copyright notice comes along. It is provided as-is, with no warranty.

This covers *this* project only. audfprint and yt-dlp are separate projects
under their own licences; you download them yourself, and their terms are
theirs.
