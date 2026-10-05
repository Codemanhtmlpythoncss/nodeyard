# shellcheck shell=bash
# Host firewall: open the ports a feature needs on whichever firewall is
# active (ufw or firewalld). Custom nftables/iptables rulesets are never
# rewritten automatically; nodeyard lists what to allow instead.
# (A full reconcile restricted to the cluster subnet is part of the security
# phase; see docs/STATUS.md.)

ny_cmd "firewall status" firewall_status_cmd "Health" "Show the host firewall and its rules" firewall
ny_cmd "firewall open" firewall_open_cmd "Health" "Open the ports this node's k3s role needs" firewall
ny_cmd "firewall disable" firewall_disable_cmd "Health" "Turn the host firewall off entirely (not recommended)" firewall

# firewall_open_ports LABEL PORT/PROTO... -- allow ports on ufw/firewalld.
firewall_open_ports() {
    local label="$1"
    shift
    local fw p
    fw="$(ny_detect_firewall)"
    case "$fw" in
        none)
            ny_vlog "No active firewall; nothing to open for ${label}."
            ;;
        ufw)
            for p in "$@"; do
                ny_run_undoable ufw delete allow "$p" -- ufw allow "$p" comment "nodeyard ${label}" >/dev/null || true
            done
            ;;
        firewalld)
            for p in "$@"; do
                ny_run_undoable firewall-cmd --permanent --remove-port="${p/:/-}" -- firewall-cmd --permanent --add-port="${p/:/-}" >/dev/null || true
            done
            ny_run firewall-cmd --reload >/dev/null || true
            ;;
        nftables | iptables)
            ny_warn "${fw} has custom rules; nodeyard won't rewrite them. Allow these between your nodes: $(ny_join ', ' "$@")"
            ;;
    esac
    [[ "$fw" == none || "$fw" == nftables || "$fw" == iptables ]] || ny_ok "Firewall (${fw}) allows ${label}: $(ny_join ', ' "$@")"
}

# firewall_open_k3s ROLE -- server or agent.
firewall_open_k3s() {
    local role="${1:-server}"
    local -a ports=(6443/tcp 8472/udp 51820/udp 51821/udp 10250/tcp)
    [[ "$role" == server ]] && ports+=(2379:2380/tcp)
    firewall_open_ports "k3s ${role}" "${ports[@]}"
}

firewall_status_cmd_help() {
    cat <<'HELP'
Usage: nodeyard firewall status

Shows which firewall is active (ufw, firewalld, nftables, iptables or none)
and its current rules.
HELP
}

firewall_status_cmd() {
    ny_need_root
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    local fw
    fw="$(ny_detect_firewall)"
    printf 'Active firewall: %s\n' "$fw"
    case "$fw" in
        ufw) ufw status verbose 2>/dev/null || true ;;
        firewalld) firewall-cmd --list-all 2>/dev/null || true ;;
        nftables) nft list ruleset 2>/dev/null | head -n 80 || true ;;
        iptables) iptables -S 2>/dev/null || true ;;
    esac
}

firewall_open_cmd_help() {
    cat <<'HELP'
Usage: nodeyard firewall open

Opens the ports k3s needs for this node's role on ufw or firewalld:
  servers: 6443/tcp, 2379-2380/tcp, 8472/udp, 51820-51821/udp, 10250/tcp
  agents:  6443/tcp, 8472/udp, 51820-51821/udp, 10250/tcp
Each rule can be removed again with 'nodeyard undo'.
HELP
}

firewall_open_cmd() {
    ny_need_root
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    k3s_load_state
    local role="server"
    [[ "$K3S_ROLE" == agent ]] && role="agent"
    firewall_open_k3s "$role"
}

firewall_disable_cmd_help() {
    cat <<'HELP'
Usage: nodeyard firewall disable [--yes]

Turns the host firewall (ufw or firewalld) off completely. This lowers the
machine's security; prefer 'nodeyard firewall open'. Undo with
'nodeyard undo --last'.
HELP
}

firewall_disable_cmd() {
    ny_need_root
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    ny_warn "Turning the firewall off exposes every listening service on this machine to your network."
    ny_confirm "Disable the host firewall entirely?" n || {
        ny_info "Cancelled."
        return 0
    }
    local fw
    fw="$(ny_detect_firewall)"
    case "$fw" in
        ufw) ny_run_undoable ufw --force enable -- ufw disable ;;
        firewalld) ny_service_disable firewalld --now ;;
        *) ny_info "No ufw or firewalld firewall is active (found: ${fw})." ;;
    esac
}
