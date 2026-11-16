# shellcheck shell=bash
# shellcheck disable=SC2034 # NY_YES is read by the shared confirmation helpers.
# relink: bring back a node that dropped off after its network cable was
# unplugged and plugged back in, without rebooting the server.
#
# A reboot of the server "fixes" that because it throws away state the kernel
# and k3s keep about each machine. This throws away only that state, for the
# missing nodes:
#   - the server's neighbour (ARP) entry for the node's address. One pinned
#     by hand (nud permanent) never updates by itself, and one learned from a
#     second machine using the same address points at the wrong machine;
#   - connection-tracking entries for that address (flows that died with the
#     cable);
#   - the pod network's (flannel VXLAN) forwarding entries for the node, set
#     again from what the node itself published;
# then, only if the node answers on the network (and its kubelet's port is
# open) but Kubernetes still hasn't heard from it, k3s restarts on this
# server (containers keep running). If
# no wired node answers at all while they are up another way, the server's
# own wired port is reset. Two machines answering for one address (an IP
# conflict) is reported, since nothing on this side can fix that.
#
# nodeyard-relink.timer runs 'relink --auto' every 20 s so this also happens
# on its own: on a server for nodes that stay NotReady; on a worker after its
# own cable comes back (it announces itself again, and restarts k3s-agent if
# the server answers but Kubernetes still doesn't).

ny_cmd "relink" relink_cmd "Nodes" "Reconnect nodes that dropped off after a cable was unplugged and plugged back in (no reboot)" relink json
ny_cmd "relink-watch install" relink_watch_install_cmd "Nodes" "Reconnect dropped nodes automatically (runs 'relink --auto' every 20 s)" relink
ny_cmd "relink-watch uninstall" relink_watch_uninstall_cmd "Nodes" "Stop reconnecting dropped nodes automatically" relink

RELINK_UNIT="/etc/systemd/system/nodeyard-relink.service"
RELINK_TIMER="/etc/systemd/system/nodeyard-relink.timer"
RELINK_INTERVAL=20
# --auto on a server: how long a node stays NotReady before it is touched, how
# often the light repair repeats, how long it must answer on the network
# while still NotReady before k3s restarts here, and how often that (and the
# wired-port reset) may happen at most.
RELINK_AUTO_GRACE=40
RELINK_AUTO_REPEAT=120
RELINK_AUTO_ANSWERING=150
RELINK_AUTO_RESTART_GAP=1800
RELINK_AUTO_LINK_GAP=3600
# --auto on a worker: how long the server may answer while Kubernetes doesn't
# before k3s-agent restarts, and how often that may happen at most.
RELINK_AGENT_STUCK=120
RELINK_AGENT_RESTART_GAP=600

declare -gA RL_IP=() RL_READY=() RL_SINCE=() RL_VTEP=() RL_PUBIP=() RL_CIDR=()
declare -gA RL_IFACE=() RL_BEFORE=() RL_PINNED=() RL_MACS=() RL_ANSWERS=() RL_KUBELET=() RL_DONE=() RL_ADVICE=()
RL_NODES=()
RL_RESTARTED=0

relink_cmd_help() {
    cat <<'HELP'
Usage: nodeyard relink [NODE...] [--no-restart] [--no-link-reset] [--wait SECONDS] [--json]
       nodeyard relink --auto

Run on the server (the control node). For each node that isn't Ready (or the
NODEs named), it clears what this server remembers about reaching it -- the
same things a reboot of the server clears -- without rebooting:

  - its neighbour (ARP) entry, including one pinned by hand,
  - connection-tracking entries for its address,
  - the pod network's forwarding entries for it (set again from the node's
    own published values).

Then it checks whether the node answers, and whether two machines answer for
its address (an IP conflict: give one of them another address). If a node
answers (and its kubelet listens on port 10250) but Kubernetes still hasn't
heard from it after --wait seconds (default 45), k3s restarts on this server
(running containers keep running).
If no wired node answers at all while they are up another way (Tailscale),
this server's own wired port is switched off and on again.

  --no-restart      never restart k3s on this server
  --no-link-reset   never reset this server's wired port
  --auto            what the timer runs: act only on nodes NotReady for a
                    while, rate-limited (see 'nodeyard relink-watch install')

On a worker, 'relink' (or --auto) checks its own cable and connection to the
server instead, and restarts k3s-agent if the server answers but Kubernetes
doesn't.
HELP
}

