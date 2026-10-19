#!/usr/bin/env bash
# PWNFORGE_LAB_ACCESS_CLI=Linux
# Persistent direct access from Linux to PwnForge's private lab subnet.
# Install this file as `pwnforge` with pwnforge-install-linux.sh, then use:
#   pwnforge install 10.200.0.0/16 192.168.1.123
#   pwnforge status | remove | test <target-IP> [port]
set -euo pipefail

CONFIG=/etc/pwnforge/lab-route.conf
WORKER=/usr/local/libexec/pwnforge-lab-route
SERVICE=/etc/systemd/system/pwnforge-lab-route.service
TIMER=/etc/systemd/system/pwnforge-lab-route.timer

usage() {
  cat <<'USAGE'
PwnForge Linux lab route helper

  install <lab-CIDR> <server-LAN-IP>  Install a persistent LAN fallback route
  status                              Show the current route and systemd status
  remove                              Remove this helper and its LAN route
  test <target-IP> [port]             Test a TCP port (default: 22)

Example:
  pwnforge install 10.200.0.0/16 192.168.1.123

Requires systemd and iproute2. Approved Tailscale routes are left in control
when the route uses tailscale0. On Linux, enable route acceptance if needed:
  sudo tailscale set --accept-routes=true
USAGE
}

valid_ipv4() {
  awk -F. 'NF != 4 {exit 1} {for (i=1;i<=4;i++) if ($i !~ /^[0-9]+$/ || $i < 0 || $i > 255) exit 1}' <<< "$1"
}

