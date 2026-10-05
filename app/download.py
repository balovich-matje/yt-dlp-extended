"""Stage 1: metadata lookup and downloading via yt-dlp."""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import shutil
import tempfile
from pathlib import Path

from . import db, jobs
from .config import ensure_dirs, yt_dlp_bin
from .media import run_capture

# Markers let us pick our structured lines out of yt-dlp's normal chatter,
# since --print and --progress-template both write to stdout.
PROG = "@PROG@"
FILE = "@FILE@"

PP = "@PP@"

PROGRESS_TEMPLATE = (
    "download:" + PROG +
    "%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
    "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|"
    "%(progress.status)s|%(info.format_id)s|"
    # HLS/DASH report no reliable byte total; the fragment counters are the
    # only trustworthy progress signal for them.
    "%(progress.fragment_index)s|%(progress.fragment_count)s"
)
# Quiet mode (forced by --print) also hides the "[Merger]" notices, so ask for
# post-processing progress explicitly to keep the phase label truthful.
POSTPROCESS_TEMPLATE = "postprocess:" + PP + "%(progress.status)s|%(progress.postprocessor)s"
PRINT_TEMPLATE = "after_move:" + FILE + "%(filepath)s"


class DownloadError(RuntimeError):
    pass


# Browsers yt-dlp can pull cookies from directly.
COOKIE_BROWSERS = [
    "brave", "chrome", "chromium", "edge", "firefox",
    "opera", "safari", "vivaldi", "whale",
]


def cookie_args(settings: dict) -> list[str]:
    """Cookie source for yt-dlp, used for age-restricted and private videos.

    An exported cookies.txt wins over live browser extraction: it is explicit,
    and it does not need Keychain or Full Disk Access permission at run time.
    """
    path = (settings.get("cookies_file") or "").strip()
    if path:
        return ["--cookies", path]
    browser = (settings.get("cookies_browser") or "").strip()
    if browser:
        return ["--cookies-from-browser", browser]
    return []


def friendly_error(stderr: str, url: str) -> str:
    """Prepend an explanation to yt-dlp's own message where we can identify it.

    The raw stderr is always kept — it is what makes a failure diagnosable — but
    "No video formats found!" is genuinely confusing on a site like Pinterest,
    where most links are images rather than videos.
    """
    text = (stderr or "").strip()
    if "No video formats found" in text:
        if "pinterest." in url.lower() or "pin.it" in url.lower():
            note = ("This Pinterest pin has no video attached — it is an image "
                    "pin. Open the pin and check it actually plays before "
                    "downloading it.")
        else:
            note = ("That link has no downloadable video stream. It may be an "
                    "image, a text post, or region-locked.")
        return f"{note}\n\n{text}" if text else note
    return text


COOKIE_COUNT_RE = re.compile(r"Extracted (\d+) cookies from (\S+)")


async def check_cookie_source(settings: dict) -> dict:
    """Report whether the configured cookie source actually yields cookies.

    yt-dlp does not treat an unreadable browser cookie store as an error — it
    says "Extracted 0 cookies" and carries on unauthenticated, so a misconfigured
    setting fails silently and only shows up later as a sign-in error. Probing
    with an unsupported URL makes yt-dlp do the extraction and nothing else, so
    this costs no network traffic.
    """
    path = (settings.get("cookies_file") or "").strip()
    if path:
        try:
            lines = [
                ln for ln in Path(path).read_text(errors="replace").splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")
            ]
        except OSError as exc:
            return {"configured": True, "source": f"file: {path}", "ok": False,
                    "count": 0, "message": f"Cookie file could not be read: {exc}"}
        ok = bool(lines)
        return {
            "configured": True, "source": f"file: {path}", "ok": ok,
            "count": len(lines),
            "message": "" if ok else "Cookie file contains no cookie entries.",
        }

    browser = (settings.get("cookies_browser") or "").strip()
    if not browser:
        return {"configured": False, "source": "none", "ok": True, "count": 0,
                "message": ""}

    argv = [yt_dlp_bin(settings), "--cookies-from-browser", browser,
            "--simulate", "--no-color", "ytdlp-cookie-probe://check"]
    try:
        _, out, err = await run_capture(argv, timeout=60)
    except Exception as exc:
        return {"configured": True, "source": f"browser: {browser}", "ok": False,
                "count": 0, "message": f"Could not run the cookie check: {exc}"}

    match = COOKIE_COUNT_RE.search(out + err)
    if not match:
        blob = (out + err).lower()
        if "could not copy" in blob or "permission" in blob or "operation not permitted" in blob:
            return {"configured": True, "source": f"browser: {browser}", "ok": False,
                    "count": 0,
                    "message": f"Cannot read {browser}'s cookie store. On macOS the "
                               "terminal running this app needs Full Disk Access."}
        return {"configured": True, "source": f"browser: {browser}", "ok": False,
                "count": 0, "message": "Could not determine the cookie count."}

    count = int(match.group(1))
    if count:
        return {"configured": True, "source": f"browser: {browser}", "ok": True,
                "count": count, "message": ""}
    return {
        "configured": True, "source": f"browser: {browser}", "ok": False, "count": 0,
        "message": (
            f"yt-dlp read 0 cookies from {browser}, so downloads are running "
            "signed-out and age-restricted videos will still fail. On macOS this "
            "is normally Full Disk Access: grant it to the terminal that runs "
            "this app (System Settings > Privacy & Security > Full Disk Access), "
            "then restart it. Exporting a cookies.txt avoids the permission "
            "entirely."
        ),
    }


