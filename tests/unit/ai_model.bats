#!/usr/bin/env bats
# ai model rm (Ollama): unload before deleting, and report every node's result
# honestly. kubectl is faked per test; nothing touches a cluster.

setup() {
    load ../helpers/common
    ny_lib_setup
    ny_is_root() { return 0; }
    ny_need_root() { return 0; }
    CALLS="${BATS_TEST_TMPDIR}/calls"
    : >"$CALLS"
    ai_pods() { printf 'ollama-a\tnode-a\nollama-b\tnode-b\n'; }
}

@test "model rm unloads then deletes on every Ollama node" {
    ny_run() { printf '%s\n' "$*" >>"$CALLS"; }
    NY_YES=1 run ai_model_rm_cmd "qwen2.5:7b"
    assert_success
    assert_output --partial "Deleted qwen2.5:7b from 2 node(s)."
    run grep -c 'ollama stop "$1" >/dev/null 2>&1; ollama rm "$1" sh qwen2.5:7b' "$CALLS"
    assert_output "2"
}

@test "model rm fails when a node couldn't delete it" {
    ny_run() { if [[ "$*" == *ollama-b* ]]; then echo "Error: permission denied"; return 1; fi; }
    NY_YES=1 run ai_model_rm_cmd "qwen2.5:7b"
    assert_failure
    assert_output --partial "Delete failed on node-b: Error: permission denied"
    assert_output --partial "couldn't be deleted on 1 node(s)"
}

@test "model rm treats 'not found' as already gone, not as a failure" {
    ny_run() { if [[ "$*" == *ollama-b* ]]; then echo "Error: model 'qwen2.5:7b' not found"; return 1; fi; }
    NY_YES=1 run ai_model_rm_cmd "qwen2.5:7b"
    assert_success
    assert_output --partial "node-b           not there"
    assert_output --partial "Deleted qwen2.5:7b from 1 node(s)."
}

@test "model rm only touches the chosen node with --node" {
    ny_run() { printf '%s\n' "$*" >>"$CALLS"; }
    NY_YES=1 run ai_model_rm_cmd "qwen2.5:7b" --node node-b
    assert_success
    run grep -c "ollama-a" "$CALLS"
    assert_output "0"
    grep -q "ollama-b" "$CALLS"
}
