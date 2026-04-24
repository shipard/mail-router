# Troubleshooting

Praktický seznam problémů, na které se narazilo — seřazeno podle frekvence
(nejčastější nahoře).

## `No such file or directory` u LMTP socketu (Postfix chroot)

**Symptom:** Mail log Postfixu ukazuje:

```
status=deferred (connect to ns-mail[/var/run/shipard-mail-router/lmtp.sock]:
 No such file or directory)
```

Socket přitom existuje — `ls -la /run/shipard-mail-router/` ho ukazuje.

**Příčina:** Postfix běží chrootovaně pod `/var/spool/postfix/`. Z jeho
perspektivy cesta `/run/shipard-mail-router/lmtp.sock` neexistuje, protože
chroot ji mapuje na `/var/spool/postfix/run/shipard-mail-router/lmtp.sock`,
která není.

**Fix:** bind mount router runtime dir do chrootu. `install.sh` to dělá
automaticky. Manuálně:

```bash
echo "/run/shipard-mail-router  /var/spool/postfix/var/run/shipard-mail-router  none  bind  0 0" \
    >> /etc/fstab
mkdir -p /var/spool/postfix/var/run/shipard-mail-router
mount -a

# Ověř:
ls /var/spool/postfix/var/run/shipard-mail-router/
# expect: lmtp.sock  policy.sock
```

Pokud `mount -a` selže s "source does not exist", router ještě neběží —
nejdřív `systemctl start shipard-mail-router.target`, pak `mount -a`.

## Services selhávají s `Read-only file system`

**Symptom:** `journalctl` ukazuje:

```
OSError: [Errno 30] Read-only file system: '/var/run/shipard-mail-router/lmtp.sock'
```

**Příčina:** Systemd sandbox (`ProtectSystem=strict`) nerespektuje
`/var/run/X` v `ReadWritePaths`, protože `/var/run` je symlink na `/run`.
Při sandbox aplikaci se resolve cesta liší — whitelist neplatí.

**Fix:** v systemd units používat kanonickou cestu `/run/shipard-mail-router`
místo `/var/run/...`. V repu aktuální verze toto používá; pokud máš starou
verzi, uprav ručně nebo re-run `install.sh`.

```ini
# Správně:
ReadWritePaths=/var/lib/shipard-mail-router /run/shipard-mail-router
```

Pokud nepomůže ani tohle, diagnostika přes vypnutí sandboxu:

```bash
systemctl edit shipard-mail-router-receiver
# [Service]
# ProtectSystem=

systemctl daemon-reload
systemctl restart shipard-mail-router-receiver
```

Pokud teď najede, máš jistotu, že problém je v sandboxu. V LXC container je
`ProtectSystem=strict` často sporné — izolace je už na úrovni kontejneru.

## Bind mount nevzniká po instalaci

**Symptom:** Po `install.sh` instalaci neexistuje
`/var/spool/postfix/var/run/shipard-mail-router/`, a/nebo `mount -a` hlásí, že
zdroj neexistuje.

**Příčina:** Router ještě nebyl spuštěn, `RuntimeDirectory=` v systemd unit
adresář `/run/shipard-mail-router/` ještě nevytvořila.

**Fix:**

```bash
systemctl start shipard-mail-router.target
ls /run/shipard-mail-router/    # musí existovat
mount -a
ls /var/spool/postfix/var/run/shipard-mail-router/    # musí obsahovat sockety
systemctl restart postfix
```

Pořadí: router → mount → postfix.

## Policy socket není čitelný pro Postfix

**Symptom:** Postfix log: `warning: connect to private/policy: Permission denied`.

**Fix:**

```bash
usermod -aG shipard-mail-router postfix
systemctl restart postfix
```

Sockety jsou vytvářené s módem 0660 a skupinou `shipard-mail-router`. Postfix
musí být v té skupině. Členství se aplikuje až při **restartu** postfixu (ne
reloadu).

## `Relay access denied` pro shipard.email

**Symptom:** externí mail vrací 550, journal Postfixu ukazuje:

```
NOQUEUE: reject: RCPT from ...: 554 5.7.1 <...>: Relay access denied
```

**Příčina:** Chybí `virtual_mailbox_domains = shipard.email` (nebo jiná tvoje
doména) v main.cf. Bez toho Postfix nepovažuje doménu za lokální a shodí mail
jako relay.

**Fix:** přidej řádek, reload postfix.

## Policy server vrací DEFER pro všechno

**Symptom:** `journalctl -u shipard-mail-router-policy` logs `policy_handler_error`.

**Příčiny:**

1. `lookup.json` není čitelný procesem `shipard-mail-router`. Zkontroluj
   `ls -la /etc/shipard-mail-router/lookup.json` a vlastnictví.
2. JSON je syntakticky chybný. Otestuj:
   ```bash
   python3 -c "import json; json.load(open('/etc/shipard-mail-router/lookup.json'))"
   ```
3. Chybějící `hosts` pole v lookup.json.

## `451 4.3.0 Temporary failure` od LMTP receiveru

**Symptom:** Postfix log:

```
status=deferred (host ... said: 451 4.3.0 Temporary failure)
```

**Příčina:** Enqueue do SQLite selhal. Typicky:

- **Disk plný** — `df -h /var/lib/shipard-mail-router`.
- **Oprávnění k queue.db** — `ls -la /var/lib/shipard-mail-router/queue.db`.
- **queue.db corrupted** — rzký restart systému během zápisu. `sqlite3 queue.db "PRAGMA integrity_check;"`.
- Mail > 25 MB — Postfix by měl odfiltrovat dřív, ale pokud to projde, zkontroluj
  `message_size_limit` v Postfixu.

