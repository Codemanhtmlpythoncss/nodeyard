# shellcheck shell=bash
# doctor: check this node for common problems and offer to fix each one.
# Each check is a function returning 0 (ok), 1 (problem), 2 (not relevant
# here) or 3 (warning: worth knowing, nothing to fix). Fixes are ordinary
# nodeyard changes, so they show up in 'nodeyard changes' and can be undone.
# Later phases register more checks (time, certificates, etcd, drift...).

ny_cmd "doctor" doctor_cmd "Health" "Check this machine for common problems and offer to fix them" doctor json

DOCTOR_CHECKS=()
DOCTOR_DETAIL=""

# doctor_register ID CATEGORY TITLE CHECK_FN [FIX_FN] [FIX_DESCRIPTION] [FEATURE] [REBOOT]
doctor_register() {
    DOCTOR_CHECKS+=("$1"$'\x1f'"$2"$'\x1f'"$3"$'\x1f'"$4"$'\x1f'"${5:-}"$'\x1f'"${6:-}"$'\x1f'"${7:-doctor}"$'\x1f'"${8:-0}")
}

# --- checks ------------------------------------------------------------------

doctor_check_deps() {
    local -a missing=()
    mapfile -t missing < <(ny_deps_missing jq curl ip flock openssl tar)
    [[ "${#missing[@]}" -eq 0 ]] && return 0
    DOCTOR_DETAIL="missing: $(ny_join ', ' "${missing[@]}")"
    return 1
}
doctor_fix_deps() { ny_deps_ensure "nodeyard" jq curl ip flock openssl tar; }

doctor_check_config() {
    [[ -f "$NY_CONFIG" ]] || {
        DOCTOR_DETAIL="no cluster config yet (created when you set up a node)"
        return 2
    }
    if ny_cfg_validate "$NY_CONFIG"; then
        if [[ "${#NY_CFG_WARNINGS[@]}" -gt 0 ]]; then
            DOCTOR_DETAIL="${NY_CFG_WARNINGS[0]}"
            return 3
        fi
        return 0
    fi
    DOCTOR_DETAIL="${NY_CFG_ERRORS[0]} (see: nodeyard config validate)"
    # Reload what the rest of the checks read.
    ny_cfg_parse "$NY_CONFIG" || true
    return 1
}

doctor_k3s_relevant() {
    [[ -n "$K3S_ROLE" ]] || ny_k3s_installed
}

doctor_check_k3s_binary() {
    doctor_k3s_relevant || return 2
    ny_k3s_installed && return 0
    DOCTOR_DETAIL="this node is configured as a ${K3S_ROLE} but /usr/local/bin/k3s is missing; reinstall with 'nodeyard install'"
    return 1
}

doctor_check_k3s_enabled() {
    doctor_k3s_relevant && ny_k3s_installed || return 2
    systemctl is-enabled --quiet "$(k3s_service_name)" 2>/dev/null
}
doctor_fix_k3s_enabled() { ny_run systemctl enable "$(k3s_service_name)"; }

doctor_check_k3s_active() {
    doctor_k3s_relevant && ny_k3s_installed || return 2
    systemctl is-active --quiet "$(k3s_service_name)" 2>/dev/null && return 0
    DOCTOR_DETAIL="see why with: sudo journalctl -u $(k3s_service_name) -n 50 --no-pager"
    return 1
}
doctor_fix_k3s_active() {
    ny_run systemctl restart "$(k3s_service_name)"
    ny_simulating || sleep 5
}

doctor_check_swap() {
    [[ -n "$(swapon --noheadings 2>/dev/null || true)" ]] || return 0
    DOCTOR_DETAIL="swap is on (fine: k3s runs the kubelet with fail-swap-on=false)"
    return 0
}

doctor_check_module() {
    grep -q "^$1 " <<<"$(lsmod 2>/dev/null || true)" && return 0
    [[ -d "$(ny_path "/sys/module/$1")" ]] && return 0
    DOCTOR_DETAIL="kernel module $1 is not loaded"
    return 1
}
doctor_check_br_netfilter() { doctor_k3s_relevant || return 2; doctor_check_module br_netfilter; }
doctor_check_overlay() { doctor_k3s_relevant || return 2; doctor_check_module overlay; }
doctor_fix_modules() { host_fix_kernel_modules; }

