#!/bin/bash
# PwnForge on this machine, for the dashboard's PwnForge page (pwnforge.py). Runs as root through systemd-run, outside
# the dashboard's sandbox; the dashboard checks every argument before it calls this.
#
#   pwnforge-host.sh install   DIR DATA REPO HOST PORT   get or update the code, its Python and website, the service
#   pwnforge-host.sh configure DIR DATA HOST PORT        the address and port it listens on (a drop-in for the unit)
#   pwnforge-host.sh virt      DIR DATA HOST PORT        real lab machines: KVM/libvirt, firmware, base images
#   pwnforge-host.sh https     on|off PORT               an https:// address on the tailnet (Tailscale Serve)
#   pwnforge-host.sh tailnet-access on|off|status [CIDR] reach lab targets by IP from LAN and tailnet (no VM egress)
#
# Safe to run again: it never touches the database's users, progress or machines, and it never downloads OS images.
# The service runs as the unprivileged user "pwnforge" (in the libvirt and kvm groups when they exist), as PwnForge's
# own unit file says; nothing here turns off a host security setting.
set -euo pipefail
export LC_ALL=C.UTF-8 DEBIAN_FRONTEND=noninteractive

say() { printf 'pwnforge-host: %s\n' "$*"; }
fail() { say "FAILED: $*"; exit 1; }

UNIT_DIR=/etc/systemd/system
UNIT_NAME=pwnforge.service
DROPIN="$UNIT_DIR/pwnforge.service.d/nodeyard.conf"
SVC_USER=pwnforge

check_args() {
    local dir="$1" data="$2" host="$3" port="$4"
    [[ "$dir" =~ ^/[A-Za-z0-9._/-]+$ && "$dir" != *..* ]] || fail "bad code folder: $dir"
    [[ "$data" =~ ^/[A-Za-z0-9._/-]+$ && "$data" != *..* ]] || fail "bad data folder: $data"
    [[ "$host" =~ ^[0-9.]+$ || "$host" =~ ^[0-9a-fA-F:]+$ ]] || fail "bad address: $host"
    [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1024 && port <= 65535 )) || fail "bad port: $port"
}

write_dropin() {
    local dir="$1" data="$2" host="$3" port="$4" groups="" g
    for g in libvirt kvm; do
        if getent group "$g" >/dev/null; then groups+="${groups:+ }$g"; fi
    done
    install -d -m 0755 "$(dirname "$DROPIN")"
    cat > "$DROPIN.tmp" <<EOF
# Written by the nodeyard dashboard (PwnForge page): change it there.
# The address here wins over PwnForge's own "LAN access" setting.
[Service]
WorkingDirectory=$dir
Environment=PWNFORGE_DATA_DIR=$data
ReadWritePaths=$data
# Only the groups this machine has: the unit asks for libvirt and kvm, and a missing one stops it starting.
SupplementaryGroups=
SupplementaryGroups=$groups
ExecStart=
ExecStart=$dir/.venv/bin/pwnforge serve --host $host --port $port
EOF
    chmod 0644 "$DROPIN.tmp"
    if [[ -f "$DROPIN" ]] && cmp -s "$DROPIN" "$DROPIN.tmp"; then
        rm -f "$DROPIN.tmp"
        say "address unchanged ($host:$port)"
        return 1
    fi
    mv -f "$DROPIN.tmp" "$DROPIN"
    say "listens on $host:$port"
    return 0
}

