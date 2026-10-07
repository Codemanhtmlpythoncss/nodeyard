# shellcheck shell=bash
# shellcheck disable=SC2034 # globals here are read by other files
# k3s on this machine: install (first server, extra server, agent), join
# info, status, service control, logs, upgrade, kubeconfig and uninstall.
# Ported from k3s-manager 3.2; every 3.2 command and flag still works.

ny_cmd "install master" k3s_install_server_cmd "Cluster" "Install this machine as the first k3s server (control plane)" k3s
ny_cmd "install join-master" k3s_install_join_server_cmd "Cluster" "Join this machine to a cluster as an additional server" k3s
ny_cmd "install worker" k3s_install_agent_cmd "Cluster" "Join this machine to a cluster as a worker (agent)" k3s
ny_cmd_alias "install agent" "install worker"
ny_cmd_alias "install server" "install master"
ny_cmd "token" k3s_token_cmd "Cluster" "Show how to join other machines to this cluster" k3s json
ny_cmd_alias "join-info" "token"
ny_cmd "status" k3s_status_cmd "Cluster" "Show this node's role, k3s service and cluster nodes" k3s json
ny_cmd "list-nodes" k3s_list_nodes_cmd "Nodes" "List the cluster's nodes" k3s json
ny_cmd_alias "get-nodes" "list-nodes"
ny_cmd "start" k3s_start_cmd "Cluster" "Start k3s on this node" k3s
ny_cmd "stop" k3s_stop_cmd "Cluster" "Stop k3s on this node" k3s
ny_cmd "restart" k3s_restart_cmd "Cluster" "Restart k3s on this node" k3s
ny_cmd "enable-boot" k3s_enable_boot_cmd "Cluster" "Start k3s automatically at boot" k3s
ny_cmd "disable-boot" k3s_disable_boot_cmd "Cluster" "Don't start k3s at boot" k3s
ny_cmd "logs" k3s_logs_cmd "Health" "Show k3s service logs for this node" k3s
ny_cmd "upgrade" k3s_upgrade_cmd "Updates" "Upgrade k3s on this node" k3s
ny_cmd "kubeconfig" k3s_kubeconfig_cmd "Cluster" "Set up kubectl access to the cluster" k3s
ny_cmd "uninstall k3s" k3s_uninstall_cmd "Settings" "Remove k3s from this node and undo nodeyard's k3s changes" k3s

K3S_ROLE="" K3S_IFACE="" K3S_NODE_IP="" K3S_SERVER_URL="" K3S_CHANNEL="" K3S_TLS_SAN=""
K3S_INSTALLER_URL="https://get.k3s.io"

# k3s_load_state -- what this node is, from the config (or from what the k3s
# installer left on disk, if the config doesn't say).
k3s_load_state() {
    local self
    self="$(ny_self_name)"
    K3S_ROLE="$(ny_role_normalize "$(ny_cfg_get node "$self" role)")"
    K3S_IFACE="$(ny_cfg_get node "$self" interface)"
    K3S_NODE_IP="$(ny_cfg_get node "$self" node-ip)"
    K3S_SERVER_URL="$(ny_cfg_get node "$self" server)"
    K3S_CHANNEL="$(ny_cfg_get cluster "" k3s-channel)"
    K3S_TLS_SAN="$(ny_cfg_get_list node "$self" tls-san | paste -sd, -)"
    if [[ -z "$K3S_ROLE" || "$K3S_ROLE" == standalone ]]; then
        if [[ -f "$(ny_path /etc/systemd/system/k3s-agent.service)" || -x "$(ny_path /usr/local/bin/k3s-agent-uninstall.sh)" ]]; then
            K3S_ROLE="agent"
        elif [[ -f "$(ny_path /etc/systemd/system/k3s.service)" || -x "$(ny_path /usr/local/bin/k3s-uninstall.sh)" ]]; then
            K3S_ROLE="server"
        fi
    fi
    local envf
    envf="$(ny_path /etc/systemd/system/k3s-agent.service.env)"
    if [[ "$K3S_ROLE" == agent && -z "$K3S_SERVER_URL" && -r "$envf" ]]; then
        K3S_SERVER_URL="$(sed -n "s/^K3S_URL=['\"]\{0,1\}\([^'\"]*\)['\"]\{0,1\}$/\1/p" "$envf" | head -n1 || true)"
    fi
    return 0
}

# k3s_save_state ROLE IFACE NODE_IP SERVER_URL [CHANNEL] [TLS_SANS]
# Saved BEFORE installing, so 'doctor'/'status' know what this node is meant
# to be even if the k3s service then fails to start.
k3s_save_state() {
    local self
    self="$(ny_self_name)"
    ny_cfg_batch_begin
    ny_cfg_set node "$self" role "$1"
    if [[ -n "$2" ]]; then ny_cfg_set node "$self" interface "$2"; else ny_cfg_unset node "$self" interface; fi
    [[ -n "$3" ]] && ny_cfg_set node "$self" node-ip "$3"
    if [[ -n "$4" ]]; then ny_cfg_set node "$self" server "$4"; else ny_cfg_unset node "$self" server; fi
    [[ -n "${5:-}" ]] && ny_cfg_set cluster "" k3s-channel "$5"
    [[ -n "${6:-}" ]] && ny_cfg_set node "$self" tls-san "$6"
    [[ -n "${7:-}" ]] && ny_cfg_set node "$self" init "$7"
    [[ -n "${8:-}" ]] && ny_cfg_set node "$self" allow-workloads "$8"
    ny_cfg_batch_commit
}

# k3s_guard_role WANTED -- refuse to turn an existing server into an agent
# (or the reverse): k3s keeps different state for each, so that needs a
# clean uninstall first.
k3s_guard_role() {
    local want="$1"
    ny_k3s_installed || return 0
    [[ -n "$K3S_ROLE" && "$K3S_ROLE" != "$want" ]] || return 0
    local -A names=([server]="server" [agent]="worker")
    ny_die "This machine is already a k3s ${names[$K3S_ROLE]:-$K3S_ROLE}; it can't be turned into a ${names[$want]:-$want} in place." \
        "Remove k3s first (sudo nodeyard uninstall k3s), then run this again." "$NY_E_PRECONDITION"
}

k3s_service_name() {
    [[ -n "$K3S_ROLE" ]] || k3s_load_state
    if [[ "$K3S_ROLE" == agent ]]; then echo k3s-agent; else echo k3s; fi
}

k3s_version() {
    "$(ny_path "$NY_K3S_BIN")" --version 2>/dev/null | awk '/k3s version/{print $3; exit}' || true
}

# k3s_validate_interface IFACE -- prints its IPv4; brings it up if needed.
k3s_validate_interface() {
    local iface="$1" state ip
    ny_valid_iface "$iface" || ny_usage_error "$NY_VALID_MSG"
    ny_iface_exists "$iface" ||
        ny_die "Network interface '${iface}' does not exist." "List interfaces with: nodeyard network-info" "$NY_E_USAGE"
    state="$(ny_iface_state "$iface")"
    if [[ "$state" != up && "$state" != unknown ]]; then
        ny_warn "Interface '${iface}' is ${state}; bringing it up."
        ny_run ip link set dev "$iface" up || true
        ny_simulating || sleep 1
    fi
    ip="$(ny_iface_ipv4 "$iface")"
    [[ -n "$ip" ]] || ny_die "Network interface '${iface}' has no IPv4 address." \
        "Give it an address first (e.g. with your network settings or nmcli), then try again."
    printf '%s\n' "$ip"
}