doctor_check_ip_forward() {
    doctor_k3s_relevant || return 2
    [[ "$(cat "$(ny_path /proc/sys/net/ipv4/ip_forward)" 2>/dev/null)" == 1 ]]
}
doctor_fix_ip_forward() { host_fix_sysctl; }

doctor_check_time_sync() {
    host_time_sync_active && return 0
    DOCTOR_DETAIL="no systemd-timesyncd, chrony or ntpd running; clock drift breaks TLS between nodes"
    return 1
}
doctor_fix_time_sync() {
    if systemctl cat systemd-timesyncd.service >/dev/null 2>&1; then
        ny_service_enable systemd-timesyncd --now
        return 0
    fi
    ny_detect_all
    ny_pkg_install chrony
    local unit=chronyd
    systemctl cat chrony.service >/dev/null 2>&1 && unit=chrony
    ny_service_enable "$unit" --now
}

doctor_check_var_space() {
    local avail
    avail="$(df -Pk "$(ny_path /var)" 2>/dev/null | awk 'NR==2{print $4}')"
    [[ "$avail" =~ ^[0-9]+$ ]] || return 2
    ((avail > 1048576)) && return 0
    DOCTOR_DETAIL="only $((avail / 1024)) MiB free in /var; free some space (journalctl --vacuum-size=200M, old images, logs)"
    return 1
}

doctor_check_eviction() {
    doctor_k3s_relevant || return 2
    host_eviction_lockout_risk || return 0
    DOCTOR_DETAIL="the disk is over 80% full and k3s would evict pods until 15% is free"
    return 1
}
doctor_fix_eviction() { host_install_eviction_fix; }

doctor_check_boot_disk() {
    ny_detect_all
    case "$NY_BOOT_DISK" in
        usb)
            DOCTOR_DETAIL="the system disk is on USB (${NY_BOOT_DISK_MODEL:-unknown}): fine for an SSD in an enclosure, but a USB flash stick can freeze the node during long writes"
            return 3
            ;;
        sd)
            if [[ "$K3S_ROLE" == server ]]; then
                DOCTOR_DETAIL="this server runs from an SD card; etcd on SD cards is often unstable and wears the card out. Move servers to an SSD/NVMe if you can"
            else
                DOCTOR_DETAIL="running from an SD card: watch its health; an SSD lasts much longer"
            fi
            return 3
            ;;
    esac
    return 0
}

doctor_check_memory_cgroup() {
    doctor_k3s_relevant || return 2
    host_memory_cgroup_available && return 0
    DOCTOR_DETAIL="the kubelet needs the memory cgroup; without it k3s fails at start ('control process exited with error code')"
    return 1
}
doctor_fix_memory_cgroup() { host_fix_memory_cgroup; }

doctor_check_iface() {
    [[ -n "$K3S_IFACE" ]] || return 2
    if ! ny_iface_exists "$K3S_IFACE"; then
        DOCTOR_DETAIL="configured interface ${K3S_IFACE} does not exist (renamed? check: ip -br link)"
        return 1
    fi
    if [[ "$(ny_iface_state "$K3S_IFACE")" != up ]]; then
        DOCTOR_DETAIL="${K3S_IFACE} is $(ny_iface_state "$K3S_IFACE")"
        return 1
    fi
    if [[ -z "$(ny_iface_ipv4 "$K3S_IFACE")" ]]; then
        DOCTOR_DETAIL="${K3S_IFACE} has no IPv4 address"
        return 1
    fi
    return 0
}
doctor_fix_iface() { ny_run ip link set dev "$K3S_IFACE" up; }

doctor_check_api_local() {
    [[ "$K3S_ROLE" == server ]] && ny_k3s_installed || return 2
    [[ "$(ny_detect_firewall)" != none ]] || return 2
    ny_tcp_check 127.0.0.1 6443 3 && return 0
    DOCTOR_DETAIL="nothing answers on port 6443 locally"
    return 1
}
doctor_fix_api_local() { firewall_open_k3s server; }

doctor_check_forward_policy() {
    doctor_k3s_relevant || return 2
    host_forward_policy_blocking || return 0
    DOCTOR_DETAIL="iptables FORWARD policy is DROP/REJECT; this silently breaks pod (and Docker) networking"
    return 1
}
doctor_fix_forward_policy() { host_fix_forward_policy; }

