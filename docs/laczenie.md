# Jak połączyć się z kolegą — inna sieć, inny system

DeskLink **nie ma publicznego serwera** (patrz [relay.md](relay.md)), więc żeby Twój
komputer był widoczny spoza własnej sieci, obie strony muszą wskazać **ten sam relay**.
To jest jedyna rzecz, którą trzeba ustawić raz. Reszta działa tak samo, jak w AnyDesk:
dziewięciocyfrowe ID, hasło, zgoda na ekran, sterowanie myszą i klawiaturą.

Systemy mogą się mieszać dowolnie — Linux ↔ Windows 11 ↔ Wine na Linuksie są testowane
w `tests/qa_wine.py`. Układ okien i mapa klawiszy przeliczają się same.

## 0. Kto jest kim

**Host** — maszyna, której ekran ma być pokazany i którą ktoś obsługuje.
**Kontroler** — maszyna, która dzwoni, patrzy i klika.

Rola nie jest przypisana na sztywno: ten sam egzemplarz programu może pełnić obie
i może łączyć się w obie strony. W danej chwili host może obsłużyć **jedną** sesję
— zajęty odpowiada „busy", dokładnie jak w AnyDesk.

## 1. Ścieżka na skróty

1. Host i kontroler wpisują **ten sam adres relaya** (Ustawienia → Relay → Zapisz) i oboje
   dostają ID.
2. Host ustawia hasło (Ustawienia → Hasło) i zostawia włączony przełącznik
   „Dostępny dla innych".
3. Kontroler wpisuje ID hosta w pole „POŁĄCZ Z KOMPUTEREM", w pole „Hasło" wpisuje hasło,
   klika **Połącz**.
4. Host widzi okno „Połączenie przychodzące" z ID i odciskiem klucza drugiej strony,
   decyduje czy daje sterowanie i klika **Przyjmij**.
5. Obraz leci. Pasek na dole pokazuje czas trwania, fps, RTT w ms, kb/s i jakość.

Poniżej szczegóły, bo krok 1 jest tym, który najczęściej wysypuje się w praktyce.

### Skąd wziąć program na drugi komputer

Obie strony potrzebują tylko jednego pliku — nic się nie instaluje, nie trzeba Pythona
ani Visual C++ Redistributable (build jest onefile). Stały link, bez limitu pobrań i bez
daty ważności:

