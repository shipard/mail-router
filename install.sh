#!/bin/bash
# Install shipard-mail-router on a clean Ubuntu 24.04 LXC.
# Idempotent — re-running upgrades the venv and refreshes the systemd units.
#
# Run as root. Does NOT start services — you must first populate:
#   /etc/shipard-mail-router/config.yaml
#   /etc/shipard-mail-router/lookup.json
# then:
#   systemctl enable --now shipard-mail-router.target

set -euo pipefail

USER_NAME="shipard-mail-router"
INSTALL_DIR="/opt/shipard-mail-router"
VENV_DIR="$INSTALL_DIR/venv"
ETC_DIR="/etc/shipard-mail-router"
LIB_DIR="/var/lib/shipard-mail-router"
RUN_DIR="/run/shipard-mail-router"
POSTFIX_CHROOT_RUN="/var/spool/postfix/var/run/shipard-mail-router"
FSTAB_ENTRY="$RUN_DIR  $POSTFIX_CHROOT_RUN  none  bind  0 0"
SYSTEMD_DIR="/etc/systemd/system"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Run as root (sudo)." >&2
    exit 1
fi

echo "[1/8] ensure user $USER_NAME"
if ! id -u "$USER_NAME" >/dev/null 2>&1; then
    useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$USER_NAME"
fi

echo "[2/8] ensure directories"
install -d -o "$USER_NAME" -g "$USER_NAME" -m 0750 "$ETC_DIR"
install -d -o "$USER_NAME" -g "$USER_NAME" -m 0750 "$LIB_DIR"
# $RUN_DIR is created by systemd via RuntimeDirectory=, no need to pre-create.
install -d -o "$USER_NAME" -g "$USER_NAME" -m 0755 "$INSTALL_DIR"

echo "[3/8] ensure python venv in $VENV_DIR"
if ! command -v python3 >/dev/null; then
    echo "python3 missing — apt install python3 python3-venv" >&2
    exit 1
fi
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip wheel

echo "[4/8] pip install mail_router from $SCRIPT_DIR"
"$VENV_DIR/bin/pip" install "$SCRIPT_DIR"

echo "[5/8] install example configs (only if not present)"
for f in config.example.yaml lookup.example.json; do
    src="$SCRIPT_DIR/deploy/config/$f"
    dst_name="$(echo "$f" | sed 's/\.example//')"
    dst="$ETC_DIR/$dst_name"
    if [[ ! -f "$dst" ]]; then
        cp "$src" "$dst"
        chown "$USER_NAME:$USER_NAME" "$dst"
        chmod 0640 "$dst"
        echo "  -> installed example $dst (EDIT BEFORE STARTING)"
    else
        echo "  -> $dst already exists, skipping"
    fi
done

echo "[6/8] install systemd units"
install -m 0644 "$SCRIPT_DIR/deploy/systemd/shipard-mail-router-policy.service" "$SYSTEMD_DIR/"
install -m 0644 "$SCRIPT_DIR/deploy/systemd/shipard-mail-router-receiver.service" "$SYSTEMD_DIR/"
install -m 0644 "$SCRIPT_DIR/deploy/systemd/shipard-mail-router-worker.service" "$SYSTEMD_DIR/"
install -m 0644 "$SCRIPT_DIR/deploy/systemd/shipard-mail-router.target" "$SYSTEMD_DIR/"
systemctl daemon-reload

echo "[7/8] configure bind mount into postfix chroot"
# Postfix runs chrooted under /var/spool/postfix/. It cannot see
# /run/shipard-mail-router/ without a bind mount. See deploy/fstab.example.
if [[ -d /var/spool/postfix ]]; then
    mkdir -p "$POSTFIX_CHROOT_RUN"

    # Add fstab entry if missing
    if ! grep -qF "$POSTFIX_CHROOT_RUN" /etc/fstab; then
        echo "  -> adding bind mount to /etc/fstab"
        {
            echo ""
            echo "# shipard-mail-router — makes router sockets visible inside postfix chroot"
            echo "$FSTAB_ENTRY"
        } >> /etc/fstab
    else
        echo "  -> /etc/fstab already has entry, skipping"
    fi

    # Activate mount if not already mounted
    if ! mountpoint -q "$POSTFIX_CHROOT_RUN"; then
        if mount "$POSTFIX_CHROOT_RUN" 2>/dev/null; then
            echo "  -> bind mount activated"
        else
            echo "  -> could not mount now (source $RUN_DIR may not exist yet)"
            echo "     will be mounted automatically on next boot, or run 'mount -a' after starting the router"
        fi
    else
        echo "  -> bind mount already active"
    fi
else
    echo "  -> /var/spool/postfix does not exist, skipping (install postfix first)"
fi

echo "[8/8] chown ownership of $INSTALL_DIR"
chown -R "$USER_NAME:$USER_NAME" "$INSTALL_DIR"

cat <<EOF

Installation complete.

Next:
  1. Edit:  $ETC_DIR/config.yaml
  2. Edit:  $ETC_DIR/lookup.json

  3. Grant postfix access to router sockets (so chrooted postfix can connect):
        usermod -aG $USER_NAME postfix
        systemctl restart postfix

  4. Update Postfix main.cf (see deploy/postfix/main.cf.example), reload postfix.

  5. Start services:
        systemctl enable --now shipard-mail-router.target

  6. Verify bind mount is active (postfix needs to see the sockets):
        ls $POSTFIX_CHROOT_RUN/
     Expect: lmtp.sock  policy.sock
     If missing, run: mount -a

EOF
