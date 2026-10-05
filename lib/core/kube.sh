# shellcheck shell=bash
# Talking to Kubernetes: the k3s admin kubeconfig when readable (root on a
# server), otherwise the caller's own, so read-only commands work without
# sudo for a user with kubectl access.

NY_K3S_BIN="/usr/local/bin/k3s"
NY_K3S_KUBECONFIG="/etc/rancher/k3s/k3s.yaml"
NY_K3S_TOKEN_FILE="/var/lib/rancher/k3s/server/node-token"

ny_k3s_installed() {
    [[ -x "$(ny_path "$NY_K3S_BIN")" ]]
}

# ny_kube_config -- path of a readable kubeconfig, or return 1.
ny_kube_config() {
    local admin
    admin="$(ny_path "$NY_K3S_KUBECONFIG")"
    if [[ -r "$admin" ]]; then
        printf '%s\n' "$admin"
        return 0
    fi
    local c="${KUBECONFIG:-${HOME:-/root}/.kube/config}"
    if [[ -r "$c" ]]; then
        printf '%s\n' "$c"
        return 0
    fi
    return 1
}

# kctl ARGS... -- kubectl against this cluster (dies with a fix if impossible).
kctl() {
    local cfg
    cfg="$(ny_kube_config)" ||
        ny_die "No kubeconfig available on this machine." \
            "Run this with sudo on a k3s server, or give your user access: sudo nodeyard kubeconfig --user \$USER" "$NY_E_PRECONDITION"
    if ny_k3s_installed; then
        KUBECONFIG="$cfg" "$(ny_path "$NY_K3S_BIN")" kubectl "$@"
    elif have kubectl; then
        KUBECONFIG="$cfg" kubectl "$@"
    else
        ny_die "kubectl is not available (k3s is not installed on this machine)." \
            "Run this on a cluster server, or install kubectl." "$NY_E_PRECONDITION"
    fi
}

# kctl_available -- like kctl's preconditions, but returns false instead of dying.
kctl_available() {
    ny_kube_config >/dev/null 2>&1 || return 1
    ny_k3s_installed || have kubectl
}

kctl_quiet() {
    kctl_available || return 1
    kctl "$@" >/dev/null 2>&1
}

# ny_need_kube -- for commands that only need Kubernetes API access.
ny_need_kube() {
    kctl_available ||
        ny_die "This needs access to the cluster's Kubernetes API." \
            "Run it with sudo on a k3s server, or set up a kubeconfig for your user: sudo nodeyard kubeconfig --user \$USER" "$NY_E_PRECONDITION"
}

# ny_wait_service UNIT [TRIES] -- wait for a systemd unit to become active.
ny_wait_service() {
    local unit="${1:-k3s}" tries=0 max="${2:-60}"
    ny_simulating && return 0
    while ((tries < max)); do
        systemctl is-active --quiet "$unit" 2>/dev/null && return 0
        sleep 2
        tries=$((tries + 1))
    done
    return 1
}

# ny_wait_node_ready [NODE] -- wait for a node to report Ready.
ny_wait_node_ready() {
    local node="${1:-$(hostname)}" tries=0 max=60
    ny_simulating && return 0
    while ((tries < max)); do
        if grep -qw Ready <<<"$(kctl get node "$node" --no-headers 2>/dev/null | awk '{print $2}' || true)"; then
            return 0
        fi
        sleep 3
        tries=$((tries + 1))
    done
    return 1
}

# ny_k8s_name TEXT -- a valid Kubernetes object name derived from TEXT.
ny_k8s_name() {
    local n="${1,,}"
    n="${n//[^a-z0-9-]/-}"
    printf '%s\n' "${n:0:40}"
}

# ny_tcp_check HOST PORT [TIMEOUT] -- can we open a TCP connection?
ny_tcp_check() {
    local host="$1" port="$2" t="${3:-4}"
    timeout "$t" bash -c 'cat </dev/null >"/dev/tcp/$1/$2"' _ "$host" "$port" 2>/dev/null
}
