# Deployment — shipard-mail-router

End-to-end: čistý LXC s Ubuntu 24.04 → funkční mail-router ve 10 minutách.

## 1. Prerekvizity

```bash
apt update
apt install -y python3 python3-venv postfix swaks
```

Nakonfiguruj DNS: `mail.shipard.email` a `MX` pro `shipard.email` směrují
na IP tohoto LXC. TLS cert přes certbot:

```bash
apt install -y certbot
certbot certonly --standalone -d mail.shipard.email
```

## 2. Instalace

```bash
git clone <repo> /tmp/mail-router
cd /tmp/mail-router
sudo ./install.sh
```

Vytvoří:
- uživatele `shipard-mail-router`
- `/opt/shipard-mail-router/venv` s nainstalovaným balíčkem
- `/etc/shipard-mail-router/{config,lookup}.yaml|json` (jen pokud chybí — NEpřepisuje)
- `/var/lib/shipard-mail-router/` (pro `queue.db`)
- systemd units v `/etc/systemd/system/`

## 3. Konfigurace

### `/etc/shipard-mail-router/config.yaml`

Defaultní hodnoty jsou dobré; nastav jen `alerts.to_addresses`.

### `/etc/shipard-mail-router/lookup.json`

```json
{
  "hosts": ["shipard.email"],
  "data_sources": {
    "firma-xyz": {
      "api_url": "https://shpd.firma-xyz.cz",
      "api_token": "shpd_ak_..."
    }
  }
}
```

- `hosts` — domény, které router přijímá (musí odpovídat `virtual_mailbox_domains` v Postfixu).
- `data_sources[<ds_id>]` — každý DS (hash-id i web-id slug) má `api_url` (origin bez cesty) a `api_token`.

Změny souboru se načtou za běhu (mtime poll). Není potřeba restart.

## 4. Postfix

Postfix běží chrootovaně pod `/var/spool/postfix/` a bez pomoci **nevidí**
sockety v `/run/shipard-mail-router/`. `install.sh` to řeší automaticky přes
bind mount v `/etc/fstab` — viz `deploy/fstab.example`.

Uprav `/etc/postfix/main.cf` podle `deploy/postfix/main.cf.example` — merge bloku
"shipard-mail-router", nenahrazuj celý soubor. Pak:

```bash
usermod -aG shipard-mail-router postfix  # umožní postfixu číst sockety
systemctl restart postfix
```

Ověř, že Postfix vidí sockety uvnitř chrootu:

```bash
ls /var/spool/postfix/var/run/shipard-mail-router/
# expect: lmtp.sock  policy.sock
```

Pokud sockety chybí, spusť `mount -a` a restartuj `shipard-mail-router.target`.

## 5. Spuštění

```bash
systemctl enable --now shipard-mail-router.target
```

Ověř:

```bash
systemctl status shipard-mail-router.target
journalctl -u shipard-mail-router-receiver -f
```

## 6. Smoke test

```bash
deploy/test-mail.sh firma-xyz@shipard.email
# nebo:
swaks --to firma-xyz@shipard.email --from me@elsewhere.com --server localhost:25
```

Ověř v queue:

```bash
sudo -u shipard-mail-router /opt/shipard-mail-router/venv/bin/shipard-mail-router-admin stats
sudo -u shipard-mail-router /opt/shipard-mail-router/venv/bin/shipard-mail-router-admin list-dlq
```

## 7. Upgrade

Pull nový kód, znovu spusť `install.sh`:

```bash
cd /tmp/mail-router && git pull
sudo ./install.sh
systemctl restart shipard-mail-router.target
```

`install.sh` je idempotentní — existující configy nepřepíše.

## 8. Troubleshooting

Viz `docs/troubleshooting.md`.