## Mail přijat, ale worker nic nedělá

1. `systemctl status shipard-mail-router-worker` — běží?
2. `journalctl -u shipard-mail-router-worker -n 50`
3. `shipard-mail-router-admin stats` — je v queue `pending`?
4. Má DS v lookup.json platnou `api_url`?
5. Ověř, že je endpoint dostupný z routeru:
   ```bash
   curl -I https://api-url.example.com/api/v1/_mail/incoming
   ```

## HTTP 404 → DLQ (`Not found`)

**Příčina:** `api_url` v `lookup.json` neobsahuje správný base URL, nebo má
dvojitý path segment. Router automaticky přidává `/api/v1/_mail/incoming`,
takže `api_url` má být **bez tohoto suffixu**.

**Správně:**

```json
"api_url": "https://shpd-server.example.com"
```

nebo v IP-based dev módu:

```json
"api_url": "http://10.0.0.5/4l3j-z0bz-kz39-echj"
```

Šipka ke shpd routeru určuje DS ID buď z domény (`domains.json`) nebo z
prvního path segmentu (dev mode s IP adresou).

## HTTP 401 → DLQ

Token v `lookup.json` je neplatný/expirovaný/revoknutý. Viz
`operations.md#rotace-api-tokenu`.

## HTTP 422 → DLQ

Server zamítl validaci. Typické důvody:

- `sender_email` neprošel validací (mail s prázdným `From:`, nebo RFC-invalid
  adresou).
- `mailbox` neexistuje — admin ho smazal, nebo DS nemá žádnou
  `is_default=1`. Na shpd straně: `bin/shpd-ds mail-router-bootstrap`.

Detailní důvod: `shipard-mail-router-admin list-dlq`.

## HTTP 500 × 4 → DLQ

shpd opakovaně selhává. Problém je na shpd straně, ne v routeru.

1. Zkontroluj shpd logy (PHP-FPM error log, nginx error log).
2. Po opravě dej mail z DLQ manuálně zpět do `pending` — viz
   `operations.md#dead-letter-replay`.

## Worker restart loop

Typicky chyba v `config.yaml`. `systemctl status` ukáže `exit-code=2`.
Otestuj parsování:

```bash
sudo -u shipard-mail-router /opt/shipard-mail-router/venv/bin/python3 \
    -c "from mail_router.config import Config; Config.load('/etc/shipard-mail-router/config.yaml')"
```

## Lookup.json se nereloaduje

**Symptom:** Změnil jsi `lookup.json`, ale router pořád používá starou hodnotu.

**Příčiny:**

- `lookup_reload: false` v `config.yaml`.
- Soubor byl přepsán `sed -i` nebo editorem, který vytvoří nový inode — mtime
  watch mtime vidí, ale některé editory zachovávají mtime. Ověř mtime:
  ```bash
  stat /etc/shipard-mail-router/lookup.json
  ```
- Syntakticky chybný JSON — reload selže tiše, starý obsah zůstává. Check v logu:
  ```bash
  journalctl -u 'shipard-mail-router-*' --since "1 min ago" | grep -i lookup
  ```

Workaround: po změně `lookup.json` udělej `systemctl restart shipard-mail-router.target`.

## Mail bez Message-ID

Dedup funguje jen když mail má RFC `Message-ID`. Bez něj každý enqueue vytvoří
nový řádek (klíč je `nomid-<uuid4>`). Postfix retry ale takto může vyrobit
duplikát. Řešení (mimo MVP): fallback hash ze `(sender, subject, date,
body-prefix)`.

## Queue roste nekontrolovaně (`pending` stoupá)

1. Ověř, že worker běží: `systemctl status shipard-mail-router-worker`.
2. Ověř, že shpd endpoint odpovídá: `curl -I <api_url>/api/v1/_mail/incoming`.
3. Zkontroluj, jestli worker neběží v retry loopu:
   ```sql
   SELECT ds_id, attempt_count, last_error, COUNT(*)
   FROM queue WHERE state IN ('pending','in_flight')
   GROUP BY ds_id, attempt_count ORDER BY attempt_count DESC;
   ```
4. Pokud je `attempt_count > 0` pro všechny → problém na shpd straně.
5. Pokud je `attempt_count = 0` pro všechny a pending jen roste → worker
   nedequeue-uje, restart: `systemctl restart shipard-mail-router-worker`.

Alert při `pending > queue_size_threshold` (default 100) pošle mail adminovi.

## Po reboot kontejneru mail-router nestartuje

**Typická příčina po LXC restartu:** pořadí služeb. Bind mount vyžaduje, aby
router už vytvořil `/run/shipard-mail-router/`. Při bootu to probíhá paralelně.

**Fix:** v `/etc/systemd/system/shipard-mail-router-receiver.service` přidej
explicitní dependency:

```ini
[Unit]
After=network.target local-fs.target
```

Pokud reboot neprochází ani s tímhle, mount se asi provede dřív než router.
`fstab` řádka s `nofail` jako mount option pomůže: systemd kontejner
neseknout, pokud mount na začátku selže.

```
/run/shipard-mail-router  /var/spool/postfix/var/run/shipard-mail-router  none  bind,nofail  0 0
```

Po prvním startu routeru pak ručně `mount -a`.
