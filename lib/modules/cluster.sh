# shellcheck shell=bash
# Multi-node operations: adding a machine over SSH, removing a node, the
# control-plane watchdog, and the pod network test (ported from k3s-manager).

ny_cmd "add-node" cluster_add_node_cmd "Nodes" "Install nodeyard on another machine over SSH and join it to this cluster" nodes
ny_cmd "remove-node" cluster_remove_node_cmd "Nodes" "Drain a node and remove it from the cluster" nodes
ny_cmd "nettest" cluster_nettest_cmd "Health" "Test pod-to-pod, DNS and pod-to-node traffic on every node" nettest
ny_cmd_alias "net-test" "nettest"
ny_cmd "watchdog-install" cluster_watchdog_install_cmd "Health" "Check the control plane on a timer and log when it is unreachable" watchdog
ny_cmd "watchdog-uninstall" cluster_watchdog_uninstall_cmd "Health" "Remove the control-plane watchdog" watchdog
ny_cmd "watchdog-check" cluster_watchdog_check_cmd "Health" "Run one watchdog check (used by the timer)" watchdog hidden
ny_cmd "promote" cluster_promote_cmd "Health" "Explain what to do when the control plane is down" watchdog

cluster_add_node_cmd_help() {
    cat <<'HELP'
Usage: nodeyard add-node worker|master --ssh USER@HOST [options]

Run this on a k3s server. It copies nodeyard to another Linux machine over
SSH, installs it there and joins it to this cluster. The join token is
copied as a root-only file; it never appears on a command line.

Without --ssh it prints the commands to run on the other machine instead.

After logging in, everything on the other machine runs as root in one
step: through sudo if the user can use it, otherwise through su (you type
the root password). Temporary files are removed afterwards either way.

Options:
  --ssh USER@HOST          The machine to set up (you'll be asked for its SSH password if needed)
  --port N                 SSH port (default 22)
  --become auto|sudo|su    How to become root there (default auto: sudo if this user may use it, else su)
  --interface IFACE        The other machine's cluster network interface (default: automatic)
  --version V              k3s version for a worker (default: this server's version)
  --host-key FINGERPRINT   Expected SSH host key (SHA256:...), for unattended runs
HELP
}

cluster_add_node_cmd() {
    ny_need_root
    local kind="${1:-}"
    [[ $# -gt 0 ]] && shift
    local target="" port=22 iface="" version="" hostkey="" become="auto"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --ssh)
                ny_need_value "$1" $#
                target="$2"
                shift 2
                ;;
            --port)
                ny_need_value "$1" $#
                ny_valid_port "$2" || ny_usage_error "$NY_VALID_MSG"
                port="$2"
                shift 2
                ;;
            --interface)
                ny_need_value "$1" $#
                ny_valid_iface "$2" || ny_usage_error "$NY_VALID_MSG"
                iface="$2"
                shift 2
                ;;
            --version)
                ny_need_value "$1" $#
                ny_valid_k3s_version "$2" || ny_usage_error "$NY_VALID_MSG"
                version="$2"
                shift 2
                ;;
            --host-key)
                ny_need_value "$1" $#
                hostkey="$2"
                shift 2
                ;;
            --become)
                ny_need_value "$1" $#
                ny_valid_enum "$2" auto sudo su || ny_usage_error "--become: ${NY_VALID_MSG}"
                become="$2"
                shift 2
                ;;
            *) ny_usage_error "Unknown option for 'add-node': $1" ;;
        esac
    done
    local role_cmd
    case "$kind" in
        worker | agent) role_cmd="worker" ;;
        master | server) role_cmd="join-master" ;;
        *) ny_usage_error "Say what to add the machine as: worker or master." "nodeyard add-node worker|master --ssh user@host" ;;
    esac
    [[ -r "$(ny_path "$NY_K3S_TOKEN_FILE")" ]] || ny_die "This must be run on a k3s server." "Run it on the machine where you ran 'install master'." "$NY_E_PRECONDITION"

    k3s_load_state
    # Join via the address the server was installed on, not the default
    # route: on multi-homed nodes the default route is usually Wi-Fi.
    local master_ip="$K3S_NODE_IP"
    if [[ -z "$master_ip" ]] && kctl_available; then
        master_ip="$(kctl get node "$(hostname)" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)"
    fi
    [[ -n "$master_ip" ]] || master_ip="$(ny_best_ip)"
    [[ -n "$master_ip" ]] || ny_die "Could not work out this server's address." "Set it in the config: nodeyard config set node.$(ny_self_name).node-ip ADDRESS"
    local server="https://${master_ip}:6443"
    # A node must never run a newer k3s than the servers.
    [[ -n "$version" ]] || version="$(k3s_version)"

    local -a join=(install "$role_cmd" --server "$server" --token-file TOKENFILE --yes)
    [[ -n "$iface" ]] && join+=(--interface "$iface")
    [[ -n "$version" && "$role_cmd" == worker ]] && join+=(--version "$version")

    if [[ -z "$target" ]]; then
        printf 'On the other machine:\n'
        printf '  1. Install nodeyard:  curl -fsSL https://raw.githubusercontent.com/%s/main/install.sh | sudo bash\n' "$NY_REPO"
        printf '  2. Save the token:    sudo install -m 600 /dev/stdin /root/k3s-token   (paste the output of "sudo nodeyard token --reveal" here, then Ctrl-D)\n'
        printf '  3. Join:              sudo nodeyard %s\n' "$(ny_quote_cmd "${join[@]}" | sed 's#TOKENFILE#/root/k3s-token#')"
        printf '\nOr let nodeyard do all of that from here: sudo nodeyard add-node %s --ssh user@<address>\n' "$kind"
        return 0
    fi

    ny_deps_ensure_feature ssh
    ny_ssh_split "$target"
    local host="$NY_SSH_HOST"
    ny_valid_host "$host" || ny_usage_error "$NY_VALID_MSG"
    ny_ssh_preflight "$host" "$port"
    ny_ssh_trust_host "$host" "$port" "$hostkey"
    ny_ssh_opts "$host" "$port"

    ny_info "Will install nodeyard ${NY_VERSION} on ${target} and join it to ${server} as a ${kind}."
    ny_confirm "Set up ${target} now?" y || {
        ny_info "Cancelled; nothing was changed."
        return 0
    }
    if ny_simulating; then
        ny_plan_add remote "Install nodeyard on ${target} and run: nodeyard $(ny_quote_cmd "${join[@]}")" "host=$host"
        [[ "$NY_DRY_RUN" -eq 1 ]] && ny_info "[dry-run] would copy nodeyard and the join token to ${target}, then join it as a ${kind}."
        return 0
    fi

    ny_step "Connecting to ${target} (enter its SSH password if asked)"
    ssh "${NY_SSH_OPTS[@]}" -fN "$target" || ny_die "Could not connect to ${target}." "Check the user name and password/key: ssh -p ${port} ${target}"

    # Look before changing anything: who we are there, whether sudo is
    # usable, and which network interfaces it has.
    local probe
    # shellcheck disable=SC2029 # the probe script is meant to run remotely
    probe="$(ssh "${NY_SSH_OPTS[@]}" "$target" "$(cluster_probe_script)" 2>/dev/null || true)"
    local remote_uid remote_sudo remote_ifaces
    remote_uid="$(sed -n 1p <<<"$probe")"
    remote_sudo="$(sed -n 2p <<<"$probe")"
    remote_ifaces="$(sed -n '3,$p' <<<"$probe" | grep -v '^lo$' | paste -sd' ' -)"
    if [[ -n "$iface" && -n "$remote_ifaces" && " ${remote_ifaces} " != *" ${iface} "* ]]; then
        ny_ssh_close "$target"
        ny_die "${host} has no network interface called '${iface}'. It has: ${remote_ifaces}." \
            "Re-run with --interface set to one of those (usually the wired one), or leave it out to use the one that routes to this server." "$NY_E_USAGE"
    fi
    local method
    method="$(cluster_become_method "$become" "$remote_uid" "$remote_sudo")"
    if [[ "$become" == sudo && "$remote_sudo" == no-sudo ]]; then
        ny_ssh_close "$target"
        ny_die "sudo is not installed on ${host}." "Use --become su (you'll type the root password), or install sudo there." "$NY_E_PRECONDITION"
    fi

    local rdir bundle runner
    rdir="$(ssh "${NY_SSH_OPTS[@]}" "$target" 'umask 077; mktemp -d /tmp/nodeyard.XXXXXXXX')" ||
        ny_die "Could not create a private temporary directory on ${target}."
    [[ "$rdir" =~ ^/tmp/nodeyard\.[A-Za-z0-9]+$ ]] || ny_die "Unexpected temporary directory name from ${target}: ${rdir}"
    bundle="$(ny_mktemp)"
    ny_self_bundle "$bundle"
    runner="$(ny_mktemp)"
    cluster_remote_script "${join[@]}" >"$runner"
    ny_step "Copying nodeyard and the join token to ${target}"
    scp "${NY_SSH_OPTS[@]}" -q "$bundle" "${target}:${rdir}/nodeyard.tar.gz" || ny_die "Copying nodeyard to ${target} failed."
    scp "${NY_SSH_OPTS[@]}" -q "$runner" "${target}:${rdir}/run.sh" || ny_die "Copying the install script to ${target} failed."
    scp "${NY_SSH_OPTS[@]}" -q "$(ny_path "$NY_K3S_TOKEN_FILE")" "${target}:${rdir}/k3s-token" || ny_die "Copying the join token to ${target} failed."

    case "$method" in
        su) ny_step "Installing and joining on ${target} as root (enter ${host}'s ROOT password for su when asked)" ;;
        sudo) ny_step "Installing and joining on ${target} as root (enter your sudo password on ${host} if asked)" ;;
        *) ny_step "Installing and joining on ${target}" ;;
    esac
    # The runner does everything as root in its own directory and removes
    # it; the files we copied are ours, so we remove them as ourselves.
    local rc=0
    ssh "${NY_SSH_OPTS[@]}" -t "$target" "$(cluster_become_cmd "$method" "${rdir}/run.sh"); rc=\$?; rm -rf '${rdir}' 2>/dev/null; exit \$rc" || rc=$?
    ny_ssh_close "$target"
    if [[ "$rc" -eq 0 ]]; then
        ny_ok "${target} joined the cluster as a ${kind}."
    elif [[ "$method" == su && "$rc" -eq 1 ]]; then
        ny_die "Becoming root with su on ${host} failed (wrong root password, or root has no password)." \
            "Try again, or use --become sudo if ${NY_SSH_USER:-that user} can use sudo there." "$NY_E_PRECONDITION"
    else
        ny_die "Setting up ${target} failed (exit ${rc}); the reason is in the output above." \
            "Fix that, then run add-node again (it is safe to repeat). 'sudo nodeyard doctor' on ${host} checks the usual problems."
    fi
    return 0
}

