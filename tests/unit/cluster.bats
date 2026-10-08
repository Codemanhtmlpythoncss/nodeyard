#!/usr/bin/env bats
# add-node's remote steps, with ssh and scp simulated.

setup() {
    load ../helpers/common
    ny_lib_setup --demo-fs
    # Run as if root; nothing here touches the real system.
    ny_is_root() { return 0; }
    NY_YES=1
    FP="SHA256:pJ8bDemoFingerprintN0tRealXk2qWv7mE4sLhA"
    ny_rule "ssh * -fN *" 0
    ny_rule "ssh *mktemp -d /tmp/nodeyard.XXXXXXXX*" 0 "/tmp/nodeyard.Test1234"
    ny_rule "ssh * -O exit *" 0
    ny_rule "ssh * -t *" 0
    ny_rule "scp *" 0
}

remote_cmd() { grep -E '^ssh .* -t ' "$NODEYARD_SHIM_LOG" | tail -n1; }

@test "the remote script runs as root in its own directory and cleans up" {
    run cluster_remote_script install worker --server https://10.0.0.1:6443 --token-file TOKENFILE --yes
    assert_output --partial "trap 'rm -rf \"\$work\"' EXIT"
    assert_output --partial '--token-file "${work}/k3s-token"'
    assert_output --partial 'install.sh" --from-dir "$work" --yes --force'
    refute_output --partial TOKENFILE
    cluster_remote_script install worker --token-file TOKENFILE >"${BATS_TEST_TMPDIR}/run.sh"
    bash -n "${BATS_TEST_TMPDIR}/run.sh"
}

@test "become: root needs nothing, sudo when allowed, su otherwise" {
    [ "$(cluster_become_method auto 0 no-sudo)" = none ]
    [ "$(cluster_become_method auto 1000 sudo)" = sudo ]
    [ "$(cluster_become_method auto 1000 sudo-nopass)" = sudo ]
    [ "$(cluster_become_method auto 1000 sudo-not-allowed)" = su ]
    [ "$(cluster_become_method auto 1000 no-sudo)" = su ]
    [ "$(cluster_become_method su 1000 sudo)" = su ]
    [ "$(cluster_become_cmd su /tmp/nodeyard.x/run.sh)" = "su - root -c \"bash '/tmp/nodeyard.x/run.sh'\"" ]
    [ "$(cluster_become_cmd sudo /tmp/nodeyard.x/run.sh)" = "sudo bash '/tmp/nodeyard.x/run.sh'" ]
}

@test "a machine without sudo is set up with su, and temp files are removed as the user" {
    ny_rule "ssh *id -u*" 0 '1000\nno-sudo\nlo\neth0'
    run cluster_add_node_cmd worker --ssh Dead_channel@10.50.0.2 --host-key "$FP"
    assert_success
    run remote_cmd
    assert_output --partial "su - root -c \"bash '/tmp/nodeyard.Test1234/run.sh'\""
    assert_output --partial "rm -rf '/tmp/nodeyard.Test1234'"
}

@test "a sudo user is set up with sudo" {
    ny_rule "ssh *id -u*" 0 '1000\nsudo\nlo\neth0'
    run cluster_add_node_cmd worker --ssh Dead_channel@10.50.0.2 --host-key "$FP"
    assert_success
    run remote_cmd
    assert_output --partial "sudo bash '/tmp/nodeyard.Test1234/run.sh'"
}

@test "--become su is honoured even when sudo would work" {
    ny_rule "ssh *id -u*" 0 '1000\nsudo-nopass\nlo\neth0'
    run cluster_add_node_cmd worker --ssh Dead_channel@10.50.0.2 --host-key "$FP" --become su
    assert_success
    run remote_cmd
    assert_output --partial "su - root -c"
}

@test "an interface that doesn't exist there is caught before installing" {
    ny_rule "ssh *id -u*" 0 '1000\nsudo\nlo\nenp1s0\nwlp2s0'
    run cluster_add_node_cmd worker --ssh Dead_channel@10.50.0.2 --host-key "$FP" --interface eth0
    assert_failure 2
    assert_output --partial "no network interface called 'eth0'"
    assert_output --partial "enp1s0 wlp2s0"
    run grep -c '^scp ' "$NODEYARD_SHIM_LOG"
    assert_output 0
}