# k3s_network_args IFACE MODE -- --node-ip/--flannel-iface (+ --advertise-address
# for servers; 'k3s agent' has no such flag and refuses to start with it).
k3s_network_args() {
    local iface="$1" mode="${2:-server}" ip
    K3S_NET_ARGS=()
    [[ -n "$iface" ]] || return 0
    ip="$(k3s_validate_interface "$iface")"
    K3S_NET_ARGS=(--node-ip "$ip" --flannel-iface "$iface")
    [[ "$mode" == server ]] && K3S_NET_ARGS+=(--advertise-address "$ip")
    return 0
}

ny_tailscale_ip() {
    have tailscale || return 1
    tailscale ip -4 2>/dev/null | head -n1
}

# k3s_tls_san_args CSV AUTO -- explicit SANs, plus hostname and Tailscale IP.
k3s_tls_san_args() {
    local csv="$1" auto="${2:-1}" v
    K3S_SAN_ARGS=()
    local -a sans=()
    while IFS= read -r v; do
        [[ -n "$v" ]] && sans+=("$v")
    done < <(ny_csv_split "$csv")
    if [[ "$auto" -eq 1 ]]; then
        sans+=("$(hostname -f 2>/dev/null || hostname)")
        v="$(ny_tailscale_ip || true)"
        [[ -n "$v" ]] && sans+=("$v")
    fi
    local -a seen=()
    for v in "${sans[@]+"${sans[@]}"}"; do
        [[ -n "$v" ]] || continue
        ny_in_list "$v" "${seen[@]+"${seen[@]}"}" && continue
        seen+=("$v")
        K3S_SAN_ARGS+=(--tls-san "$v")
    done
    return 0
}

# k3s_store_token VALUE|"" FILE -- keep the join token in nodeyard's secret
# store (0600) and set K3S_TOKEN_PATH to where k3s should read it from
# (empty if no token was given). Runs in the main shell so the step shows
# up in --dry-run plans.
k3s_store_token() {
    local value="$1" file="$2"
    K3S_TOKEN_PATH=""
    if [[ -n "$file" ]]; then
        [[ -r "$file" ]] || ny_die "Cannot read the token file ${file}." "Check the path and permissions (it should be readable by root only)." "$NY_E_USAGE"
        value="$(<"$file")"
        value="$(ny_trim "$value")"
    fi
    [[ -n "$value" ]] || return 0
    printf '%s' "$value" | ny_secret_set k3s-token
    K3S_TOKEN_PATH="$(ny_secret_path k3s-token)"
}

# k3s_read_token_stdin -- the join token from the first line of stdin.
k3s_read_token_stdin() {
    local t=""
    IFS= read -r t || true
    t="$(ny_trim "$t")"
    [[ -n "$t" ]] || ny_usage_error "--token-stdin was given but no token arrived on standard input."
    ny_secret_register "$t"
    printf '%s' "$t"
}

# k3s_run_installer ENV_ASSIGNMENT... -- run the official k3s installer.
k3s_run_installer() {
    host_write_eviction_config
    local tmp
    tmp="$(ny_mktemp)"
    ny_step "Running the official k3s installer (${K3S_INSTALLER_URL})"
    ny_download "$K3S_INSTALLER_URL" "$tmp"
    # The installer only installs; nodeyard starts the service itself so that
    # if it won't start, it can read the log and explain why.
    ny_run env INSTALL_K3S_SKIP_START=true "$@" sh "$tmp" ||
        ny_die "The k3s installer failed." "See the output above, then run this command again (it is safe to repeat). 'sudo nodeyard doctor' checks the usual problems."
}

# k3s_start_service SERVICE -- enable and (re)start a k3s service. If it won't
# start or stay up, show why in plain English and stop.
k3s_start_service() {
    local svc="$1"
    if [[ "$NY_INIT" == systemd ]]; then
        ny_run systemctl enable "$svc" >/dev/null 2>&1 || true
        if ! ny_run systemctl restart "$svc"; then
            # k3s often fails its very first start for a moment (for example
            # while the cluster still lists this node under its old address)
            # and systemd restarts it by itself. Give it time before judging.
            ny_warn "${svc} did not start on the first try. systemd retries it, so waiting up to 90 seconds before giving up..."
            if ny_wait_service "$svc" 45; then
                ny_ok "${svc} is running (it came up on a retry)."
                return 0
            fi
            k3s_diagnose "$svc"
            ny_die "${svc}.service failed to start." "Read the cause above and fix it, then run the same command again (it is safe to repeat)."
        fi
    fi
    if ! ny_wait_service "$svc"; then
        k3s_diagnose "$svc"
        ny_die "${svc}.service did not stay running." "Read the cause above and fix it, then run the same command again (it is safe to repeat)."
    fi
    return 0
}

