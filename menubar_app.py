#!/usr/bin/env python3
"""Menu-bar app for the meeting recorder.

    🎙 вкл   — armed, waiting for a meeting
    🔴 REC   — recording right now
    🎙 выкл  — paused
    ⚠️ настрой — no Deepgram key yet

First launch walks through: what the app does (including that it records the
other side), the Deepgram key, and where transcripts should land.

Runs inside its own .app bundle so macOS attributes the microphone and
Chrome-control permissions to the app rather than to a terminal.
"""

from __future__ import annotations

import subprocess
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# py2app bundles can start with an ASCII stdio locale; force UTF-8 so logging
# never trips over emoji or Cyrillic.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import rumps  # noqa: E402

import config  # noqa: E402
import naming  # noqa: E402
from meeting_recorder import RecorderService  # noqa: E402
from transcription import log  # noqa: E402

DEEPGRAM_SIGNUP = "https://console.deepgram.com/signup"


def choose_folder(prompt: str, default: Path) -> Path | None:
    """Native folder picker via osascript — no extra dependency."""
    script = (
        f'POSIX path of (choose folder with prompt "{prompt}" '
        f'default location POSIX file "{default}")'
    )
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", script],
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None  # user cancelled
    picked = (r.stdout or "").strip()
    return Path(picked) if picked else None


class MeetingRecorderApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("🎙", quit_button=None)

        self.status_item = rumps.MenuItem("Ожидание встречи")
        self.toggle_item = rumps.MenuItem("Запись звонков", callback=self.on_toggle)
        self.toggle_item.state = 1

        self.menu = [
            self.status_item,
            None,
            self.toggle_item,
            None,
            rumps.MenuItem("Открыть папку с расшифровками", callback=self.on_open_folder),
            rumps.MenuItem("Изменить папку…", callback=self.on_change_folder),
            rumps.MenuItem("Изменить ключ Deepgram…", callback=self.on_change_key),
            None,
            rumps.MenuItem("Показать журнал", callback=self.on_open_log),
            rumps.MenuItem("Выйти", callback=self.on_quit),
        ]

        self.service = RecorderService()
        self.service.run_in_thread()

        # A start line in the log is what makes remote diagnosis possible:
        # "открой журнал и пришли последние строки" has to show something.
        from meeting_recorder import TAP_HELPER
        log(f"приложение запущено (macOS {'.'.join(str(p) for p in config.macos_version())}, "
            f"помощник={'ok' if TAP_HELPER.is_file() else 'НЕТ'}, "
            f"ключ={'задан' if config.is_configured() else 'не задан'}, "
            f"папка={config.output_dir()})")

        if not config.macos_supported():
            self._warn_old_macos()
        elif not config.is_configured():
            # Let the menu bar draw before a modal appears on top of it.
            rumps.Timer(self._first_run, 1).start()

    # ------------------------------------------------------------ onboarding ---
    def _warn_old_macos(self) -> None:
        version = ".".join(str(p) for p in config.macos_version())
        rumps.alert(
            title="Нужна macOS 14.2 или новее",
            message=(
                f"На этом компьютере macOS {version}.\n\n"
                "Запись звука собеседника опирается на механизм, которого в "
                "более ранних версиях macOS нет. Обнови систему — приложение "
                "заработает без переустановки."
            ),
            ok="Понятно",
        )

    def _first_run(self, timer) -> None:
        timer.stop()

        agreed = rumps.alert(
            title="Запись звонков",
            message=(
                "Приложение само замечает начало встречи в Zoom, Google Meet "
                "или Яндекс Телемост, записывает обе стороны и превращает "
                "разговор в текст.\n\n"
                "Записывается и собеседник. Предупреждать участников о записи — "
                "твоя ответственность: во многих странах это требование закона.\n\n"
                "Телефонные и FaceTime-звонки не записываются."
            ),
            ok="Продолжить",
            cancel="Отмена",
        )
        if not agreed:
            return

        if not self._ask_key():
            return
        self._ask_folder(first_run=True)

        rumps.alert(
            title="Готово",
            message=(
                "Начни или войди во встречу — запись включится сама.\n\n"
                "При первой записи macOS спросит разрешение на микрофон и на "
                "запись звука этого компьютера. Оба нужно разрешить, иначе "
                "собеседник в запись не попадёт."
            ),
            ok="Понятно",
        )

    def _ask_key(self) -> bool:
        """Returns True once a key is stored."""
        rumps.alert(
            title="Ключ Deepgram",
            message=(
                "Расшифровка идёт через Deepgram — сервис работает на твоём "
                "аккаунте, ключ хранится только на этом компьютере.\n\n"
                "Новым аккаунтам Deepgram даёт 200 долларов бесплатно, они не "
                "сгорают. Часа записи стоит примерно 46 центов, так что этого "
                "хватает очень надолго.\n\n"
                "Сейчас откроется страница регистрации. Создай ключ и скопируй его."
            ),
            ok="Открыть страницу",
        )
        webbrowser.open(DEEPGRAM_SIGNUP)

        window = rumps.Window(
            title="Ключ Deepgram",
            message="Вставь ключ и нажми «Сохранить»:",
            default_text="",
            ok="Сохранить",
            cancel="Позже",
            dimensions=(320, 24),
        )
        response = window.run()
        key = (response.text or "").strip()
        if not response.clicked or not key:
            log("Ключ не введён — запись не начнётся, пока он не задан")
            return False

        config.update(deepgram_api_key=key)
        return True

    def _ask_folder(self, first_run: bool = False) -> None:
        current = config.output_dir()
        picked = choose_folder("Где хранить расшифровки встреч?", current.parent
                               if first_run else current)
        if picked:
            config.update(output_dir=str(picked / "Записи звонков"
                                         if first_run and picked == current.parent
                                         else picked))
        elif first_run:
            current.mkdir(parents=True, exist_ok=True)  # keep the default
        log(f"Папка для расшифровок: {config.output_dir()}")

    # --------------------------------------------------------------- status ---
    @rumps.timer(2)
    def refresh(self, _sender) -> None:
        state, platform = self.service.status()
        if state == "unconfigured":
            self.title = "⚠️ настрой"
            self.status_item.title = "Не задан ключ Deepgram"
        elif state == "recording":
            self.title = "🔴 REC"
            self.status_item.title = f"● Идёт запись: {naming.PLATFORM_NAMES.get(platform, platform)}"
        elif state == "idle":
            self.title = "🎙 вкл"
            self.status_item.title = "Ожидание встречи"
        else:
            self.title = "🎙 выкл"
            self.status_item.title = "Запись выключена"

    # -------------------------------------------------------------- actions ---
    def on_toggle(self, sender) -> None:
        sender.state = 0 if sender.state else 1
        self.service.set_enabled(bool(sender.state))

    def on_open_folder(self, _sender) -> None:
        folder = config.output_dir()
        folder.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(folder)], check=False)

    def on_change_folder(self, _sender) -> None:
        self._ask_folder()

    def on_change_key(self, _sender) -> None:
        window = rumps.Window(
            title="Ключ Deepgram",
            message="Вставь новый ключ:",
            default_text="",
            ok="Сохранить",
            cancel="Отмена",
            dimensions=(320, 24),
        )
        response = window.run()
        key = (response.text or "").strip()
        if response.clicked and key:
            config.update(deepgram_api_key=key)
            log("Ключ Deepgram обновлён")

    def on_open_log(self, _sender) -> None:
        config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.LOG_FILE.touch(exist_ok=True)
        subprocess.run(["open", str(config.LOG_FILE)], check=False)

    def on_quit(self, _sender) -> None:
        if self.service.rec.recording:
            keep = rumps.alert(
                title="Идёт запись",
                message="Встреча ещё записывается. Остановить и расшифровать?",
                ok="Остановить", cancel="Отмена",
            )
            if not keep:
                return
        self.service.set_enabled(False)
        rumps.quit_application()


if __name__ == "__main__":
    MeetingRecorderApp().run()