@test "a failed su says so plainly" {
    ny_rule "ssh *id -u*" 0 '1000\nno-sudo\neth0'
    ny_rule_first "ssh * -t *" 1
    run cluster_add_node_cmd worker --ssh Dead_channel@10.50.0.2 --host-key "$FP"
    assert_failure
    assert_output --partial "su on 10.50.0.2 failed"
}

# --- worker-info ------------------------------------------------------------

demo_cmd() { # run nodeyard in a demo sandbox (own setup: this file's setup is lib-based)
    export NODEYARD_DEMO_DIR="${BATS_TEST_TMPDIR}/demo"
    export NODEYARD_SHIM_RULES_EXTRA="${BATS_TEST_TMPDIR}/rules"
    "${NY_REPO_ROOT}/bin/nodeyard" --demo "$@"
}

@test "worker-info shows the join address, port, ports table and both ways to add a worker" {
    run demo_cmd worker-info
    assert_success
    assert_output --partial "https://192.168.1.10:6443"
    assert_output --partial "port 6443"
    assert_output --partial "6443/tcp"
    assert_output --partial "10250/tcp"
    assert_output --partial "8472/udp"
    assert_output --partial "v1.33.4+k3s1"
    assert_output --partial "nodeyard add-node worker --ssh"
    assert_output --partial "nodeyard install worker --server https://192.168.1.10:6443 --token-file /root/k3s-token"
}

@test "worker-info never prints the token" {
    run demo_cmd worker-info
    refute_output --partial "K10demo"
    run demo_cmd worker-info --json
    refute_output --partial "K10demo"
}

@test "worker-info --json is complete" {
    run --separate-stderr demo_cmd worker-info --json
    assert_success
    printf '%s' "$output" | jq -e '.server == "https://192.168.1.10:6443" and .port == 6443 and .token_hidden == true
        and (.ports | map(.port) | index("6443/tcp")) != null and (.commands.join | contains("install worker"))
        and (.other_addresses | length) >= 1' >/dev/null
}

@test "worker-info reports ports closed on a ufw firewall" {
    ny_rule "ufw status*" 0 'Status: active\n10250/tcp    ALLOW    Anywhere'
    run demo_cmd worker-info
    assert_success
    assert_output --partial "closed"
    assert_output --partial "nodeyard firewall open"
    run --separate-stderr demo_cmd worker-info --json
    printf '%s' "$output" | jq -e '(.ports[] | select(.port == "6443/tcp") | .open_on_this_server) == "closed"
        and (.ports[] | select(.port == "10250/tcp") | .open_on_this_server) == "open"' >/dev/null
}

@test "worker-info says so on a machine that is not a server" {
    export NODEYARD_DEMO_DIR="${BATS_TEST_TMPDIR}/demo"
    "${NY_REPO_ROOT}/bin/nodeyard" --demo version >/dev/null 2>&1
    rm -f "${NODEYARD_DEMO_DIR}/fs/var/lib/rancher/k3s/server/node-token"
    run demo_cmd worker-info
    assert_failure 3
    assert_output --partial "not a k3s server"
}

@test "the menu offers it on a server and shows it without revealing the token" {
    export NODEYARD_DEMO_DIR="${BATS_TEST_TMPDIR}/demo"
    run bash -c 'printf "workerinfo\nn\n\nq\n" | NODEYARD_INTERACTIVE=1 NODEYARD_UI=plain NODEYARD_COLOR=never "$0" --demo menu' "${NY_REPO_ROOT}/bin/nodeyard"
    assert_success
    assert_output --partial "What a worker needs to join"
    assert_output --partial "Adding a worker to this cluster"
    refute_output --partial "K10demo"
}

@test "the menu can reveal the token on request" {
    export NODEYARD_DEMO_DIR="${BATS_TEST_TMPDIR}/demo"
    run bash -c 'printf "workerinfo\ny\n\nq\n" | NODEYARD_INTERACTIVE=1 NODEYARD_UI=plain NODEYARD_COLOR=never "$0" --demo menu' "${NY_REPO_ROOT}/bin/nodeyard"
    assert_success
    assert_output --partial "K10demo"
}

# --- SSH host keys ------------------------------------------------------------

NEW_FP="SHA256:pJ8bDemoFingerprintN0tRealXk2qWv7mE4sLhA"
OLD_FP="SHA256:OLDoldOLDoldOLDoldOLDoldOLDoldOLDoldOLDold"

