# Shipard Mail Router — Fáze 1 (MVP)

**Status:** Draft k odsouhlasení
**Cíl fáze:** Minimální funkční mail-router daemon který:
1. Přijímá e-maily z Postfixu přes LMTP
2. Validuje příjemce proti lookup tabulce (SMTP policy server)
3. Parsuje MIME, ukládá `.eml` a přílohy
4. Volá `POST /_mail/incoming` na shpd
5. Retry + dead-letter pro nespolehlivé doručení
6. E-mail alert při problémech

**Cílové prostředí:** LXC kontejner s Ubuntu LTS (24.04+), Python 3.11+, Postfix.

**Návaznost:**
- Spotřebovává API endpoint z Fáze 2a na straně `shpd` (viz `shpd:tasks/mail-phase2a.md`)
- API kontrakt je definován v `shpd:docs/mail/api-contract.md`

---

## 1. Scope

**V rozsahu:**

- Python 3.11+ projekt (pyproject.toml, virtualenv install)
- Tři procesy: **LMTP receiver**, **policy server**, **worker** (vše v jednom repo, samostatné systemd services)
- SQLite persistent queue s retry + dead-letter
- Postfix konfigurace (příklady v `deploy/`)
- systemd units v `deploy/`
- Install script pro čistý LXC
- Adresová logika: parsing `<ds-id>[{+|--}mailbox]@domain`
- Strukturované logy do journaldu
- E-mail alert při dead-letter eventech
- Lokální test harness (fake shpd server) pro offline vývoj

**Mimo rozsah:**

- IMAP poll mode (odloženo)
- Web UI / admin panel (operace přes systemd + SQLite CLI)
- Prometheus / metrics endpoint (odloženo)
- Antivir (provádí Postfix/amavisd před doručením, router předpokládá clean mail)
- Outbound mail (odesílání ze shpd) — samostatná fáze
- HA / multi-node deployment
- DEB balíček (MVP je `git pull && ./install.sh`)

---

## 2. Architektura

```
                    Internet
                       ↓
               ┌───────────────┐
               │   Postfix     │  :25 (SMTP)
               │   (smtpd)     │
               └───────┬───────┘
                       │ smtpd_recipient_restrictions
                       │ check_policy_service
                       ↓
               ┌───────────────┐   unix:/var/run/shipard-mail-router/
               │ policy server │   policy.sock  (TCP-style text proto)
               │  (process 1)  │
               └───────┬───────┘
                       │ lookup.json
                       ↓ OK / REJECT
               ┌───────────────┐
               │   Postfix     │ virtual_transport = lmtp:unix:/var/run/…/lmtp.sock
               └───────┬───────┘
                       │ LMTP
                       ↓
               ┌───────────────┐
               │ LMTP receiver │ (process 2)
               │               │ durable write → queue.db
               └───────┬───────┘
                       │ 2xx ACK only after COMMIT
                       ↓
              ┌─────────────────┐
              │   queue.db      │  SQLite
              │ (pending|       │
              │  in_flight|     │
              │  delivered|     │
              │  dead_letter)   │
              └────────┬────────┘
                       │ poll every 5s
                       ↓
               ┌───────────────┐
               │    worker     │ (process 3)
               │               │ httpx POST /_mail/incoming
               └───────┬───────┘
                       ↓
                  shpd API
```

Tři procesy, jedna codebase, tři systemd units. Společná SQLite queue jako nárazník.

**Proč tři procesy, ne jeden monolit:**
- Policy server běží pod tvrdým timeoutem (Postfix čeká max. ~5 s na verdict), musí být lehký
- LMTP receiver musí ACK co nejrychleji (Postfix čeká na LMTP 2xx)
- Worker dělá pomalé věci (HTTP, retry, backoff) — nesmí blokovat příjem
- Při restartu workeru nebo crash se příjem nezastaví

---

## 3. Komponenty

### 3.1 `mail_router/policy.py` — policy server