# cluster_probe_script -- shell run (as the SSH user) on a new machine: prints
# its uid, whether sudo is usable, then its network interfaces.
cluster_probe_script() {
    cat <<'PROBE'
id -u
if command -v sudo >/dev/null 2>&1; then
    if sudo -n true 2>/dev/null; then echo sudo-nopass
    elif id -nG | tr ' ' '\n' | grep -qxE 'sudo|wheel|admin'; then echo sudo
    else echo sudo-not-allowed; fi
else
    echo no-sudo
fi
ls /sys/class/net 2>/dev/null
PROBE
}

# cluster_become_method REQUESTED REMOTE_UID REMOTE_SUDO -- none, sudo or su.
cluster_become_method() {
    local want="$1" uid="$2" sudo_state="$3"
    if [[ "$uid" == 0 ]]; then
        echo none
    elif [[ "$want" == sudo || "$want" == su ]]; then
        echo "$want"
    elif [[ "$sudo_state" == sudo-nopass || "$sudo_state" == sudo ]]; then
        echo sudo
    else
        echo su
    fi
}

# cluster_become_cmd METHOD SCRIPT -- the remote command that runs SCRIPT as root.
cluster_become_cmd() {
    local method="$1" script="$2"
    case "$method" in
        none) printf "bash '%s'" "$script" ;;
        sudo) printf "sudo bash '%s'" "$script" ;;
        su) printf "su - root -c \"bash '%s'\"" "$script" ;;
    esac
}

