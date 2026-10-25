#!/usr/bin/env bash
# pwnforge-vpn.sh — let a friend reach PwnForge's labs from anywhere, without Tailscale on their
# device and without forwarding a port on your router, by carrying OpenVPN over Tailscale Funnel.
#
# Why the extra layer: Tailscale Funnel only accepts TLS connections and routes them by the SNI name
# in the TLS ClientHello — a raw OpenVPN stream has no SNI, so a plain .ovpn can't pass through it.
# So OpenVPN here runs on localhost and is wrapped in a TLS layer by `stunnel`: the friend's stunnel
# presents the host's ts.net name as SNI (so Funnel routes it) and verifies the server certificate;
# the host's stunnel terminates that TLS and hands the plaintext to OpenVPN. Funnel forwards the
# encrypted bytes between them.
#
#   friend: OpenVPN -> stunnel(TLS,SNI) --Funnel--> stunnel(terminate TLS) -> OpenVPN  (on this host)
#                                                                                \-> routed to the lab subnet
#
# Commands (run on the PwnForge host, as root):
#   pwnforge-vpn.sh setup [--port 443|8443|10000] [--cidr 10.200.0.0/16]   set it all up and publish it
#   pwnforge-vpn.sh client <name> [OUTFILE.tgz]                            make a friend's connection bundle
#   pwnforge-vpn.sh status                                                 show what's running and the endpoint
#   pwnforge-vpn.sh off                                                    stop and remove everything
#
# PKI and configuration live under $PWNFORGE_VPN_DIR (default /var/lib/pwnforge-funnel-vpn).
set -euo pipefail

STATE="${PWNFORGE_VPN_DIR:-/var/lib/pwnforge-funnel-vpn}"
OVPN_PORT=1194                 # OpenVPN listens here, on localhost only
STUNNEL_LOCAL=11443            # stunnel server listens here, on localhost; Funnel forwards to it
CLIENT_LOCAL=11194             # the friend's OpenVPN connects to their local stunnel on this port
VPN_NET="10.8.0.0"
VPN_CIDR="10.8.0.0/24"
VPN_MASK="255.255.255.0"
DEFAULT_PUB_PORT=10000         # a Funnel-allowed public port (443, 8443 or 10000)
UNIT_OVPN="pwnforge-funnel-openvpn.service"
UNIT_STUN="pwnforge-funnel-stunnel.service"