# k3s_diagnose SERVICE -- show the log lines that explain a failed start, then
# name the likely cause(s) and the fix. Everything shown is redacted.
k3s_diagnose() {
    local svc="$1" log="" shown line
    if have journalctl; then
        log="$(journalctl -u "$svc" -n 80 --no-pager 2>/dev/null || true)"
    fi
    if [[ -z "$log" && -r "$NY_LOG_FILE" ]]; then
        log="$(tail -n 40 "$NY_LOG_FILE" 2>/dev/null || true)"
    fi
    printf '\n' >&2
    ny_err "${svc}.service would not start. Its log says:"
    # The decisive lines first; only if there are none, anything error-like.
    shown="$(grep -E 'level=(error|fatal)|Shutdown request|Failed with result|Failed to start|Job for' <<<"$log" | tail -n 8 || true)"
    [[ -n "$shown" ]] || shown="$(grep -iE 'error|fatal|failed|refused|denied|rejected|unauthorized|x509|nm-cloud|exec format|no such|not found|unable|cannot' <<<"$log" | tail -n 8 || true)"
    [[ -n "$shown" ]] || shown="$(tail -n 8 <<<"$log")"
    while IFS= read -r line; do
        [[ -n "$line" ]] && printf '    %s\n' "$(ny_redact "${line:0:230}")" >&2
    done <<<"$shown"

    local found=0 server="${K3S_J_SERVER:-${K3S_SERVER_URL:-}}"
    k3s_cause() { # PATTERN CAUSE FIX...
        local pat="$1" cause="$2"
        shift 2
        grep -qiE "$pat" <<<"$log" || return 0
        found=1
        printf '\n' >&2
        ny_warn "Likely cause: ${cause}"
        local fix
        for fix in "$@"; do ny_hint "$fix"; done
        return 0
    }
    k3s_cause 'failed to find interface with specified node ip' \
        "the cluster has this node registered under a different address (its IP changed, or another machine had the same name)." \
        "This normally fixes itself within a minute as k3s updates the node: check with 'sudo nodeyard status' on a server." \
        "If it doesn't: on a server run 'sudo nodeyard remove-node NAME', then add this machine again." \
        "If two machines share a hostname, give one a new name first (sudo hostnamectl set-hostname NEW-NAME)."
    k3s_cause 'nm-cloud-setup' "NetworkManager's cloud-setup service is enabled, and k3s refuses to run beside it." \
        "Fix: sudo systemctl disable --now nm-cloud-setup.service nm-cloud-setup.timer" "then reboot, and run the command again."
    k3s_cause 'failed to find memory cgroup|memory cgroup (is |was )?(not|disabled|missing)|cgroup_memory=1|cgroup_enable=memory' "the kernel's memory cgroup is switched off (common on Raspberry Pi OS)." \
        "Fix: sudo nodeyard doctor --fix   (edits the boot command line)" "then: sudo reboot, and run the command again."
    k3s_cause 'node password rejected|duplicate hostname|password.*(does not match|rejected)' "the server already has a node with this machine's name, registered with a different password." \
        "Fix: give this machine a unique hostname: sudo hostnamectl set-hostname NEW-NAME" \
        "or, on a server, remove the old node: sudo nodeyard remove-node NAME   (and here: sudo rm /etc/rancher/node/password)"
    k3s_cause 'unauthorized|invalid (cluster )?token|bad (cluster )?token' "the server did not accept the join token." \
        "Fix: get the current token on a server (sudo nodeyard token --reveal) and use exactly that."
    k3s_cause 'failed to get CA certs|connection refused|no route to host|i/o timeout|dial tcp|network is unreachable|no such host|context deadline exceeded' "this machine can't reach the k3s server${server:+ at ${server}}." \
        "Fix: on the server, check the firewall allows 6443/tcp: sudo nodeyard firewall status" \
        "and from here: curl -k ${server:-https://SERVER:6443}/ping   (should print 'pong')" \
        "Also check both machines are on the same network and the server is running: sudo nodeyard status"
    k3s_cause 'address already in use' "another program is already using a port k3s needs (6443, 10250...)." \
        "Fix: find it with: sudo ss -ltnp | grep -E ':(6443|10250|2379|2380) '"
    k3s_cause 'flannel.*(interface|iface|not found|no such)|(interface|iface).*not found|could not find (the )?(interface|iface)' "k3s could not use the network interface it was given." \
        "Fix: list this machine's interfaces with: ip -br addr" "then run the command again with --interface set to the wired one."
    k3s_cause 'x509|certificate has expired|not yet valid|clock (skew|drift)' "a certificate problem, usually because this machine's clock is wrong." \
        "Fix: sudo nodeyard doctor --fix   (turns on time sync), check with: timedatectl"
    k3s_cause '(iptables|ip6tables|nftables|nf_tables).*(not found|failed|error|unable|no such|cannot)' "a firewall tool or kernel module k3s needs is missing or failing." \
        "Fix: install iptables (or nftables) with your package manager, then: sudo nodeyard doctor --fix"
    k3s_cause 'exec format error' "the k3s binary does not match this machine's CPU." \
        "Fix: remove it and install again: sudo nodeyard uninstall k3s   (check the CPU with: uname -m)"
    k3s_cause 'flag provided but not defined|unknown flag|unknown shorthand' "k3s rejected one of the options nodeyard gave it." \
        "Fix: please report this at https://github.com/${NY_REPO}/issues with the lines above."
    if [[ "$found" -eq 0 ]]; then
        printf '\n' >&2
        ny_warn "No known cause matched. The log lines above are the reason."
    fi
    printf '\n' >&2
    ny_hint "Full log:  sudo journalctl -u ${svc} -n 100 --no-pager"
    ny_hint "Checks:    sudo nodeyard doctor"
    ny_hint "Start over on this machine (removes k3s): sudo nodeyard uninstall k3s"
    return 0
}

k3s_install_server_cmd_help() {
    cat <<'HELP'
Usage: nodeyard install master [options]

Installs k3s on this machine as the first server (control plane).

Options:
  --ha                     Use embedded etcd so more servers can join later
  --worker                 Also run workloads on this server (good for small clusters)
  --interface IFACE        Pin cluster traffic to this network interface (e.g. eth0)
  --channel C              k3s release channel: stable, latest or testing
  --version V              Exact k3s version, e.g. v1.33.4+k3s1
  --tls-san HOST           Extra name/address for the API certificate (repeatable)
  --no-auto-tls-san        Don't add this host's name and Tailscale address
  --disable NAME           Disable a bundled component, e.g. traefik (repeatable)
  --keep-ingress           Keep Traefik/ServiceLB even if ports 80/443 are taken
  --cluster-cidr CIDR      Pod network (default 10.42.0.0/16)
  --service-cidr CIDR      Service network (default 10.43.0.0/16)
  --datastore-endpoint DSN External datastore instead of SQLite/etcd
  --node-label K=V         Kubernetes label for this node (repeatable)
  --node-taint K=V:Effect  Kubernetes taint for this node (repeatable)
  --token-file PATH        Use the cluster token in PATH (otherwise k3s makes one)
  --token TOKEN            Same, on the command line (visible in shell history: prefer --token-file)

Example:
  sudo nodeyard install master --ha --worker --interface eth0
HELP
}

