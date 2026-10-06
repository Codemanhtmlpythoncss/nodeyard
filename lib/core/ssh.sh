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

# ny_ssh_stored_fps NAME FILE -- SHA256 fingerprints of the keys FILE holds for NAME.
ny_ssh_stored_fps() {
    [[ -f "$2" ]] || return 0
    ssh-keygen -l -F "$1" -f "$2" 2>/dev/null | grep -oE 'SHA256:[A-Za-z0-9+/=]+' || true
    return 0
}

# ny_ssh_fps_overlap LIST_A LIST_B -- true if the two newline-separated
# fingerprint lists have one in common.
ny_ssh_fps_overlap() {
    local a b
    while IFS= read -r a; do
        [[ -n "$a" ]] || continue
        while IFS= read -r b; do
            [[ "$a" == "$b" ]] && return 0
        done <<<"$2"
    done <<<"$1"
    return 1
}

# ny_ssh_trust_host HOST [PORT] [EXPECTED_FINGERPRINT] -- make sure HOST's key
# is in nodeyard's known_hosts and matches what the machine presents NOW.
#   - a saved key that still matches: fine, nothing to ask;
#   - a key in your own ~/.ssh/known_hosts is only reused if it matches the
#     machine's current key (an out-of-date one is ignored, never copied);
#   - a new machine, or one whose key changed (e.g. after a reinstall): the
#     fingerprint is shown and you confirm it at the terminal. --yes does not
#     skip this; without a terminal, pass --host-key FINGERPRINT.
ny_ssh_trust_host() {
    local host="$1" port="${2:-22}" expected="${3:-}"
    local kh name ukh
    kh="$(ny_ssh_known_hosts)"
    name="$(ny_ssh_keyname "$host" "$port")"
    ukh="$(ny_ssh_user_known_hosts)"
    if [[ "$NY_DRY_RUN" -ne 1 ]]; then
        mkdir -p "$(ny_ssh_dir)"
        chmod 0700 "$(ny_ssh_dir)"
        touch "$kh"
    fi

    # What the machine presents right now.
    local scanned fps live fp_main
    scanned="$(ssh-keyscan -T 6 -p "$port" "$host" 2>/dev/null || true)"
    [[ -n "$scanned" ]] || ny_die "Could not read the SSH host key of ${host}:${port}." \
        "Check that the machine is on, SSH is running (sudo systemctl status ssh) and port ${port} is reachable."
    fps="$(ssh-keygen -lf - <<<"$scanned" 2>/dev/null || true)"
    live="$(grep -oE 'SHA256:[A-Za-z0-9+/=]+' <<<"$fps" || true)"
    fp_main="$(awk '/ED25519/{print $2; exit}' <<<"$fps")"
    [[ -n "$fp_main" ]] || fp_main="$(awk 'NR==1{print $2}' <<<"$fps")"

    if [[ -n "$expected" ]] && ! grep -qxF -- "$expected" <<<"$live"; then
        ny_die "The SSH host key of ${host} does not match the fingerprint you gave (${expected}); it presents ${fp_main}." \
            "If the machine was reinstalled, check its fingerprint on its console: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub. Otherwise someone may be intercepting the connection."
    fi

    # Keys we already know for this machine.
    local own_stored user_stored old=""
    own_stored="$(ny_ssh_stored_fps "$name" "$kh")"
    if ny_ssh_fps_overlap "$own_stored" "$live"; then
        return 0
    fi
    [[ -z "$own_stored" ]] || old="$own_stored"
    if [[ -z "$own_stored" && -z "$expected" ]]; then
        user_stored="$(ny_ssh_stored_fps "$name" "$ukh")"
        if ny_ssh_fps_overlap "$user_stored" "$live"; then
            if [[ "$NY_DRY_RUN" -ne 1 ]]; then
                ssh-keygen -F "$name" -f "$ukh" | grep -v '^#' >>"$kh"
            fi
            ny_vlog "trusting ${name}: it matches the key in ${ukh}"
            return 0
        fi
        if [[ -n "$user_stored" ]]; then
            ny_vlog "ignoring an out-of-date key for ${name} in ${ukh}"
            old="$user_stored"
        fi
    fi
    local changed=0
    [[ -z "$old" ]] || changed=1

    if [[ -z "$expected" ]]; then
        if [[ "$changed" -eq 1 ]]; then
            ny_warn "The SSH host key of ${host} has CHANGED since it was last trusted."
            ny_info "    Trusted before:  $(paste -sd' ' - <<<"$old")"
            ny_info "    Presents now:    ${fp_main}"
            ny_hint "That is expected if the machine was reinstalled, had its disk or SD card replaced, or another device took its address."
            ny_hint "It is NOT expected otherwise: it can mean someone is intercepting the connection."
        else
            ny_info "First connection to ${host}. Its SSH host key fingerprint is:"
            ny_info "    ${fp_main}"
        fi
        ny_hint "To be sure, compare with what the machine itself shows: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub"
        if [[ "$NY_DRY_RUN" -eq 1 ]]; then
            ny_info "[dry-run] would ask you to confirm this host key."
            return 0
        fi
        if ! ny_ui_interactive; then
            ny_die "Not trusting an unverified SSH host key for ${host} without confirmation." \
                "Run this at a terminal, or pass --host-key ${fp_main} once you have checked it." "$NY_E_CONFIRM"
        fi
        local default=y what="this"
        if [[ "$changed" -eq 1 ]]; then
            default=n
            what="the new"
        fi
        ny_ui_yesno "Trust ${what} host key for ${host}?" "$default" ||
            ny_die "Host key not trusted; nothing was changed on ${host}." "" "$NY_E_CANCELLED"
    fi
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        ny_info "[dry-run] would trust the SSH host key of ${host} (${fp_main})."
        return 0
    fi
    if [[ -n "$own_stored" ]]; then
        ssh-keygen -R "$name" -f "$kh" >/dev/null 2>&1 || true
    fi
    printf '%s\n' "$scanned" | grep -v '^#' >>"$kh"
    ny_ok "Trusted the SSH host key of ${host} (${fp_main})."
    return 0
}

# ny_ssh_failure_fix ERRFILE HOST PORT TARGET -- a plain-English next step for
# a failed ssh login, from what ssh said.
ny_ssh_failure_fix() {
    local errf="$1" host="$2" port="$3" target="$4" text
    text="$(<"$errf")"
    case "$text" in
        *"HOST IDENTIFICATION HAS CHANGED"* | *"Host key verification failed"*)
            echo "The machine's SSH key no longer matches the one nodeyard saved (a reinstall changes it). Run this again: nodeyard will show the new fingerprint and ask you to confirm it."
            ;;
        *"Permission denied"*) echo "The user name or password was refused. Try it by hand: ssh -p ${port} ${target}" ;;
        *"Connection refused"*) echo "Nothing is listening on port ${port}. Is SSH running on ${host}? (sudo systemctl status ssh)" ;;
        *"timed out"* | *"No route to host"* | *"unreachable"*) echo "${host} did not answer: is it on, on the same network, and not blocking port ${port}?" ;;
        *) echo "Try it by hand to see the reason: ssh -p ${port} ${target}" ;;
    esac
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
