#!/usr/bin/env python3
"""Auto-record video meetings (Zoom / Google Meet / Яндекс Телемост) and
transcribe them.

How it works
------------
A background loop watches for an *active* video meeting:
  - Zoom desktop app in a meeting  (helper process `CptHost` is running), or
  - Chrome has a tab on a live Meet call (meet.google.com/<xxx-xxxx-xxx>), or
  - Chrome has a tab on a live Телемост call (telemost.yandex.ru/<path>).

Phone / FaceTime calls are intentionally NOT matched.

While a meeting runs it records microphone + system audio (both sides) into a
single mono WAV via a small native helper (`meeting_tap`) built on a Core Audio
*process tap* (macOS 14.2+). The tap takes a COPY of the system output mix, so
the user's own audio is untouched: they keep hearing the call on whatever
device they use, volume keys keep working, headphones can be swapped mid-call.
No output switching, no virtual audio device.

When the meeting ends the recording is transcribed with Deepgram and written as
a Markdown file into the folder chosen during setup.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402
import naming  # noqa: E402
from transcription import (  # noqa: E402
    DeepgramAuthError, format_transcript, log, transcribe,
)


def _find_tap_helper() -> Path:
    """Locate the native capture helper.

    Inside a standalone py2app bundle the Python modules live in a zip, so
    __file__ points into an archive — the helper has to be found via the
    bundle's Resources dir instead (py2app exports RESOURCEPATH for exactly
    this). Falls back to sitting next to the sources when run from a checkout.
    """
    candidates = []
    res = os.environ.get("RESOURCEPATH")
    if res:
        candidates.append(Path(res) / "meeting_tap")
    # …/Contents/MacOS/<exe> → …/Contents/Resources/meeting_tap
    candidates.append(Path(sys.executable).resolve().parent.parent / "Resources" / "meeting_tap")
    candidates.append(SCRIPT_DIR / "meeting_tap")

    for path in candidates:
        if path.is_file():
            return path
    return SCRIPT_DIR / "meeting_tap"  # reported as missing, with a clear path


# --- native tap helper (built from meeting_tap.swift by install.sh) ---
TAP_HELPER = _find_tap_helper()


def _tool(name: str, fallback: str) -> str:
    return shutil.which(name) or fallback


FFMPEG = _tool("ffmpeg", "/opt/homebrew/bin/ffmpeg")
OSASCRIPT = "/usr/bin/osascript"
PGREP = "/usr/bin/pgrep"

# --- detection tuning ---
POLL_SECONDS = 5
STOP_DEBOUNCE_SECONDS = 25            # must look "gone" this long before stopping
MAX_RECORDING_SECONDS = 3 * 60 * 60   # safety cap: never record longer than 3h
MIN_USEFUL_BYTES = 20_000             # below this the capture never really started

# Zoom spawns one of these helper processes only while in an actual meeting.
ZOOM_MEETING_PROCS = ["CptHost", "cpthost", "aomhost", "AOMHost"]

MEET_CALL_RE = re.compile(r"meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}", re.I)
TELEMOST_CALL_RE = re.compile(r"telemost\.yandex\.ru/\S+", re.I)


# ---------------------------------------------------------------- detection ---
def _proc_running(names: list[str]) -> bool:
    for n in names:
        try:
            if subprocess.run([PGREP, "-x", n], capture_output=True,
                              timeout=5).returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    return False


_CHROME_TABS_AS = (
    'tell application "Google Chrome"\n'
    '  set out to ""\n'
    '  repeat with w in windows\n'
    '    repeat with t in tabs of w\n'
    '      set out to out & (URL of t) & linefeed\n'
    '    end repeat\n'
    '  end repeat\n'
    '  return out\n'
    'end tell'
)


def _chrome_meeting_tab() -> str | None:
    """'meet' / 'telemost' if Chrome has a live call tab, else None.

    Only queried when Chrome is already running, so we never launch it.
    Needs Automation permission for Chrome (macOS asks once).
    """
    if not _proc_running(["Google Chrome"]):
        return None
    try:
        r = subprocess.run(
            [OSASCRIPT, "-e", _CHROME_TABS_AS],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        # -1743 = not allowed to send AppleEvents (Automation not granted yet)
        if "1743" in (r.stderr or "") and not getattr(_chrome_meeting_tab, "_warned", False):
            log("Нет разрешения на управление Chrome — Meet и Телемост "
                "детектиться не будут. Zoom работает и без этого.")
            _chrome_meeting_tab._warned = True  # type: ignore[attr-defined]
        return None

    urls = r.stdout or ""
    if MEET_CALL_RE.search(urls):
        return "meet"
    if TELEMOST_CALL_RE.search(urls):
        return "telemost"
    return None


def detect_meeting() -> str | None:
    """Platform name if a video meeting is active, else None."""
    if _proc_running(ZOOM_MEETING_PROCS):
        return "zoom"
    return _chrome_meeting_tab()


# --------------------------------------------------------------- recording ---
class Recorder:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.tmp_path: Path | None = None
        self.start_dt: dt.datetime | None = None
        self.platform: str | None = None
        self._err = None
        self._err_path = config.TMP_DIR / "tap-last.err"

    def start(self, platform: str) -> bool:
        if not TAP_HELPER.exists():
            log(f"Не найден помощник записи: {TAP_HELPER}. "
                f"Собери его: bash install.sh")
            return False

        config.TMP_DIR.mkdir(parents=True, exist_ok=True)
        self.start_dt = dt.datetime.now()
        self.platform = platform
        self.tmp_path = config.TMP_DIR / f"rec-{self.start_dt:%Y%m%d-%H%M%S}-{platform}.wav"

        try:
            self._err = open(self._err_path, "wb")
            self.proc = subprocess.Popen(
                [str(TAP_HELPER), str(self.tmp_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=self._err,
            )
        except OSError as e:
            log(f"Не удалось запустить помощник записи: {e}")
            self.proc = None
            return False

        # The helper prints READY once audio actually flows. Waiting for it
        # means a permission-blocked start is not silently counted as a
        # recording — the user gets told instead of losing the call.
        if not self._wait_ready(timeout=6.0):
            if self.proc.poll() is not None:
                err = self._read_err_tail()
                log(f"Помощник записи завершился до старта{(' | ' + err) if err else ''} "
                    f"— проверь разрешение «Запись звука с этого компьютера». "
                    f"Встреча не записывается.")
                self._close_err()
                self.proc = None
                return False
            log("Помощник записи медленно стартует — пишем дальше")

        log(f"▶ запись {platform} → {self.tmp_path.name}")
        return True

    def _wait_ready(self, timeout: float) -> bool:
        if not self.proc or not self.proc.stdout:
            return False
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not ready:
            return False
        return b"READY" in (self.proc.stdout.readline() or b"")

    def _read_err_tail(self) -> str:
        try:
            return self._err_path.read_text(errors="replace")[-300:].strip()
        except OSError:
            return ""

    def _close_err(self) -> None:
        try:
            if self._err:
                self._err.close()
        except Exception:  # noqa: BLE001
            pass
        self._err = None

    def stop_and_finalize(self) -> None:
        if not self.proc:
            return
        # SIGTERM → helper finalizes the WAV header and tears the tap down.
        try:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._close_err()

        tmp, start_dt, platform = self.tmp_path, self.start_dt, self.platform
        self.proc = self.tmp_path = self.start_dt = self.platform = None

        if not tmp or not tmp.exists() or tmp.stat().st_size < MIN_USEFUL_BYTES:
            err_tail = self._read_err_tail()
            log("Запись пустая или слишком короткая — удалена"
                + (f" | {err_tail}" if err_tail else ""))
            if tmp:
                tmp.unlink(missing_ok=True)
            return

        dur = (dt.datetime.now() - start_dt).total_seconds()
        log(f"■ встреча {platform} закончилась, ~{dur / 60:.0f} мин — расшифровываю")
        threading.Thread(
            target=_finalize_worker, args=(tmp, start_dt, platform, dur), daemon=True
        ).start()

    @property
    def recording(self) -> bool:
        return self.proc is not None

    @property
    def elapsed(self) -> float:
        return (dt.datetime.now() - self.start_dt).total_seconds() if self.start_dt else 0.0


def _to_m4a(wav: Path) -> Path:
    """Shrink the WAV for upload. Falls back to the WAV if ffmpeg is missing —
    Deepgram sniffs the container from the bytes either way."""
    m4a = wav.with_suffix(".m4a")
    try:
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav),
             "-ar", "16000", "-ac", "1", "-c:a", "aac", "-b:a", "64k",
             "-movflags", "+faststart", str(m4a)],
            capture_output=True, timeout=600,
        )
        if r.returncode == 0 and m4a.exists() and m4a.stat().st_size > 1000:
            return m4a
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"Сжатие не удалось ({type(e).__name__}) — отправляю WAV как есть")
    return wav


def _finalize_worker(tmp: Path, start_dt: dt.datetime, platform: str,
                     dur: float) -> None:
    """Transcribe and file the recording — runs off the detection loop."""
    upload = _to_m4a(tmp)
    m4a = tmp.with_suffix(".m4a")

    try:
        response = transcribe(upload)
    except DeepgramAuthError as e:
        # Audio is deliberately kept: the user can fix the key and re-send it.
        log(f"Deepgram: {e} Аудио сохранено: {tmp}")
        return
    except Exception as e:  # noqa: BLE001
        log(f"Расшифровка не удалась: {type(e).__name__}: {e}. Аудио сохранено: {tmp}")
        return

    transcript = format_transcript(response)
    if not transcript.strip():
        log(f"Пустая расшифровка ({platform}). Аудио сохранено: {tmp}")
        m4a.unlink(missing_ok=True)
        return

    out_dir = config.output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = naming.unique_path(
        out_dir, naming.build_name(start_dt, platform, dur, transcript)
    )

    title = naming.PLATFORM_NAMES.get(platform, platform)
    target.write_text(
        "---\n"
        f"date: {start_dt.isoformat(timespec='seconds')}\n"
        "source: meeting-recorder\n"
        f"platform: {platform}\n"
        f"duration_sec: {dur:.1f}\n"
        "---\n\n"
        f"# {title} — {start_dt:%Y-%m-%d %H:%M}\n\n"
        f"{transcript}\n",
        encoding="utf-8",
    )

    tmp.unlink(missing_ok=True)
    m4a.unlink(missing_ok=True)
    log(f"✓ готово: {target.name}")


# ----------------------------------------------------------------- service ---
class RecorderService:
    """Watch loop, wrapped so the menu-bar UI can start/stop/toggle it."""

    def __init__(self) -> None:
        self.rec = Recorder()
        self.idle_since: dt.datetime | None = None
        self.enabled = True
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _tick(self) -> None:
        if not self.enabled:
            if self.rec.recording:
                self.rec.stop_and_finalize()
                self.idle_since = None
            return

        # Without a key the recording could never be transcribed — don't burn
        # disk and permissions capturing audio nobody asked to keep.
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
            except Exception as e:  # noqa: BLE001 — the watcher must never die
                log(f"Ошибка цикла: {type(e).__name__}: {e}")
            self._stop.wait(POLL_SECONDS)

    def run_in_thread(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def run_blocking(self) -> None:
        try:
            self._loop()
        finally:
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


# -------------------------------------------------------------------- main ---
def main() -> int:
    if not config.macos_supported():
        v = ".".join(str(p) for p in config.macos_version())
        log(f"Нужна macOS 14.2 или новее (сейчас {v}). "
            f"Запись системного звука на более старых версиях невозможна.")
        return 1

    log(f"meeting-recorder запущен (помощник={'ok' if TAP_HELPER.exists() else 'НЕТ'}, "
        f"ffmpeg={'ok' if Path(FFMPEG).exists() else 'нет'}, "
        f"ключ={'задан' if config.is_configured() else 'НЕ ЗАДАН'})")

    if not TAP_HELPER.exists():
        log(f"Помощник записи не собран: {TAP_HELPER}. Запусти: bash install.sh")
        return 1
    if not config.is_configured():
        log("Не задан ключ Deepgram. Укажи его в config.json или через меню приложения.")
        return 1

    RecorderService().run_blocking()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
