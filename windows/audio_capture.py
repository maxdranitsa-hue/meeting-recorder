#!/usr/bin/env python3
"""Запись обеих сторон разговора на Windows.

Пишет два потока одновременно:
  * системный звук (собеседник) — через WASAPI loopback, копия того, что идёт
    в колонки или наушники; выход при этом не трогается, слышно всё как обычно
  * микрофон (пользователь)

Потоки пишутся в два отдельных WAV и в конце сводятся в один моно-файл через
ffmpeg. Сводить на лету не получится честно: у микрофона и у колонок разная
частота дискретизации и свой дрейф, а `audioop` из стандартной библиотеки
удалён в Python 3.13. ffmpeg делает и ресемплинг, и микс.

Самопроверка (5 секунд, скажет что слышно, а что нет):

    python audio_capture.py --test
"""

from __future__ import annotations

import array
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

CHUNK = 1024
SAMPLE_WIDTH = 2          # paInt16
TARGET_RATE = 16000       # Deepgram не выигрывает от большего
SILENCE_THRESHOLD = 200   # ниже этого пика дорожку считаем пустой


class CaptureError(RuntimeError):
    """Захват не удался, и пользователю надо сказать почему."""


def _import_pyaudio():
    try:
        import pyaudiowpatch as pyaudio  # type: ignore
    except ImportError as e:
        raise CaptureError(
            "Не установлена библиотека PyAudioWPatch — без неё нельзя записать "
            "звук собеседника. Установи: pip install PyAudioWPatch"
        ) from e
    return pyaudio


def find_ffmpeg() -> str | None:
    import shutil
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (r"C:\ffmpeg\bin\ffmpeg.exe",
                      r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"):
        if Path(candidate).is_file():
            return candidate
    return None


class _StreamWriter:
    """Один поток захвата, пишущий в свой WAV."""

    def __init__(self, pa, device: dict, path: Path, label: str) -> None:
        self.pa = pa
        self.device = device
        self.path = path
        self.label = label
        self.channels = max(1, int(device.get("maxInputChannels", 1)))
        self.rate = int(device.get("defaultSampleRate", 48000))
        self.stream = None
        self.wav: wave.Wave_write | None = None
        self.error: str | None = None
        self.frames = 0

    def start(self, pyaudio) -> None:
        self.wav = wave.open(str(self.path), "wb")
        self.wav.setnchannels(self.channels)
        self.wav.setsampwidth(SAMPLE_WIDTH)
        self.wav.setframerate(self.rate)

        def callback(in_data, frame_count, time_info, status):
            try:
                if self.wav:
                    self.wav.writeframes(in_data)
                    self.frames += frame_count
            except Exception as e:  # noqa: BLE001 — не роняем поток захвата
                self.error = f"{type(e).__name__}: {e}"
            return (in_data, pyaudio.paContinue)

        try:
            self.stream = self.pa.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.rate,
                frames_per_buffer=CHUNK,
                input=True,
                input_device_index=int(self.device["index"]),
                stream_callback=callback,
            )
        except Exception as e:  # noqa: BLE001
            self.close()
            raise CaptureError(
                f"Не удалось открыть поток «{self.label}» "
                f"({self.device.get('name', '?')}): {type(e).__name__}: {e}"
            ) from e

    def close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:  # noqa: BLE001
                pass
            self.stream = None
        if self.wav is not None:
            try:
                self.wav.close()
            except Exception:  # noqa: BLE001
                pass
            self.wav = None


