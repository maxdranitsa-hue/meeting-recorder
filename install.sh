#!/usr/bin/env bash
#
# Установка «Записи звонков».
#
# Проверяет систему, ставит недостающее, собирает нативный помощник записи
# и меню-бар приложение, кладёт его в ~/Applications.
#
#   bash install.sh          — с подтверждениями
#   bash install.sh -y       — ставить зависимости без вопросов
#
set -euo pipefail

cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
VENV="$PROJECT_DIR/.venv"
APP_NAME="Запись звонков.app"
# Переопределяется для тестов: INSTALL_DIR=/tmp/proba bash install.sh
INSTALL_DIR="${INSTALL_DIR:-$HOME/Applications}"

ASSUME_YES=false
[[ "${1:-}" == "-y" ]] && ASSUME_YES=true

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
die()  { printf "\n\033[31m✗ %s\033[0m\n" "$1" >&2; exit 1; }

confirm() {
  $ASSUME_YES && return 0
  read -r -p "  $1 [Y/n] " reply </dev/tty
  [[ -z "$reply" || "$reply" =~ ^[YyДд]$ ]]
}

# ---------------------------------------------------------------- проверки ---
bold "1/6  Проверяю систему"

os_version="$(sw_vers -productVersion)"
os_major="${os_version%%.*}"
os_minor="$(echo "$os_version" | cut -d. -f2)"
os_minor="${os_minor:-0}"
if (( os_major < 14 )) || { (( os_major == 14 )) && (( os_minor < 2 )); }; then
  die "Нужна macOS 14.2 или новее, у вас $os_version.
     Запись звука собеседника опирается на Core Audio process tap —
     в более ранних версиях macOS этого механизма нет."
fi
ok "macOS $os_version"

if [[ "$(uname -s)" != "Darwin" ]]; then
  die "Это приложение работает только на macOS."
fi

if ! xcode-select -p >/dev/null 2>&1; then
  warn "Не установлены Command Line Tools — без них не собрать помощник записи."
  if confirm "Запустить установку сейчас?"; then
    xcode-select --install || true
    die "Дождитесь окончания установки Command Line Tools и запустите install.sh снова."
  else
    die "Установите их командой: xcode-select --install"
  fi
fi
ok "Command Line Tools на месте"

if ! command -v brew >/dev/null 2>&1; then
  die "Не найден Homebrew — через него ставятся ffmpeg и Python.
     Установите с https://brew.sh и запустите install.sh снова."
fi
ok "Homebrew на месте"

# ------------------------------------------------------------ зависимости ---
bold "2/6  Зависимости"

if ! command -v ffmpeg >/dev/null 2>&1; then
  warn "Нет ffmpeg — он сжимает запись перед отправкой на расшифровку."
  if confirm "Установить ffmpeg через brew?"; then
    brew install ffmpeg
  else
    warn "Пропускаю. Записи будут отправляться без сжатия — это дольше."
  fi
fi
command -v ffmpeg >/dev/null 2>&1 && ok "ffmpeg $(ffmpeg -version | head -1 | awk '{print $3}')"

# Системный python 3.9 слишком стар для rumps/py2app — нужен свежий.
PYTHON=""
for candidate in \
    /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
    /opt/homebrew/bin/python3.11 /usr/local/bin/python3.11; do
  [[ -x "$candidate" ]] && { PYTHON="$candidate"; break; }
done

if [[ -z "$PYTHON" ]]; then
  warn "Не найден Python 3.11+ (системный $(/usr/bin/python3 --version 2>&1 | awk '{print $2}') слишком старый)."
  if confirm "Установить python@3.11 через brew?"; then
    brew install python@3.11
    PYTHON="$(brew --prefix)/bin/python3.11"
  else
    die "Нужен Python 3.11 или новее."
  fi
fi
ok "Python $("$PYTHON" --version | awk '{print $2}')"

# ------------------------------------------------- нативный помощник записи ---
bold "3/6  Собираю помощник записи"

swiftc -O meeting_tap.swift -o meeting_tap \
  -framework CoreAudio -framework AudioToolbox -framework Foundation \
  || die "Не удалось собрать meeting_tap. Проверьте, что установлены Command Line Tools."
ok "meeting_tap собран"

# ------------------------------------------------------------------- venv ---
bold "4/6  Виртуальное окружение"

if [[ ! -d "$VENV" ]]; then
  "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet rumps py2app
ok "rumps и py2app установлены"

# ------------------------------------------------------------------ сборка ---
bold "5/6  Собираю приложение"

rm -rf build dist

BUILD_MODE="standalone"
if ! "$VENV/bin/python" setup.py py2app >/tmp/meetingrec-build.log 2>&1; then
  warn "Обычная сборка не прошла (подробности: /tmp/meetingrec-build.log)."
  warn "Пробую режим alias — приложение будет ссылаться на эту папку."
  rm -rf build dist
  "$VENV/bin/python" setup.py py2app -A >/tmp/meetingrec-build-alias.log 2>&1 \
    || die "Сборка не удалась. Загляните в /tmp/meetingrec-build-alias.log"
  BUILD_MODE="alias"
