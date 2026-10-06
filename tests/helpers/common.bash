# shellcheck shell=bash
# Shared setup for nodeyard's bats tests.
#
# Two ways to test:
#   ny_lib_setup   source the libraries into the test shell, with every
#                  system path redirected into a private sandbox and system
#                  commands answered by the shim (see share/nodeyard/demo).
#   ny_cmd_run     run bin/nodeyard --demo in a private demo sandbox.
# Per-test command answers go in "$BATS_TEST_TMPDIR/rules" (ny_rule).

NY_REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd -P)"
export NY_REPO_ROOT

bats_require_minimum_version 1.5.0
load "${NY_REPO_ROOT}/.tools/bats-support/load.bash"
load "${NY_REPO_ROOT}/.tools/bats-assert/load.bash"

# ny_rule GLOB RC [OUTPUT] -- add a per-test shim answer (checked first).
ny_rule() {
    printf '%s\t%s\t%s\n' "$1" "$2" "${3:-}" >>"${BATS_TEST_TMPDIR}/rules"
}

ny_shims() {
    local dir="${BATS_TEST_TMPDIR}/shims" c
    mkdir -p "$dir"
    for c in curl timeout ip systemctl nmcli netplan kubectl k3s uname hostname findmnt lsblk swapon lsmod modprobe sysctl \
        iptables nft ufw firewall-cmd ss journalctl ping nproc systemd-detect-virt lspci nvidia-smi ssh scp ssh-keyscan \
        ssh-keygen rc-service timedatectl chronyc tailscale snap docker getent apt-get dnf yum zypper pacman apk flock networkctl ifup; do
        ln -sfn "${NY_REPO_ROOT}/share/nodeyard/demo/shim.sh" "${dir}/${c}"
    done
    printf '%s\n' "$dir"
}

ny_lib_setup() {
    export NODEYARD_ROOT="${BATS_TEST_TMPDIR}/root"
    mkdir -p "$NODEYARD_ROOT"
    if [[ "${1:-}" == --demo-fs ]]; then
        cp -R "${NY_REPO_ROOT}/share/nodeyard/demo/fs/." "$NODEYARD_ROOT/"
    fi
    mkdir -p "${NODEYARD_ROOT}/run/systemd/system"
    : >"${BATS_TEST_TMPDIR}/rules"
    export NODEYARD_SHIM_RULES="${BATS_TEST_TMPDIR}/rules:${NY_REPO_ROOT}/share/nodeyard/demo/rules"
    export NODEYARD_SHIM_DATA="${NY_REPO_ROOT}/share/nodeyard/demo/data"
    export NODEYARD_SHIM_LOG="${BATS_TEST_TMPDIR}/calls.log"
    PATH="$(ny_shims):${PATH}"
    export PATH
    export NODEYARD_COLOR=never
    unset NODEYARD_DEMO NODEYARD_UI
    NY_HOME="$NY_REPO_ROOT"
    # shellcheck source=../../lib/nodeyard.sh
    source "${NY_REPO_ROOT}/lib/nodeyard.sh"
    ny_term_init
}

ny_cmd_setup() {
    export NODEYARD_DEMO_DIR="${BATS_TEST_TMPDIR}/demo"
    : >"${BATS_TEST_TMPDIR}/rules"
    export NODEYARD_SHIM_RULES_EXTRA="${BATS_TEST_TMPDIR}/rules"
    export NODEYARD_SHIM_LOG="${BATS_TEST_TMPDIR}/calls.log"
    export NODEYARD_COLOR=never
    unset NODEYARD_UI
    # Build the sandbox once so tests can edit it before running commands.
    "${NY_REPO_ROOT}/bin/nodeyard" --demo version >/dev/null 2>&1
    DEMO_ROOT="${NODEYARD_DEMO_DIR}/fs"
    export DEMO_ROOT
}

# ny_cmd_run ARGS... -- run nodeyard in demo mode (use with bats' run).
ny_cmd_run() {
    "${NY_REPO_ROOT}/bin/nodeyard" --demo "$@"
}

# assert_json -- the last command's output is one valid JSON document.
# shellcheck disable=SC2154 # $output is set by bats' run
assert_json() {
    printf '%s' "$output" | jq -e . >/dev/null || fail "output is not valid JSON: $output"
}
