# shellcheck shell=bash
# shellcheck disable=SC2034 # globals here are read by other files
# Host preparation for k3s: packages, kernel modules, sysctls, swap, time
# sync, the memory cgroup, and the fixes for things that commonly break k3s
# on homelab machines (ported from k3s-manager 3.2).

HOST_MODULES_FILE="/etc/modules-load.d/nodeyard.conf"
HOST_SYSCTL_FILE="/etc/sysctl.d/90-nodeyard.conf"
HOST_EVICTION_DROPIN="/etc/rancher/k3s/config.yaml.d/60-nodeyard-eviction.yaml"
HOST_PODFWD_SCRIPT="/usr/local/sbin/nodeyard-allow-pod-forwarding"
HOST_PODFWD_UNIT="/etc/systemd/system/nodeyard-podforward.service"
HOST_PODFWD_MINIUPNPD="/etc/systemd/system/miniupnpd.service.d/nodeyard-pod-forwarding.conf"

# host_install_prereqs -- packages and kernel settings k3s needs.
host_install_prereqs() {
    ny_need_root
    ny_detect_all
    ny_info "Detected ${NY_OS_LABEL} (${NY_ARCH}, package manager: ${NY_PKG:-none}, init: ${NY_INIT})"
    [[ -n "$NY_PKG" ]] || ny_die "No supported package manager was found." \
        "nodeyard supports apt, dnf, zypper and pacman; see docs/distros-and-hardware.md." "$NY_E_PRECONDITION"
    [[ "$NY_INIT" != unknown ]] || ny_die "k3s needs systemd (or OpenRC), and neither was detected." "" "$NY_E_PRECONDITION"
    if [[ "$NY_OS_SUPPORT" == unsupported ]]; then
        ny_warn "${NY_OS_LABEL} is not supported. ${NY_OS_NOTE}"
    elif [[ "$NY_OS_SUPPORT" == best-effort && -n "$NY_OS_NOTE" ]]; then
        ny_warn "$NY_OS_NOTE"
    fi

    ny_step "Installing prerequisites"
    case "$NY_PKG" in
        apt) ny_pkg_install curl ca-certificates open-iscsi nfs-common apparmor apparmor-utils conntrack socat iptables ||
            ny_warn "Some prerequisite packages failed to install." ;;
        dnf | yum)
            ny_pkg_install curl ca-certificates nfs-utils socat conntrack-tools iscsi-initiator-utils iptables ||
                ny_warn "Some prerequisite packages failed to install."
            if have iscsiadm || ny_simulating; then ny_service_enable iscsid --now || true; fi
            ;;
        zypper) ny_pkg_install curl ca-certificates nfs-client socat conntrack-tools open-iscsi iptables ||
            ny_warn "Some prerequisite packages failed to install." ;;
        # No iptables on Arch: it conflicts with the default iptables-nft, and
        # k3s ships its own iptables binaries.
        pacman) ny_pkg_install curl ca-certificates nfs-utils socat conntrack-tools open-iscsi ||
            ny_warn "Some prerequisite packages failed to install." ;;
        apk) ny_pkg_install curl ca-certificates nfs-utils socat conntrack-tools open-iscsi iptables ||
            ny_warn "Some prerequisite packages failed to install." ;;
    esac

    host_fix_kernel_modules
    host_fix_sysctl
    host_check_swap
    host_check_time_sync
}

host_fix_kernel_modules() {
    local mod
    for mod in overlay br_netfilter; do
        if ! grep -q "^${mod} " <<<"$(lsmod 2>/dev/null || true)"; then
            ny_run modprobe "$mod" 2>/dev/null || ny_warn "Could not load kernel module '${mod}' (it may be built into the kernel, which is fine)."
        fi
    done
    printf 'overlay\nbr_netfilter\n' | ny_write_file "$HOST_MODULES_FILE"
}

host_fix_sysctl() {
    ny_write_file "$HOST_SYSCTL_FILE" <<'SYSCTL'
# Written by nodeyard: settings k3s networking needs.
net.ipv4.ip_forward = 1
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
SYSCTL
    ny_run sysctl --system >/dev/null 2>&1 || ny_warn "sysctl --system reported problems; some settings may not have applied."
}

