"""Stage 2: trimming, transcoding and the size estimator.

The estimator here is the single source of truth: the UI's live prediction and
the bitrate actually handed to ffmpeg both come from `estimate()`, so what the
panel promises is what gets encoded.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path

from . import db, jobs, media
from .config import ensure_dirs, ffmpeg_bin

# Muxing overhead we hold back from the bitrate budget.
OVERHEAD = 0.02
# Below this, x264/VP9 output is mush regardless of settings.
MIN_VIDEO_BITRATE = 100_000
MIN_AUDIO_BITRATE = 32_000
MAX_AUDIO_BITRATE = 320_000

# Bits per pixel per frame, the usual yardstick for "will this look bad".
BPP_SEVERE = 0.025
BPP_POOR = 0.06

CONTAINERS: dict[str, dict] = {
    "mp4": {
        "kind": "video", "label": "MP4 (H.264 + AAC)",
        "vcodec": "libx264", "acodec": "aac",
        "crf_default": 23, "crf_min": 0, "crf_max": 51,
        "video_codecs": ["libx264", "libx265"], "two_pass": True,
    },
    "mkv": {
        "kind": "video", "label": "MKV (H.264 + AAC)",
        "vcodec": "libx264", "acodec": "aac",
        "crf_default": 23, "crf_min": 0, "crf_max": 51,
        "video_codecs": ["libx264", "libx265"], "two_pass": True,
    },
    "webm": {
        "kind": "video", "label": "WebM (VP9 + Opus)",
        "vcodec": "libvpx-vp9", "acodec": "libopus",
        "crf_default": 32, "crf_min": 0, "crf_max": 63,
        "video_codecs": ["libvpx-vp9"], "two_pass": True,
    },
    "mp3": {
        "kind": "audio", "label": "MP3 (audio only)",
        "acodec": "libmp3lame", "two_pass": False,
    },
    "m4a": {
        "kind": "audio", "label": "M4A (AAC audio only)",
        "acodec": "aac", "two_pass": False,
    },
    "gif": {
        "kind": "gif", "label": "GIF (animated, no audio)",
        "two_pass": False,
    },
}

PRESETS = ["ultrafast", "veryfast", "faster", "fast", "medium", "slow", "slower"]

# ffmpeg's native AAC encoder saturates around this per channel: asking a mono
# stream for 192k yields ~129k. Model the ceiling so predictions don't overshoot.
AAC_MAX_BITRATE_PER_CHANNEL = 128_000


def _effective_audio_bitrate(spec: dict, probe: dict, requested: int) -> tuple[int, str | None]:
    """Clamp a requested audio bitrate to what the encoder will actually deliver."""
    if spec.get("acodec") != "aac":
        return requested, None
    channels = ((probe.get("audio") or {}).get("channels")) or 2
    ceiling = AAC_MAX_BITRATE_PER_CHANNEL * int(channels)
    if requested <= ceiling:
        return requested, None
    return ceiling, (
        f"ffmpeg's AAC encoder tops out near {ceiling // 1000} kbps for "
        f"{channels}-channel audio, so {requested // 1000} kbps was reduced to that."
    )


class ConvertError(RuntimeError):
    pass


# ------------------------------------------------------------ time parsing

TS_RE = re.compile(r"^(?:(\d+):)?([0-5]?\d):([0-5]?\d)(?:[.,](\d{1,3}))?$")


def parse_timestamp(value: str | None) -> float | None:
    """HH:MM:SS.mmm -> seconds. Also accepts MM:SS and bare seconds."""
    if value is None:
        return None
    value = str(value).strip()
    if value == "":
        return None
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)
    m = TS_RE.match(value)
    if not m:
        raise ConvertError(f"Invalid timestamp {value!r}. Use HH:MM:SS.mmm")
    hours, minutes, seconds, millis = m.groups()
    total = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
    if millis:
        total += int(millis.ljust(3, "0")) / 1000.0
    return float(total)


def format_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"


def resolve_trim(probe: dict, start_raw, end_raw) -> tuple[float, float]:
    """Validate the trim window against the real source duration."""
    source_duration = probe.get("duration")
    start = parse_timestamp(start_raw) or 0.0
    end = parse_timestamp(end_raw)

    if source_duration is None:
        if end is None:
            raise ConvertError(
                "The source duration is unknown, so an end timestamp is required."
            )
    else:
        if end is None:
            end = source_duration
        if start >= source_duration:
            raise ConvertError(
                f"Start {format_timestamp(start)} is at or past the source duration "
                f"({format_timestamp(source_duration)})."
            )
        # Allow a hair of slop; container durations are approximate.
        if end > source_duration + 0.25:
            raise ConvertError(
                f"End {format_timestamp(end)} is past the source duration "
                f"({format_timestamp(source_duration)})."
            )
        end = min(end, source_duration)

    if start < 0:
        raise ConvertError("Start timestamp cannot be negative.")
    if end <= start:
        raise ConvertError(
            f"End ({format_timestamp(end)}) must be after start ({format_timestamp(start)})."
        )
    return start, end


# --------------------------------------------------------------- estimator


def _scaled_dimensions(probe: dict, target_height: int | None) -> tuple[int | None, int | None]:
    video = probe.get("video") or {}
    width, height = video.get("width"), video.get("height")
    if not width or not height:
        return None, None
    if not target_height or target_height >= height:
        return width, height
    scaled_w = int(round(width * target_height / height / 2)) * 2
    return max(2, scaled_w), target_height


def estimate(
    probe: dict,
    *,
    container: str,
    duration: float,
    mode: str,
    target_mb: float | None,
    audio_bitrate: int,
    crf: int | None,
    scale_height: int | None,
    fps: float | None,
) -> dict:
    """Predict output size / bitrate and flag quality risks.

    `mode` is "size" (two-pass to a byte target) or "quality" (single-pass CRF).
    """
    spec = CONTAINERS.get(container)
    if spec is None:
        raise ConvertError(f"Unknown container {container!r}")

    warnings: list[str] = []
    notes: list[str] = []
    has_audio = bool(probe.get("has_audio"))
    kind = spec["kind"]

    if duration <= 0:
        raise ConvertError("Trimmed duration must be greater than zero.")

    audio_bps = 0 if (kind == "gif" or not has_audio) else int(audio_bitrate)
    if kind == "audio" and not has_audio:
        raise ConvertError("The source has no audio track, so an audio-only output is empty.")

    if audio_bps:
        audio_bps, capped_note = _effective_audio_bitrate(spec, probe, audio_bps)
        if capped_note:
            notes.append(capped_note)

    out_w, out_h = _scaled_dimensions(probe, scale_height)
    src_fps = ((probe.get("video") or {}).get("fps")) or 30.0
    out_fps = fps or src_fps

    result = {
        "mode": mode,
        "container": container,
        "duration": duration,
        "audio_bitrate": audio_bps,
        "video_bitrate": None,
        "predicted_bytes": None,
        "target_bytes": None,
        "clamped": False,
        "bpp": None,
        "quality": "ok",
        "warnings": warnings,
        "notes": notes,
        "width": out_w,
        "height": out_h,
        "fps": out_fps,
    }

    # ---- GIF: size targeting is not meaningful, so report a rough figure only.
    if kind == "gif":
        if out_w and out_h:
            # ~1 byte per pixel per frame after LZW on typical footage; crude but
            # better than showing nothing.
            rough = out_w * out_h * out_fps * duration * 0.35
            result["predicted_bytes"] = int(rough)
        notes.append(
            "GIF size is dominated by resolution and fps, not bitrate. "
            "Use the scale and fps overrides to control it."
        )
        result["quality"] = "ok"
        return result

    # ---- Audio-only: the target maps straight onto the audio bitrate.
    if kind == "audio":
        if mode == "size" and target_mb:
            target_bytes = int(target_mb * 1_000_000)
            raw = (target_bytes * 8 / duration) * (1 - OVERHEAD)
            chosen = int(max(MIN_AUDIO_BITRATE, min(MAX_AUDIO_BITRATE, raw)))
            if chosen > raw:
                result["clamped"] = True
                warnings.append(
                    f"Target is below the {MIN_AUDIO_BITRATE // 1000} kbps floor; "
                    "the file will come out larger than requested."
                )
            elif chosen < raw:
                notes.append(
                    f"Capped at {MAX_AUDIO_BITRATE // 1000} kbps; the file will be "
                    "smaller than the target."
                )
            audio_bps = chosen
            result["target_bytes"] = target_bytes

        # Re-apply the encoder ceiling: a size target can ask for more than the
        # codec will produce, and the prediction has to match reality.
        audio_bps, capped_note = _effective_audio_bitrate(spec, probe, audio_bps)
        if capped_note and capped_note not in notes:
            notes.append(capped_note)
        result["audio_bitrate"] = audio_bps
        result["predicted_bytes"] = int(audio_bps * duration / 8 / (1 - OVERHEAD))
        if audio_bps < 96_000:
            result["quality"] = "warn"
            warnings.append(f"{audio_bps // 1000} kbps audio will sound noticeably compressed.")
        return result

    # ---- Video containers.
    if mode == "quality":
        result["crf"] = crf if crf is not None else spec["crf_default"]
        notes.append(
            "CRF mode targets a visual quality level, so the final size is not "
            "known until the encode finishes."
        )
        # A rough guide so the panel isn't empty: scale the source bitrate by the
        # pixel-count change. Deliberately labelled as approximate in the UI.
        src_bps = (probe.get("video") or {}).get("bitrate") or probe.get("overall_bitrate")
        if src_bps and out_w and out_h:
            src_v = probe.get("video") or {}
            src_pixels = (src_v.get("width") or out_w) * (src_v.get("height") or out_h)
            ratio = (out_w * out_h) / src_pixels if src_pixels else 1.0
            approx_v = float(src_bps) * ratio
            result["video_bitrate"] = int(approx_v)
            result["predicted_bytes"] = int((approx_v + audio_bps) * duration / 8 / (1 - OVERHEAD))
            result["approximate"] = True
        return result

    # ---- Size-targeted two-pass.
    if not target_mb or target_mb <= 0:
        raise ConvertError("A target size in MB is required in size mode.")

    target_bytes = int(target_mb * 1_000_000)
    result["target_bytes"] = target_bytes

    budget_bps = target_bytes * 8 / duration
    available_video = budget_bps * (1 - OVERHEAD) - audio_bps

    video_bps = available_video
    if video_bps < MIN_VIDEO_BITRATE:
        video_bps = MIN_VIDEO_BITRATE
        result["clamped"] = True
        warnings.append(
            f"A {target_mb:.1f} MB target over {duration:.1f}s leaves "
            f"{max(0, int(available_video)) // 1000} kbps for video. Clamped to the "
            f"{MIN_VIDEO_BITRATE // 1000} kbps floor — the output will exceed the target."
        )
        if audio_bps and available_video < 0:
            warnings.append(
                f"The {audio_bps // 1000} kbps audio track alone already exceeds the "
                "target. Lower the audio bitrate or raise the target."
            )

    video_bps = int(video_bps)
    result["video_bitrate"] = video_bps
    result["predicted_bytes"] = int((video_bps + audio_bps) * duration / 8 / (1 - OVERHEAD))

    if out_w and out_h and out_fps:
        bpp = video_bps / (out_w * out_h * out_fps)
        result["bpp"] = bpp
        if bpp < BPP_SEVERE:
            result["quality"] = "severe"
            warnings.append(
                f"About {bpp:.3f} bits per pixel — expect heavy blocking and smearing. "
                f"Scale down to a smaller resolution or raise the target."
            )
        elif bpp < BPP_POOR:
            result["quality"] = "warn"
            warnings.append(
                f"About {bpp:.3f} bits per pixel — visible artifacts on detailed or "
                "fast-moving scenes."
            )

    return result


def slider_range(probe: dict, duration: float, audio_bitrate: int) -> dict:
    """Floor from a very-low-bitrate encode, ceiling from the source's own rate."""
    has_audio = bool(probe.get("has_audio"))
    audio_bps = audio_bitrate if has_audio else 0

    floor_bytes = (MIN_VIDEO_BITRATE + audio_bps) * duration / 8
    floor_mb = max(0.1, math.floor(floor_bytes / 1_000_000 * 10) / 10)

    src_duration = probe.get("duration") or duration
    src_size = probe.get("size") or 0
    if src_size and src_duration:
        ceiling_mb = src_size * (duration / src_duration) / 1_000_000
    else:
        bitrate = probe.get("overall_bitrate") or 5_000_000
        ceiling_mb = bitrate * duration / 8 / 1_000_000
    ceiling_mb = math.ceil(max(ceiling_mb, floor_mb * 4) * 10) / 10

    default_mb = round(min(ceiling_mb, max(floor_mb, ceiling_mb * 0.5)), 1)
    step = 0.1 if ceiling_mb <= 20 else (1.0 if ceiling_mb <= 500 else 5.0)
    return {"min": floor_mb, "max": ceiling_mb, "default": default_mb, "step": step}