# Errors that are worth another attempt: the server rejected or dropped this
# particular connection rather than refusing the content itself.
TRANSIENT_SIGNS = (
    "http error 403", "http error 429", "http error 500", "http error 502",
    "http error 503", "http error 504", "connection reset", "connection aborted",
    "incompleteread", "remote end closed", "timed out", "timeout",
    "temporary failure", "unable to download video data",
)
MAX_DOWNLOAD_ATTEMPTS = 3


def transient_reason(stderr_lines: list[str]) -> str | None:
    """Return a short label when the failure looks worth retrying."""
    blob = "\n".join(stderr_lines[-40:]).lower()
    # Content-level refusals never improve on a retry, whatever else is in the
    # output, so they win over the transient markers.
    for permanent in ("sign in to confirm", "no video formats", "private video",
                      "members-only", "unsupported url", "video unavailable",
                      "removed by the uploader", "is not available"):
        if permanent in blob:
            return None
    for sign in TRANSIENT_SIGNS:
        if sign in blob:
            if "403" in sign:
                return "HTTP 403"
            if "429" in sign:
                return "HTTP 429 (rate limited)"
            if sign.startswith("http error 5"):
                return sign.replace("http error ", "HTTP ")
            return "a dropped connection"
    return None


def _num(value: str):
    """yt-dlp writes 'NA' for anything it doesn't know yet."""
    if value in ("", "NA", "None", "N/A"):
        return None
    try:
        f = float(value)
    except ValueError:
        return None
    return None if f != f else f  # drop NaN


# -------------------------------------------------------------- metadata


def _pick_formats(info: dict) -> list[dict]:
    """Flatten yt-dlp's format list into what the picker needs."""
    out = []
    for f in info.get("formats") or []:
        if f.get("format_id") in (None, ""):
            continue
        # Storyboard/preview streams are not downloadable media.
        if f.get("format_note") == "storyboard" or f.get("ext") == "mhtml":
            continue

        # yt-dlp says "none" when a track is definitely absent and leaves the
        # field null when it simply doesn't know (the generic extractor on a
        # direct file link). Treating unknown as absent would hide the format
        # entirely, so assume the track is present in that case.
        vcodec_raw, acodec_raw = f.get("vcodec"), f.get("acodec")
        has_video = vcodec_raw != "none" if vcodec_raw else True
        has_audio = acodec_raw != "none" if acodec_raw else True
        vcodec = (vcodec_raw or "").split(".")[0] or None
        acodec = (acodec_raw or "").split(".")[0] or None
        out.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "has_video": has_video,
            "has_audio": has_audio,
            "vcodec": vcodec if has_video else None,
            "acodec": acodec if has_audio else None,
            "width": f.get("width"),
            "height": f.get("height"),
            "fps": f.get("fps"),
            "tbr": f.get("tbr"),
            "abr": f.get("abr"),
            "vbr": f.get("vbr"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "filesize_is_estimate": not f.get("filesize") and bool(f.get("filesize_approx")),
            "format_note": f.get("format_note"),
            "resolution": f.get("resolution") or (
                f"{f.get('width')}x{f.get('height')}" if f.get("width") else None
            ),
            "dynamic_range": f.get("dynamic_range"),
            "protocol": f.get("protocol"),
        })

    def sort_key(f):
        return (
            f["height"] or 0,
            f["fps"] or 0,
            f["tbr"] or f["abr"] or 0,
        )

    out.sort(key=sort_key, reverse=True)
    return out


