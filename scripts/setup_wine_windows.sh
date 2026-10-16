#!/usr/bin/env bash
# DeskLink - bootstrap Windows Python w IZOLOWANYM prefiksie Wine.
# Nic nie dotyka głównego ~/.wine (tam masz AnyDesk/GOG).
# Efekt: wine C:\Python311\python.exe gotowy do uruchomienia DeskLink i budowania .exe.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${DESKLINK_WINEPREFIX:-$HERE/.wine-desklink}"
VENDOR="$HERE/vendor"
PY_VER="3.11.9"
PY_EXE="python-$PY_VER-amd64.exe"
PY_URL="https://www.python.org/ftp/python/$PY_VER/$PY_EXE"
PY_WIN="C:\\Python311"
PY_WIN_UNIX="$PREFIX/drive_c/Python311"
LOG="${2:-$HERE/wine-setup.log}"

export WINEPREFIX="$PREFIX"
export WINEDEBUG="${WINEDEBUG:--all}"
export DISPLAY="${DISPLAY:-:0}"

wipy() { wine "$PY_WIN\\python.exe" "$@"; }

log() { echo "[setup-wine] $*" | tee -a "$LOG"; }

mkdir -p "$VENDOR"
: > "$LOG"

# 1. Pobierz instalator Windows Pythona.
if [ ! -f "$VENDOR/$PY_EXE" ]; then
  log "pobieram $PY_EXE ..."
  curl -fsSL --retry 3 -o "$VENDOR/$PY_EXE.part" "$PY_URL" || { log "BLAD: pobieranie nie poszlo"; exit 1; }
  mv "$VENDOR/$PY_EXE.part" "$VENDOR/$PY_EXE"
fi
log "instalator: $(du -h "$VENDOR/$PY_EXE" | cut -f1)"

# 2. Prefiks Wine (tworzenie trwa ~1-2 min przy pierwszym razie).
if [ ! -d "$PREFIX/drive_c/windows/system32" ]; then
  log "tworze prefiks Wine w $PREFIX ..."
  wineboot -u >>"$LOG" 2>&1 || { log "BLAD: wineboot"; exit 1; }
fi
log "prefiks OK"

# 3. Python (cicha instalacja MSI przez Wine) - jesli jeszcze nie zainstalowany.
if [ ! -x "$PY_WIN_UNIX/python.exe" ]; then
  log "instaluje Python $PY_VER w prefiksie ..."
  wine "$VENDOR/$PY_EXE" /quiet InstallAllUsers=1 PrependPath=0 Include_tcltk=1 \
       Include_pip=1 Include_test=0 Shortcuts=0 TargetDir="$PY_WIN" >>"$LOG" 2>&1
  wineboot -u >>"$LOG" 2>&1 || true
fi

for i in $(seq 1 40); do
  [ -x "$PY_WIN_UNIX/python.exe" ] && break
  sleep 3
done
if [ ! -x "$PY_WIN_UNIX/python.exe" ]; then
  log "BLAD: Python nie zostal zainstalowany w prefiksie - zobacz $LOG"
  exit 2
fi
log "python: $(wipy --version 2>&1 | tr -d '\r')"
wipy -c "import tkinter, ssl, ctypes; print('stdlib OK:', tkinter.TkVersion)" 2>&1 | tr -d '\r' | tee -a "$LOG"

# 4. Zaleznosci aplikacji + PyInstaller (do budowy natywnego .exe).
log "pip install zaleznosci (mss, pillow, pyautogui, pyperclip, cryptography, pyinstaller) ..."
wipy -m pip install --no-input --disable-pip-version-check --upgrade pip >>"$LOG" 2>&1
wipy -m pip install --no-input --disable-pip-version-check \
     "pillow" "mss" "pyautogui" "pyperclip" "cryptography" "pyinstaller" >>"$LOG" 2>&1 \
  || log "UWAGA: pip przez Wine zwolnił/przerwane - sprobuj ponownie"
wipy -c "import PIL,mss,pyautogui,cryptography;print('deps OK')" 2>&1 | tr -d '\r' | tee -a "$LOG"

log "GOTOWE. Uruchom: wine C:\\\\Python311\\\\python.exe -m rd   (z katalogu $HERE)"
log "Log: $LOG"
