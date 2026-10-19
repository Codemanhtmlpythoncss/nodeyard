#!/bin/bash
# Persistent direct access from a Mac to PwnForge's private lab subnet.
# Install this file as `pwnforge` with pwnforge-install-macos.sh, then use:
#   pwnforge install 10.200.0.0/16 192.168.1.123
#   pwnforge status
#   pwnforge remove
#   pwnforge test 10.200.0.20 22
# PWNFORGE_LAB_ACCESS_CLI=macOS
#
# macOS accepts approved Tailscale subnet routes automatically. This helper adds a
# launchd-managed LAN route when the Mac is on the same LAN, and yields to a route
# that Tailscale has installed through utun. It does not configure the server.
set -euo pipefail

PLIST=/Library/LaunchDaemons/com.pwnforge.lab-route.plist
WORKER=/usr/local/libexec/pwnforge-lab-route
CONFIG="/Library/Application Support/PwnForge/lab-route.conf"
LABEL=com.pwnforge.lab-route

usage() {
  cat <<'USAGE'
PwnForge macOS lab route helper

  install <lab-CIDR> <server-LAN-IP>  Install a persistent LAN fallback route
  status                              Show the current route and launchd status
  remove                              Remove this helper and its LAN route
  test <target-IP> [port]             Test a TCP port (default: 22)

Example:
  pwnforge install 10.200.0.0/16 192.168.1.123

Approved Tailscale subnet routes are picked up automatically by macOS.
USAGE
}

valid_ipv4() {
  awk -F. 'NF != 4 {exit 1} {for (i=1;i<=4;i++) if ($i !~ /^[0-9]+$/ || $i < 0 || $i > 255) exit 1}' <<< "$1"
}

