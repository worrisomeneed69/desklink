# Protokół DeskLink

Wersja protokołu: **1** (`PROTOCOL_VERSION = 1`, magic `DL`).
Źródła: [`rd/protocol.py`](../rd/protocol.py), [`rd/crypto.py`](../rd/crypto.py),
[`rd/session.py`](../rd/session.py), [`server/desklink_relay.py`](../server/desklink_relay.py).

Trzy warstwy, które da się czytać osobno:

1. **Sygnalizacja** — linie JSON nad TCP, tylko przez relay. Uzgadnia ID, presence i
   tunel. Nic nie wie o treści sesji.
2. **Ramki** — binarny nagłówek 13 B + payload. Ta sama warstwa działa przez relay i
   przez połączenie bezpośrednie `ip:port`.
3. **AEAD** — AES-256-GCM na kierunek, z kluczy z X25519. Poniżej jest opisany cały
   handshake, bo to tu mieszkają decyzje bezpieczeństwa.

## 0. Topologia połączeń

Klient relay trzyma **dwa** gniazda na ten sam port serwera:

| Gniazdo | Pierwsza linia | Życie |
|---|---|---|
| sygnalizacja | `{"t":"hello",...}` | od startu programu do exitu (presence + dzwonki) |
| tunel | `{"t":"join","token":...}` | od zgody hosta do `BYE` |

Dwa gniazda są po to, żeby ID przeżywało kończenie sesji — relay nie musi wiedzieć,
że za chwilę będzie kolejna. Tryb bezpośredni (ta sama sieć) pomija sygnalizację:
jedno TCP na port 7000 i od razu `HELLO_KEX`.

## 1. Sygnalizacja (JSON lines)

Jedna linia = jeden obiekt JSON, `\n` na końcu. Nazwy pól 1:1 z kodem.

| Kierunek | Wiadomość | Kluczowe pola |
|---|---|---|
| klient → relay | `hello` | `ver`, `name`, `pub`, `fp`, `want_id`, `secret`, `alias`, `key`, `platform` |
| relay → klient | `welcome` | `id` (9 cyfr), `secret` (dostaje właściciel, do odzyskania ID), `reclaimed`, `alias` |
| klient → relay | `ping` | `state` (dostępny / zajęty) — co 22 s |
| relay → klient | `pong` | `now` |
| klient → relay | `open` | `peer`, `input` — dzwonek na ID |
| relay → host | `ring` | `peer`, `name`, `fp`, `token` |
| relay → dzwoniący | `ringing` | `peer`, `token` |
| host → relay | `accept` / `decline` | `token`, `reason` |
| relay → obie strony | `join` | `token`, `peer`, `role` (`host` / `controller`) |
| klient → relay | `join` (na gnieździe tunelu) | `token`, `role`, `key` |
| relay → obie strony | `joined`, `tunnel` | `status`: `open` / `closed` |
| relay → dzwoniący | `fail` | `reason`: `offline`, `busy`, `siebie`, `timeout`, `join-timeout`, `already-dialing`, `relay-busy`, `gone`, `declined` |
| klient → relay | `lookup`, `alias`, `bye` | `id`, `alias` |

Czasy serwera: `RING_TIMEOUT` 75 s na odpowiedź, `JOIN_TIMEOUT` 75 s na oba gniazda
tunelu, `ALIVE_TIMEOUT` 95 s na brak `ping`.

## 2. Ramka binarna

```
 0      2  3     4     5           9          13
+------++--+-----+-----+-----------+-----------+-- payload --+
| 'DL'  |ver|type |flags|   seq     |  length   |  (length B) |
+------++--+-----+-----+-----------+-----------+-- ----------+
  magic 1B  1B    1B    u32 BE      u32 BE
```

`struct.Struct(">2sBBBII")`, 13 bajtów, big-endian. `length` ≤ `MAX_PAYLOAD`
(32 MiB) — większa ramka to `ProtocolError` i rozłączenie, więc nadawca nie może
wymusić `MemoryError` na odbiorcy.