doctor_check_foreign_nft() {
    doctor_k3s_relevant || return 2
    host_pod_forward_unpatched || return 0
    DOCTOR_DETAIL="another nftables table (e.g. miniupnpd) has a forward chain that drops pod traffic"
    return 1
}
doctor_fix_foreign_nft() { host_install_pod_forward_fix; }

doctor_check_nextcloud() {
    local how
    how="$(host_detect_nextcloud || true)"
    [[ -n "$how" ]] || return 2
    if kctl_available && { host_port_in_use 80 || host_port_in_use 443; } && kctl_quiet get svc -n kube-system traefik; then
        DOCTOR_DETAIL="Nextcloud (${how}) and k3s's Traefik both want ports 80/443"
        return 1
    fi
    DOCTOR_DETAIL="Nextcloud found (${how}); no port clash with k3s"
    return 0
}
doctor_fix_nextcloud() {
    ny_run "$(ny_path "$NY_K3S_BIN")" kubectl delete svc traefik -n kube-system >/dev/null 2>&1 || true
    ny_run "$(ny_path "$NY_K3S_BIN")" kubectl scale deploy/traefik -n kube-system --replicas=0 >/dev/null 2>&1 || true
}

doctor_check_server_reachable() {
    [[ "$K3S_ROLE" == agent && -n "$K3S_SERVER_URL" ]] || return 2
    local host
    host="$(sed -E 's#^https?://##; s#[:/].*$##' <<<"$K3S_SERVER_URL")"
    ny_tcp_check "$host" 6443 4 && return 0
    DOCTOR_DETAIL="cannot reach the server at ${host}:6443 (check the server is up and its firewall: sudo nodeyard firewall status)"
    return 1
}

doctor_cluster_relevant() {
    [[ "$K3S_ROLE" == server ]] && ny_k3s_installed && [[ -r "$(ny_path "$NY_K3S_KUBECONFIG")" ]]
}

doctor_check_api_healthz() {
    doctor_cluster_relevant || return 2
    kctl get --raw=/healthz >/dev/null 2>&1
}

doctor_check_node_ready() {
    doctor_cluster_relevant || return 2
    grep -qw Ready <<<"$(kctl get node "$(hostname)" --no-headers 2>/dev/null | awk '{print $2}' || true)"
}

doctor_check_cluster_nodes() {
    doctor_cluster_relevant || return 2
    local notready pressured
    notready="$(kctl get nodes --no-headers 2>/dev/null | awk '$2 !~ /^Ready/{print $1}' | paste -sd, - || true)"
    pressured="$(kctl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.conditions[?(@.type=="DiskPressure")].status}{" "}{.status.conditions[?(@.type=="MemoryPressure")].status}{"\n"}{end}' 2>/dev/null |
        awk '$2 == "True" {print $1 " (disk)"} $3 == "True" {print $1 " (memory)"}' | paste -sd, - || true)"
    if [[ -n "$notready" || -n "$pressured" ]]; then
        DOCTOR_DETAIL="${notready:+not Ready: ${notready}}${notready:+${pressured:+; }}${pressured:+under pressure: ${pressured} - run 'sudo nodeyard doctor --fix' on those nodes}"
        return 3
    fi
    return 0
}

doctor_check_pods() {
    doctor_cluster_relevant || return 2
    local bad
    bad="$(kctl get pods -A --no-headers 2>/dev/null | awk '$4 !~ /Running|Completed/{print $1 "/" $2 " (" $4 ")"}' | head -n 5 | paste -sd, - || true)"
    [[ -z "$bad" ]] && return 0
    DOCTOR_DETAIL="pods not running: ${bad}"
    return 3
}