async def fetch_metadata(url: str) -> dict:
    """`yt-dlp -J` for a single video, normalised for the UI."""
    settings = db.get_settings()
    argv = [
        yt_dlp_bin(settings), "-J", "--no-playlist",
        # A board/channel URL is a playlist extractor, which --no-playlist does
        # not bound: yt-dlp would resolve every entry before answering. We only
        # ever show the first one, so ask for exactly that.
        "--playlist-items", "1",
        "--no-warnings", "--no-color",
        *cookie_args(settings),
        url,
    ]
    rc, out, err = await run_capture(argv, timeout=180)
    if rc != 0:
        raise DownloadError(
            friendly_error(err, url) or f"yt-dlp exited with code {rc}"
        )
    try:
        info = json.loads(out)
    except json.JSONDecodeError as exc:
        raise DownloadError(f"Could not parse yt-dlp metadata: {exc}\n{err.strip()}")

    # A playlist URL still yields a playlist object even with --no-playlist.
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if not entries:
            raise DownloadError("That URL is a playlist with no playable entries.")
        info = entries[0]

    return {
        "url": info.get("webpage_url") or url,
        "id": info.get("id"),
        "title": info.get("title") or "(untitled)",
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "extractor": info.get("extractor_key"),
        "description": (info.get("description") or "")[:1000],
        "formats": _pick_formats(info),
        "stderr": err.strip(),
    }


# -------------------------------------------------------------- downloading


def build_argv(
    settings: dict,
    url: str,
    mode: str,
    format_id: str | None,
    format_has_audio: bool,
    container: str,
    audio_format: str,
    download_dir: Path,
    temp_dir: Path,
) -> tuple[list[str], int]:
    """Assemble the yt-dlp command. Returns (argv, expected stream count)."""
    fid = (format_id or "").strip()
    streams = 1

    if mode == "audio":
        selector = fid or "bestaudio/best"
    elif mode == "video":
        selector = fid or "bestvideo/best"
    else:  # full
        if not fid:
            selector = "bestvideo*+bestaudio/best"
            streams = 2
        elif format_has_audio:
            # Progressive format already carries both tracks.
            selector = fid
        else:
            selector = f"{fid}+bestaudio/{fid}"
            streams = 2

    argv = [
        yt_dlp_bin(settings),
        # --print implies quiet, which also silences the progress readout;
        # --progress forces it back on so we can parse it.
        "--no-playlist", "--playlist-items", "1",
        "--no-color", "--newline", "--no-simulate", "--progress",
        "--progress-template", PROGRESS_TEMPLATE,
        "--progress-template", POSTPROCESS_TEMPLATE,
        "--print", PRINT_TEMPLATE,
        # Fragments and .part files go to a per-job scratch dir, so cancelling
        # one download can never disturb another's partial files.
        "-P", f"home:{download_dir}",
        "-P", f"temp:{temp_dir}",
        "-o", "%(title).180B [%(id)s].%(ext)s",
        "-f", selector,
        # yt-dlp retries 5xx and fragment errors itself. It deliberately does
        # not retry a 4xx (see downloader/http.py: "err.status < 500 ... raise"),
        # so an intermittent 403 is handled by retrying the whole command below.
        "--retries", "10",
        "--fragment-retries", "10",
        *cookie_args(settings),
    ]

    if mode == "audio":
        if audio_format and audio_format != "source":
            argv += ["-x", "--audio-format", audio_format, "--audio-quality", "0"]
    else:
        if container and container != "auto":
            argv += ["--merge-output-format", container]

    argv.append(url)
    return argv, streams


PP_LABELS = {
    "Merger": "Merging streams",
    "ExtractAudio": "Extracting audio",
    "MoveFiles": "Moving into place",
    "FixupM4a": "Fixing up container",
    "FixupM3u8": "Fixing up container",
    "VideoConvertor": "Converting",
    "EmbedThumbnail": "Embedding thumbnail",
}


