# shellcheck shell=bash
# shellcheck disable=SC2034 # globals here are read by other files
# The web dashboard: a page about the cluster (nodes, resources, IP addresses,
# pods, logs, AI), on port 9092. The server is share/nodeyard/dashboard/server.py
# (Python 3, standard library). This module runs it as a hardened systemd
# service, or in the foreground. It listens on this machine and, by default,
# on the Tailscale address; anything beyond this machine needs the sign-in key.

ny_cmd "dashboard" dashboard_status_cmd "Dashboard" "Show whether the dashboard is running and how to open it" dashboard hidden,json
ny_cmd "dashboard status" dashboard_status_cmd "Dashboard" "Show whether the dashboard is running and how to open it" dashboard json
ny_cmd "dashboard start" dashboard_start_cmd "Dashboard" "Start the web dashboard (port 9092) as a service" dashboard
ny_cmd "dashboard stop" dashboard_stop_cmd "Dashboard" "Stop the dashboard and remove its service" dashboard
ny_cmd "dashboard password" dashboard_password_cmd "Dashboard" "Set, show or randomise the dashboard's sign-in password" dashboard
ny_cmd "dashboard run" dashboard_run_cmd "Dashboard" "Run the dashboard in this terminal (Ctrl-C stops it)" dashboard
ny_cmd "dashboard agent install" dashboard_agent_install_cmd "Dashboard" "Run the read-only node agent on every node (processes, CPU clocks, temperatures)" dashboard
ny_cmd "dashboard agent remove" dashboard_agent_remove_cmd "Dashboard" "Remove the node agents" dashboard
ny_cmd "dashboard agent status" dashboard_agent_status_cmd "Dashboard" "Show the node agents" dashboard

DASH_UNIT="nodeyard-dashboard"

ny_cfg_section_add dashboard single
ny_cfg_schema_add dashboard.weak-password bool "" "Allow any dashboard password, even a very short one (risky once the dashboard is public)"

# dashboard_weak_ok -- true when `nodeyard config set dashboard.weak-password true`
dashboard_weak_ok() {
    [[ "$(ny_cfg_get dashboard "" weak-password)" == "true" ]]
}
DASH_UNIT_FILE="/etc/systemd/system/nodeyard-dashboard.service"
DASH_PW_NAME="dashboard-password"
DASH_AGENT_NS="nodeyard-system"
DASH_AGENT_TOKEN="agent-token"
DASH_AGENT_IMAGE="python:3.12-alpine"
DASH_AGENT_GPU_IMAGE="python:3.12-slim" # glibc, so NVIDIA's nvidia-smi runs in it
DASH_AI_KEY="ai-split-api-key"
DASH_DEFAULT_PORT=9092
DASH_PORT="$DASH_DEFAULT_PORT"
DASH_INTERVAL=2
DASH_LISTEN="auto"
DASH_PW_STDIN=0
DASH_PW_RANDOM=0
DASH_PW_NORESTART=0
DASH_PW_SHOW=0

dashboard_start_cmd_help() {
    cat <<'HELP'
Usage: nodeyard dashboard start [--port PORT] [--listen WHERE] [--interval SECONDS]
       nodeyard dashboard run   [--port PORT] [--listen WHERE] [--interval SECONDS]
       nodeyard dashboard stop
       nodeyard dashboard status
       nodeyard dashboard password [--show | --random | --stdin]
       nodeyard dashboard agent install|remove|status

A web page about your cluster: every node with its IP address and load,
total resources and usage over time, pods with live usage and logs,
services and the addresses to reach them, storage, events, alerts and AI
models. It only looks at the cluster, apart from the AI features.

`start` installs a systemd service that starts at boot (hardened: no
write access to the system, no extra privileges, 300 MB memory limit);
`stop` removes it. `run` runs it in this terminal instead. Needs python3.
With --demo it shows a simulated cluster.

Who can reach it (--listen):
  auto        This machine, plus this machine's Tailscale address when it has
              one (the default). Browse to http://TAILSCALE-IP:9092.
  local       Only this machine. Reach it with an SSH tunnel:
              ssh -L 9092:localhost:9092 USER@THIS-MACHINE
  tailscale   Like auto, but fails if Tailscale isn't running.
  all         Every network interface (plain HTTP: avoid on a shared network).
  ADDR[,ADDR] Specific addresses, e.g. 192.168.1.10 (use "local" for localhost).

Anything beyond this machine asks for a password on a sign-in page. The
first `start` makes a random one; choose your own with
`sudo nodeyard dashboard password` (it asks twice, hidden), or
`--show` / `--random`. Changing it signs everyone out. After 5 wrong tries
an address is locked out for 5 minutes. Tailscale already encrypts the
connection.

The node agent (`dashboard agent install`) is a small read-only program that
runs on every node and lets the dashboard show each machine's processes,
CPU clock speeds, temperatures, load and memory breakdown. It runs as a
DaemonSet (a python:3.12-alpine pod with the host's process view, no
extra privileges, nothing written) and answers only the dashboard.

Options:
  --port PORT          Port to listen on (default 9092)
  --listen WHERE       See above (default auto)
  --interval SECONDS   How often to read the cluster (default 2)
  --show               With `password`: print the current password
  --random             With `password`: make a new random password
  --stdin              With `password`: read the new password from standard input
HELP
}
dashboard_stop_cmd_help() { dashboard_start_cmd_help; }
dashboard_run_cmd_help() { dashboard_start_cmd_help; }
dashboard_status_cmd_help() { dashboard_start_cmd_help; }
dashboard_password_cmd_help() { dashboard_start_cmd_help; }
dashboard_agent_install_cmd_help() { dashboard_start_cmd_help; }
dashboard_agent_remove_cmd_help() { dashboard_start_cmd_help; }
dashboard_agent_status_cmd_help() { dashboard_start_cmd_help; }