| Bit `flags` | Znaczenie |
|---|---|
| `0x01` `FLAG_PLAIN` | bez szyfrowania (tylko handshake, `BYE`, `ERROR`) |
| `0x02` `FLAG_COMPRESSED` | payload po zlibie (poziom 6) |
| `0x04` `FLAG_KEYFRAME` | pełna klatka ekranu |
| `0x08` `FLAG_PARTIAL` | tylko region (dirty rect) |

Kompresja wchodzi tylko tam, gdzie ma sens: payload > 900 B, nie jest `FLAG_PLAIN` i
to nie jest `FRAME` (JPEG jest już skompresowany). `Pipe.send_frame` sprawdza jeszcze,
czy efekt jest mniejszy od oryginału.

**`seq` to nonce.** Licznik per kierunek, `u32`, używany jako 12-bajtowe nonce
AES-GCM (`_nonce(counter)` = big-endian, czyli 8 zerowych bajtów + 4 bajty licznika).
Dzięki temu nonce nigdy się nie powtarza, a przy 30 klatkach/s wyczerpanie licznika
groziłoby rekeyingiem dopiero po ~45 latach. Kierunki mają osobne klucze
(`data_c2s` / `data_s2c`), więc pełny duplex jest bezpieczny.

Zasada, która trzyma liczniki w ryzach: **ramki jawne nie zjadają `seq`**. `BYE` czy
`ERROR` wysłane w środku sesji nie przesuwają licznika AEAD, więc druga strona nie
dostaje „ramka poza kolejnością". Odbiór jest rygorystyczny — `decrypt()` odrzuca
dowolną ramkę, której `seq` nie jest następny, a `AAD = flags || seq`, więc podmiana
flagi albo numeru w locie kasuje AEAD.

## 3. Typy ramek

| `type` | Nazwa | Payload | Kto → kogo |
|---|---|---|---|
| `0x01` | `HELLO_KEX` | JSON: `v`, `role`, `pub`, `fp`, `id`, `name`, `prefs` | kontroler → host (plain) |
| `0x02` | `HELLO_KEX_ACK` | JSON: `v`, `pub`, `fp`, `id`, `name`, `challenge`, `salt`, `rounds`, `need_password`, `screen`, `caps` | host → kontroler (plain) |
| `0x03` | `AUTH_REQ` | JSON: `proof`, `pub`, `prefs` | kontroler → host (klucz `auth`) |
| `0x04` / `0x05` | `AUTH_OK` / `AUTH_NO` | JSON: `caps`, `screen` / `reason`, `retry` | host → kontroler |
| `0x06` | `CAPS` | JSON: preferencje, `input_request` | obie strony |
| `0x07` / `0x08` | `PING` / `PONG` | `ts` (+ `stats` w `PONG`) | kontroler → host co 1 s |
| `0x09` / `0x0A` | `BYE` / `ERROR` | JSON, plain | obie strony |
| `0x10` | `SCREEN_INFO` | JSON: `width`, `height`, `x`, `y`, `monitors`, `platform` | host → kontroler |
| `0x11` | `SCREEN_REQ` | JSON: `want`, `force`, `fps`, `quality`, `scale` | kontroler → host |
| `0x12` | `FRAME` | nagłówek klatki + JPEG | host → kontroler |
| `0x13` | `CURSOR` | JSON: kształt i pozycja kursora — **zarezerwowane**, na razie ani raz nie wysłane | host → kontroler |
| `0x20` | `MOUSE` | JSON: `fx`, `fy` (0..1, 5 miejsc), `act`, `btn`, `n` | kontroler → host |
| `0x21` | `KEY` | JSON: `canon` \| `combo`, `act`, `char` | kontroler → host |
| `0x22` | `TEXT` | JSON: `s` | kontroler → host |
| `0x23` | `SCROLL` | JSON: `dx`, `dy`, `fx`, `fy` | kontroler → host |
| `0x24` | `GRANT` | JSON: `input`, `by` | **tylko host → kontroler** |
| `0x30` / `0x31` | `CLIP_GET` / `CLIP_SET` | JSON: `s` | obie strony |
| `0x40`–`0x42` | `FILE_META` / `FILE_DATA` / `FILE_DONE` | JSON / `>I fid` + 128 KiB / JSON | obie strony |
| `0x50` / `0x51` | `SYSINFO` / `STATUS` | JSON | obie strony |