k3s_install_server_cmd() {
    ny_need_root
    local cluster_init=0 allow=0 keep_ingress=0 token="" token_file="" iface="" channel="" version=""
    local san_csv="" no_auto_san=0 cluster_cidr="" service_cidr="" datastore=""
    local -a disable=() labels=() taints=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --ha | --cluster-init)
                cluster_init=1
                shift
                ;;
            --worker | --allow-workloads)
                allow=1
                shift
                ;;
            --token)
                ny_need_value "$1" $#
                token="$2"
                ny_secret_register "$2"
                shift 2
                ;;
            --token-file)
                ny_need_value "$1" $#
                token_file="$2"
                shift 2
                ;;
            --token-stdin)
                token="$(k3s_read_token_stdin)"
                shift
                ;;
            --interface)
                ny_need_value "$1" $#
                iface="$2"
                shift 2
                ;;
            --channel)
                ny_need_value "$1" $#
                channel="$2"
                shift 2
                ;;
            --version)
                ny_need_value "$1" $#
                version="$2"
                shift 2
                ;;
            --tls-san)
                ny_need_value "$1" $#
                san_csv+="${san_csv:+,}$2"
                shift 2
                ;;
            --no-auto-tls-san)
                no_auto_san=1
                shift
                ;;
            --disable)
                ny_need_value "$1" $#
                disable+=(--disable "$2")
                shift 2
                ;;
            --keep-ingress)
                keep_ingress=1
                shift
                ;;
            --cluster-cidr)
                ny_need_value "$1" $#
                cluster_cidr="$2"
                shift 2
                ;;
            --service-cidr)
                ny_need_value "$1" $#
                service_cidr="$2"
                shift 2
                ;;
            --datastore-endpoint)
                ny_need_value "$1" $#
                datastore="$2"
                ny_secret_register "$2"
                shift 2
                ;;
            --node-label)
                ny_need_value "$1" $#
                labels+=(--node-label "$2")
                shift 2
                ;;
            --node-taint)
                ny_need_value "$1" $#
                taints+=(--node-taint "$2")
                shift 2
                ;;
            --)
                shift
                break
                ;;
            *) ny_usage_error "Unknown option for 'install master': $1" ;;
        esac
    done
    [[ -z "$token" ]] || ny_warn "--token puts the secret in your shell history; next time use --token-file."
    [[ -z "$channel" ]] || ny_valid_enum "$channel" stable latest testing || ny_usage_error "$NY_VALID_MSG"
    [[ -z "$version" ]] || ny_valid_k3s_version "$version" || ny_usage_error "$NY_VALID_MSG"
    [[ -z "$cluster_cidr" ]] || ny_valid_cidr4 "$cluster_cidr" || ny_usage_error "$NY_VALID_MSG"
    [[ -z "$service_cidr" ]] || ny_valid_cidr4 "$service_cidr" || ny_usage_error "$NY_VALID_MSG"
    local l
    for l in "${labels[@]+"${labels[@]}"}"; do
        [[ "$l" == --node-label ]] || ny_valid_label "$l" || ny_usage_error "$NY_VALID_MSG"
    done
    for l in "${taints[@]+"${taints[@]}"}"; do
        [[ "$l" == --node-taint ]] || ny_valid_taint "$l" || ny_usage_error "$NY_VALID_MSG"
    done

    k3s_load_state
    k3s_guard_role server
    ny_deps_ensure_feature core k3s
    host_preflight_ports "$keep_ingress"
    [[ "$HOST_DISABLE_INGRESS" -eq 1 ]] && disable+=(--disable traefik --disable servicelb)

    local node_ip
    if [[ -n "$iface" ]]; then
        node_ip="$(k3s_validate_interface "$iface")"
    else
        node_ip="$(ny_best_ip)"
    fi
    [[ -n "$node_ip" ]] || ny_die "Could not work out this machine's IPv4 address." "Pass --interface with the network card to use (see: nodeyard network-info)."
    local server_url="https://${node_ip}:6443"

    ny_info "About to install k3s here as the first server:"
    ny_hint "address:   ${node_ip}${iface:+ (interface ${iface})}"
    local store="SQLite (single server)"
    [[ -n "$datastore" ]] && store="external datastore"
    [[ "$cluster_init" -eq 1 ]] && store="embedded etcd (more servers can join)"
    ny_hint "datastore: ${store}"
    ny_hint "workloads: $([[ $allow -eq 1 ]] && echo 'allowed on this server' || echo 'not scheduled here (dedicated control plane)')"
    ny_hint "k3s:       ${version:-${channel:-stable} channel}"
    [[ "${#disable[@]}" -gt 0 ]] && ny_hint "disabled:  $(printf '%s ' "${disable[@]}" | sed 's/--disable //g')"
    ny_confirm "Install k3s server on this machine?" y || {
        ny_info "Cancelled; nothing was changed."
        return 0
    }

    k3s_save_state server "$iface" "$node_ip" "" "$channel" "$san_csv" \
        "$([[ $cluster_init -eq 1 ]] && echo true || echo false)" "$([[ $allow -eq 1 ]] && echo true || echo false)"

    host_install_prereqs
    k3s_network_args "$iface" server
    k3s_tls_san_args "$san_csv" "$((no_auto_san == 1 ? 0 : 1))"

    k3s_store_token "$token" "$token_file"
    local token_path="$K3S_TOKEN_PATH"

    local -a args=(server)
    [[ "$cluster_init" -eq 1 ]] && args+=(--cluster-init)
    [[ -n "$token_path" ]] && args+=(--token-file "$token_path")
    args+=("${K3S_NET_ARGS[@]+"${K3S_NET_ARGS[@]}"}" "${K3S_SAN_ARGS[@]+"${K3S_SAN_ARGS[@]}"}")
    args+=("${disable[@]+"${disable[@]}"}" "${labels[@]+"${labels[@]}"}" "${taints[@]+"${taints[@]}"}")
    [[ -n "$cluster_cidr" ]] && args+=(--cluster-cidr "$cluster_cidr")
    [[ -n "$service_cidr" ]] && args+=(--service-cidr "$service_cidr")

    local -a env=("INSTALL_K3S_EXEC=$(ny_quote_cmd "${args[@]}")")
    # An external datastore DSN usually holds a password: pass it through the
    # environment (k3s's installer keeps K3S_* in a root-only env file), never
    # in the world-readable service file.
    [[ -n "$datastore" ]] && env+=("K3S_DATASTORE_ENDPOINT=${datastore}")
    if [[ -n "$version" ]]; then
        env+=("INSTALL_K3S_VERSION=${version}")
    elif [[ -n "$channel" ]]; then
        env+=("INSTALL_K3S_CHANNEL=${channel}")
    fi

    ny_k3s_installed && ny_info "k3s is already installed; the installer will reconcile its settings (safe to repeat)."
    k3s_run_installer "${env[@]}"
    K3S_ROLE="server"
    k3s_start_service k3s

    firewall_open_k3s server

    if [[ "$allow" -eq 1 ]]; then
        ny_simulating || sleep 5
        ny_run "$(ny_path "$NY_K3S_BIN")" kubectl taint nodes "$(hostname)" \
            node-role.kubernetes.io/control-plane:NoSchedule- node-role.kubernetes.io/master:NoSchedule- >/dev/null 2>&1 || true
    fi
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        return 0
    elif ny_wait_node_ready "$(hostname)"; then
        ny_ok "k3s server installed and this node is Ready."
    else
        ny_warn "k3s server installed, but the node hasn't reported Ready yet. If this persists: sudo nodeyard doctor"
    fi
    ny_info "API address: ${server_url}"
    ny_hint "Join other machines: sudo nodeyard token"
    ny_hint "kubectl access:      sudo nodeyard kubeconfig"
    if [[ "$HOST_DISABLE_INGRESS" -eq 1 ]]; then
        ny_hint "Traefik and ServiceLB were disabled because ports 80/443 were taken. Re-run with --keep-ingress to change that."
    fi
    return 0
}

k3s_join_common_args() {
    K3S_J_SERVER="" K3S_J_TOKEN="" K3S_J_TOKEN_FILE="" K3S_J_IFACE="" K3S_J_CHANNEL="" K3S_J_VERSION="" K3S_J_SAN="" K3S_J_NOAUTO=0
    K3S_J_LABELS=() K3S_J_TAINTS=()
}

k3s_install_agent_cmd_help() {
    cat <<'HELP'
Usage: nodeyard install worker --server URL (--token-file PATH | --token TOKEN) [options]

Joins this machine to an existing cluster as a worker (k3s agent).

Options:
  --server URL             The cluster's API address, e.g. https://192.168.1.10:6443
  --token-file PATH        File holding the join token (from 'nodeyard token' on a server)
  --token-stdin            Read the join token from standard input
  --token TOKEN            The join token itself (visible in shell history: prefer --token-file)
  --interface IFACE        Pin cluster traffic to this interface (default: the one that routes to the server)
  --version V | --channel C  k3s version; use the server's version (e.g. v1.33.4+k3s1)
  --node-label K=V         Kubernetes label (repeatable)
  --node-taint K=V:Effect  Kubernetes taint (repeatable)
HELP
}

