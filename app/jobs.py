"""Server-side job state, subprocess lifecycle and the SSE broadcast hub.

A job is a dict so it can be shipped to the browser and stored in SQLite without
translation. Running jobs live in `_jobs`; every job is also mirrored into the
`jobs` table so history survives a restart.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
import uuid
from typing import Any, Awaitable, Callable

from . import db

# Cap on retained stderr so a chatty failure can't grow without bound. The tail
# is what matters for diagnosing a crash, so we keep the last N lines.
MAX_LOG_LINES = 400

_jobs: dict[str, dict] = {}
_procs: dict[str, asyncio.subprocess.Process] = {}
_tasks: dict[str, asyncio.Task] = {}
_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


# --------------------------------------------------------------- event bus


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def broadcast(event: dict) -> None:
    """Fan an event out to every SSE client. Never blocks; slow clients drop."""
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


# ------------------------------------------------------------------- jobs


def create_job(kind: str, title: str, detail: dict | None = None) -> dict:
    now = time.time()
    job = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "status": "queued",
        "title": title,
        "created_at": now,
        "updated_at": now,
        "progress": 0.0,
        "detail": detail or {},
        "error": None,
        "file_id": None,
        "log": [],
    }
    _jobs[job["id"]] = job
    _persist(job)
    broadcast({"type": "job", "job": public_job(job)})
    return job


def public_job(job: dict) -> dict:
    """The wire form of a job: log tail included, nothing else stripped."""
    out = {k: v for k, v in job.items() if k != "log"}
    out["log"] = "\n".join(job.get("log", [])[-MAX_LOG_LINES:])
    return out


def get_job(job_id: str) -> dict | None:
    job = _jobs.get(job_id)
    if job:
        return job
    stored = db.get_job(job_id)
    if stored:
        stored["log"] = (stored.get("detail") or {}).get("log_tail", "").split("\n")
        return stored
    return None


def all_jobs(limit: int = 100) -> list[dict]:
    """Live jobs first, then history from SQLite, de-duplicated by id."""
    seen = set()
    out = []
    for job in sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True):
        seen.add(job["id"])
        out.append(public_job(job))
    for row in db.list_jobs(limit):
        if row["id"] in seen:
            continue
        row["log"] = (row.get("detail") or {}).get("log_tail", "")
        out.append(row)
    out.sort(key=lambda j: j["created_at"], reverse=True)
    return out[:limit]


def _persist(job: dict) -> None:
    detail = dict(job.get("detail") or {})
    detail["log_tail"] = "\n".join(job.get("log", [])[-MAX_LOG_LINES:])
    db.save_job({**job, "detail": detail})


def update_job(job: dict, **fields: Any) -> None:
    job.update(fields)
    job["updated_at"] = time.time()
    _persist(job)
    broadcast({"type": "job", "job": public_job(job)})


def log_line(job: dict, line: str, *, emit: bool = True) -> None:
    if not line:
        return
    job.setdefault("log", []).append(line.rstrip())
    if len(job["log"]) > MAX_LOG_LINES * 2:
        del job["log"][:-MAX_LOG_LINES]
    if emit:
        broadcast({"type": "log", "job_id": job["id"], "line": line.rstrip()})


# ------------------------------------------------------------ subprocesses


async def spawn(job: dict, argv: list[str], **kwargs) -> asyncio.subprocess.Process:
    """Start a child in its own process group so cancel can kill the whole tree.

    yt-dlp shells out to ffmpeg and ffmpeg spawns nothing, but killing the group
    is what makes cancel reliable for the merge/postprocess phase.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        **kwargs,
    )
    _procs[job["id"]] = proc
    return proc


def _kill_proc(proc: asyncio.subprocess.Process, sig: int) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(sig)


async def cancel_job(job_id: str) -> bool:
    """Kill the running subprocess and mark the job cancelled."""
    job = _jobs.get(job_id)
    if not job or job["status"] not in ("running", "queued"):
        return False

    job["cancelled"] = True
    proc = _procs.get(job_id)
    if proc is not None and proc.returncode is None:
        _kill_proc(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            _kill_proc(proc, signal.SIGKILL)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5)

    task = _tasks.get(job_id)
    if task and not task.done():
        task.cancel()
    return True


def release(job_id: str) -> None:
    _procs.pop(job_id, None)
    _tasks.pop(job_id, None)


def run_job(job: dict, coro_factory: Callable[[], Awaitable[None]]) -> None:
    """Schedule a job body, translating cancellation and crashes into job state."""

    async def runner() -> None:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            update_job(job, status="cancelled", error="Cancelled by user.")
            raise
        except Exception as exc:  # surfaced verbatim in the UI
            log_line(job, f"{type(exc).__name__}: {exc}")
            update_job(job, status="error", error=f"{type(exc).__name__}: {exc}")
        finally:
            release(job["id"])

    task = asyncio.ensure_future(runner())
    _tasks[job["id"]] = task


async def shutdown() -> None:
    for job_id in list(_procs):
        await cancel_job(job_id)
