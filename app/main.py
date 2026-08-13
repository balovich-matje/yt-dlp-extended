"""FastAPI application: REST endpoints plus an SSE stream of job events."""
from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import os
import re
import shutil
import signal
import time
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import convert, db, download, jobs, media
from .config import (
    DEFAULT_SETTINGS, PATH_SETTINGS, WEB_DIR, ensure_dirs,
    ffmpeg_bin, ffprobe_bin, yt_dlp_bin,
)

app = FastAPI(title="yt-dlp-extended", docs_url="/api/docs", openapi_url="/api/openapi.json")


# ------------------------------------------------------------------ models


class MetadataRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    mode: str = "full"                    # full | audio | video
    format_id: str | None = None
    format_has_audio: bool = False
    container: str = "auto"               # merge container for full/video
    audio_format: str = "source"          # for audio mode: source|mp3|m4a|opus|flac|wav
    title: str | None = None
    thumbnail: str | None = None


class EstimateRequest(BaseModel):
    file_id: str
    container: str = "mp4"
    start: str | None = None
    end: str | None = None
    size_mode: str = "quality"            # size | quality
    target_mb: float | None = None
    audio_bitrate: int = 128_000
    crf: int | None = None
    scale_height: int | None = None
    fps: float | None = None


class ConvertRequest(EstimateRequest):
    video_codec: str | None = None
    preset: str = "medium"
    output_name: str | None = None


class SettingsRequest(BaseModel):
    download_dir: str | None = None
    output_dir: str | None = None
    yt_dlp_path: str | None = None
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None


# ------------------------------------------------------------- lifecycle


def _install_shutdown_hook() -> None:
    """Release the SSE streams the moment a stop signal arrives.

    uvicorn waits for in-flight responses *before* running the lifespan shutdown
    handler, and an idle event stream is an in-flight response — so by the time
    `on_shutdown` runs it is already blocked. Chaining onto uvicorn's own signal
    handler lets us close the streams first, turning what would be a
    timeout-forced kill into a clean exit.
    """
    def make(previous):
        def handler(signum, frame):
            jobs.broadcast({"type": "server_shutdown"})
            previous(signum, frame)
        return handler

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(sig)
        # Only wrap a real handler. If uvicorn has not installed one, leaving
        # the default in place is safer than replacing it with ours.
        if callable(previous):
            signal.signal(sig, make(previous))


@app.on_event("startup")
async def on_startup() -> None:
    jobs.set_loop(asyncio.get_running_loop())
    _install_shutdown_hook()
    db.connect()
    db.mark_orphaned_jobs()
    settings = db.get_settings()
    ensure_dirs(Path(settings["download_dir"]), Path(settings["output_dir"]))


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await jobs.shutdown()


# ------------------------------------------------------------- exceptions


