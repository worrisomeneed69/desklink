# DeskLink — zdalny pulpit przez 9-cyfrowe ID

Klon AnyDesk działający na **Windows 11**, pod **Wine** i na **Linuksie**. Jedno okno,
te same funkcje na każdej platformie: podgląd ekranu, sterowanie myszą i klawiaturą,
schowek, pliki i model zgód. Połączysz się z komputerem pracującym pod innym systemem
i w innej sieci — inne Wi-Fi, inny dostawca, NAT po obu stronach.

Strony spotykają się przez **relay z 9-cyfrowym ID** (jak w AnyDesk), który stawiasz
u siebie w pięć sekund, albo **bezpośrednio po adresie IP**, gdy oba komputery są
w tej samej sieci. Ruch między komputerami jest szyfrowany end-to-end — serwer
przepuszcza zaszyfrowane bajty i nie widzi ani ekranu, ani hasła, ani klawiszy.

## Co potrafi

- **Obraz** — 3–60 fps (suwak), JPEG z adaptacyjną jakością 20–95, wiele monitorów
  (przycisk „Monitor"), pełny ekran, zrzucanie klatek przy słabym łączu.
- **Wejście** — ruch i kliknięcia myszy, scroll, klawiatura, skróty (Ctrl+C,
  Alt+Tab, Win), wpisywanie tekstu z polskimi znakami.
- **Schowek** — synchronizacja w dwie strony, przycisk „Schowek" wysyła to, co
  masz skopiowane.
- **Pliki** — „Plik…" wysyła plik na drugą stronę, odbiór do katalogu
  `DeskLink/downloads` (domyślnie wyłączona zgoda, włącz ją w Ustawieniach).
- **Zgody** — hasło dostępu, pytanie przy każdym połączeniu albo tryb bez nadzoru,
  przełączniki „tylko podgląd", blokada lokalnego wejścia, natychmiastowe
  „Rozłącz"/„ROZŁĄCZ" po stronie hosta.
- **Bezpieczeństwo** — X25519 → HKDF → AES-256-GCM na sesję, dowód hasła zamiast
  hasła na drucie (PBKDF2, 240 000 rund), pinowanie odcisku klucza (TOFU).

## 1. Szybki start

Wymagany Python 3.9+ (testowane na 3.11). Zależności są miękkie — bez nich DeskLink
uruchomi się i powie, czego brakuje:

```bash
pip install -r requirements.txt
```

**Linux:**

```bash
./run_linux.sh              # okno
./run_linux.sh id           # pokaż moje ID i odcisk klucza
```

**Windows 11:** podwójne kliknięcie `run_windows.bat` (albo `run_windows.bat id`
w `cmd`). Jeśli wolisz gotowy plik `.exe`, patrz sekcja 5.

### Scenariusz A — dwa komputery w tej samej sieci (bez serwera)

1. Na komputerze, którym chcesz sterować: włącz DeskLink i suwak **Dostępny dla
   innych**. Karta „Połączenie bezpośrednie (ta sama sieć)" pokaże port i adresy IP.
2. Na swoim komputerze: w polu „Połącz z komputerem" wpisz `192.168.1.20:7000`
   (adres i port z punktu 1) i kliknij **Połącz**.
3. Host zapyta o zgodę — kliknij **Zezwól**. Gotowe.

Możesz też kliknąć **Skanuj** w karcie „Znalezione w sieci lokalnej": hosty
nasłuchujące w tej samej podsieci wyskoczą na liście (UDP 51255).

### Scenariusz B — inne Wi-Fi, inny system, NAT (przez ID)

1. Postaw relay tam, gdzie masz publiczny adres (VPS, kontener, drugi router):

   ```bash
   python3 -m rd relay --port 7001
   ```

2. Na **obu** komputerach: Ustawienia → zakładka **Relay** → wpisz adres serwera
   (np. `relay.mojadomena.pl:7001`) i zapisz. Albo bez GUI:
   `./run_linux.sh gui --relay relay.mojadomena.pl:7001`.
3. Na komputerze zdalnym: ustaw **hasło** (Ustawienia → Hasło), jeśli chcesz, żeby
   łączyło się bez pukania w drzwi. Suwak **Dostępny dla innych** musi być włączony.
   Karta „Twój adres" pokaże ID w formacie `452 106 640`.
4. U siebie: wpisz to ID w pole „Połącz z komputerem", podaj hasło, **Połącz**.

ID jest zapamiętywane — po restarcie programu i komputera dostajesz to samo
(nawet gdy relay przydzielił je komuś innemu na chwilę).

## 2. Widok okna

Okno główne: **Twój adres** (ID, odcisk klucza, alias), **Połącz z komputerem**
(ID albo `ip:port`, checkbox „tylko podgląd", hasło), **Połączenie bezpośrednie**,
**Znalezione w sieci lokalnej**, **Ostatnie komputery**, pasek logu na dole.

Podczas sesji kontroler dostaje okno z paskiem narzędzi (Rozłącz, Sterowanie,
Schowek, Plik…, Monitor, Jakość, Pełny ekran) i statystykami na dole (fps, RTT,
kb/s, jakość). Host dostaje panel „Twój ekran jest udostępniany" z trzema
przełącznikami, suwakami fps/jakości i czerwonym **ROZŁĄCZ**.

## 3. Uprawnienia — co realnie decyduje o dostępie

| Przełącznik / ustawienie | Gdzie | Efekt |
|---|---|---|
| Pytaj o zgodę przy każdym połączeniu | Ustawienia → Przychodzące | bez niego i z ustawionym hasłem = tryb bez nadzoru |
| Zezwól na sterowanie myszką i klawiaturą | Ustawienia → Przychodzące | globalny zakaz pilotowania (nawet przy zgodzie) |
| Zezwól na przesył plików | Ustawienia → Przychodzące | domyślnie **wyłączone** |
| Synchronizuj schowek | Ustawienia → Przychodzące | kierunek: obustronny, za zgodą hosta |
| Hasło dostępu | Ustawienia → Hasło | PBKDF2 + dowód; relay nigdy nie widzi hasła |
| Udostępniaj obraz / Zezwól na sterowanie | panel hosta w trakcie sesji | wyłączane w dowolnym momencie, bez rozłączania |
| Zablokuj tej maszynie wejście lokalne | panel hosta (Linux/X11) | osoba przy komputerze nie może ruszać myszy |
| Dostępny dla innych | okno główne | twardy kill switch: brak nowych połączeń |

Hasło nie jest zapisywane na dysku w postaci jawnej — w pliku konfiguracyjnym leży
sól i weryfikator PBKDF2, a plik ma prawa `0600`. Odcisk klucza drugiej strony jest
zapisywany przy **pierwszym** połączeniu (TOFU) i widoczny w karcie „Twój adres" oraz
na liście „Ostatnie komputery". Jeśli przy kolejnym połączeniu odcisk się różni, sesja
jest przerywana, a DeskLink pokazuje okno **„Ostrzeżenie o kluczu"** z przyciskiem
„Zaufaj" — wtedy (i tylko wtedy) trzeba świadomie potwierdzić nowy klucz.

## 4. Własny serwer relay

Relay to jeden plik `server/desklink_relay.py`, **czysta biblioteka standardowa** —
zero `pip install`, zero baz danych, działa na Raspberry Pi i w kontenerze:

```bash
python3 server/desklink_relay.py --port 7001
python3 server/desklink_relay.py --port 7001 --cert fullchain.pem --key privkey.pem
python3 server/desklink_relay.py --port 7001 --require-auth --secret WLASNY_KLUCZ
```

`--require-auth` ma sens, jeśli nie chcesz, żeby pół internetu używało Twojego
relaya: klienci muszą wtedy podać klucz (Ustawienia → Relay → „Klucz wstępu").
Unit systemd, limity, logi i odporność na ataki opisuje [docs/relay.md](docs/relay.md).

### Żeby zadziałało z sieci kolegi

Publicznego relaya nie ma, więc **ID pokaże się dopiero wtedy, gdy obie strony wskażą
relay osiągalny z obu sieci** — bez tego w oknie stoi `--- --- ---`, a `DeskLink.exe id`
wypisuje zero. Trzy sprawdzone drogi (Tailscale bez przekierowań na routerze, port
forward, VPS), dokładne wartości pól w oknie Ustawienia → Relay i tabela „nie działa —
od czego zacząć" są w **[docs/laczenie.md](docs/laczenie.md)**.

## 5. Gotowy plik `.exe` (budowa przez Wine)

Nie potrzebujesz maszyny wirtualnej z Windowsem — PyInstaller odpalony w
**izolowanym** prefiksie Wine robi robotę:

```bash
./scripts/setup_wine_windows.sh      # ściąga Windows Pythona 3.11 i stawia prefiks .wine-desklink
./scripts/build_windows.sh           # -> dist/cli/DeskLink.exe i dist/gui/DeskLink.exe
```

Oba pliki mają ~24 MB i są samodzielne (onefile): wystarczy skopiować je na
Windows 11 i kliknąć. Ten sam `.exe` startuje też pod Wine — `tests/qa_wine.py` w
trybie `DESKLINK_QA_WINE_EXE=1` prowadzi pełną sesję, w której hostem jest właśnie
zamrożony plik, a nie Python z repo.

`dist/gui/DeskLink.exe` to pojedynczy plik do kliknięcia (bez czarnego okienka),
`dist/cli/DeskLink.exe` ten sam program z konsolą — potrzebny do `relay`, `host`
i `id`. Skróty i parametry: `DeskLink.exe connect 123 456 789`.

### Skąd pobrać bez budowania

Zbudowane pliki są na stałym wydaniu GitHub — linki nie wygasają i nie mają limitu
pobrań, więc można je wysłać koledze w komunikatorze:

| Plik | Link | SHA-256 |
|---|---|---|
| GUI | [DeskLink.exe](https://github.com/worrisomeneed69/desklink-releases/releases/download/v1.0.0/DeskLink.exe) | `c8aae9a96785930f5b867648ebff93bf…` |
| CLI + relay | [DeskLink-cli.exe](https://github.com/worrisomeneed69/desklink-releases/releases/download/v1.0.0/DeskLink-cli.exe) | `87d4f5ed610e65e8586fe350a0d798c…` |
| sumy | [SHA256SUMS](https://github.com/worrisomeneed69/desklink-releases/releases/download/v1.0.0/SHA256SUMS) | — |

Wydanie: <https://github.com/worrisomeneed69/desklink-releases/releases/tag/v1.0.0>.
To osobne, publiczne repozytorium z **samymi binarkami** — kod źródłowy zostaje tutaj.
Nowe pliki wrzuca się tam jedną komendą (`gh release upload` / `gh release create`), a
opis połączenia z innej sieci i z innego systemu daje [docs/laczenie.md](docs/laczenie.md).

## 6. Wine bez budowania `.exe`

```bash
WINEPREFIX=$PWD/.wine-desklink wine 'C:\Python311\python.exe' -m rd gui
# albo przez starter Windows, tak jak na prawdziwym Windowsie:
WINEPREFIX=$PWD/.wine-desklink DESKLINK_PY='C:\Python311\python.exe' \
  wine cmd /c run_windows.bat id
```

Wine jest tu traktowane jak osobny Windows: aplikacja sama wykrywa platformę
(`Windows`), bierze ekran przez `mss`, a układ okien drugiej strony jest przeliczany
na współrzędne 0..1. Sesja Linux ↔ Wine jest objęta testem `tests/qa_wine.py`.

## 7. Testy

```bash
python3 tests/test_core.py         # 18 testów: ramki, krypto, mapy klawiszy, konfiguracja
python3 tests/test_e2e.py          #  7 testów: pełna sesja przez relay, hasło, view-only, busy
python3 tests/test_gui_windows.py  #  9 testów: okna i widgety budują się w całości (wymaga DISPLAY)
DISPLAY=:0 python3 tests/qa_gui.py # 23 punkty QA: dwie maszyny, klatki, wejście, schowek + zrzuty
DISPLAY=:0 python3 tests/qa_wine.py#  8 punktów QA: host pod Wine (Windows Python) + kontroler Linux
DISPLAY=:0 DESKLINK_QA_WINE_EXE=1 python3 tests/qa_wine.py  # to samo, ale hostem jest dist/gui/DeskLink.exe
```

Zrzuty ekranu z testów lądują w `qa_out/`. Testy QA nie ruszają Twojej myszki:
injektor hosta zastępowany jest zapisem do pliku (`--dry-inject`).

## 8. Ograniczenia, o których warto wiedzieć

- **Przez relay leci cały ruch sesji** (nie tylko uzgadnianie połączenia), więc
  liczy się przepustowość serwera. Brak automatycznego przekazywania portów
  (UPnP/STUN) — w tej samej sieci użyj połączenia bezpośredniego, poza nią relaya.
- **Jedna sesja na raz** na komputerze — zajęty host odpowiada „busy", tak jak
  AnyDesk.
- **Wayland**: czysty Wayland bez X11 nie da zrzutu ekranu przez `mss`; użyj sesji
  X11/XWayland albo Windowsa.
- **Audio nie jest przesyłane.**
- **Pierwsze spotkanie jest ufnego z założenia (TOFU)** — odcisk klucza zapisuje się
  bez pytania, więc przy pierwszym połączeniu sam sprawdź ID i odcisk drugim kanałem
  (tak samo robi AnyDesk). Dopóki tego nie zrobisz, atak „w środku" przez przejęty
  relay jest teoretycznie możliwy — szczegóły w `docs/protokol.md`.
- Jakość obrazu to JPEG + różnice klatek, nie H.264 — przy 4K i 60 fps potrzebne
  jest porządne łącze; suwak „Jakość" i skalowanie pomagają.

## 9. Co gdzie jest

| Plik | Rola |
|---|---|
| `rd/protocol.py` | ramki binarne, strumień `Pipe`, rodzaje klatek |
| `rd/crypto.py` | tożsamość X25519, E2E, dowody hasła, odciski |
| `rd/net.py` | relay-klient, nasłuch bezpośredni, odkrywanie w LAN |
| `rd/session.py` | maszynownia sesji: zgody, klatki, wejście, schowek, pliki |
| `rd/capture.py` | zrzut ekranu (mss → pyautogui → PIL), detekcja zmian, fps |
| `rd/input_remote.py` | wtrysk myszy/klawiatury po stronie hosta |
| `rd/keys.py` | platformowo neutralne nazwy klawiszy (Windows ↔ Linux ↔ Wine) |
| `rd/agent.py` | orkiestrator: ID, dzwonki, kolejka zdarzeń dla GUI |
| `rd/gui/` | Tkinter: motyw, widgety rysowane na canvasie, okna sesji |
| `rd/cli.py` | `gui`, `host`, `connect`, `id`, `relay` |
| `server/desklink_relay.py` | serwer rendezvous/relay (stdlib) |
| `docs/protokol.md` | format ramek, kolejność uzgadniania sesji, model zagrożeń |
| `docs/relay.md` | systemd, TLS, klucz wstępu, limity, komunikaty błędów |
| `docs/laczenie.md` | tutorial: łączenie z inną siecią i innym systemem (Tailscale / port forward / VPS) |
| `scripts/` | `setup_wine_windows.sh` (prefiks Wine) i `build_windows.sh` (`.exe`) |
| `run_linux.sh`, `run_windows.bat` | startery: sprawdzają zależności i odpalają `rd` |