valid_private_cidr() {
  local ip prefix
  ip="${1%/*}"
  prefix="${1#*/}"
  [[ "$1" == */* && "$prefix" =~ ^[0-9]+$ ]] || return 1
  valid_ipv4 "$ip" || return 1
  awk -F'[./]' -v p="$prefix" '
    BEGIN { if (p < 8 || p > 30) exit 1 }
    {
      private = ($1 == 10) || ($1 == 172 && $2 >= 16 && $2 <= 31) || ($1 == 192 && $2 == 168)
      if (!private) exit 1
      for (i=1; i<=4; i++) {
        bits = p - ((i-1)*8)
        if (bits >= 8) continue
        if (bits <= 0) { if ($i != 0) exit 1; continue }
        step = 2 ^ (8-bits)
        if (($i % step) != 0) exit 1
      }
    }' <<< "$1"
}

route_value() {
  awk -v key="$1" '{for (i=1; i<NF; i++) if ($i == key) {print $(i+1); exit}}'
}

as_root() {
  if [[ "$EUID" -eq 0 ]]; then "$@"; else exec sudo "$0" "$@"; fi
}

install_route() {
  local cidr="$1" gateway="$2"
  valid_private_cidr "$cidr" || { echo "Use a canonical private IPv4 network, such as 10.200.0.0/16." >&2; exit 2; }
  valid_ipv4 "$gateway" || { echo "The server LAN gateway must be an IPv4 address." >&2; exit 2; }
  command -v systemctl >/dev/null 2>&1 || { echo "This Linux route helper requires systemd." >&2; exit 1; }
  command -v ip >/dev/null 2>&1 || { echo "Install iproute2 first (the 'ip' command is missing)." >&2; exit 1; }
  as_root "$0" _install-root "$cidr" "$gateway"
}

install_root() {
  local cidr="$1" gateway="$2"
  install -d -o root -g root -m 0755 /etc/pwnforge /usr/local/libexec
  printf 'CIDR=%s\nGATEWAY=%s\n' "$cidr" "$gateway" > "$CONFIG"
  chown root:root "$CONFIG"
  chmod 0644 "$CONFIG"

  cat > "$WORKER" <<'WORKER_EOF'
#!/usr/bin/env bash
set -u
CONFIG=/etc/pwnforge/lab-route.conf
[[ -r "$CONFIG" ]] || exit 0
source "$CONFIG"
PROBE="${CIDR%/*}"
route_info="$(ip -4 route get "$PROBE" 2>/dev/null | head -n1 || true)"
route_dev="$(awk '{for (i=1;i<NF;i++) if ($i=="dev") {print $(i+1); exit}}' <<< "$route_info")"
route_via="$(awk '{for (i=1;i<NF;i++) if ($i=="via") {print $(i+1); exit}}' <<< "$route_info")"

# An approved subnet route belongs to Tailscale; never replace or delete it.
if [[ "$route_dev" == tailscale* ]]; then exit 0; fi

default_info="$(ip -4 route show default 2>/dev/null | head -n1 || true)"
default_dev="$(awk '{for (i=1;i<NF;i++) if ($i=="dev") {print $(i+1); exit}}' <<< "$default_info")"
host_info="$(ip -4 route get "$GATEWAY" 2>/dev/null | head -n1 || true)"
host_dev="$(awk '{for (i=1;i<NF;i++) if ($i=="dev") {print $(i+1); exit}}' <<< "$host_info")"
host_via="$(awk '{for (i=1;i<NF;i++) if ($i=="via") {print $(i+1); exit}}' <<< "$host_info")"

if [[ "$route_via" == "$GATEWAY" ]]; then
  if [[ "$host_dev" == "$default_dev" && -z "$host_via" ]]; then exit 0; fi
  # The client left the server LAN. Remove only the route this helper owns.
  ip -4 route del "$CIDR" via "$GATEWAY" dev "$route_dev" 2>/dev/null || true
  exit 0
fi

# Never replace another specific route, or add a route while away from the LAN.
if ip -4 route show exact "$CIDR" 2>/dev/null | grep -q .; then exit 0; fi
[[ -n "$default_dev" && "$host_dev" == "$default_dev" && -z "$host_via" ]] || exit 0
ip -4 route add "$CIDR" via "$GATEWAY" dev "$default_dev" metric 50 2>/dev/null || true
WORKER_EOF
  chmod 0755 "$WORKER"
  chown root:root "$WORKER"

  cat > "$SERVICE" <<'SERVICE_EOF'
[Unit]
Description=Maintain the PwnForge lab LAN route
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/pwnforge-lab-route
SERVICE_EOF

  cat > "$TIMER" <<'TIMER_EOF'
[Unit]
Description=Check PwnForge LAN and Tailscale routes periodically

[Timer]
OnBootSec=15s
OnUnitActiveSec=30s
AccuracySec=5s
Persistent=true
Unit=pwnforge-lab-route.service

[Install]
WantedBy=timers.target
TIMER_EOF

  chown root:root "$SERVICE" "$TIMER"
  chmod 0644 "$SERVICE" "$TIMER"
  systemctl daemon-reload
  systemctl enable --now pwnforge-lab-route.timer
  "$WORKER"
  echo "Installed persistent route helper for $cidr via $gateway."
  echo "It yields to an approved route on tailscale0 and adds the LAN route only when the server is on this LAN."
}

status() {
  if [[ ! -r "$CONFIG" ]]; then
    echo "PwnForge route helper is not installed."
    return 0
  fi
  # shellcheck disable=SC1090
  source "$CONFIG"
  local route_info dev via timer
  route_info="$(ip -4 route get "${CIDR%/*}" 2>/dev/null | head -n1 || true)"
  dev="$(route_value dev <<< "$route_info")"
  via="$(route_value via <<< "$route_info")"
  [[ -n "$dev" ]] || dev=unavailable
  [[ -n "$via" ]] || via=direct-or-default
  timer=inactive
  systemctl is-active --quiet pwnforge-lab-route.timer 2>/dev/null && timer=active
  echo "Lab network: $CIDR"
  echo "LAN gateway: $GATEWAY"
  echo "Current route: interface=$dev, next-hop=$via"
  if [[ "$dev" == tailscale* ]]; then
    echo "Path: Tailscale subnet route"
  elif [[ "$via" == "$GATEWAY" ]]; then
    echo "Path: direct LAN route"
  else
    echo "Path: no PwnForge route is active (connect to Tailscale or the server's LAN)."
  fi
  echo "Persistent helper timer: $timer"
}

remove_root() {
  if [[ -r "$CONFIG" ]]; then
    # shellcheck disable=SC1090
    source "$CONFIG"
    local route_info dev via
    route_info="$(ip -4 route get "${CIDR%/*}" 2>/dev/null | head -n1 || true)"
    dev="$(route_value dev <<< "$route_info")"
    via="$(route_value via <<< "$route_info")"
    if [[ "$via" == "$GATEWAY" && "$dev" != tailscale* ]]; then
      ip -4 route del "$CIDR" via "$GATEWAY" dev "$dev" 2>/dev/null || true
    fi
  fi
  systemctl disable --now pwnforge-lab-route.timer >/dev/null 2>&1 || true
  systemctl stop pwnforge-lab-route.service >/dev/null 2>&1 || true
  rm -f "$SERVICE" "$TIMER" "$WORKER" "$CONFIG"
  rmdir /etc/pwnforge 2>/dev/null || true
  systemctl daemon-reload
  echo "Removed the PwnForge LAN helper and its route. Tailscale settings are unchanged."
}

if [[ "$#" -eq 0 ]]; then usage; exit 2; fi
case "$1" in
  install)
    [[ "$#" -eq 3 ]] || { usage; exit 2; }
    install_route "$2" "$3"
    ;;
  _install-root)
    [[ "$EUID" -eq 0 && "$#" -eq 3 ]] || exit 2
    install_root "$2" "$3"
    ;;
  status)
    [[ "$#" -eq 1 ]] || { usage; exit 2; }
    status
    ;;
  remove)
    [[ "$#" -eq 1 ]] || { usage; exit 2; }
    as_root "$0" _remove-root
    ;;
  _remove-root)
    [[ "$EUID" -eq 0 && "$#" -eq 1 ]] || exit 2
    remove_root
    ;;
  test)
    [[ "$#" -ge 2 && "$#" -le 3 ]] || { usage; exit 2; }
    valid_ipv4 "$2" || { echo "Invalid target IPv4 address." >&2; exit 2; }
    port=${3:-22}
    [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )) || { echo "Invalid TCP port." >&2; exit 2; }
    nc -w 2 -vz "$2" "$port"
    ;;
  *) usage; exit 2 ;;
esac