doctor_register deps nodeyard "nodeyard's required tools are installed" doctor_check_deps doctor_fix_deps "install the missing packages"
doctor_register config nodeyard "cluster config is valid" doctor_check_config
doctor_register k3s-binary k3s "k3s is installed" doctor_check_k3s_binary
doctor_register k3s-enabled k3s "k3s service starts at boot" doctor_check_k3s_enabled doctor_fix_k3s_enabled "systemctl enable the k3s service" k3s
doctor_register k3s-active k3s "k3s service is running" doctor_check_k3s_active doctor_fix_k3s_active "restart the k3s service" k3s
doctor_register swap host "swap" doctor_check_swap
doctor_register br-netfilter host "br_netfilter kernel module loaded" doctor_check_br_netfilter doctor_fix_modules "load it now and at every boot" k3s
doctor_register overlay host "overlay kernel module loaded" doctor_check_overlay doctor_fix_modules "load it now and at every boot" k3s
doctor_register ip-forward host "IP forwarding enabled" doctor_check_ip_forward doctor_fix_ip_forward "set net.ipv4.ip_forward=1 (persistently)" k3s
doctor_register time-sync host "clock is kept in sync" doctor_check_time_sync doctor_fix_time_sync "enable systemd-timesyncd, or install chrony" host
doctor_register var-space host "more than 1 GiB free in /var" doctor_check_var_space
doctor_register eviction host "a full-disk scare can't lock this node out" doctor_check_eviction doctor_fix_eviction "make k3s reclaim only 2 GiB after a low-disk eviction (restarts k3s)" k3s
doctor_register boot-disk host "system disk" doctor_check_boot_disk
doctor_register memory-cgroup host "memory cgroup available" doctor_check_memory_cgroup doctor_fix_memory_cgroup "add cgroup_memory=1 cgroup_enable=memory to the kernel command line" k3s 1
doctor_register interface network "cluster interface is up with an address" doctor_check_iface doctor_fix_iface "bring the interface up" k3s
doctor_register api-port network "port 6443 answers locally" doctor_check_api_local doctor_fix_api_local "open the k3s ports in the firewall" k3s
doctor_register forward-policy network "iptables FORWARD policy lets traffic through" doctor_check_forward_policy doctor_fix_forward_policy "insert an ACCEPT rule ahead of the policy" k3s
doctor_register foreign-nft network "no other nftables table drops pod traffic" doctor_check_foreign_nft doctor_fix_foreign_nft "let only the pod network through those chains (persistent)" k3s
doctor_register nextcloud coexistence "no port 80/443 clash with Nextcloud" doctor_check_nextcloud doctor_fix_nextcloud "remove k3s's Traefik service" k3s
doctor_register server-reachable network "the k3s server is reachable" doctor_check_server_reachable
doctor_register api-healthz cluster "Kubernetes API responds" doctor_check_api_healthz
doctor_register node-ready cluster "this node is Ready" doctor_check_node_ready
doctor_register cluster-nodes cluster "all nodes Ready, none under pressure" doctor_check_cluster_nodes
doctor_register pods cluster "all pods running" doctor_check_pods

doctor_cmd_help() {
    cat <<'HELP'
Usage: nodeyard doctor [--fix] [--only ID[,ID]] [--strict] [--json]

Checks this machine for common problems: required tools, config, the k3s
service, kernel modules, IP forwarding, time sync, disk space, the memory
cgroup (Raspberry Pi), firewalls dropping pod traffic, port clashes, and on
a server the cluster's nodes and pods.

Without --fix it lists problems and, when run interactively, offers to fix
each one. Every fix can be undone with 'nodeyard undo'.

Options:
  --fix          Apply every available fix without asking
  --only IDS     Only run these checks (see the ids in --json output)
  --strict       Exit with status 1 if any problem is found
HELP
}