# ---------------------------------------------------------- ffmpeg commands


def _filters(container: str, scale_height: int | None, fps: float | None,
             probe: dict) -> str | None:
    spec = CONTAINERS[container]
    chain: list[str] = []
    if fps:
        chain.append(f"fps={fps:g}")

    src_h = (probe.get("video") or {}).get("height")
    if scale_height and (not src_h or scale_height < src_h):
        if spec["kind"] == "gif":
            chain.append(f"scale=-2:{scale_height}:flags=lanczos")
        else:
            chain.append(f"scale=-2:{scale_height}")

    if spec["kind"] == "gif":
        base = ",".join(chain) if chain else "null"
        # Per-clip palette gives far better colour than the default 216-colour cube.
        return f"{base},split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3"

    return ",".join(chain) if chain else None


def build_commands(
    src: str,
    dest: str,
    *,
    container: str,
    start: float,
    duration: float,
    est: dict,
    probe: dict,
    video_codec: str | None,
    preset: str,
    scale_height: int | None,
    fps: float | None,
    settings: dict,
) -> list[list[str]]:
    """Return one command (single pass) or two (two-pass), in run order."""
    spec = CONTAINERS[container]
    ffmpeg = ffmpeg_bin(settings)
    kind = spec["kind"]
    vf = _filters(container, scale_height, fps, probe)

    def head() -> list[str]:
        # Input seek is fast and, with a re-encode, frame-accurate. `-t` after the
        # input avoids the ambiguity of `-to` on a seeked stream.
        return [
            ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-ss", f"{start:.3f}", "-i", src, "-t", f"{duration:.3f}",
            "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        ]

    # ---- audio-only
    if kind == "audio":
        cmd = head() + ["-vn", "-c:a", spec["acodec"], "-b:a", str(est["audio_bitrate"])]
        return [cmd + [dest]]

    # ---- gif
    if kind == "gif":
        cmd = head() + ["-an"]
        if vf:
            cmd += ["-filter_complex", vf]
        cmd += ["-loop", "0", dest]
        return [cmd]

    # ---- video
    vcodec = video_codec or spec["vcodec"]
    if vcodec not in spec["video_codecs"]:
        vcodec = spec["vcodec"]

    has_audio = bool(probe.get("has_audio"))

    def audio_args() -> list[str]:
        if not has_audio:
            return ["-an"]
        return ["-c:a", spec["acodec"], "-b:a", str(est["audio_bitrate"])]

    def video_common() -> list[str]:
        args = ["-c:v", vcodec]
        if vcodec in ("libx264", "libx265"):
            args += ["-preset", preset, "-pix_fmt", "yuv420p"]
        elif vcodec == "libvpx-vp9":
            args += ["-row-mt", "1", "-pix_fmt", "yuv420p"]
        if vf:
            args += ["-vf", vf]
        return args

    container_args = ["-movflags", "+faststart"] if container == "mp4" else []

    if est["mode"] == "quality":
        crf = est.get("crf", spec["crf_default"])
        cmd = head() + video_common() + ["-crf", str(crf)]
        if vcodec == "libvpx-vp9":
            cmd += ["-b:v", "0"]
        cmd += audio_args() + container_args + [dest]
        return [cmd]

    # Two-pass, size-targeted.
    bitrate = str(est["video_bitrate"])
    passlog = str(Path(tempfile.mkdtemp(prefix="ytx-2pass-")) / "ffmpeg2pass")
    est["_passlog"] = passlog

    if vcodec == "libx265":
        p1_extra = ["-x265-params", f"pass=1:stats={passlog}.log"]
        p2_extra = ["-x265-params", f"pass=2:stats={passlog}.log"]
    else:
        p1_extra = ["-pass", "1", "-passlogfile", passlog]
        p2_extra = ["-pass", "2", "-passlogfile", passlog]

    pass1 = (
        head() + video_common() + ["-b:v", bitrate] + p1_extra
        + ["-an", "-f", "null", os.devnull]
    )
    if vcodec == "libvpx-vp9":
        # VP9's analysis pass is slow at default speed and gains nothing from it.
        pass1 += ["-speed", "4"]

    pass2 = (
        head() + video_common() + ["-b:v", bitrate] + p2_extra
        + audio_args() + container_args + [dest]
    )
    return [pass1, pass2]


# ------------------------------------------------------------ progress pump

TIME_KEYS = ("out_time_us", "out_time_ms")


async def _pump_progress(job: dict, proc, duration: float, state: dict) -> None:
    """Parse ffmpeg's key=value -progress stream."""
    assert proc.stdout is not None
    fields: dict[str, str] = {}
    async for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key] = value
        if key != "progress":
            continue

        seconds = None
        for tk in TIME_KEYS:
            if tk in fields:
                try:
                    raw_v = float(fields[tk])
                except ValueError:
                    continue
                seconds = raw_v / (1_000_000 if tk == "out_time_us" else 1_000)
                break

        frac = 0.0
        if seconds is not None and duration > 0:
            frac = max(0.0, min(1.0, seconds / duration))

        weight_start, weight_span = state["weight"]
        overall = (weight_start + frac * weight_span) * 100.0
        state["last_progress"] = max(state.get("last_progress", 0.0), overall)

        try:
            size_bytes = int(fields.get("total_size", "0") or 0)
        except ValueError:
            size_bytes = 0
        speed = fields.get("speed", "").strip()

        eta = None
        speed_x = None
        if speed.endswith("x"):
            try:
                speed_x = float(speed[:-1])
            except ValueError:
                speed_x = None
        if speed_x and speed_x > 0 and seconds is not None:
            eta = max(0.0, (duration - seconds) / speed_x)

        detail = dict(job.get("detail") or {})
        detail.update({
            "phase": state["phase"],
            "pass_index": state["pass_index"],
            "pass_count": state["pass_count"],
            "pass_percent": frac * 100.0,
            "out_time": seconds,
            "encoded_bytes": size_bytes,
            "speed": speed,
            "eta": eta,
            "fps": fields.get("fps"),
            "frame": fields.get("frame"),
        })
        job["detail"] = detail
        job["progress"] = state["last_progress"]

        now = asyncio.get_event_loop().time()
        if now - state.get("last_emit", 0) >= 0.25 or fields.get("progress") == "end":
            state["last_emit"] = now
            jobs.update_job(job, detail=detail, progress=state["last_progress"])
        fields = {}


