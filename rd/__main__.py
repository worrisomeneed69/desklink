"""Wejscie `python -m rd` (oraz desklink.exe po spakowaniu PyInstallerem).

Windows: podwojne klikniecie odpala GUI przez `pythonw.exe`, ktory nie ma
konsoli. Dlatego kazdy wyjatek startowy drukujemy do stderr (jesli jest) i
pokazujemy w oknie, a nie gubimy w powietrzu.
"""

from __future__ import annotations

import sys


def _report(exc: BaseException) -> None:
    import traceback

    traceback.print_exc()
    try:
        from .settings import cache_dir

        log = cache_dir() / "startup-error.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("\n=== %s python %s %s ===\n" % (
                __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
                sys.version.split()[0], sys.platform))
            traceback.print_exc(file=fh)
    except Exception:  # noqa: BLE001 - log to tylko udogodnienie
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "DeskLink - blad startu",
            "%s: %s\n\nPython %s / %s\nSzczegoly: %s" % (
                type(exc).__name__, exc, sys.version.split()[0], sys.platform,
                str(_report_last_log())),
        )
        root.destroy()
    except Exception:  # noqa: BLE001 - brak GUI, zostal stderr i log
        pass


def _report_last_log() -> str:
    try:
        from .settings import cache_dir

        return str(cache_dir() / "startup-error.log")
    except Exception:  # noqa: BLE001
        return "stderr"


def main() -> int:
    try:
        from .cli import main as cli_main

        return int(cli_main() or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        _report(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
