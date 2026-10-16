# Serwer relay — instalacja i konfiguracja

Relay to jedyne, co DeskLink wymaga od serwera. Trzyma 9-cyfrowe ID, wie kto jest
online i dokleja do siebie dwa komputery. **Nie jest potrzebny**, gdy oba
komputery siedzą w tej samej sieci (wtedy łączysz się po `ip:port`), ale jest
potrzebny, żeby połączyć się z innym Wi-Fi, za NAT-em i z innym systemem.

Plik: [`server/desklink_relay.py`](../server/desklink_relay.py) — czysta biblioteka
standardowa Pythona 3.9+, zero `pip install`, zero zależności, zero bazy danych.

## 1. Uruchomienie

```bash
python3 server/desklink_relay.py --port 7001
```

Tyle wystarczy. Klient łączy się adresem `serwer:7001` (Ustawienia → Relay w GUI
albo `--relay host:7001` w CLI). **Jeden port TCP** obsługuje i sygnalizację, i tunele:
pierwsza linia JSON od klienta rozstrzyga, czy to `hello` (długie połączenie
presence), czy `join` (rura z bajtami sesji).

Jeśli ten relay ma połączyć **dwie różne sieci** (kolega z innego WiFi, inny system
operacyjny), samo wystawienie serwera nie wystarczy — adres musi być osiągalny z obu
stron. Trzy sprawdzone sposoby i gotowe wartości pól opisuje
[laczenie.md](laczenie.md).

Wersja przez `rd` (to samo, ścieżka resolwowana też w zamrożonym `.exe`):

```bash
python3 -m rd relay --port 7001
DeskLink.exe relay --port 7001
```

Uwaga na nazwy flag: otoczka `rd relay` używa `--bind` / `--port` / `--tls-cert` /
`--tls-key` / `--secret`, a surowy skrypt `server/desklink_relay.py` — `--host`,
`--cert`, `--key`, `--require-auth`. `rd relay --secret X` dopisuje
`--require-auth` za Ciebie.

`DeskLink.exe relay` działa bez żadnego obok katalogu `server/` — plik serwera jest
dopakowany do środka archiwum onefile (`--add-data` w `scripts/build_windows.sh`),
więc na VPS-a wystarczy skopiować jeden plik.

## 2. Parametry i zmienne środowiskowe

| Parametr | Domyślnie | Co robi |
|---|---|---|
| `--host` | `0.0.0.0` | adres nasłuchu |
| `--port` | `7001`, `DESKLINK_RELAY_PORT` | port TCP (sygnalizacja + tunel) |
| `--cert`, `--key` | `DESKLINK_TLS_CERT` / `DESKLINK_TLS_KEY` | TLS dla obu rodzajów połączeń |
| `--require-auth` | wyłączony | odrzuca klientów bez klucza wstępu |
| `--secret` | `DESKLINK_RELAY_SECRET` | klucz, który klienci podają w `hello`/`join` |

Klucz wstępu po stronie klienta: Ustawienia → Relay → „Klucz wstępu", albo
`--relay-key` w CLI. Relay sprawdza go dwukrotnie — raz dla sygnalizacji, raz dla
tunelu — więc bez niego klient zobaczy komunikat `relay key wymagany`.

Limity i czasy (zmienne środowiskowe, bez flag):

| Zmienna | Domyślnie | Znaczenie |
|---|---|---|
| `DESKLINK_MAX_CLIENTS` | `2000` | max połączeń presence; powyżej → `relay pelny` |
| `DESKLINK_MAX_TUNNELS` | `500` | max równoległych sesji przekazywanych przez serwer |
| `DESKLINK_RING_TIMEOUT` | `75` | ile sekund czeka zaproszenie na odpowiedź hosta |

Stałe po stronie klienta: `KEEPALIVE = 22 s` (ping), relay zamyca klienta milczącego
dłużej niż `95 s`. Klient reconnectuje z wykładniczym backoffem 1 s → 30 s i prosi o
swoje stare ID.

## 3. systemd

```ini
# /etc/systemd/system/desklink-relay.service
[Unit]
Description=DeskLink relay (rendezvous + tunnel)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=desklink
Group=desklink
StateDirectory=desklink
RuntimeDirectory=desklink
ExecStart=/usr/bin/python3 /opt/desklink/desklink_relay.py --port 7001
Environment=DESKLINK_MAX_CLIENTS=2000
Environment=DESKLINK_MAX_TUNNELS=500
EnvironmentFile=-/etc/default/desklink-relay
Restart=always
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=read-only
ProtectSystem=strict
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6
RestrictSUIDSGID=yes
MemoryMax=512M

[Install]
WantedBy=multi-user.target
```

```bash
sudo install -d -o desklink -g desklink /opt/desklink
sudo cp server/desklink_relay.py /opt/desklink/
sudo systemctl daemon-reload && sudo systemctl enable --now desklink-relay
sudo systemctl status desklink-relay --no-pager
```

Klucz wstępu trzymaj w `/etc/default/desklink-relay` (`DESKLINK_RELAY_SECRET=...`),
nie w unitach. Plik ma mieć `0600` i być roota.

Firewall: otwórz tylko ten jeden port, np. `sudo ufw allow 7001/tcp`.

### Docker