def peak_level(wav_path: Path) -> int:
    """Максимальная амплитуда дорожки — чтобы понять, есть ли там звук."""
    try:
        with wave.open(str(wav_path), "rb") as w:
            if w.getsampwidth() != 2:
                return -1
            frames = w.readframes(min(w.getnframes(), 16000 * 30))
    except (wave.Error, OSError):
        return -1
    samples = array.array("h")
    samples.frombytes(frames[: len(frames) // 2 * 2])
    return max((abs(s) for s in samples), default=0)


class DualCapture:
    """Запись системного звука и микрофона до вызова stop()."""

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.pa = None
        self.system: _StreamWriter | None = None
        self.mic: _StreamWriter | None = None
        self._started = threading.Event()

    def start(self, stamp: str) -> None:
        pyaudio = _import_pyaudio()
        self.pa = pyaudio.PyAudio()

        try:
            loopback = self.pa.get_default_wasapi_loopback()
        except Exception as e:  # noqa: BLE001
            self.pa.terminate()
            raise CaptureError(
                "Не найдено устройство WASAPI loopback — звук собеседника "
                "записать не получится. Обычно это значит, что звук выводится "
                "не через обычное устройство Windows. "
                f"({type(e).__name__}: {e})"
            ) from e
        if not loopback:
            self.pa.terminate()
            raise CaptureError("Windows не отдала устройство loopback для колонок.")

        self.system = _StreamWriter(
            self.pa, loopback, self.work_dir / f"{stamp}-system.wav", "звук собеседника"
        )
        self.system.start(pyaudio)

        # Микрофон не критичен: без него запись всё ещё содержит собеседника,
        # и потерять половину разговора лучше, чем не записать ничего.
        try:
            mic_device = self.pa.get_default_input_device_info()
            self.mic = _StreamWriter(
                self.pa, mic_device, self.work_dir / f"{stamp}-mic.wav", "микрофон"
            )
            self.mic.start(pyaudio)
        except Exception as e:  # noqa: BLE001
            self.mic = None
            print(f"[!] Микрофон недоступен, пишу только собеседника: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)

        self._started.set()

    def stop(self, out_path: Path) -> Path:
        """Останавливает запись и сводит дорожки в один моно-файл."""
        if self.system:
            self.system.close()
        if self.mic:
            self.mic.close()
        if self.pa:
            try:
                self.pa.terminate()
            except Exception:  # noqa: BLE001
                pass
            self.pa = None

        tracks = [w.path for w in (self.system, self.mic)
                  if w and w.path.exists() and w.path.stat().st_size > 1000]
        if not tracks:
            raise CaptureError("Ничего не записалось — обе дорожки пустые.")

        merged = mix_tracks(tracks, out_path)
        for path in tracks:
            path.unlink(missing_ok=True)
        return merged


def mix_tracks(tracks: list[Path], out_path: Path) -> Path:
    """Свести дорожки в один моно-файл 16 кГц. Одна дорожка — просто пережать."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise CaptureError(
            "Не найден ffmpeg — без него нельзя свести микрофон и звук "
            "собеседника в один файл. Установи: winget install ffmpeg"
        )

    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for track in tracks:
        cmd += ["-i", str(track)]
    if len(tracks) > 1:
        # normalize=0 — иначе микс делит громкость на число входов и тихая
        # сторона разговора становится совсем неразличимой.
        cmd += ["-filter_complex", f"amix=inputs={len(tracks)}:duration=longest:normalize=0"]
    cmd += ["-ar", str(TARGET_RATE), "-ac", "1", "-c:a", "aac", "-b:a", "64k",
            "-movflags", "+faststart", str(out_path)]

    r = subprocess.run(cmd, capture_output=True, timeout=1800)
    if r.returncode != 0 or not out_path.exists():
        raise CaptureError(
            "ffmpeg не смог свести дорожки: "
            f"{(r.stderr or b'').decode('utf-8', 'replace')[:300]}"
        )
    return out_path


# ------------------------------------------------------------ самопроверка ---
def self_test(seconds: int = 5) -> int:
    print("=" * 60)
    print("  Проверка записи звука")
    print("=" * 60)
    print()
    print(f"Записываю {seconds} секунд. Прямо сейчас:")
    print("  • включи любое видео со звуком (это будет «собеседник»)")
    print("  • и скажи что-нибудь в микрофон")
    print()

    work = Path.cwd() / "_capture_test"
    capture = DualCapture(work)
    try:
        capture.start("test")
    except CaptureError as e:
        print(f"✗ {e}")
        return 1

    system_path = capture.system.path if capture.system else None
    mic_path = capture.mic.path if capture.mic else None

    for left in range(seconds, 0, -1):
        print(f"  запись… {left}  ", end="\r", flush=True)
        time.sleep(1)
    print(" " * 30, end="\r")

    levels = {}
    for label, path in (("собеседник", system_path), ("микрофон", mic_path)):
        if path and path.exists():
            levels[label] = peak_level(path)

    out = work / "test-mixed.m4a"
    try:
        capture.stop(out)
    except CaptureError as e:
        print(f"✗ {e}")
        return 1

    print("Результат:")
    ok = True
    for label in ("собеседник", "микрофон"):
        level = levels.get(label)
        if level is None:
            print(f"  ✗ {label}: дорожка не создана")
            ok = False
        elif level < SILENCE_THRESHOLD:
            print(f"  ✗ {label}: тишина (уровень {level})")
            ok = False
        else:
            print(f"  ✓ {label}: звук есть (уровень {level})")
    print()
    print(f"Сведённый файл: {out}")
    print()

    if ok:
        print("✓ Захват работает. Можно запускать запись встреч.")
        return 0

    print("Что проверить:")
    print("  • «собеседник» молчит — звук выводится не на обычное устройство")
    print("    Windows, либо во время проверки ничего не играло")
    print("  • «микрофон» молчит — Windows не дала доступ к микрофону:")
    print("    Параметры → Конфиденциальность → Микрофон → разрешить")
    print("    классическим приложениям")
    return 1


if __name__ == "__main__":
    if "--test" in sys.argv:
        idx = sys.argv.index("--test")
        secs = int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else 5
        sys.exit(self_test(secs))
    print(__doc__)
    sys.exit(0)