# cluster_remote_script JOIN_ARG... -- the root script that runs on the new
# machine: unpack nodeyard, install it, join, and clean up after itself. The
# word TOKENFILE in the arguments becomes the path of the copied token.
cluster_remote_script() {
    local a args=""
    for a in "$@"; do
        if [[ "$a" == TOKENFILE ]]; then
            # shellcheck disable=SC2016 # expanded by the remote script
            args+=' "${work}/k3s-token"'
        else
            args+=" $(printf '%q' "$a")"
        fi
    done
    cat <<SCRIPT
#!/bin/bash
# Written by 'nodeyard add-node'. Runs as root on the new machine, then
# removes its working directory (and with it the copy of the join token).
set -euo pipefail
here="\$(cd "\$(dirname "\$0")" && pwd -P)"
work="\$(mktemp -d /root/.nodeyard-join.XXXXXXXX 2>/dev/null || mktemp -d /tmp/nodeyard-join.XXXXXXXX)"
trap 'rm -rf "\$work"' EXIT
chmod 700 "\$work"
install -m 600 "\$here/k3s-token" "\$work/k3s-token"
tar --no-same-owner -xzf "\$here/nodeyard.tar.gz" -C "\$work"
bash "\$work/install.sh" --from-dir "\$work" --yes
/usr/local/bin/nodeyard${args}
SCRIPT
}