# k3s runs the kubelet with fail-swap-on=false, so swap is fine. It is never
# turned off just because --yes was passed: on a laptop that breaks
# hibernation. Only an explicit "yes" from a person disables it.
host_check_swap() {
    local swap_on
    swap_on="$(swapon --noheadings 2>/dev/null || true)"
    [[ -n "$swap_on" ]] || return 0
    ny_info "Swap is on. k3s runs fine with swap, so it is left alone."
    if [[ "$NY_YES" -eq 0 ]] && ny_ui_interactive && [[ "$NY_DRY_RUN" -eq 0 ]] &&
        ny_ui_yesno "Turn swap off anyway and comment it out of /etc/fstab?" n; then
        ny_run swapoff -a || ny_warn "swapoff failed."
        local fstab
        fstab="$(ny_path /etc/fstab)"
        if [[ -f "$fstab" ]]; then
            sed -E 's/^([^#].*[[:space:]]swap[[:space:]].*)$/#\1/' "$fstab" | ny_write_file /etc/fstab
        fi
        ny_ok "Swap disabled (undo with: nodeyard undo --last)."
    fi
    return 0
}

host_time_sync_active() {
    local svc
    for svc in systemd-timesyncd chronyd chrony ntpd ntpsec openntpd; do
        systemctl is-active --quiet "$svc" 2>/dev/null && return 0
    done
    return 1
}

host_check_time_sync() {
    host_time_sync_active && return 0
    ny_warn "No time-sync service is running (systemd-timesyncd, chrony or ntpd). Clock drift breaks TLS between nodes."
    ny_hint "Fix: sudo nodeyard doctor --fix (installs and starts one)"
    return 0
}

# --- port clashes and other services on the same host --------------------------

# host_port_in_use PORT -- something already listens on this TCP port.
host_port_in_use() {
    local port="$1"
    if have ss; then
        grep -qE "[.:]${port}\$" <<<"$(ss -ltn 2>/dev/null | awk 'NR>1{print $4}' || true)"
    elif have netstat; then
        grep -qE "[.:]${port}\$" <<<"$(netstat -ltn 2>/dev/null | awk 'NR>2{print $4}' || true)"
    else
        ny_tcp_check 127.0.0.1 "$port" 2
    fi
}

# host_detect_nextcloud -- how Nextcloud is installed here, if it is.
host_detect_nextcloud() {
    local how=""
    if have snap && grep -qi '^nextcloud ' <<<"$(snap list 2>/dev/null || true)"; then
        how="snap package 'nextcloud'"
    fi
    if have docker && grep -qi nextcloud <<<"$(docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null || true)"; then
        how="${how:+$how, }a running Docker container"
    fi
    if [[ -d "$(ny_path /var/www/nextcloud)" || -d "$(ny_path /var/www/html/nextcloud)" ]]; then
        how="${how:+$how, }a web root at /var/www*/nextcloud"
    fi
    if grep -qi nextcloud <<<"$(systemctl list-units --type=service --all 2>/dev/null || true)"; then
        how="${how:+$how, }a systemd service"
    fi
    [[ -n "$how" ]] || return 1
    printf '%s\n' "$how"
}

# host_preflight_ports KEEP_INGRESS -- k3s's bundled Traefik + ServiceLB bind
# 80/443 on every node. If something already uses them, sets
# HOST_DISABLE_INGRESS=1 (unless --keep-ingress was given).
HOST_DISABLE_INGRESS=0
host_preflight_ports() {
    local keep="${1:-0}" nc_how
    nc_how="$(host_detect_nextcloud || true)"
    [[ -n "$nc_how" ]] && ny_warn "Detected Nextcloud on this host (${nc_how})."
    local -a busy=()
    host_port_in_use 80 && busy+=(80)
    host_port_in_use 443 && busy+=(443)
    [[ "${#busy[@]}" -gt 0 ]] || return 0
    ny_warn "Port $(ny_join '/' "${busy[@]}") is already in use on this host."
    ny_info "k3s installs Traefik and ServiceLB by default, which bind ports 80/443 on EVERY node and would clash with it."
    if [[ "$keep" -eq 1 ]]; then
        ny_warn "Keeping them anyway because --keep-ingress was given. Expect a port 80/443 conflict."
    else
        ny_info "Disabling Traefik and ServiceLB for this install to avoid the clash (pass --keep-ingress to override)."
        HOST_DISABLE_INGRESS=1
    fi
    return 0
}