say()  { printf 'pwnforge-vpn: %s\n' "$*"; }
warn() { printf 'pwnforge-vpn: %s\n' "$*" >&2; }
fail() { printf 'pwnforge-vpn: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
need_root() { [ "$(id -u)" = 0 ] || fail "run this as root (sudo)"; }

ts_fqdn() {
    tailscale status --json 2>/dev/null | python3 -c \
        'import json,sys; print(json.load(sys.stdin).get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null
}

cidr_mask() {   # 10.200.0.0/16 -> 255.255.0.0
    local p="${1#*/}" i b out=""
    for i in 0 1 2 3; do
        b=$(( p - 8 * i ))
        if   [ "$b" -ge 8 ]; then out="$out.255"
        elif [ "$b" -le 0 ]; then out="$out.0"
        else out="$out.$(( 256 - 2 ** (8 - b) ))"; fi
    done
    printf '%s' "${out#.}"
}

ossl_key() { openssl ecparam -name prime256v1 -genkey -noout; }   # a P-256 private key on stdout

# -- PKI: an OpenVPN CA/server/client set, a tls-crypt key, and a separate TLS CA for the stunnel layer
gen_pki() {
    local fqdn="$1"
    mkdir -p "$STATE/pki" "$STATE/tls"
    chmod 700 "$STATE"
    if [ ! -f "$STATE/pki/ca.crt" ]; then
        ossl_key > "$STATE/pki/ca.key"
        openssl req -x509 -new -key "$STATE/pki/ca.key" -sha256 -days 3650 \
            -subj "/CN=pwnforge-vpn-ca" -out "$STATE/pki/ca.crt"
    fi
    if [ ! -f "$STATE/pki/server.crt" ]; then
        ossl_key > "$STATE/pki/server.key"
        openssl req -new -key "$STATE/pki/server.key" -subj "/CN=pwnforge-vpn-server" -out "$STATE/pki/server.csr"
        printf 'extendedKeyUsage=serverAuth\n' > "$STATE/pki/server.ext"
        openssl x509 -req -in "$STATE/pki/server.csr" -CA "$STATE/pki/ca.crt" -CAkey "$STATE/pki/ca.key" \
            -CAcreateserial -sha256 -days 3650 -extfile "$STATE/pki/server.ext" -out "$STATE/pki/server.crt"
        rm -f "$STATE/pki/server.csr" "$STATE/pki/server.ext"
    fi
    if [ ! -f "$STATE/pki/ta.key" ]; then
        openvpn --genkey secret "$STATE/pki/ta.key" 2>/dev/null || openvpn --genkey --secret "$STATE/pki/ta.key"
    fi
    # TLS layer (what stunnel presents / Funnel routes by). Separate self-signed CA, pinned by the client.
    if [ ! -f "$STATE/tls/tls-ca.crt" ]; then
        ossl_key > "$STATE/tls/tls-ca.key"
        openssl req -x509 -new -key "$STATE/tls/tls-ca.key" -sha256 -days 3650 \
            -subj "/CN=pwnforge-tls-ca" -out "$STATE/tls/tls-ca.crt"
    fi
    if [ "$(cat "$STATE/tls/.fqdn" 2>/dev/null || true)" != "$fqdn" ] || [ ! -f "$STATE/tls/tls-server.pem" ]; then
        ossl_key > "$STATE/tls/tls-server.key"
        openssl req -new -key "$STATE/tls/tls-server.key" -subj "/CN=$fqdn" -out "$STATE/tls/tls-server.csr"
        printf 'subjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\n' "$fqdn" > "$STATE/tls/tls-server.ext"
        openssl x509 -req -in "$STATE/tls/tls-server.csr" -CA "$STATE/tls/tls-ca.crt" -CAkey "$STATE/tls/tls-ca.key" \
            -CAcreateserial -sha256 -days 3650 -extfile "$STATE/tls/tls-server.ext" -out "$STATE/tls/tls-server.crt"
        cat "$STATE/tls/tls-server.crt" "$STATE/tls/tls-server.key" > "$STATE/tls/tls-server.pem"
        printf '%s' "$fqdn" > "$STATE/tls/.fqdn"
        rm -f "$STATE/tls/tls-server.csr" "$STATE/tls/tls-server.ext"
    fi
    chmod 600 "$STATE"/pki/*.key "$STATE"/pki/ta.key "$STATE"/tls/*.key "$STATE"/tls/*.pem 2>/dev/null || true
}

write_server_configs() {
    local cidr="$1"
    mkdir -p "$STATE/server"
    cat > "$STATE/server/openvpn.conf" <<CONF
# PwnForge friend VPN — OpenVPN over TCP on localhost; stunnel + Funnel carry it to the internet.
port $OVPN_PORT
proto tcp-server
local 127.0.0.1
dev tun
topology subnet
server $VPN_NET $VPN_MASK
push "route ${cidr%/*} $(cidr_mask "$cidr")"
keepalive 10 60
tls-crypt $STATE/pki/ta.key
ca $STATE/pki/ca.crt
cert $STATE/pki/server.crt
key $STATE/pki/server.key
data-ciphers AES-256-GCM
auth SHA256
persist-key
persist-tun
user nobody
group nogroup
verb 3
CONF
    cat > "$STATE/server/stunnel.conf" <<CONF
; Terminates the TLS that Funnel forwards, and hands the plaintext to OpenVPN.
[pwnforge-vpn]
accept = 127.0.0.1:$STUNNEL_LOCAL
connect = 127.0.0.1:$OVPN_PORT
cert = $STATE/tls/tls-server.pem
CONF
}

write_units() {
    cat > "/etc/systemd/system/$UNIT_OVPN" <<UNIT
[Unit]
Description=PwnForge friend VPN (OpenVPN, localhost)
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=$(command -v openvpn) --config $STATE/server/openvpn.conf
Restart=on-failure
[Install]
WantedBy=multi-user.target
UNIT
    cat > "/etc/systemd/system/$UNIT_STUN" <<UNIT
[Unit]
Description=PwnForge friend VPN (stunnel TLS front)
After=$UNIT_OVPN
Wants=$UNIT_OVPN
[Service]
ExecStart=$(command -v stunnel) $STATE/server/stunnel.conf
Restart=on-failure
[Install]
WantedBy=multi-user.target
UNIT
}

apply_firewall() {   # allow the VPN subnet to reach the lab subnet, and mask replies back
    local cidr="$1"
    if [ -w /proc/sys/net/ipv4/ip_forward ]; then echo 1 > /proc/sys/net/ipv4/ip_forward; fi
    iptables -C FORWARD -i tun0 -s "$VPN_CIDR" -d "$cidr" -j ACCEPT 2>/dev/null \
        || iptables -I FORWARD 1 -i tun0 -s "$VPN_CIDR" -d "$cidr" -j ACCEPT
    iptables -C FORWARD -o tun0 -d "$VPN_CIDR" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null \
        || iptables -I FORWARD 2 -o tun0 -d "$VPN_CIDR" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -t nat -C POSTROUTING -s "$VPN_CIDR" -d "$cidr" -j MASQUERADE 2>/dev/null \
        || iptables -t nat -I POSTROUTING 1 -s "$VPN_CIDR" -d "$cidr" -j MASQUERADE
}

clear_firewall() {
    local cidr; cidr="$(cat "$STATE/.cidr" 2>/dev/null || echo 10.200.0.0/16)"
    iptables -D FORWARD -i tun0 -s "$VPN_CIDR" -d "$cidr" -j ACCEPT 2>/dev/null || true
    iptables -D FORWARD -o tun0 -d "$VPN_CIDR" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
    iptables -t nat -D POSTROUTING -s "$VPN_CIDR" -d "$cidr" -j MASQUERADE 2>/dev/null || true
}

cmd_setup() {
    need_root
    local port="$DEFAULT_PUB_PORT" cidr="10.200.0.0/16"
    while [ $# -gt 0 ]; do
        case "$1" in
            --port) port="${2:-}"; shift 2 ;;
            --cidr) cidr="${2:-}"; shift 2 ;;
            *) fail "unknown option: $1" ;;
        esac
    done
    case "$port" in 443|8443|10000) ;; *) fail "public port must be 443, 8443 or 10000 (Funnel only allows those)";; esac
    echo "$cidr" | grep -qE '^10\.|^172\.(1[6-9]|2[0-9]|3[01])\.|^192\.168\.' || fail "lab CIDR must be a private range"
    have openvpn  || fail "OpenVPN isn't installed (apt install openvpn)"
    have stunnel  || have stunnel4 || fail "stunnel isn't installed (apt install stunnel4)"
    have tailscale|| fail "Tailscale isn't installed"
    have iptables || fail "iptables isn't available"
    local fqdn; fqdn="$(ts_fqdn)"
    [ -n "$fqdn" ] || fail "couldn't read this host's ts.net name — is Tailscale logged in? (tailscale status)"

    say "1/5 keys and certificates"
    gen_pki "$fqdn"
    printf '%s\n' "$cidr" > "$STATE/.cidr"
    printf '%s %s\n' "$fqdn" "$port" > "$STATE/endpoint"

    say "2/5 OpenVPN + stunnel configuration"
    write_server_configs "$cidr"

    say "3/5 background services (survive reboot)"
    write_units
    systemctl daemon-reload
    systemctl enable --now "$UNIT_OVPN" "$UNIT_STUN" >/dev/null 2>&1 \
        || fail "couldn't start the VPN services (journalctl -u $UNIT_OVPN -u $UNIT_STUN)"

    say "4/5 routing from the VPN to the lab network ($cidr)"
    apply_firewall "$cidr"

    say "5/5 publishing over Tailscale Funnel on :$port"
    if ! tailscale funnel --bg --tcp "$port" "tcp://127.0.0.1:$STUNNEL_LOCAL" >/dev/null 2>&1; then
        warn "couldn't turn on Funnel automatically. Enable Funnel for this tailnet in the admin console"
        warn "(Settings -> Access controls, add the funnel nodeAttr), then run:"
        warn "  tailscale funnel --bg --tcp $port tcp://127.0.0.1:$STUNNEL_LOCAL"
    fi
    say "OK"
    say "public endpoint: $fqdn:$port"
    say "make a friend a connection bundle with:  pwnforge-vpn.sh client <their-name>"
}

cmd_client() {
    [ -f "$STATE/endpoint" ] || fail "run 'pwnforge-vpn.sh setup' first"
    local name="${1:-}" out="${2:-}"
    [ -n "$name" ] || fail "usage: pwnforge-vpn.sh client <name> [OUTFILE.tgz]"
    echo "$name" | grep -qE '^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$' || fail "name must be letters, digits, . _ - (max 32)"
    local fqdn port; read -r fqdn port < "$STATE/endpoint" || true
    out="${out:-$STATE/pwnforge-vpn-$name.tgz}"

    local tmp; tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' RETURN
    ossl_key > "$tmp/client.key"
    openssl req -new -key "$tmp/client.key" -subj "/CN=$name" -out "$tmp/client.csr"
    printf 'extendedKeyUsage=clientAuth\n' > "$tmp/client.ext"
    openssl x509 -req -in "$tmp/client.csr" -CA "$STATE/pki/ca.crt" -CAkey "$STATE/pki/ca.key" \
        -CAcreateserial -sha256 -days 1825 -extfile "$tmp/client.ext" -out "$tmp/client.crt" 2>/dev/null

    local b="$tmp/pwnforge-vpn-$name"; mkdir -p "$b"
    {
        cat <<OVPN
client
dev tun
proto tcp
# Connects to the local stunnel (below), which carries this over TLS/Funnel to the host.
remote 127.0.0.1 $CLIENT_LOCAL
nobind
persist-key
persist-tun
remote-cert-tls server
data-ciphers AES-256-GCM
auth SHA256
verb 3
<ca>
$(cat "$STATE/pki/ca.crt")
</ca>
<cert>
$(openssl x509 -in "$tmp/client.crt")
</cert>
<key>
$(cat "$tmp/client.key")
</key>
<tls-crypt>
$(cat "$STATE/pki/ta.key")
</tls-crypt>
OVPN
    } > "$b/pwnforge.ovpn"

    cat > "$b/stunnel.conf" <<STUN
; Wraps the local OpenVPN connection in TLS and sends it to the host over Tailscale Funnel.
client = yes
[pwnforge-vpn]
accept = 127.0.0.1:$CLIENT_LOCAL
connect = $fqdn:$port
sni = $fqdn
verifyChain = yes
CAfile = tls-ca.crt
checkHost = $fqdn
STUN
    cp "$STATE/tls/tls-ca.crt" "$b/tls-ca.crt"

    cat > "$b/connect.command" <<'LAUNCH'
#!/bin/bash
# Double-click on macOS, or run on Linux. Starts the TLS tunnel, then the VPN.
cd "$(cd "$(dirname "$0")" && pwd)" || exit 1
command -v stunnel >/dev/null 2>&1 || { echo "Install stunnel first:  brew install stunnel   (macOS)   /   sudo apt install stunnel4   (Linux)"; read -r -p "Press Enter to close."; exit 1; }
command -v openvpn >/dev/null 2>&1 || { echo "Install openvpn first:  brew install openvpn    (macOS)   /   sudo apt install openvpn    (Linux)"; read -r -p "Press Enter to close."; exit 1; }
echo "Starting the TLS tunnel…"
stunnel stunnel.conf &
STUNNEL_PID=$!
trap 'kill "$STUNNEL_PID" 2>/dev/null' EXIT
sleep 1
echo "Connecting the VPN — you'll be asked for your computer password."
sudo openvpn --config pwnforge.ovpn
LAUNCH
    chmod +x "$b/connect.command"

    cat > "$b/README.txt" <<TXT
PwnForge VPN — connect from anywhere
====================================

You need two small tools once:
  macOS:  brew install openvpn stunnel
  Linux:  sudo apt install openvpn stunnel4

Then connect:
  macOS:  double-click connect.command   (if macOS blocks it: right-click -> Open)
  Linux:  ./connect.command
Leave that window open while you use PwnForge. Close it (Ctrl-C) to disconnect.

Once connected you can reach the lab network ($(cat "$STATE/.cidr" 2>/dev/null || echo 10.200.0.0/16))
directly from your own tools, e.g.:
  nmap -Pn -sV <lab-IP>
  ssh user@<lab-IP>
(Lab targets ignore ping, so always use nmap's -Pn.)

How it works: connect.command starts 'stunnel' (a TLS tunnel to the PwnForge host over Tailscale
Funnel) and then OpenVPN through it. No Tailscale and no router changes are needed on your side.
TXT

    tar -czf "$out" -C "$tmp" "pwnforge-vpn-$name"
    say "wrote friend bundle: $out"
    say "send the whole file to $name; they follow README.txt inside it."
}

cmd_status() {
    local fqdn port cidr
    if [ -f "$STATE/endpoint" ]; then read -r fqdn port < "$STATE/endpoint" || true; else fqdn="(not set up)"; port="-"; fi
    cidr="$(cat "$STATE/.cidr" 2>/dev/null || echo '-')"
    printf 'endpoint=%s:%s\nlab_cidr=%s\n' "$fqdn" "$port" "$cidr"
    if have systemctl; then
        printf 'openvpn=%s\nstunnel=%s\n' \
            "$(systemctl is-active "$UNIT_OVPN" 2>/dev/null || echo inactive)" \
            "$(systemctl is-active "$UNIT_STUN" 2>/dev/null || echo inactive)"
    fi
    if have tailscale; then
        printf 'funnel:\n'; tailscale funnel status 2>/dev/null | sed 's/^/  /' || true
    fi
}

cmd_off() {
    need_root
    local fqdn port
    if [ -f "$STATE/endpoint" ]; then read -r fqdn port < "$STATE/endpoint" || true
        tailscale funnel --tcp "$port" off >/dev/null 2>&1 || true
    fi
    systemctl disable --now "$UNIT_STUN" "$UNIT_OVPN" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$UNIT_OVPN" "/etc/systemd/system/$UNIT_STUN"
    systemctl daemon-reload 2>/dev/null || true
    clear_firewall
    say "friend VPN stopped and unpublished. Keys are kept in $STATE (delete it to remove them)."
}

case "${1:-}" in
    setup)  shift; cmd_setup "$@" ;;
    client) shift; cmd_client "$@" ;;
    status) shift; cmd_status "$@" ;;
    off)    shift; cmd_off "$@" ;;
    -h|--help|help|"") sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) fail "unknown command '$1' (try: setup, client, status, off)" ;;
esac