# ny_self_bundle DEST -- a tarball of this nodeyard installation.
ny_self_bundle() {
    local dest="$1" item
    local -a items=()
    for item in bin lib share completions install.sh uninstall.sh LICENSE CHANGELOG.md; do
        [[ -e "${NY_HOME}/${item}" ]] && items+=("$item")
    done
    tar -czf "$dest" -C "$NY_HOME" "${items[@]}"
}

cluster_remove_node_cmd_help() {
    cat <<'HELP'
Usage: nodeyard remove-node NODE [--purge]

Drains NODE (moves its workloads elsewhere) and removes it from the
cluster. To also wipe k3s from that machine, run 'sudo nodeyard uninstall k3s'
on it afterwards.
HELP
}

cluster_remove_node_cmd() {
    ny_need_root
    local node="" purge=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --purge)
                purge=1
                shift
                ;;
            -*) ny_usage_error "Unknown option for 'remove-node': $1" ;;
            *)
                [[ -z "$node" ]] || ny_usage_error "Unexpected argument: $1"
                node="$1"
                shift
                ;;
        esac
    done
    [[ -n "$node" ]] || ny_usage_error "Say which node to remove." "nodeyard remove-node NODE"
    ny_need_kube
    kctl get node "$node" >/dev/null 2>&1 || ny_die "There is no node called '${node}'." "List nodes with: nodeyard list-nodes" "$NY_E_USAGE"
    ny_confirm "Drain ${node} and remove it from the cluster? Its workloads move to other nodes." n || {
        ny_info "Cancelled."
        return 0
    }
    ny_run "$(ny_path "$NY_K3S_BIN")" kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data --force --timeout=120s ||
        ny_warn "Drain reported problems; continuing."
    ny_run "$(ny_path "$NY_K3S_BIN")" kubectl delete node "$node" || ny_warn "Deleting the node object reported problems."
    [[ "$purge" -eq 1 ]] && ny_hint "To remove k3s from ${node} itself, run there: sudo nodeyard uninstall k3s"
    ny_ok "Node removed: ${node}"
}

# --- watchdog ----------------------------------------------------------------

CLUSTER_WD_UNIT="/etc/systemd/system/nodeyard-watchdog.service"
CLUSTER_WD_TIMER="/etc/systemd/system/nodeyard-watchdog.timer"

cluster_watchdog_install_cmd_help() {
    cat <<'HELP'
Usage: nodeyard watchdog-install --master ADDRESS [--check-interval SECONDS] [--fail-threshold N]

Checks the control plane's /healthz on a timer and logs (to the system
journal: journalctl -t nodeyard-watchdog) when it has been unreachable
for N checks in a row. It never changes the cluster by itself.
HELP
}

