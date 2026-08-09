#!/usr/bin/env python3
"""Watch folders for new meeting recordings and turn them into text.

Runs quietly in the background. When a new recording appears in a watched
folder — Zoom writes one after every meeting it records — the file is
transcribed with Deepgram and a Markdown transcript lands in the output
folder. The original recording is never modified.

Works on Windows, macOS and Linux. Start it with:

    python watcher.py

Settings come from config.py (run `python setup_watcher.py` first).
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from transcription import (  # noqa: E402
    DeepgramAuthError, format_transcript, log, transcribe,
)

POLL_SECONDS = 20
# A recording still being written must not be uploaded half-finished: we wait
# until its size stops changing between two consecutive scans.
STABLE_CHECKS = 2

MEDIA_SUFFIXES = {".m4a", ".mp4", ".mp3", ".wav", ".mov", ".mkv", ".webm",
                  ".m4v", ".aac", ".ogg", ".opus", ".flac"}
# Zoom leaves these behind mid-conversion — they are not playable recordings.
SKIP_SUFFIXES = {".zoom", ".tmp", ".part", ".crdownload", ".download"}
SKIP_NAME_HINTS = ("double_click_to_convert",)

MIN_USEFUL_BYTES = 20_000
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # Deepgram limit

ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]+')
TRIM_CHARS = "`'\"«»„“”‘’– -. "

CLAUDE_CANDIDATES = [
    "claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    str(Path.home() / ".claude/local/claude"),
    str(Path.home() / ".local/bin/claude"),
    str(Path.home() / "AppData/Roaming/npm/claude.cmd"),
]


# ------------------------------------------------------------------ ffmpeg ---
def find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
                      r"C:\ffmpeg\bin\ffmpeg.exe"):
        if Path(candidate).is_file():
            return candidate
    return None


def extract_audio(src: Path, work_dir: Path) -> Path:
    """Video → small mono m4a. Deepgram does not take video containers, and a
    64 kbit mono track uploads far faster than a screen recording."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        if src.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
            log(f"Нет ffmpeg — видео {src.name} отправляю как есть, это медленнее. "
                f"Поставь ffmpeg, чтобы ускорить.")
        return src

    work_dir.mkdir(parents=True, exist_ok=True)
    out = work_dir / f"{src.stem}-audio.m4a"
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
             "-vn", "-ar", "16000", "-ac", "1", "-c:a", "aac", "-b:a", "64k",
             "-movflags", "+faststart", str(out)],
            capture_output=True, timeout=1800,
        )
        if r.returncode == 0 and out.exists() and out.stat().st_size > 1000:
            return out
        log(f"ffmpeg не смог обработать {src.name}: "
            f"{(r.stderr or b'').decode('utf-8', 'replace')[:200]}")
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"ffmpeg упал на {src.name}: {type(e).__name__}")
    return src


# ------------------------------------------------------------------ naming ---
def find_claude() -> str | None:
    for candidate in CLAUDE_CANDIDATES:
        found = shutil.which(candidate) if not Path(candidate).is_absolute() else (
            candidate if Path(candidate).is_file() else None
        )
        if found:
            return found
    return None


def _clean_topic(topic: str) -> str:
    topic = ILLEGAL_CHARS.sub("", topic.strip().strip(TRIM_CHARS))
    topic = re.sub(r"\s+", " ", topic).strip(TRIM_CHARS).lower()
    if len(topic) > 70 or len(topic.split()) > 12:
        return ""
    return topic