dashboard_parse() {
    DASH_PORT="$DASH_DEFAULT_PORT"
    DASH_INTERVAL=2
    DASH_LISTEN="auto"
    DASH_PW_STDIN=0
    DASH_PW_RANDOM=0
    DASH_PW_NORESTART=0
    DASH_PW_SHOW=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --port)
                [[ $# -ge 2 ]] || ny_usage_error "--port needs a number"
                ny_valid_port "$2" || ny_usage_error "$NY_VALID_MSG"
                DASH_PORT="$((10#$2))"
                shift 2
                ;;
            --interval)
                [[ $# -ge 2 ]] || ny_usage_error "--interval needs a number of seconds"
                ny_valid_int "$2" 1 300 || ny_usage_error "--interval must be a whole number of seconds from 1 to 300."
                DASH_INTERVAL="$2"
                shift 2
                ;;
            --listen)
                [[ $# -ge 2 ]] || ny_usage_error "--listen needs a value: auto, local, tailscale, all or addresses"
                DASH_LISTEN="$2"
                shift 2
                ;;
            --no-restart)
                DASH_PW_NORESTART=1
                shift
                ;;
            --stdin)
                DASH_PW_STDIN=1
                shift
                ;;
            --random)
                DASH_PW_RANDOM=1
                shift
                ;;
            --show)
                DASH_PW_SHOW=1
                shift
                ;;
            *) ny_usage_error "Unknown option: $1" "nodeyard dashboard start [--port PORT] [--listen WHERE] [--interval SECONDS]" ;;
        esac
    done
    return 0
}

dashboard_server() {
    printf '%s\n' "${NY_SHARE}/dashboard/server.py"
}

# dashboard_python -- the python3 to use, or a fix to show.
dashboard_python() {
    local py
    py="$(command -v python3 2>/dev/null || true)"
    if [[ -z "$py" ]] && ny_simulating; then
        py="/usr/bin/python3"
    fi
    [[ -n "$py" ]] || ny_die "The dashboard needs python3, which isn't installed." \
        "Install it (Debian/Ubuntu: sudo apt install python3; Fedora: sudo dnf install python3; Arch: sudo pacman -S python), then try again." "$NY_E_PRECONDITION"
    if ! ny_simulating && ! "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
        ny_die "The dashboard needs Python 3.8 or newer (this is $("$py" -V 2>&1))." "Update python3 on this machine." "$NY_E_PRECONDITION"
    fi
    printf '%s\n' "$py"
}

# dashboard_need_cluster -- the kubeconfig the dashboard reads the cluster with.
dashboard_need_cluster() {
    DASH_KUBECONFIG="$(ny_kube_config)" ||
        ny_die "No kubeconfig is available on this machine, so the dashboard has no cluster to show." \
            "Run this on a k3s server (with sudo), or try the simulated cluster: nodeyard --demo dashboard run" "$NY_E_PRECONDITION"
    return 0
}

dashboard_ssh_user() {
    printf '%s\n' "${SUDO_USER:-${USER:-user}}"
}

dashboard_address() {
    k3s_load_state
    local dev src gw
    read -r dev src gw <<<"$(ny_primary_route)"
    printf '%s\n' "${K3S_NODE_IP:-${src#-}}"
}

# dashboard_tailscale_ip -- this machine's Tailscale IPv4, or nothing.
dashboard_tailscale_ip() {
    local ts=""
    if have tailscale; then
        ts="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
        ny_valid_ipv4 "$ts" 2>/dev/null || ts=""
    fi
    printf '%s\n' "$ts"
}