Nagłówek klatki to `struct.Struct(">HHHHBB")` = `x, y, w, h, fmt, quality` (10 B) i
dopiero za nim idzie strumień JPEG. Przy `FLAG_PARTIAL` `x,y,w,h` opisują tylko
zmieniony region, przy `FLAG_KEYFRAME` całość od `0,0`.

Nieznany `type` jest po prostu ignorowany przez `_dispatch` — dzięki temu klient
nowszej wersji nie rozjeżdża starszego.

## 4. Handshake krok po kroku

```
kontroler                                host
   |  HELLO_KEX   {pub E_k, fp, prefs}     |   plain, timeout 30 s
   |-------------------------------------->|
   |  HELLO_KEX_ACK {pub E_h, challenge,   |
   |                 salt, rounds, screen} |   plain
   |<--------------------------------------|
   |  obie strony: shared = X25519(E, pub drugiej)
   |                transcript = sort([E_k.pub, E_h.pub]) + "|" + challenge(b64)
   |                salt16 = sha256(b"desklink-salt-v1" + transcript)[:16]
   |                master = HKDF-SHA256(shared, "desklink-master-v1", salt16)
   |                auth_c2s | auth_s2c | data_c2s | data_s2c = HKDF(master, ...)
   |
   |  --- podpięcie kluczy auth ---
   |  AUTH_REQ {proof}                     |   AEAD(auth), timeout 60 s
   |-------------------------------------->|
   |  AUTH_OK {caps, screen} | AUTH_NO     |
   |<--------------------------------------|
   |  --- przełączenie na klucze data ---
   |  FRAME / MOUSE / KEY / GRANT / PING ...
```

Czas na decyzję użytkownika po stronie hosta: `PERMISSION_TIMEOUT = 45 s`, po nim
`AUTH_NO {"reason": "timeout"}`.

**Dowód hasła zamiast hasła.** `challenge` to 16 losowych bajtów od hosta, `salt` i
`rounds` pochodzą z jego konfiguracji (PBKDF2-HMAC-SHA256, 240 000 rund):

```python
pw_hash = PBKDF2(haslo, salt, 240000, 32)
proof   = HMAC-SHA256(auth_c2s, b"desklink-auth-v1" + challenge + fp_kontrolera + pw_hash)
```

Host liczy to samo ze swojego **weryfikatora** (`password_hash` w pliku konfiguracyjnym,
hex z PBKDF2) i porównuje przez `hmac.compare_digest`. Na drucie nie ma ani hasła, ani
jego hashu w jawnej postaci — jest tylko HMAC pod kluczem `auth`, który z kolei jest
pod kluczem z X25519. Bez hasła kontroler podstawia 32 zera jako `pw_hash`; host
wtedy ma `remote_pw_ok = False` i o dopuszczeniu decyduje wyłącznie zgoda użytkownika.

**Transcript w HKDF** jest po to, żeby atakujący w środku nie podmienił klucza
publicznego w locie: klucze są związane z konkretną parą kluczy efemerycznych i
konkretnym `challenge`.

**TOFU (trust-on-first-use).** Kontroler po `HELLO_KEX_ACK` patrzy na `fp` hosta i
`is_trusted(id, fp)`: brak zapisanego odcisku = akceptacja i zapis, inny odcisk niż
zapisany = `ERROR`, przerwanie sesji i okno „Ostrzeżenie o kluczu". Klucz długoterminowy
tożsamości służy wyłącznie do odcisku i pinowania — same klucze sesji są efemeryczne
(osobne X25519 na sesję), więc nagranie ruchu dziś nie odsłoni sesji wczorajszych.