@test "a new machine's key can't be trusted without a terminal; the message names the fix" {
    run ny_ssh_trust_host 10.50.0.9 22
    assert_failure 10
    assert_output --partial "First connection to 10.50.0.9"
    assert_output --partial "--host-key ${NEW_FP}"
}

@test "with a terminal, a new machine's key is confirmed with Enter (default yes)" {
    export NODEYARD_INTERACTIVE=1
    NY_UI=plain
    run ny_ssh_trust_host 10.50.0.9 22 <<<""
    assert_success
    assert_output --partial "Trusted the SSH host key of 10.50.0.9"
}

@test "--yes does not skip the key confirmation when there is no terminal" {
    NY_YES=1
    run ny_ssh_trust_host 10.50.0.9 22
    assert_failure 10
}

@test "a saved key that still matches is accepted silently" {
    ny_rule "ssh-keygen -l -F *" 0 "# Host 10.50.0.9 found: line 1\n10.50.0.9 ED25519 ${NEW_FP}"
    run ny_ssh_trust_host 10.50.0.9 22
    assert_success
    assert_output ""
}

@test "a changed key is explained, with old and new fingerprints, and refused without a terminal" {
    ny_rule "ssh-keygen -l -F *" 0 "# Host 10.50.0.4 found: line 7\n10.50.0.4 ED25519 ${OLD_FP}"
    run ny_ssh_trust_host 10.50.0.4 22
    assert_failure 10
    assert_output --partial "has CHANGED"
    assert_output --partial "$OLD_FP"
    assert_output --partial "$NEW_FP"
    assert_output --partial "reinstalled"
}

@test "an out-of-date key in the user's own known_hosts is ignored, not copied" {
    mkdir -p "${NODEYARD_ROOT}/home"
    export HOME="${NODEYARD_ROOT}/home"
    unset SUDO_USER
    mkdir -p "${HOME}/.ssh"
    : >"${HOME}/.ssh/known_hosts"
    # The rule answers for any file: nodeyard's own has nothing, the user's has an old key.
    ny_rule "ssh-keygen -l -F 10.50.0.4 -f ${HOME}/.ssh/known_hosts" 0 "# Host 10.50.0.4 found: line 5\n10.50.0.4 ED25519 ${OLD_FP}"
    run ny_ssh_trust_host 10.50.0.4 22
    assert_failure 10
    assert_output --partial "has CHANGED"
    run grep -c . "$(ny_ssh_known_hosts)"
    assert_output 0
}

@test "--host-key that matches the machine's current key replaces a stale one without asking" {
    ny_rule "ssh-keygen -l -F *" 0 "# Host 10.50.0.4 found: line 7\n10.50.0.4 ED25519 ${OLD_FP}"
    run ny_ssh_trust_host 10.50.0.4 22 "$NEW_FP"
    assert_success
    assert_output --partial "Trusted the SSH host key"
}

@test "--host-key that does not match is refused" {
    run ny_ssh_trust_host 10.50.0.4 22 "SHA256:somethingElseEntirely"
    assert_failure
    assert_output --partial "does not match the fingerprint you gave"
}

@test "dry-run never blocks on the key and says it would ask" {
    NY_DRY_RUN=1
    run ny_ssh_trust_host 10.50.0.9 22
    assert_success
    assert_output --partial "would ask you to confirm this host key"
}

@test "login failures get a plain next step" {
    local f="${BATS_TEST_TMPDIR}/err"
    printf 'Dead_channel@10.50.0.4: Permission denied (publickey,password).\n' >"$f"
    run ny_ssh_failure_fix "$f" 10.50.0.4 22 Dead_channel@10.50.0.4
    assert_output --partial "user name or password was refused"
    printf 'ssh: connect to host 10.50.0.4 port 22: Connection refused\n' >"$f"
    run ny_ssh_failure_fix "$f" 10.50.0.4 22 Dead_channel@10.50.0.4
    assert_output --partial "Is SSH running"
    printf 'Host key verification failed.\n' >"$f"
    run ny_ssh_failure_fix "$f" 10.50.0.4 22 Dead_channel@10.50.0.4
    assert_output --partial "reinstall"
}

# --- remove-node -----------------------------------------------------------------

