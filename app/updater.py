"""Keep yt-dlp current.

YouTube changes often, and an old yt-dlp is the usual reason downloads start
failing. This compares the installed version with the latest GitHub release and
updates yt-dlp through whatever installed it — Homebrew, pipx, winget, pip, or
the standalone binary's own self-updater. A package manager has to do the update:
`yt-dlp -U` refuses to touch a copy that a package manager owns.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import shlex
import shutil
import time
import urllib.request
from pathlib import Path

from . import db, jobs
from .config import yt_dlp_bin
from .media import run_capture

RELEASES_URL = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
CHECK_EVERY = 6 * 3600      # how stale the cached latest version may get
AUTO_TICK = 30 * 60         # how often the auto-updater wakes up
FIRST_TICK = 20             # seconds after startup before the first auto check
NOOP_BACKOFF = 24 * 3600    # see _noop below

_latest: dict = {"version": None, "checked_at": 0.0, "error": None}
# Homebrew and winget can lag a GitHub release by a day or two, so an update can
# legitimately change nothing. Remember that per release, so the auto-updater
# does not re-run the package manager every half hour until it catches up.
_noop: dict[str, float] = {}
_auto_task: asyncio.Task | None = None


# ----------------------------------------------------------------- versions


def parse_version(version: str | None) -> tuple[int, ...] | None:
    """yt-dlp versions are dates: 2026.11.23, or 2026.11.23.123456 for nightlies."""
    nums = re.findall(r"\d+", version or "")
    return tuple(int(n) for n in nums) if nums else None


def age_days(version: str | None) -> int | None:
    m = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", version or "")
    if not m:
        return None
    try:
        released = datetime.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None
    return (datetime.date.today() - released).days


async def installed_version(settings: dict) -> str | None:
    try:
        rc, out, _ = await run_capture([yt_dlp_bin(settings), "--version"], timeout=30)
    except Exception:
        return None
    lines = out.strip().splitlines()
    return lines[0].strip() if rc == 0 and lines else None


def _fetch_latest() -> str:
    req = urllib.request.Request(RELEASES_URL, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "yt-dlp-extended",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)["tag_name"]


async def latest_version(force: bool = False) -> dict:
    if force or time.time() - _latest["checked_at"] > CHECK_EVERY:
        try:
            _latest["version"] = await asyncio.to_thread(_fetch_latest)
            _latest["error"] = None
        except Exception as exc:
            # Offline or rate limited: keep the last answer we had.
            _latest["error"] = f"Could not check for updates: {exc}"
        _latest["checked_at"] = time.time()
    return dict(_latest)


# ------------------------------------------------------- install detection


def _display(argv: list[str]) -> str:
    return " ".join([os.path.basename(argv[0])] + [shlex.quote(a) for a in argv[1:]])


def detect_install(binary: str) -> dict:
    """Work out what installed yt-dlp, and therefore how to update it."""
    resolved = shutil.which(binary) or binary
    real = os.path.realpath(resolved)
    low = real.replace("\\", "/").lower()

    def updater(method: str, label: str, argv: list[str] | None, hint: str = "") -> dict:
        return {
            "method": method, "label": label, "argv": argv,
            "command": _display(argv) if argv else "",
            "can_update": bool(argv), "manual_hint": hint, "path": real,
        }

    if "/cellar/yt-dlp/" in low:
        # Use the brew that owns this Cellar, not whichever brew is on PATH.
        prefix = real[: low.index("/cellar/")]
        brew = os.path.join(prefix, "bin", "brew")
        if not os.access(brew, os.X_OK):
            brew = shutil.which("brew")
        return updater("homebrew", "Homebrew", [brew, "upgrade", "yt-dlp"] if brew else None,
                       "" if brew else "Run: brew upgrade yt-dlp")

    if "/pipx/venvs/yt-dlp/" in low:
        pipx = shutil.which("pipx") or str(Path.home() / ".local" / "bin" / "pipx")
        ok = os.access(pipx, os.X_OK)
        return updater("pipx", "pipx", [pipx, "upgrade", "yt-dlp"] if ok else None,
                       "" if ok else "Run: pipx upgrade yt-dlp")

    if "/winget/" in low:
        winget = shutil.which("winget")
        # --disable-interactivity makes winget fail rather than wait on a prompt
        # nobody can answer from here.
        argv = ([winget, "upgrade", "--id", "yt-dlp.yt-dlp", "--exact",
                 "--disable-interactivity"] if winget else None)
        return updater("winget", "winget", argv,
                       "" if winget else "Run: winget upgrade yt-dlp.yt-dlp")

    if low.startswith(("/usr/bin/", "/bin/")):
        return updater("system", "your system package manager", None,
                       "yt-dlp came from your system's package manager, which needs "
                       "admin rights to update — and its copy is usually months old. "
                       "Installing it with pipx instead lets this app keep it current.")

    try:
        with open(real, "rb") as fh:
            head = fh.read(256)
    except OSError:
        head = b""
    if head.startswith(b"#!"):
        # A pip-installed console script names its interpreter in the shebang.
        parts = shlex.split(head[2:].split(b"\n", 1)[0].decode(errors="replace"))
        python = None
        if parts and os.path.basename(parts[0]) == "env" and len(parts) > 1:
            python = shutil.which(parts[1])
        elif parts and "python" in os.path.basename(parts[0]):
            python = parts[0]
        if python:
            return updater("pip", "pip", [python, "-m", "pip", "install", "--upgrade", "yt-dlp"])
        return updater("unknown", "an unrecognised installer", None,
                       "Update yt-dlp the way you installed it.")

    # Anything else is the self-contained release binary, which updates itself.
    return updater("standalone", "the standalone binary", [real, "-U"])


# ------------------------------------------------------------------ status


def _running(kind: str) -> list[dict]:
    return [j for j in jobs._jobs.values()
            if j["kind"] == kind and j["status"] in ("running", "queued")]


def update_in_progress() -> bool:
    return bool(_running("update"))


async def status(force: bool = False) -> dict:
    settings = db.get_settings()
    installed = await installed_version(settings)
    latest = await latest_version(force)
    info = detect_install(yt_dlp_bin(settings))

    inst_v, latest_v = parse_version(installed), parse_version(latest["version"])
    available = bool(inst_v and latest_v and latest_v[:3] > inst_v[:3])
    updating = _running("update")
    return {
        "installed": installed,
        "latest": latest["version"],
        "update_available": available,
        "age_days": age_days(installed),
        "method": info["method"],
        "method_label": info["label"],
        "command": info["command"],
        "can_update": info["can_update"],
        "manual_hint": info["manual_hint"],
        "auto_update": settings.get("ytdlp_auto_update") == "1",
        "checked_at": latest["checked_at"] or None,
        "check_error": latest["error"],
        "updating": updating[0]["id"] if updating else None,
        "busy_downloads": len(_running("download")),
    }


# ------------------------------------------------------------------ update


class UpdateRefused(RuntimeError):
    pass


def start_update(auto: bool = False) -> dict:
    if update_in_progress():
        raise UpdateRefused("yt-dlp is already being updated.")
    busy = len(_running("download"))
    if busy:
        # Replacing yt-dlp under a running download kills that download.
        raise UpdateRefused(
            f"Wait for {busy} running download{'s' if busy != 1 else ''} to finish first.")

    settings = db.get_settings()
    info = detect_install(yt_dlp_bin(settings))
    if not info["can_update"]:
        raise UpdateRefused(info["manual_hint"] or "This yt-dlp cannot be updated from here.")

    job = jobs.create_job(
        "update", "Update yt-dlp" + (" (automatic)" if auto else ""),
        detail={"phase": "Queued", "command": info["command"], "method": info["label"]},
    )
    jobs.run_job(job, lambda: _run_update(job, info))
    return job


async def _run_update(job: dict, info: dict) -> None:
    settings = db.get_settings()
    before = await installed_version(settings)
    target = (await latest_version())["version"]

    jobs.update_job(job, status="running", detail={
        **job["detail"], "phase": f"Updating via {info['label']}", "before": before,
    })
    jobs.log_line(job, f"$ {info['command']}")

    env = dict(os.environ, HOMEBREW_NO_ENV_HINTS="1", NONINTERACTIVE="1")
    proc = await jobs.spawn(job, info["argv"], env=env, stdin=asyncio.subprocess.DEVNULL)
    tail: list[str] = []

    async def pump(stream) -> None:
        async for raw in stream:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                tail.append(line)
                jobs.log_line(job, line)

    await asyncio.gather(pump(proc.stdout), pump(proc.stderr))
    rc = await proc.wait()

    after = await installed_version(settings)
    detail = {**job["detail"], "before": before, "after": after}

    if rc != 0:
        jobs.update_job(job, status="error", detail={**detail, "phase": "Failed"},
                        error=f"{info['command']} exited with code {rc}.\n\n"
                              + "\n".join(tail[-30:]))
    else:
        if after and after != before:
            phase = f"Updated {before} → {after}"
        elif target and parse_version(after) and parse_version(target) \
                and parse_version(target)[:3] > parse_version(after)[:3]:
            _noop[target] = time.time()
            phase = (f"Already the newest {info['label']} offers ({after}). "
                     f"{target} is out on GitHub but has not reached "
                     f"{info['label']} yet; it usually does within a day or two.")
        else:
            phase = f"Already up to date ({after})"
        jobs.update_job(job, status="done", progress=100.0,
                        detail={**detail, "phase": phase})
    jobs.broadcast({"type": "ytdlp"})


# ------------------------------------------------------------ auto-update


async def _auto_tick() -> None:
    if db.get_settings().get("ytdlp_auto_update") != "1":
        return
    st = await status()
    if not (st["update_available"] and st["can_update"]):
        return
    if st["updating"] or st["busy_downloads"]:
        return  # try again on the next tick
    if time.time() - _noop.get(st["latest"], 0) < NOOP_BACKOFF:
        return
    try:
        start_update(auto=True)
    except UpdateRefused:
        pass


async def _auto_loop() -> None:
    await asyncio.sleep(FIRST_TICK)
    while True:
        try:
            await _auto_tick()
        except Exception:
            pass  # a failed check must never take the server down
        await asyncio.sleep(AUTO_TICK)


def start_auto_updater() -> None:
    global _auto_task
    if _auto_task is None:
        _auto_task = asyncio.ensure_future(_auto_loop())


def stop_auto_updater() -> None:
    global _auto_task
    if _auto_task is not None:
        _auto_task.cancel()
        _auto_task = None
