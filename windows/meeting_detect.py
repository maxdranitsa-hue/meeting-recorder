#!/usr/bin/env python3
"""Определение активной видеовстречи на Windows.

Смотрит на окна и процессы:
  * Zoom в звонке   — окно класса ZPContentViewWndClass (окно конференции)
    либо процесс Zoom.exe с окном, чей заголовок похож на конференцию
  * Google Meet     — окно браузера с заголовком «Meet - xxx-xxxx-xxx»
  * Яндекс Телемост — окно браузера с «Телемост» в заголовке

Обычные телефонные звонки и просто запущенный Zoom без встречи не считаются.

Только стандартная библиотека: ctypes к user32, без pywin32.

Посмотреть, что видит программа прямо сейчас (пригодится, если встреча не
определяется):

    python meeting_detect.py --list
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import sys

IS_WINDOWS = sys.platform.startswith("win")

# ctypes.wintypes существует только на Windows — импорт на другой системе
# падает с ValueError, а модуль должен хотя бы читаться где угодно.
if IS_WINDOWS:
    from ctypes import wintypes

# Окно конференции Zoom. Класс не зависит от языка интерфейса, в отличие от
# заголовка, поэтому проверяем в первую очередь его.
ZOOM_MEETING_CLASSES = {"ZPContentViewWndClass", "ZPFloatVideoWndClass"}
ZOOM_PROCESS = "zoom.exe"

# Google Meet ставит в заголовок код встречи — он и отличает активный звонок
# от списка встреч на главной странице.
MEET_TITLE_RE = re.compile(r"\bMeet\b.*[a-z]{3}-[a-z]{4}-[a-z]{3}", re.I)
MEET_TITLE_SIMPLE_RE = re.compile(r"[a-z]{3}-[a-z]{4}-[a-z]{3}", re.I)
TELEMOST_TITLE_RE = re.compile(r"телемост|telemost", re.I)

BROWSER_HINTS = ("chrome", "edge", "yandex", "opera", "brave", "firefox",
                 "хром", "браузер")


def _visible_windows() -> list[tuple[str, str]]:
    """[(заголовок, класс окна)] для всех видимых окон."""
    if not IS_WINDOWS:
        return []

    user32 = ctypes.windll.user32
    results: list[tuple[str, str]] = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, 256)
        results.append((title_buf.value, class_buf.value))
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(callback), 0)
    except Exception:  # noqa: BLE001 — детект не должен ронять программу
        return []
    return results


def _running_processes() -> set[str]:
    """Имена запущенных процессов в нижнем регистре."""
    if not IS_WINDOWS:
        return set()
    try:
        r = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    names = set()
    for line in (r.stdout or "").splitlines():
        if line.startswith('"'):
            names.add(line.split('","')[0].strip('"').lower())
    return names


def _looks_like_browser(title: str, win_class: str) -> bool:
    haystack = f"{title} {win_class}".lower()
    return any(hint in haystack for hint in BROWSER_HINTS)


def detect_meeting() -> str | None:
    """'zoom' / 'meet' / 'telemost', либо None если встречи нет."""
    if not IS_WINDOWS:
        return None

    windows = _visible_windows()

    for title, win_class in windows:
        if win_class in ZOOM_MEETING_CLASSES:
            return "zoom"

    for title, win_class in windows:
        if not _looks_like_browser(title, win_class):
            continue
        if TELEMOST_TITLE_RE.search(title):
            return "telemost"
        if MEET_TITLE_RE.search(title):
            return "meet"
        # Chrome иногда оставляет в заголовке только код встречи
        if "meet.google.com" in title.lower() and MEET_TITLE_SIMPLE_RE.search(title):
            return "meet"

    # Запасной признак для Zoom: процесс идёт и есть окно, похожее на встречу.
    # Заголовок зависит от языка, поэтому это именно запасной путь.
    if ZOOM_PROCESS in _running_processes():
        for title, _ in windows:
            low = title.lower()
            if "zoom meeting" in low or "конференция zoom" in low or low == "zoom":
                return "zoom"
    return None


def describe() -> str:
    """Диагностика: что программа видит прямо сейчас."""
    if not IS_WINDOWS:
        return "Этот модуль работает только на Windows."

    windows = _visible_windows()
    lines = [f"Видимых окон: {len(windows)}", ""]
    lines.append("Окна (заголовок | класс):")
    for title, win_class in windows:
        marker = ""
        if win_class in ZOOM_MEETING_CLASSES:
            marker = "   ← окно конференции Zoom"
        elif _looks_like_browser(title, win_class) and (
            TELEMOST_TITLE_RE.search(title) or MEET_TITLE_RE.search(title)
        ):
            marker = "   ← вкладка со звонком"
        lines.append(f"  {title[:70]!r} | {win_class}{marker}")

    procs = _running_processes()
    lines.append("")
    lines.append(f"Zoom.exe запущен: {'да' if ZOOM_PROCESS in procs else 'нет'}")
    lines.append("")
    lines.append(f"ИТОГ: встреча {'определена: ' + str(detect_meeting())}"
                 if detect_meeting() else "ИТОГ: активной встречи не видно")
    return "\n".join(lines)


if __name__ == "__main__":
    if "--list" in sys.argv:
        print(describe())
    else:
        print(f"Активная встреча: {detect_meeting()}")