need_packages() {
    local need=()
    command -v git >/dev/null 2>&1 || need+=(git)
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null \
        || fail "PwnForge needs Python 3.11 or newer; this machine has $(python3 --version 2>&1 || echo none)"
    python3 -c 'import ensurepip, venv' 2>/dev/null || need+=(python3-venv)
    command -v npm >/dev/null 2>&1 || need+=(nodejs npm)
    if (( ${#need[@]} )); then
        command -v apt-get >/dev/null 2>&1 || fail "missing ${need[*]}: install them with your package manager and try again"
        say "installing ${need[*]} (apt)"
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends "${need[@]}" >/dev/null
    fi
}

cmd_install() {
    local dir="$1" data="$2" repo="$3" host="$4" port="$5"
    check_args "$dir" "$data" "$host" "$port"
    [[ "$repo" =~ ^https://[A-Za-z0-9./_-]+$ ]] || fail "bad repository address"
    [[ $(id -u) == 0 ]] || fail "needs root"
    say "step 1/7: checking what this machine has"
    need_packages
    if [[ ! -e /dev/kvm ]]; then
        say "note: no /dev/kvm; real guests will use QEMU software emulation after real-machine setup (slower, never simulated)"
    fi

    say "step 2/7: service user"
    if ! id -u "$SVC_USER" >/dev/null 2>&1; then
        useradd --system --user-group --home-dir "$data" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
    fi
    local g
    for g in libvirt kvm; do
        if getent group "$g" >/dev/null; then usermod -aG "$g" "$SVC_USER"; fi
    done

    say "step 3/7: code ($repo)"
    if [[ -d "$dir/.git" ]]; then
        git -C "$dir" fetch -q --depth 1 origin HEAD
        git -C "$dir" reset -q --hard FETCH_HEAD
    elif [[ -e "$dir" && -n "$(ls -A "$dir" 2>/dev/null)" ]]; then
        fail "$dir exists and isn't a PwnForge checkout; pick another folder"
    else
        git clone -q --depth 1 "$repo" "$dir"
    fi
    [[ -f "$dir/backend/app/main.py" && -f "$dir/deploy/systemd/pwnforge.service" ]] || fail "$dir doesn't look like PwnForge"

    say "step 4/7: Python packages"
    [[ -x "$dir/.venv/bin/python" ]] || python3 -m venv "$dir/.venv"
    "$dir/.venv/bin/pip" install -q --upgrade pip
    "$dir/.venv/bin/pip" install -q -r "$dir/backend/requirements.txt"
    "$dir/.venv/bin/pip" install -q --no-deps -e "$dir"

    say "step 5/7: data folder and database"
    # data, images and overlays: 0751, so libvirt's QEMU user can reach a machine's disks (it can't list them);
    # everything else stays the service's own
    install -d -o "$SVC_USER" -g "$SVC_USER" -m 0751 "$data"
    for g in images overlays; do install -d -o "$SVC_USER" -g "$SVC_USER" -m 0751 "$data/$g"; done
    for g in iso snapshots backups logs; do install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 "$data/$g"; done
    (cd "$dir" && runuser -u "$SVC_USER" -- env PWNFORGE_DATA_DIR="$data" "$dir/.venv/bin/alembic" upgrade head)
    (cd "$dir" && runuser -u "$SVC_USER" -- env PWNFORGE_DATA_DIR="$data" "$dir/.venv/bin/python" -c \
        'from backend.app.database.init_db import seed; print("catalogue:", seed(include_profiles=True))')
    if [[ -f "$data/pwnforge.db" ]]; then chmod 0640 "$data/pwnforge.db"; fi
    link_libvirt "$dir" || true      # (keeps real machines working after an update)

    say "step 6/7: website"
    # An older npm (Debian's 10) won't "npm ci" from a lock file a newer npm wrote, so fall back to "npm install".
    if (cd "$dir/frontend" && { npm ci --no-audit --no-fund --loglevel=error >/dev/null 2>&1 \
            || npm install --no-audit --no-fund --loglevel=error >/dev/null; } && npm run -s build >/dev/null); then
        say "website built"
    else
        say "warning: the website didn't build; the API still runs (see npm's messages above)"
    fi

    say "step 7/7: service"
    install -m 0644 "$dir/deploy/systemd/pwnforge.service" "$UNIT_DIR/pwnforge.service"
    write_dropin "$dir" "$data" "$host" "$port" || true
    systemctl daemon-reload

    # Real machines, automatically: PwnForge disables simulated labs, so on a host that can run real guests
    # (apt-based, x86_64/arm64) set them up now. Idempotent — present images aren't re-downloaded — and
    # best-effort, so a host that can't do it still finishes install (labs show how to enable real machines).
    case "$(uname -m)" in
        x86_64 | amd64 | aarch64 | arm64)
            if command -v apt-get >/dev/null 2>&1; then
                say "setting up real machines automatically"
                if ( cmd_virt "$dir" "$data" "$host" "$port" ); then
                    say "real machines ready"
                else
                    say "note: couldn't finish real-machine setup automatically; open the PwnForge page and press 'Set up real machines' to retry"
                fi
            else
                say "note: not an apt host, so real machines aren't set up automatically; install QEMU and libvirt, then use 'Set up real machines'"
            fi
            ;;
        *) say "note: $(uname -m) can't run PwnForge's real machines (needs x86_64 or arm64)" ;;
    esac
    say "OK"
}

# libvirt's Python binding from the distro, linked into PwnForge's own Python (made from the same python3), when
# the distro package is installed. Returns 1 when it isn't.
link_libvirt() {
    local dir="$1" site f found=1
    site="$("$dir/.venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" || return 1
    for f in /usr/lib/python3/dist-packages/libvirt*.py /usr/lib/python3/dist-packages/libvirtmod*.so; do
        if [[ -e "$f" ]]; then ln -sfn "$f" "$site/$(basename "$f")"; found=0; fi
    done
    return "$found"
}

cmd_virt() {
    local dir="$1" data="$2" host="$3" port="$4" arch k
    check_args "$dir" "$data" "$host" "$port"
    [[ $(id -u) == 0 ]] || fail "needs root"
    [[ -x "$dir/.venv/bin/pwnforge" ]] || fail "PwnForge isn't installed in $dir: install it first"
    command -v apt-get >/dev/null 2>&1 || fail "real machines are set up here with apt (Debian, Ubuntu, Raspberry Pi OS); install QEMU and libvirt yourself, then run this again"
    arch="$(uname -m)"
    local pkgs=(qemu-utils libvirt-daemon-system libvirt-clients python3-libvirt ipxe-qemu)
    local keys=()
    case "$arch" in
        aarch64 | arm64) pkgs+=(qemu-system-arm qemu-efi-aarch64); keys=(debian-13-base-arm64 alpine-base-arm64 ubuntu-2404-base-arm64) ;;
        x86_64 | amd64) pkgs+=(qemu-system-x86); keys=(debian-13-base alpine-base ubuntu-2404-base) ;;
        *) fail "real machines need an x86_64 or arm64 CPU; this one is $arch" ;;
    esac

    say "step 1/5: QEMU, libvirt and firmware (apt)"
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends "${pkgs[@]}" >/dev/null
    systemctl enable --now libvirtd.service >/dev/null 2>&1 || systemctl enable --now virtqemud.socket >/dev/null 2>&1 || true

    say "step 2/5: PwnForge's access to libvirt"
    for k in libvirt kvm; do
        if getent group "$k" >/dev/null; then usermod -aG "$k" "$SVC_USER"; fi
    done
    link_libvirt "$dir" || fail "python3-libvirt didn't install its files where expected"
    runuser -u "$SVC_USER" -- "$dir/.venv/bin/python" -c 'import libvirt; c = libvirt.open("qemu:///system"); print("pwnforge-host: libvirt", c.getLibVersion())' \
        || fail "PwnForge's user can't reach libvirt (qemu:///system)"

    say "step 3/5: folders libvirt's QEMU can reach"
    chmod 0751 "$data" "$data/images" "$data/overlays"
    if [[ -f "$data/pwnforge.db" ]]; then chmod 0640 "$data/pwnforge.db"; fi

    say "step 4/5: base images for this CPU (${keys[*]})"
    for k in "${keys[@]}"; do
        if ! (cd "$dir" && runuser -u "$SVC_USER" -- env PWNFORGE_DATA_DIR="$data" "$dir/.venv/bin/pwnforge" images fetch "$k"); then
            say "warning: couldn't download $k (labs that need it won't start until it's fetched)"
        fi
    done

    say "step 5/5: service"
    write_dropin "$dir" "$data" "$host" "$port" || true
    systemctl daemon-reload
    systemctl try-restart "$UNIT_NAME" || true
    if [[ -e /dev/kvm ]]; then
        say "real machines: on (KVM accelerated; the first start of each kind of machine prepares its image once)"
    else
        say "real machines: on (QEMU TCG software emulation; real guests, slower without KVM)"
    fi
    say "OK"
}