host_forward_policy_blocking() {
    have iptables || return 1
    local policy
    policy="$(iptables -S FORWARD 2>/dev/null | awk '/^-P FORWARD/{print $3}' || true)"
    [[ "$policy" == DROP || "$policy" == REJECT ]]
}

# Docker (e.g. Nextcloud AIO) and k3s/flannel both need FORWARD to pass
# traffic. A DROP policy is a classic "worked until I installed the other
# one" breakage. Only the rule order changes; the policy is left as it is.
host_fix_forward_policy() {
    host_forward_policy_blocking || return 0
    ny_run_undoable iptables -D FORWARD -j ACCEPT -- iptables -I FORWARD -j ACCEPT
    ny_ok "Inserted an ACCEPT rule at the top of the FORWARD chain (the policy itself is unchanged)."
}

# kubelet needs the memory cgroup controller. Raspberry Pi OS and some
# minimal ARM images ship with it disabled, and k3s then fails at once.
host_memory_cgroup_available() {
    [[ -d "$(ny_path /sys/fs/cgroup/memory)" ]] && return 0
    grep -qw memory "$(ny_path /sys/fs/cgroup/cgroup.controllers)" 2>/dev/null && return 0
    return 1
}

# Edits the kernel command line; this needs a reboot, and says so.
host_fix_memory_cgroup() {
    local params="cgroup_memory=1 cgroup_enable=memory"
    local cmdline
    cmdline="$(cat "$(ny_path /proc/cmdline)" 2>/dev/null || true)"
    ny_info "Current kernel command line: ${cmdline}"
    if grep -qw 'cgroup_memory=1' <<<"$cmdline"; then
        ny_warn "The running kernel already has cgroup_memory=1 but the controller is still missing. Look for 'cgroup_disable=memory' later on the line, or a kernel built without CONFIG_MEMCG."
        return 1
    fi
    local f file=""
    for f in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
        if [[ -f "$(ny_path "$f")" ]]; then
            file="$f"
            break
        fi
    done
    if [[ -n "$file" ]]; then
        if grep -q 'cgroup_memory=1' "$(ny_path "$file")"; then
            ny_warn "${file} already has cgroup_memory=1; it takes effect after a reboot: sudo reboot"
            return 0
        fi
        # The Pi bootloader reads only the FIRST line of cmdline.txt.
        sed "1 s/\$/ ${params}/" "$(ny_path "$file")" | ny_write_file "$file"
        ny_ok "Added '${params}' to ${file} (undo with: nodeyard undo --last)."
        ny_warn "A REBOOT is needed for this to take effect: sudo reboot"
        return 0
    fi
    if [[ -f "$(ny_path /etc/default/grub)" ]] && have update-grub; then
        if grep -q 'cgroup_memory=1' "$(ny_path /etc/default/grub)"; then
            ny_warn "/etc/default/grub already has cgroup_memory=1; run 'sudo update-grub' if you haven't, then reboot."
            return 0
        fi
        sed "s/^GRUB_CMDLINE_LINUX=\"\(.*\)\"/GRUB_CMDLINE_LINUX=\"\1 ${params}\"/" "$(ny_path /etc/default/grub)" |
            ny_write_file /etc/default/grub
        ny_run update-grub
        ny_ok "Added '${params}' to GRUB_CMDLINE_LINUX and ran update-grub."
        ny_warn "A REBOOT is needed for this to take effect: sudo reboot"
        return 0
    fi
    ny_warn "Couldn't find /boot/firmware/cmdline.txt, /boot/cmdline.txt or GRUB to edit."
    ny_hint "Add '${params}' to your bootloader's kernel command line yourself, then reboot."
    return 1
}

