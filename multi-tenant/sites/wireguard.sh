#!/bin/bash
# WireGuard tunnel to the CUPS print servers (install/install-wireguard.sh
# installs it).
#
# Usage:
#   wireguard.sh status
#   wireguard.sh add <name> [--site <site>]
#   wireguard.sh remove <name>
#   wireguard.sh setup-script <name>
#   wireguard.sh config <name>
#   wireguard.sh test <name> [port]
#   wireguard.sh apply
#
#   add           a new print server; prints the tunnel address to enter as
#                 the host of the Printer Server in ERPNext (port 631).
#                 --site is a label only: the containers are shared by every
#                 site, so every site can reach every print server.
#   setup-script  a bash script to run as root ON the print server: installs
#                 WireGuard and opens CUPS to this server. CONTAINS A PRIVATE
#                 KEY: copy it over a safe channel and delete it afterwards.
#   config        only the print server's WireGuard config (also secret)
#   test          ask CUPS for a page from inside the backend container
set -euo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

usage() { sed -n '6,12p' "$0" | sed 's/^#  */Usage: /' >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || err "Run as root (sudo)."
wg_installed || err "WireGuard is not installed — run install/install-wireguard.sh from the checkout"

need_peer() {
    [[ "${1:-}" =~ $WG_NAME_RE ]] || usage
    [ -n "$(wg_peer_ip "$1")" ] || err "no print server '$1'"
}

# Addresses are not reused: an old Printer Server record must never start
# printing one tenant's bills on another tenant's printer.
next_ip() {
    local last used
    last=$(cat "$WG_DIR/sspl-peers/.last" 2>/dev/null || echo 1)
    used=$(wg_peer_lines | awk '{n = split($2, a, "."); if (a[n] > m) m = a[n]} END {print m + 0}')
    if [ "$used" -gt "$last" ]; then last="$used"; fi
    [ "$last" -lt 254 ] || err "no free address left in $WG_SUBNET"
    echo $((last + 1))
}

edit_peers() {   # filter command: old file on stdin, new on stdout
    local tmp
    tmp=$(mktemp "$WG_PEERS_FILE.XXXXXX")
    if [ -r "$WG_PEERS_FILE" ]; then "$@" < "$WG_PEERS_FILE"; else "$@" < /dev/null; fi > "$tmp" \
        || { rm -f "$tmp"; err "could not update $WG_PEERS_FILE"; }
    chmod 644 "$tmp"; mv -f "$tmp" "$WG_PEERS_FILE"
}

peer_config() {
    cat <<CONF
[Interface]
Address = $(wg_peer_ip "$1")/32
PrivateKey = $(cat "$(wg_peer_key "$1")")

[Peer]
PublicKey = $(wg pubkey < "$WG_DIR/sspl-wg.key")
PresharedKey = $(cat "$(wg_psk "$1")")
Endpoint = $WG_ENDPOINT:$WG_PORT
AllowedIPs = $(wg_hub_ip)/32
PersistentKeepalive = 25
CONF
}

setup_script() {
    local ip hub
    ip=$(wg_peer_ip "$1"); hub=$(wg_hub_ip)
    cat <<SCRIPT
#!/bin/bash
# Print server '$1' → ERP. Run as root on the CUPS machine (Debian/Ubuntu).
# CONTAINS A PRIVATE KEY — delete this file after running it.
set -euo pipefail
IP=$ip
HUB=$hub
IF=$WG_IFACE
[ "\$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }
[ -f /etc/cups/cupsd.conf ] || { echo "CUPS is not installed here." >&2; exit 1; }
command -v wg-quick >/dev/null || { apt-get update -qq; apt-get install -y wireguard-tools; }
umask 077
mkdir -p /etc/wireguard
cat > "/etc/wireguard/\$IF.conf" <<'CONF'
$(peer_config "$1")
CONF
systemctl enable "wg-quick@\$IF" >/dev/null 2>&1
systemctl restart "wg-quick@\$IF"
SCRIPT
    cat <<'SCRIPT'

# CUPS: listen on the tunnel address and allow only the ERP server on it
C=/etc/cups/cupsd.conf
B="$C.bak-sspl-$(date +%Y%m%d%H%M%S)"
cp -a "$C" "$B"
T=$(mktemp)
sed '/# sspl-wg$/d' "$C" | awk -v hub="$HUB" '
    {print}
    /^[ \t]*<Location \/>/ {print "  # sspl-wg"; print "  Allow " hub; print "  # sspl-wg"; f = 1}
    END {if (!f) print "<Location />\n  Order allow,deny\n  Allow localhost\n  Allow " hub "\n</Location>"}
' | awk -v a="  Allow $HUB" '$0 == a && seen++ {next} {print}' > "$T"
grep -Eq '^[ \t]*(Port[ \t]+631|Listen[ \t]+(\*:|0\.0\.0\.0:)?631)[ \t]*$' "$T" \
    || grep -Eq "^[ \t]*Listen[ \t]+$IP:631" "$T" || echo "Listen $IP:631" >> "$T"
grep -Eq "^[ \t]*ServerAlias[ \t]+(\*|$IP)[ \t]*$" "$T" || echo "ServerAlias $IP" >> "$T"
# No idle exit: a socket-started cupsd would not come back for the tunnel address
sed -i '/^[ \t]*IdleExitTimeout/d' "$T"
echo "IdleExitTimeout 0" >> "$T"
cat "$T" > "$C"; rm -f "$T"
if ! cupsd -t; then
    cat "$B" > "$C"; echo "cupsd rejected the change; cupsd.conf restored." >&2; exit 1
fi
mkdir -p /etc/systemd/system/cups.service.d
printf '[Unit]\nWants=wg-quick@%s.service\nAfter=wg-quick@%s.service\n' "$IF" "$IF" \
    > /etc/systemd/system/cups.service.d/sspl-wg.conf
chmod 755 /etc/systemd/system/cups.service.d
chmod 644 /etc/systemd/system/cups.service.d/sspl-wg.conf
systemctl daemon-reload
systemctl enable cups >/dev/null 2>&1 || true
systemctl restart cups
if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    ufw allow in on "$IF" from "$HUB" to any port 631 proto tcp >/dev/null
fi
if ping -c 3 -W 3 "$HUB" >/dev/null; then echo "Tunnel is up."; else
    echo "No answer from $HUB: is the UDP port open on the ERP server?" >&2; fi
echo "In ERPNext, set the Printer Server host to $IP, port 631."
echo "Printers must be shared: lpadmin -p <printer> -o printer-is-shared=true"
echo "Now delete this script: it contains a private key."
SCRIPT
}

CMD="${1:-}"; [ $# -eq 0 ] || shift
case "$CMD" in
status)
    wg show "$WG_IFACE" >/dev/null 2>&1 || warn "$WG_IFACE is down"
    printf '%-24s %-15s %-30s %s\n' NAME ADDRESS SITE "LAST HANDSHAKE"
    NOW=$(date +%s)
    HS=$(wg show "$WG_IFACE" latest-handshakes 2>/dev/null || true)
    while read -r name ip pub site; do
        t=$(awk -v k="$pub" '$1 == k {print $2}' <<< "$HS")
        if [ "${t:-0}" -gt 0 ]; then t="$((NOW - t))s ago"; else t=never; fi
        printf '%-24s %-15s %-30s %s\n' "$name" "$ip" "${site:--}" "$t"
    done < <(wg_peer_lines)
    ;;
add)
    NAME="${1:-}"; SITE=""
    [[ "$NAME" =~ $WG_NAME_RE ]] || err "name: lowercase letters, digits and - (31 at most)"
    case "$#:${2:-}" in 1:) ;; 3:--site) SITE="$3"; require_site "$SITE" ;; *) usage ;; esac
    take_lock
    [ -z "$(wg_peer_ip "$NAME")" ] || err "print server '$NAME' already exists"
    umask 077
    N=$(next_ip); IP="$(wg_prefix).$N"
    wg genkey > "$(wg_peer_key "$NAME")"
    wg genpsk > "$(wg_psk "$NAME")"
    PUB=$(wg pubkey < "$(wg_peer_key "$NAME")")
    edit_peers sh -c 'cat; printf "%s %s %s %s\n" "$@"' _ "$NAME" "$IP" "$PUB" "$SITE"
    echo "$N" > "$WG_DIR/sspl-peers/.last"
    apply_wg || err "WireGuard did not take the change — journalctl -u wg-quick@$WG_IFACE"
    ok "$NAME is $IP"
    echo "   On the print server: run its setup script (wireguard.sh setup-script $NAME, or download it in the panel)"
    echo "   In ERPNext: Printer Server host $IP, port 631"
    ;;
remove)
    need_peer "${1:-}"; take_lock
    edit_peers awk -v n="$1" '$1 != n'
    apply_wg || err "WireGuard did not take the change — journalctl -u wg-quick@$WG_IFACE"
    rm -f "$(wg_peer_key "$1")" "$(wg_psk "$1")"
    ok "$1 removed: its key no longer works"
    ;;
setup-script) need_peer "${1:-}"; setup_script "$1" ;;
config)       need_peer "${1:-}"; peer_config "$1" ;;
test)
    need_peer "${1:-}"; PORT="${2:-631}"; valid_port "$PORT" || usage
    IP=$(wg_peer_ip "$1")
    if dc exec -T backend curl -sS -o /dev/null -m 10 -w 'HTTP %{http_code}\n' "http://$IP:$PORT/"; then
        ok "the ERP containers reach CUPS on $IP:$PORT (HTTP 403 = CUPS answers but does not allow $(wg_hub_ip))"
    else
        err "no answer from $IP:$PORT — check 'status' for a handshake, then CUPS on the print server"
    fi
    ;;
apply) take_lock; apply_wg || err "could not apply"; ok "applied" ;;
*) usage ;;
esac