# Ports the dashboard keeps for itself on Tailscale Serve: its own https:// (443) and public access (Funnel: 8443, 10000).
cmd_https() {
    local action="$1" port="$2"
    [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1024 && port <= 65535 )) || fail "bad port: $port"
    case "$port" in 8443 | 10000) fail "port $port is kept for the dashboard's public access: pick another" ;; esac
    [[ $(id -u) == 0 ]] || fail "needs root"
    command -v tailscale >/dev/null 2>&1 || fail "Tailscale isn't installed on this machine"
    if [[ "$action" == off ]]; then
        timeout 30 tailscale serve --https="$port" off >/dev/null 2>&1 || true
        say "https:// address on port $port: off"
        say "OK"
        return 0
    fi
    [[ "$action" == on ]] || fail "say on or off"
    # (an older tailscale waits for you to allow Serve in the admin console: don't hang here)
    if ! timeout 60 tailscale serve --bg --https="$port" "http://127.0.0.1:$port" >/dev/null 2>&1; then
        fail "Tailscale didn't set up the https:// address: turn on MagicDNS and HTTPS Certificates for your tailnet (https://login.tailscale.com/admin/dns), then try again"
    fi
    say "https:// address on port $port: on (your tailnet only)"
    say "OK"
}

cmd_configure() {
    local dir="$1" data="$2" host="$3" port="$4"
    check_args "$dir" "$data" "$host" "$port"
    [[ -f "$UNIT_DIR/pwnforge.service" ]] || fail "PwnForge's service isn't installed"
    if write_dropin "$dir" "$data" "$host" "$port"; then
        systemctl daemon-reload
        if systemctl -q is-active pwnforge.service; then systemctl restart pwnforge.service; say "restarted on the new address"; fi
    fi
    say "OK"
}

