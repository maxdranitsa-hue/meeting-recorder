#!/usr/bin/env python3
"""User settings for the meeting recorder.

Everything the app needs to know about *this* machine lives in one JSON file:

    ~/Library/Application Support/MeetingRecorder/config.json

Nothing is hardcoded — no API keys in the source, no paths to anyone's
personal folders. On first launch the menu-bar app asks for a Deepgram key
and a destination folder and writes them here.
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

__all__ = ["load", "save", "update", "output_dir", "is_configured",
           "macos_supported", "macos_version", "LOG_FILE", "TMP_DIR"]

APP_SUPPORT = Path.home() / "Library/Application Support/MeetingRecorder"
CONFIG_FILE = APP_SUPPORT / "config.json"
LOG_FILE = Path.home() / "Library/Logs/meeting-recorder.log"
TMP_DIR = Path.home() / ".cache/meeting-recorder"

DEFAULT_OUTPUT_DIR = Path.home() / "Documents/Записи звонков"
DEFAULT_LANGUAGE = "ru"

# Core Audio process taps — the whole capture approach — need macOS 14.2+.
MIN_MACOS = (14, 2)

DEFAULTS = {
    "deepgram_api_key": "",
    "output_dir": str(DEFAULT_OUTPUT_DIR),
    "language": DEFAULT_LANGUAGE,
    "smart_naming": True,   # use Claude Code CLI to name files, when installed
}


def macos_version() -> tuple[int, ...]:
    """(major, minor) of the running macOS, or (0, 0) if it can't be read."""
    raw = platform.mac_ver()[0]
    if not raw:
        return (0, 0)
    parts = []
    for chunk in raw.split(".")[:2]:
        try:
            parts.append(int(chunk))
        except ValueError:
            return (0, 0)
    while len(parts) < 2:
        parts.append(0)
    return tuple(parts)


def macos_supported() -> bool:
    v = macos_version()
    return v >= MIN_MACOS if v != (0, 0) else False


def load() -> dict:
    """Config merged over defaults. Missing/corrupt file → plain defaults."""
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (json.JSONDecodeError, OSError):
            pass  # a broken config must never stop the app from starting

    # An env var wins over the file — handy for testing without touching config.
    env_key = os.environ.get("DEEPGRAM_API_KEY")
    if env_key:
        cfg["deepgram_api_key"] = env_key
    return cfg


def save(cfg: dict) -> None:
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    CONFIG_FILE.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    # The key is a secret: keep the file readable by this user only.
    try:
        CONFIG_FILE.chmod(0o600)
    except OSError:
        pass


def update(**changes) -> dict:
    cfg = load()
    cfg.update(changes)
    save(cfg)
    return cfg


def output_dir() -> Path:
    return Path(load()["output_dir"]).expanduser()


def is_configured() -> bool:
    """True once there's a key to send to Deepgram."""
    return bool(load()["deepgram_api_key"].strip())