```bash
docker run -d --name desklink-relay --restart unless-stopped \
  -p 7001:7001 \
  -e DESKLINK_RELAY_SECRET=... -e DESKLINK_MAX_TUNNELS=500 \
  -v $PWD/server:/srv:ro \
  python:3.12-slim python3 /srv/desklink_relay.py --port 7001 --require-auth
```

## 4. TLS

Relay mówi TLS natownie (`--cert`/`--key`, certyfikaty na oba rodzaje połączeń):

```bash
sudo certbot certonly --standalone -d relay.mojadomena.pl
DESKLINK_TLS_CERT=/etc/letsencrypt/live/relay.mojadomena.pl/fullchain.pem \
DESKLINK_TLS_KEY=/etc/letsencrypt/live/relay.mojadomena.pl/privkey.pem \
  python3 server/desklink_relay.py --port 7001 --require-auth --secret ...
```

Klient wtedy w Ustawieniach → Relay zaznacza TLS. Certyfikat self-signed można
zaakceptować jednorazowo (checkbox „nie weryfikuj hosta" jest domyślnie **wyłączony**
— bez niego połączenie z niezaufanym certyfikatem pada). Alternatywa bez TLS w
Pythonie: `stunnel` albo nginx `stream` z certyfikatem, a relay nasłuchuje lokalnie.

Ruch sesji i tak jest szyfrowany end-to-end (AES-256-GCM, patrz
[protokol.md](protokol.md)), więc TLS chroni przede wszystkim metadane: to, że się
łączysz, z jakim ID i z jakiego adresu.

## 5. Co relay widzi, a czego nie

Widzi: adresy IP:port obu stron, 9-cyfrowe ID, alias, nazwę komputera, platformę,
klucz publiczny X25519 i odcisk (to dane jawne z definicji), liczbę bajtów i czasu
życia tunelu. Nie widzi: hasła (nawet dowodu — leci AES-GCM), obrazu ekranu,
klawiszy, zawartości plików ani schowka. Log to jedna linia na zdarzenie:

```
[2026-10-15 21:58:03] + 747748540 (mikolaj-win) z 83.11.22.33:51820
[2026-10-15 21:58:11] tunnel 4f9c1a: 747748540 <-> 452106640
[2026-10-15 21:59:03] status: klientow 2, tuneli 1, zaproszen 0, par lacznie 1
```

`log(...)` pisze na stdout — przy systemd trafia do `journalctl -u desklink-relay`.

## 6. ID przeżywają restart agenta, ale nie restart serwera

Para ID ↔ sekret jest trzymana **w RAM** serwera (`by_secret`), a sam sekret leży
w pliku konfiguracyjnym klienta (`ident.secret`, plik `0600`). **Restart agenta**
jest bezbolesny: klient wraca z `want_id`, serwer rozpoznaje sekret i oddaje to samo
ID, o ile nikt go w międzyczasie nie zajął (`reclaim()` zwraca `None`, gdy ID jest
zajęte albo gdy klient prosi o inny numer niż zapisany pod tym sekretem).

**Restart serwera** kasuje mapę, więc `reclaim()` nie ma czego rozpoznać i klient
dostaje **nowy, wylosowany ID** — stary numer nie jest zwracany tylko dlatego, że jest
wolny. Praktyczne konsekwencje: po restarcie relaya trzeba podać kolege nowy numer albo
połączyć się po aliasie. Trzymanie ID w pliku (mapa na dysku, prawa `0600`) to zmiana
w `server/desklink_relay.py` o kilkanaście linii — jeśli ma to być używane produkcyjnie,
rozważ ten krok albo po prostu `--require-auth` plus alias, który jest wolny od
losowania ID.

## 7. Typowe komunikaty błędów

| Klient widzi | Znaczenie | Co sprawdzić |
|---|---|---|
| `relay key wymagany` | serwer ma `--require-auth`, klient nie podał klucza | Ustawienia → Relay → Klucz wstępu |
| `relay pelny` | przekroczony `DESKLINK_MAX_CLIENTS` albo `MAX_TUNNELS` | podnieść limit, zrestartować serwer |
| `offline` | host nie ma sygnalizacji na tym serwerze | czy host ma ten sam adres relay, czy suwak „Dostępny dla innych" jest włączony |
| `busy` | host już prowadzi sesję (jedna na raz) | rozłączyć tamtą sesję |
| `already-dialing` | ten sam klient dzwoni już gdzie indziej | odczekać ~75 s (RING_TIMEOUT) |
| `timeout` / `join-timeout` | host nie kliknął „Przyjmij" w porę | zgoda ma 75 s; przy haśle ten krok odpada |
| `siebie` | wpisano własne ID | — |
| `relay milczy (timeout)` | serwer padł albo wypiło go NAT | klient reconnectuje sam (backoff 1→30 s) |

## 8. Własny relay zamiast publicznego

Nie ma publicznego relay „od nas" i nie będzie: model jest taki, że serwer stawiasz
u siebie (VPS za kilkanaście złotych, kontener, drugi router, Raspberry Pi). Dzięki
temu nikt trzeci nie zna Twoich ID ani harmonogramu połączeń, a cała sesja jest i tak
szyfrowana E2E, więc nawet przejęty serwer nie dostaje obrazu.