# dashboard_resolve_listen -- turns DASH_LISTEN into the server's --listen value
# (DASH_LISTEN_ARG) and the non-local addresses in it (DASH_REMOTE_ADDRS).
dashboard_resolve_listen() {
    local want="$DASH_LISTEN" ts item
    DASH_LISTEN_ARG="local"
    DASH_REMOTE_ADDRS=()
    ts="$(dashboard_tailscale_ip)"
    case "$want" in
        local) ;;
        auto)
            if [[ -n "$ts" ]]; then
                DASH_LISTEN_ARG="local,${ts}"
                DASH_REMOTE_ADDRS=("$ts")
            fi
            ;;
        tailscale)
            [[ -n "$ts" ]] || ny_die "Tailscale doesn't seem to be running on this machine." \
                "Start it (sudo tailscale up), or use --listen local with an SSH tunnel." "$NY_E_PRECONDITION"
            DASH_LISTEN_ARG="local,${ts}"
            DASH_REMOTE_ADDRS=("$ts")
            ;;
        all)
            DASH_LISTEN_ARG="all"
            DASH_REMOTE_ADDRS=("0.0.0.0")
            ;;
        *)
            DASH_LISTEN_ARG=""
            local -a parts
            IFS=, read -r -a parts <<<"$want"
            for item in "${parts[@]}"; do
                if [[ "$item" == local ]]; then
                    DASH_LISTEN_ARG+="${DASH_LISTEN_ARG:+,}local"
                elif ny_valid_ipv4 "$item" 2>/dev/null; then
                    DASH_LISTEN_ARG+="${DASH_LISTEN_ARG:+,}${item}"
                    DASH_REMOTE_ADDRS+=("$item")
                else
                    ny_usage_error "--listen: '${item}' isn't local, tailscale, all or an IPv4 address."
                fi
            done
            [[ -n "$DASH_LISTEN_ARG" ]] || ny_usage_error "--listen needs a value: auto, local, tailscale, all or addresses"
            ;;
    esac
    return 0
}

dashboard_password_file() {
    ny_secret_path "$DASH_PW_NAME"
}

# dashboard_new_password -- 24 random hex digits in groups of four.
dashboard_new_password() {
    local hex
    if have openssl; then
        hex="$(openssl rand -hex 12)"
    else
        hex="$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
    fi
    hex="${hex^^}"
    printf '%s-%s-%s-%s-%s-%s\n' "${hex:0:4}" "${hex:4:4}" "${hex:8:4}" "${hex:12:4}" "${hex:16:4}" "${hex:20:4}"
}

# dashboard_ensure_password -- make a random sign-in password if none is set
# (sets DASH_PW_CREATED and DASH_PW_NEW).
dashboard_ensure_password() {
    DASH_PW_CREATED=0
    DASH_PW_NEW=""
    if ny_secret_exists "$DASH_PW_NAME"; then
        return 0
    fi
    # Earlier versions called it the dashboard key: keep the same secret, so it still works.
    if ny_secret_exists "dashboard-key"; then
        ny_secret_get "dashboard-key" | ny_secret_set "$DASH_PW_NAME"
        ny_remove_file "$(ny_secret_path dashboard-key)"
        return 0
    fi
    DASH_PW_NEW="$(dashboard_new_password)"
    ny_secret_register "$DASH_PW_NEW"
    printf '%s' "$DASH_PW_NEW" | ny_secret_set "$DASH_PW_NAME"
    DASH_PW_CREATED=1
    return 0
}

dashboard_firewall() {
    local addr fw iface=""
    fw="$(ny_detect_firewall)"
    [[ "${#DASH_REMOTE_ADDRS[@]}" -gt 0 && "$fw" != none ]] || return 0
    for addr in "${DASH_REMOTE_ADDRS[@]}"; do
        iface=""
        if [[ "$addr" != 0.0.0.0 ]]; then
            iface="$(ip -o -4 addr show 2>/dev/null | awk -v a="$addr" '{split($4, x, "/"); if (x[1] == a) {print $2; exit}}' || true)"
        fi
        case "$fw" in
            ufw)
                if [[ -n "$iface" ]]; then
                    ny_run_undoable ufw delete allow in on "$iface" to any port "$DASH_PORT" proto tcp -- \
                        ufw allow in on "$iface" to any port "$DASH_PORT" proto tcp comment "nodeyard dashboard" >/dev/null || true
                else
                    ny_run_undoable ufw delete allow "${DASH_PORT}/tcp" -- ufw allow "${DASH_PORT}/tcp" comment "nodeyard dashboard" >/dev/null || true
                fi
                ;;
            firewalld)
                ny_run_undoable firewall-cmd --permanent --remove-port="${DASH_PORT}/tcp" -- firewall-cmd --permanent --add-port="${DASH_PORT}/tcp" >/dev/null || true
                ny_run firewall-cmd --reload >/dev/null || true
                ;;
            *)
                ny_warn "${fw} has custom rules; nodeyard won't rewrite them. Allow TCP port ${DASH_PORT} from your Tailscale network to reach the dashboard."
                return 0
                ;;
        esac
    done
    ny_ok "Firewall (${fw}) allows port ${DASH_PORT}${iface:+ on ${iface}}."
    return 0
}

dashboard_firewall_remove() {
    local fw
    fw="$(ny_detect_firewall)"
    case "$fw" in
        ufw)
            # Remove the rules we added (their comment says so).
            local n
            while n="$(ufw status numbered 2>/dev/null | sed -n 's/^\[ *\([0-9][0-9]*\)\].*nodeyard dashboard.*/\1/p' | tail -n1)" && [[ -n "$n" ]]; do
                ny_run ufw --force delete "$n" >/dev/null || break
            done
            ;;
        firewalld) ny_run firewall-cmd --permanent --remove-port="${1}/tcp" >/dev/null 2>&1 || true ;;
    esac
    return 0
}

