#!/usr/bin/env python3
"""Автоматическая запись видеовстреч на Windows.

Замечает начало встречи (Zoom, Google Meet, Яндекс Телемост), записывает обе
стороны через WASAPI loopback + микрофон, а по окончании расшифровывает запись
и кладёт текст в выбранную папку.

В отличие от `watcher/`, здесь запись делает сама программа, а не Zoom —
поэтому работает и на чужих встречах, где вы не организатор, и не требует
ничьих разрешений внутри Zoom.

Запуск в консоли (видно, что происходит):

    python recorder.py

Обычно же запускается через иконку в трее: `python tray_app.py`.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHARED = HERE.parent / "watcher"

if not SHARED.is_dir():
    print("Не найдена папка watcher рядом с этой — скачай репозиторий целиком, "
          "а не одну папку windows.", file=sys.stderr)
    raise SystemExit(1)

sys.path.insert(0, str(SHARED))
sys.path.insert(0, str(HERE))

import config  # noqa: E402  (из ../watcher — кросс-платформенный)
from transcription import (  # noqa: E402
    DeepgramAuthError, format_transcript, log, transcribe,
)
from audio_capture import CaptureError, DualCapture, find_ffmpeg  # noqa: E402
from meeting_detect import detect_meeting  # noqa: E402

POLL_SECONDS = 5
STOP_DEBOUNCE_SECONDS = 25
MAX_RECORDING_SECONDS = 3 * 60 * 60
MIN_USEFUL_BYTES = 20_000

PLATFORM_NAMES = {"zoom": "Zoom", "meet": "Google Meet",
                  "telemost": "Яндекс Телемост"}

TRIM_CHARS = "`'\"«»„“”‘’– -. "
ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]+')


def work_dir() -> Path:
    return config.APP_DIR / "recordings"


# ------------------------------------------------------------------ naming ---
def _clean_topic(topic: str) -> str:
    topic = ILLEGAL_CHARS.sub("", topic.strip().strip(TRIM_CHARS))
    topic = re.sub(r"\s+", " ", topic).strip(TRIM_CHARS).lower()
    if len(topic) > 70 or len(topic.split()) > 12:
        return ""
    return topic


def _find_claude() -> str | None:
    import shutil
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (Path.home() / "AppData/Roaming/npm/claude.cmd",
                      Path.home() / ".local/bin/claude.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def smart_topic(transcript: str) -> str:
    if not config.load()["smart_naming"]:
        return ""
    claude_bin = _find_claude()
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
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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


def build_name(started: dt.datetime, platform: str, duration: float,
               transcript: str) -> str:
    stamp = f"{started:%Y-%m-%d %H-%M}"
    label = PLATFORM_NAMES.get(platform, platform)
    topic = smart_topic(transcript)
    if topic:
        return f"{stamp} {label} — {topic}.md"
    return f"{stamp} {label} ({duration / 60:.0f} мин).md"


def unique_path(folder: Path, name: str) -> Path:
    target = folder / name
    if not target.exists():
        return target
    stem = name[: -len(".md")]
    n = 2
    while (folder / f"{stem} ({n}).md").exists():
        n += 1
    return folder / f"{stem} ({n}).md"


# --------------------------------------------------------------- recording ---
class Recorder:
    def __init__(self) -> None:
        self.capture: DualCapture | None = None
        self.start_dt: dt.datetime | None = None
        self.platform: str | None = None

    def start(self, platform: str) -> bool:
        self.start_dt = dt.datetime.now()
        self.platform = platform
        stamp = f"{self.start_dt:%Y%m%d-%H%M%S}-{platform}"
        capture = DualCapture(work_dir())
        try:
            capture.start(stamp)
        except CaptureError as e:
            log(f"Запись не началась: {e}")
            self.capture = None
            return False
        self.capture = capture
        log(f"▶ запись {platform}")
        return True

    def stop_and_finalize(self) -> None:
        if not self.capture:
            return
        capture, started, platform = self.capture, self.start_dt, self.platform
        self.capture = self.start_dt = self.platform = None

        out = work_dir() / f"{started:%Y%m%d-%H%M%S}-{platform}.m4a"
        try:
            audio = capture.stop(out)
        except CaptureError as e:
            log(f"Не удалось сохранить запись: {e}")
            return

        if not audio.exists() or audio.stat().st_size < MIN_USEFUL_BYTES:
            log("Запись пустая или слишком короткая — удалена")
            audio.unlink(missing_ok=True)
            return

        duration = (dt.datetime.now() - started).total_seconds()
        log(f"■ встреча {platform} закончилась, ~{duration / 60:.0f} мин — расшифровываю")
        threading.Thread(target=_finalize_worker,
                         args=(audio, started, platform, duration),
                         daemon=True).start()

    @property
    def recording(self) -> bool:
        return self.capture is not None

    @property
    def elapsed(self) -> float:
        return (dt.datetime.now() - self.start_dt).total_seconds() if self.start_dt else 0.0


def _finalize_worker(audio: Path, started: dt.datetime, platform: str,
                     duration: float) -> None:
    try:
        response = transcribe(audio)
    except DeepgramAuthError as e:
        log(f"Deepgram: {e} Запись сохранена: {audio}")
        return
    except Exception as e:  # noqa: BLE001
        log(f"Расшифровка не удалась: {type(e).__name__}: {e}. Запись: {audio}")
        return

    transcript = format_transcript(response)
    if not transcript.strip():
        log(f"Пустая расшифровка. Запись сохранена: {audio}")
        return

    out_dir = config.output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = unique_path(out_dir, build_name(started, platform, duration, transcript))
    target.write_text(
        "---\n"
        f"date: {started.isoformat(timespec='seconds')}\n"
        "source: meeting-recorder-windows\n"
        f"platform: {platform}\n"
        f"duration_sec: {duration:.1f}\n"
        "---\n\n"
        f"# {PLATFORM_NAMES.get(platform, platform)} — {started:%Y-%m-%d %H:%M}\n\n"
        f"{transcript}\n",
        encoding="utf-8",
    )
    audio.unlink(missing_ok=True)
    log(f"✓ готово: {target.name}")


# ----------------------------------------------------------------- service ---
class RecorderService:
    """Цикл слежения, обёрнутый так, чтобы иконка в трее могла им управлять."""

    def __init__(self) -> None:
        self.rec = Recorder()
        self.idle_since: dt.datetime | None = None
        self.enabled = True
        self._stop = threading.Event()

    def _tick(self) -> None:
        if not self.enabled:
            if self.rec.recording:
                self.rec.stop_and_finalize()
                self.idle_since = None
            return

        if not config.is_configured():
            return

        platform = detect_meeting()
        if platform and not self.rec.recording:
            self.rec.start(platform)
            self.idle_since = None
        elif self.rec.recording:
            if platform:
                self.idle_since = None
                if self.rec.elapsed > MAX_RECORDING_SECONDS:
                    log("Достигнут предел длительности — закрываю файл")
                    self.rec.stop_and_finalize()
            elif self.idle_since is None:
                self.idle_since = dt.datetime.now()
            elif (dt.datetime.now() - self.idle_since).total_seconds() > STOP_DEBOUNCE_SECONDS:
                self.rec.stop_and_finalize()
                self.idle_since = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001 — цикл не должен умирать
                log(f"Ошибка цикла: {type(e).__name__}: {e}")
            self._stop.wait(POLL_SECONDS)

    def run_in_thread(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def run_blocking(self) -> None:
        try:
            self._loop()
        finally:
            if self.rec.recording:
                self.rec.stop_and_finalize()

    def shutdown(self) -> None:
        self._stop.set()
        if self.rec.recording:
            self.rec.stop_and_finalize()

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        log(f"запись {'ВКЛЮЧЕНА' if on else 'ВЫКЛЮЧЕНА'}")
        if not on and self.rec.recording:
            self.rec.stop_and_finalize()
            self.idle_since = None

    def status(self) -> tuple[str, str | None]:
        if not config.is_configured():
            return ("unconfigured", None)
        if not self.enabled:
            return ("off", None)
        if self.rec.recording:
            return ("recording", self.rec.platform)
        return ("idle", None)


def main() -> int:
    if not sys.platform.startswith("win"):
        print("Эта версия для Windows. На macOS используй приложение из корня "
              "проекта, на Linux — папку watcher.", file=sys.stderr)
        return 1
    if not config.is_configured():
        log("Не задан ключ Deepgram. Запусти: python setup_windows.py")
        return 1
    if not find_ffmpeg():
        log("Не найден ffmpeg — он нужен, чтобы свести микрофон и звук "
            "собеседника. Установи: winget install ffmpeg")
        return 1

    log(f"Запись встреч запущена. Расшифровки: {config.output_dir()}")
    RecorderService().run_blocking()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Остановлено пользователем")
        sys.exit(0)