cluster_watchdog_install_cmd() {
    ny_need_root
    local master="" interval=15 threshold=8
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --master)
                ny_need_value "$1" $#
                ny_valid_host "$2" || ny_usage_error "$NY_VALID_MSG"
                master="$2"
                shift 2
                ;;
            --check-interval)
                ny_need_value "$1" $#
                ny_valid_int "$2" 5 3600 || ny_usage_error "$NY_VALID_MSG"
                interval="$2"
                shift 2
                ;;
            --fail-threshold)
                ny_need_value "$1" $#
                ny_valid_int "$2" 1 1000 || ny_usage_error "$NY_VALID_MSG"
                threshold="$2"
                shift 2
                ;;
            *) ny_usage_error "Unknown option for 'watchdog-install': $1" ;;
        esac
    done
    [[ -n "$master" ]] || ny_usage_error "--master is required (the control plane's address)."
    ny_cfg_set watchdog "" master "$master"
    ny_cfg_set watchdog "" interval "$interval"
    ny_cfg_set watchdog "" threshold "$threshold"
    ny_write_file "$CLUSTER_WD_UNIT" <<UNIT
[Unit]
Description=nodeyard control-plane watchdog

[Service]
Type=oneshot
ExecStart=/usr/local/bin/nodeyard watchdog-check
UNIT
    ny_write_file "$CLUSTER_WD_TIMER" <<UNIT
[Unit]
Description=nodeyard control-plane watchdog timer

[Timer]
OnBootSec=30s
OnUnitActiveSec=${interval}s
AccuracySec=1s

[Install]
WantedBy=timers.target
UNIT
    ny_service_daemon_reload
    ny_service_enable nodeyard-watchdog.timer --now
    ny_ok "Watchdog installed: checking ${master} every ${interval}s, reporting after ${threshold} failures."
}

cluster_watchdog_uninstall_cmd() {
    ny_need_root
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    ny_run systemctl disable --now nodeyard-watchdog.timer >/dev/null 2>&1 || true
    ny_journal_undo_feature watchdog 1 || true
    ny_cfg_drop_section watchdog ""
    ny_remove_file "$(ny_unroot "${NY_STATE}/watchdog-failures")"
    ny_ok "Watchdog removed."
}

cluster_watchdog_check_cmd() {
    local master threshold failures=0 sf
    master="$(ny_cfg_get watchdog "" master)"
    threshold="$(ny_cfg_get watchdog "" threshold 8)"
    [[ -n "$master" ]] || return 0
    sf="${NY_STATE}/watchdog-failures"
    [[ -r "$sf" ]] && failures="$(<"$sf")"
    [[ "$failures" =~ ^[0-9]+$ ]] || failures=0
    mkdir -p -- "$NY_STATE"
    if curl -kfsS --connect-timeout 3 --max-time 5 "https://${master}:6443/healthz" >/dev/null 2>&1 ||
        curl -kfsS --connect-timeout 3 --max-time 5 -o /dev/null -w '%{http_code}' "https://${master}:6443/healthz" 2>/dev/null | grep -q '^401$'; then
        echo 0 >"$sf"
        return 0
    fi
    failures=$((failures + 1))
    echo "$failures" >"$sf"
    logger -t nodeyard-watchdog "k3s control plane ${master} unreachable (${failures}/${threshold})" 2>/dev/null || true
    if ((failures >= threshold)); then
        logger -t nodeyard-watchdog "Control plane down for ${failures} checks. nodeyard will not change the cluster by itself; see 'nodeyard promote'." 2>/dev/null || true
    fi
    return 0
}

cluster_promote_cmd() {
    ny_need_root
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    local master
    master="$(ny_cfg_get watchdog "" master)"
    if [[ -n "$master" ]]; then
        cluster_watchdog_check_cmd
        local f
        f="$(cat "${NY_STATE}/watchdog-failures" 2>/dev/null || echo 0)"
        ny_info "Watchdog: ${master} has failed ${f} check(s) in a row."
    else
        ny_info "The watchdog is not installed (sudo nodeyard watchdog-install --master ADDRESS)."
    fi
    ny_info "nodeyard does not promote nodes automatically. With 3 or more servers (install master --ha"
    ny_info "plus join-master), the cluster keeps running when one server fails; see docs/k3s.md."
}

# --- pod network test ----------------------------------------------------------
# A node can be Ready (the kubelet reaches the API server) while its pods are
# cut off, so this checks what nothing else reports.

