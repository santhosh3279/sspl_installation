#!/bin/bash
# Install AdGuard Home DNS service

set -e

err()  { echo "❌ $*" >&2; exit 1; }
step() { echo ""; echo "════════════════════════════════════════"; echo " $*"; echo "════════════════════════════════════════"; }

step "Installing AdGuard Home DNS Service"

# ──────────────────────────────────────────────────────────── preflight checks
if [ "$(id -u)" -ne 0 ]; then
    err "This installer must be run as root (or with sudo)."
fi

# ──────────────────────────────────────────────────────────── Resolve port 53 conflict
step "Resolving port 53 conflict (systemd-resolved)"
if [ -d /etc/systemd/resolved.conf.d ]; then
    echo "Configuring systemd-resolved to disable DNSStubListener..."
    cat > /etc/systemd/resolved.conf.d/adguardhome.conf <<EOF
[Resolve]
DNS=127.0.0.1
DNSStubListener=no
EOF
    # Backup /etc/resolv.conf if it's a symlink or file
    if [ -L /etc/resolv.conf ] || [ -f /etc/resolv.conf ]; then
        echo "Backing up /etc/resolv.conf..."
        mv /etc/resolv.conf /etc/resolv.conf.backup || true
    fi
    echo "Creating symlink to resolv.conf..."
    ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf
    
    echo "Restarting systemd-resolved..."
    systemctl restart systemd-resolved || echo "Warning: systemctl restart systemd-resolved failed"
else
    echo "systemd-resolved config directory /etc/systemd/resolved.conf.d does not exist, skipping systemd-resolved configuration."
fi

# ──────────────────────────────────────────────────────────── Install AdGuard Home
step "Downloading and running AdGuard Home official installation script"
curl -s -S -L https://raw.githubusercontent.com/AdguardTeam/AdGuardHome/master/scripts/install.sh | sh

step "AdGuard Home installed successfully!"
echo "You can now configure AdGuard Home via the web interface on port 3000."
echo "Access it at: http://<your-server-ip>:3000"
