#!/usr/bin/env python3
"""Name a finished transcript file.

Two tiers, and the app degrades quietly from the first to the second:

  1. Claude Code CLI is installed → it reads the first part of the transcript
     and returns a short topic, so the file lands as
     `2026-08-09 14-30 Zoom — обсуждение бюджета на квартал.md`
  2. It isn't (or it fails) → a plain, always-correct name:
     `2026-08-09 14-30 Zoom (44 мин).md`

Tier 2 is never an error state. A file always gets written.
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
from pathlib import Path

import config
from transcription import log

# Claude Code installs here via npm/homebrew; PATH is unreliable inside a
# .app bundle, so check the usual locations explicitly too.
CLAUDE_CANDIDATES = [
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    str(Path.home() / ".claude/local/claude"),
    str(Path.home() / ".local/bin/claude"),
]

TRANSCRIPT_CHARS_FOR_NAMING = 6000  # enough to judge the topic, keeps it cheap
NAMING_TIMEOUT = 120

PLATFORM_NAMES = {
    "zoom": "Zoom",
    "meet": "Google Meet",
    "telemost": "Яндекс Телемост",
}

ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]+')
# Models like to wrap their answer in quotes — including typographic ones.
TRIM_CHARS = "`'\"«»„“”‘’– -. "


def find_claude() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for candidate in CLAUDE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def _ask_claude_topic(claude_bin: str, transcript: str) -> str | None:
    prompt = (
        "Ниже транскрипт рабочего звонка. Верни РОВНО одну строку — короткую "
        "тему разговора, 3–7 слов строчными буквами, без кавычек, без markdown, "
        "без точки в конце, без пояснений. Только тема.\n\n"
        f"Транскрипт:\n<<<\n{transcript[:TRANSCRIPT_CHARS_FOR_NAMING]}\n>>>"
    )
    try:
        proc = subprocess.run(
            [claude_bin, "--dangerously-skip-permissions", "-p", prompt],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=NAMING_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"именование через Claude не удалось: {type(e).__name__}")
        return None
    if proc.returncode != 0:
        log(f"claude вернул код {proc.returncode}: {(proc.stderr or '').strip()[:200]}")
        return None

    for raw in (proc.stdout or "").splitlines():
        line = raw.strip().strip(TRIM_CHARS).strip()
        if line:
            return line
    return None


def _clean_topic(topic: str) -> str:
    topic = ILLEGAL_CHARS.sub("", topic.strip().strip(TRIM_CHARS))
    topic = re.sub(r"\s+", " ", topic).strip(TRIM_CHARS).lower()
    # A model that ignored the instruction and wrote a paragraph is not a topic.
    if len(topic) > 70 or len(topic.split()) > 12:
        return ""
    return topic


def build_name(started: dt.datetime, platform: str, duration_sec: float,
               transcript: str) -> str:
    """Filename for this recording, smart topic when possible."""
    stamp = f"{started:%Y-%m-%d %H-%M}"
    label = PLATFORM_NAMES.get(platform, platform)

    if config.load()["smart_naming"]:
        claude_bin = find_claude()
        if claude_bin:
            topic = _clean_topic(_ask_claude_topic(claude_bin, transcript) or "")
            if topic:
                return f"{stamp} {label} — {topic}.md"
        else:
            log("Claude Code не найден — имя файла без темы разговора")

    return f"{stamp} {label} ({duration_sec / 60:.0f} мин).md"


def unique_path(folder: Path, name: str) -> Path:
    """Never overwrite an existing transcript."""
    target = folder / name
    if not target.exists():
        return target
    stem, suffix = name[: -len(".md")], ".md"
    n = 2
    while (folder / f"{stem} ({n}){suffix}").exists():
        n += 1
    return folder / f"{stem} ({n}){suffix}"