async def _pump_stderr(job: dict, proc, sink: list[str]) -> None:
    assert proc.stderr is not None
    async for raw in proc.stderr:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            continue
        sink.append(line)
        jobs.log_line(job, line)


# ----------------------------------------------------------------- the job


def _unique_path(directory: Path, stem: str, ext: str) -> Path:
    safe = re.sub(r'[/\\:*?"<>|]', "_", stem).strip() or "output"
    candidate = directory / f"{safe}.{ext}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{safe} ({counter}).{ext}"
        counter += 1
    return candidate


async def run_convert(job: dict, req: dict) -> None:
    settings = db.get_settings()
    output_dir = Path(settings["output_dir"]).expanduser()
    ensure_dirs(output_dir)

    src_path = Path(req["source_path"])
    probe = await media.probe(src_path)

    start, end = resolve_trim(probe, req.get("start"), req.get("end"))
    duration = end - start

    container = req.get("container", "mp4")
    est = estimate(
        probe,
        container=container,
        duration=duration,
        mode=req.get("size_mode", "quality"),
        target_mb=req.get("target_mb"),
        audio_bitrate=int(req.get("audio_bitrate") or 128_000),
        crf=req.get("crf"),
        scale_height=req.get("scale_height"),
        fps=req.get("fps"),
    )

    dest = _unique_path(
        output_dir,
        req.get("output_name") or f"{src_path.stem}_converted",
        container,
    )

    commands = build_commands(
        str(src_path), str(dest),
        container=container, start=start, duration=duration,
        est=est, probe=probe,
        video_codec=req.get("video_codec"),
        preset=req.get("preset") or "medium",
        scale_height=req.get("scale_height"),
        fps=req.get("fps"),
        settings=settings,
    )

    detail = dict(job.get("detail") or {})
    detail.update({
        "estimate": {k: v for k, v in est.items() if not k.startswith("_")},
        "trim": {"start": format_timestamp(start), "end": format_timestamp(end),
                 "duration": duration},
        "output_path": str(dest),
        "commands": [" ".join(shlex.quote(a) for a in c) for c in commands],
        "phase": "Encoding",
    })
    jobs.update_job(job, status="running", detail=detail)

    pass_count = len(commands)
    # Pass 1 does no muxing and runs faster; weight the bar accordingly.
    weights = [(0.0, 0.4), (0.4, 0.6)] if pass_count == 2 else [(0.0, 1.0)]
    stderr_all: list[str] = []
    # Anything short of a clean finish must not leave a truncated file on disk.
    # This has to be a finally: cancelling the job raises CancelledError out of
    # the awaits below, skipping any cleanup placed inline.
    completed = False

    try:
        for index, cmd in enumerate(commands):
            state = {
                "phase": (
                    f"Pass {index + 1} of {pass_count}" if pass_count > 1 else "Encoding"
                ),
                "pass_index": index + 1,
                "pass_count": pass_count,
                "weight": weights[index],
                "last_emit": 0.0,
                "last_progress": job.get("progress") or 0.0,
            }
            jobs.log_line(job, f"$ {' '.join(shlex.quote(a) for a in cmd)}")

            proc = await jobs.spawn(job, cmd)
            await asyncio.gather(
                _pump_progress(job, proc, duration, state),
                _pump_stderr(job, proc, stderr_all),
            )
            rc = await proc.wait()

            if job.get("cancelled"):
                jobs.update_job(job, status="cancelled", error="Cancelled by user.")
                return

            if rc != 0:
                msg = "\n".join(stderr_all[-40:]).strip() or f"ffmpeg exited with code {rc}"
                jobs.update_job(
                    job, status="error",
                    error=f"ffmpeg failed on pass {index + 1} of {pass_count} "
                          f"(exit {rc}):\n{msg}",
                )
                return

        if not dest.exists():
            jobs.update_job(
                job, status="error",
                error="ffmpeg reported success but produced no output file.\n"
                      + "\n".join(stderr_all[-40:]),
            )
            return

        actual = dest.stat().st_size
        predicted = est.get("predicted_bytes")
        accuracy = None
        if predicted:
            accuracy = (actual - predicted) / predicted * 100.0

        row = db.add_file(
            dest, origin="convert",
            title=dest.stem, job_id=job["id"], duration=duration,
        )
        detail = dict(job.get("detail") or {})
        detail.update({
            "phase": "Complete",
            "actual_bytes": actual,
            "predicted_bytes": predicted,
            "target_bytes": est.get("target_bytes"),
            "accuracy_pct": accuracy,
            "output_size": actual,
        })
        jobs.update_job(job, status="done", progress=100.0, detail=detail,
                        file_id=row["id"])
        jobs.broadcast({"type": "library"})
        completed = True
    finally:
        _cleanup_passlog(est)
        if not completed:
            _remove_partial(dest)


def _remove_partial(dest: Path) -> None:
    """Remove a half-written output so it never lands in the library."""
    try:
        if dest.exists():
            dest.unlink()
    except OSError:
        pass


def _cleanup_passlog(est: dict) -> None:
    passlog = est.get("_passlog")
    if not passlog:
        return
    parent = Path(passlog).parent
    if parent.name.startswith("ytx-2pass-"):
        shutil.rmtree(parent, ignore_errors=True)
    est.pop("_passlog", None)


def start_convert(req: dict, title: str) -> dict:
    job = jobs.create_job("convert", title, detail={
        "source_path": req["source_path"],
        "container": req.get("container"),
        "phase": "Queued",
    })
    jobs.run_job(job, lambda: run_convert(job, req))
    return job
