#!/usr/bin/env python3
"""Первичная настройка транскрибатора встреч.

Спрашивает ключ Deepgram, папку с записями и папку для расшифровок,
настраивает автозапуск вместе с системой и проверяет, что всё сходится.

    python setup_watcher.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402

HERE = Path(__file__).resolve().parent
WATCHER = HERE / "watcher.py"
DEEPGRAM_SIGNUP = "https://console.deepgram.com/signup"


def say(msg: str = "") -> None:
    print(msg)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def yes(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{prompt} ({hint})").lower()
    if not answer:
        return default
    return answer in ("y", "yes", "д", "да")


def pick_folder(title: str, initial: Path) -> Path | None:
    """Native folder picker when a GUI is available, else typed input."""
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
    except Exception:  # noqa: BLE001 — headless machine, fall back to typing
        return None
    return Path(picked) if picked else None


def choose_dir(title: str, default: Path) -> Path:
    say(f"\n{title}")
    say(f"  По умолчанию: {default}")
    if yes("  Открыть окно выбора папки?"):
        picked = pick_folder(title, default)
        if picked:
            return picked
        say("  Окно выбора недоступно — введи путь вручную.")
    typed = ask("  Путь к папке", str(default))
    return Path(typed).expanduser()


# ---------------------------------------------------------------- автозапуск ---
def python_for_background() -> str:
    """pythonw на Windows — чтобы не висело чёрное окно консоли."""
    exe = Path(sys.executable)
    if config.IS_WINDOWS:
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
    return str(exe)


def setup_autostart_windows() -> str:
    startup = Path(os.environ["APPDATA"]) / (
        "Microsoft/Windows/Start Menu/Programs/Startup"
    )
    startup.mkdir(parents=True, exist_ok=True)
    bat = startup / "MeetingTranscriber.bat"
    bat.write_text(
        '@echo off\r\n'
        f'start "" "{python_for_background()}" "{WATCHER}"\r\n',
        encoding="utf-8",
    )
    return str(bat)


def setup_autostart_macos() -> str:
    label = "com.meetingtranscriber.watcher"
    plist_dir = Path.home() / "Library/LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist = plist_dir / f"{label}.plist"
    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f'  <key>Label</key><string>{label}</string>\n'
        '  <key>ProgramArguments</key><array>\n'
        f'    <string>{sys.executable}</string>\n'
        f'    <string>{WATCHER}</string>\n'
        '  </array>\n'
        '  <key>RunAtLoad</key><true/>\n'
        '  <key>KeepAlive</key><true/>\n'
        f'  <key>StandardErrorPath</key><string>{config.LOG_FILE}</string>\n'
        '</dict></plist>\n',
        encoding="utf-8",
    )
    subprocess.run(["launchctl", "unload", str(plist)],
                   capture_output=True, check=False)
    subprocess.run(["launchctl", "load", str(plist)],
                   capture_output=True, check=False)
    return str(plist)


def setup_autostart_linux() -> str:
    autostart = Path.home() / ".config/autostart"
    autostart.mkdir(parents=True, exist_ok=True)
    desktop = autostart / "meeting-transcriber.desktop"
    desktop.write_text(
        "[Desktop Entry]\nType=Application\nName=Meeting Transcriber\n"
        f"Exec={sys.executable} {WATCHER}\nX-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    return str(desktop)


def setup_autostart() -> None:
    try:
        if config.IS_WINDOWS:
            path = setup_autostart_windows()
        elif config.IS_MAC:
            path = setup_autostart_macos()
        else:
            path = setup_autostart_linux()
        say(f"  ✓ Автозапуск настроен: {path}")
    except Exception as e:  # noqa: BLE001
        say(f"  ! Не удалось настроить автозапуск: {type(e).__name__}: {e}")
        say(f"    Запускать вручную: {sys.executable} {WATCHER}")


# --------------------------------------------------------------------- main ---
def main() -> int:
    say("=" * 62)
    say("  Транскрибатор встреч — настройка")
    say("=" * 62)
    say()
    say("Программа следит за папкой, куда сохраняются записи встреч.")
    say("Появилась новая запись — она сама превращается в текст и")
    say("ложится в выбранную вами папку.")
    say()
    say("Записи делает сам Zoom (или другая программа) — включите в нём")
    say("локальную запись. Записывать может организатор встречи или")
    say("участник, которому организатор разрешил.")
    say()

    cfg = config.load()

    # 1. Ключ
    say("-" * 62)
    say("1. Ключ Deepgram — сервис, который превращает речь в текст")
    say()
    if cfg["deepgram_api_key"]:
        say("  Ключ уже сохранён.")
        if not yes("  Заменить его?", default=False):
            key = cfg["deepgram_api_key"]
        else:
            key = ""
    else:
        key = ""

    if not key:
        say("  Новым аккаунтам Deepgram даёт 200 долларов бесплатно, и они не")
        say("  сгорают. Час записи стоит около 46 центов — хватает надолго.")
        say()
        if yes("  Открыть страницу регистрации?"):
            webbrowser.open(DEEPGRAM_SIGNUP)
        while not key:
            key = ask("  Вставь ключ Deepgram")
            if not key:
                say("  Без ключа расшифровка невозможна.")
                if not yes("  Попробовать ещё раз?"):
                    return 1

    # 2. Папки
    watch = choose_dir("2. Папка, где появляются записи встреч",
                       config.default_watch_dir())
    output = choose_dir("3. Папка, куда складывать расшифровки",
                        config.default_output_dir())
    output.mkdir(parents=True, exist_ok=True)

    config.update(deepgram_api_key=key, watch_dirs=[str(watch)],
                  output_dir=str(output))

    say()
    say("-" * 62)
    say("4. Автозапуск вместе с компьютером")
    if yes("  Настроить?"):
        setup_autostart()

    say()
    say("=" * 62)
    say("  Готово")
    say("=" * 62)
    say(f"  Слежу за папкой:  {watch}")
    say(f"  Расшифровки в:    {output}")
    say(f"  Журнал:           {config.LOG_FILE}")
    say()
    if not watch.exists():
        say("  ! Папки с записями пока нет — она появится после первой")
        say("    записанной встречи. Это нормально.")
        say()
    say("  Записи, которые уже лежат в папке, пропускаются — расшифровываются")
    say("  только новые встречи.")
    say()

    if yes("Запустить прямо сейчас?"):
        say("\nРаботаю. Останови сочетанием Ctrl+C.\n")
        os.execv(sys.executable, [sys.executable, str(WATCHER)])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\nОтменено.")
        sys.exit(1)