- **Windows 11** → [DeskLink.exe](https://github.com/worrisomeneed69/desklink-releases/releases/download/v1.0.0/DeskLink.exe)
  (okienkowy host i kontroler w jednym)
- **CLI / własny relay** → [DeskLink-cli.exe](https://github.com/worrisomeneed69/desklink-releases/releases/download/v1.0.0/DeskLink-cli.exe)
- **sumy do sprawdzenia** → [SHA256SUMS](https://github.com/worrisomeneed69/desklink-releases/releases/download/v1.0.0/SHA256SUMS)

Po pobraniu warto zweryfikować plik przed pierwszym klikiem:

```powershell
Get-FileHash .\DeskLink.exe -Algorithm SHA256      # Windows (PowerShell)
```

```bash
sha256sum DeskLink.exe                             # Linux / Wine
```

Ma wyjść `c8aae9a96785930f5b867648ebff93bfb827a37d9df98052cf04995d75becb7e`. Pierwsze
uruchomienie Windows może zapytać o zaufanie do wydawcy i przepustnicę w zaporze —
to normalne przy programie, który nasłuchuje na port. **Na Linuxie nie pobierasz
`.exe`**: bierzesz projekt i odpalasz go z Pythona, czyli w tej samej wersji, którą
uruchomiony jest host Windowsowy:

```bash
git clone https://github.com/worrisomeneed69/desklink.git && cd desklink
pip install -r requirements.txt
python3 -m rd gui
```

## 2. Wspólny relay — trzy sposoby

Adres relaya musi być osiągalny **z obu sieci**. To znaczy: albo sieć wirtualna, albo
publiczny serwer, albo przekierowanie portu na routerze. Kolejność od najprostszej
do najbardziej samodzielnej.

### A. Tailscale — darmowe, bez karty, działa za CGNAT-em

Najmniej roboty i najstabilniejsze, jeśli macie obu klientów pod kontrolą. Tailscale
robi przedzieranie przez NAT i daje każdemu urządzeniu stały adres z puli `100.64.0.0/10`.
Przekierowanie portów na routerze jest wtedy zbędne.

Na Linuksie (Debian/Ubuntu, z roota):

```bash
curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg \
  -o /usr/share/keyrings/tailscale-archive-keyring.gpg
curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list \
  -o /etc/apt/sources.list.d/tailscale.list
apt update && apt install -y tailscale
systemctl enable --now tailscaled
tailscale up --hostname=moj-pc        # otworzy link logowania w przeglądarce
tailscale ip -4                       # -> np. 100.101.102.103
```

Na Windows 11: instalator `.msi` z <https://pkgs.tailscale.com/stable/> (sprawdź sumę
kontrolną plikiem `.sha256` obok), instalacja domyślna, potem z ikony w zasobniku
„Log in" i **to samo konto** co na Linuksie.

Od tej chwili `100.x.y.z` jest osiągalne z każdego urządzenia Waszego tailnetu —
również z kawiarnianego WiFi i z telefonu.

Relay na maszynie, która ma go użyczać, odpalasz jednym z:

```bash
python3 -m rd relay --bind 0.0.0.0 --port 7010        # szybko, na próbę
DeskLink.exe relay --bind 0.0.0.0 --port 7010         # to samo z gotowego .exe
```

albo wystawiasz go jako usługę, żeby wstawała po restarcie komputera
(`/etc/systemd/system/desklink-relay.service`):

```ini
[Unit]
Description=DeskLink relay (rendezvous + tunnel)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mikolaj
WorkingDirectory=/mnt/data/DeskLink
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 -m rd relay --bind 0.0.0.0 --port 7010
Restart=always
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
RestrictAddressFamilies=AF_INET AF_INET6
CapabilityBoundingSet=
MemoryMax=256M

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now desklink-relay
journalctl -u desklink-relay -f        # widać '+ <ID> (nazwa)' gdy klient wchodzi
```

**Warto wiedzieć:** w trybie darmowym Tailscale nie blokuje ruchu między Waszymi
maszynami — DeskLink gada sobie po TCP jak po sieci lokalnej, a sesja i tak jest
szyfrowana E2E. Klucze urządzeń wygasają po 180 dniach; odświeżenie to jeden klik
w konsoli Tailscale, bez przeinstalowania czegokolwiek.

### B. Przekierowanie portu na routerze

Jeśli nie chcesz instalować nic u kolegi i masz **prawdziwy publiczny adres** na WAN:

1. W panelu routera (`192.168.1.1` i podobne) dopisz regułę port forward: TCP `7010`
   → `192.168.8.163` (adres maszyny z relayem, najlepiej statyczny albo z rezerwacją DHCP).
2. Sprawdź, czy adres WAN w statusie routera jest tym samym, co pokazuje
   `curl https://api.ipify.org`. Jeśli router pokazuje `100.64.x.x`, `10.x.x.x`
   albo `172.16-31.x.x`, jesteś za CGNAT-em operatora i przekierowanie **nie zadziała**
   — wróć do wariantu A.
3. W DeskLink obie strony wpisują host `twój.publiczny.adres`, port `7010`.

Firewall na hoście z relayem musi przepuszczać przychodzący TCP 7010
(`firewall-cmd --add-port=7010/tcp --permanent && firewall-cmd --reload`,
albo `ufw allow 7010/tcp`). Po stronie klientów wystarczy ruch **wychodzący**.

### C. Własny serwer / VPS

Dowolny tani VPS z publicznym IP. Jeden plik, czysta biblioteka standardowa, zero `pip install`:

```bash
scp server/desklink_relay.py user@vps:~/
ssh user@vps 'python3 desklink_relay.py --port 7010 --require-auth --secret JAKIS_DLUGI_KLUCZ'
```

`--require-auth` ma sens, bo relay stoi na adresie, który za chwilę znają skanery
w całym internecie. Klienci podają wtedy ten sam klucz w polu „Klucz wstepu relaya".
Jeśli startujesz relay przez `rd relay` albo `DeskLink.exe relay`, samo `--secret`
wystarczy — w tej ścieżce wymaganie klucza włącza się razem z sekretem.
Gotowy unit, TLS i limity opisuje [relay.md](relay.md).

### Który adres wpisać — ściąga

| Sytuacja | Host relay | Port |
|---|---|---|
| Test na jednej maszynie (np. Wine + Linux) | `127.0.0.1` | `7010` |
| Ten sam WiFi/LAN | adres LAN maszyny z relayem, np. `192.168.8.163` | `7010` |
| Inna sieć, tailnet | adres Tailscale maszyny z relayem, `100.x.y.z` | `7010` |
| Inna sieć, przekierowany router | publiczny IP WAN | `7010` |
| VPS | domena albo IP serwera | `7010` |

## 3. Host: co ustawić w oknie

Otwórz **Ustawienia → Relay** i wypełnij. Uwaga: GUI podpisuje część pól bez ogonków
(„Lacz sie", „Klucz wstepu") — szukaj dokładnie tych napisów.

- **Host relay** — adres z tabeli wyżej. To ma być **adres lub domena**, nie numer portu;
  wpis `7002` w tym polu wygląda niewinnie i kończy się wiecznym „offline".
- **Port** — `7010` (albo `7001`, jeśli nic go nie zajmuje).
- **TLS do relaya (własny certyfikat)** — tylko gdy relay wystartował z `--cert/--key`.
- **Akceptuj self-signed certyfikat relaya** — tylko dla własnej infrastruktury z certyfikatem
  samopodpisanym.
- **Klucz wstepu relaya** — tylko gdy relay działa z `--require-auth --secret ...`.
- **Odkrywanie w sieci lokalnej (UDP)** — zostaw włączone, pomaga w tej samej podsieci.
- **Lacz sie z relayem przy starcie** — **włącz**. Wyłączone sprawia, że poprawny adres
  jest zapisywany, ale aplikacja nigdy nie próbuje się połączyć.

Kliknij **Zapisz**. W lewej kolumnie („TWÓJ ADRES") pojawi się dziewięciocyfrowe ID,
a chip na górze zmieni się na zielone „relay online". Wyślij ID koledze.

Następnie **Ustawienia → Hasło**: podaj hasło i potwierdź. Bez hasła może się do Ciebie
połączyć każdy, kto zna Twoje ID — i tak zobaczy okno zgody, ale hasło to druga,
niezależna zapora. Na dole okna jest jeszcze „Dostępny dla innych" — twardy wyłącznik:
przy wyłączonym nikt nie nawiąże sesji, choć relay będzie pokazywał „online".

## 4. Kolega: jak dzwonić

W polu „POŁĄCZ Z KOMPUTEREM" wpisuje ID (`123 456 789` — spacje i myślniki są obojętne),
w „Hasło" hasło hosta, i klika **Połącz**. Odhaczając „Tylko podgląd (bez sterowania)"
prosi od razu wyłącznie o obraz.

Zamiast ID można wpisać adres bezpośredni `192.168.8.163:7000` — to omija relay i działa,
gdy obie maszyny są w jednej sieci (albo w jednym tailnecie). Wtedy host nie potrzebuje
nawet ID, tylko włączony przełącznik przy „Nasłuchuj na porcie".

## 5. Zgoda i odcisk klucza

Na hoście wyskakuje okno „Połączenie przychodzące": nazwa maszyny, `ID`, odcisk klucza
drugiej strony i przełącznik zgody na sterowanie. **Przyjmij** otwiera sesję,
**Odrzuć** rozłącza. Przy pierwszym kontakcie DeskLink zapamiętuje odcisk (TOFU) —
przy kolejnej zmianie klucza zobaczysz „Ostrzeżenie o kluczu" i warto wtedy zadzwonić
do kolegi i porównać odcisk innym kanałem.

Hasło weryfikuje się bez jego przesyłania (dowód HMAC nad wyzwaniem, a w nim klucz
PBKDF2-SHA256 240 000 rund), a treść sesji idzie przez relaya zaszyfrowana AES-GCM
z kluczem X25519 — operator relaya widzi metadane i strumień zaszyfrowanych bajtów,
nie ekran.

## 6. W sesji

Kontroler ma na pasku: **Sterowanie** (oddaje/zabiera mysz i klawiaturę), **Schowek**
(przekazuje zawartość), **Plik...** (wysyła plik na drugą stronę), **Monitor**
(przełącza ekrany przy wielu monitorach), **Jakosc** (presety: „Szybko (niska)" 18 fps/40,
„Zrownowazona" 24/65, „Wysoka" 30/85, „Bardzo wysoka" 40/95 oraz „Dopasuj / wypelnij"),
**Pelny ekran** i **Rozlacz**. Host w swoim oknie widzi „TWOJ EKRAN JEST UDOSTEPNIANY",
kto i od kiedy, przycisk **Odbierz plik...**, wybór monitora i czerwone **ROZLACZ**,
które zrywa sesję natychmiast — to jest awaryjny wyłącznik, zawsze działa.

Suwaki na dokładne ustawienia strumienia są w oknie hosta podczas sesji: **fps** (3–60)
i **jakosc** (20–95), obok przełączników „Udostepniaj obraz ekranu", „Zezwal na sterowanie
mysza i klawiatura" i „Zablokuj te maszynie wejście lokalne". Stałe domyślne dla wszystkich
połączeń ustawia się w **Ustawienia → Jakosc obrazu** (fps, JPEG, skala obrazu 0.30–1.00,
format JPEG/PNG, adaptacyjna jakość) — to host nadaje ekran, więc jego parametry są
podstawą. Presety z paska kontrolera nadpisują je na czas sesji.

## 7. Zanim zadzwonisz — test w dwie minuty na jednej maszynie

Wszystko da się przećwiczyć bez drugiego komputera, bo Wine udaje osobnego Windowsa:

```bash
python3 -m rd relay --bind 0.0.0.0 --port 7010 &          # terminal 1
WINEPREFIX=$PWD/.wine-desklink wine 'C:\desklink\dist\gui\DeskLink.exe' gui &   # terminal 2 — "Windows"
python3 -m rd gui                                          # terminal 3 — Linux jako kontroler
```

Oba okna wskazują na `127.0.0.1:7010`, każde dostaje inne ID, i dzwonisz z jednego
do drugiego. Jak to zadziała, to znaczy, że konfiguracja jest dobra — zostaje tylko
zamienić `127.0.0.1` na adres osiągalny z sieci kolegi.

## 8. Nie działa — od czego zacząć

| Objaw | Przyczyna i naprawa |
|---|---|
| ID to `0`, chip „relay: brak relay" | Pusty adres relaya. Ustawienia → Relay → host + port, „Lacz sie z relayem przy starcie" włączone, Zapisz. |
| Chip „relay: 127.0.0.1" i „offline" | Adres zapisany, ale połączenia nie ma. Sprawdź, czy relay naprawdę nasłucha: `ss -ltn \| grep 7010`. W starszym `.exe` (sprzed poprawki) zmiana adresu w oknie nie restartowała połączenia — zamknij i uruchom DeskLink ponownie. |
| „Host relay" przyjmuje `7002` i nic nie działa | W tym polu ma być adres, nie port. |
| `bind: Adres już w używaniu` przy starcie relaya | Port zajęty — zwykle przez nasłuch bezpośredni DeskLink („Nasłuchuj na porcie", domyślnie 7000). Przenieś relay na inny port albo zmień ten nasłuch. |
| Dzwoni, ale „brak odpowiedzi" / timeout | Kontroler nie ma tego samego relaya, albo relay nie jest osiągalny z jego sieci (CGNAT, firewall, brak przekierowania). |
| „Odmowa: busy" | Host już prowadzi sesję — jedna na raz. |
| „Odmowa: offline" | Host nie jest zarejestrowany w relayu (patrz wyżej) albo wyłączył „Dostępny dla innych". |
| Hasło odrzucane mimo poprawnego | Host zmienił hasło po ustawieniu Waszego ID — niech sprawdzi Ustawienia → Hasło. |
| „Ostrzeżenie o kluczu" | Druga strona ma nowy klucz (reinstalka, inny profil, atak MITM). Porównajcie odcisk telefonem przed przyjęciem. |
| ID zmieniło się po restarcie relaya | Mapa ID↔sekret żyje w pamięci relaya. Po restarcie numer może być nowy — to samo urządzenie dostanie go przy rejestracji. |
| Czarny/brak obrazu na Waylandzie | `mss` nie zgarnia ekranu znad czystego Waylanda: użyj sesji X11/XWayland albo Windowsa. |
| Obraz tnie przy dużym ekranie | Pasek **Jakosc** kontrolera: „Szybko (niska)". Po stronie hosta **Ustawienia → Jakosc obrazu**: mniej fps, niższy JPEG, skala 0.5. Przez relay liczy się przepustowość łącza serwera. |

## 9. Bezpieczeństwo w skrócie

- **Hasło hosta** — weryfikator PBKDF2-SHA256, 240 000 rund, jawne hasło nie trafia
  na dysk ani na sieć.
- **E2E AES-GCM** — klucz z X25519 przy każdym połączeniu; sekwencja ramek jest
  jednoznacznym nonce, więc powtórka klatek nie przejdzie.
- **TOFU** — pierwszy odcisk zapamiętywany, każda zmiana sygnalizowana.
- **Klucz wstępu relaya** (`--require-auth`) — odcina skanery od rejestracji na Twoim
  publicznym relaya.
- **Zgoda + „Dostępny dla innych" + ROZLACZ** — trzy niezależne sposoby przerwania sesji.
- Relay **nie widzi** ekranu, klawiszy, schowka ani plików; widzi ID, nazwy maszyn,
  odciski, godziny i wolumen bajtów.
