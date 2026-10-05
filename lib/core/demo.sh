# shellcheck shell=bash
# Demo mode: run nodeyard against a simulated cluster so it can be tried,
# tested and screenshotted without hardware.
#
# * Every system path points into a sandbox copy of share/nodeyard/demo/fs.
# * System commands (ip, systemctl, kubectl, ...) are replaced by a shim
#   that answers from share/nodeyard/demo/rules.
# * ny_run never executes anything; file writes land in the sandbox.
#
# The sandbox lives in ${NODEYARD_DEMO_DIR:-~/.cache/nodeyard-demo}; delete it
# (or run 'nodeyard demo reset') to start over.

NY_DEMO_SHIMS=(ip systemctl nmcli netplan kubectl k3s uname hostname findmnt lsblk swapon lsmod modprobe sysctl
    iptables nft ufw firewall-cmd ss journalctl ping nproc systemd-detect-virt lspci nvidia-smi
    ssh scp ssh-keyscan ssh-keygen rc-service timedatectl chronyc tailscale snap docker getent
    apt-get dnf yum zypper pacman apk ollama networkctl)

ny_demo_dir() {
    printf '%s\n' "${NODEYARD_DEMO_DIR:-${XDG_CACHE_HOME:-${HOME:-/tmp}/.cache}/nodeyard-demo}"
}

ny_demo_reset() {
    local dir
    dir="$(ny_demo_dir)"
    [[ "$dir" == */nodeyard-demo* || -n "${NODEYARD_DEMO_DIR:-}" ]] || return 1
    rm -rf -- "$dir"
}

ny_demo_setup() {
    local dir src
    dir="$(ny_demo_dir)"
    src="${NY_SHARE}/demo"
    if [[ ! -d "${dir}/fs" ]]; then
        mkdir -p -- "$dir"
        cp -R -- "${src}/fs" "${dir}/fs"
        # Directories git can't keep empty.
        mkdir -p -- "${dir}/fs/run/systemd/system" "${dir}/fs/sys/class/net/eth0/device" \
            "${dir}/fs/sys/class/net/wlan0/wireless" "${dir}/fs/usr/local/bin"
    fi
    mkdir -p -- "${dir}/shims" "${dir}/fs/usr/local/bin"
    local c
    for c in "${NY_DEMO_SHIMS[@]}"; do
        ln -sfn -- "${src}/shim.sh" "${dir}/shims/${c}"
    done
    ln -sfn -- "${src}/shim.sh" "${dir}/fs/usr/local/bin/k3s"

    export NODEYARD_DEMO=1
    export NODEYARD_ROOT="${dir}/fs"
    export NODEYARD_SHIM_RULES="${src}/rules"
    export NODEYARD_SHIM_DATA="${src}/data"
    case ":${PATH}:" in
        *":${dir}/shims:"*) ;;
        *) export PATH="${dir}/shims:${PATH}" ;;
    esac
    NY_DEMO=1
    ny_paths_init
}
