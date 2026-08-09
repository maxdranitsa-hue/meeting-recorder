#!/usr/bin/env python3
"""Settings for the folder watcher. Works on Windows, macOS and Linux.

One JSON file holds everything:

    Windows:  %APPDATA%\\MeetingTranscriber\\config.json
    macOS:    ~/Library/Application Support/MeetingTranscriber/config.json
    Linux:    ~/.config/MeetingTranscriber/config.json

Nothing is hardcoded — the key and both folders are chosen during setup.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


def _app_dir() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
        return Path(base) / "MeetingTranscriber"
    if IS_MAC:
        return Path.home() / "Library/Application Support/MeetingTranscriber"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "MeetingTranscriber"


APP_DIR = _app_dir()
CONFIG_FILE = APP_DIR / "config.json"
STATE_FILE = APP_DIR / "processed.json"
LOG_FILE = APP_DIR / "transcriber.log"


def default_watch_dir() -> Path:
    """Where Zoom saves local recordings out of the box."""
    return Path.home() / "Documents" / "Zoom"


def default_output_dir() -> Path:
    return Path.home() / "Documents" / "Расшифровки встреч"


DEFAULTS = {
    "deepgram_api_key": "",
    "watch_dirs": [str(default_watch_dir())],
    "output_dir": str(default_output_dir()),
    "language": "ru",
    "smart_naming": True,
    "delete_source": False,   # never touch the user's recordings unless asked
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (json.JSONDecodeError, OSError):
            pass  # a broken config must never stop the watcher from starting

    env_key = os.environ.get("DEEPGRAM_API_KEY")
    if env_key:
        cfg["deepgram_api_key"] = env_key
    return cfg


def save(cfg: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    CONFIG_FILE.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if not IS_WINDOWS:
        try:
            CONFIG_FILE.chmod(0o600)  # the key is a secret
        except OSError:
            pass


def update(**changes) -> dict:
    cfg = load()
    cfg.update(changes)
    save(cfg)
    return cfg


def watch_dirs() -> list[Path]:
    return [Path(p).expanduser() for p in load()["watch_dirs"]]


def output_dir() -> Path:
    return Path(load()["output_dir"]).expanduser()


def is_configured() -> bool:
    return bool(load()["deepgram_api_key"].strip())


# ------------------------------------------------------------------ state ---
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"done": {}}


def save_state(state: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                          encoding="utf-8")
