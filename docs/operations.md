# Operations runbook

Praktický průvodce pro administrátora. Předpokládá, že router běží podle
`deploy/README.md`.

## Co udělat každé ráno (health check)

Jeden liner na ověření, že všechny tři services běží a queue je v pořádku:

```bash
systemctl is-active shipard-mail-router-policy \
                    shipard-mail-router-receiver \
                    shipard-mail-router-worker && \
sudo -u shipard-mail-router \
    /opt/shipard-mail-router/venv/bin/shipard-mail-router-admin stats
```

Očekávaný výstup:

```
active
active
active
{
  "pending": 0,       # nebo malé číslo, roste/klesá
  "in_flight": 0,     # téměř vždy 0, jen milisekundy při zpracování
  "delivered": 12453, # kumulativní, roste
  "dead_letter": 0    # ideálně 0; > 0 = vyžaduje pozornost
}
```

Když `dead_letter > 0`, přečti si, co v DLQ uvízlo:

```bash
sudo -u shipard-mail-router \
    /opt/shipard-mail-router/venv/bin/shipard-mail-router-admin list-dlq
```

Reálné scénáře a řešení jsou níže v sekci [Dead-letter replay](#dead-letter-replay).

## Systemd management

Router se ovládá přes `shipard-mail-router.target`, který sdružuje všechny tři
služby (policy, receiver, worker). Target samotný běh nespouští — jen reprezentuje
celou skupinu.

```bash
# Vše najednou
systemctl start    shipard-mail-router.target
systemctl stop     shipard-mail-router.target
systemctl restart  shipard-mail-router.target

# Jednotlivě (když chceš oprášit jen worker kvůli hotfixu)
systemctl restart  shipard-mail-router-worker

# Enable at boot
systemctl enable   shipard-mail-router.target

# Disable (pauza router, Postfix bude kupit mail v queue)
systemctl disable  shipard-mail-router.target
```

**Pořadí při potížích:** nikdy nerestartuj **receiver bez pozornosti k running LMTP**
— Postfix čeká na 250 OK. Pokud receiver zabiješ v půlce přenosu, Postfix retryuje
za pár minut, není to problém, jen to generuje warning v mail logu.

## Inspekce queue

```bash
# Počty v jednotlivých stavech:
sudo -u shipard-mail-router /opt/shipard-mail-router/venv/bin/shipard-mail-router-admin stats

# Posledních 50 dead-letter položek:
sudo -u shipard-mail-router /opt/shipard-mail-router/venv/bin/shipard-mail-router-admin list-dlq

# Smazat delivered starší než 7 dní (cron 1×/den doporučen):
sudo -u shipard-mail-router /opt/shipard-mail-router/venv/bin/shipard-mail-router-admin prune-delivered --days 7
```

Přímo SQLite pro ad-hoc dotazy:

```bash
sudo -u shipard-mail-router sqlite3 /var/lib/shipard-mail-router/queue.db

sqlite> SELECT state, COUNT(*) FROM queue GROUP BY state;
sqlite> SELECT id, ds_id, recipient_email, dead_letter_reason
        FROM queue WHERE state='dead_letter' ORDER BY id DESC LIMIT 20;
sqlite> SELECT id, ds_id, recipient_email, subject, attempt_count, last_error
        FROM queue WHERE state='pending' AND attempt_count > 0;
```

### Význam stavů

| Stav          | Co znamená                                                                 |
|---------------|----------------------------------------------------------------------------|
| `pending`     | Čeká na workera. Přirozeně se pohybuje `pending → in_flight → delivered`. |
| `in_flight`   | Worker právě volá shpd API. Obvykle jen desítky ms. Dlouhodobě > 1 min = zablokovaný worker (viz níže). |
| `delivered`   | Úspěšně zpracované shpd. Drží se 7 dní kvůli debugu, pak prune.            |
| `dead_letter` | 4 neúspěchy, nebo permanentní 4xx. Vyžaduje manuální zásah.                |

**`in_flight` zasekne-li se:** nejčastěji kvůli restartu workera uprostřed HTTP
requestu. Ruční reset:

```sql
UPDATE queue SET state='pending'
WHERE state='in_flight' AND id IN (SELECT id FROM queue WHERE state='in_flight');
```

## Dead-letter replay

DLQ nemá automatickou retry logiku (záměrně — 4xx bývají permanentní). Když je v
DLQ mail, který tam neměl být (třeba 500 kvůli dočasné shpd chybě, kterou jsi
mezitím opravil), vrať ho do fronty takto:

```bash
# 1. Zjisti id a důvod
sudo -u shipard-mail-router \
    /opt/shipard-mail-router/venv/bin/shipard-mail-router-admin list-dlq
```

```sql
-- 2. Ověř, že je to ta správná položka
SELECT id, ds_id, recipient_email, subject, dead_letter_reason, attempt_count
FROM queue WHERE id=123;

-- 3. Vrať do fronty
UPDATE queue SET state='pending',
                 attempt_count=0,
                 next_attempt_at=NULL,
                 dead_letter_reason=NULL,
                 last_error=NULL
WHERE id=123;
```

Worker ji zpracuje při dalším tick (5 s default).

**Bulk replay** (např. všechny z jednoho DS po jeho downtime):

```sql
UPDATE queue SET state='pending', attempt_count=0,
                 next_attempt_at=NULL, dead_letter_reason=NULL, last_error=NULL
WHERE state='dead_letter' AND ds_id='firma-xyz';
```

**Varování:** Když důvod DLQ byl 422 VALIDATION_ERROR kvůli trvale chybnému
obsahu mailu (např. neexistující mailbox, malformed MIME), replay skončí
okamžitě zase v DLQ. Nejdřív oprav příčinu na shpd straně (např. doplň
mailbox, nebo `bin/shpd-ds mail-router-bootstrap`).

## Rotace API tokenu

**Router napojený na hosting (lookup-sync):** rotaci udělej na hostingu —
`shpd-ds mail-router-setup --force --json` na DS a nový token vlož do
evidence hostingu (admin form, pole Mail token); do 2 minut ho stáhne
timer. Ruční edit `lookup.json` by další sync přepsal.

**Ručně spravovaný router:**

1. **Na shpd straně:** `bin/shpd-ds mail-router-setup --force` — vygeneruje nový
   `shpd_ak_...` token. Starý **zůstane platný**, dokud ho nerevokneš.
2. Edituj `/etc/shipard-mail-router/lookup.json` — nahraď `api_token`.
3. Změna se načte při dalším requestu (mtime watch). Netřeba restart routeru.
4. Ověř: `journalctl -u shipard-mail-router-worker -f` + pošli testovací mail.
   Úspěšné doručení = nový token funguje.
5. **Až teď** revokni starý token na shpd straně.

Kdyby krok 4 selhal (401 v logu), nerevokuj starý token a zjisti, proč nový
nefunguje (překlep, chybný scope, atd.).

## Přidání / přejmenování DS

Lookup tabulka podporuje více klíčů pro jeden DS. Při přidání:

```json
"data_sources": {
  "4l3j-z0bz-kz39-echj": { "api_url": "...", "api_token": "..." },
  "firma-xyz":           { "api_url": "...", "api_token": "..." }
}
```

Oba klíče směřují na stejný DS — router nerozlišuje. Pro přejmenování stačí
přidat nový klíč a nechat starý ještě chvíli aktivní, po přechodu odstranit.

Změny se načtou automaticky (mtime watch), bez restartu.

**Router napojený na hosting (lookup-sync):** nové DS přibývají samy —
hosting servíruje všechny aktivní DS s mail tokenem (ds_id i web-id
slug). Ruční zásahy do `lookup.json` další sync přepíše.

## Kapacita a limity

**Disk — sleduj:**

```bash
df -h /var/lib/shipard-mail-router    # queue.db + WAL
df -h /run                            # sockety, zanedbatelné
df -h /var/log                        # journald
```

Růst `queue.db` by měl být lineární — ~10 KB per mail × deliver + 7 dní = cca
70 KB/mail. 100 000 mailů/měsíc → ~7 GB. Prune snižuje na ~5 % objemu.

**Queue size alerty:** `alerts.queue_size_threshold` v `config.yaml` (default 100).
Když `pending > 100`, pošle se mail admin-adresám. Throttle 30 min.

**Message size limit:** 25 MB (Postfix, `message_size_limit = 26214400`). Mail
větší → Postfix ho odmítne v SMTP.

**Počet příloh:** bez limitu, ale warning v logu při > 50. Praktický limit dá
shpd při uploadu.

## Přidání dalšího workera (škálování)

Default je 1 worker. SQLite `BEGIN IMMEDIATE` v `dequeue_batch` zabrání
double-fetch. Přidání N-tého workera:

```bash
# /etc/systemd/system/shipard-mail-router-worker-2.service
# (zkopíruj shipard-mail-router-worker.service a přejmenuj)

systemctl daemon-reload
systemctl enable --now shipard-mail-router-worker-2
```

Ověř, že oba zpracovávají:

```bash
journalctl -u 'shipard-mail-router-worker*' -f
```

Doporučená hranice pro uvažování o druhém workeru: trvalý `pending > 50`.

## Backup a restore

### Backup

Před major ops (upgrade, velká úprava queue):

```bash
sudo -u shipard-mail-router sqlite3 /var/lib/shipard-mail-router/queue.db \
    ".backup /tmp/queue-backup-$(date +%F).db"

# Konfigurace:
cp -a /etc/shipard-mail-router /tmp/shipard-mail-router-config-$(date +%F)
```

### Restore

```bash
systemctl stop shipard-mail-router.target
cp /tmp/queue-backup-YYYY-MM-DD.db /var/lib/shipard-mail-router/queue.db
chown shipard-mail-router:shipard-mail-router /var/lib/shipard-mail-router/queue.db
systemctl start shipard-mail-router.target
```

**Pozor:** SQLite má WAL soubory `queue.db-wal` a `queue.db-shm` vedle
hlavního souboru. Backup pomocí `.backup` je konzistentní, plain `cp` není.

## Logy

Všechny tři services logují do journaldu jako JSON:

```bash
journalctl -u shipard-mail-router-receiver -f
journalctl -u shipard-mail-router-worker -f --since "1 hour ago"
journalctl -u shipard-mail-router-policy -n 100
```

Klíčové eventy (hledej `msg=...`):

| Event              | Význam                                                      |
|--------------------|-------------------------------------------------------------|
| `mail_enqueued`    | Přijato LMTP, zapsáno do queue.                             |
| `mail_duplicate`   | Přišlo znovu, idempotency zabránilo duplikátu.              |
| `processing`       | Worker začal zpracovávat.                                   |
| `delivered`        | Úspěch — shpd vrátil 201.                                   |
| `retry_scheduled`  | shpd 5xx, zařadí se za N minut zpět.                        |
| `dead_letter`      | Překročen max_attempts nebo 4xx.                            |
| `policy_verdict`   | Verdikt pro každý RCPT (DEBUG level).                       |
| `lookup_loaded`    | Lookup reload po změně `lookup.json`.                       |
| `alert_sent`       | Alert mail odeslán adminovi.                                |
| `lookup_sync_unchanged` | lookup-sync: hosting vrátil 304, soubor beze změny.    |
| `lookup_sync_updated`   | lookup-sync: nový obsah atomicky zapsán.               |
| `lookup_sync_failed`    | lookup-sync: síť/HTTP/validace selhala — jede se na stale lookup. |
| `lookup_sync_empty_data_sources` | lookup-sync: hosting poslal prázdný seznam DS (zapsáno, ale podezřelé). |

**Zdravý log při provozu** vypadá jako: `lookup_loaded` (při startu), pak stream
`policy_verdict`, `mail_enqueued`, `processing`, `delivered`. Nic varovného.

**Nezdravý log:** opakovaný `retry_scheduled` na stejném id, `dead_letter`,
`alert_sent`, nebo `WARNING`/`ERROR` level.

## Chaos testing checklist (před produkcí)

Před tím, než pustíš na router externí maily, projdi tyto scénáře:

- [ ] **shpd down během doručení** — `systemctl stop shpd` na shpd straně,
      pošli mail, zkontroluj, že jde do `pending` s rostoucím `attempt_count`.
      Po `systemctl start shpd` se během 1–30 min doručí.
- [ ] **Worker restart uprostřed** — pošli mail, rychle `systemctl restart worker`.
      Mail buď ještě nebyl odeslán (zůstane `pending`) nebo byl odeslán a shpd
      ho díky idempotency klíči nezduplikuje.
- [ ] **Neplatný token** — dočasně zneplatni token v shpd, pošli mail, nech
      worker 4× retrynout → DLQ → e-mail alert dorazí.
- [ ] **25 MB attachment** — pošli velký mail, zkontroluj že projde plnou pipeline.
- [ ] **Mail bez Message-ID** — pošli raw přes netcat bez Message-ID, zkontroluj,
      že projde (idempotency se jen neaplikuje).
- [ ] **Policy reject** — mail na neznámé DS → `550` během SMTP, neenqueue.
- [ ] **Prune** — ručně posuň `delivered_at` o 10 dní zpátky, spusť prune,
      ověř cleanup.

## Deinstalace (pro potřeby čisté re-instalace)

```bash
# 1. Zastavit a disable services
systemctl stop    shipard-mail-router.target
systemctl disable shipard-mail-router.target

# 2. Postfix back to default (otevři main.cf, odstraň blok shipard-mail-router)
systemctl reload postfix

# 3. Backup queue, kdyby něco:
cp /var/lib/shipard-mail-router/queue.db /tmp/queue-$(date +%F).db

# 4. Odmontovat bind mount (pokud existuje)
umount /var/spool/postfix/var/run/shipard-mail-router 2>/dev/null
sed -i '/shipard-mail-router/d' /etc/fstab

# 5. Smazat
rm -rf /opt/shipard-mail-router
rm -rf /etc/shipard-mail-router
rm -rf /var/lib/shipard-mail-router
rm -f  /etc/systemd/system/shipard-mail-router-*.service
rm -f  /etc/systemd/system/shipard-mail-router.target
systemctl daemon-reload

# 6. Uživatel (opce)
userdel shipard-mail-router
```

Po tomhle jde `./install.sh` pustit znovu na zelené louce.