# Reach lab target VMs directly from the LAN and tailnet (so you can nmap/ssh them by IP), while the VMs stay unable
# to reach the internet or each other's networks. Enables forwarding, advertises the lab supernet over Tailscale
# (you approve the route once in the admin console), and installs narrow firewall rules. A boot unit reapplies them.
# Current advertised routes with $1 added (op=add) or removed (op=del), preserving everything else (the exit-node
# default routes in particular). Prints a comma-separated list (possibly empty).
ts_routes() {
    local op="$1" cidr="$2"
    tailscale debug prefs 2>/dev/null | CIDR="$cidr" OP="$op" python3 -c '
import json, os, sys
cur = (json.load(sys.stdin).get("AdvertiseRoutes") or [])
cidr, op = os.environ["CIDR"], os.environ["OP"]
cur = [r for r in cur if r != cidr]
if op == "add":
    cur.append(cidr)
print(",".join(cur))
'
}

TAILNET_RULES=/usr/local/sbin/pwnforge-tailnet-rules.sh
TAILNET_UNIT="$UNIT_DIR/pwnforge-tailnet.service"
TAILNET_SYSCTL=/etc/sysctl.d/99-pwnforge-tailnet.conf
TAILNET_STATE=/var/lib/pwnforge-tailnet

private_lab_cidr() {
    python3 - "$1" <<'PY'
import ipaddress, sys
try:
    network = ipaddress.ip_network(sys.argv[1], strict=False)
except ValueError:
    raise SystemExit(1)
if network.version != 4 or not network.is_private or network.prefixlen < 8 or network.prefixlen > 30:
    raise SystemExit(1)
print(network)
PY
}