# dashboard_tell_how_to_open PORT LISTEN_ARG -- URLs and sign-in hints.
dashboard_tell_how_to_open() {
    local port="$1" listen="$2" user addr item shown=0
    user="$(dashboard_ssh_user)"
    addr="$(dashboard_address)"
    printf '\n' >&2
    local -a parts
    IFS=, read -r -a parts <<<"$listen"
    for item in "${parts[@]}"; do
        case "$item" in
            local) ;;
            all)
                ny_info "Open it from any machine that can reach this one: http://${addr:-THIS-MACHINE}:${port}"
                shown=1
                ;;
            *)
                ny_info "Open it in your browser: http://${item}:${port}"
                shown=1
                ;;
        esac
    done
    if [[ "$shown" -eq 1 ]]; then
        ny_info "It asks for the dashboard password (see it or change it: sudo nodeyard dashboard password)"
        ny_hint "On this machine it's also at http://localhost:${port} (or through an SSH tunnel: ssh -L ${port}:localhost:${port} ${user}@${addr:-THIS-MACHINE})."
    else
        ny_info "The dashboard listens on this machine only: http://localhost:${port}"
        ny_info "From your own computer, open an SSH tunnel and browse to http://localhost:${port}:"
        printf '\n    ssh -L %s:localhost:%s %s@%s\n\n' "$port" "$port" "$user" "${addr:-THIS-MACHINE}" >&2
        ny_hint "Leave that SSH session open while you use the dashboard. To reach it directly over Tailscale instead: nodeyard dashboard start --listen auto"
    fi
    return 0
}

dashboard_unit_text() {
    local py="$1" port="$2" interval="$3" name="$4" listen="$5"
    local args
    args="$(dashboard_server) --port ${port} --listen ${listen} --interval ${interval} --kubeconfig ${DASH_KUBECONFIG} --password-file $(dashboard_password_file)"
    args+=" --agent-token-file $(ny_secret_path "$DASH_AGENT_TOKEN") --ai-key-file $(ny_secret_path "$DASH_AI_KEY") --nodeyard-bin ${NY_HOME}/bin/nodeyard --nodeyard-version ${NY_VERSION}"
    [[ -z "$name" ]] || args+=" --cluster-name ${name}"
    local tuser
    tuser="$(dashboard_terminal_user)"
    [[ -z "$tuser" ]] || args+=" --terminal-user ${tuser}"
    cat <<UNIT
[Unit]
Description=nodeyard dashboard (web page about the cluster)
After=network-online.target k3s.service tailscaled.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=${py} ${args}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=HOME=/tmp
Restart=on-failure
RestartSec=5
# It reads the cluster through its API and answers web requests. Signed-in
# users can also run nodeyard's model commands from the page, so it may write
# nodeyard's own state and logs, and nothing else on the system.
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ReadWritePaths=-/var/lib/nodeyard -/var/log/nodeyard -/etc/nodeyard
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=true
LockPersonality=true
SystemCallArchitectures=native
CapabilityBoundingSet=
MemoryMax=400M

[Install]
WantedBy=multi-user.target
UNIT
}