def smart_topic(transcript: str) -> str:
    """Short topic via Claude Code CLI, or '' when it isn't available."""
    if not config.load()["smart_naming"]:
        return ""
    claude_bin = find_claude()
    if not claude_bin:
        return ""
    prompt = (
        "Ниже транскрипт рабочего звонка. Верни РОВНО одну строку — короткую "
        "тему разговора, 3–7 слов строчными буквами, без кавычек, без markdown, "
        "без точки в конце, без пояснений. Только тема.\n\n"
        f"Транскрипт:\n<<<\n{transcript[:6000]}\n>>>"
    )
    try:
        proc = subprocess.run(
            [claude_bin, "--dangerously-skip-permissions", "-p", prompt],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip().strip(TRIM_CHARS).strip()
        if line:
            return _clean_topic(line)
    return ""


def build_name(recorded: dt.datetime, transcript: str, source: Path) -> str:
    stamp = f"{recorded:%Y-%m-%d %H-%M}"
    topic = smart_topic(transcript)
    if topic:
        return f"{stamp} — {topic}.md"
    # Zoom names its folder after the meeting, which is a decent fallback —
    # but it prefixes it with its own timestamp ("2026-08-09 14.30.00 Планёрка"),
    # and repeating that in the filename reads as a bug.
    folder_hint = re.sub(r"^\d{4}[-.]\d{2}[-.]\d{2}[\s\d.:-]*", "",
                         source.parent.name.replace("_", " "))
    folder_hint = _clean_topic(folder_hint)
    if folder_hint and not re.fullmatch(r"[\d\s\-.:]+", folder_hint):
        return f"{stamp} — {folder_hint}.md"
    return f"{stamp} — запись встречи.md"


def unique_path(folder: Path, name: str) -> Path:
    target = folder / name
    if not target.exists():
        return target
    stem = name[: -len(".md")]
    n = 2
    while (folder / f"{stem} ({n}).md").exists():
        n += 1
    return folder / f"{stem} ({n}).md"


# ---------------------------------------------------------------- scanning ---
def is_candidate(path: Path) -> bool:
    name = path.name.lower()
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    if any(hint in name for hint in SKIP_NAME_HINTS):
        return False
    if path.suffix.lower() not in MEDIA_SUFFIXES:
        return False
    if name.startswith("."):
        return False
    try:
        return path.stat().st_size >= MIN_USEFUL_BYTES
    except OSError:
        return False


def collect_candidates() -> list[Path]:
    """New recordings worth transcribing.

    Zoom writes both `zoom_0.mp4` and (optionally) `audio_only.m4a` into the
    same folder. Transcribing both would double the bill and produce two
    identical transcripts, so per folder we keep the audio-only file when it
    exists and the largest media file otherwise.
    """
    by_folder: dict[Path, list[Path]] = {}
    for watch_dir in config.watch_dirs():
        if not watch_dir.exists():
            continue
        for path in watch_dir.rglob("*"):
            if path.is_file() and is_candidate(path):
                by_folder.setdefault(path.parent, []).append(path)

    picked: list[Path] = []
    for folder, files in by_folder.items():
        audio_only = [f for f in files if "audio_only" in f.name.lower()]
        if audio_only:
            picked.extend(audio_only)
            continue
        audio = [f for f in files if f.suffix.lower() in
                 {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".flac"}]
        pool = audio or files
        picked.append(max(pool, key=lambda f: f.stat().st_size))
    return picked


def file_key(path: Path) -> str:
    try:
        return f"{path.resolve()}|{path.stat().st_size}"
    except OSError:
        return str(path)


# -------------------------------------------------------------- processing ---
def process(path: Path, state: dict) -> None:
    log(f"Новая запись: {path.name} ({path.stat().st_size / 1_048_576:.0f} МБ)")

    if path.stat().st_size > MAX_UPLOAD_BYTES:
        log(f"Файл больше 2 ГБ — Deepgram столько не принимает. Пропускаю: {path.name}")
        state["done"][file_key(path)] = "too_big"
        config.save_state(state)
        return

    work_dir = config.APP_DIR / "work"
    upload = extract_audio(path, work_dir)

    try:
        response = transcribe(upload)
    except DeepgramAuthError as e:
        # Not marked as done: fix the key and it gets picked up next round.
        log(f"Deepgram: {e}")
        return
    except Exception as e:  # noqa: BLE001
        log(f"Не удалось расшифровать {path.name}: {type(e).__name__}: {e}")
        return
    finally:
        if upload != path:
            upload.unlink(missing_ok=True)

    transcript = format_transcript(response)
    if not transcript.strip():
        log(f"Пустая расшифровка: {path.name}")
        state["done"][file_key(path)] = "empty"
        config.save_state(state)
        return

    recorded = dt.datetime.fromtimestamp(path.stat().st_mtime)
    out_dir = config.output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = unique_path(out_dir, build_name(recorded, transcript, path))

    target.write_text(
        "---\n"
        f"date: {recorded.isoformat(timespec='seconds')}\n"
        "source: meeting-transcriber\n"
        f"recording: {path.name}\n"
        "---\n\n"
        f"# Встреча — {recorded:%Y-%m-%d %H:%M}\n\n"
        f"{transcript}\n",
        encoding="utf-8",
    )
    state["done"][file_key(path)] = target.name
    config.save_state(state)
    log(f"✓ готово: {target.name}")

    if config.load()["delete_source"]:
        path.unlink(missing_ok=True)
        log(f"  исходная запись удалена: {path.name}")


def main() -> int:
    if not config.is_configured():
        log("Не задан ключ Deepgram. Запусти: python setup_watcher.py")
        return 1

    log(f"Слежу за папками: {', '.join(str(d) for d in config.watch_dirs())}")
    log(f"Расшифровки складываю в: {config.output_dir()}")

    state = config.load_state()
    pending: dict[str, int] = {}  # path → how many scans its size stayed put

    # Recordings that already exist at first launch are skipped: the user
    # wants new meetings transcribed, not a surprise bill for their archive.
    if not state["done"]:
        for path in collect_candidates():
            state["done"][file_key(path)] = "pre-existing"
        config.save_state(state)
        log(f"Уже лежавшие записи ({len(state['done'])} шт.) пропускаю — "
            f"расшифровываю только новые.")

    while True:
        try:
            for path in collect_candidates():
                key = file_key(path)
                if key in state["done"]:
                    pending.pop(str(path), None)
                    continue
                # Size is part of the key, so a still-growing file gets a new
                # key each scan; count stability by path instead.
                seen = pending.get(str(path), 0) + 1
                pending[str(path)] = seen
                if seen < STABLE_CHECKS:
                    continue
                pending.pop(str(path), None)
                process(path, state)
        except Exception as e:  # noqa: BLE001 — the watcher must never die
            log(f"Ошибка обхода папок: {type(e).__name__}: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Остановлено пользователем")
        sys.exit(0)
