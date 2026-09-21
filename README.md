# YouTube Channel Fingerprinter

Point it at a YouTube channel, playlist, or archive.org page. It downloads the
audio, cuts long recordings into shorter pieces, and turns every piece into an
audio **fingerprint** — a small `.pklz` file that
[audfprint](https://github.com/dpwe/audfprint) can later use to recognise that
audio when it turns up somewhere else.

It is a normal desktop window with buttons. You do not need to use a command
line to run it.

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

## What you need before you start

Four things. All free, all on Windows.

| What | Why it is needed | Where to get it |
|---|---|---|
| **Python 3.10 or newer** | Runs this program. During install, tick **"Add Python to PATH"**. | [python.org/downloads](https://www.python.org/downloads/) |
| **ffmpeg** (includes ffprobe) | Reads audio lengths and does the cutting | [ffmpeg.org/download.html](https://ffmpeg.org/download.html) |
| **yt-dlp** | Does the actual downloading | `pip install yt-dlp` |
| **audfprint** | Makes the fingerprints | [github.com/dpwe/audfprint](https://github.com/dpwe/audfprint) |

> **"On PATH" — what that means.** Windows needs to know where a program lives
> before you can call it by name. Python's installer does this for you if you
> tick the box. For ffmpeg you unzip it somewhere permanent (say `C:\ffmpeg`)
> and add its `bin` folder to your PATH — search Windows for *"Edit the system
> environment variables"* → **Environment Variables** → select **Path** →
> **Edit** → **New** → paste `C:\ffmpeg\bin` → OK.
>
> To check it worked, open a new Command Prompt and type `ffmpeg -version`. If
> you get version information rather than "not recognized", you are set.

## Setting it up

**1. Download this project.** Green **Code** button at the top of this page →
**Download ZIP** → unzip it somewhere sensible, for example
`C:\fingerprints\Fingerprinter`.

**2. Put audfprint inside it.** Download audfprint (same Code → Download ZIP
trick on [its page](https://github.com/dpwe/audfprint)) and unpack it so the
layout looks exactly like this:

```
C:\fingerprints\Fingerprinter\
├── yt-fingerprinter.pyw
├── requirements.txt
└── audfprint\
    └── audfprint.py      <-- this file must be here
```

The program checks for `audfprint\audfprint.py` on startup and will tell you if
it cannot find it.

**3. Install the Python packages.** Open a Command Prompt in that folder (click
the address bar in File Explorer, type `cmd`, press Enter) and run:

```bat
pip install -r requirements.txt
pip install yt-dlp
```

**4. Start it.** Double-click `yt-fingerprinter.pyw`.

That is the whole setup. There is nothing to configure by hand — the program
writes its own `config.json` with your settings when you close it and reads it
back next time. (`config.example.json` in this repository shows what that file
looks like, if you are curious.)

## Using it

### The short version

1. Paste a channel or playlist URL into **YouTube URL**.
2. Set **Output directory** to where the audio should be saved.
3. Click **Start queue**.
4. Untick anything you do not want, confirm the size estimate, and let it run.

When it finishes, your `.pklz` files are in the `pklz-files` folder inside the
Fingerprinter directory, and it will open that folder for you.

### The two main buttons

**Start queue** — the full job: download, cut, fingerprint. This is the one you
want almost always.

**Split + Fingerprint** — skips downloading and works on audio you already have
in the output directory. Use it when you have files from elsewhere, or when a
previous run downloaded successfully but you want to redo the fingerprinting.

**Skip current** abandons the item being worked on and moves to the next one.
**Cancel** stops everything. **Test Connection** checks that Python, ffmpeg,
yt-dlp and audfprint are all present and working — run it first if something
seems off.

### Doing several channels in one go

The **Channel queue** section takes a list. **Add to queue** adds the URL
currently in the box; **Import file...** reads a plain text file with one URL
per line. Reorder with **Move up** / **Move down**, tidy up with **Remove
checked** or **Clear all**, then press **Start queue** and leave it alone. It
works through the list from top to bottom.

The queue is saved when you close the program, so an interrupted batch is still
there the next morning.

## The settings, explained

Everything here has a sensible default. Change things only if you have a
reason.

| Setting | What it does |
|---|---|
| **Output directory** | Where downloaded audio goes. Each channel gets its own subfolder. |
| **Fingerprinter directory** | Where this program and the `audfprint` folder live. It fills itself in; leave it alone. |
| **Move pklzs to (optional)** | If set, finished `.pklz` files are moved here afterwards — handy if a matching tool reads from a fixed location. Leave empty to keep them in `pklz-files`. |
| **Parallel downloads** | How many downloads run at once. 10 is a good default. Lower it if your connection struggles. |
| **Concurrent batches** | How many fingerprinting jobs run side by side. 4 is a good default. |
| **audfprint cores** | Leave at **1**. audfprint's own multi-core mode turns out to be slower in practice than simply running more batches at once — which is what *Concurrent batches* does. |
| **Files per pklz** | How many recordings go into one fingerprint file. Keep it high: a matching tool reloads *every* `.pklz` each time it runs, so hundreds of small ones make every future search slower. 1000 is a good default. |
| **Filename template** | How downloaded files are named, in yt-dlp's notation. The default gives `Title [videoid].m4a`. |
| **Extra yt-dlp args** | Passed straight to yt-dlp. Most people leave this empty — see *Private or age-restricted videos* below. |

And the four tickboxes:

| Tickbox | What it does |
|---|---|
| **Verbose yt-dlp output** | Shows every line yt-dlp prints. Useful when diagnosing a failure, noisy otherwise. |
| **Open channel subfolder on start** | Opens the download folder in Explorer so you can watch files arrive. |
| **Open pklz-files folder when done** | Opens the results folder at the end. |
| **Split into pieces of at least 6:00** | **Keep this ticked.** Explained just below. |

### Why the splitting matters

Anything longer than 12 minutes is cut into pieces of at least 6 minutes each.
This is not about file size — it is about how matching works.

A fingerprint tells you *that* a match happened, but a match against a
three-hour mix only tells you "it is somewhere in this three-hour mix". Cutting
long uploads into 6-minute pieces means a hit points at a 6-minute window
instead, which is a far more useful answer. Six minutes is the floor because
shorter pieces start to give audfprint too little to work with.

The cutting happens in place and copies the audio rather than re-encoding it,
so nothing is lost and it takes seconds rather than minutes.

## Where everything ends up

Inside your Fingerprinter directory:

- **`pklz-files\`** — your results. These are the fingerprints.
- **`texts\`** — working lists the program writes for audfprint. Housekeeping.
- **`logs\`** — a record of each run, worth keeping if you need to ask for help.

Both `texts\` and `pklz-files\` are cleared at the start of a run, with a
confirmation prompt first if they are not already empty. If you want to keep a
batch of results, move them out (or set **Move pklzs to**) before starting the
next one.

## If something goes wrong

**Press *Test Connection* first.** It checks each requirement in turn and names
the one that is missing, which usually answers the question on its own.

| Problem | What is going on |
|---|---|
| *"Could not find audfprint\audfprint.py"* | audfprint is missing, or nested one folder too deep. You want `...\Fingerprinter\audfprint\audfprint.py` exactly — the common slip is ending up with `audfprint\audfprint-master\audfprint.py`. |
| *"ffmpeg is not recognized"* | ffmpeg is not on PATH. See the note in *What you need*. Open a **new** Command Prompt after changing PATH — already-open ones keep the old value. |
| Downloads fail with HTTP errors | Usually YouTube rate-limiting. Lower **Parallel downloads** and try again. |
| Some videos are skipped | Members-only, private, or region-blocked. See below. |
| A run seems frozen | Large channels take a while to list before anything visible happens. Tick **Verbose yt-dlp output** to confirm it is still working. |
| Brief black windows flash past | Cosmetic, and only during fingerprinting. See *A note on flashing windows*. |

### Private or age-restricted videos

If videos need you to be signed in, put this in **Extra yt-dlp args**:

```
--cookies-from-browser firefox
```

replacing `firefox` with `chrome`, `edge`, or `brave` as appropriate. yt-dlp
then borrows the login from that browser. Close the browser first — it may lock
its own cookie database while it is running.

One catch worth knowing: this reads the cookies of *the Windows account the
program is running under*. If you run it as a scheduled task or a service, that
account has no browser profile, and this fails with a "file not found" error
mentioning `systemprofile`.

### A note on flashing windows

This program already hides the helper windows it opens itself. audfprint,
however, calls ffmpeg on its own, which produces a brief black flicker per
file. To silence those too, open `audfprint\audio_read.py`, find where it
starts ffmpeg with `subprocess.Popen(...)`, and add this argument:

```python
creationflags=subprocess.CREATE_NO_WINDOW
```

Entirely optional — it changes nothing except the flicker.

## Good to know

- **Audio is never re-encoded.** Downloads are taken in their original format,
  and splitting copies the stream, so quality is exactly what YouTube served.
- **Closing the window saves your settings**, including the queue.
- **It does not fingerprint the same batch twice.** Finished work is recorded,
  so a re-run after a crash picks up rather than starting over.
- **Large channels are large.** You get a size and duration estimate, with a
  confirmation step, before anything downloads.

## Credits

Fingerprinting is done by **[audfprint](https://github.com/dpwe/audfprint)** by
Dan Ellis, and downloading by **[yt-dlp](https://github.com/yt-dlp/yt-dlp)**.
This project is the desktop window around them; credit for the hard parts
belongs to those projects.

Only download and store material you have the right to. This tool does not
decide that for you.
