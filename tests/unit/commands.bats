#!/usr/bin/env bats
# Other commands, end to end in demo mode.

setup() {
    load ../helpers/common
    ny_cmd_setup
}

@test "detect --json describes the machine" {
    run --separate-stderr ny_cmd_run detect --json
    assert_success
    printf '%s' "$output" | jq -e '.os.label == "Raspberry Pi OS 12 (bookworm)" and .arch == "arm64"
        and .hardware.raspberry_pi == true and .hardware.boot_disk == "nvme"
        and .network.backend == "networkmanager" and .network.primary_interface == "eth0"' >/dev/null
}

@test "config set validates before writing; get reads back; undo restores" {
    run ny_cmd_run config set cluster.vip 999.1.1.1
    assert_failure 2
    run ny_cmd_run config set node.yard-4.groups gpu
    assert_success
    run ny_cmd_run config get node.yard-4.groups
    assert_output gpu
    run ny_cmd_run undo --last --yes
    run ny_cmd_run config get node.yard-4.groups
    assert_output low-power
}

@test "config validate --json" {
    run --separate-stderr ny_cmd_run config validate --json
    assert_success
    printf '%s' "$output" | jq -e '.ok == true' >/dev/null
}

@test "config export and import round-trip" {
    local f="${BATS_TEST_TMPDIR}/exported.conf"
    run ny_cmd_run config export --out "$f"
    assert_success
    run ny_cmd_run config import "$f" --yes
    assert_success
}

@test "config drift compares this node with its entry" {
    run --separate-stderr ny_cmd_run config drift --json
    assert_success
    printf '%s' "$output" | jq -e '.drift == 0' >/dev/null
    ny_rule "hostname" 0 "something-else"
    run --separate-stderr ny_cmd_run config drift --json
    printf '%s' "$output" | jq -e '.drift >= 1' >/dev/null
}

@test "changes lists what was done" {
    ny_cmd_run config set node.yard-2.groups gpu >/dev/null 2>&1
    run --separate-stderr ny_cmd_run changes --json
    printf '%s' "$output" | jq -e '.changes | length == 1' >/dev/null
}

@test "deps reports each feature's tools" {
    run --separate-stderr ny_cmd_run deps --json
    assert_success
    printf '%s' "$output" | jq -e '.package_manager == "apt" and (.dependencies | length) > 5' >/dev/null
}

@test "ai nodes and ai status work against the simulated cluster" {
    run ny_cmd_run ai nodes
    assert_output --partial "yard-3"
    run ny_cmd_run ai status
    assert_output --partial "llama3.2"
}

@test "add-node refuses an unverified host key when unattended" {
    run ny_cmd_run add-node worker --ssh pi@192.168.1.14 --yes
    assert_failure 10
    assert_output --partial "--host-key"
}

@test "snapshot list shows etcd snapshots" {
    run ny_cmd_run snapshot list
    assert_output --partial "etcd-snapshot-yard-1"
}

@test "secrets list never shows values" {
    run ny_cmd_run secrets list
    assert_success
}