k3s_install_agent_cmd() {
    ny_need_root
    k3s_join_common_args
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --server)
                ny_need_value "$1" $#
                K3S_J_SERVER="$2"
                shift 2
                ;;
            --token)
                ny_need_value "$1" $#
                K3S_J_TOKEN="$2"
                ny_secret_register "$2"
                shift 2
                ;;
            --token-file)
                ny_need_value "$1" $#
                K3S_J_TOKEN_FILE="$2"
                shift 2
                ;;
            --token-stdin)
                K3S_J_TOKEN="$(k3s_read_token_stdin)"
                shift
                ;;
            --interface)
                ny_need_value "$1" $#
                K3S_J_IFACE="$2"
                shift 2
                ;;
            --channel)
                ny_need_value "$1" $#
                K3S_J_CHANNEL="$2"
                shift 2
                ;;
            --version)
                ny_need_value "$1" $#
                K3S_J_VERSION="$2"
                shift 2
                ;;
            --node-label)
                ny_need_value "$1" $#
                ny_valid_label "$2" || ny_usage_error "$NY_VALID_MSG"
                K3S_J_LABELS+=(--node-label "$2")
                shift 2
                ;;
            --node-taint)
                ny_need_value "$1" $#
                ny_valid_taint "$2" || ny_usage_error "$NY_VALID_MSG"
                K3S_J_TAINTS+=(--node-taint "$2")
                shift 2
                ;;
            *) ny_usage_error "Unknown option for 'install worker': $1" ;;
        esac
    done
    k3s_join agent
}

k3s_install_join_server_cmd_help() {
    cat <<'HELP'
Usage: nodeyard install join-master --server URL (--token-file PATH | --token TOKEN) [options]

Joins this machine to an existing cluster (created with --ha) as an
additional server. etcd needs 3 or more servers to survive one failing.

Options:
  --server URL             An existing server, e.g. https://192.168.1.10:6443
  --token-file PATH        File holding the cluster token
  --token-stdin            Read the token from standard input
  --token TOKEN            The token itself (visible in shell history: prefer --token-file)
  --interface IFACE        Pin cluster traffic to this interface
  --tls-san HOST           Extra name/address for the API certificate (repeatable)
  --no-auto-tls-san        Don't add this host's name and Tailscale address
  --version V | --channel C  k3s version (use the existing servers' version)
HELP
}

k3s_install_join_server_cmd() {
    ny_need_root
    k3s_join_common_args
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --server)
                ny_need_value "$1" $#
                K3S_J_SERVER="$2"
                shift 2
                ;;
            --token)
                ny_need_value "$1" $#
                K3S_J_TOKEN="$2"
                ny_secret_register "$2"
                shift 2
                ;;
            --token-file)
                ny_need_value "$1" $#
                K3S_J_TOKEN_FILE="$2"
                shift 2
                ;;
            --token-stdin)
                K3S_J_TOKEN="$(k3s_read_token_stdin)"
                shift
                ;;
            --interface)
                ny_need_value "$1" $#
                K3S_J_IFACE="$2"
                shift 2
                ;;
            --tls-san)
                ny_need_value "$1" $#
                K3S_J_SAN+="${K3S_J_SAN:+,}$2"
                shift 2
                ;;
            --no-auto-tls-san)
                K3S_J_NOAUTO=1
                shift
                ;;
            --channel)
                ny_need_value "$1" $#
                K3S_J_CHANNEL="$2"
                shift 2
                ;;
            --version)
                ny_need_value "$1" $#
                K3S_J_VERSION="$2"
                shift 2
                ;;
            *) ny_usage_error "Unknown option for 'install join-master': $1" ;;
        esac
    done
    k3s_join server
}

# k3s_join agent|server -- shared by 'install worker' and 'install join-master'.
k3s_join() {
    local mode="$1" what="worker"
    [[ "$mode" == server ]] && what="additional server"
    [[ -n "$K3S_J_SERVER" ]] || ny_usage_error "--server is required (the cluster's API address, e.g. https://192.168.1.10:6443)."
    ny_valid_url "$K3S_J_SERVER" || ny_usage_error "$NY_VALID_MSG"
    [[ -n "$K3S_J_TOKEN" || -n "$K3S_J_TOKEN_FILE" ]] ||
        ny_usage_error "A join token is required: --token-file PATH (get it with 'sudo nodeyard token' on a server)."
    [[ -z "$K3S_J_TOKEN" ]] || ny_warn "--token puts the secret in your shell history; next time use --token-file."
    [[ -z "$K3S_J_CHANNEL" ]] || ny_valid_enum "$K3S_J_CHANNEL" stable latest testing || ny_usage_error "$NY_VALID_MSG"
    [[ -z "$K3S_J_VERSION" ]] || ny_valid_k3s_version "$K3S_J_VERSION" || ny_usage_error "$NY_VALID_MSG"

    k3s_load_state
    k3s_guard_role "$mode"
    ny_deps_ensure_feature core k3s
    k3s_preflight_join "$K3S_J_SERVER"

    local iface="$K3S_J_IFACE"
    if [[ -z "$iface" ]]; then
        local host
        host="$(sed -E 's#^https?://##; s#[:/].*$##' <<<"$K3S_J_SERVER")"
        iface="$(ip -4 route get "$host" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}' || true)"
        [[ -n "$iface" ]] && ny_info "No --interface given; using ${iface}, the interface that routes to ${host}."
    fi
    local node_ip
    if [[ -n "$iface" ]]; then
        node_ip="$(k3s_validate_interface "$iface")"
    else
        node_ip="$(ny_best_ip)"
    fi

    ny_info "About to join this machine to ${K3S_J_SERVER} as a ${what} (address ${node_ip}${iface:+ on ${iface}})."
    ny_confirm "Join the cluster as a ${what}?" y || {
        ny_info "Cancelled; nothing was changed."
        return 0
    }

    k3s_save_state "$mode" "$iface" "$node_ip" "$K3S_J_SERVER" "" "$K3S_J_SAN"
    host_install_prereqs
    k3s_store_token "$K3S_J_TOKEN" "$K3S_J_TOKEN_FILE"
    local token_path="$K3S_TOKEN_PATH"

    local -a args=() env=()
    if [[ "$mode" == agent ]]; then
        k3s_network_args "$iface" agent
        args=(agent "${K3S_NET_ARGS[@]+"${K3S_NET_ARGS[@]}"}" "${K3S_J_LABELS[@]+"${K3S_J_LABELS[@]}"}" "${K3S_J_TAINTS[@]+"${K3S_J_TAINTS[@]}"}")
        env=("K3S_URL=${K3S_J_SERVER}" "K3S_TOKEN_FILE=${token_path}")
    else
        k3s_network_args "$iface" server
        k3s_tls_san_args "$K3S_J_SAN" "$((K3S_J_NOAUTO == 1 ? 0 : 1))"
        args=(server --server "$K3S_J_SERVER" --token-file "$token_path" "${K3S_NET_ARGS[@]+"${K3S_NET_ARGS[@]}"}" "${K3S_SAN_ARGS[@]+"${K3S_SAN_ARGS[@]}"}")
    fi
    env+=("INSTALL_K3S_EXEC=$(ny_quote_cmd "${args[@]}")")
    if [[ -n "$K3S_J_VERSION" ]]; then
        env+=("INSTALL_K3S_VERSION=${K3S_J_VERSION}")
    elif [[ -n "$K3S_J_CHANNEL" ]]; then
        env+=("INSTALL_K3S_CHANNEL=${K3S_J_CHANNEL}")
    fi

    k3s_run_installer "${env[@]}"
    local svc="k3s"
    [[ "$mode" == agent ]] && svc="k3s-agent"
    K3S_ROLE="$mode"
    k3s_start_service "$svc"
    firewall_open_k3s "$mode"
    [[ "$NY_DRY_RUN" -eq 1 ]] && return 0
    ny_ok "Joined ${K3S_J_SERVER} as a ${what}."
}