lan_interface() {
    # The main-table default route remains the physical LAN even when Tailscale provides an exit node.
    ip -4 route show default 2>/dev/null | awk '{for (i=1; i<NF; i++) if ($i=="dev" && $(i+1)!="tailscale0" && $(i+1)!="lo") {print $(i+1); exit}}'
}

lan_address() {
    local iface="$1"
    [[ -n "$iface" ]] || return 0
    ip -4 -o addr show dev "$iface" scope global 2>/dev/null | awk 'NR==1 {sub(/\/.*/, "", $4); print $4}'
}

write_tailnet_rules() {
    local cidr="$1"
    cat > "$TAILNET_RULES" <<EOF
#!/bin/sh
# Written by the nodeyard dashboard (PwnForge lab access). Lets your tailnet AND the host's local network reach lab
# targets in $cidr, and nothing else; lab VMs cannot start connections out (no internet, no other labs). Idempotent.
set -e
CIDR="$cidr"
CH=PWNFORGE-TAILNET
NAT=PWNFORGE-TAILNET-NAT
# the host's local-network interface (main-table default route), so LAN devices can reach labs too
LAN=\$(ip -4 route show default 2>/dev/null | awk '{for (i=1; i<NF; i++) if (\$i=="dev" && \$(i+1)!="tailscale0" && \$(i+1)!="lo") {print \$(i+1); exit}}')
iptables -N \$CH 2>/dev/null || iptables -F \$CH
iptables -A \$CH -i tailscale0 -d \$CIDR -j ACCEPT
if [ -n "\$LAN" ]; then iptables -A \$CH -i "\$LAN" -d \$CIDR -j ACCEPT; fi
iptables -A \$CH -s \$CIDR -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A \$CH -s \$CIDR -j DROP
iptables -A \$CH -d \$CIDR -j DROP
iptables -A \$CH -j RETURN
# jump from FORWARD, first, exactly once
while iptables -C FORWARD -j \$CH 2>/dev/null; do iptables -D FORWARD -j \$CH; done
iptables -I FORWARD 1 -j \$CH
# Lab guests have no default route. SNAT incoming connections to the host's bridge
# address so guest replies return through conntrack; the filter above still
# blocks new connections initiated by guests.
iptables -t nat -N \$NAT 2>/dev/null || iptables -t nat -F \$NAT
iptables -t nat -A \$NAT -d \$CIDR ! -s \$CIDR -j MASQUERADE
while iptables -t nat -C POSTROUTING -j \$NAT 2>/dev/null; do iptables -t nat -D POSTROUTING -j \$NAT; done
iptables -t nat -I POSTROUTING 1 -j \$NAT
EOF
    chmod 0755 "$TAILNET_RULES"
}

clear_tailnet_rules() {
    local cidr="${1:-10.200.0.0/16}"
    while iptables -C FORWARD -j PWNFORGE-TAILNET 2>/dev/null; do iptables -D FORWARD -j PWNFORGE-TAILNET; done
    iptables -F PWNFORGE-TAILNET 2>/dev/null || true
    iptables -X PWNFORGE-TAILNET 2>/dev/null || true
    while iptables -t nat -C POSTROUTING -j PWNFORGE-TAILNET-NAT 2>/dev/null; do iptables -t nat -D POSTROUTING -j PWNFORGE-TAILNET-NAT; done
    iptables -t nat -F PWNFORGE-TAILNET-NAT 2>/dev/null || true
    iptables -t nat -X PWNFORGE-TAILNET-NAT 2>/dev/null || true
    # Remove any duplicate direct MASQUERADE rules used by an earlier helper version.
    while iptables -t nat -C POSTROUTING -d "$cidr" ! -s "$cidr" -j MASQUERADE 2>/dev/null; do
        iptables -t nat -D POSTROUTING -d "$cidr" ! -s "$cidr" -j MASQUERADE
    done
    rm -f "$TAILNET_RULES"
    systemctl disable --now pwnforge-tailnet.service >/dev/null 2>&1 || true
    rm -f "$TAILNET_UNIT"
    systemctl daemon-reload || true
}

