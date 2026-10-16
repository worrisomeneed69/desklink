"""Warstwa GUI DeskLink (Tkinter).

Podzial:
  theme.py        paleta, typografia, styl ttk
  widgets.py      widgety rysowane na Canvasie (przenosne miedzy Windows/Wine/Linux)
  render.py       obraz z sesji -> PhotoImage z awaryjna sciezka bez PIL.ImageTk
  session_view.py okno zdalnego pulpitu i okno udostepniania
  app.py          okno glowne, dialogi, petla zdarzen

Modul celowo NIE importuje tkintera na poziomie pakietu - dzieki temu
`python -m rd --help` dziala nawet tam, gdzie Tk nie jest zainstalowany.
"""

__all__ = ["theme", "widgets", "render", "session_view", "app"]