# k3s_preflight_join SERVER_URL -- explain connection failures before joining.
k3s_preflight_join() {
    local server="$1" host port
    host="$(sed -E 's#^https?://##; s#[:/].*$##' <<<"$server")"
    port="$(sed -E 's#^https?://[^:/]+:?##; s#/.*$##' <<<"$server")"
    [[ "$port" =~ ^[0-9]+$ ]] || port=6443
    ny_step "Checking that ${host}:${port} is reachable"
    if ! ping -c1 -W2 "$host" >/dev/null 2>&1; then
        ny_warn "${host} did not answer ping. It may be off, the cable/interface may be down, or ping may be blocked (not necessarily fatal)."
    fi
    if ny_tcp_check "$host" "$port" 5; then
        ny_ok "TCP ${host}:${port} is reachable."
        return 0
    fi
    ny_warn "Cannot reach ${host}:${port} (no route to host / connection refused)."
    ny_hint "1. On the server, is k3s running?        sudo systemctl status k3s"
    ny_hint "2. Is it listening?                      sudo ss -tlnp | grep ${port}"
    ny_hint "3. Is its firewall open?                 sudo nodeyard firewall status"
    ny_hint "4. Is this machine's interface up?       ip -br addr"
    ny_hint "5. Same subnet, cable/switch connected?"
    ny_offer "Continue anyway?" n || ny_die "Cannot reach the server at ${host}:${port}." "Fix the connection (see the checklist above), then run this again."
}

k3s_token_cmd_help() {
    cat <<'HELP'
Usage: nodeyard token [--reveal] [--json]

Shows ready-to-run commands for joining other machines to this cluster, one
per address this server has. The token itself is a secret and is only
printed with --reveal; the commands read it from a file instead.

Options:
  --reveal   Also print the join token
HELP
}

k3s_token_cmd() {
    ny_need_root
    local reveal=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --reveal)
                reveal=1
                shift
                ;;
            *) ny_usage_error "Unknown option for 'token': $1" ;;
        esac
    done
    local tf
    tf="$(ny_path "$NY_K3S_TOKEN_FILE")"
    [[ -r "$tf" ]] || ny_die "No k3s server token here: this machine is not a k3s server." "Run this on the first server (install master)." "$NY_E_PRECONDITION"
    local token
    token="$(<"$tf")"
    ny_secret_register "$token"

    local -a addrs=()
    local name kind state cidr
    while IFS=$'\t' read -r name kind state cidr; do
        [[ -n "$cidr" && "$kind" != virtual ]] && addrs+=("${name}:${cidr%%/*}")
    done < <(ny_list_ifaces)
    local ts
    ts="$(ny_tailscale_ip || true)"
    [[ -n "$ts" ]] && addrs+=("tailscale:${ts}")

    if [[ "$NY_JSON" -eq 1 ]]; then
        local -a items=()
        local a
        for a in "${addrs[@]+"${addrs[@]}"}"; do
            items+=("$(ny_json_obj "interface=${a%%:*}" "address=${a#*:}" "server=https://${a#*:}:6443")")
        done
        ny_json_out "$(ny_json_obj ok:=true "token_file=$NY_K3S_TOKEN_FILE" "token?=$([[ $reveal -eq 1 ]] && printf '%s' "$token")" "servers:=$(ny_json_arr "${items[@]+"${items[@]}"}")")"
        return 0
    fi

    printf '%s\n\n' "$(ny_color bold "Joining machines to this cluster")"
    printf 'The join token is in %s on this server.\n' "$NY_K3S_TOKEN_FILE"
    if [[ "$reveal" -eq 1 ]]; then
        printf 'Token: %s\n' "$token"
        ny_warn "Anyone with this token can join machines to your cluster. Don't paste it anywhere public."
    else
        printf 'It is hidden here; show it with: sudo nodeyard token --reveal\n'
    fi
    printf '\nOn the other machine, put the token in a root-only file, then run one of these:\n'
    printf '  sudo install -m 600 /dev/stdin /root/k3s-token   %s\n' "$(ny_color dim "# paste the token, then Ctrl-D")"
    local default_ip a
    default_ip="$(ny_best_ip)"
    for a in "${addrs[@]+"${addrs[@]}"}"; do
        local ifn="${a%%:*}" ip="${a#*:}" note=""
        [[ "$ip" == "$default_ip" ]] && note=" $(ny_color dim "(default route)")"
        printf '\n-- via %s (%s)%s --\n' "$ifn" "$ip" "$note"
        printf '  Worker:            sudo nodeyard install worker --server https://%s:6443 --token-file /root/k3s-token --interface eth0\n' "$ip"
        printf '  Additional server: sudo nodeyard install join-master --server https://%s:6443 --token-file /root/k3s-token --interface eth0\n' "$ip"
    done
    printf '\nChange --interface to the OTHER machine'\''s network card (check with: ip -br addr).\n'
    printf 'Or set the machine up from here over SSH: sudo nodeyard add-node worker --ssh user@<address>\n'
}

k3s_status_cmd_help() {
    cat <<'HELP'
Usage: nodeyard status [--json]

Shows this node's role, address and k3s service state, and on a server the
cluster's nodes and any pods that aren't running.
HELP
}

k3s_status_cmd() {
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    k3s_load_state
    ny_detect_all
    local svc active="not installed" version=""
    svc="$(k3s_service_name)"
    if ny_k3s_installed; then
        version="$(k3s_version)"
        if systemctl is-active --quiet "$svc" 2>/dev/null; then active="active"; else active="inactive"; fi
    fi
    local nodes_json="[]" ready=0 total=0
    if [[ "$K3S_ROLE" == server ]] && kctl_available; then
        local raw
        raw="$(kctl get nodes --no-headers -o wide 2>/dev/null || true)"
        if [[ -n "$raw" ]]; then
            total="$(grep -c . <<<"$raw")"
            ready="$(awk '$2 == "Ready" {n++} END {print n+0}' <<<"$raw")"
            nodes_json="$(awk '{printf "%s{\"name\":\"%s\",\"status\":\"%s\",\"roles\":\"%s\",\"version\":\"%s\",\"address\":\"%s\"}", (NR>1?",":""), $1, $2, $3, $5, $6}' <<<"$raw")"
            nodes_json="[${nodes_json}]"
        fi
    fi
    if [[ "$NY_JSON" -eq 1 ]]; then
        ny_json_out "$(ny_json_obj ok:=true "nodeyard=$NY_VERSION" "node=$(ny_self_name)" "role?=$K3S_ROLE" "interface?=$K3S_IFACE" \
            "address?=${K3S_NODE_IP:-$(ny_best_ip)}" "server?=$K3S_SERVER_URL" "k3s_version?=$version" "service=$svc" "service_state=$active" \
            "nodes_ready:=$ready" "nodes_total:=$total" "nodes:=$nodes_json")"
        return 0
    fi
    printf '%s\n' "$(ny_color bold "This node")"
    printf '  %-12s %s\n' "Name:" "$(ny_self_name)" "Role:" "${K3S_ROLE:-not installed}" \
        "Address:" "${K3S_NODE_IP:-$(ny_best_ip)}${K3S_IFACE:+ (${K3S_IFACE})}" "Joins:" "${K3S_SERVER_URL:--}" \
        "k3s:" "${version:-not installed}" "Service:" "${svc} $(ny_status_color "$active")" "nodeyard:" "$NY_VERSION"
    if [[ "$K3S_ROLE" == server ]] && kctl_available; then
        printf '\n%s (%s/%s Ready)\n' "$(ny_color bold "Cluster nodes")" "$ready" "$total"
        k3s_nodes_table
        printf '\n%s\n' "$(ny_color bold "Pods that are not running")"
        local bad
        bad="$(kctl get pods -A --no-headers 2>/dev/null | awk '$4 !~ /Running|Completed/' || true)"
        if [[ -n "$bad" ]]; then
            {
                printf 'NAMESPACE\tPOD\tREADY\tSTATUS\n'
                awk '{print $1"\t"$2"\t"$3"\t"$4}' <<<"$bad"
            } | ny_table --status STATUS
        else
            printf '  %s\n' "$(ny_color green "all pods are running")"
        fi
    elif [[ "$NY_VERBOSE" -eq 1 ]] && ny_k3s_installed; then
        systemctl --no-pager --full status "$svc" 2>/dev/null || true
    fi
    return 0
}

k3s_nodes_table() {
    {
        printf 'NAME\tSTATUS\tROLES\tVERSION\tADDRESS\tOS\n'
        kctl get nodes --no-headers -o wide 2>/dev/null | awk '{os=""; for(i=8;i<=NF-2;i++) os=os (os?" ":"") $i; print $1"\t"$2"\t"$3"\t"$5"\t"$6"\t"os}'
    } | ny_table --status STATUS
}

k3s_list_nodes_cmd() {
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    ny_need_kube
    if [[ "$NY_JSON" -eq 1 ]]; then
        ny_json_out "$(kctl get nodes -o json | jq -c '{ok: true, nodes: [.items[] | {name: .metadata.name,
            ready: ((.status.conditions // []) | map(select(.type == "Ready")) | .[0].status == "True"),
            roles: ([.metadata.labels | to_entries[] | select(.key | startswith("node-role.kubernetes.io/")) | .key | sub("node-role.kubernetes.io/"; "")]),
            address: ((.status.addresses // []) | map(select(.type == "InternalIP")) | .[0].address),
            version: .status.nodeInfo.kubeletVersion, arch: .status.nodeInfo.architecture}]}')"
        return 0
    fi
    k3s_nodes_table
}

k3s_service_action() {
    local action="$1"
    ny_need_root
    k3s_load_state
    ny_detect_all
    local svc
    svc="$(k3s_service_name)"
    ny_k3s_installed || ny_die "k3s is not installed on this machine." "Install it first: sudo nodeyard install master (or worker)" "$NY_E_PRECONDITION"
    if [[ "$NY_INIT" == openrc ]]; then
        case "$action" in
            enable) ny_run rc-update add "$svc" default ;;
            disable) ny_run rc-update del "$svc" default || true ;;
            *) ny_run rc-service "$svc" "$action" ;;
        esac
    else
        case "$action" in
            enable | disable) ny_run systemctl "$action" "$svc" ;;
            *) ny_run systemctl "$action" "$svc" ;;
        esac
    fi
    return 0
}

