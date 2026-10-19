#!/bin/bash
# PwnForge on this machine, for the dashboard's PwnForge page (pwnforge.py). Runs as root through systemd-run, outside
# the dashboard's sandbox; the dashboard checks every argument before it calls this.
#
#   pwnforge-host.sh install   DIR DATA REPO HOST PORT   get or update the code, its Python and website, the service
#   pwnforge-host.sh configure DIR DATA HOST PORT        the address and port it listens on (a drop-in for the unit)
#   pwnforge-host.sh virt      DIR DATA HOST PORT        real lab machines: KVM/libvirt, firmware, base images
#   pwnforge-host.sh https     on|off PORT               an https:// address on the tailnet (Tailscale Serve)
#   pwnforge-host.sh tailnet-access on|off [CIDR]        reach lab targets by IP from your tailnet (no VM egress)
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

# Reach lab target VMs directly from the tailnet (so you can nmap/ssh them by IP), while the VMs stay unable to
# reach the internet or each other's networks. Does three things: enables IPv4 forwarding, advertises the lab
# supernet over Tailscale (you approve the route once in the admin console), and installs narrow firewall rules
# that permit ONLY tailscale0 <-> lab-subnet forwarding. A boot unit re-applies the rules.
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

write_tailnet_rules() {
    local cidr="$1"
    cat > "$TAILNET_RULES" <<EOF
#!/bin/sh
# Written by the nodeyard dashboard (PwnForge tailnet access). Allows your tailnet to reach lab targets in $cidr,
# and nothing else; lab VMs cannot start connections out (no internet, no other labs). Idempotent.
set -e
CIDR="$cidr"
CH=PWNFORGE-TAILNET
iptables -N \$CH 2>/dev/null || iptables -F \$CH
iptables -A \$CH -i tailscale0 -d \$CIDR -j ACCEPT
iptables -A \$CH -s \$CIDR -o tailscale0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A \$CH -s \$CIDR ! -o tailscale0 -j DROP
iptables -A \$CH -j RETURN
# jump from FORWARD, first, exactly once
iptables -C FORWARD -j \$CH 2>/dev/null && iptables -D FORWARD -j \$CH 2>/dev/null || true
iptables -I FORWARD 1 -j \$CH
EOF
    chmod 0755 "$TAILNET_RULES"
}

clear_tailnet_rules() {
    iptables -D FORWARD -j PWNFORGE-TAILNET 2>/dev/null || true
    iptables -F PWNFORGE-TAILNET 2>/dev/null || true
    iptables -X PWNFORGE-TAILNET 2>/dev/null || true
    rm -f "$TAILNET_RULES"
    systemctl disable --now pwnforge-tailnet.service >/dev/null 2>&1 || true
    rm -f "$TAILNET_UNIT"
    systemctl daemon-reload || true
}

cmd_tailnet_access() {
    local action="$1" cidr="${2:-10.200.0.0/16}"
    [[ $(id -u) == 0 ]] || fail "needs root"
    case "$action" in on|off) ;; *) fail "say on or off" ;; esac
    [[ "$cidr" =~ ^10\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ || "$cidr" =~ ^172\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ || "$cidr" =~ ^192\.168\.[0-9]+\.[0-9]+/[0-9]+$ ]] \
        || fail "lab subnet must be a private CIDR (e.g. 10.200.0.0/16)"
    command -v tailscale >/dev/null 2>&1 || fail "Tailscale isn't installed on this machine"
    command -v iptables >/dev/null 2>&1 || fail "iptables isn't available on this machine"

    if [[ "$action" == off ]]; then
        clear_tailnet_rules
        local routes; routes="$(ts_routes del "$cidr")"
        timeout 20 tailscale set --advertise-routes="$routes" >/dev/null 2>&1 || true
        say "tailnet access to lab targets: off (other advertised routes kept)"
        say "OK"
        return 0
    fi

    say "step 1/4: IPv4 forwarding"
    echo 1 > /proc/sys/net/ipv4/ip_forward
    echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-pwnforge-tailnet.conf

    say "step 2/4: firewall (tailnet <-> $cidr only; no VM egress)"
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
    systemctl enable --now pwnforge-tailnet.service >/dev/null 2>&1 || true

    say "step 4/4: advertise the lab subnet over Tailscale (keeping your existing routes, e.g. the exit node)"
    local routes; routes="$(ts_routes add "$cidr")"
    if ! timeout 30 tailscale set --advertise-routes="$routes" >/dev/null 2>&1; then
        say "warning: couldn't advertise the route automatically; run: tailscale set --advertise-routes=$routes"
    fi
    say "tailnet access: on. APPROVE the route $cidr for this machine at https://login.tailscale.com/admin/machines"
    say "then on your device: tailscale up --accept-routes, and you can reach a lab target by its IP (ssh/nmap)."
    say "OK"
}

case "${1:-}" in
    install) shift; (( $# == 5 )) || fail "usage: install DIR DATA REPO HOST PORT"; cmd_install "$@" ;;
    configure) shift; (( $# == 4 )) || fail "usage: configure DIR DATA HOST PORT"; cmd_configure "$@" ;;
    virt) shift; (( $# == 4 )) || fail "usage: virt DIR DATA HOST PORT"; cmd_virt "$@" ;;
    https) shift; (( $# == 2 )) || fail "usage: https on|off PORT"; cmd_https "$@" ;;
    tailnet-access) shift; (( $# >= 1 && $# <= 2 )) || fail "usage: tailnet-access on|off [CIDR]"; cmd_tailnet_access "$@" ;;
    *) fail "usage: $0 install|configure|virt|https|tailnet-access ..." ;;
esac
