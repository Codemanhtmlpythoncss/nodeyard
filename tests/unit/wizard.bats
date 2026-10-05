#!/usr/bin/env bats
# Wizard specs and the wizard engine.

setup() {
    load ../helpers/common
    ny_lib_setup
}

@test "every wizard spec is valid and runs an existing command" {
    local f
    for f in "${NY_REPO_ROOT}"/share/nodeyard/wizards/*.json; do
        jq -e '.id and .title and (.command | length > 0) and (.steps | length > 0)' "$f" >/dev/null || fail "bad spec $f"
        local -a cmd=()
        mapfile -t cmd < <(jq -r '.command[]' "$f")
        ny_cmd_resolve "${cmd[@]}" || fail "$(basename "$f"): command '${cmd[*]}' is not registered"
        [[ "$(jq -r '.id' "$f").json" == "$(basename "$f")" ]] || fail "id/filename mismatch in $f"
    done
}

@test "every wizard flag is accepted by its command" {
    local f flag help
    for f in "${NY_REPO_ROOT}"/share/nodeyard/wizards/*.json; do
        local -a cmd=()
        mapfile -t cmd < <(jq -r '.command[]' "$f")
        ny_cmd_resolve "${cmd[@]}"
        help="$(ny_help_command "$NY_RESOLVED_PATH")"
        while IFS= read -r flag; do
            [[ "$help" == *"$flag"* ]] || fail "$(basename "$f"): $flag not in '${cmd[*]} --help'"
        done < <(jq -r '.steps[].flag // empty' "$f")
    done
}

@test "answers become flags; conditions hide steps" {
    local spec
    spec="$(jq -c . "${NY_REPO_ROOT}/share/nodeyard/wizards/install-master.json")"
    declare -gA NY_WIZ_ANS=([ha]=yes [worker]=no [interface]=eth0 [channel]="" [version]=v1.33.4+k3s1)
    run ny_wizard_build_args "$spec"
    assert_output $'install\nmaster\n--ha\n--interface\neth0\n--version\nv1.33.4+k3s1'
    NY_WIZ_ANS=([ha]=no [worker]=yes [interface]="" [channel]=stable [version]=ignored)
    run ny_wizard_build_args "$spec"
    assert_output $'install\nmaster\n--worker\n--channel\nstable'
}

@test "secrets are passed on stdin, never as arguments" {
    local spec
    spec="$(jq -c . "${NY_REPO_ROOT}/share/nodeyard/wizards/install-worker.json")"
    declare -gA NY_WIZ_ANS=([server]=https://192.168.1.9:6443 [token]=supersecrettoken123 [interface]="" [version]="")
    run ny_wizard_build_args "$spec"
    refute_output --partial supersecrettoken123
    assert_output --partial --token-stdin
    run ny_wizard_stdin "$spec"
    assert_output supersecrettoken123
}

@test "positional steps come right after the command" {
    local spec
    spec="$(jq -c . "${NY_REPO_ROOT}/share/nodeyard/wizards/add-node.json")"
    declare -gA NY_WIZ_ANS=([kind]=worker [ssh]=pi@10.0.0.2 [port]=22 [interface]="")
    run ny_wizard_build_args "$spec"
    assert_output $'add-node\nworker\n--ssh\npi@10.0.0.2\n--port\n22'
}