# dashboard_unit_value OPTION -- an option's value from the installed unit.
# dashboard_terminal_user -- who the Terminal page's shell runs as: whoever ran
# 'sudo nodeyard dashboard start' (never root), else what the unit already says.
dashboard_terminal_user() {
    local u="${SUDO_USER:-}"
    if [[ -n "$u" && "$u" != root && "$u" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] && getent passwd "$u" >/dev/null 2>&1; then
        printf '%s\n' "$u"
    else
        dashboard_unit_value terminal-user
    fi
    return 0
}

dashboard_unit_value() {
    local f
    f="$(ny_path "$DASH_UNIT_FILE")"
    [[ -r "$f" ]] || return 0
    sed -n "s/^ExecStart=.* --${1} \\([^ ]*\\).*/\\1/p" "$f" | head -n1
    return 0
}

dashboard_is_active() {
    systemctl is-active --quiet "$DASH_UNIT" 2>/dev/null
}

# dashboard_wait PORT -- wait until the server answers.
dashboard_wait() {
    local port="$1" tries=0
    ny_simulating && return 0
    while ((tries < 20)); do
        if curl -fsS --max-time 2 "http://127.0.0.1:${port}/api/health" >/dev/null 2>&1; then
            return 0
        fi
        sleep "${NODEYARD_WAIT_INTERVAL:-1}"
        tries=$((tries + 1))
    done
    return 1
}

dashboard_start_cmd() {
    ny_need_root
    dashboard_parse "$@"
    ny_deps_ensure "the dashboard" python3 curl openssl
    local py name
    py="$(dashboard_python)"
    dashboard_need_cluster
    name="$(ny_cfg_get cluster "" name)"
    if [[ "$NY_DEMO" -eq 1 ]] && ! ny_systemd_available; then
        ny_die "Demo mode doesn't start real services." "To see the dashboard with a simulated cluster: nodeyard --demo dashboard run" "$NY_E_PRECONDITION"
    fi
    ny_systemd_available || ny_die "The dashboard service needs systemd, and it wasn't found." \
        "Run it in a terminal instead: nodeyard dashboard run" "$NY_E_PRECONDITION"
    if ! ny_simulating && ny_tcp_check 127.0.0.1 "$DASH_PORT" 1 && ! dashboard_is_active; then
        ny_die "Something else is already listening on port ${DASH_PORT}." "Pick another port: nodeyard dashboard start --port 9093" "$NY_E_PRECONDITION"
    fi
    dashboard_resolve_listen
    if [[ "$DASH_LISTEN" == auto && "${#DASH_REMOTE_ADDRS[@]}" -eq 0 ]]; then
        ny_info "Tailscale isn't running here, so the dashboard will listen on this machine only."
    fi

    ny_step "Starting the dashboard on port ${DASH_PORT}"
    dashboard_ensure_password
    dashboard_unit_text "$py" "$DASH_PORT" "$DASH_INTERVAL" "$name" "$DASH_LISTEN_ARG" | ny_write_file "$DASH_UNIT_FILE" 0644
    ny_service_daemon_reload
    ny_service_enable "${DASH_UNIT}.service" --now
    # Settings changes (a new port or password) need a restart of an already-running unit.
    ny_simulating || ny_run systemctl restart "${DASH_UNIT}.service"
    dashboard_firewall
    if ! dashboard_wait "$DASH_PORT"; then
        ny_simulating || {
            ny_warn "The dashboard service started but isn't answering on port ${DASH_PORT}."
            journalctl -u "$DASH_UNIT" --no-pager -n 15 2>/dev/null | sed 's/^/    /' >&2 || true
            ny_die "The dashboard didn't come up." "Look at the log above, or run it in the foreground to see the error: nodeyard dashboard run --port ${DASH_PORT}"
        }
    fi
    if ny_simulating; then
        ny_info "Nothing was started (dry run or demo). This is what you would see:"
    else
        ny_ok "The dashboard is running (it also starts at boot)."
    fi
    dashboard_tell_how_to_open "$DASH_PORT" "$DASH_LISTEN_ARG"
    if [[ "$DASH_PW_CREATED" -eq 1 ]] && ! ny_simulating; then
        printf '\n  %s\n    %s\n\n' "$(ny_color bold "Your dashboard password (the sign-in page asks for it; this is the only time it's shown unprompted):")" "$DASH_PW_NEW" >&2
        ny_hint "Choose your own with: sudo nodeyard dashboard password"
    elif [[ "$DASH_PW_CREATED" -eq 1 ]]; then
        ny_info "A random sign-in password would be created (change it any time with: sudo nodeyard dashboard password)."
    fi
    return 0
}

dashboard_stop_cmd() {
    ny_need_root
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    if [[ ! -f "$(ny_path "$DASH_UNIT_FILE")" ]]; then
        ny_info "The dashboard service isn't installed; nothing to stop."
        return 0
    fi
    local port
    port="$(dashboard_unit_value port)"
    ny_step "Stopping the dashboard"
    ny_service_disable "${DASH_UNIT}.service" --now || true
    ny_remove_file "$DASH_UNIT_FILE"
    ny_service_daemon_reload
    dashboard_firewall_remove "${port:-$DASH_DEFAULT_PORT}"
    ny_ok "The dashboard is stopped and its service removed. Start it again with: nodeyard dashboard start"
    ny_hint "Its sign-in password is kept (nodeyard dashboard password --show); delete $(dashboard_password_file) to forget it."
    return 0
}

dashboard_password_cmd() {
    ny_need_root
    dashboard_parse "$@"
    local listen pw="" again=""
    local modes=$((DASH_PW_SHOW + DASH_PW_RANDOM + DASH_PW_STDIN))
    [[ "$modes" -le 1 ]] || ny_usage_error "Use only one of --show, --random and --stdin."

    if [[ "$DASH_PW_SHOW" -eq 1 ]]; then
        ny_secret_exists "$DASH_PW_NAME" || ny_die "There is no dashboard password yet." \
            "It is made when you start the dashboard (sudo nodeyard dashboard start), or set one now: sudo nodeyard dashboard password" "$NY_E_PRECONDITION"
        pw="$(ny_secret_get "$DASH_PW_NAME")"
        if [[ "$NY_JSON" -eq 1 ]]; then
            ny_json_out "$(ny_json_obj ok:=true "password=${pw}")"
        else
            printf '%s\n' "$pw"
        fi
        return 0
    fi

    if [[ "$DASH_PW_RANDOM" -eq 1 ]]; then
        pw="$(dashboard_new_password)"
    elif [[ "$DASH_PW_STDIN" -eq 1 ]]; then
        IFS= read -r pw || [[ -n "$pw" ]] || ny_usage_error "--stdin needs the new password on standard input."
    else
        ny_ui_interactive || ny_die "Setting a password needs a terminal to type it in." \
            "Pipe it in instead: printf '%s' 'your password' | sudo nodeyard dashboard password --stdin" "$NY_E_PRECONDITION"
        pw="$(ny_ui_secret "New dashboard password")" || return 0
        again="$(ny_ui_secret "Type it again")" || return 0
        [[ "$pw" == "$again" ]] || ny_die "The two passwords didn't match." "Nothing was changed. Try again." "$NY_E_FAIL"
    fi
    pw="${pw%$'\r'}"
    ny_secret_register "$pw"
    ((${#pw} >= 1)) || ny_die "The password can't be empty." "" "$NY_E_USAGE"
    if ! dashboard_weak_ok; then
        ((${#pw} >= 6)) || ny_die "That password is too short: use at least 6 characters." \
            "Or allow any password: sudo nodeyard config set dashboard.weak-password true" "$NY_E_USAGE"
    fi
    ((${#pw} <= 200)) || ny_die "That password is too long (200 characters at most)." "" "$NY_E_USAGE"
    [[ "$pw" != *[[:cntrl:]]* ]] || ny_die "Passwords can't contain control characters." "" "$NY_E_USAGE"

    printf '%s' "$pw" | ny_secret_set "$DASH_PW_NAME"
    if [[ "$DASH_PW_NORESTART" -eq 1 ]]; then
        ny_ok "Password saved (the running dashboard was told directly)."
    elif [[ -f "$(ny_path "$DASH_UNIT_FILE")" ]] && ! ny_simulating; then
        ny_run systemctl restart "${DASH_UNIT}.service"
        ny_ok "Password changed; everyone who was signed in has to sign in again."
    else
        ny_ok "Password saved. It takes effect when the dashboard starts (sudo nodeyard dashboard start)."
    fi
    if [[ "$DASH_PW_RANDOM" -eq 1 ]]; then
        printf '%s\n' "$pw"
    fi
    ((${#pw} >= 10)) || ny_hint "Short passwords are easier to guess. The dashboard locks an address out for 5 minutes after 5 wrong tries, and only Tailscale (and this machine) can reach it."
    listen="$(dashboard_unit_value listen)"
    [[ -z "$listen" ]] || ny_hint "Listening on: ${listen}"
    return 0
}

# --- node agents -------------------------------------------------------------

dashboard_agent_manifest() {
    local token script
    if ny_secret_exists "$DASH_AGENT_TOKEN"; then
        token="$(ny_secret_get "$DASH_AGENT_TOKEN")"
    else
        token="(made when you install)" # a dry run, before the secret exists
    fi
    if [[ "${DASH_AGENT_ELIDE:-0}" == 1 ]]; then
        script="    # (the agent program, $(wc -l <"${NY_SHARE}/agent/agent.py" | tr -d ' ') lines, is stored here)"
    else
        script="$(sed 's/^/    /' "${NY_SHARE}/agent/agent.py")"
    fi
    cat <<YAML
apiVersion: v1
kind: Namespace
metadata:
  name: ${DASH_AGENT_NS}
  labels:
    pod-security.kubernetes.io/enforce: privileged
---
apiVersion: v1
kind: Secret
metadata: {name: nodeyard-agent-token, namespace: ${DASH_AGENT_NS}}
type: Opaque
stringData: {token: "${token}"}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: nodeyard-agent-script, namespace: ${DASH_AGENT_NS}}
data:
  agent.py: |
${script}
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: nodeyard-agent
  namespace: ${DASH_AGENT_NS}
  labels: {app: nodeyard-agent}
spec:
  selector:
    matchLabels: {app: nodeyard-agent}
  updateStrategy: {type: RollingUpdate, rollingUpdate: {maxUnavailable: 1}}
  template:
    metadata:
      labels: {app: nodeyard-agent}
      # a new agent program restarts the pods (a ConfigMap change alone doesn't)
      annotations: {nodeyard/program-sha256: "$(ny_sha256 "${NY_HOME}/share/nodeyard/agent/agent.py" 2>/dev/null || echo unknown)"}
    spec:
      hostPID: true
      # on the host's network so it sees the machine's real network links
      # (and their speed); it still only answers requests with the token
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      tolerations: [{operator: Exists}]
      terminationGracePeriodSeconds: 2
      # GPU nodes run the GPU flavour below instead
      affinity: {nodeAffinity: {requiredDuringSchedulingIgnoredDuringExecution: {nodeSelectorTerms: [{matchExpressions: [{key: nodeyard.io/gpu, operator: NotIn, values: [nvidia]}]}]}}}
      containers:
      - name: agent
        image: ${DASH_AGENT_IMAGE}
        command: ["python", "-u", "/agent/agent.py", "--passwd", "/host-passwd"]
        ports: [{containerPort: 9093}]
        env:
        - {name: PYTHONDONTWRITEBYTECODE, value: "1"}
        - {name: NODE_NAME, valueFrom: {fieldRef: {fieldPath: spec.nodeName}}}
        - {name: NODE_IP, valueFrom: {fieldRef: {fieldPath: status.hostIP}}}
        - {name: AGENT_TOKEN, valueFrom: {secretKeyRef: {name: nodeyard-agent-token, key: token}}}
        resources:
          requests: {cpu: 10m, memory: 24Mi}
          limits: {cpu: 300m, memory: 96Mi}
        securityContext:
          allowPrivilegeEscalation: false
          readOnlyRootFilesystem: true
          capabilities: {drop: [ALL]}
          runAsUser: 65534
          runAsNonRoot: true
        readinessProbe: {httpGet: {path: /healthz, port: 9093}, periodSeconds: 10}
        volumeMounts:
        - {name: script, mountPath: /agent, readOnly: true}
        - {name: passwd, mountPath: /host-passwd, readOnly: true}
      volumes:
      - {name: script, configMap: {name: nodeyard-agent-script}}
      - {name: passwd, hostPath: {path: /etc/passwd, type: File}}
YAML
    # nodes with an NVIDIA GPU set up for containers (nodeyard ai gpu setup)
    if [[ -n "$(kctl get nodes -l nodeyard.io/gpu=nvidia -o name 2>/dev/null || true)" ]]; then
        printf -- '---\n'
        cat <<YAML
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: nodeyard-agent-gpu
  namespace: ${DASH_AGENT_NS}
  labels: {app: nodeyard-agent}
spec:
  selector:
    matchLabels: {app: nodeyard-agent}
  updateStrategy: {type: RollingUpdate, rollingUpdate: {maxUnavailable: 1}}
  template:
    metadata:
      labels: {app: nodeyard-agent}
      # a new agent program restarts the pods (a ConfigMap change alone doesn't)
      annotations: {nodeyard/program-sha256: "$(ny_sha256 "${NY_HOME}/share/nodeyard/agent/agent.py" 2>/dev/null || echo unknown)"}
    spec:
      hostPID: true
      # on the host's network so it sees the machine's real network links
      # (and their speed); it still only answers requests with the token
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      tolerations: [{operator: Exists}]
      terminationGracePeriodSeconds: 2
      # NVIDIA's container runtime adds nvidia-smi and the GPU's device files (read only)
      runtimeClassName: nvidia
      nodeSelector: {nodeyard.io/gpu: nvidia}
      containers:
      - name: agent
        image: ${DASH_AGENT_GPU_IMAGE}
        command: ["python", "-u", "/agent/agent.py", "--passwd", "/host-passwd"]
        ports: [{containerPort: 9093}]
        env:
        - {name: PYTHONDONTWRITEBYTECODE, value: "1"}
        - {name: NODE_NAME, valueFrom: {fieldRef: {fieldPath: spec.nodeName}}}
        - {name: NODE_IP, valueFrom: {fieldRef: {fieldPath: status.hostIP}}}
        - {name: NVIDIA_VISIBLE_DEVICES, value: all}
        - {name: NVIDIA_DRIVER_CAPABILITIES, value: utility}
        - {name: AGENT_TOKEN, valueFrom: {secretKeyRef: {name: nodeyard-agent-token, key: token}}}
        resources:
          requests: {cpu: 10m, memory: 24Mi}
          limits: {cpu: 300m, memory: 160Mi}
        securityContext:
          allowPrivilegeEscalation: false
          readOnlyRootFilesystem: true
          capabilities: {drop: [ALL]}
          runAsUser: 65534
          runAsNonRoot: true
        readinessProbe: {httpGet: {path: /healthz, port: 9093}, periodSeconds: 10}
        volumeMounts:
        - {name: script, mountPath: /agent, readOnly: true}
        - {name: passwd, mountPath: /host-passwd, readOnly: true}
      volumes:
      - {name: script, configMap: {name: nodeyard-agent-script}}
      - {name: passwd, hostPath: {path: /etc/passwd, type: File}}
YAML
    fi
    return 0
}

dashboard_agent_install_cmd() {
    ny_need_root
    ny_need_kube
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    [[ -r "${NY_SHARE}/agent/agent.py" ]] || ny_die "The agent program is missing from this nodeyard install." "Update nodeyard: sudo nodeyard update" "$NY_E_PRECONDITION"
    ny_step "Setting up the node agent on every node"
    ny_secret_generate "$DASH_AGENT_TOKEN" 24
    ny_info "Each node runs one small read-only pod (${DASH_AGENT_IMAGE}); the first start downloads that image."
    ny_confirm "Run the node agent on every node?" y || return 0
    if ny_simulating; then
        ny_plan_add apply "Apply the node agent DaemonSet in namespace ${DASH_AGENT_NS}"
        if [[ "$NY_DRY_RUN" -eq 1 ]]; then
            DASH_AGENT_ELIDE=1 dashboard_agent_manifest | sed -E 's/(token: ")[^"]*"/\1***"/' >&2
        fi
    else
        dashboard_agent_manifest | kctl apply -f - >/dev/null || ny_die "Applying the node agent failed." "Check: sudo nodeyard dashboard agent status"
        ny_step "Waiting for the agents to start (the first run downloads the image)"
        kctl rollout status daemonset/nodeyard-agent -n "$DASH_AGENT_NS" --timeout=300s || ny_warn "Not finished yet; check: sudo nodeyard dashboard agent status"
    fi
    if ny_simulating; then
        ny_info "Nothing was installed (dry run or demo)."
    else
        ny_ok "The node agent is installed. The dashboard's Processes page fills in within a few seconds."
    fi
    return 0
}

dashboard_agent_remove_cmd() {
    ny_need_root
    ny_need_kube
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    ny_confirm "Remove the node agents (namespace ${DASH_AGENT_NS})?" n || return 0
    ny_run "$(ny_path "$NY_K3S_BIN")" kubectl delete namespace "$DASH_AGENT_NS" --ignore-not-found >/dev/null || true
    ny_ok "The node agents are removed. The dashboard falls back to what the kubelets report."
    return 0
}

dashboard_agent_status_cmd() {
    ny_need_kube
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    if ! kctl get namespace "$DASH_AGENT_NS" >/dev/null 2>&1; then
        ny_info "The node agents aren't installed. Install them with: sudo nodeyard dashboard agent install"
        return 0
    fi
    kctl get pods -n "$DASH_AGENT_NS" -o wide
    return 0
}

dashboard_run_cmd() {
    dashboard_parse "$@"
    ny_deps_ensure "the dashboard" python3
    local py
    py="$(dashboard_python)"
    local -a args=("$(dashboard_server)" --port "$DASH_PORT" --interval "$DASH_INTERVAL" --nodeyard-version "$NY_VERSION")
    if [[ "$NY_DEMO" -eq 1 ]]; then
        args+=(--demo)
        DASH_LISTEN_ARG="local"
        [[ "$DASH_LISTEN" == auto || "$DASH_LISTEN" == local ]] || ny_die "The demo has no sign-in, so it only listens on this machine."
    else
        dashboard_need_cluster
        local name
        name="$(ny_cfg_get cluster "" name)"
        args+=(--kubeconfig "$DASH_KUBECONFIG")
        [[ -z "$name" ]] || args+=(--cluster-name "$name")
        # In the foreground the default stays this machine only; asking for more brings the sign-in.
        [[ "$DASH_LISTEN" != auto ]] || DASH_LISTEN="local"
        dashboard_resolve_listen
        args+=(--listen "$DASH_LISTEN_ARG")
        if [[ "${#DASH_REMOTE_ADDRS[@]}" -gt 0 ]]; then
            ny_need_root
            dashboard_ensure_password
            args+=(--password-file "$(dashboard_password_file)")
        fi
    fi
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        printf '%s %s\n' "$(ny_color cyan "[dry-run] would run:")" "$(ny_quote_cmd "$py" "${args[@]}")" >&2
        return 0
    fi
    if ny_tcp_check 127.0.0.1 "$DASH_PORT" 1; then
        ny_die "Something is already listening on port ${DASH_PORT}." "If that's the dashboard service, it's already running (nodeyard dashboard status). Otherwise pick another port: --port 9093" "$NY_E_PRECONDITION"
    fi
    dashboard_tell_how_to_open "$DASH_PORT" "$DASH_LISTEN_ARG"
    ny_info "Press Ctrl-C to stop."
    # Not exec: nodeyard's own cleanup (temp files) still runs afterwards.
    "$py" "${args[@]}" || {
        local rc=$?
        [[ "$rc" -eq 130 || "$rc" -eq 143 ]] || ny_die "The dashboard stopped with an error (exit ${rc})." "See the message above."
    }
    return 0
}

dashboard_status_cmd() {
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    local installed=false active=false enabled=false port listen
    [[ -f "$(ny_path "$DASH_UNIT_FILE")" ]] && installed=true
    dashboard_is_active && active=true
    systemctl is-enabled --quiet "$DASH_UNIT" 2>/dev/null && enabled=true
    port="$(dashboard_unit_value port)"
    port="${port:-$DASH_DEFAULT_PORT}"
    listen="$(dashboard_unit_value listen)"
    listen="${listen:-local}"
    if [[ "$NY_JSON" -eq 1 ]]; then
        local url="http://localhost:${port}" item
        local -a parts
        IFS=, read -r -a parts <<<"$listen"
        for item in "${parts[@]}"; do
            [[ "$item" == local || "$item" == all ]] || {
                url="http://${item}:${port}"
                break
            }
        done
        ny_json_out "$(ny_json_obj ok:=true "installed:=${installed}" "running:=${active}" "enabled:=${enabled}" "port:=${port}" "listen=${listen}" "url=${url}")"
        return 0
    fi
    if [[ "$active" == true ]]; then
        ny_ok "The dashboard is running on port ${port}."
        dashboard_tell_how_to_open "$port" "$listen"
    elif [[ "$installed" == true ]]; then
        ny_warn "The dashboard service is installed but not running."
        ny_hint "Start it: nodeyard dashboard start     See why it stopped: journalctl -u ${DASH_UNIT} -n 30"
    else
        ny_info "The dashboard isn't running. Start it with: sudo nodeyard dashboard start"
        ny_hint "Want to look first? nodeyard --demo dashboard run shows a simulated cluster."
    fi
    return 0
}