@app.exception_handler(convert.ConvertError)
async def convert_error_handler(request: Request, exc: convert.ConvertError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(download.DownloadError)
async def download_error_handler(request: Request, exc: download.DownloadError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(media.ProbeError)
async def probe_error_handler(request: Request, exc: media.ProbeError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ------------------------------------------------------------------ health


@app.get("/api/health")
async def health() -> dict:
    """Report which external binaries actually resolve, for the UI banner."""
    settings = db.get_settings()
    out = {}
    for name, resolver, flag in (
        ("yt-dlp", yt_dlp_bin, "--version"),
        ("ffmpeg", ffmpeg_bin, "-version"),
        ("ffprobe", ffprobe_bin, "-version"),
    ):
        path = resolver(settings)
        resolved = shutil.which(path) or (path if os.path.isabs(path) and os.access(path, os.X_OK) else None)
        version = None
        if resolved:
            try:
                rc, stdout, stderr = await media.run_capture([resolved, flag], timeout=15)
                first = (stdout or stderr).strip().splitlines()
                version = first[0] if first else None
            except Exception:
                version = None
        out[name] = {"path": resolved, "ok": bool(resolved), "version": version}
    return out


# ---------------------------------------------------------------- settings


@app.get("/api/settings")
async def get_settings() -> dict:
    return db.get_settings()


@app.post("/api/settings")
async def update_settings(req: SettingsRequest) -> dict:
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    for key in PATH_SETTINGS:
        if key in updates:
            path = Path(updates[key]).expanduser()
            if not path.is_absolute():
                raise HTTPException(400, f"{key} must be an absolute path.")
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HTTPException(400, f"Cannot use {path} as {key}: {exc}")
            updates[key] = str(path)
    settings = db.set_settings(updates)
    jobs.broadcast({"type": "settings"})
    return settings


# ------------------------------------------------------------ stage 1: DL


@app.post("/api/metadata")
async def metadata(req: MetadataRequest) -> dict:
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "A URL is required.")
    return await download.fetch_metadata(url)


@app.post("/api/download")
async def start_download(req: DownloadRequest) -> dict:
    if not req.url.strip():
        raise HTTPException(400, "A URL is required.")
    if req.mode not in ("full", "audio", "video"):
        raise HTTPException(400, f"Unknown download mode {req.mode!r}.")
    job = download.start_download(req.model_dump())
    return jobs.public_job(job)


# ----------------------------------------------------------------- library


@app.get("/api/library")
async def library() -> list[dict]:
    return db.list_files()


@app.post("/api/library/scan")
async def scan_library() -> dict:
    """Pick up files dropped into the managed directories outside the app."""
    settings = db.get_settings()
    known = {row["path"] for row in db.list_files()}
    added = 0
    media_exts = {
        ".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".ts",
        ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac", ".aac", ".gif",
    }
    for key, origin in (("download_dir", "download"), ("output_dir", "convert")):
        directory = Path(settings[key])
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in media_exts:
                continue
            if str(path) in known:
                continue
            db.add_file(path, origin=origin, title=path.stem)
            added += 1
    removed = db.cleanup_missing_files()
    jobs.broadcast({"type": "library"})
    return {"added": added, "removed": removed}


@app.delete("/api/library/{file_id}")
async def delete_library_file(file_id: str, keep_file: bool = False) -> dict:
    row = db.get_file(file_id)
    if not row:
        raise HTTPException(404, "No such file in the library.")
    deleted = False
    if not keep_file:
        path = Path(row["path"])
        try:
            if path.exists():
                path.unlink()
                deleted = True
        except OSError as exc:
            raise HTTPException(400, f"Could not delete {path}: {exc}")
    db.delete_file_row(file_id)
    jobs.broadcast({"type": "library"})
    return {"removed_from_library": True, "deleted_from_disk": deleted}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    settings = db.get_settings()
    target_dir = Path(settings["download_dir"]) / "uploads"
    ensure_dirs(target_dir)

    safe_name = re.sub(r'[/\\:*?"<>|]', "_", Path(file.filename or "upload").name)
    dest = target_dir / safe_name
    counter = 2
    while dest.exists():
        dest = target_dir / f"{Path(safe_name).stem} ({counter}){Path(safe_name).suffix}"
        counter += 1

    try:
        with dest.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                fh.write(chunk)
    except OSError as exc:
        raise HTTPException(400, f"Could not save upload: {exc}")
    finally:
        await file.close()

    row = db.add_file(dest, origin="upload", title=dest.stem)
    jobs.broadcast({"type": "library"})
    return row


def _require_file(file_id: str) -> dict:
    row = db.get_file(file_id)
    if not row:
        raise HTTPException(404, "No such file in the library.")
    if not Path(row["path"]).exists():
        raise HTTPException(404, f"File is missing from disk: {row['path']}")
    return row


@app.get("/api/files/{file_id}/probe")
async def probe_file(file_id: str) -> dict:
    row = _require_file(file_id)
    info = await media.probe(row["path"])
    info["file_id"] = file_id
    info["slider"] = None
    if info.get("duration"):
        info["slider"] = convert.slider_range(info, info["duration"], 128_000)
    return info


RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


@app.get("/api/files/{file_id}/raw")
async def raw_file(file_id: str, request: Request):
    """Serve media with Range support so the preview player can scrub."""
    row = _require_file(file_id)
    path = Path(row["path"])
    size = path.stat().st_size
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(
            path, media_type=media_type,
            headers={"Accept-Ranges": "bytes", "Content-Length": str(size)},
        )

    match = RANGE_RE.match(range_header)
    if not match:
        raise HTTPException(400, "Malformed Range header.")
    raw_start, raw_end = match.groups()
    if raw_start:
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1
    else:
        # Suffix form: "bytes=-500" means the last 500 bytes.
        length = int(raw_end or 0)
        start = max(0, size - length)
        end = size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        return Response(
            status_code=416, headers={"Content-Range": f"bytes */{size}"}
        )

    chunk_size = 1024 * 512

    def iterator():
        remaining = end - start + 1
        with path.open("rb") as fh:
            fh.seek(start)
            while remaining > 0:
                data = fh.read(min(chunk_size, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(
        iterator(), status_code=206, media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        },
    )


@app.get("/api/files/{file_id}/download")
async def download_file(file_id: str):
    row = _require_file(file_id)
    return FileResponse(row["path"], filename=row["name"])


# ------------------------------------------------------- stage 2: convert


@app.get("/api/containers")
async def containers() -> dict:
    return {
        "containers": {
            key: {
                "label": spec["label"],
                "kind": spec["kind"],
                "crf_default": spec.get("crf_default"),
                "crf_min": spec.get("crf_min"),
                "crf_max": spec.get("crf_max"),
                "video_codecs": spec.get("video_codecs", []),
                "two_pass": spec.get("two_pass", False),
            }
            for key, spec in convert.CONTAINERS.items()
        },
        "presets": convert.PRESETS,
        "min_video_bitrate": convert.MIN_VIDEO_BITRATE,
        "overhead": convert.OVERHEAD,
    }


async def _estimate_payload(req: EstimateRequest) -> dict:
    row = _require_file(req.file_id)
    probe = await media.probe(row["path"])
    start, end = convert.resolve_trim(probe, req.start, req.end)
    duration = end - start
    est = convert.estimate(
        probe,
        container=req.container,
        duration=duration,
        mode=req.size_mode,
        target_mb=req.target_mb,
        audio_bitrate=req.audio_bitrate,
        crf=req.crf,
        scale_height=req.scale_height,
        fps=req.fps,
    )
    est.pop("_passlog", None)
    return {
        "probe": probe,
        "trim": {
            "start": convert.format_timestamp(start),
            "end": convert.format_timestamp(end),
            "duration": duration,
        },
        "estimate": est,
        "slider": convert.slider_range(probe, duration, req.audio_bitrate),
    }


@app.post("/api/estimate")
async def estimate_endpoint(req: EstimateRequest) -> dict:
    return await _estimate_payload(req)


@app.post("/api/convert")
async def start_convert(req: ConvertRequest) -> dict:
    row = _require_file(req.file_id)
    if req.container not in convert.CONTAINERS:
        raise HTTPException(400, f"Unknown container {req.container!r}.")

    # Validate up front so bad input is a 400 rather than a failed job.
    probe = await media.probe(row["path"])
    start, end = convert.resolve_trim(probe, req.start, req.end)
    convert.estimate(
        probe, container=req.container, duration=end - start,
        mode=req.size_mode, target_mb=req.target_mb,
        audio_bitrate=req.audio_bitrate, crf=req.crf,
        scale_height=req.scale_height, fps=req.fps,
    )

    payload = req.model_dump()
    payload["source_path"] = row["path"]
    title = f"{row['name']} → {req.container}"
    job = convert.start_convert(payload, title)
    return jobs.public_job(job)


# -------------------------------------------------------------------- jobs


@app.get("/api/jobs")
async def list_jobs(limit: int = 100) -> list[dict]:
    return jobs.all_jobs(limit)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    return jobs.public_job(job) if "log" in job and isinstance(job["log"], list) else job


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    ok = await jobs.cancel_job(job_id)
    if not ok:
        raise HTTPException(400, "That job is not running.")
    return {"cancelled": True}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    job = jobs.get_job(job_id)
    if job and job.get("status") in ("running", "queued"):
        raise HTTPException(400, "Cancel the job before removing it.")
    db.delete_job(job_id)
    jobs._jobs.pop(job_id, None)
    jobs.broadcast({"type": "jobs_changed"})
    return {"deleted": True}


@app.post("/api/jobs/clear")
async def clear_jobs() -> dict:
    """Drop every finished job from history, leaving running ones alone."""
    cleared = 0
    for job in jobs.all_jobs(1000):
        if job["status"] in ("running", "queued"):
            continue
        db.delete_job(job["id"])
        jobs._jobs.pop(job["id"], None)
        cleared += 1
    jobs.broadcast({"type": "jobs_changed"})
    return {"cleared": cleared}


# --------------------------------------------------------------------- SSE


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    queue = jobs.subscribe()

    async def stream():
        try:
            yield _sse({"type": "hello", "jobs": jobs.all_jobs(50)})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    # Keep proxies and the browser's connection alive.
                    yield ": keepalive\n\n"
                    continue
                if event.get("type") == "server_shutdown":
                    break
                yield _sse(event)
        finally:
            jobs.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


# ---------------------------------------------------------------- frontend


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the SPA, stamping asset URLs so an edited JS/CSS file is picked up.

    Without this the browser happily keeps a cached app.js after an update.
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    stamp = 0
    for asset in ("app.js", "style.css"):
        path = WEB_DIR / asset
        if path.exists():
            stamp = max(stamp, int(path.stat().st_mtime))
    return HTMLResponse(
        html.replace("__ASSET_VERSION__", str(stamp)),
        headers={"Cache-Control": "no-store"},
    )


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