## 5. Współrzędne i klawisze między systemami

Host zgłasza `SCREEN_INFO` z `width`, `height`, offsetem `x`,`y` i listą monitorów.
Kontroler wysyła **ułamek** pozycji (`fx`, `fy` w 0..1, zaokrąglone do 5 miejsc), a
host przelicza go na swoją rozdzielczość. Dlatego sterowanie działa Windows ↔ Linux
↔ Wine, na różnych rozdzielczościach i wielu monitorach — bez znajomości ekranu
drugiej strony po swojej stronie. Klawisze jadą jako nazwy kanoniczne (`rd/keys.py`:
`ctrl`, `super`, `pageup`, `num3`, `ł`) albo jako `combo` (`ctrl+c`) i `TEXT` dla
tekstu dosłownego — mapowanie na keysymy/events Windowsa robi injektor u hosta.

`GRANT` ma semantykę jednokierunkową: tylko host decyduje, że kontroler może sterować
(`Session._can_input()` blokuje wysyłkę wejścia bez grantu). Wyłączenie suwaka
„Udostępniaj obraz" albo „Zezwól na sterowanie" w panelu hosta przesyła `GRANT`/`CAPS`
natychmiast, bez zrywania połączenia.

## 6. Co jest, a czego nie jest chronione

| Napastnik / scenariusz | Co dostaje | Dlaczego to za mało |
|---|---|---|
| podsłuch WiFi / routera / przejęty relay | `DL`, wersję, typy ramek, długości, `seq`, IP:port, ID i aliasy, liczniki bajtów | treść ramek to AES-256-GCM z kluczem sesji; rozmiary klatek mówią tylko „ekran się zmienia" |
| relay, który kłamie (odmowa `join`, `busy`, zerwanie tunelu) | możliwość zablokowania albo zerwania połączenia (DoS) | nie może odszyfrować ani wstrzyknąć ramki — `seq` i AAD to odrzucą |
| replay nagranych ramek | nic | `decrypt()` wymaga dokładnie następnego `seq`, dawniej wysłane ramki padają |
| zgadywanie hasła przez sieć | 1 próba na sesję, przy tym host zwykle pyta użytkownika | dowód nie ujawnia hasła; brak „oracle" do offline łamania |
| kradzież pliku `config.json` | klucz prywatny tożsamości, weryfikator PBKDF2 hasła, sekret ID | hasło nie jest jawne, ale offline łamanie słabego hasła jest realne — plik ma `0600`, a hasło musi być długie; atakujący może się przedstawić hostom, które zapamiętały Twój odcisk |
| brak pakietu `cryptography` | **brak E2E** — `keys.plain = True`, ramki lecą jawnie | to jedyny przypadek, w którym protokół schodzi do plaintextu; status sesji pokazuje `encrypted: false`, a `requirements.txt` instaluje `cryptography` |

Świadome braki na dziś: brak odgórnej listy rate-limitów po stronie hosta (zgadywanie
hasła powstrzymuje raczej monit zgody i `busy` niż blokada konta), brak przekazywania
portów (UPnP/STUN) — bez relay trzeba trafić w otwarty port, brak sygnatury ramki
podpisem tożsamości długoterminowej (ufa się odciskowi zapisanemu przy pierwszym
spotkaniu), brak audio i kolejki plików z wznawianiem po zerwaniu.

## 7. Jak to przeczytać w kodzie

`Pipe.recv()` / `Pipe.send_frame()` — całe ramkowanie i AEAD.
`Session._handshake_controller()` / `_handshake_host()` — kolejność powyżej, 1:1.
`Session._dispatch()` — co znaczy każdy `type` po stronie odbiorcy.
`Relay.dispatch()` w serwerze — cała sygnalizacja.
