# Troubleshooting

## Postfix `reject_unauth_destination` pro shipard.email

**Symptom:** externí mail vrací 550, journal Postfixu ukazuje
`NOQUEUE: reject: RCPT from ...: 554 5.7.1 <...>: Relay access denied`.

**Příčina:** Chybí `virtual_mailbox_domains = shipard.email` v main.cf. Bez toho
Postfix nepovažuje `shipard.email` za lokální doménu a shodí mail jako relay.

**Fix:** přidej řádek, reload postfix.

## Policy server vrací DEFER pro všechno

**Symptom:** `journalctl -u shipard-mail-router-policy` logs `policy_handler_error`.

**Příčiny:**

1. `lookup.json` není čitelný procesem `shipard-mail-router`. Zkontroluj `ls -la
   /etc/shipard-mail-router/lookup.json` a vlastnictví.
2. JSON je syntakticky chybný. Otestuj `python3 -c "import json; json.load(open('/etc/shipard-mail-router/lookup.json'))"`.

## `451 4.3.0 Temporary failure` od LMTP receiveru

**Symptom:** Postfix log: `status=deferred (host ... said: 451 4.3.0 Temporary failure)`.

**Příčina:** Enqueue do SQLite selhal. Typicky:
- Disk plný (`df -h /var/lib/shipard-mail-router`).
- Oprávnění k `queue.db` (`ls -la /var/lib/shipard-mail-router/queue.db`).
- Mail > 25 MB — Postfix by měl odfiltrovat dřív, ale pokud to projde, SQLite si
  poradí; spíš zkontroluj `message_size_limit` v Postfixu.

Journal workera: `journalctl -u shipard-mail-router-receiver --since "5 min ago"`.

## Mail přijat, ale worker nic nedělá

1. `systemctl status shipard-mail-router-worker` — běží?
2. `journalctl -u shipard-mail-router-worker -n 50`
3. `shipard-mail-router-admin stats` — je v queue `pending`?
4. Má DS v lookup.json platnou `api_url`?

## HTTP 401 → DLQ

Token v `lookup.json` je neplatný/expirovaný. Viz `operations.md#rotace-api-tokenu`.

## HTTP 422 → DLQ

Server zamítl validaci. Typické důvody:
- `sender_email` neprošel `FILTER_VALIDATE_EMAIL` (mail s prázdným From, nebo
  RFC-invalid adresou).
- `mailbox` neexistuje — admin ho smazal, nebo DS nemá žádnou
  `is_default=1`. Na shpd straně: `bin/shpd-ds mail-router-bootstrap`.

Detailní důvod: `shipard-mail-router-admin list-dlq`.

## HTTP 500 × 4 → DLQ

shpd opakovaně selhává. Zkontroluj shpd logy na jeho straně — problem je tam,
ne tady. Naše retry funguje; po opravě dej DLQ mail manuálně zpět do `pending`
(viz `operations.md`).

## Worker restart loop

Typicky chyba v config.yaml. `systemctl status` ukáže `exit-code=2`.
Otestuj parsování:

```bash
sudo -u shipard-mail-router /opt/shipard-mail-router/venv/bin/python3 \
    -c "from mail_router.config import Config; Config.load('/etc/shipard-mail-router/config.yaml')"
```

## Policy socket není čitelný pro Postfix

**Symptom:** Postfix log: `warning: connect to private/policy: Permission denied`.

**Fix:**

```bash
usermod -aG shipard-mail-router postfix
systemctl restart postfix
```

Sockety jsou vytvářené s módem 0660 a skupinou `shipard-mail-router`. Postfix
musí být v té skupině.

## Mail bez Message-ID

Dedup funguje jen když mail má RFC `Message-ID`. Bez něj každý enqueue vytvoří
nový řádek (klíč je `nomid-<uuid4>`). Postfix retry ale takto může vyrobit
duplikát. Řešení (mimo MVP): fallback hash ze `(sender, subject, date, body-prefix)`.