fi

[[ -d "dist/$APP_NAME" ]] || die "Приложение не появилось в dist/ — смотрите /tmp/meetingrec-build.log"
ok "Сборка готова ($BUILD_MODE)"

# PyObjCTools — namespace-пакет без __init__.py. py2app такие не видит ни через
# packages, ни через includes, а objc импортирует его при загрузке. Без этого
# приложение собирается и падает при запуске. Кладём папку в бандл руками.
if [[ "$BUILD_MODE" == "standalone" ]]; then
  # Кавычки только вокруг путей с пробелами, звёздочка снаружи — иначе глоб
  # не раскроется («Запись звонков.app» содержит пробел).
  src_matches=("$VENV/lib/"python3.*/site-packages/PyObjCTools)
  dst_matches=("dist/$APP_NAME/Contents/Resources/lib/"python3.*)
  if [[ -d "${src_matches[0]}" && -d "${dst_matches[0]}" ]]; then
    cp -R "${src_matches[0]}" "${dst_matches[0]}/"
    ok "PyObjCTools добавлен в приложение"
  else
    warn "Не нашёл PyObjCTools — приложение может не запуститься"
  fi
fi

# Бандл может собраться и упасть при запуске — например, если py2app не втянул
# PyObjC. Проверяем импорты до того, как пользователь решит, что «приложение
# просто не работает». В alias-режиме модули берутся из venv, в обычном — из
# самого бандла, поэтому проверяем разными интерпретаторами.
RES_DIR="$PROJECT_DIR/dist/$APP_NAME/Contents/Resources"
BUNDLE_PY="dist/$APP_NAME/Contents/MacOS/python"
SMOKE_IMPORTS="import rumps, config, transcription, naming, meeting_recorder"

if [[ "$BUILD_MODE" == "standalone" && -x "$BUNDLE_PY" ]]; then
  SMOKE_PY="$BUNDLE_PY"
  # Воспроизводим пути, которые бандлу настраивает его загрузчик.
  SMOKE_CODE="import sys, glob, os
sys.path[:0] = [os.environ['RESOURCEPATH']] + glob.glob(os.path.join(os.environ['RESOURCEPATH'], 'lib', 'python3.*'))
$SMOKE_IMPORTS"
else
  SMOKE_PY="$VENV/bin/python"
  SMOKE_CODE="$SMOKE_IMPORTS"
fi

if ! RESOURCEPATH="$RES_DIR" "$SMOKE_PY" -c "$SMOKE_CODE" \
     >/tmp/meetingrec-smoke.log 2>&1; then
  warn "Приложение собралось, но не смогло загрузиться:"
  tail -3 /tmp/meetingrec-smoke.log | sed 's/^/      /'
  die "Сборка неполная. Полный вывод: /tmp/meetingrec-smoke.log"
fi
ok "Проверка запуска пройдена"

# --------------------------------------------------------------- установка ---
bold "6/6  Устанавливаю"

mkdir -p "$INSTALL_DIR"
if [[ -d "$INSTALL_DIR/$APP_NAME" ]]; then
  pkill -f "$APP_NAME/Contents/MacOS" 2>/dev/null || true
  rm -rf "$INSTALL_DIR/$APP_NAME"
fi
cp -R "dist/$APP_NAME" "$INSTALL_DIR/"

# Ad-hoc signature: TCC ties microphone and audio permissions to the app's
# identity, and an unsigned bundle gets a new one on every rebuild — which
# would make macOS re-ask for permissions each time.
codesign --force --deep --sign - "$INSTALL_DIR/$APP_NAME" 2>/dev/null \
  && ok "Приложение подписано локально" \
  || warn "Не удалось подписать — macOS может переспрашивать разрешения после пересборки"

ok "Установлено: $INSTALL_DIR/$APP_NAME"

cat <<EOF

$(bold "Готово.")

Запустить:   open "$INSTALL_DIR/$APP_NAME"
Иконка 🎙 появится в строке меню сверху. При первом запуске приложение
попросит ключ Deepgram и папку для расшифровок.

Автозапуск:  Системные настройки → Основные → Элементы входа → добавить
             «Запись звонков».

EOF

if [[ "$BUILD_MODE" == "alias" ]]; then
  cat <<EOF
$(warn "Важно: собрано в режиме alias.")
  Приложение ссылается на файлы в папке
      $PROJECT_DIR
  Не удаляйте и не переименовывайте её, иначе приложение перестанет работать.

EOF
fi

echo "Разрешения, которые запросит macOS при первой записи:"
echo "  • Микрофон — ваш голос"
echo "  • Запись звука с этого компьютера — голос собеседника"
echo "  • Управление Google Chrome — определение встреч в Meet и Телемост"
echo "Все три нужно разрешить."
echo
