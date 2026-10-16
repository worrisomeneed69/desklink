"""Punkt wejscia dla PyInstallera: jeden plik .exe zamiast 'python -m rd'.

PyInstaller nie umie zbudowac aplikacji z '-m rd', wiec potrzebujemy skryptu
proxy. Cal logika zostaje w rd/cli.py - tu tylko wywolanie main().
"""

from __future__ import annotations

from rd.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
