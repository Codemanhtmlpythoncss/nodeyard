#!/usr/bin/env bats
# Public access and the dashboard password rules (demo mode: nothing really runs).

setup() {
    load ../helpers/common
    ny_cmd_setup
}

@test "a short dashboard password is refused unless dashboard.weak-password is on" {
    run bash -c "printf 'abc' | '${NY_REPO_ROOT}/bin/nodeyard' --demo dashboard password --stdin --no-restart"
    assert_failure
    assert_output --partial "too short"
    assert_output --partial "dashboard.weak-password"
    run ny_cmd_run config set dashboard.weak-password true
    assert_success
    run bash -c "printf 'abc' | '${NY_REPO_ROOT}/bin/nodeyard' --demo dashboard password --stdin --no-restart"
    assert_success
}

@test "an empty dashboard password is always refused" {
    run ny_cmd_run config set dashboard.weak-password true
    run bash -c "printf '' | '${NY_REPO_ROOT}/bin/nodeyard' --demo dashboard password --stdin --no-restart"
    assert_failure
}

@test "public commands are registered with help" {
    run ny_cmd_run public on --help
    assert_success
    assert_output --partial "Tailscale Funnel"
    assert_output --partial "dashboard.weak-password" || assert_output --partial "strong"
}
