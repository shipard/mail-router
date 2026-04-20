# Architektura

## Přehled

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
               │ policy server │   policy.sock  (text protocol)
               └───────┬───────┘
                       │ lookup.json → OK / REJECT
                       ↓
               ┌───────────────┐   virtual_transport = lmtp:unix:…/lmtp.sock
               │   Postfix     │
               └───────┬───────┘
                       │ LMTP
                       ↓
               ┌───────────────┐
               │ LMTP receiver │   durable write → queue.db
               │               │   2xx ACK až po COMMIT
               └───────┬───────┘
                       │
                       ↓
              ┌─────────────────┐
              │   queue.db      │   SQLite WAL
              │ (pending|       │   UNIQUE(idempotency_key)
              │  in_flight|     │
              │  delivered|     │
              │  dead_letter)   │
              └────────┬────────┘
                       │ poll every 5s
                       ↓
               ┌───────────────┐
               │    worker     │   httpx POST /api/v1/_mail/incoming
               │               │   backoff [0, 60, 300, 1800]s, MAX=4
               └───────┬───────┘
                       ↓
                 shpd HTTP API
```

## Tři procesy — proč

Vše běží jako tři samostatné systemd services (jeden codebase, různé entry pointy):

- **policy server** — Postfix drží SMTP session otevřenou a čeká na verdict do
  ~5 s. Policy server musí odpovídat sub-ms, proto je to samostatný lehký
  asyncio proces bez síťových závislostí (jen čte lookup.json).
- **LMTP receiver** — Postfix čeká na `250 OK`. My nesmíme ACK dokud není mail
  v SQLite (jinak ztratíme mail při crashi). Durable write před ACK.
- **worker** — Dělá pomalé věci (HTTP, retry, backoff). Pokud worker crashne
  nebo restartuje, příjem dál běží. Queue je nárazník.

Monolitní verze byla zvažovaná a zamítnutá: jeden crash (HTTP timeout v 500)
by zastavil příjem.

## Adresová logika

Formát: `<ds-id>[{+|--}mailbox]@<domain>`

- **ds-id** je hash-id (`4l3j-z0bz-kz39-echj`) nebo web-id slug (`firma-xyz`).
- **Separátor** — první výskyt `+` nebo `--` v local-part ukončuje ds-id; zbytek
  je mailbox (volitelný).

Důvod pro dva separátory: `+` funguje všude, ale některé spam filtry ho
normalizují pryč. `--` je bezpečný fallback. Viz diskuzi ze 17. 4. 2026.

Implementace: `mail_router.address.parse_recipient()`.

## Idempotence

Klíč: `sha256(domain + "/" + local_part + "/" + message_id)`.

- Generuje ho LMTP receiver při enqueue.
- SQLite `UNIQUE(idempotency_key)` zabrání duplicitní enqueue, pokud Postfix
  retryuje kvůli 451 po DB selhání.
- Klíč se posílá i v HTTP headeru `X-Idempotency-Key` → shpd vrátí uloženou
  odpověď při replay (`idempotent_replay: true`) bez duplikátu záznamu.
- Mail bez RFC `Message-ID` → klíč se negeneruje (interně ukládáme
  `nomid-<uuid4>` pro unikátní index, ale do headeru jde `None`).

## Retry / dead-letter

- 2xx → `delivered`.
- 4xx (kromě 408/429) → `dead_letter` ihned (retry nepomůže).
- 5xx, timeout, connection error → retry s backoff `[0, 60, 300, 1800]` s.
  Po `max_attempts` (default 4) → `dead_letter`.

Dead-letter neexpiruje — admin ho řeší ručně přes
`shipard-mail-router-admin list-dlq`.

## Durability

- SQLite v `journal_mode=WAL`, `synchronous=NORMAL`.
- `BEGIN IMMEDIATE` v `dequeue_batch` — žádná race mezi více workery.
- Crash workera během HTTP calla → řádek zůstane `in_flight`. Při startu
  worker `requeue_in_flight()` vrátí vše zpět na `pending`. Idempotency na
  shpd straně chrání před duplikátem.

## Co je mimo MVP

Viz `tasks/phase1.md` §1. Klíčové odložené položky:

- IMAP poll (jen SMTP v MVP)
- Prometheus metrics (alerty místo nich)
- Outbound mail (samostatná fáze)
- Scope‑limited API keys (shpd follow-up)
- Antivir — očekává se na straně Postfixu (amavisd).