# nftables runs EVERY table's forward hook and a drop in any of them wins, so
# e.g. miniupnpd's "inet filter" silently drops all k3s pod traffic even
# though k3s/flannel accept it. Lists such chains as "family<TAB>table<TAB>chain".
host_foreign_forward_drop_chains() {
    have nft || return 0
    { nft list ruleset 2>/dev/null || true; } | awk '
        /^table / {fam=$2; tbl=$3}
        /^[ \t]*chain / {ch=$2}
        /hook forward/ && /policy drop/ {
            if (fam != "ip6" && !(fam == "ip" && tbl == "filter")) printf "%s\t%s\t%s\n", fam, tbl, ch
        }'
    return 0
}

host_pod_forward_unpatched() {
    local fam tbl ch out
    while IFS=$'\t' read -r fam tbl ch; do
        [[ -n "$fam" ]] || continue
        out="$(nft list chain "$fam" "$tbl" "$ch" 2>/dev/null || true)"
        grep -q 'k3s pod network' <<<"$out" || return 0
    done < <(host_foreign_forward_drop_chains)
    return 1
}

host_install_pod_forward_fix() {
    ny_write_file "$HOST_PODFWD_SCRIPT" 0755 <<'SCRIPT'
#!/bin/sh
# Installed by nodeyard (doctor --fix). nftables evaluates every table's
# forward hook and a drop in ANY of them wins, so a table not managed by
# iptables with "policy drop" on forward (e.g. miniupnpd's "inet filter")
# silently drops all k3s pod traffic. This lets ONLY the pod network through
# each such chain; nothing else changes. Idempotent.
# Undo: sudo nodeyard undo --feature host
command -v nft >/dev/null 2>&1 || exit 0
CIDR=$(sed -n 's/^FLANNEL_NETWORK=//p' /run/flannel/subnet.env 2>/dev/null)
[ -n "$CIDR" ] || CIDR=10.42.0.0/16
nft list ruleset 2>/dev/null | awk '/^table /{f=$2;t=$3} /^[ \t]*chain /{c=$2} /hook forward/ && /policy drop/ { if (f!="ip6" && !(f=="ip" && t=="filter")) print f, t, c }' |
while read -r f t c; do
    nft list chain "$f" "$t" "$c" 2>/dev/null | grep -q 'k3s pod network' && continue
    nft insert rule "$f" "$t" "$c" ip daddr "$CIDR" accept comment '"k3s pod network"'
    nft insert rule "$f" "$t" "$c" ip saddr "$CIDR" accept comment '"k3s pod network"'
done
exit 0
SCRIPT
    ny_write_file "$HOST_PODFWD_UNIT" <<UNIT
[Unit]
Description=nodeyard: let k3s pod traffic through nftables forward chains that drop by default
After=network-online.target nftables.service ufw.service firewalld.service miniupnpd.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=${HOST_PODFWD_SCRIPT}

[Install]
WantedBy=multi-user.target
UNIT
    # miniupnpd recreates its table whenever it restarts: re-apply after it.
    if systemctl cat miniupnpd.service >/dev/null 2>&1; then
        printf '[Service]\nExecStartPost=%s\n' "$HOST_PODFWD_SCRIPT" | ny_write_file "$HOST_PODFWD_MINIUPNPD"
    fi
    ny_service_daemon_reload
    ny_service_enable nodeyard-podforward.service
    ny_run "$HOST_PODFWD_SCRIPT"
}

# k3s evicts pods when a disk is 95% full, then keeps evicting until 15% is
# free. On a big disk shared with personal files (a laptop) that can be tens
# of GB of someone else's data, so the node stays locked out. Reclaim 2GiB.
host_write_eviction_config() {
    ny_write_file "$HOST_EVICTION_DROPIN" <<'YAML'
# nodeyard: after a low-disk eviction only reclaim 2GiB (k3s default: 10% of the disk)
kubelet-arg+:
  - eviction-minimum-reclaim=imagefs.available=2Gi,nodefs.available=2Gi
YAML
}

host_eviction_lockout_risk() {
    [[ -f "$(ny_path "$HOST_EVICTION_DROPIN")" ]] && return 1
    local pct
    pct="$(df -P "$(ny_path /var/lib)" 2>/dev/null | awk 'NR == 2 {sub("%", "", $5); print 100 - $5}')"
    [[ "$pct" =~ ^[0-9]+$ ]] || return 1
    ((pct < 20))
}

host_install_eviction_fix() {
    host_write_eviction_config
    ny_run systemctl restart "$(k3s_service_name)"
}