cluster_nettest_cmd_help() {
    cat <<'HELP'
Usage: nodeyard nettest

Starts a small test pod on every node and checks pod-to-pod traffic across
nodes (flannel VXLAN), cluster DNS, and pod-to-kubelet (10250) traffic,
then cleans up. Failures that all involve one node usually mean that
node's firewall drops forwarded traffic: run 'sudo nodeyard doctor --fix' there.
HELP
}

cluster_nettest_cmd() {
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    ny_need_kube
    local ns="nodeyard-nettest" node addr name
    local -a nodes=() pods=() nodeips=()
    while IFS=$'\t' read -r node addr; do
        [[ -n "$node" ]] || continue
        nodes+=("$node")
        nodeips+=("$addr")
    done < <(kctl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}')
    [[ "${#nodes[@]}" -gt 0 ]] || ny_die "No nodes found." "Is k3s running? sudo nodeyard status"

    kctl delete namespace "$ns" --wait=true >/dev/null 2>&1 || true
    kctl create namespace "$ns" >/dev/null
    for node in "${nodes[@]}"; do
        name="t-$(ny_k8s_name "$node")"
        pods+=("$name")
        kctl -n "$ns" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata: {name: ${name}, namespace: ${ns}, labels: {app: nodeyard-nettest}}
spec:
  nodeSelector: {kubernetes.io/hostname: ${node}}
  tolerations: [{operator: Exists}]
  terminationGracePeriodSeconds: 0
  containers: [{name: c, image: "busybox:1.36", command: ["sleep", "900"]}]
YAML
    done
    ny_step "Started a test pod on each of ${#nodes[@]} nodes; waiting for them"
    kctl -n "$ns" wait --for=condition=Ready pod -l app=nodeyard-nettest --timeout=240s >/dev/null 2>&1 ||
        ny_warn "Not every test pod started in time; its checks will show as failures."

    local -a podips=()
    local i j fails=0 total=0 ip
    for name in "${pods[@]}"; do
        ip="$(kctl -n "$ns" get pod "$name" -o jsonpath='{.status.podIP}' 2>/dev/null || true)"
        podips+=("${ip:-none}")
    done
    cluster_nettest_check() {
        local label="$1"
        shift
        total=$((total + 1))
        if kctl -n "$ns" exec "$@" >/dev/null 2>&1; then
            printf '  %s  %s\n' "$(ny_color green "$NY_SYM_OK")" "$label"
        else
            printf '  %s  %s\n' "$(ny_color red "$NY_SYM_FAIL")" "$label"
            fails=$((fails + 1))
        fi
        return 0
    }
    printf '%s\n' "$(ny_color bold "Pod to pod across nodes (flannel VXLAN)")"
    for i in "${!pods[@]}"; do
        for j in "${!pods[@]}"; do
            [[ $i -eq $j ]] && continue
            cluster_nettest_check "${nodes[i]} -> ${nodes[j]} (pod ${podips[j]})" "${pods[i]}" -- ping -c 2 -W 3 "${podips[j]}"
        done
    done
    printf '%s\n' "$(ny_color bold "Cluster DNS")"
    for i in "${!pods[@]}"; do
        cluster_nettest_check "${nodes[i]}: resolve kubernetes.default" "${pods[i]}" -- nslookup kubernetes.default.svc.cluster.local
    done
    printf '%s\n' "$(ny_color bold "Pod to node (kubelet :10250)")"
    for i in "${!pods[@]}"; do
        for j in "${!nodes[@]}"; do
            cluster_nettest_check "pod on ${nodes[i]} -> ${nodes[j]} (${nodeips[j]})" "${pods[i]}" -- nc -z -w 4 "${nodeips[j]}" 10250
        done
    done
    kctl delete namespace "$ns" --wait=false >/dev/null 2>&1 || true
    printf '\n'
    if [[ "$fails" -eq 0 ]]; then
        ny_ok "All ${total} network checks passed."
    else
        ny_err "${fails} of ${total} checks failed."
        ny_hint "If the failures all involve one node's pods, that node's firewall is dropping forwarded traffic: run 'sudo nodeyard doctor --fix' on it."
    fi
    return 0
}
