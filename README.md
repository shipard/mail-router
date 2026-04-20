# shipard-mail-router

Mail-router daemon pro [Shipard](https://shipard.com). Přijímá e-maily přes
Postfix (LMTP), validuje příjemce proti lookup tabulce (SMTP policy server),
parsuje MIME a posílá je do shpd serveru přes HTTP API
(`POST /api/v1/_mail/incoming`).

Mezi přijetím a odesláním stojí perzistentní SQLite queue s retry + dead-letter
— mail je tak chráněný před ztrátou i když shpd server není dostupný.

## Architektura

```
Internet → Postfix(:25)
             ├─(check_policy_service)→ policy server ─┐
             └─(lmtp)────────────────→ LMTP receiver  │  lookup.json
                                         │            │
                                         ↓            │
                                       queue.db ←─────┘
                                         │
                                         ↓ poll
                                       worker ──HTTP──→ shpd
```

Tři samostatné procesy, jedna codebase, tři systemd services.
Detaily a rozhodovací pozadí: [`docs/architecture.md`](docs/architecture.md).

## Rychlý start (dev)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

## Produkční deploy (LXC)

```bash
sudo ./install.sh
# poté: edit /etc/shipard-mail-router/{config.yaml,lookup.json}
sudo systemctl enable --now shipard-mail-router.target
```

Detailní návod: [`deploy/README.md`](deploy/README.md).

## Operační runbook

- [`docs/operations.md`](docs/operations.md) — co dělat, když: mail nedoručen, queue roste, token vyexpiroval, DS se přejmenuje.
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — časté chyby.

## Scope

MVP (fáze 1) pokrývá:

- Inbound mail přes SMTP → LMTP → HTTP API
- Policy server pro validaci příjemců v `RCPT TO`
- Retry + dead-letter s exponenciálním backoff
- E-mail alerty při dead-letter / queue backlog
- Idempotentní doručení podle RFC Message-ID
- systemd + Postfix integrace, install skript

Mimo MVP: IMAP poll, web UI, metrics, outbound mail, HA.

## Licence

MIT — viz [`LICENSE`](LICENSE).