def _consume_marker(job: dict, line: str, state: dict) -> bool:
    """Handle one of our template markers. Returns True if the line was ours.

    yt-dlp splits its output across both pipes — download progress arrives on
    stdout while post-processor progress arrives on stderr — so both pumps feed
    every line through here before falling back to plain logging.
    """
    if line.startswith(PROG):
        _handle_progress(job, line[len(PROG):], state)
        return True

    if line.startswith(PP):
        status, _, name = line[len(PP):].partition("|")
        label = PP_LABELS.get(name)
        if not label:
            label = f"Post-processing ({name})" if name and name != "NA" \
                    else "Post-processing"
        _set_phase(job, state, label)
        return True

    if line.startswith(FILE):
        path = line[len(FILE):].strip()
        if path:
            state["files"].append(path)
        jobs.log_line(job, f"[output] {path}")
        return True

    return False


async def _pump_stdout(job: dict, proc, state: dict) -> None:
    """Parse progress markers, filepath markers and phase lines from stdout."""
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line or _consume_marker(job, line, state):
            continue

        jobs.log_line(job, line)
        # Phase hints from yt-dlp's ordinary chatter, when it isn't suppressed.
        if line.startswith("[download] Destination:"):
            _set_phase(job, state, "Downloading")


def _set_phase(job: dict, state: dict, phase: str) -> None:
    if state.get("phase") == phase:
        return
    state["phase"] = phase
    detail = dict(job.get("detail") or {})
    detail["phase"] = phase
    jobs.update_job(job, detail=detail)


def _handle_progress(job: dict, payload: str, state: dict) -> None:
    parts = payload.split("|")
    if len(parts) < 9:
        return
    (downloaded, total, total_est, speed, eta, status, fmt_id,
     frag_index, frag_count) = parts[:9]

    downloaded_b = _num(downloaded)
    total_known = _num(total)              # a real content-length
    total_guess = _num(total_est)          # a running guess, and a jumpy one
    frag_idx, frag_cnt = _num(frag_index), _num(frag_count)

    # yt-dlp downloads each selected stream in turn, restarting at 0. Spread the
    # overall bar across the streams we expect so it never runs backwards.
    if fmt_id and fmt_id != state.get("current_format"):
        if state.get("current_format") is not None:
            state["done_streams"] += 1
        state["current_format"] = fmt_id
        state["stream_started"] = asyncio.get_event_loop().time()

    # Order matters. A known byte total is the smoothest signal, but a
    # fragmented stream has none — and its total_bytes_estimate swings by a
    # factor of two as segments arrive, which made the bar lurch. Fragment
    # counters are exact there, so they come before the guess.
    if downloaded_b is not None and total_known:
        frac = downloaded_b / total_known
    elif frag_cnt and frag_idx is not None:
        frac = frag_idx / frag_cnt
    elif downloaded_b is not None and total_guess:
        frac = downloaded_b / total_guess
    else:
        frac = 0.0
    frac = max(0.0, min(1.0, frac))
    total_b = total_known or total_guess

    if state.get("phase") in (None, "Starting", "Queued"):
        _set_phase(job, state, "Downloading")

    total_streams = max(1, state["total_streams"])
    overall = (state["done_streams"] + frac) / total_streams
    # Hold short of 100% while the process is alive: merging and moving happen
    # after the last byte arrives, and a full bar there reads as "finished".
    overall = max(0.0, min(0.99, overall)) * 100.0
    # Guard against a mid-download stream count surprise.
    state["last_progress"] = max(state.get("last_progress", 0.0), overall)

    speed_b = _num(speed)
    eta_s = _num(eta)

    now = asyncio.get_event_loop().time()
    if eta_s is None and frac > 0.02:
        # Fragmented downloads report eta=NA. Extrapolating from elapsed time is
        # rough, but a moving estimate beats a permanently empty field.
        started = state.get("stream_started")
        if started:
            elapsed = now - started
            if elapsed > 1:
                eta_s = max(0.0, elapsed * (1 - frac) / frac)
    detail = dict(job.get("detail") or {})
    detail.update({
        "phase": state.get("phase") or "Downloading",
        "downloaded_bytes": downloaded_b,
        "total_bytes": total_b,
        "speed": speed_b,
        "eta": eta_s,
        "stream": fmt_id,
        "stream_index": state["done_streams"] + 1,
        "stream_count": total_streams,
        "stream_percent": frac * 100.0,
        "status": status,
        "fragment_index": frag_idx,
        "fragment_count": frag_cnt,
    })
    job["detail"] = detail
    job["progress"] = state["last_progress"]

    # Throttle the SSE fan-out; yt-dlp emits progress far faster than anyone reads.
    if now - state.get("last_emit", 0) >= 0.25 or frac >= 1.0:
        state["last_emit"] = now
        jobs.update_job(job, detail=detail, progress=state["last_progress"])


