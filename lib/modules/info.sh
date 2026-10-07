# shellcheck shell=bash
# What this machine is: distro, hardware, network. Also version/help and
# shell completion.

ny_cmd "detect" info_detect_cmd "Start here" "Show what nodeyard detected about this machine" info json
ny_cmd "sysinfo" info_detect_cmd "Health" "Hardware, OS and network summary (same as detect)" info json
ny_cmd "network-info" info_network_cmd "Network" "List network interfaces, addresses and the default route" info json
ny_cmd "version" info_version_cmd "Tool" "Show the nodeyard version" info json
ny_cmd "help" info_help_cmd "Tool" "List every command" info
ny_cmd "commands" info_commands_cmd "Tool" "Every command with its help, as JSON (the dashboard's Commands page uses it)" info hidden,json
ny_cmd "completion" info_completion_cmd "Tool" "Print a shell completion script (bash or zsh)" info

info_version_cmd() {
    if [[ "$NY_JSON" -eq 1 ]]; then
        ny_json_out "$(ny_json_obj ok:=true "version=$NY_VERSION")"
    else
        printf 'nodeyard %s\n' "$NY_VERSION"
    fi
    return 0
}

info_help_cmd() {
    if [[ $# -gt 0 ]] && ny_cmd_resolve "$@"; then
        ny_help_command "$NY_RESOLVED_PATH"
    elif [[ $# -gt 0 && -n "$(ny_cmd_children "$1")" ]]; then
        ny_help_group "$1"
    else
        ny_help_main
    fi
    return 0
}

# info_commands_cmd -- [{path, group, summary, help}] for every listed command
info_commands_cmd() {
    local g p first=1
    {
        printf '{"ok":true,"commands":['
        for g in "${NY_GROUP_ORDER[@]}"; do
            for p in "${NY_CMD_ORDER[@]}"; do
                [[ -n "${NY_CMD_HIDDEN[$p]:-}" || "${NY_CMD_GROUP[$p]}" != "$g" ]] && continue
                [[ $first -eq 1 ]] || printf ','
                first=0
                jq -cn --arg p "$p" --arg g "$g" --arg s "${NY_CMD_SUM[$p]}" --arg h "$(NY_COLOR=never ny_help_command "$p" 2>/dev/null || true)" \
                    '{path: $p, group: $g, summary: $s, help: $h}'
            done
        done
        printf ']}'
    } | if [[ "$NY_JSON" -eq 1 ]]; then
        ny_json_out "$(jq -c .)"
    else
        jq -r '.commands[] | "\(.group): \(.path)  \(.summary)"'
    fi
    return 0
}

info_completion_cmd_help() {
    cat <<'HELP'
Usage: nodeyard completion bash|zsh

Prints a completion script. To enable it:
  bash:  nodeyard completion bash | sudo tee /etc/bash_completion.d/nodeyard
  zsh:   nodeyard completion zsh > "${fpath[1]}/_nodeyard"
(install.sh sets these up for you.)
HELP
}

info_completion_cmd() {
    case "${1:-}" in
        bash) cat "${NY_HOME}/completions/nodeyard.bash" ;;
        zsh) cat "${NY_HOME}/completions/_nodeyard" ;;
        *) ny_usage_error "Say which shell: bash or zsh." "nodeyard completion bash|zsh" ;;
    esac
    return 0
}

# info_gpu -- a short description of any GPU.
info_gpu() {
    if have nvidia-smi && nvidia-smi -L >/dev/null 2>&1; then
        printf 'NVIDIA: %s\n' "$(nvidia-smi -L 2>/dev/null | head -n1 | sed 's/ (UUID.*//')"
        return 0
    fi
    local pci=""
    have lspci && pci="$(lspci 2>/dev/null || true)"
    if grep -qiE '(vga|3d|display).*nvidia' <<<"$pci"; then
        echo "NVIDIA (driver not installed)"
    elif grep -qiE '(vga|3d|display).*(amd|ati|radeon)' <<<"$pci"; then
        echo "AMD"
    elif grep -qiE '(vga|3d|display).*intel' <<<"$pci"; then
        echo "Intel (integrated)"
    else
        echo "none"
    fi
    return 0
}

info_detect_cmd_help() {
    cat <<'HELP'
Usage: nodeyard detect [--json]

Shows what nodeyard detected about this machine: distribution and whether
it is supported, package manager, init system, architecture, hardware,
boot disk, network interfaces and which tool manages them, and firewall.
HELP
}

info_detect_cmd() {
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    ny_detect_all
    local dev src gw backend fw gpu timesync="no" k3s_state="not installed"
    read -r dev src gw <<<"$(ny_primary_route)"
    backend="$(ny_detect_netbackend "${dev#-}")"
    fw="$(ny_detect_firewall)"
    gpu="$(info_gpu)"
    host_time_sync_active && timesync="yes"
    k3s_load_state
    if ny_k3s_installed; then
        k3s_state="installed (${K3S_ROLE:-unknown role})"
    fi
    local -a ifaces=()
    local name kind state cidr mac
    while IFS=$'\t' read -r name kind state cidr; do
        [[ -n "$name" ]] || continue
        mac="$(cat "$(ny_path "/sys/class/net/${name}/address")" 2>/dev/null || true)"
        ifaces+=("$(ny_json_obj "name=$name" "kind=$kind" "state=$state" "address?=$cidr" "mac?=$mac")")
    done < <(ny_list_ifaces)

    if [[ "$NY_JSON" -eq 1 ]]; then
        ny_json_out "$(ny_json_obj ok:=true "hostname=$(hostname 2>/dev/null || uname -n)" "node=$(ny_self_name)" \
            "os:=$(ny_json_obj "id=$NY_OS_ID" "version=$NY_OS_VERSION" "label=$NY_OS_LABEL" "family=$NY_OS_FAMILY" "support=$NY_OS_SUPPORT" "note?=$NY_OS_NOTE")" \
            "package_manager?=$NY_PKG" "init=$NY_INIT" "arch=$NY_ARCH" \
            "hardware:=$(ny_json_obj "model=$NY_HW_MODEL" "raspberry_pi:=$(ny_json_bool "$NY_IS_PI")" "ram_mb:=$NY_RAM_MB" "cpus:=$NY_CPUS" "gpu=$gpu" "boot_disk=$NY_BOOT_DISK" "boot_disk_model?=$NY_BOOT_DISK_MODEL" "container=$NY_CONTAINER")" \
            "network:=$(ny_json_obj "primary_interface?=${dev#-}" "address?=${src#-}" "gateway?=${gw#-}" "backend=$backend" "firewall=$fw" "interfaces:=$(ny_json_arr "${ifaces[@]+"${ifaces[@]}"}")")" \
            "time_sync:=$(ny_json_bool "$([[ $timesync == yes ]] && echo 1)")" "k3s=$k3s_state")"
        return 0
    fi

    local support
    support="$(ny_status_color "$NY_OS_SUPPORT")"
    printf '%s\n' "$(ny_color bold "This machine")"
    printf '  %-14s %s\n' "Hostname:" "$(hostname 2>/dev/null || uname -n)" \
        "OS:" "${NY_OS_LABEL} (${support})" \
        "Packages:" "${NY_PKG:-none found}" "Init:" "$NY_INIT" "Architecture:" "${NY_ARCH} (${NY_ARCH_RAW})" \
        "Hardware:" "$NY_HW_MODEL" "CPU / RAM:" "${NY_CPUS} cores, $(awk -v m="$NY_RAM_MB" 'BEGIN{printf "%.1f", m/1024}') GiB" \
        "GPU:" "$gpu" "Boot disk:" "${NY_BOOT_DISK}${NY_BOOT_DISK_MODEL:+ (${NY_BOOT_DISK_MODEL})}" \
        "Time sync:" "$timesync" "k3s:" "$k3s_state"
    [[ -n "$NY_OS_NOTE" ]] && ny_hint "$NY_OS_NOTE"
    if [[ "$NY_BOOT_DISK" == sd ]]; then
        ny_hint "Running from an SD card: fine for workers, but SD cards wear out and etcd on them is often unstable. Use an SSD for servers if you can."
    fi
    printf '\n%s\n' "$(ny_color bold "Network")"
    printf '  %-14s %s\n' "Default route:" "${dev#-}${src:+ ($src)} via ${gw#-}" "Managed by:" "$backend" "Firewall:" "$fw"
    printf '\n'
    info_iface_table
}

info_iface_table() {
    local name kind state cidr mac
    {
        printf 'INTERFACE\tTYPE\tSTATE\tADDRESS\tMAC\n'
        while IFS=$'\t' read -r name kind state cidr; do
            [[ -n "$name" ]] || continue
            mac="$(cat "$(ny_path "/sys/class/net/${name}/address")" 2>/dev/null || echo -)"
            printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$kind" "$state" "${cidr:--}" "$mac"
        done < <(ny_list_ifaces)
    } | ny_table --status STATE
}

info_network_cmd() {
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    if [[ "$NY_JSON" -eq 1 ]]; then
        info_detect_cmd "$@"
        return 0
    fi
    info_iface_table
    local dev src gw ts
    read -r dev src gw <<<"$(ny_primary_route)"
    printf '\n%s %s\n' "$(ny_color bold "Default route:")" "$([[ $dev == - ]] && echo "none (fine if this network is cluster-only by design)" || echo "${dev} via ${gw} (source ${src})")"
    if have tailscale; then
        ts="$(ny_tailscale_ip || true)"
        printf '%s %s\n' "$(ny_color bold "Tailscale:")" "${ts:-not connected}"
    fi
    return 0
}