cmd_tailnet_status() {
    local cidr="$1" iface lanip advertised=no firewall=no return_path_nat=no boot=no forwarding=no route_state
    if [[ -r /proc/sys/net/ipv4/ip_forward ]] && [[ $(cat /proc/sys/net/ipv4/ip_forward) == 1 ]]; then forwarding=yes; fi
    if iptables -C FORWARD -j PWNFORGE-TAILNET 2>/dev/null &&
        iptables -C PWNFORGE-TAILNET -i tailscale0 -d "$cidr" -j ACCEPT 2>/dev/null &&
        iptables -C PWNFORGE-TAILNET -s "$cidr" -j DROP 2>/dev/null; then firewall=yes; fi
    if iptables -t nat -C POSTROUTING -j PWNFORGE-TAILNET-NAT 2>/dev/null &&
        iptables -t nat -C PWNFORGE-TAILNET-NAT -d "$cidr" ! -s "$cidr" -j MASQUERADE 2>/dev/null; then return_path_nat=yes; fi
    if systemctl is-enabled --quiet pwnforge-tailnet.service 2>/dev/null; then boot=yes; fi
    if command -v tailscale >/dev/null 2>&1; then
        route_state="$(tailscale debug prefs 2>/dev/null | CIDR="$cidr" python3 -c 'import json,os,sys; print("yes" if os.environ["CIDR"] in (json.load(sys.stdin).get("AdvertiseRoutes") or []) else "no")' 2>/dev/null || echo no)"
        [[ "$route_state" == yes ]] && advertised=yes
    fi
    iface="$(lan_interface)"
    lanip="$(lan_address "$iface")"
    [[ -n "$iface" ]] || iface=unavailable
    [[ -n "$lanip" ]] || lanip=unavailable
    local client_command="unavailable"
    if [[ "$lanip" != unavailable ]]; then client_command="pwnforge install $cidr $lanip"; fi
    printf 'cidr=%s\nipv4_forwarding=%s\nfirewall=%s\nreturn_path_nat=%s\nboot_persistence=%s\ntailnet_route_advertised=%s\nlan_interface=%s\nlan_address=%s\nclient_command=%s\n' \
        "$cidr" "$forwarding" "$firewall" "$return_path_nat" "$boot" "$advertised" "$iface" "$lanip" "$client_command"
}

