# yt-dlp-extended

A locally-hosted web front end for `yt-dlp` and `ffmpeg`. It shells out to both
binaries and parses their output — nothing about downloading or encoding is
reimplemented.

Two independent stages:

- **Download** — paste a URL, inspect the real format list, pick video / audio /
  both, watch live progress, cancel mid-flight.
- **Convert** — trim any file in the library (or one you upload), transcode it to
  another container, and either target a file size (two-pass) or a quality level
  (single-pass CRF).

You can use either stage on its own. Converting never requires downloading, and
downloading never requires converting.

---

## Requirements

| Tool | Why | Check |
| --- | --- | --- |
| Python 3.10+ | runs the server | `python3 --version` |
| `yt-dlp` | stage 1 | `yt-dlp --version` |
| `ffmpeg` | stage 2, and merging in stage 1 | `ffmpeg -version` |
| `ffprobe` | source inspection and the size estimator | `ffprobe -version` |

On macOS:

```bash
brew install yt-dlp ffmpeg
```

`ffprobe` ships with `ffmpeg`. If any binary lives somewhere unusual, set an
explicit path on the **Settings** tab instead of putting it on `PATH` — the
header shows a green dot per binary once it resolves.

## Run it

```bash
./run.sh
```

That is the whole thing. On first run it creates `.venv`, installs the Python
dependencies, then serves the app and opens a browser at
<http://127.0.0.1:8723>. Later runs skip straight to serving unless
`requirements.txt` has changed. Press `Ctrl-C` to stop.

Useful environment variables:

| Variable | Default | Effect |
| --- | --- | --- |
| `YTX_PORT` | `8723` | port to serve on |
| `YTX_HOST` | `127.0.0.1` | bind address — localhost only by default |
| `YTX_NO_BROWSER` | `0` | set to `1` to skip opening a browser |
| `YTX_DATA_DIR` | `./data` | where the SQLite database lives |
| `PYTHON` | auto-detected | interpreter used to build the venv |

Anything after `./run.sh` is passed through to uvicorn, so `./run.sh --reload`
works for development.

There is no authentication and none is wanted: the server binds to loopback and
assumes a single trusted user.

---

## Stage 1 — Download

Paste a URL and metadata is fetched automatically via `yt-dlp -J`; title,
uploader, duration, thumbnail and the full format table appear.

Pick a **mode**, and the quality dropdown repopulates from the actual metadata:

- **Full video** — video and audio. Choosing a video-only format merges the best
  audio into it with ffmpeg; choosing a progressive format skips the merge.
- **Audio only** — audio-only formats, optionally re-encoded to mp3/m4a/opus/
  flac/wav.
- **Video only** — video-only formats, producing a file with no audio track.

Progress shows percent, speed and ETA parsed from yt-dlp's
`--progress-template` output. When two streams are downloaded and merged, the
bar spreads across both and the phase label tracks the post-processing
(`Merging streams`, `Extracting audio`) rather than sitting at 100% while ffmpeg
is still working.

**Cancel** sends `SIGTERM` to the child's process group, escalating to
`SIGKILL` after five seconds. Each download writes its partial fragments to a
private scratch directory, so cancelling one job never disturbs another's
in-progress files.

## Stage 2 — Convert

Pick any library file, or upload one directly.

**Trim.** Start and end take `HH:MM:SS.mmm`. The scrubbable preview player has
draggable in/out markers, and the `⇤`/`⇥` buttons set each point from the
playhead. Text entry is always available and is validated against the real
duration from `ffprobe` — inverted ranges, out-of-range endpoints and malformed
timestamps are rejected before a job starts, with a specific message.

**Output.** Containers with sane codec defaults:

| Container | Video | Audio | Default CRF |
| --- | --- | --- | --- |
| mp4 | libx264 (or libx265) | aac | 23 |
| mkv | libx264 (or libx265) | aac | 23 |
| webm | libvpx-vp9 | libopus | 32 |
| mp3 | — | libmp3lame | — |
| m4a | — | aac | — |
| gif | palettegen/paletteuse | — | — |