async def _pump_stderr(job: dict, proc, state: dict) -> None:
    assert proc.stderr is not None
    async for raw in proc.stderr:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line or _consume_marker(job, line, state):
            continue
        # Only genuine stderr chatter counts towards the error message shown on
        # failure; our own progress markers must not end up in it.
        state["stderr"].append(line)
        jobs.log_line(job, line)


async def run_download(job: dict, req: dict) -> None:
    settings = db.get_settings()
    download_dir = Path(settings["download_dir"]).expanduser()
    ensure_dirs(download_dir)
    temp_dir = Path(tempfile.mkdtemp(prefix="ytx-dl-"))

    argv, streams = build_argv(
        settings,
        url=req["url"],
        mode=req.get("mode", "full"),
        format_id=req.get("format_id"),
        format_has_audio=bool(req.get("format_has_audio")),
        container=req.get("container", "auto"),
        audio_format=req.get("audio_format", "source"),
        download_dir=download_dir,
        temp_dir=temp_dir,
    )

    state = {
        "files": [], "stderr": [], "total_streams": streams,
        "done_streams": 0, "current_format": None,
        "last_emit": 0.0, "last_progress": 0.0, "phase": "Starting",
    }

    detail = dict(job.get("detail") or {})
    detail["command"] = " ".join(shlex.quote(a) for a in argv)
    jobs.update_job(job, status="running", detail=detail)
    jobs.log_line(job, f"$ {detail['command']}")

    try:
        # yt-dlp will not retry a 403 itself, and YouTube issues them
        # intermittently part way through large downloads. Re-running the same
        # command resumes from the partial file in temp_dir rather than starting
        # over, so another attempt is cheap.
        for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
            state["stderr"] = []
            state["files"] = []
            state["current_format"] = None
            state["done_streams"] = 0

            if attempt > 1:
                jobs.log_line(job, f"--- attempt {attempt} of {MAX_DOWNLOAD_ATTEMPTS} ---")

            proc = await jobs.spawn(job, argv)
            await asyncio.gather(
                _pump_stdout(job, proc, state),
                _pump_stderr(job, proc, state),
            )
            rc = await proc.wait()

            if job.get("cancelled"):
                jobs.update_job(job, status="cancelled", error="Cancelled by user.",
                                progress=job["progress"])
                return

            if rc == 0:
                break

            reason = transient_reason(state["stderr"])
            if reason and attempt < MAX_DOWNLOAD_ATTEMPTS:
                _set_phase(job, state,
                           f"{reason} — retrying ({attempt + 1} of "
                           f"{MAX_DOWNLOAD_ATTEMPTS})")
                jobs.log_line(job, f"{reason}; resuming and retrying.")
                await asyncio.sleep(2 * attempt)
                continue

            raw = "\n".join(state["stderr"][-40:])
            msg = friendly_error(raw, req["url"]) or f"yt-dlp exited with code {rc}"
            if reason:
                msg = (f"Failed after {MAX_DOWNLOAD_ATTEMPTS} attempts "
                       f"({reason} each time).\n\n{msg}")
            jobs.update_job(job, status="error", error=msg)
            return

        # --print after_move fires once per output file; the last one is the
        # final muxed result when a merge happened.
        final = state["files"][-1] if state["files"] else None
        if not final or not Path(final).exists():
            jobs.update_job(
                job, status="error",
                error="yt-dlp finished but no output file was reported.\n"
                      + "\n".join(state["stderr"][-40:]),
            )
            return

        row = db.add_file(
            Path(final),
            origin="download",
            title=req.get("title") or Path(final).stem,
            source_url=req["url"],
            job_id=job["id"],
        )
        detail = dict(job.get("detail") or {})
        detail.update({"phase": "Complete", "output_path": final,
                       "output_size": row["size"]})
        jobs.update_job(job, status="done", progress=100.0, detail=detail,
                        file_id=row["id"])
        jobs.broadcast({"type": "library"})
    finally:
        # Runs even when cancellation unwinds the awaits above.
        shutil.rmtree(temp_dir, ignore_errors=True)


def start_download(req: dict) -> dict:
    title = req.get("title") or req["url"]
    job = jobs.create_job("download", title, detail={
        "url": req["url"],
        "mode": req.get("mode", "full"),
        "format_id": req.get("format_id"),
        "phase": "Queued",
        "thumbnail": req.get("thumbnail"),
    })
    jobs.run_job(job, lambda: run_download(job, req))
    return job
