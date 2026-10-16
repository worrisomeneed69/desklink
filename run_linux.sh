#!/usr/bin/env bash
# DeskLink - start pod Linuksem (i pod Wine, jesli wywolasz przez wine).
#
#   ./run_linux.sh                 # GUI
#   ./run_linux.sh id              # moje ID + odcisk klucza
#   ./run_linux.sh relay --port 7001   # wlasny serwer rendezvous
#
# Skrypt dolozyla tylko to, czego nie da sie zrobic jednym 'python -m rd':
# sprawdza Pythona, dokleja katalog projektu do PYTHONPATH (nie trzeba instalowac
# pakietu) i w razie braku Pillow instaluje brakuja zaleznosci z requirements.txt.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-}"

if [ -z "$PY" ]; then
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo "[desklink] nie znalazlem pythona - zainstaluj Python 3.9+ i wystartuj ponownie" >&2
  exit 1
fi

if ! "$PY" -c "import tkinter, PIL" >/dev/null 2>&1; then
  echo "[desklink] brakuje tkinter/Pillow - probam doczytac z requirements.txt ..."
  "$PY" -m pip install --user -r "$HERE/requirements.txt" || {
    echo "[desklink] pip nie dal rady; pod Windowsem: py -m pip install -r requirements.txt" >&2
    echo "[desklink] pod Debianem/Ubuntu:  sudo apt install python3-tk python3-pip" >&2
    exit 1
  }
fi

export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" -m rd "$@"