relink_watch_install_cmd_help() {
    cat <<'HELP'
Usage: nodeyard relink-watch install

Installs nodeyard-relink.timer, which runs 'nodeyard relink --auto' every 20
seconds, so a machine whose network cable was unplugged and plugged back in
comes back by itself:

  - on a server: a node NotReady for 40 s gets its stale network entries
    cleared (every 2 minutes at most); if it answers on the network but is
    still NotReady 2.5 minutes later, k3s restarts here (at most every 30
    minutes); if no wired node answers at all while they are up another way,
    the wired port is reset (at most hourly);
  - on a worker: when its cable comes back it announces itself to the server
    again; if the server answers but Kubernetes hasn't heard from this node
    for 2 minutes, k3s-agent restarts (at most every 10 minutes).

What it does goes to the journal: journalctl -t nodeyard-relink
HELP
}

relink_log() {
    ny_simulating && return 0
    logger -t nodeyard-relink "$*" 2>/dev/null || true
}

# relink_epoch RFC3339 -- seconds since 1970, or 0 if it can't be read.
relink_epoch() {
    local e
    e="$(date -d "$1" +%s 2>/dev/null)" || e=0
    [[ "$e" =~ ^[0-9]+$ ]] || e=0
    printf '%s\n' "$e"
}

# relink_load_nodes -- every node's readiness, address and pod-network values.
relink_load_nodes() {
    local out name ready since ips bd pub cidr ip
    RL_NODES=()
    out="$(kctl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.status.conditions[?(@.type=="Ready")].status}{"|"}{.status.conditions[?(@.type=="Ready")].lastTransitionTime}{"|"}{.status.addresses[?(@.type=="InternalIP")].address}{"|"}{.metadata.annotations.flannel\.alpha\.coreos\.com/backend-data}{"|"}{.metadata.annotations.flannel\.alpha\.coreos\.com/public-ip}{"|"}{.spec.podCIDR}{"\n"}{end}' 2>/dev/null)" || return 1
    while IFS='|' read -r name ready since ips bd pub cidr; do
        [[ -n "$name" ]] || continue
        RL_NODES+=("$name")
        RL_READY[$name]="$ready"
        RL_SINCE[$name]="$(relink_epoch "$since")"
        ip=""
        for ip in $ips; do
            [[ "$ip" == *.* ]] && break
        done
        [[ "$ip" == *.* ]] || ip=""
        RL_IP[$name]="$ip"
        RL_VTEP[$name]="$(sed -n 's/.*"VtepMAC":"\([^"]*\)".*/\1/p' <<<"$bd" | tr 'A-F' 'a-f')"
        RL_PUBIP[$name]="${pub:-$ip}"
        RL_CIDR[$name]="$cidr"
    done <<<"$out"
    [[ "${#RL_NODES[@]}" -gt 0 ]]
}

relink_is_self() {
    local self
    self="$(ny_self_name)"
    [[ "${1,,}" == "${self,,}" || "${1,,}" == "$(hostname 2>/dev/null | tr '[:upper:]' '[:lower:]')" ]]
}

# relink_route_dev ADDRESS -- the interface this machine reaches ADDRESS on.
relink_route_dev() {
    ip -4 route get "$1" 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -n1 || true
}

# relink_neigh ADDRESS IFACE -- this machine's neighbour entry ("" if none).
relink_neigh() {
    ip neigh show to "$1" dev "$2" 2>/dev/null | head -n1 || true
}

# relink_port_open ADDRESS PORT -- does something accept a TCP connection there?
relink_port_open() {
    have timeout || return 1
    timeout 2 bash -c "exec 3<>/dev/tcp/${1}/${2}" >/dev/null 2>&1
}

# relink_answers ADDRESS [IFACE] -- does it answer ping or the kubelet/agent/ssh ports?
relink_answers() {
    local addr="$1" iface="${2:-}" port
    [[ -n "$addr" ]] || return 1
    ping -c 2 -W 1 ${iface:+-I "$iface"} "$addr" >/dev/null 2>&1 && return 0
    for port in 10250 9093 22; do
        relink_port_open "$addr" "$port" && return 0
    done
    return 1
}

# relink_who IFACE ADDRESS -- hardware address of every machine answering for ADDRESS.
relink_who() {
    ny_simulating && return 0
    have python3 || return 0
    python3 -I "${NY_SHARE}/net/arpwho.py" "$1" "$2" 2 2>/dev/null || true
}

relink_note() {
    RL_DONE[$1]+="${RL_DONE[$1]:+$'\n'}$2"
}

# relink_clear NODE -- the light repair: forget the stale entries for NODE.
# Fills RL_IFACE/RL_BEFORE/RL_PINNED/RL_MACS/RL_ANSWERS/RL_DONE for it.
relink_clear() {
    local node="$1" ip iface before vtep pub cidr gw cur rt flag out cnt=0
    ip="${RL_IP[$node]:-}"
    RL_DONE[$node]="" RL_MACS[$node]="" RL_ANSWERS[$node]=0 RL_KUBELET[$node]=0 RL_PINNED[$node]=0 RL_ADVICE[$node]=""
    if [[ -z "$ip" ]]; then
        RL_ADVICE[$node]="Kubernetes has no address for it."
        RL_IFACE[$node]="" RL_BEFORE[$node]=""
        return 0
    fi
    iface="$(relink_route_dev "$ip")"
    RL_IFACE[$node]="$iface"
    before=""
    [[ -n "$iface" ]] && before="$(relink_neigh "$ip" "$iface")"
    RL_BEFORE[$node]="$before"
    if [[ -n "$before" ]]; then
        [[ "$before" == *PERMANENT* ]] && RL_PINNED[$node]=1
        ny_run ip neigh del "$ip" dev "$iface" >/dev/null 2>&1 || true
        if [[ "${RL_PINNED[$node]}" -eq 1 ]]; then
            relink_note "$node" "removed a hand-pinned network entry for ${ip} (${before}); a pinned entry never updates, so the machine stayed unreachable"
        else
            relink_note "$node" "cleared this server's network entry for ${ip} (was: ${before#"$ip "})"
        fi
    fi
    if have conntrack; then
        for flag in -s -d; do
            out="$(ny_run conntrack -D "$flag" "$ip" 2>&1 >/dev/null || true)"
            out="$(sed -n 's/.*[^0-9]\([0-9][0-9]*\) flow entr.*/\1/p' <<<"$out" | head -n1)"
            cnt=$((cnt + ${out:-0}))
        done
        if ((cnt > 0)); then
            relink_note "$node" "dropped ${cnt} old connection-tracking entr$([[ $cnt -eq 1 ]] && echo y || echo ies) for ${ip}"
        fi
    fi
    # The pod network: flannel's forwarding entry and neighbour for this
    # node's VXLAN endpoint, set again from the values the node published.
    vtep="${RL_VTEP[$node]:-}" pub="${RL_PUBIP[$node]:-}" cidr="${RL_CIDR[$node]:-}"
    if [[ -n "$vtep" && -n "$pub" && -e "$(ny_path /sys/class/net/flannel.1)" ]] && have bridge; then
        cur="$(bridge fdb show dev flannel.1 2>/dev/null | awk -v m="$vtep" 'tolower($1) == m' | head -n1 || true)"
        if [[ "$cur" != *"dst ${pub} "* && "$cur" != *"dst ${pub}" ]]; then
            ny_run bridge fdb replace "$vtep" dev flannel.1 dst "$pub" self permanent >/dev/null 2>&1 &&
                relink_note "$node" "re-set its pod-network forwarding entry (${vtep} -> ${pub})"
        fi
        if [[ -n "$cidr" && "$cidr" == *.*/* ]]; then
            gw="${cidr%/*}"
            cur="$(relink_neigh "$gw" flannel.1)"
            if [[ "${cur,,}" != *"lladdr ${vtep}"* ]]; then
                ny_run ip neigh replace "$gw" lladdr "$vtep" dev flannel.1 nud permanent >/dev/null 2>&1 &&
                    relink_note "$node" "re-set its pod-network neighbour entry (${gw} -> ${vtep})"
            fi
            rt="$(ip route show "$cidr" 2>/dev/null || true)"
            if [[ "$rt" != *"dev flannel.1"* ]]; then
                ny_run ip route replace "$cidr" via "$gw" dev flannel.1 onlink >/dev/null 2>&1 &&
                    relink_note "$node" "re-added the route to its pods (${cidr})"
            fi
        fi
    fi
    # Who answers now that the old entry is gone?
    if [[ -n "$iface" ]]; then
        RL_MACS[$node]="$(relink_who "$iface" "$ip" | paste -sd' ' - || true)"
    fi
    if relink_answers "$ip" "$iface"; then
        RL_ANSWERS[$node]=1
        # Its kubelet listening means k3s-agent runs there, so a restart here
        # (which makes every agent connect again) can help; if it doesn't
        # listen, the agent on the node is stopped and restarting here won't.
        if relink_port_open "$ip" 10250; then
            RL_KUBELET[$node]=1
        fi
    fi
    if [[ "${RL_MACS[$node]}" == *" "* ]]; then
        RL_ADVICE[$node]="Two or more machines answer for ${ip} (${RL_MACS[$node]// /, }): an IP address conflict. Give the other machine its own address; until then this node will keep dropping out."
        relink_log "${node}: IP conflict on ${ip}: ${RL_MACS[$node]}"
    fi
    return 0
}

# relink_wait_ready SECONDS NODE... -- wait until each NODE is Ready; sets RL_READY.
relink_wait_ready() {
    local limit="$1" t0=$SECONDS n left
    shift
    if ny_simulating; then
        # Demo and --dry-run: the nodes come back once k3s "restarted" here.
        if [[ "$RL_RESTARTED" -eq 1 ]]; then
            for n in "$@"; do RL_READY[$n]=True; done
            return 0
        fi
        return 1
    fi
    while :; do
        relink_load_nodes >/dev/null 2>&1 || true
        left=0
        for n in "$@"; do
            [[ "${RL_READY[$n]:-}" == True ]] || left=$((left + 1))
        done
        if ((left == 0)); then
            return 0
        fi
        [[ $((SECONDS - t0)) -lt "$limit" ]] || return 1
        sleep 4
    done
}

# relink_restart_server -- restart k3s here and wait for the API to answer again.
relink_restart_server() {
    local svc t0
    svc="$(k3s_service_name)"
    ny_step "Restarting ${svc} on this server (running containers keep running)"
    relink_log "restarting ${svc}: a node answers on the network but Kubernetes hasn't heard from it"
    ny_run systemctl restart "$svc" || {
        ny_warn "Couldn't restart ${svc}."
        return 1
    }
    RL_RESTARTED=1
    ny_simulating && return 0
    t0=$SECONDS
    until kctl get nodes >/dev/null 2>&1; do
        [[ $((SECONDS - t0)) -lt 180 ]] || {
            ny_warn "Kubernetes didn't answer again within 180 s."
            return 1
        }
        sleep 3
    done
    return 0
}

# relink_reset_link IFACE -- switch this machine's wired port off and on, the
# part of a reboot that resets the network card. Puts its addresses back if
# the network manager doesn't.
relink_reset_link() {
    local iface="$1" t0 now a r
    local -a addrs=() routes=() words=()
    mapfile -t addrs < <(ip -4 -o addr show dev "$iface" scope global 2>/dev/null | awk '{print $4}')
    mapfile -t routes < <(ip -4 route show dev "$iface" 2>/dev/null | sed 's/ linkdown//g')
    ny_step "Switching ${iface} off and on again (no wired node answers)"
    relink_log "resetting ${iface}: no wired node answers while they are up another way"
    ny_run ip link set dev "$iface" down || return 1
    ny_simulating || sleep 3
    ny_run ip link set dev "$iface" up || return 1
    ny_simulating && return 0
    t0=$SECONDS
    until [[ "$(cat "/sys/class/net/${iface}/carrier" 2>/dev/null)" == 1 ]]; do
        [[ $((SECONDS - t0)) -lt 20 ]] || break
        sleep 1
    done
    sleep 5
    now="$(ip -4 -o addr show dev "$iface" 2>/dev/null || true)"
    for a in "${addrs[@]+"${addrs[@]}"}"; do
        if [[ "$now" != *" ${a} "* ]]; then
            ny_run ip addr add "$a" dev "$iface" 2>/dev/null || true
        fi
    done
    # Routes through the port (a default route, say) go when it goes down.
    now="$(ip -4 route show dev "$iface" 2>/dev/null | sed 's/ linkdown//g' || true)"
    for r in "${routes[@]+"${routes[@]}"}"; do
        [[ -n "$r" && $'\n'"$now"$'\n' != *$'\n'"$r"$'\n'* ]] || continue
        read -r -a words <<<"$r"
        ny_run ip route replace "${words[@]}" dev "$iface" 2>/dev/null || true
    done
    return 0
}

# relink_wired_dead IFACE NODE... -- true if none of the nodes on IFACE answer
# there, yet at least one of them is up another way (so it's this side).
relink_wired_dead() {
    local iface="$1" n any_other=0
    shift
    [[ -n "$iface" ]] || return 1
    for n in "${RL_NODES[@]}"; do
        relink_is_self "$n" && continue
        [[ -n "${RL_IP[$n]:-}" && "$(relink_route_dev "${RL_IP[$n]}")" == "$iface" ]] || continue
        if [[ -n "${RL_ANSWERS[$n]+x}" ]]; then
            [[ "${RL_ANSWERS[$n]}" -eq 1 ]] && return 1
        else
            relink_answers "${RL_IP[$n]}" "$iface" && return 1
        fi
    done
    for n in "$@"; do
        cluster_node_reachable "$n" && any_other=1 && break
    done
    ((any_other))
}

relink_lock() {
    local wait="$1" lf
    ny_simulating && return 0
    have flock || return 0
    mkdir -p -- "$NY_STATE" 2>/dev/null || return 0
    lf="${NY_STATE}/relink.lock"
    exec 8>"$lf" || return 0
    flock -w "$wait" 8
}

relink_cmd() {
    ny_need_root
    local auto=0 restart=1 linkreset=1 wait=45
    local -a named=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --auto) auto=1; shift ;;
            --no-restart) restart=0; shift ;;
            --no-link-reset) linkreset=0; shift ;;
            --wait)
                ny_need_value "$1" $#
                ny_valid_int "$2" 0 900 || ny_usage_error "--wait: ${NY_VALID_MSG}"
                wait="$2"
                shift 2
                ;;
            --yes | -y) NY_YES=1; shift ;;
            -*) ny_usage_error "Unknown option for 'relink': $1" ;;
            *)
                ny_valid_hostname "$1" || ny_usage_error "$NY_VALID_MSG"
                named+=("$1")
                shift
                ;;
        esac
    done
    k3s_load_state
    if [[ "$K3S_ROLE" == agent ]]; then
        [[ "${#named[@]}" -eq 0 ]] || ny_usage_error "On a worker, relink checks this machine only; run it on the server to name nodes."
        if [[ "$auto" -eq 1 ]]; then
            relink_lock 0 || return 0
        fi
        relink_agent "$auto"
        return 0
    fi
    if [[ "$auto" -eq 1 ]]; then
        relink_lock 0 || return 0
        relink_server_auto
        return 0
    fi
    ny_need_kube
    relink_load_nodes || ny_die "Couldn't read the node list: is k3s running here?" "Check: sudo nodeyard doctor" "$NY_E_PRECONDITION"
    local n line
    local -a targets=()
    RL_RESTARTED=0
    if [[ "${#named[@]}" -gt 0 ]]; then
        for n in "${named[@]}"; do
            [[ -n "${RL_READY[$n]+x}" ]] || ny_die "There is no node called ${n}." "List them with: nodeyard status" "$NY_E_USAGE"
            relink_is_self "$n" && ny_die "${n} is this server; relink reconnects other nodes to it." "" "$NY_E_USAGE"
            targets+=("$n")
        done
    else
        for n in "${RL_NODES[@]}"; do
            relink_is_self "$n" && continue
            [[ "${RL_READY[$n]}" == True ]] || targets+=("$n")
        done
    fi
    if [[ "${#targets[@]}" -eq 0 ]]; then
        ny_ok "Every node is Ready: nothing to reconnect."
        [[ "$NY_JSON" -eq 1 ]] && ny_json_out "$(ny_json_obj ok:=true "nodes:=[]" restarted_server:=false link_reset:=false)"
        return 0
    fi
    ny_confirm "Reconnect $(ny_join ', ' "${targets[@]}")? This clears this server's old network entries for them, and restarts k3s here only if they answer but still don't report in (containers keep running)." y || {
        ny_info "Cancelled."
        return 0
    }
    relink_lock 120 || ny_warn "Another relink is still running; going ahead anyway."

    local restarted=0 linked=0
    local -a answering=() silent=()
    for n in "${targets[@]}"; do
        ny_step "${n} (${RL_IP[$n]:-no address})"
        relink_clear "$n"
        if [[ -n "${RL_DONE[$n]}" ]]; then
            while IFS= read -r line; do ny_hint "$line"; done <<<"${RL_DONE[$n]}"
        fi
        if [[ "${RL_ANSWERS[$n]}" -eq 1 ]]; then
            ny_info "  ${n} answers on ${RL_IFACE[$n]:-the network}."
            answering+=("$n")
        else
            ny_warn "${n} doesn't answer at ${RL_IP[$n]:-?}${RL_IFACE[$n]:+ on ${RL_IFACE[$n]}}."
            silent+=("$n")
        fi
        if [[ -n "${RL_ADVICE[$n]}" ]]; then
            ny_warn "${RL_ADVICE[$n]}"
        fi
    done

    # Nothing on this server's wired port answers, yet the machines are up:
    # the port itself is stuck (the part of a reboot that resets the card).
    if [[ "${#silent[@]}" -gt 0 && "${#answering[@]}" -eq 0 && "$linkreset" -eq 1 ]]; then
        local dev="${RL_IFACE[${silent[0]}]:-}"
        if relink_wired_dead "$dev" "${silent[@]}"; then
            if relink_reset_link "$dev"; then
                linked=1
                answering=() silent=()
                for n in "${targets[@]}"; do
                    relink_clear "$n"
                    relink_note "$n" "switched this server's wired port (${dev}) off and on: nothing answered on it"
                    if [[ "${RL_ANSWERS[$n]}" -eq 1 ]]; then answering+=("$n"); else silent+=("$n"); fi
                done
            fi
        fi
    fi

    local -a waiting=()
    if [[ "${#answering[@]}" -gt 0 ]]; then
        ny_step "Waiting up to ${wait} s for $(ny_join ', ' "${answering[@]}") to report in"
        relink_wait_ready "$wait" "${answering[@]}" || true
        for n in "${answering[@]}"; do
            if [[ "${RL_READY[$n]}" != True && "${RL_KUBELET[$n]:-0}" -eq 1 ]]; then
                waiting+=("$n")
            fi
        done
        if [[ "${#waiting[@]}" -gt 0 && "$restart" -eq 1 ]]; then
            ny_info "Kubernetes still hasn't heard from $(ny_join ', ' "${waiting[@]}"), though the network answers."
            if relink_restart_server; then
                restarted=1
                for n in "${waiting[@]}"; do
                    relink_note "$n" "restarted k3s on this server: the node answered, but Kubernetes still hadn't heard from it"
                done
                relink_wait_ready 120 "${waiting[@]}" || true
            fi
        fi
    fi

    # Report.
    local -a results=() still=()
    for n in "${targets[@]}"; do
        local ready=0 advice="${RL_ADVICE[$n]:-}"
        [[ "${RL_READY[$n]}" == True ]] && ready=1
        if ((ready)); then
            ny_ok "${n} is Ready again."
        else
            still+=("$n")
            if [[ -z "$advice" ]]; then
                if [[ "${RL_ANSWERS[$n]}" -eq 1 && "${RL_KUBELET[$n]:-0}" -eq 0 ]]; then
                    advice="It answers on the network, but nothing listens on its kubelet port (10250): k3s-agent on it is stopped or still starting. On ${n}: sudo systemctl restart k3s-agent"
                elif [[ "${RL_ANSWERS[$n]}" -eq 1 ]]; then
                    advice="It answers on the network, but Kubernetes still hasn't heard from it, so its k3s-agent is probably stuck. On ${n}: sudo systemctl restart k3s-agent (a worker with 'nodeyard relink-watch install' does that by itself)."
                elif cluster_node_reachable "$n"; then
                    advice="It is up (it answers another way, e.g. over Tailscale) but not at ${RL_IP[$n]:-its address}. Check its cable and switch port, and that it still has ${RL_IP[$n]:-its address} on its wired port (on it: ip -4 addr)."
                else
                    advice="It doesn't answer at all. Check that it is switched on and its cable is in, then run this again."
                fi
            fi
            ny_warn "${n} isn't Ready yet. ${advice}"
        fi
        local -a macs=()
        read -r -a macs <<<"${RL_MACS[$n]:-}" || true
        results+=("$(ny_json_obj "name=$n" "ip?=${RL_IP[$n]:-}" "interface?=${RL_IFACE[$n]:-}" "before?=${RL_BEFORE[$n]:-}" \
            "pinned:=$(ny_json_bool "${RL_PINNED[$n]:-0}")" "answers:=$(ny_json_bool "${RL_ANSWERS[$n]:-0}")" "kubelet:=$(ny_json_bool "${RL_KUBELET[$n]:-0}")" \
            "answered_by:=$(ny_json_arr_str "${macs[@]+"${macs[@]}"}")" "conflict:=$(ny_json_bool "$([[ ${#macs[@]} -gt 1 ]] && echo 1 || echo 0)")" \
            "ready:=$(ny_json_bool "$ready")" "done:=$(ny_json_arr_str_lines "${RL_DONE[$n]:-}")" "advice?=$([[ $ready -eq 1 ]] || echo "$advice")")")
    done
    if [[ "$NY_JSON" -eq 1 ]]; then
        ny_json_out "$(ny_json_obj ok:=true "nodes:=$(ny_json_arr "${results[@]}")" "restarted_server:=$(ny_json_bool "$restarted")" "link_reset:=$(ny_json_bool "$linked")" "plan:=$(ny_plan_json)")"
    elif [[ "${#still[@]}" -eq 0 ]]; then
        ny_ok "Reconnected: $(ny_join ', ' "${targets[@]}")."
    fi
    return 0
}

# ny_json_arr_str_lines TEXT -- a JSON array of TEXT's non-empty lines.
ny_json_arr_str_lines() {
    local -a lines=()
    local l
    while IFS= read -r l; do
        [[ -n "$l" ]] && lines+=("$l")
    done <<<"$1"
    ny_json_arr_str "${lines[@]+"${lines[@]}"}"
}

# --- automatic (the timer) -----------------------------------------------------

# relink_state_get FILE KEY -- one value from a small KEY=VALUE state file.
relink_state_get() {
    [[ -r "$1" ]] || return 0
    sed -n "s/^$2=//p" "$1" | head -n1
}

# relink_state_put FILE KEY=VALUE... -- rewrite the state file with these values.
relink_state_put() {
    local f="$1" kv
    shift
    mkdir -p -- "$(dirname -- "$f")" 2>/dev/null || return 0
    : >"${f}.new" || return 0
    for kv in "$@"; do
        printf '%s\n' "$kv" >>"${f}.new"
    done
    mv -f -- "${f}.new" "$f"
}

relink_server_auto() {
    kctl_available || return 0
    relink_load_nodes || return 0
    local dir="${NY_STATE}/relink" now n f down light answering
    local last_restart last_link
    local -a due=() silent=() keep=()
    now="$(date +%s)"
    mkdir -p -- "$dir" 2>/dev/null || return 0
    last_restart="$(relink_state_get "${dir}/server" restart)"
    last_link="$(relink_state_get "${dir}/server" link)"
    for n in "${RL_NODES[@]}"; do
        relink_is_self "$n" && continue
        f="${dir}/node-${n}"
        keep+=("node-${n}")
        if [[ "${RL_READY[$n]}" == True ]]; then
            if [[ -e "$f" ]]; then
                relink_log "${n} is Ready again"
                rm -f -- "$f"
            fi
            continue
        fi
        down=$((now - ${RL_SINCE[$n]:-0}))
        ((down >= RELINK_AUTO_GRACE)) || continue
        light="$(relink_state_get "$f" light)"
        answering="$(relink_state_get "$f" answering)"
        if ((now - ${light:-0} >= RELINK_AUTO_REPEAT)); then
            relink_clear "$n"
            light="$now"
            if [[ "${RL_ANSWERS[$n]:-0}" -eq 1 && "${RL_KUBELET[$n]:-0}" -eq 1 ]]; then
                relink_log "${n} NotReady for ${down}s: cleared stale network entries; it answers on ${RL_IFACE[$n]:-the network}"
                answering="${answering:-$now}"
            elif [[ "${RL_ANSWERS[$n]:-0}" -eq 1 ]]; then
                relink_log "${n} NotReady for ${down}s: cleared stale network entries; it answers, but its kubelet doesn't (k3s-agent stopped there?)"
                answering=""
            else
                relink_log "${n} NotReady for ${down}s: cleared stale network entries; it doesn't answer at ${RL_IP[$n]:-?}"
                answering=""
                silent+=("$n")
            fi
        fi
        relink_state_put "$f" "light=${light}" "answering=${answering}"
        if [[ -n "$answering" ]] && ((now - answering >= RELINK_AUTO_ANSWERING)); then
            due+=("$n")
        fi
    done
    # Forget nodes that were removed.
    for f in "$dir"/node-*; do
        [[ -e "$f" ]] || continue
        ny_in_list "${f##*/}" "${keep[@]+"${keep[@]}"}" || rm -f -- "$f"
    done
    if [[ "${#due[@]}" -gt 0 ]] && ((now - ${last_restart:-0} >= RELINK_AUTO_RESTART_GAP)); then
        relink_log "still NotReady although they answer: $(ny_join ', ' "${due[@]}"); restarting k3s here"
        relink_restart_server && last_restart="$now"
        relink_state_put "${dir}/server" "restart=${last_restart}" "link=${last_link}"
        return 0
    fi
    if [[ "${#silent[@]}" -gt 0 ]] && ((now - ${last_link:-0} >= RELINK_AUTO_LINK_GAP)); then
        local dev="${RL_IFACE[${silent[0]}]:-}"
        if relink_wired_dead "$dev" "${silent[@]}"; then
            relink_log "nothing answers on ${dev}: switching it off and on"
            relink_reset_link "$dev" && last_link="$now"
            relink_state_put "${dir}/server" "restart=${last_restart}" "link=${last_link}"
        fi
    fi
    return 0
}

# relink_agent AUTO -- on a worker: re-announce after the cable comes back, and
# restart k3s-agent if the server answers but Kubernetes doesn't.
relink_agent() {
    local auto="$1" server host port iface now f link was stuck last code ok=1 self
    server="${K3S_SERVER_URL:-}"
    [[ -n "$server" ]] || {
        [[ "$auto" -eq 1 ]] || ny_info "This worker doesn't know its server's address; nothing to check."
        return 0
    }
    host="${server#*://}"
    host="${host%%/*}"
    port="${host##*:}"
    [[ "$port" == "$host" ]] && port=6443
    host="${host%:*}"
    iface="${K3S_IFACE:-$(relink_route_dev "$host")}"
    f="${NY_STATE}/relink/agent"
    now="$(date +%s)"
    was="$(relink_state_get "$f" link)"
    stuck="$(relink_state_get "$f" stuck)"
    last="$(relink_state_get "$f" restart)"
    link=up
    if [[ -n "$iface" ]]; then
        [[ "$(cat "$(ny_path "/sys/class/net/${iface}/carrier")" 2>/dev/null)" == 0 ]] && link=down
        [[ -n "$(ny_iface_ipv4 "$iface" 2>/dev/null)" ]] || link=down
    fi
    if [[ "$link" == down ]]; then
        [[ "$was" == down ]] || relink_log "${iface}: cable out or no address"
        [[ "$auto" -eq 1 ]] || ny_warn "${iface} has no link or no address: check the cable."
        relink_state_put "$f" "link=down" "stuck=" "restart=${last}"
        return 0
    fi
    if [[ "$was" == down || "$auto" -eq 0 ]]; then
        # Back on the wire: say hello to the server so it updates its entry
        # for this machine, and drop flows that died with the cable.
        [[ "$was" == down ]] && relink_log "${iface}: cable back; announcing this machine to ${host}"
        ny_run ip neigh del "$host" dev "$iface" >/dev/null 2>&1 || true
        have conntrack && { ny_run conntrack -D -d "$host" >/dev/null 2>&1 || true; }
        if have arping; then
            ny_run arping -U -c 2 -I "$iface" "$(ny_iface_ipv4 "$iface")" >/dev/null 2>&1 || true
        fi
        ny_simulating || ping -c 2 -W 1 -I "$iface" "$host" >/dev/null 2>&1 || true
    fi
    # Does Kubernetes answer, and does it see this node as Ready?
    if ny_simulating; then
        code=200
    else
        code="$(curl -ksS --connect-timeout 3 --max-time 5 -o /dev/null -w '%{http_code}' "https://${host}:${port}/ping" 2>/dev/null || true)"
    fi
    [[ "$code" =~ ^[1-5][0-9][0-9]$ ]] || ok=0
    if ((ok)); then
        local kc st
        kc="$(ny_path /var/lib/rancher/k3s/agent/kubelet.kubeconfig)"
        self="$(hostname 2>/dev/null | tr '[:upper:]' '[:lower:]')"
        if [[ -r "$kc" ]] && ny_k3s_installed; then
            st="$(KUBECONFIG="$kc" "$(ny_path "$NY_K3S_BIN")" kubectl get node "$self" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
            [[ -z "$st" || "$st" == True ]] || ok=0
        fi
    fi
    if ((ok)); then
        [[ -n "$stuck" ]] && relink_log "Kubernetes hears from this node again"
        [[ "$auto" -eq 1 ]] || ny_ok "This node reaches the server at ${host} and Kubernetes hears from it."
        relink_state_put "$f" "link=up" "stuck=" "restart=${last}"
        return 0
    fi
    stuck="${stuck:-$now}"
    relink_state_put "$f" "link=up" "stuck=${stuck}" "restart=${last}"
    # Restart only when the server itself answers (so it's this side that's
    # stuck), it's been a while, and not too often.
    if ! relink_answers "$host" "$iface"; then
        [[ "$auto" -eq 1 ]] || ny_warn "The server ${host} doesn't answer from here (on ${iface:-the network}): check the cable and switch."
        return 0
    fi
    if [[ "$auto" -eq 1 ]] && ((now - stuck < RELINK_AGENT_STUCK || now - ${last:-0} < RELINK_AGENT_RESTART_GAP)); then
        return 0
    fi
    relink_log "the server answers but Kubernetes hasn't heard from this node since $((now - stuck))s ago: restarting k3s-agent"
    ny_step "Restarting $(k3s_service_name) (the server answers, Kubernetes doesn't)"
    ny_run systemctl restart "$(k3s_service_name)" || ny_warn "Couldn't restart $(k3s_service_name)."
    relink_state_put "$f" "link=up" "stuck=${stuck}" "restart=${now}"
    return 0
}

# --- the timer -----------------------------------------------------------------

relink_watch_installed() {
    [[ -f "$(ny_path "$RELINK_TIMER")" ]] || return 1
    ny_simulating && return 0
    systemctl is-enabled --quiet nodeyard-relink.timer 2>/dev/null
}

# relink_watch_write -- the unit files and enable the timer (no messages).
relink_watch_write() {
    ny_write_file "$RELINK_UNIT" <<UNIT
[Unit]
Description=nodeyard: reconnect nodes that dropped off the network
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/nodeyard relink --auto
TimeoutStartSec=600
UNIT
    ny_write_file "$RELINK_TIMER" <<UNIT
[Unit]
Description=nodeyard: reconnect nodes that dropped off the network (every ${RELINK_INTERVAL} s)

[Timer]
OnBootSec=60s
OnUnitInactiveSec=${RELINK_INTERVAL}s
AccuracySec=2s

[Install]
WantedBy=timers.target
UNIT
    ny_service_daemon_reload
    ny_service_enable nodeyard-relink.timer --now
}

relink_watch_install_cmd() {
    ny_need_root
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    ny_systemd_available || ny_die "This needs systemd." "" "$NY_E_PRECONDITION"
    relink_watch_write
    ny_ok "Dropped nodes now reconnect by themselves (every ${RELINK_INTERVAL} s; see journalctl -t nodeyard-relink)."
}

relink_watch_uninstall_cmd() {
    ny_need_root
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    ny_run systemctl disable --now nodeyard-relink.timer >/dev/null 2>&1 || true
    ny_remove_file "$RELINK_TIMER"
    ny_remove_file "$RELINK_UNIT"
    ny_service_daemon_reload
    ny_simulating || rm -rf -- "${NY_STATE}/relink.lock" "${NY_STATE}/relink"
    ny_ok "Dropped nodes are no longer reconnected automatically (run 'nodeyard relink' when one goes missing)."
}
