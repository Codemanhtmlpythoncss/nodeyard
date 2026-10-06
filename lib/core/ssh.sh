# shellcheck shell=bash
# shellcheck disable=SC2034 # globals here are read by other files
# SSH to other machines with real host-key verification: a host's key is
# trusted only if you already trust it, you confirm its fingerprint, or you
# pass the expected fingerprint. Host-key checking is never turned off.

ny_ssh_dir() {
    printf '%s/ssh\n' "$NY_STATE"
}

ny_ssh_known_hosts() {
    printf '%s/known_hosts\n' "$(ny_ssh_dir)"
}

# ny_ssh_split TARGET -- sets NY_SSH_USER, NY_SSH_HOST from user@host.
ny_ssh_split() {
    local t="$1"
    if [[ "$t" == *@* ]]; then
        NY_SSH_USER="${t%@*}"
        NY_SSH_HOST="${t##*@}"
    else
        NY_SSH_USER=""
        NY_SSH_HOST="$t"
    fi
    return 0
}

# ny_ssh_keyname HOST PORT -- the known_hosts name for a host.
ny_ssh_keyname() {
    if [[ "${2:-22}" == 22 ]]; then
        printf '%s\n' "$1"
    else
        printf '[%s]:%s\n' "$1" "$2"
    fi
    return 0
}

ny_ssh_user_known_hosts() {
    local home=""
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]] && have getent; then
        home="$(getent passwd "$SUDO_USER" | cut -d: -f6 || true)"
    fi
    [[ -n "$home" ]] || home="${HOME:-/root}"
    printf '%s/.ssh/known_hosts\n' "$home"
}

# ny_ssh_trust_host HOST [PORT] [EXPECTED_FINGERPRINT] -- make sure HOST's key
# is in nodeyard's known_hosts, verified by the user or the given fingerprint.
ny_ssh_trust_host() {
    local host="$1" port="${2:-22}" expected="${3:-}"
    local kh name
    kh="$(ny_ssh_known_hosts)"
    name="$(ny_ssh_keyname "$host" "$port")"
    if [[ "$NY_DRY_RUN" -ne 1 ]]; then
        mkdir -p "$(ny_ssh_dir)"
        chmod 0700 "$(ny_ssh_dir)"
        touch "$kh"
    fi

    if [[ -f "$kh" ]] && ssh-keygen -F "$name" -f "$kh" >/dev/null 2>&1; then
        return 0
    fi
    local ukh
    ukh="$(ny_ssh_user_known_hosts)"
    if [[ -z "$expected" && -r "$ukh" ]] && ssh-keygen -F "$name" -f "$ukh" >/dev/null 2>&1; then
        [[ "$NY_DRY_RUN" -eq 1 ]] || ssh-keygen -F "$name" -f "$ukh" | grep -v '^#' >>"$kh"
        ny_vlog "trusting ${name}: already in ${ukh}"
        return 0
    fi

    local scanned
    scanned="$(ssh-keyscan -T 6 -p "$port" "$host" 2>/dev/null || true)"
    [[ -n "$scanned" ]] || ny_die "Could not read the SSH host key of ${host}:${port}." \
        "Check that the machine is on, SSH is running (sudo systemctl status ssh) and port ${port} is reachable."

    local fps fp_main
    fps="$(ssh-keygen -lf - <<<"$scanned" 2>/dev/null || true)"
    fp_main="$(awk '/ED25519/{print $2; exit}' <<<"$fps")"
    [[ -n "$fp_main" ]] || fp_main="$(awk 'NR==1{print $2}' <<<"$fps")"

    if [[ -n "$expected" ]]; then
        if ! grep -qF -- "$expected" <<<"$fps"; then
            ny_die "The SSH host key of ${host} does not match the fingerprint you gave (${expected}); it reported ${fp_main}." \
                "If the machine was reinstalled, check its fingerprint on its console: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub. Otherwise someone may be intercepting the connection."
        fi
    else
        ny_info "First connection to ${host}. Its SSH host key fingerprint is:"
        ny_info "    ${fp_main}"
        ny_hint "To be sure it's really that machine, compare with what its own console shows for:"
        ny_hint "    ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub"
        if [[ "$NY_YES" -eq 1 ]] || ! ny_ui_interactive; then
            ny_die "Not trusting an unverified SSH host key for ${host} without confirmation." \
                "Run interactively, or pass --host-key ${fp_main} once you have checked it." "$NY_E_CONFIRM"
        fi
        ny_ui_yesno "Trust this host key for ${host}?" n ||
            ny_die "Host key not trusted; nothing was changed on ${host}." "" "$NY_E_CANCELLED"
    fi
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        ny_info "[dry-run] would trust the SSH host key of ${host} (${fp_main})."
        return 0
    fi
    printf '%s\n' "$scanned" | grep -v '^#' >>"$kh"
    ny_ok "Trusted the SSH host key of ${host} (${fp_main})."
}

# ny_ssh_opts HOST [PORT] -- fills NY_SSH_OPTS for ssh/scp.
ny_ssh_opts() {
    local port="${2:-22}"
    NY_SSH_OPTS=(-o "UserKnownHostsFile=$(ny_ssh_known_hosts)" -o StrictHostKeyChecking=yes
    -o ConnectTimeout=10 -o ServerAliveInterval=15
    -o ControlMaster=auto -o "ControlPath=$(ny_ssh_dir)/cm-%C" -o ControlPersist=300
    -o "Port=${port}")
}

# ny_ssh_close TARGET -- close a shared connection.
ny_ssh_close() {
    ssh "${NY_SSH_OPTS[@]}" -O exit "$1" >/dev/null 2>&1 || true
}

# ny_ssh_preflight HOST [PORT] -- explain "no route to host" before trying.
ny_ssh_preflight() {
    local host="$1" port="${2:-22}"
    ny_step "Checking that ${host} is reachable over SSH"
    if ! ping -c1 -W2 "$host" >/dev/null 2>&1; then
        ny_warn "No ping reply from ${host}. It may be off, on another network, or blocking ping."
    fi
    if ny_tcp_check "$host" "$port" 5; then
        ny_ok "TCP ${host}:${port} is reachable."
        return 0
    fi
    ny_warn "Cannot reach ${host}:${port} (no route to host, or connection refused)."
    ny_hint "On the target machine, check:"
    ny_hint "  SSH is running:        sudo systemctl status ssh   (or sshd)"
    ny_hint "  The firewall allows it: sudo ufw status / sudo firewall-cmd --list-all"
    ny_hint "  The network is up:     ip -br addr"
    ny_hint "  Same subnet/route:     ip route"
    ny_offer "Try connecting anyway?" n ||
        ny_die "Cannot reach ${host}:${port}." "Fix the connection (see the checklist above) and try again."
}