cmd_tailnet_access() {
    local action="$1" cidr="${2:-}"
    [[ -n "$cidr" ]] || cidr="10.200.0.0/16"
    [[ $(id -u) == 0 ]] || fail "needs root"
    case "$action" in on|off|status) ;; *) fail "say on, off or status" ;; esac
    cidr="$(private_lab_cidr "$cidr")" || fail "lab subnet must be a valid private IPv4 CIDR (e.g. 10.200.0.0/16)"
    if [[ "$action" == status ]]; then cmd_tailnet_status "$cidr"; return 0; fi
    command -v iptables >/dev/null 2>&1 || fail "iptables isn't available on this machine"
    [[ "$action" != on ]] || command -v tailscale >/dev/null 2>&1 || fail "Tailscale isn't installed on this machine"

    if [[ "$action" == off ]]; then
        clear_tailnet_rules "$cidr"
        if command -v tailscale >/dev/null 2>&1; then
            local routes; routes="$(ts_routes del "$cidr")"
            timeout 20 tailscale set --advertise-routes="$routes" >/dev/null 2>&1 || fail "firewall is disabled, but Tailscale route withdrawal failed; remove $cidr from the device's advertised routes"
        else
            say "warning: Tailscale isn't installed, so remove the stale $cidr route from the admin console if it is still listed"
        fi
        if [[ -r "$TAILNET_STATE/forwarding.previous" ]]; then
            local previous; previous="$(cat "$TAILNET_STATE/forwarding.previous")"
            if [[ -w /proc/sys/net/ipv4/ip_forward ]]; then echo "$previous" > /proc/sys/net/ipv4/ip_forward; fi
        fi
        rm -f "$TAILNET_SYSCTL"
        rm -rf "$TAILNET_STATE"
        say "lab access disabled"
        say "OK"
        return 0
    fi

    say "step 1/4: IPv4 forwarding"
    install -d -m 0755 "$TAILNET_STATE"
    if [[ ! -e "$TAILNET_STATE/forwarding.previous" ]]; then cat /proc/sys/net/ipv4/ip_forward > "$TAILNET_STATE/forwarding.previous"; fi
    echo 1 > /proc/sys/net/ipv4/ip_forward
    echo "net.ipv4.ip_forward=1" > "$TAILNET_SYSCTL"

    say "step 2/4: firewall (tailnet and LAN to $cidr; no VM egress)"
    write_tailnet_rules "$cidr"
    sh "$TAILNET_RULES"

    say "step 3/4: a boot unit so the rules survive a reboot"
    cat > "$TAILNET_UNIT" <<EOF
[Unit]
Description=PwnForge lab tailnet access firewall rules
After=tailscaled.service network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=$TAILNET_RULES
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now pwnforge-tailnet.service >/dev/null 2>&1 || fail "couldn't enable the persistent lab firewall unit"

    say "step 4/4: advertise the lab subnet over Tailscale (keeping other routes)"
    local routes; routes="$(ts_routes add "$cidr")"
    if ! timeout 30 tailscale set --advertise-routes="$routes" >/dev/null 2>&1; then
        fail "firewall and forwarding are enabled, but Tailscale route advertisement failed; check tailscale status"
    fi
    local lanif lanip
    lanif="$(lan_interface)"
    lanip="$(lan_address "$lanif")"
    [[ -n "$lanif" ]] || lanif=unavailable
    [[ -n "$lanip" ]] || lanip='<this-host-LAN-IP>'
    say "lab access: on (tailnet + local network)."
    say "  Tailnet: approve route $cidr at https://login.tailscale.com/admin/machines. macOS accepts approved routes automatically; Linux may need: sudo tailscale set --accept-routes=true"
    say "  Same LAN only: use this host's LAN address ($lanip), never its 100.x Tailscale address, as the gateway."
    say "    macOS temporary route: sudo route -n add -net $cidr $lanip"
    say "    Linux temporary route: sudo ip route add $cidr via $lanip"
    say "    For a persistent LAN fallback, download the Mac/Linux helper and run: pwnforge install $cidr $lanip"
    say "  LAN interface: $lanif; check with: pwnforge-host.sh tailnet-access status $cidr"
    say "  Then reach a lab target by its IP (ssh/nmap)."
    say "OK"
}

case "${1:-}" in
    install) shift; (( $# == 5 )) || fail "usage: install DIR DATA REPO HOST PORT"; cmd_install "$@" ;;
    configure) shift; (( $# == 4 )) || fail "usage: configure DIR DATA HOST PORT"; cmd_configure "$@" ;;
    virt) shift; (( $# == 4 )) || fail "usage: virt DIR DATA HOST PORT"; cmd_virt "$@" ;;
    https) shift; (( $# == 2 )) || fail "usage: https on|off PORT"; cmd_https "$@" ;;
    tailnet-access) shift; (( $# >= 1 && $# <= 2 )) || fail "usage: tailnet-access on|off|status [CIDR]"; cmd_tailnet_access "$@" ;;
    *) fail "usage: $0 install|configure|virt|https|tailnet-access ..." ;;
esac