k3s_start_cmd() {
    k3s_service_action start
    ny_ok "$(k3s_service_name) started."
}
k3s_stop_cmd() {
    ny_confirm "Stop k3s on this node? Its workloads stop until it is started again." y || return 0
    k3s_service_action stop
    ny_ok "$(k3s_service_name) stopped."
}
k3s_restart_cmd() {
    k3s_service_action restart
    ny_ok "$(k3s_service_name) restarted."
}
k3s_enable_boot_cmd() {
    k3s_service_action enable
    ny_ok "$(k3s_service_name) will start at boot."
}
k3s_disable_boot_cmd() {
    k3s_service_action disable
    ny_ok "$(k3s_service_name) will not start at boot."
}

k3s_logs_cmd_help() {
    cat <<'HELP'
Usage: nodeyard logs [--follow] [--lines N]

Shows the k3s (or k3s-agent) service log on this node.

Options:
  --follow, -f   Keep showing new lines
  --lines N      How many recent lines to show (default 200)
HELP
}

k3s_logs_cmd() {
    ny_need_root
    local lines=200 follow=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --follow | -f)
                follow=1
                shift
                ;;
            --lines)
                ny_need_value "$1" $#
                ny_valid_int "$2" 1 100000 || ny_usage_error "$NY_VALID_MSG"
                lines="$2"
                shift 2
                ;;
            *) ny_usage_error "Unknown option for 'logs': $1" ;;
        esac
    done
    local svc
    svc="$(k3s_service_name)"
    if have journalctl; then
        if [[ "$follow" -eq 1 ]]; then
            journalctl -u "$svc" -f
        else
            journalctl -u "$svc" -n "$lines" --no-pager
        fi
    else
        tail -n "$lines" "$NY_LOG_FILE"
    fi
    return 0
}

k3s_upgrade_cmd_help() {
    cat <<'HELP'
Usage: nodeyard upgrade [--channel stable|latest|testing | --version vX.Y.Z+k3sN]

Upgrades k3s on this node (default: the stable channel). Upgrade servers
before agents; an agent must never run a newer k3s than the servers.
HELP
}

k3s_upgrade_cmd() {
    ny_need_root
    k3s_load_state
    ny_detect_all
    local channel="" version=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --channel)
                ny_need_value "$1" $#
                ny_valid_enum "$2" stable latest testing || ny_usage_error "$NY_VALID_MSG"
                channel="$2"
                shift 2
                ;;
            --version)
                ny_need_value "$1" $#
                ny_valid_k3s_version "$2" || ny_usage_error "$NY_VALID_MSG"
                version="$2"
                shift 2
                ;;
            *) ny_usage_error "Unknown option for 'upgrade': $1" ;;
        esac
    done
    ny_k3s_installed || ny_die "k3s is not installed on this machine." "" "$NY_E_PRECONDITION"
    local before svc
    before="$(k3s_version)"
    svc="$(k3s_service_name)"
    # The installer rewrites the service from what it is given, so pass the
    # node's current arguments and K3S_* settings back in. (Without them an
    # agent would be silently reinstalled as a server.)
    local -a env=()
    mapfile -t env < <(k3s_current_install_env "$svc")
    [[ "${#env[@]}" -gt 0 ]] || ny_die "Could not read the current ${svc} service settings." \
        "Check /etc/systemd/system/${svc}.service exists; reinstall with 'nodeyard install' if it doesn't." "$NY_E_PRECONDITION"
    if [[ -n "$version" ]]; then
        env+=("INSTALL_K3S_VERSION=${version}")
    else
        env+=("INSTALL_K3S_CHANNEL=${channel:-stable}")
    fi
    ny_info "Current k3s: ${before:-unknown}; target: ${version:-latest ${channel:-stable}}"
    ny_confirm "Upgrade k3s on this node (${svc})? Its workloads restart." y || {
        ny_info "Cancelled."
        return 0
    }
    k3s_run_installer "${env[@]}"
    k3s_start_service "$svc"
    [[ "$NY_DRY_RUN" -eq 1 ]] && return 0
    ny_ok "k3s upgraded: ${before:-?} -> $(k3s_version)"
}

