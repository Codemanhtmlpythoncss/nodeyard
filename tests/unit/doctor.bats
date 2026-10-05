#!/usr/bin/env bats
# doctor checks and fixes.

setup() {
    load ../helpers/common
    ny_cmd_setup
}

@test "a healthy demo node has no problems" {
    run --separate-stderr ny_cmd_run doctor --json
    assert_success
    printf '%s' "$output" | jq -e '.issues == 0' >/dev/null
}

@test "a missing kernel module is reported with a fix" {
    ny_rule "lsmod" 0 "Module Size Used by\noverlay 1 0"
    run --separate-stderr ny_cmd_run doctor --json
    printf '%s' "$output" | jq -e '.checks[] | select(.id == "br-netfilter") | .status == "issue" and (.fix | length) > 0' >/dev/null
}

@test "--fix applies the fix and it can be undone" {
    ny_rule "lsmod" 0 "Module Size Used by\noverlay 1 0"
    run ny_cmd_run doctor --fix --only br-netfilter
    assert_success
    [ -f "${DEMO_ROOT}/etc/modules-load.d/nodeyard.conf" ]
    run ny_cmd_run undo --last --yes
    assert_success
    [ ! -f "${DEMO_ROOT}/etc/modules-load.d/nodeyard.conf" ]
}

@test "--strict exits non-zero when problems remain" {
    ny_rule "systemctl is-active --quiet k3s" 3
    run ny_cmd_run doctor --strict --only k3s-active
    assert_failure 1
}

@test "an SD-card server gets a warning" {
    ny_rule "findmnt -no SOURCE /" 0 /dev/mmcblk0p2
    ny_rule "lsblk -no PKNAME /dev/mmcblk0p2" 0 mmcblk0
    run --separate-stderr ny_cmd_run doctor --json --only boot-disk
    printf '%s' "$output" | jq -e '.checks[0].status == "warn" and (.checks[0].detail | test("SD card"))' >/dev/null
}
