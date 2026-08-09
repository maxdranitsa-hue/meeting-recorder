#!/usr/bin/env python3
"""Иконка в области уведомлений Windows для записи встреч.

    зелёная точка  — включено, ждём встречу
    красная точка  — идёт запись
    серая точка    — выключено

Правый клик по иконке — меню: включить или выключить запись, открыть папку с
расшифровками, посмотреть журнал, выйти.

    python tray_app.py

Обычно запускается автоматически при входе в систему — это настраивает
setup_windows.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "watcher"))
sys.path.insert(0, str(HERE))

import config  # noqa: E402
from transcription import log  # noqa: E402
from recorder import PLATFORM_NAMES, RecorderService  # noqa: E402

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    print("Не установлены pystray и Pillow — без них не будет иконки в трее.\n"
          "Установи: pip install pystray Pillow\n"
          "Либо запусти без иконки: python recorder.py", file=sys.stderr)
    raise SystemExit(1)

COLORS = {
    "idle": (46, 160, 67),          # зелёный — ждём встречу
    "recording": (218, 54, 51),     # красный — пишем
    "off": (110, 118, 129),         # серый — выключено
    "unconfigured": (219, 154, 4),  # жёлтый — не настроено
}


def make_icon(state: str) -> Image.Image:
    """Кружок нужного цвета — иконку рисуем сами, чтобы не тащить файл."""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, size - 8, size - 8), fill=COLORS.get(state, COLORS["off"]))
    if state == "recording":
        draw.ellipse((24, 24, size - 24, size - 24), fill=(255, 255, 255))
    return image


def open_path(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]  # только Windows
    except OSError as e:
        log(f"Не удалось открыть {path}: {e}")


class TrayApp:
    def __init__(self) -> None:
        self.service = RecorderService()
        self.icon = pystray.Icon(
            "meeting_recorder",
            make_icon("idle"),
            "Запись встреч",
            menu=pystray.Menu(
                pystray.MenuItem(lambda item: self._status_text(), None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Запись включена", self._toggle,
                                 checked=lambda item: self.service.enabled),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Папка с расшифровками", self._open_output),
                pystray.MenuItem("Журнал", self._open_log),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", self._quit),
            ),
        )

    def _status_text(self) -> str:
        state, platform = self.service.status()
        if state == "unconfigured":
            return "Не задан ключ Deepgram"
        if state == "recording":
            return f"Идёт запись: {PLATFORM_NAMES.get(platform, platform)}"
        if state == "off":
            return "Запись выключена"
        return "Ожидание встречи"

    def _refresh(self) -> None:
        state, _ = self.service.status()
        self.icon.icon = make_icon(state)
        self.icon.title = f"Запись встреч — {self._status_text()}"

    def _toggle(self, icon, item) -> None:
        self.service.set_enabled(not self.service.enabled)
        self._refresh()

    def _open_output(self, icon, item) -> None:
        folder = config.output_dir()
        folder.mkdir(parents=True, exist_ok=True)
        open_path(folder / ".")

    def _open_log(self, icon, item) -> None:
        config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.LOG_FILE.touch(exist_ok=True)
        open_path(config.LOG_FILE)

    def _quit(self, icon, item) -> None:
        self.service.shutdown()
        icon.stop()

    def run(self) -> None:
        if not config.is_configured():
            log("Ключ Deepgram не задан — запусти setup_windows.py")

        self.service.run_in_thread()

        def setup(icon):
            icon.visible = True
            import time
            while icon.visible:
                self._refresh()
                time.sleep(2)

        self.icon.run(setup=setup)


if __name__ == "__main__":
    if not sys.platform.startswith("win"):
        print("Иконка в трее рассчитана на Windows.", file=sys.stderr)
        raise SystemExit(1)
    TrayApp().run()