# k3s_current_install_env SERVICE -- INSTALL_K3S_EXEC and K3S_* assignments
# that reproduce the installed service (one per line).
k3s_current_install_env() {
    local svc="$1" unit envf exec_line
    unit="$(ny_path "/etc/systemd/system/${svc}.service")"
    envf="$(ny_path "/etc/systemd/system/${svc}.service.env")"
    [[ -r "$unit" ]] || return 0
    exec_line="$(awk '/^ExecStart=/{f=1} f{print} f && !/\\$/{exit}' "$unit" | sed -e 's/^ExecStart=//' -e 's/\\$//' | tr '\n' ' ')"
    local -a words=()
    mapfile -t words < <(xargs -n1 printf '%s\n' <<<"$exec_line" 2>/dev/null || true)
    [[ "${#words[@]}" -ge 2 ]] || return 0
    printf 'INSTALL_K3S_EXEC=%s\n' "$(ny_quote_cmd "${words[@]:1}")"
    if [[ -r "$envf" ]]; then
        local line k v
        while IFS= read -r line; do
            [[ "$line" =~ ^(K3S_[A-Z_]+)=(.*)$ ]] || continue
            k="${BASH_REMATCH[1]}"
            v="${BASH_REMATCH[2]}"
            v="${v#[\'\"]}"
            v="${v%[\'\"]}"
            [[ "$k" == K3S_TOKEN || "$k" == K3S_DATASTORE_ENDPOINT ]] && ny_secret_register "$v"
            printf '%s=%s\n' "$k" "$v"
        done <"$envf"
    fi
    return 0
}

k3s_kubeconfig_cmd_help() {
    cat <<'HELP'
Usage: nodeyard kubeconfig [--user USER] [--ip ADDRESS] [--merge] [--stdout]

Creates a kubeconfig for kubectl that points at this server.

Options:
  --user USER     Install it as USER's ~/.kube/config (default: the user running sudo)
  --ip ADDRESS    Address to put in it (default: Tailscale address, else this machine's address)
  --merge         Merge into an existing ~/.kube/config instead of replacing it
  --stdout        Print it instead (e.g. sudo nodeyard kubeconfig --stdout > k3s.yaml)
HELP
}

k3s_kubeconfig_cmd() {
    ny_need_root
    local target_ip="" user="${SUDO_USER:-}" merge=0 to_stdout=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --ip)
                ny_need_value "$1" $#
                ny_valid_host "$2" || ny_usage_error "$NY_VALID_MSG"
                target_ip="$2"
                shift 2
                ;;
            --user)
                ny_need_value "$1" $#
                user="$2"
                shift 2
                ;;
            --merge)
                merge=1
                shift
                ;;
            --stdout)
                to_stdout=1
                shift
                ;;
            *) ny_usage_error "Unknown option for 'kubeconfig': $1" ;;
        esac
    done
    local admin
    admin="$(ny_path "$NY_K3S_KUBECONFIG")"
    [[ -r "$admin" ]] || ny_die "This machine has no k3s admin kubeconfig: it is not a k3s server." "Run this on a server." "$NY_E_PRECONDITION"
    [[ -n "$target_ip" ]] || target_ip="$(ny_tailscale_ip || ny_best_ip)"
    [[ -n "$target_ip" ]] || ny_die "Could not work out an address to put in the kubeconfig." "Pass --ip ADDRESS." "$NY_E_USAGE"

    local content
    content="$(sed "s#https://127.0.0.1:6443#https://${target_ip}:6443#" "$admin")"
    if [[ "$to_stdout" -eq 1 ]]; then
        printf '%s\n' "$content"
        return 0
    fi
    if [[ -z "$user" || "$user" == root ]]; then
        local dest="${NY_STATE}/kubeconfig/${target_ip}.yaml"
        ny_ensure_dir "$(ny_unroot "${NY_STATE}/kubeconfig")" 0700
        printf '%s\n' "$content" | ny_write_file "$(ny_unroot "$dest")" 0600
        ny_ok "Kubeconfig written to $(ny_unroot "$dest") (server https://${target_ip}:6443)."
        ny_hint "Copy it to your computer, e.g.: scp root@${target_ip}:$(ny_unroot "$dest") ~/.kube/config"
        return 0
    fi
    local home
    home="$(getent passwd "$user" | cut -d: -f6 || true)"
    [[ -n "$home" ]] || ny_die "User '${user}' was not found." "Check the name with: getent passwd ${user}" "$NY_E_USAGE"
    local dest="${home}/.kube/config"
    if [[ "$merge" -eq 1 && -f "$(ny_path "$dest")" ]]; then
        local tmp
        tmp="$(ny_mktemp)"
        printf '%s\n' "$content" >"$tmp"
        content="$(KUBECONFIG="$(ny_path "$dest"):${tmp}" kctl config view --flatten)"
    fi
    ny_ensure_dir "${home}/.kube" 0700
    printf '%s\n' "$content" | ny_write_file "$dest" 0600 "${user}:$(id -gn "$user" 2>/dev/null || echo "$user")"
    ny_simulating || chown "${user}:" "$(ny_path "${home}/.kube")" 2>/dev/null || true
    ny_ok "Kubeconfig installed for ${user} at ${dest} (server https://${target_ip}:6443)."
}

k3s_uninstall_cmd_help() {
    cat <<'HELP'
Usage: nodeyard uninstall k3s [--yes]

Removes k3s from THIS node (all its containers and cluster data on this
machine) and undoes the system changes nodeyard made for k3s: kernel
settings, firewall rules, the watchdog and the stored join token. Your
cluster.conf is kept.
HELP
}

k3s_uninstall_cmd() {
    ny_need_root
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    k3s_load_state
    ny_warn "This removes k3s and all of its data (containers, images, and on a server the cluster state) from this machine."
    ny_confirm "Uninstall k3s from this node?" n || {
        ny_info "Cancelled; nothing was changed."
        return 0
    }
    local s
    for s in /usr/local/bin/k3s-uninstall.sh /usr/local/bin/k3s-agent-uninstall.sh; do
        if [[ -x "$(ny_path "$s")" ]]; then
            ny_run "$s"
            break
        fi
    done
    ny_journal_undo_feature watchdog 1 || true
    ny_journal_undo_feature k3s || ny_warn "Some changes could not be undone automatically (see above)."
    local self
    self="$(ny_self_name)"
    ny_cfg_unset node "$self" role
    ny_cfg_unset node "$self" server
    ny_cfg_unset node "$self" node-ip
    ny_cfg_unset node "$self" init
    ny_secret_exists k3s-token && ny_remove_file "$(ny_secret_path k3s-token)"
    ny_ok "k3s has been removed from this node."
}