@test "remove-node drains a Ready node, deletes it, and clears its stored password" {
    run --separate-stderr demo_cmd remove-node yard-4 --yes --dry-run --json
    assert_success
    printf '%s' "$output" | jq -e '[.plan[].command | join(" ")] | (map(test("drain yard-4")) | any) and (map(test("delete node yard-4")) | any)
        and (map(test("delete secret -n kube-system yard-4.node-password.k3s")) | any)' >/dev/null
}

@test "remove-node skips the drain for a node that is not Ready" {
    ny_rule "k3s kubectl get node yard-3 --no-headers*" 0 "yard-3   NotReady   control-plane   40d   v1.33.4+k3s1"
    run --separate-stderr demo_cmd remove-node yard-3 --yes --dry-run --json
    assert_success
    printf '%s' "$output" | jq -e '[.plan[].command | join(" ")] | (map(test("drain")) | any | not)
        and (map(test("delete secret -n kube-system yard-3.node-password.k3s")) | any)' >/dev/null
}

# --- restart-cluster -------------------------------------------------------------

cluster_node_names_rule() {
    ny_rule_first "k3s kubectl get nodes -o name*" 0 'node/yard-1\nnode/yard-2\nnode/yard-3\nnode/yard-4'
}

@test "restart-cluster plans every worker first, one at a time, and this server last" {
    cluster_node_names_rule
    run --separate-stderr demo_cmd restart-cluster --yes --dry-run --json
    assert_success
    printf '%s' "$output" | jq -e '[.plan[] | tostring] | (map(test("Restart k3s on yard-")) | any) and (map(test("this server")) | any)
        and (map(test("this server")) | index(true)) > (map(test("Restart k3s on yard-")) | index(true))' >/dev/null
}

@test "restart-cluster --workers-only leaves this server alone" {
    cluster_node_names_rule
    run --separate-stderr demo_cmd restart-cluster --yes --dry-run --json --workers-only
    assert_success
    printf '%s' "$output" | jq -e '[.plan[] | tostring] | (map(test("this server")) | any | not)' >/dev/null
}

@test "restart-cluster rejects unknown options and has help" {
    run --separate-stderr demo_cmd restart-cluster --bogus
    assert_failure
    run demo_cmd restart-cluster --help
    assert_success
    assert_output --partial "restarted first"
}

@test "reboot-cluster plans full worker reboots before the control server" {
    cluster_node_names_rule
    run --separate-stderr demo_cmd reboot-cluster --yes --dry-run --json
    assert_success
    printf '%s' "$output" | jq -e '[.plan[] | tostring] as $p |
        ($p | map(test("whole machine")) | any) and
        ($p | map(test("boot ID")) | any) and
        ($p | map(test("Drain yard-")) | index(true)) < ($p | map(test("control server")) | index(true))' >/dev/null
}

@test "reboot-cluster refuses to start if this server is not in the Kubernetes node list" {
    ny_rule_first "k3s kubectl get nodes -o name*" 0 'node/worker-1\nnode/worker-2'
    run --separate-stderr demo_cmd reboot-cluster --yes --dry-run --json
    assert_failure
    assert_output --partial "isn't listed as a node"
    assert_output --partial "No machines were rebooted"
}

@test "reboot-cluster rejects unsafe timeout values and documents that it reboots the OS" {
    run --separate-stderr demo_cmd reboot-cluster --timeout 10 --yes --dry-run
    assert_failure
    run demo_cmd reboot-cluster --help
    assert_success
    assert_output --partial "Fully reboots every machine"
    assert_output --partial "workloads are interrupted"
}

# --- menu header -------------------------------------------------------------------

@test "the menu header names the interface that holds the address, not the default route's" {
    # demo: address 192.168.1.10 is on eth0; pretend the default route uses wlan0
    ny_rule "ip -4 route get 1.1.1.1" 0 "1.1.1.1 via 192.168.1.1 dev wlan0 src 192.168.1.53 uid 0"
    export NODEYARD_DEMO_DIR="${BATS_TEST_TMPDIR}/demo"
    run bash -c 'printf "q\n" | NODEYARD_INTERACTIVE=1 NODEYARD_UI=plain NODEYARD_COLOR=never NODEYARD_SHIM_RULES_EXTRA="$1" "$0" --demo menu' "${NY_REPO_ROOT}/bin/nodeyard" "${BATS_TEST_TMPDIR}/rules"
    assert_output --partial "192.168.1.10 (eth0)"
    refute_output --partial "192.168.1.10 (wlan0)"
}
