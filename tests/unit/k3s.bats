#!/usr/bin/env bats
# k3s install/join/upgrade commands (demo mode: nothing really runs).

setup() {
    load ../helpers/common
    ny_cmd_setup
}

# Demo yard-1 is a server; make it a blank machine for install tests.
blank_machine() {
    rm -f "${DEMO_ROOT}/etc/systemd/system/k3s.service" "${DEMO_ROOT}/usr/local/bin/k3s"
    rm -rf "${DEMO_ROOT}/etc/nodeyard/cluster.conf" "${DEMO_ROOT}/var/lib/rancher"
}

plan() { # the dry-run plan's commands, one per line
    printf '%s' "$output" | jq -r '.plan[] | .command // empty | join(" ")'
}

@test "install master builds the k3s command line from the flags" {
    blank_machine
    run --separate-stderr ny_cmd_run install master --ha --worker --interface eth0 --version v1.33.4+k3s1 \
        --disable traefik --node-label nodeyard.io/group=gpu --dry-run --json
    assert_success
    assert_json
    run plan
    assert_output --partial "INSTALL_K3S_EXEC=server --cluster-init --node-ip 192.168.1.10 --flannel-iface eth0 --advertise-address 192.168.1.10"
    assert_output --partial "--disable traefik"
    assert_output --partial "--node-label nodeyard.io/group=gpu"
    assert_output --partial "INSTALL_K3S_VERSION=v1.33.4+k3s1"
}

@test "a token given to install master goes to a root-only file, never the command line" {
    blank_machine
    run --separate-stderr ny_cmd_run install master --token Sup3rS3cretT0ken --dry-run --json
    assert_success
    refute_output --partial Sup3rS3cretT0ken
    [[ "$stderr" != *Sup3rS3cretT0ken* ]]
    assert_output --partial "--token-file /etc/nodeyard/secrets/k3s-token"
    printf '%s' "$output" | jq -e '.plan[] | select(.path == "/etc/nodeyard/secrets/k3s-token") | .secret == true' >/dev/null
    run grep -r Sup3rS3cretT0ken "${DEMO_ROOT}/var/log"
    assert_failure
}

@test "an external datastore DSN goes through the environment and is redacted" {
    blank_machine
    run --separate-stderr ny_cmd_run install master --datastore-endpoint "postgres://k3s:dbpassw0rd@db:5432/k3s" --dry-run --json
    assert_success
    refute_output --partial dbpassw0rd
    run plan
    refute_output --partial "--datastore-endpoint"
}

@test "install worker reads the token from stdin and uses K3S_TOKEN_FILE" {
    blank_machine
    run --separate-stderr bash -c 'printf "W0rkerJ0inT0ken\n" | "$0" --demo install worker --server https://192.168.1.9:6443 --token-stdin --dry-run --json' "${NY_REPO_ROOT}/bin/nodeyard"
    assert_success
    refute_output --partial W0rkerJ0inT0ken
    run plan
    assert_output --partial "K3S_URL=https://192.168.1.9:6443"
    assert_output --partial "K3S_TOKEN_FILE=/etc/nodeyard/secrets/k3s-token"
    assert_output --partial "INSTALL_K3S_EXEC=agent --node-ip 192.168.1.10 --flannel-iface eth0"
    refute_output --partial "--advertise-address"
}

@test "joining needs a server and a token, explained" {
    blank_machine
    run ny_cmd_run install worker --token-file /x
    assert_failure 2
    assert_output --partial "--server is required"
    run ny_cmd_run install worker --server https://10.0.0.1:6443
    assert_failure 2
    assert_output --partial "join token is required"
}

@test "a server can't be turned into a worker in place" {
    run ny_cmd_run install worker --server https://192.168.1.9:6443 --token-file /x --yes
    assert_failure 3
    assert_output --partial "already a k3s server"
}

@test "installing needs confirmation when not interactive" {
    blank_machine
    run ny_cmd_run install master
    assert_failure 10
    assert_output --partial "--yes"
}

@test "upgrade keeps the node's current settings" {
    run --separate-stderr ny_cmd_run upgrade --version v1.34.1+k3s1 --dry-run --json
    assert_success
    run plan
    assert_output --partial "INSTALL_K3S_EXEC=server --cluster-init --node-ip 192.168.1.10 --flannel-iface eth0 --advertise-address 192.168.1.10"
    assert_output --partial "INSTALL_K3S_VERSION=v1.34.1+k3s1"
}

@test "token hides the token unless --reveal" {
    run --separate-stderr ny_cmd_run token --json
    printf '%s' "$output" | jq -e '.token == null and (.servers | length) > 0' >/dev/null
    run --separate-stderr ny_cmd_run token --reveal --json
    printf '%s' "$output" | jq -e '.token | startswith("K10")' >/dev/null
    run ny_cmd_run token
    refute_output --partial "K10demo"
}

@test "status reports the cluster as JSON" {
    run --separate-stderr ny_cmd_run status --json
    assert_success
    printf '%s' "$output" | jq -e '.role == "server" and .nodes_total == 4 and .nodes_ready == 4' >/dev/null
}

@test "uninstall k3s asks first and keeps the config" {
    run ny_cmd_run uninstall k3s
    assert_failure 10
    run --separate-stderr ny_cmd_run uninstall k3s --yes --json
    assert_success
    [ -f "${DEMO_ROOT}/etc/nodeyard/cluster.conf" ]
}

@test "kubeconfig points at the requested address" {
    run ny_cmd_run kubeconfig --stdout --ip 192.168.1.9
    assert_output --partial "server: https://192.168.1.9:6443"
}