**Protokol:** Postfix policy delegation protocol (https://www.postfix.org/SMTPD_POLICY_README.html).
Textový protokol přes Unix socket, dotaz je multi-line key=value, odpověď `action=OK|REJECT ...`.

**Logika:**

```
1. Read request from socket
2. Parse `recipient` attribute (`foo@shipard.email`)
3. Split local_part @ domain
4. Verify domain in allowed_domains list
5. Parse local_part per §4 — extract ds_id, mailbox_id
6. Lookup ds_id in lookup.json
   match → return "action=OK"
   no match → return "action=REJECT 550 Unknown recipient"
7. Repeat for next request
```

**Implementace:** `asyncio` server, každé spojení je persistent (Postfix recykluje). Jednoduchý text parser — nic složitého.

### 3.2 `mail_router/receiver.py` — LMTP receiver

**Knihovna:** `aiosmtpd` v LMTP režimu.

**Logika:**

```
RCPT TO handler:
  - Stejná validace jako policy server (defense in depth)
  - ACK

DATA handler:
  - Read raw message (bytes)
  - Extract From: and To: for quick indexing
  - Extract Message-ID for idempotency
  - BEGIN SQLite tx
    - INSERT INTO queue (state='pending', sender, recipient, raw_eml, idempotency_key, ...)
  - COMMIT
  - Return 250 OK (Postfix smí smazat ze své queue)

Neúspěch DB commit → 451 4.3.0 Temporary failure, Postfix retry
```

**Durability guarantee:** SQLite je konfigurováno `journal_mode=WAL` a `synchronous=NORMAL`. Commit dává f-sync před návratem 250. Při hard crash po 250 ale před fsync můžeme ztratit max. posledních pár ms, což je akceptovatelné pro e-mail (Postfix by stejně retryoval).

**Idempotency key generation (client-side):**

```python
key = sha256(f"{domain}/{local_part}/{message_id}".encode()).hexdigest()
```

Pokud mail nemá Message-ID hlavičku (nemělo by se stát, ale stává se), vygenerujeme UUID4 a idempotency se neaplikuje.

### 3.3 `mail_router/worker.py` — worker

**Logika:**

```
loop:
  sleep 5s
  SELECT * FROM queue
    WHERE state = 'pending'
      AND (next_attempt_at IS NULL OR next_attempt_at <= now())
    LIMIT 10
  
  for item in items:
    UPDATE queue SET state='in_flight' WHERE id=?
    try:
      parsed = parse_eml(item.raw_eml)
      response = post_to_shpd(item, parsed)
      if response.status == 201:
        UPDATE queue SET state='delivered', delivered_at=now(), delivered_message_id=?
      elif 400 <= response.status < 500:
        # No retry — bad request
        UPDATE queue SET state='dead_letter', dead_letter_reason=?
      else:
        raise RetryableError(response)
    except RetryableError as e:
      attempt_count += 1
      if attempt_count >= MAX_ATTEMPTS:
        UPDATE queue SET state='dead_letter', dead_letter_reason=?
      else:
        next = now() + backoff(attempt_count)
        UPDATE queue SET state='pending', attempt_count=?, next_attempt_at=?
```

**Backoff:**
- Attempt 1: immediate
- Attempt 2: +1 min
- Attempt 3: +5 min
- Attempt 4: +30 min
- Attempt 5: dead-letter

MAX_ATTEMPTS = 4 (přechody na dead-letter po čtyřech neúspěších).

**Concurrency:** 1 worker proces, zpracovává sériově. Pro MVP naprosto postačí (propustnost 100+ mailů/min i při 500 ms per call).

**MIME parsing:** Python `email.parser.BytesParser`. Extrakce:
- Headers (`Subject`, `From`, `To`, `Date`, `In-Reply-To`, `References`, `Message-ID`)
- Body: `text/plain` part → `body_plain`; `text/html` → `body_html`
- Attachments: všechny non-text parts (neinlined image/* zahrneme též, inline image/* s Content-ID ne)
- Parsing robustní — nevalidní MIME jde do dead-letter s explicit message

### 3.4 `mail_router/client.py` — shpd HTTP client

`httpx.AsyncClient` s timeout 30 s. Konstruuje `multipart/form-data` request z parsed emailu. Hlavičky:

```
Authorization: Bearer <token z lookup[ds_id].api_token>
X-Idempotency-Key: <idempotency_key z queue>
```

Vrátí status + body pro worker rozhodování.

### 3.5 `mail_router/queue.py` — SQLite queue abstraction

Tenká vrstva nad `sqlite3`. Operace:
- `enqueue(sender, recipient, raw_eml, idempotency_key) -> id`
- `dequeue_batch(limit) -> list[QueueItem]` (atomicky mění `pending → in_flight`)
- `mark_delivered(id, message_id)`
- `mark_failed(id, error, retry: bool)` (retry → pending + next_attempt_at, no retry → dead_letter)
- `dead_letter_stats() -> dict` (pro alerting)
- `prune_delivered(days) -> int` (cleanup starých delivered záznamů)

### 3.6 `mail_router/lookup.py` — DS resolution

**Lokální lookup z `/etc/shipard-mail-router/lookup.json`:**

```json
{
  "hosts": ["shipard.email"],
  "data_sources": {
    "4l3j-z0bz-kz39-echj": {
      "api_url": "https://shpd-server-1.example.com/4l3j-z0bz-kz39-echj",
      "api_token": "shpd_ak_XXXXXXXXXXXXXXXXXXXX"
    },
    "firma-xyz": {
      "api_url": "https://shpd-server-1.example.com/4l3j-z0bz-kz39-echj",
      "api_token": "shpd_ak_XXXXXXXXXXXXXXXXXXXX"
    }
  }
}
```

Poznámka: DS může mít víc klíčů (hash-id + web-id) — router nerozlišuje, oba se resolvují na stejnou konfiguraci.

Soubor načítán při startu + watch pro reload (`inotify` nebo prostý stat + mtime check). Později to nahradí volání lookup API na shpd masteru, ale rozhraní `lookup.resolve(email) -> DsConfig | None` zůstane stejné.

### 3.7 `mail_router/alerts.py` — e-mail alerting

Trigger events:
- Mail přešel do dead-letter
- Queue size > threshold (např. 100 pending)
- Worker neodpovídá (heartbeat check)

Odesílá mail přes lokální Postfix (`smtplib`, localhost:25). Throttling: max. 1 alert na 30 min per event type (žádný flood).

Alert recipient v `config.yaml`.

---

## 4. Adresová logika

Viz diskuze ze 17. 4. 2026, bod 1. Implementace v `mail_router/address.py`:

```python
def parse_recipient(email: str) -> ParsedAddress | None:
    """
    Parse <ds_id>[{+|--}mailbox]@<domain>.
    First occurrence of + or -- wins.
    Returns None if domain not allowed or local_part malformed.
    """
    local_part, domain = email.rsplit("@", 1)
    if domain.lower() not in ALLOWED_DOMAINS:
        return None
    
    plus_idx = local_part.find("+")
    dashes_idx = local_part.find("--")
    
    separators = [i for i in (plus_idx, dashes_idx) if i >= 0]
    if not separators:
        return ParsedAddress(ds_id=local_part, mailbox=None)
    
    sep_idx = min(separators)
    sep_len = 1 if sep_idx == plus_idx else 2
    return ParsedAddress(
        ds_id=local_part[:sep_idx],
        mailbox=local_part[sep_idx + sep_len:] or None,
    )
```

Následná validace `ds_id`:
- Hash-id pattern: `^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$`
- Web-id pattern: `^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$` (žádný leading/trailing dash, žádné double-dash)

---

## 5. SQLite queue schema

```sql
CREATE TABLE queue (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    state                TEXT    NOT NULL DEFAULT 'pending'
                              CHECK(state IN ('pending','in_flight','delivered','dead_letter')),
    received_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sender_email         TEXT    NOT NULL,
    recipient_email      TEXT    NOT NULL,
    ds_id                TEXT    NOT NULL,
    mailbox              TEXT,             -- NULL = default
    idempotency_key      TEXT    NOT NULL,
    raw_eml              BLOB    NOT NULL,
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at      TIMESTAMP,
    last_error           TEXT,
    delivered_at         TIMESTAMP,
    delivered_message_id TEXT,              -- shpd response message_id
    dead_letter_reason   TEXT
);

CREATE INDEX idx_state_next_attempt ON queue(state, next_attempt_at);
CREATE UNIQUE INDEX unq_idempotency ON queue(idempotency_key);
CREATE INDEX idx_ds_id ON queue(ds_id);
```

**Retence:**
- `delivered` → `prune_delivered(days=7)` přes cron 1×/den
- `dead_letter` → nikdy se nemaže automaticky, admin mění stav ručně

---

## 6. Konfigurace

### 6.1 `/etc/shipard-mail-router/config.yaml`

```yaml
# Sockets
policy_socket:   /var/run/shipard-mail-router/policy.sock
lmtp_socket:     /var/run/shipard-mail-router/lmtp.sock

# Queue
queue_db:        /var/lib/shipard-mail-router/queue.db

# Worker
worker:
  poll_interval: 5             # seconds
  batch_size:    10
  max_attempts:  4
  http_timeout:  30            # seconds
  backoff:                    # per-attempt wait in seconds
    - 0
    - 60
    - 300
    - 1800

# Lookup
lookup_file:     /etc/shipard-mail-router/lookup.json
lookup_reload:   true          # watch for changes

# Alerting
alerts:
  enabled:       true
  smtp_host:     localhost
  smtp_port:     25
  from_address:  mail-router@shipard.email
  to_addresses:
    - admin@example.com
  throttle:      1800          # seconds between alerts of same type

# Logging
log_level:       INFO
```

### 6.2 `/etc/shipard-mail-router/lookup.json`

Viz §3.6.

---

## 7. Postfix konfigurace (příklad)

Součást repa jako `deploy/postfix/`. Klíčové úpravy:

**`/etc/postfix/main.cf` (přidat/upravit):**

```
# Mail domains handled by shipard
virtual_mailbox_domains = shipard.email

# Route to shipard-mail-router via LMTP
virtual_transport = lmtp:unix:/var/run/shipard-mail-router/lmtp.sock

# Recipient validation via policy server
smtpd_recipient_restrictions =
  permit_mynetworks,
  reject_unauth_destination,
  check_policy_service unix:/var/run/shipard-mail-router/policy.sock

# Limits
message_size_limit = 26214400    # 25 MB

# TLS (Let's Encrypt cert)
smtpd_tls_security_level = may
smtpd_tls_cert_file = /etc/letsencrypt/live/mail.shipard.email/fullchain.pem
smtpd_tls_key_file = /etc/letsencrypt/live/mail.shipard.email/privkey.pem
```

**`/etc/postfix/virtual`** není potřeba — policy server vše rozhoduje.

**Antivir (amavisd-new):** standardní setup, instrukce v `deploy/README.md`. Mail-router předpokládá čisté maily; pokud amavisd přidá `X-Virus-Scanned: Clean` hlavičku, router ji jen loguje, nefiltruje.

---

## 8. systemd units

V `deploy/systemd/`:

- `shipard-mail-router-policy.service` — policy server
- `shipard-mail-router-receiver.service` — LMTP receiver
- `shipard-mail-router-worker.service` — worker

Všechny jako `Type=simple`, pod uživatelem `shipard-mail-router`, restart on failure, log do journaldu.

Target: `shipard-mail-router.target` sdružuje všechny tři pro bulk restart.

---

## 9. Instalace

Script `install.sh` v repu. Na čistém LXC Ubuntu 24.04:

```
1. Vytvoří uživatele shipard-mail-router
2. Adresáře /etc, /var/lib, /var/run s práv
3. Python venv v /opt/shipard-mail-router/venv
4. pip install .
5. Kopíruje example configy do /etc/shipard-mail-router/
6. Instaluje systemd units
7. Instruuje admina: upravit config, naplnit lookup.json, spustit services
```

Vše idempotentní (pro upgrade).

---

## 10. Testing

### 10.1 Unit testy (pytest)

- `tests/unit/test_address.py` — parser, edge cases (prázdný local, unicode, víc separátorů)
- `tests/unit/test_queue.py` — enqueue/dequeue, transakce, retry state transitions
- `tests/unit/test_policy.py` — policy server proto parsing
- `tests/unit/test_client.py` — shpd client, mock HTTP

### 10.2 Integrační testy

- **Fake shpd server** — `tests/fakes/shpd_server.py` — FastAPI nebo aiohttp app který mockuje `/_mail/incoming`. Umí: 201, 422, 500, timeout.
- `tests/integration/test_end_to_end.py` — pošle email přes LMTP socket, ověří:
  1. Happy path: mail → queue → delivered
  2. 422 → dead_letter bez retry
  3. 500 → retry → eventual delivered
  4. Persistent 500 × 4 → dead_letter
  5. Idempotentní retry téhož mailu (duplicate Message-ID) → queue neuloží duplikát

### 10.3 Manuál

- `deploy/test-mail.sh` — pošle testovací mail přes lokální Postfix (`swaks` nebo `sendmail`) a ukáže výsledek v queue.

---

## 11. Task breakdown

### Task 1 — Project skeleton

- `pyproject.toml` (Python 3.11+, deps)
- Adresáře: `mail_router/`, `tests/`, `deploy/`, `docs/`
- `README.md` s quickstart
- `.gitignore`, `ruff.toml` / `pyproject.toml` linting config

**Akceptace:** `pip install -e .` projde, `pytest` nenajde žádné testy a skončí 0.

### Task 2 — Address parser + lookup

- `mail_router/address.py` s `parse_recipient()`
- `mail_router/lookup.py` s loading + watch
- Jednotkové testy pro obě (všechny kombinace z design tabulky)

**Akceptace:** `pytest tests/unit/test_address.py tests/unit/test_lookup.py` zelené.

### Task 3 — Queue module

- `mail_router/queue.py` s SQLite abstrakcí
- Migrace schema při startu (idempotentní `CREATE TABLE IF NOT EXISTS`)
- Unit testy — enqueue/dequeue/state transitions

**Akceptace:** Unit testy zelené. Concurrent enqueue z dvou procesů nevede ke ztrátě dat (test s threads).

### Task 4 — Policy server

- `mail_router/policy.py` — asyncio TCP-like server na Unix socketu
- Parsing Postfix policy proto
- Unit testy pro proto parser, smoke test proti reálné Postfix žádosti (zachytit vzorek)
- systemd unit

**Akceptace:** Skript posílá simulované Postfix requesty přes `nc -U`, dostává OK/REJECT podle lookupu.

### Task 5 — LMTP receiver

- `mail_router/receiver.py` — aiosmtpd LMTP handler
- Durable write do queue
- ACK až po COMMIT
- Unit test s fake LMTP klientem
- systemd unit

**Akceptace:** Pošlu mail přes `swaks --lhlo` → záznam je v queue, 250 OK dostanu.

### Task 6 — MIME parser

- `mail_router/parser.py` — extrakce headerů, body, příloh z raw .eml
- Robust handling malformed MIME
- Unit testy s různými vzorky (plain, html, multipart mixed, multipart alternative, s přílohami, bez Message-ID, s unicode subjecty)

**Akceptace:** Testy pokrývají 8+ reálných scénářů.

### Task 7 — Shpd client

- `mail_router/client.py` — `httpx.AsyncClient` wrapper
- Multipart/form-data konstrukce
- Idempotency header
- Unit testy s mock HTTP (respx)

**Akceptace:** Klient posílá správně strukturované requesty, správně interpretuje 201/422/500.

### Task 8 — Worker

- `mail_router/worker.py` — asyncio loop, dequeue → parse → post → update
- Backoff + retry podle §3.3
- Integrace s alertem při dead-letter
- systemd unit

**Akceptace:** Ve fake shpd server setupu projde end-to-end test (Task 10).

### Task 9 — Alerts

- `mail_router/alerts.py` — SMTP send
- Throttling per event type
- Konfigurace v yaml
- Unit testy s mock SMTP

**Akceptace:** Dead-letter event → 1 alert. Druhý během throttle → nic. Po throttle → další alert.

### Task 10 — Integrační testy

- Fake shpd server (FastAPI) v `tests/fakes/shpd_server.py`
- `tests/integration/test_end_to_end.py` podle §10.2
- README pro spouštění (potřebuje spuštěný policy + receiver + worker + fake shpd)

**Akceptace:** 5 scénářů z §10.2 projde zeleně v CI.

### Task 11 — Postfix integrace a install script

- `deploy/postfix/main.cf.example`
- `deploy/systemd/*.service`
- `install.sh`
- `deploy/README.md` — end-to-end návod pro admina (čistý LXC → funkční mail router)

**Akceptace:** Checklist: nainstalovat do čistého LXC podle README → odeslat testovací mail → vidět ho v queue → vidět ho delivered (proti skutečnému dev shpd serveru).

### Task 12 — Dokumentace

- `README.md` — intro, quickstart
- `docs/architecture.md` — diagramy, rozhodnutí
- `docs/operations.md` — runbook (jak inspektovat queue, co s dead-letter, rotace klíčů)
- `docs/troubleshooting.md` — časté problémy

**Akceptace:** Dokumentace pokrývá všechny běžné admin úkoly.

---

## 12. Open decisions

1. **Antivir integrace** — v MVP předpokládáme Postfix + amavisd oddělené; router jen konzumuje. Možná varianta: přidat check na `X-Virus-Status: Infected` a skipnout delivery (auto dead-letter). Drobné rozšíření Task 8.

2. **Queue concurrency** — 1 worker stačí pro MVP. Pokud bude potřeba víc, lze spustit N workerů (SQLite `BEGIN IMMEDIATE` na dequeue řeší race). Rozhodnutí odložit na první výkonnostní problém.

3. **Lookup API místo JSON souboru** — v PRD máme `lookup.json`. Rozhraní `lookup.resolve()` je připravené tak, aby šlo bez bolesti vyměnit za HTTP volání na shpd master. Samostatný task, po MVP.

4. **Idempotency bez Message-ID** — pokud mail nemá RFC Message-ID, duplicitu na naší straně nedetekujeme (Postfix ji může zopakovat, my pošleme shpd podruhé). V praxi 99 % mailů Message-ID má. Při problému: generovat hash z (sender, subject, date, body prefix) jako fallback. Neimplementovat teď.

5. **Retry strategie při částečném shpd úspěchu** — pokud shpd vrátí 201 ale spojení se přeruší před tím, než klient odpověď dostane, worker to bude považovat za fail a retrynout. Idempotency na shpd straně (Fáze 2a, §2.3) to zachytí — druhé volání vrátí 201 replay. Takže žádný problém, ale stojí za zmínku v dokumentaci.
