"""Paths, binary discovery and persisted settings."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent
WEB_DIR = PROJECT_ROOT / "web"

DATA_DIR = Path(os.environ.get("YTX_DATA_DIR") or PROJECT_ROOT / "data")
DB_PATH = DATA_DIR / "ytx.sqlite3"

DEFAULT_DOWNLOAD_DIR = PROJECT_ROOT / "downloads"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"

# Settings keys that hold filesystem paths, with their fallbacks.
PATH_SETTINGS = {
    "download_dir": DEFAULT_DOWNLOAD_DIR,
    "output_dir": DEFAULT_OUTPUT_DIR,
}

DEFAULT_SETTINGS = {
    "download_dir": str(DEFAULT_DOWNLOAD_DIR),
    "output_dir": str(DEFAULT_OUTPUT_DIR),
    "yt_dlp_path": "",  # blank => auto-discover on PATH
    "ffmpeg_path": "",
    "ffprobe_path": "",
    # Authentication for age-restricted / private / members-only videos.
    "cookies_browser": "",  # safari, chrome, firefox, … or blank for none
    "cookies_file": "",     # exported cookies.txt; takes precedence when set
}


def _which(name: str, override: str) -> str:
    """Resolve a binary: explicit override wins, else look on PATH."""
    if override:
        return override
    found = shutil.which(name)
    return found or name


def yt_dlp_bin(settings: dict) -> str:
    return _which("yt-dlp", settings.get("yt_dlp_path", ""))


def ffmpeg_bin(settings: dict) -> str:
    return _which("ffmpeg", settings.get("ffmpeg_path", ""))


def ffprobe_bin(settings: dict) -> str:
    return _which("ffprobe", settings.get("ffprobe_path", ""))


def ensure_dirs(*paths: Path) -> None:
    for p in paths:
        Path(p).mkdir(parents=True, exist_ok=True)
