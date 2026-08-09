#!/usr/bin/env python3
"""Установка автоматической записи встреч на Windows.

Ставит зависимости, спрашивает ключ Deepgram и папку для расшифровок,
проверяет, что звук реально записывается, и настраивает автозапуск.

    python setup_windows.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHARED = HERE.parent / "watcher"
sys.path.insert(0, str(SHARED))
sys.path.insert(0, str(HERE))

import config  # noqa: E402

TRAY_APP = HERE / "tray_app.py"
DEEPGRAM_SIGNUP = "https://console.deepgram.com/signup"
PIP_PACKAGES = ["PyAudioWPatch", "pystray", "Pillow"]


def say(msg: str = "") -> None:
    print(msg)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        return input(f"{prompt}{suffix}: ").strip() or default
    except EOFError:
        return default


def yes(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{prompt} ({hint})").lower()
    if not answer:
        return default
    return answer in ("y", "yes", "д", "да")


def pick_folder(title: str, initial: Path) -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        initial.mkdir(parents=True, exist_ok=True)
        picked = filedialog.askdirectory(title=title, initialdir=str(initial))
        root.destroy()
    except Exception:  # noqa: BLE001
        return None
    return Path(picked) if picked else None


# ------------------------------------------------------------- зависимости ---
def install_packages() -> bool:
    say("Ставлю библиотеки: " + ", ".join(PIP_PACKAGES))
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", *PIP_PACKAGES],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        say("  ✗ Не получилось:")
        say("    " + (r.stderr or "").strip()[-400:])
        return False
    say("  ✓ Библиотеки установлены")
    return True


def check_ffmpeg() -> bool:
    sys.path.insert(0, str(HERE))
    from audio_capture import find_ffmpeg

    if find_ffmpeg():
        say("  ✓ ffmpeg на месте")
        return True

    say("  ! Не найден ffmpeg — он сводит микрофон и звук собеседника в один файл.")
    if yes("    Установить через winget?"):
        r = subprocess.run(["winget", "install", "--id", "Gyan.FFmpeg", "-e",
                            "--accept-source-agreements", "--accept-package-agreements"],
                           capture_output=False)
        if r.returncode == 0:
            say("  ✓ ffmpeg установлен. Возможно, понадобится перезапустить консоль.")
            return True
        say("  ✗ winget не справился.")
    say("    Поставь вручную: winget install ffmpeg — и запусти установку снова.")
    return False


# -------------------------------------------------------------- автозапуск ---
def setup_autostart() -> None:
    try:
        startup = Path(os.environ["APPDATA"]) / (
            "Microsoft/Windows/Start Menu/Programs/Startup"
        )
        startup.mkdir(parents=True, exist_ok=True)
        exe = Path(sys.executable)
        pythonw = exe.with_name("pythonw.exe")
        runner = pythonw if pythonw.is_file() else exe
        bat = startup / "ЗаписьВстреч.bat"
        bat.write_text(
            '@echo off\r\n'
            f'start "" "{runner}" "{TRAY_APP}"\r\n',
            encoding="utf-8",
        )
        say(f"  ✓ Автозапуск настроен: {bat}")
    except Exception as e:  # noqa: BLE001
        say(f"  ! Не удалось настроить автозапуск: {type(e).__name__}: {e}")
        say(f"    Запускать вручную: python {TRAY_APP}")


# --------------------------------------------------------------------- main ---
def main() -> int:
    if not sys.platform.startswith("win"):
        say("Эта установка для Windows.")
        say("На macOS 14.2+ — приложение из корня проекта, оно удобнее.")
        say("На Linux — папка watcher.")
        return 1

    say("=" * 62)
    say("  Автоматическая запись встреч — установка")
    say("=" * 62)
    say()
    say("Программа сама заметит начало встречи в Zoom, Google Meet или")
    say("Яндекс Телемост, запишет обе стороны и превратит разговор в текст.")
    say()
    say("Записывается и собеседник, причём Zoom об этом не знает — значит")
    say("индикатора записи участники не увидят. Предупреждать их о записи —")
    say("ваша ответственность, во многих странах это требование закона.")
    say()
    if not yes("Продолжить?"):
        return 1

    say()
    say("-" * 62)
    say("1. Библиотеки")
    if not install_packages():
        say("\nБез них дальше нельзя. Попробуй запустить установку от имени")
        say("администратора или проверь подключение к интернету.")
        return 1
    if not check_ffmpeg():
        return 1

    say()
    say("-" * 62)
    say("2. Ключ Deepgram — сервис, который превращает речь в текст")
    say()
    cfg = config.load()
    key = cfg["deepgram_api_key"]
    if key and not yes("  Ключ уже сохранён. Заменить?", default=False):
        pass
    else:
        key = ""
        say("  Новым аккаунтам Deepgram даёт 200 долларов бесплатно, и они не")
        say("  сгорают. Час записи стоит около 46 центов.")
        say()
        if yes("  Открыть страницу регистрации?"):
            webbrowser.open(DEEPGRAM_SIGNUP)
        while not key:
            key = ask("  Вставь ключ Deepgram")
            if not key and not yes("  Без ключа расшифровки не будет. Ещё раз?"):
                return 1

    say()
    say("-" * 62)
    say("3. Куда складывать расшифровки")
    default_out = config.default_output_dir()
    say(f"  По умолчанию: {default_out}")
    output = default_out
    if yes("  Выбрать другую папку?", default=False):
        picked = pick_folder("Папка для расшифровок", default_out)
        output = picked or Path(ask("  Путь к папке", str(default_out))).expanduser()
    output.mkdir(parents=True, exist_ok=True)

    config.update(deepgram_api_key=key, output_dir=str(output))

    say()
    say("-" * 62)
    say("4. Проверка звука")
    say()
    say("  Сейчас запишу 5 секунд и скажу, что слышно. Приготовься:")
    say("  включи видео со звуком и скажи что-нибудь в микрофон.")
    say()
    if yes("  Начать проверку?"):
        from audio_capture import self_test
        if self_test(5) != 0:
            say()
            say("  Проверка не прошла. Установку можно продолжить, но сначала")
            say("  стоит разобраться со звуком — иначе записи будут пустыми.")
            if not yes("  Всё равно продолжить?", default=False):
                return 1

    say()
    say("-" * 62)
    say("5. Автозапуск вместе с Windows")
    if yes("  Настроить?"):
        setup_autostart()

    say()
    say("=" * 62)
    say("  Готово")
    say("=" * 62)
    say(f"  Расшифровки: {output}")
    say(f"  Журнал:      {config.LOG_FILE}")
    say()
    say("  Иконка появится в области уведомлений, рядом с часами.")
    say("  Зелёная — ждёт встречу, красная — идёт запись.")
    say()

    if yes("Запустить сейчас?"):
        subprocess.Popen([sys.executable, str(TRAY_APP)])
        say("Запущено — ищи иконку рядом с часами.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\nОтменено.")
        sys.exit(1)
