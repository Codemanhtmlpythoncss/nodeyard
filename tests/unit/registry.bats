#!/usr/bin/env bats
# Command registry, dispatcher, help and completion.

setup() {
    load ../helpers/common
    ny_cmd_setup
}

@test "nodeyard --version and version --json" {
    run ny_cmd_run --version
    assert_output --regexp '^nodeyard [0-9]+\.[0-9]+\.[0-9]+'
    run ny_cmd_run version --json
    assert_json
}

@test "help lists commands in groups" {
    run ny_cmd_run help
    assert_success
    assert_output --partial "Cluster"
    assert_output --partial "install master"
    assert_output --partial "--dry-run"
}

@test "every command has help" {
    run ny_cmd_run install master --help
    assert_output --partial "Usage: nodeyard install master"
    run ny_cmd_run doctor -h
    assert_output --partial "Usage: nodeyard doctor"
}

@test "a group name lists its commands" {
    run ny_cmd_run ai
    assert_success
    assert_output --partial "ai deploy"
    assert_output --partial "ai split plan"
}

@test "aliases from k3s-manager still work" {
    run ny_cmd_run get-nodes
    assert_success
    assert_output --partial yard-1
    run ny_cmd_run join-info --json
    assert_json
}

@test "unknown commands fail with a suggestion and exit code 2" {
    run ny_cmd_run instal
    assert_failure 2
    assert_output --partial "Unknown command: instal"
    assert_output --partial "install master"
}

@test "bad options are usage errors" {
    run ny_cmd_run doctor --frobnicate
    assert_failure 2
    assert_output --partial "Unknown option"
}

@test "--json errors are machine-readable on stdout" {
    run --separate-stderr ny_cmd_run kubeconfig --ip not_an_ip --json
    assert_failure 2
    printf '%s' "$output" | jq -e '.ok == false and (.error.message | length) > 0' >/dev/null
}

@test "commands without their own JSON output still give a result object" {
    run --separate-stderr ny_cmd_run firewall status --json
    assert_success
    printf '%s' "$output" | jq -e '.ok == true and (.plan | type) == "array"' >/dev/null
}

@test "completion suggests commands, subcommands and flags" {
    run "${NY_REPO_ROOT}/bin/nodeyard" __complete -- ins
    assert_output install
    run "${NY_REPO_ROOT}/bin/nodeyard" __complete install -- ""
    assert_output --partial master
    assert_output --partial worker
    run "${NY_REPO_ROOT}/bin/nodeyard" __complete install master -- --int
    assert_output --interface
}

@test "completion scripts are printed" {
    run ny_cmd_run completion bash
    assert_output --partial "complete -F"
    run ny_cmd_run completion zsh
    assert_output --partial "#compdef"
}