doctor_cmd() {
    ny_need_root
    local fix=0 strict=0 only=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --fix) fix=1; shift ;;
            --strict) strict=1; shift ;;
            --only) ny_need_value "$1" $#; only=",$2,"; shift 2 ;;
            *) ny_usage_error "Unknown option for 'doctor': $1" ;;
        esac
    done
    k3s_load_state
    ny_detect_all

    local entry id cat title check fixfn fixdesc feature reboot rc status last_cat=""
    local -a results=() pending=()
    local issues=0 warnings=0 fixed=0
    [[ "$NY_JSON" -eq 1 ]] || printf '%s %s\n\n' "$(ny_color bold "nodeyard doctor")" "$(ny_color dim "($(ny_self_name), role: ${K3S_ROLE:-none})")"
    for entry in "${DOCTOR_CHECKS[@]}"; do
        IFS=$'\x1f' read -r id cat title check fixfn fixdesc feature reboot <<<"$entry"
        [[ -z "$only" || "$only" == *",$id,"* ]] || continue
        DOCTOR_DETAIL=""
        rc=0
        "$check" || rc=$?
        case "$rc" in
            0) status="ok" ;;
            2) status="skip" ;;
            3) status="warn"; warnings=$((warnings + 1)) ;;
            *) status="issue"; issues=$((issues + 1)) ;;
        esac
        local was_fixed=0
        if [[ "$status" == issue && -n "$fixfn" && "$fix" -eq 1 ]]; then
            doctor_apply_fix "$title" "$fixfn" "$feature" "$reboot" && {
                was_fixed=1
                fixed=$((fixed + 1))
            }
        elif [[ "$status" == issue && -n "$fixfn" ]]; then
            pending+=("$entry")
        fi
        results+=("$(ny_json_obj "id=$id" "category=$cat" "title=$title" "status=$status" "detail?=$DOCTOR_DETAIL" \
            "fix?=$fixdesc" "fixed:=$(ny_json_bool "$was_fixed")" "needs_reboot:=$(ny_json_bool "$reboot")")")
        [[ "$NY_JSON" -eq 1 ]] && continue
        [[ "$status" == skip ]] && continue
        if [[ "$cat" != "$last_cat" ]]; then
            printf '%s\n' "$(ny_color dim "${cat^}")"
            last_cat="$cat"
        fi
        case "$status" in
            ok) printf '  %s %s%s\n' "$(ny_color green "$NY_SYM_OK")" "$title" "${DOCTOR_DETAIL:+ $(ny_color dim "- $DOCTOR_DETAIL")}" ;;
            warn) printf '  %s %s\n    %s\n' "$(ny_color yellow "$NY_SYM_WARN")" "$title" "$(ny_color dim "$DOCTOR_DETAIL")" ;;
            issue)
                printf '  %s %s%s\n' "$(ny_color red "$NY_SYM_FAIL")" "$title" "${DOCTOR_DETAIL:+ $(ny_color dim "- $DOCTOR_DETAIL")}"
                if [[ "$was_fixed" -eq 1 ]]; then
                    printf '    %s\n' "$(ny_color green "fixed: ${fixdesc}")"
                elif [[ -n "$fixdesc" ]]; then
                    printf '    %s %s\n' "$(ny_color bold "Fix:")" "$fixdesc"
                fi
                ;;
        esac
    done

    # Offer each fix in turn when run interactively without --fix.
    if [[ "$fix" -eq 0 && "${#pending[@]}" -gt 0 && "$NY_JSON" -eq 0 ]] && ny_ui_interactive && [[ "$NY_DRY_RUN" -eq 0 ]]; then
        printf '\n'
        for entry in "${pending[@]}"; do
            IFS=$'\x1f' read -r id cat title check fixfn fixdesc feature reboot <<<"$entry"
            if ny_ui_yesno "Fix \"${title}\" now? (${fixdesc}$([[ $reboot == 1 ]] && echo '; needs a reboot'))" y; then
                doctor_apply_fix "$title" "$fixfn" "$feature" "$reboot" && fixed=$((fixed + 1))
            fi
        done
    fi

    if [[ "$NY_JSON" -eq 1 ]]; then
        ny_json_out "$(ny_json_obj ok:=true "issues:=$issues" "warnings:=$warnings" "fixed:=$fixed" "checks:=$(ny_json_arr "${results[@]+"${results[@]}"}")" "plan:=$(ny_plan_json)")"
    else
        printf '\n'
        if [[ "$issues" -eq 0 ]]; then
            ny_ok "No problems found${warnings:+$([[ $warnings -gt 0 ]] && echo " (${warnings} warning(s) above)")}."
        else
            local left=$((issues - fixed))
            if ((left > 0)); then
                ny_warn "${issues} problem(s) found, ${fixed} fixed."
                [[ "$fix" -eq 0 ]] && ny_hint "Fix them all with: sudo nodeyard doctor --fix"
            else
                ny_ok "${issues} problem(s) found and fixed. Undo any fix with: sudo nodeyard undo --last"
            fi
        fi
    fi
    if [[ "$strict" -eq 1 && $((issues - fixed)) -gt 0 ]]; then
        return 1
    fi
    return 0
}

# doctor_apply_fix TITLE FIX_FN FEATURE REBOOT -- run one fix as FEATURE so
# uninstalling that feature also reverts it.
doctor_apply_fix() {
    local title="$1" fn="$2" feature="$3" reboot="$4" rc=0
    local NY_FEATURE="$feature"
    ny_step "Fixing: ${title}"
    "$fn" || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        ny_warn "Could not fix \"${title}\" automatically; see the message above."
        return 1
    fi
    [[ "$reboot" == 1 ]] && ny_warn "Reboot for this fix to take effect: sudo reboot"
    return 0
}
