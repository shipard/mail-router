# Operations runbook

## Inspekce queue

```bash
# Počty v jednotlivých stavech:
sudo -u shipard-mail-router /opt/shipard-mail-router/venv/bin/shipard-mail-router-admin stats

# Posledních 50 dead-letter položek:
sudo -u shipard-mail-router /opt/shipard-mail-router/venv/bin/shipard-mail-router-admin list-dlq

# Smazat delivered starší než 7 dní:
sudo -u shipard-mail-router /opt/shipard-mail-router/venv/bin/shipard-mail-router-admin prune-delivered --days 7
```

Přímo SQLite:

```bash
sudo -u shipard-mail-router sqlite3 /var/lib/shipard-mail-router/queue.db

sqlite> SELECT state, COUNT(*) FROM queue GROUP BY state;
sqlite> SELECT id, ds_id, recipient_email, dead_letter_reason
        FROM queue WHERE state='dead_letter' ORDER BY id DESC LIMIT 20;
```

## Logy

Všechny tři services logují do journaldu jako JSON:

```bash
journalctl -u shipard-mail-router-receiver -f
journalctl -u shipard-mail-router-worker -f --since "1 hour ago"
journalctl -u shipard-mail-router-policy -n 100
```

Klíčové události:
- `mail_enqueued` — přijato LMTP, zapsáno do queue.
- `mail_duplicate` — přišlo znovu, idempotency zabránilo duplikátu.
- `processing` → `delivered` | `retry_scheduled` | `dead_letter` — životní cyklus v workeru.
- `policy_verdict` — verdict pro každý RCPT (DEBUG level).

## Re-driving dead-letter

DLQ nemá automatickou retry logiku (záměrně — 4xx bývají permanentní).

Manuální re-drive: editací stavu na `pending`.

```sql
-- Zkontroluj důvod:
SELECT dead_letter_reason, attempt_count FROM queue WHERE id=123;

-- Vrať do fronty:
UPDATE queue SET state='pending', attempt_count=0,
                 next_attempt_at=NULL,
                 dead_letter_reason=NULL
WHERE id=123;
```

Worker je při další tick (5 s default) zpracuje.

**Varování:** Pokud důvod DLQ byl 422 VALIDATION_ERROR kvůli trvale chybnému
obsahu mailu, re-drive skončí znova v DLQ. Nejdřív oprav příčinu (např. missing
mailbox na shpd straně).

## Rotace API tokenu

1. Na shpd straně: `bin/shpd-ds mail-router-setup` (viz shpd docs) — vygeneruje
   nový `shpd_ak_...` token. Starý zůstane platný, dokud ho nerevokneš.
2. Edituj `/etc/shipard-mail-router/lookup.json` — nahraď `api_token`.
3. Změna se načte při dalším requestu (mtime watch). Netřeba restart.
4. Na shpd straně revokni starý token.

## Přejmenování DS (hash-id ↔ web-id)

Lookup tabulka podporuje více klíčů pro jeden DS. Při přejmenování:

```json
"data_sources": {
  "4l3j-z0bz-kz39-echj": { "api_url": "...", "api_token": "..." },
  "firma-xyz":           { "api_url": "...", "api_token": "..." }
}
```

Oba klíče směřují na stejný DS — router nerozlišuje.

## Queue backlog

Alert přijde, když `pending > queue_size_threshold` (default 100).

Možné příčiny:
- **shpd pomalý / nedostupný** — zkontroluj `journalctl -u shipard-mail-router-worker`
  na `shpd_timeout` / `shpd_http_error`.
- **Expirovaný token** — worker dostane 401, pošle do DLQ. Zkontroluj DLQ.
- **Rate limit** — shpd zatím nemá, ale proxy před ním může. Zkus manuální curl.

Zvýšení propustnosti: spusť víc workerů (`N` systemd services). SQLite
`BEGIN IMMEDIATE` v `dequeue_batch` ochrání před double-fetchem.

## Backup queue.db

Before major ops:

```bash
sudo -u shipard-mail-router sqlite3 /var/lib/shipard-mail-router/queue.db \
    ".backup /tmp/queue-backup-$(date +%F).db"
```
