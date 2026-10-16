#!/usr/bin/env bash
# DeskLink - budowa pliku .exe przez PyInstaller odpalony w Wine.
#
#   ./scripts/build_windows.sh            # oba pliki: DeskLink.exe (konsola) + GUI
#   ./scripts/build_windows.sh cli        # tylko wersja z konsola (relay/host/id)
#   ./scripts/build_windows.sh gui        # tylko wersja bez okna konsoli
#
# Potrzebujesz: wine + Windows Python w IZOLOWANYM prefiksie (.wine-desklink),
# ktory stawia scripts/setup_wine_windows.sh. Nic nie dotyka ~/.wine.
#
# Efekt:
#   dist/cli/DeskLink.exe   - dziala z cmd: DeskLink.exe relay --port 7001
#   dist/gui/DeskLink.exe   - podwojne klikniecie = okno, zero czarnego okienka
#
# Uwaga o Wine: PyInstaller buduje aplikacje Windows, ale NIE musi byc odpalany
# na Windowsie - 1:1 z tym, co dostalbyes na maszynie wirtualnej. Zrzut 'exe'
# przenosisz na Windows 11 calym katalogiem (onedir) albo jednym plikiem (onefile).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${DESKLINK_WINEPREFIX:-$HERE/.wine-desklink}"
PY_WIN='C:\\Python311\\python.exe'
TARGET="${1:-both}"
export WINEPREFIX="$PREFIX"
export WINEDEBUG="${WINEDEBUG:--all}"
export DISPLAY="${DISPLAY:-:0}"

log() { echo "[build-win] $*"; }

if ! command -v wine >/dev/null 2>&1; then
  echo "[build-win] brak wine w PATH" >&2
  exit 1
fi
if [ ! -x "$PREFIX/drive_c/Python311/python.exe" ]; then
  log "prefiks bez Pythona - odpalam setup_wine_windows.sh"
  "$HERE/scripts/setup_wine_windows.sh" "$PREFIX" || exit 1
fi

# Wine widzi katalog projektu jako C:\desklink (dowiazanie, nie kopia - PyInstaller
# i tak zapisuje wszystko w build/ i dist/, wiec repo nie ucierpi).
ln -sfn "$HERE" "$PREFIX/drive_c/desklink"

wipy() { ( cd "$HERE" && WINEPREFIX="$PREFIX" wine "$PY_WIN" "$@" ); }

log "sprawdzam PyInstalera w prefiksie ..."
if ! wipy -c "import PyInstaller" >/dev/null 2>&1; then
  log "instaluje pyinstaller ..."
  wipy -m pip install --quiet pyinstaller || { echo "[build-win] pip padl" >&2; exit 1; }
fi
wipy -c "import PyInstaller, PIL, mss, cryptography, pyautogui; print('  deps OK', PyInstaller.__version__)"

build_one() {
  local mode="$1" out="$2"
  local winopt=()
  [ "$mode" = "gui" ] && winopt=(--windowed --noconsole)
  log "PyInstaller ($mode) -> $out"
  rm -rf "$HERE/$out"
  ( cd "$HERE" && WINEPREFIX="$PREFIX" wine "$PY_WIN" -m PyInstaller \
      --noconfirm --clean --onefile \
      --name DeskLink \
      --distpath "$out" \
      --workpath "build/pyi-$mode" \
      --specpath "build/pyi-$mode" \
      --paths . \
      --hidden-import tkinter \
      --collect-submodules rd \
      --collect-all PIL \
      --collect-all pyautogui \
      --collect-all mss \
      --add-data "$WINREPO\\server;server" \
      "${winopt[@]}" \
      desklink.py ) 2>&1 | tail -12
  if [ -f "$HERE/$out/DeskLink.exe" ]; then
    log "OK: $out/DeskLink.exe ($(du -h "$HERE/$out/DeskLink.exe" | cut -f1))"
  else
    echo "[build-win] PADL: brak $out/DeskLink.exe (patrz build/pyi-$mode)" >&2
    return 1
  fi
}

# --add-data jest resolwowane wzgledem --specpath, nie cwd, wiec podajemy sciezke
# bezwzgledna w formie Windowsowej (Z:\... albo C:\desklink), a nie '/home/...'.
WINREPO="$(winepath -w "$HERE" 2>/dev/null || true)"
[ -n "$WINREPO" ] || WINREPO='C:\desklink'
log "sciezka repo dla PyInstalera: $WINREPO"

rc=0
case "$TARGET" in
  cli) build_one cli dist/cli || rc=1 ;;
  gui) build_one gui dist/gui || rc=1 ;;
  both) build_one cli dist/cli || rc=1; build_one gui dist/gui || rc=1 ;;
  *) echo "uzycie: $0 [cli|gui|both]" >&2; exit 2 ;;
esac
if [ "$rc" -ne 0 ]; then
  echo "[build-win] budowa nie udana - patrz build/pyi-*" >&2
  exit "$rc"
fi


log "smoke test: DeskLink.exe id"
if [ -f "$HERE/dist/cli/DeskLink.exe" ]; then
  ( cd "$HERE" && WINEPREFIX="$PREFIX" timeout 90 wine "dist/cli/DeskLink.exe" id ) || \
    log "UWAGA: 'DeskLink.exe id' wrocilo z bledem"
fi

# 'relay' to najczestszy powod, dla ktorego ktas kopiuje DeskLink.exe na serwer -
# a dziala tylko wtedy, gdy katalog server/ wszedl do archiwum (--add-data).
log "smoke test: DeskLink.exe relay"
if [ -f "$HERE/dist/cli/DeskLink.exe" ]; then
  RPORT=$(( 8000 + RANDOM % 1500 ))
  out="$( cd "$HERE" && WINEPREFIX="$PREFIX" timeout 25 wine "dist/cli/DeskLink.exe" \
            relay --bind 127.0.0.1 --port "$RPORT" 2>&1 )"
  echo "$out" | head -3 | sed 's/^/[build-win]   /'
  if echo "$out" | grep -q "slucha na"; then
    log "OK: relay z .exe wystartowal"
  else
    echo "[build-win] PADL: 'DeskLink.exe relay' nie wstal - server/desklink_relay.py nie jest w archiwum" >&2
    exit 1
  fi
fi

log "gotowe. Pliki:"
ls -la "$HERE/dist" 2>/dev/null | sed 's/^/[build-win]   /'
