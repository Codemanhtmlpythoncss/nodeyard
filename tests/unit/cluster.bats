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
