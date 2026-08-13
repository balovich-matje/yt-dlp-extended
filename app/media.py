"""ffprobe helpers: inspect a media file and describe it for the estimator."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from . import db
from .config import ffprobe_bin


class ProbeError(RuntimeError):
    pass


async def run_capture(argv: list[str], timeout: float = 120) -> tuple[int, str, str]:
    """Run a command to completion, returning (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ProbeError(f"`{argv[0]}` timed out after {timeout:.0f}s")
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _to_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fps(rate: str | None) -> float | None:
    """Parse ffprobe's rational frame rate ("30000/1001")."""
    if not rate or rate == "0/0":
        return None
    try:
        num, _, den = rate.partition("/")
        den_f = float(den) if den else 1.0
        return float(num) / den_f if den_f else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


async def probe(path: str | Path) -> dict:
    """Return a normalised description of a media file.

    Raises ProbeError with ffprobe's own stderr so failures stay legible.
    """
    path = Path(path)
    if not path.exists():
        raise ProbeError(f"File not found: {path}")

    settings = db.get_settings()
    argv = [
        ffprobe_bin(settings), "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    rc, out, err = await run_capture(argv)
    if rc != 0:
        raise ProbeError(err.strip() or f"ffprobe exited {rc}")

    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"Could not parse ffprobe output: {exc}")

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    # Some containers only carry duration on the stream, not the format.
    duration = _to_float(fmt.get("duration"))
    if duration is None and video:
        duration = _to_float(video.get("duration"))
    if duration is None and audio:
        duration = _to_float(audio.get("duration"))

    size = _to_float(fmt.get("size"))
    if size is None and path.exists():
        size = float(path.stat().st_size)

    info = {
        "path": str(path),
        "name": path.name,
        "duration": duration,
        "size": int(size) if size else None,
        "format_name": fmt.get("format_name"),
        "overall_bitrate": _to_float(fmt.get("bit_rate")),
        "has_video": video is not None,
        "has_audio": audio is not None,
        "video": None,
        "audio": None,
    }

    if video:
        info["video"] = {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": _fps(video.get("avg_frame_rate")) or _fps(video.get("r_frame_rate")),
            "bitrate": _to_float(video.get("bit_rate")),
            "pix_fmt": video.get("pix_fmt"),
        }
    if audio:
        info["audio"] = {
            "codec": audio.get("codec_name"),
            "channels": audio.get("channels"),
            "sample_rate": _to_float(audio.get("sample_rate")),
            "bitrate": _to_float(audio.get("bit_rate")),
        }

    # A still image (cover art, PNG) probes as a video stream with no duration;
    # treat it as unusable rather than letting the estimator divide by zero.
    if not info["duration"] or info["duration"] <= 0:
        info["duration"] = None

    return info