valid_private_cidr() {
  local ip prefix
  ip="$(cut -d/ -f1 <<< "$1")"
  prefix="$(cut -s -d/ -f2 <<< "$1")"
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

route_field() {
  awk -F': *' -v key="$1" '$1 ~ key {print $2; exit}'
}

ipv4_to_int() {
  awk -F. 'NF != 4 {exit 1} {n=0; for (i=1;i<=4;i++) {if ($i !~ /^[0-9]+$/ || $i < 0 || $i > 255) exit 1; n=n*256+$i} printf "%.0f", n}' <<< "$1"
}

same_lan() {
  local iface="$1" info local_ip mask mask_hex local_num gateway_num mask_num
  info="$(/sbin/ifconfig "$iface" 2>/dev/null || true)"
  local_ip="$(awk '/inet / && $2 != "127.0.0.1" {print $2; exit}' <<< "$info")"
  mask="$(awk '/inet / {for (i=1; i<=NF; i++) if ($i=="netmask") {print $(i+1); exit}}' <<< "$info")"
  [[ -n "$local_ip" && "$mask" == 0x* ]] || return 1
  mask_hex="${mask#0x}"
  local_num="$(ipv4_to_int "$local_ip")" || return 1
  gateway_num="$(ipv4_to_int "$GATEWAY")" || return 1
  mask_num=$((16#$mask_hex))
  (( (local_num & mask_num) == (gateway_num & mask_num) ))
}

install_helper() {
  local cidr="$1" gateway="$2"
  valid_private_cidr "$cidr" || { echo "Use a canonical private IPv4 network, such as 10.200.0.0/16." >&2; exit 2; }
  valid_ipv4 "$gateway" || { echo "The server LAN gateway must be an IPv4 address." >&2; exit 2; }
  if [[ "$EUID" -ne 0 ]]; then exec sudo /bin/bash "$0" install "$cidr" "$gateway"; fi

  install -d -o root -g wheel -m 0755 /usr/local/libexec "$(/usr/bin/dirname "$CONFIG")"
  printf 'CIDR=%s\nGATEWAY=%s\n' "$cidr" "$gateway" > "$CONFIG.tmp"
  chown root:wheel "$CONFIG.tmp"
  # The config contains only a private CIDR and the host's LAN address; status
  # should work as the regular user, while the root-owned directory protects it.
  chmod 0644 "$CONFIG.tmp"
  mv -f "$CONFIG.tmp" "$CONFIG"

  cat > "$WORKER" <<'WORKER_EOF'
#!/bin/bash
set -u
CONFIG="/Library/Application Support/PwnForge/lab-route.conf"
route_field() { awk -F': *' -v key="$1" '$1 ~ key {print $2; exit}'; }
[[ -r "$CONFIG" ]] || exit 0
source "$CONFIG"
PROBE="$(cut -d/ -f1 <<< "$CIDR")"
route_info="$(/sbin/route -n get -inet "$PROBE" 2>/dev/null || true)"
route_iface="$(route_field interface <<< "$route_info")"
route_gateway="$(route_field gateway <<< "$route_info")"

# Tailscale owns an active utun route when the approved subnet route is available.
# Leave it alone; the launchd job adds the LAN fallback when Tailscale disconnects.
if [[ "$route_iface" == utun* ]]; then exit 0; fi
default_info="$(/sbin/route -n get -inet default 2>/dev/null || true)"
default_gateway="$(route_field gateway <<< "$default_info")"
default_iface="$(route_field interface <<< "$default_info")"
host_info="$(/sbin/route -n get -inet "$GATEWAY" 2>/dev/null || true)"
host_iface="$(route_field interface <<< "$host_info")"

if [[ "$route_gateway" == "$GATEWAY" ]]; then
  if [[ "$host_iface" == "$default_iface" ]] && same_lan "$default_iface" && /usr/sbin/arp -n "$GATEWAY" 2>/dev/null | /usr/bin/grep -Eq ' at [[:xdigit:]]{1,2}(:[[:xdigit:]]{1,2}){5} '; then exit 0; fi
  # The Mac left that LAN; remove only our own stale route so an approved utun route can take over.
  /sbin/route -n delete -inet -net "$CIDR" "$GATEWAY" >/dev/null 2>&1 || true
  exit 0
fi

# Don't replace another specific route or try to install a LAN route while away from that LAN.
if [[ -n "$route_gateway" && "$route_gateway" != "$default_gateway" ]]; then exit 0; fi
if [[ -z "$default_iface" || "$host_iface" != "$default_iface" ]] || ! same_lan "$default_iface"; then exit 0; fi
if ! /usr/sbin/arp -n "$GATEWAY" 2>/dev/null | /usr/bin/grep -Eq ' at [[:xdigit:]]{1,2}(:[[:xdigit:]]{1,2}){5} '; then exit 0; fi
/sbin/route -n add -inet -net "$CIDR" "$GATEWAY" >/dev/null 2>&1 || true
WORKER_EOF
  chown root:wheel "$WORKER"
  chmod 0755 "$WORKER"

  cat > "$PLIST" <<'PLIST_EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.pwnforge.lab-route</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>/usr/local/libexec/pwnforge-lab-route</string></array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>/var/log/pwnforge-lab-route.log</string>
  <key>StandardErrorPath</key><string>/var/log/pwnforge-lab-route.log</string>
</dict></plist>
PLIST_EOF
  chown root:wheel "$PLIST"
  chmod 0644 "$PLIST"
  /bin/launchctl bootout system "$PLIST" >/dev/null 2>&1 || true
  /bin/launchctl bootstrap system "$PLIST"
  /bin/launchctl kickstart -k "system/$LABEL"
  echo "Installed persistent route helper for $cidr via $gateway."
  echo "It uses an approved Tailscale route when macOS has one, and the LAN route when this Mac is on that LAN."
  echo "Check with: $0 status"
}

show_status() {
  local cidr gateway probe route_info iface next_hop
  if [[ ! -r "$CONFIG" ]]; then
    echo "PwnForge LAN route helper is not installed."
    return 0
  fi
  source "$CONFIG"
  cidr="$CIDR"; gateway="$GATEWAY"; probe="$(cut -d/ -f1 <<< "$CIDR")"
  route_info="$(/sbin/route -n get -inet "$probe" 2>/dev/null || true)"
  iface="$(route_field interface <<< "$route_info")"
  next_hop="$(route_field gateway <<< "$route_info")"
  [[ -n "$iface" ]] || iface=unavailable
  [[ -n "$next_hop" ]] || next_hop=unavailable
  echo "Lab network: $cidr"
  echo "LAN gateway: $gateway"
  echo "Current route: interface=$iface, next-hop=$next_hop"
  if [[ "$iface" == utun* ]]; then
    echo "Path: Tailscale subnet route"
  elif [[ "$next_hop" == "$gateway" ]]; then
    echo "Path: direct LAN route"
  else
    echo "Path: no PwnForge route is active (connect to Tailscale or the server's LAN)."
  fi
  if /bin/launchctl print "system/$LABEL" >/dev/null 2>&1; then echo "Persistent helper: loaded"; else echo "Persistent helper: not loaded"; fi
}

remove_helper() {
  if [[ "$EUID" -ne 0 ]]; then exec sudo /bin/bash "$0" remove; fi
  if [[ -r "$CONFIG" ]]; then
    source "$CONFIG"
    probe="$(cut -d/ -f1 <<< "$CIDR")"
    route_info="$(/sbin/route -n get -inet "$probe" 2>/dev/null || true)"
    if [[ "$(route_field gateway <<< "$route_info")" == "$GATEWAY" ]]; then
      /sbin/route -n delete -inet -net "$CIDR" "$GATEWAY" >/dev/null 2>&1 || true
    fi
  fi
  /bin/launchctl bootout system "$PLIST" >/dev/null 2>&1 || true
  rm -f "$PLIST" "$WORKER" "$CONFIG" "$CONFIG.tmp"
  rmdir "$(/usr/bin/dirname "$CONFIG")" >/dev/null 2>&1 || true
  echo "Removed the PwnForge LAN helper and its route. Tailscale settings are unchanged."
}

if [[ "$#" -eq 0 ]]; then usage; exit 2; fi
case "$1" in
  install)
    [[ "$#" -eq 3 ]] || { usage; exit 2; }
    install_helper "$2" "$3"
    ;;
  status)
    [[ "$#" -eq 1 ]] || { usage; exit 2; }
    show_status
    ;;
  remove)
    [[ "$#" -eq 1 ]] || { usage; exit 2; }
    remove_helper
    ;;
  test)
    [[ "$#" -ge 2 && "$#" -le 3 ]] || { usage; exit 2; }
    valid_ipv4 "$2" || { echo "Invalid target IPv4 address." >&2; exit 2; }
    port=22
    if [[ "$#" -eq 3 ]]; then port="$3"; fi
    [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )) || { echo "Invalid TCP port." >&2; exit 2; }
    /usr/bin/nc -G 2 -vz "$2" "$port"
    ;;
  *)
    usage
    exit 2
    ;;
esac
