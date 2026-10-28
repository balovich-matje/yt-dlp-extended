# yt-dlp-extended

A local web app for downloading videos with [yt-dlp](https://github.com/yt-dlp/yt-dlp)
and trimming or re-encoding them with [ffmpeg](https://ffmpeg.org). It runs on your
own machine at <http://127.0.0.1:8723>, calls both tools directly, and needs no account.

- **Download** — paste a link (YouTube, Reddit, Pinterest and
  [everything else yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)),
  pick video + audio, audio only or video only, choose a quality, and watch progress live.
- **Convert** — trim any file in your library (or one you upload) to mp4, mkv, webm,
  mp3, m4a or gif, either at a quality level or squeezed to a target file size.

---

## Install and run

You need **Python 3.10+**, **yt-dlp** and **ffmpeg** (which includes ffprobe). The
first launch creates a private Python environment in `.venv` and installs the rest.

Clone with `git` rather than GitHub's *Download ZIP*: launchers from a downloaded
zip get blocked by macOS Gatekeeper and flagged by Windows SmartScreen.

### macOS

```bash
brew install yt-dlp ffmpeg
git clone https://github.com/balovich-matje/yt-dlp-extended.git
```

Double-click **`start.command`**, or run `./run.sh` in Terminal. The launcher stops any
running copy of the app, starts a fresh one and opens your browser, so it doubles as
a restart button. `Ctrl-C` in its window stops the server.

### Linux

On Debian or Ubuntu:

```bash
sudo apt install ffmpeg python3 python3-venv curl pipx
pipx install yt-dlp
pipx ensurepath          # then open a new terminal so yt-dlp is on PATH
git clone https://github.com/balovich-matje/yt-dlp-extended.git
cd yt-dlp-extended && ./run.sh
```

yt-dlp comes from `pipx` because the distribution package is usually months behind,
and an old yt-dlp is the most common reason YouTube downloads fail. On other
distributions, install the same things with your package manager — make sure the
ffmpeg build includes `libx264`.

### Windows

> **Untested.** The Windows support is written but has not yet been run on a real
> Windows machine. If something breaks, please open an issue with the error.

In PowerShell:

```powershell
winget install Python.Python.3.12
winget install yt-dlp.yt-dlp
winget install Gyan.FFmpeg
winget install Git.Git
```

Open a **new** terminal so the tools are on `PATH`, then:

```powershell
git clone https://github.com/balovich-matje/yt-dlp-extended.git
```

Double-click **`start.bat`**. Like the macOS launcher, it restarts the app and opens
your browser.

### Options

| Environment variable | Default | Effect |
| --- | --- | --- |
| `YTX_PORT` | `8723` | port to serve on |
| `YTX_HOST` | `127.0.0.1` | bind address — local-only by default |
| `YTX_NO_BROWSER` | `0` | `1` skips opening a browser |
| `YTX_DATA_DIR` | `./data` | where the database lives |
| `PYTHON` | auto | interpreter used for the virtualenv (`run.sh` only) |

Download and output folders, and explicit paths to yt-dlp/ffmpeg if they aren't on
`PATH`, are set on the **Settings** tab. The header shows a green dot for each tool
it can find.

There is no login and none is needed: the server only listens on your own machine.

---

## Downloading

Paste a link and the title, duration, thumbnail and real format list appear. Pick a
mode and quality, then **Download**. Progress shows percent, speed and ETA, and
**Cancel** stops it immediately.

YouTube occasionally cuts off a download part way through with
`HTTP Error 403: Forbidden`. The app retries up to three times automatically, resuming
from where it stopped, and the job card shows `HTTP 403 — retrying (2 of 3)` while it
does.

### Signed-in content: age-restricted videos, Reddit

Some content needs you to be logged in. Under **Settings → Cookies**, pick the browser
you're signed in with, or point at an exported `cookies.txt`. The page checks the
source when you save and tells you if it yields no cookies, because yt-dlp otherwise
carries on signed-out without saying so.

Your browser does not need to be open. What does need setting up depends on the OS:

- **macOS** — give your terminal **Full Disk Access** (System Settings → Privacy &
  Security → Full Disk Access), then restart it. Chrome also asks for your Keychain
  password once.
- **Windows** — Chrome, Edge and Brave encrypt their cookies so other programs can't
  read them. Choose **Firefox**, or export a `cookies.txt`.
- **Linux** — Chromium-based browsers are decrypted through your desktop keyring;
  Firefox is the simplest choice.

A `cookies.txt` grants access to your account, so keep it outside this folder.

### Site notes

**Reddit** requires you to be signed in — Reddit blocks every signed-out request.
Sign into reddit.com in your cookie browser and paste the post link. Full links,
share-button `/s/` links and `redd.it` links all work.

**Pinterest** works with pin links and `pin.it` share links. Image pins have no video,
and the app says so. A board link only downloads its first video, and can take a
minute to load on big boards.

---

## Converting

Choose a file from the library or upload one.

**Trim** with `HH:MM:SS.mmm` start and end times, by dragging the markers under the
preview, or with the `⇤`/`⇥` buttons, which set a point from the playhead. The preview
plays **only the selection**: play starts at the in point and stops at the out point,
or repeats it with **Loop** on. Dragging a marker shows the frame you're cutting at,
and if the preview was playing it restarts with the new cut. While paused, you can
scrub anywhere to find new cut points.

**Output formats:**

| Container | Video | Audio | Notes |
| --- | --- | --- | --- |
| mp4, mkv | H.264 (or H.265) | AAC | |
| webm | VP9 | Opus | |
| mp3, m4a | — | MP3 / AAC | audio only |
| gif | palette-optimised | — | no size target |

Resolution, frame rate and audio bitrate can be overridden.

**Size** has two modes:

- **Quality mode** sets a visual quality level (CRF); the file is as big as it needs
  to be.
- **Target size** encodes in two passes to fit a size you choose in MB. The panel above
  the slider predicts the result and warns when the target is too small to look good.
  When the job finishes, it shows the actual size against the prediction — they
  usually agree within a few percent.

---

## Troubleshooting

**Every failure shows the tool's real error.** Expand *Process output* on a job to see
the exact command and its output.

**YouTube downloads failing?** Update yt-dlp first — YouTube changes often and old
versions break:

| | |
| --- | --- |
| macOS | `brew upgrade yt-dlp` |
| Linux | `pipx upgrade yt-dlp` |
| Windows | `winget upgrade yt-dlp.yt-dlp` |

**"Sign in to confirm your age" or a Reddit login error?** Set up cookies as described
above, and check the Cookies section of Settings says cookies were found.

**A tool shows a red dot?** It isn't on `PATH`. Install it, or enter its full path on
the Settings tab.

---

## Your data

Everything stays on your machine:

- downloads in `downloads/`, encodes in `output/` (both changeable in Settings)
- the library, job history and settings in `data/ytx.sqlite3`

Deleting a file from the library deletes it from disk. **Rescan folders** picks up
files you added yourself. None of these folders are tracked by git.

## Project layout

```
app/main.py      web API and live progress stream (Server-Sent Events)
app/download.py  yt-dlp: metadata, format choice, progress, retries, cookies
app/convert.py   ffmpeg: size estimator, command building, two-pass encoding
app/jobs.py      background jobs and cancellation
app/db.py        SQLite storage
web/             the single-page interface — plain JavaScript, no build step
run.sh           launcher for macOS and Linux
start.command    double-click launcher for macOS
start.bat        double-click launcher for Windows
```

The API is browsable at <http://127.0.0.1:8723/api/docs> while the app is running.