Resolution scaling, frame rate and audio bitrate are optional overrides that
default to the source's own values.

**Sizing.** Two mutually exclusive modes:

- **Quality mode** — single-pass CRF. Targets a visual quality level; the final
  size is genuinely not knowable in advance, and the panel says so rather than
  pretending otherwise.
- **Target size** — two-pass encode to a byte budget, with a slider and a
  numeric input in MB. The slider floor is a very-low-bitrate encode, and the
  ceiling is roughly what the trimmed span occupies in the source file.

The estimate panel above the slider updates on every change and shows predicted
size, the video bitrate that will actually be used, resolution, frame rate and
bits-per-pixel. It warns when the target is low enough to visibly hurt quality,
and clamps to a 100 kbps floor rather than silently producing garbage.

The arithmetic:

```
budget_bps      = target_bytes * 8 / trimmed_duration_sec
video_bitrate   = budget_bps * (1 - overhead) - audio_bitrate     # overhead = 2%
video_bitrate   = max(video_bitrate, 100_000)                     # clamped, and warned about
predicted_bytes = (video_bitrate + audio_bitrate) * duration / 8 / (1 - overhead)
```

Two things keep the prediction honest. The overhead margin accounts for
container muxing. And the requested audio bitrate is clamped to what the encoder
will really deliver — ffmpeg's native AAC encoder saturates near 128 kbps per
channel, so asking a mono track for 192 kbps quietly yields ~129 kbps, which
would otherwise skew every prediction that included it.

After each job finishes, the job card reports **actual vs. predicted** size and
the percentage error, so the estimator can be checked against reality. Measured
on the test clips here, size-targeted encodes land within a few percent:

| Job | Predicted | Actual | Error |
| --- | --- | --- | --- |
| mp4 two-pass, 10s @ 2 MB | 2,000,000 | 2,031,364 | +1.6% |
| mp4 two-pass, 7.75s @ 1.2 MB | 1,199,999 | 1,216,744 | +1.4% |
| mkv/x265 two-pass, 4s @ 0.5 MB | 500,000 | 489,121 | −2.2% |
| m4a, 5s | 81,632 | 79,541 | −2.6% |

GIF is a deliberate exception: its size is driven by resolution and frame count,
not bitrate, so size targeting is disabled for it and the panel says why.

---

## When things break

Failures are surfaced, not swallowed. A failed job shows the actual stderr from
`yt-dlp` or `ffmpeg` verbatim, and every job keeps an expandable **Process
output** panel with the exact command line that was run plus the tail of its
output. A geo-blocked video, a 403, a missing codec or a bad filter all read as
what they are.

Cancelled and failed encodes delete their partial output rather than leaving a
truncated file behind, so nothing half-written enters the library.

## Persistence

State lives in SQLite at `data/ytx.sqlite3`:

- **settings** — download and output directories, binary path overrides
- **files** — the library, with origin (download / upload / convert)
- **jobs** — full history including the command line, the estimate and the log tail

Job history, the library and settings all survive a restart. A job still marked
running at startup is flagged as interrupted, since its subprocess died with the
previous server. Deleting a library entry removes the file from disk; **Rescan
folders** picks up files added to the managed directories by other means and
drops entries whose file has disappeared.

## Layout

```
app/
  main.py      FastAPI routes, SSE hub, range-aware media serving
  jobs.py      job state, subprocess lifecycle, cancellation, event broadcast
  download.py  yt-dlp: metadata, format selection, progress parsing
  convert.py   ffmpeg: estimator, command building, two-pass, progress parsing
  media.py     ffprobe wrapper
  db.py        SQLite persistence
  config.py    paths and binary discovery
web/
  index.html   the single page
  app.js       all frontend logic, no dependencies and no build step
  style.css
run.sh         the one command
```

The frontend is plain JavaScript served straight from disk — there is no
bundler, no `node_modules`, and no build step. Asset URLs are stamped with the
file mtime so an edit is picked up on reload instead of being served from cache.

Progress reaches the browser over Server-Sent Events on `/api/events`, which
also carries library and settings invalidations. The REST API is browsable at
<http://127.0.0.1:8723/api/docs>.
