#!/bin/bash
# Send a test email via the local Postfix and inspect the queue.
# Usage: deploy/test-mail.sh <recipient>
#
# Requires `swaks` (apt install swaks).

set -euo pipefail

RECIPIENT="${1:-firma-xyz@shipard.email}"
SENDER="${2:-$(whoami)@$(hostname -f)}"

if ! command -v swaks >/dev/null; then
    echo "swaks not installed — apt install swaks" >&2
    exit 1
fi

swaks \
    --to "$RECIPIENT" \
    --from "$SENDER" \
    --server localhost:25 \
    --header "Subject: mail-router smoke test $(date -Iseconds)" \
    --body "Test from deploy/test-mail.sh at $(date -Iseconds)"

echo
echo "--- queue stats after send ---"
sudo -u shipard-mail-router /opt/shipard-mail-router/venv/bin/shipard-mail-router-admin stats
