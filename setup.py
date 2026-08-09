"""py2app build for the menu-bar app.

Driven by install.sh — you shouldn't need to run this by hand:

    .venv/bin/python setup.py py2app        # standalone bundle
    .venv/bin/python setup.py py2app -A     # alias bundle (dev / fallback)
"""

from setuptools import setup

APP = ["menubar_app.py"]

# Modules py2app must pull in even though they're imported dynamically.
# PyObjCTools is a namespace package — py2app's package scan uses the old
# imp.find_module and chokes on it, so its submodule goes in `includes`
# instead of `packages`.
INCLUDES = [
    "config", "transcription", "naming", "meeting_recorder",
    # rumps needs AppHelper; objc itself imports KeyValueCoding at import time.
    # The rest are cheap and spare us another round of "builds, then crashes".
    "PyObjCTools.AppHelper",
    "PyObjCTools.KeyValueCoding",
    "PyObjCTools.Conversion",
    "PyObjCTools.MachSignals",
    "PyObjCTools.Signals",
    "PyObjCTools.AppCategories",
    "PyObjCTools.FndCategories",
]

# rumps sits on PyObjC, and py2app's import scan does not reach these on its
# own — without them the bundle builds fine and then dies on launch with
# "No module named 'Foundation'". install.sh smoke-tests the bundle to catch
# exactly this.
PACKAGES = ["rumps", "objc", "Foundation", "AppKit", "CoreFoundation"]

# meeting_tap is the native capture helper; the app is useless without it.
DATA_FILES = ["meeting_tap"]

OPTIONS = {
    "argv_emulation": False,
    "includes": INCLUDES,
    "packages": PACKAGES,
    "plist": {
        "CFBundleName": "Запись звонков",
        "CFBundleDisplayName": "Запись звонков",
        "CFBundleIdentifier": "com.meetingrecorder.app",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSUIElement": True,          # menu-bar only, no Dock icon
        "LSMinimumSystemVersion": "14.2",
        "NSMicrophoneUsageDescription":
            "Запись вашего голоса во время видеовстреч для последующей расшифровки.",
        "NSAudioCaptureUsageDescription":
            "Запись звука собеседника во время видеовстреч для расшифровки.",
        "NSAppleEventsUsageDescription":
            "Определение активной встречи по открытым вкладкам Google Chrome.",
    },
}

setup(
    name="Запись звонков",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
