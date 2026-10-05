#!/usr/bin/env bats
# The cluster config file: parsing, editing and validation.

setup() {
    load ../helpers/common
    ny_lib_setup
    CONF="${BATS_TEST_TMPDIR}/cluster.conf"
    cat >"$CONF" <<'CONF'
# Example cluster
[cluster]
    name = homelab        # inline comment
    domain = "home.arpa"
[network]
subnet = 192.168.1.0/24
dhcp-range = 192.168.1.100-192.168.1.250

; nodes
[node "pi-1"]
    address = 192.168.1.10/24
    role = server
    init = true
    groups = storage, low-power
    groups = gpu
[Node "pi-2"]
    ADDRESS = 192.168.1.11/24
    role = worker
    note = "keeps # and ; inside quotes"
CONF
}

@test "values, comments, quotes and case-insensitive names" {
    ny_cfg_parse "$CONF"
    [ "$(ny_cfg_get cluster "" name)" = homelab ]
    [ "$(ny_cfg_get cluster "" domain)" = home.arpa ]
    [ "$(ny_cfg_get node pi-2 address)" = 192.168.1.11/24 ]
    [ "$(ny_cfg_get node pi-2 note)" = "keeps # and ; inside quotes" ]
    [ "$(ny_cfg_get node pi-9 address fallback)" = fallback ]
}

@test "lists come from commas and repeated keys" {
    ny_cfg_parse "$CONF"
    run ny_cfg_get_list node pi-1 groups
    assert_output $'storage\nlow-power\ngpu'
}

@test "subsections are listed in file order" {
    ny_cfg_parse "$CONF"
    run ny_cfg_subs node
    assert_output $'pi-1\npi-2'
}

@test "syntax errors report the line and a fix" {
    printf '[cluster]\nname homelab\nkey = "unterminated\nstray = value\n' >"$CONF"
    printf 'orphan = 1\n' | cat - "$CONF" >"${CONF}.2"
    run ny_cfg_parse "${CONF}.2"
    assert_failure
    [[ "${NY_CFG_ERRORS[*]}" == "" ]] # run used a subshell; re-run here
    ny_cfg_parse "${CONF}.2" || true
    [[ "${NY_CFG_ERRORS[0]}" == "line 1: 'orphan' is outside any [section]"* ]]
    [[ "${NY_CFG_ERRORS[1]}" == "line 3: cannot understand"* ]]
    [[ "${NY_CFG_ERRORS[2]}" == "line 4: unbalanced quotes"* ]]
}

@test "set replaces in place and keeps comments and layout" {
    ny_cfg_set node pi-1 role agent "$CONF"
    run grep -c '^# Example cluster' "$CONF"
    assert_output 1
    run grep -n 'role = agent' "$CONF"
    assert_output --partial "12:"
    ny_cfg_parse "$CONF"
    [ "$(ny_cfg_get node pi-1 role)" = agent ]
}

@test "set on a repeated key leaves one value; add appends; unset removes" {
    ny_cfg_set node pi-1 groups solo "$CONF"
    ny_cfg_parse "$CONF"
    run ny_cfg_get_list node pi-1 groups
    assert_output solo
    ny_cfg_add node pi-1 groups extra "$CONF"
    ny_cfg_parse "$CONF"
    run ny_cfg_get_list node pi-1 groups
    assert_output $'solo\nextra'
    ny_cfg_unset node pi-1 groups "$CONF"
    ny_cfg_parse "$CONF"
    ! ny_cfg_has node pi-1 groups
}

@test "set creates missing sections; drop removes them" {
    ny_cfg_set node pi-3 address 192.168.1.12/24 "$CONF"
    ny_cfg_parse "$CONF"
    [ "$(ny_cfg_get node pi-3 address)" = 192.168.1.12/24 ]
    ny_cfg_drop_section node pi-2 "$CONF"
    ny_cfg_parse "$CONF"
    run ny_cfg_subs node
    assert_output $'pi-1\npi-3'
}

@test "values that need it are quoted on write" {
    ny_cfg_set cluster "" name "has # hash" "$CONF"
    run grep 'name = ' "$CONF"
    assert_output --partial '"has # hash"'
    ny_cfg_parse "$CONF"
    [ "$(ny_cfg_get cluster "" name)" = "has # hash" ]
}

@test "edits are journaled and can be undone" {
    export NODEYARD_CONFIG="${NODEYARD_ROOT}/etc/nodeyard/cluster.conf"
    ny_paths_init
    mkdir -p "${NODEYARD_ROOT}/etc/nodeyard"
    cp "$CONF" "$NY_CONFIG"
    NY_CMD_PATH="config set"
    ny_cfg_set cluster "" name changed
    run ny_journal_entries --feature config
    assert_output --partial '"path":"/etc/nodeyard/cluster.conf"'
    ny_journal_undo_txn "$(ny_journal_txn)"
    run grep 'name = homelab' "$NY_CONFIG"
    assert_success
}

@test "batched edits make one write" {
    export NODEYARD_CONFIG="${NODEYARD_ROOT}/etc/nodeyard/cluster.conf"
    ny_paths_init
    mkdir -p "${NODEYARD_ROOT}/etc/nodeyard"
    cp "$CONF" "$NY_CONFIG"
    ny_cfg_batch_begin
    ny_cfg_set node pi-1 role agent
    ny_cfg_set node pi-1 interface eth0
    ny_cfg_batch_commit
    run ny_journal_entries
    [ "$(printf '%s\n' "$output" | grep -c '"op":"write"')" -eq 1 ]
    ny_cfg_parse "$NY_CONFIG"
    [ "$(ny_cfg_get node pi-1 interface)" = eth0 ]
}

@test "validation accepts a good file and explains bad values" {
    ny_cfg_validate "$CONF"
    [[ "${NY_CFG_WARNINGS[*]}" == *"unknown key 'note'"* ]]
    printf '[node "pi-1"]\naddress = 192.168.1.300/24\nrole = boss\n' >"$CONF"
    run ny_cfg_validate "$CONF"
    assert_failure
    ny_cfg_validate "$CONF" || true
    [[ "${NY_CFG_ERRORS[*]}" == *"line 2: address:"*"not a valid"* ]]
    [[ "${NY_CFG_ERRORS[*]}" == *"line 3: role:"*"not one of"* ]]
}

@test "cross-checks: duplicate addresses, two init servers, VIP and ranges" {
    cat >"$CONF" <<'CONF'
[cluster]
vip = 192.168.1.10
[network]
subnet = 192.168.1.0/24
dhcp-range = 192.168.1.100-192.168.1.250
lb-range = 192.168.1.200-192.168.1.220
node-range = 10.0.0.1-10.0.0.5
[node "a"]
address = 192.168.1.10/24
init = true
[node "b"]
address = 192.168.1.10/24
init = true
[node "c"]
address = 10.1.1.1/24
CONF
    ny_cfg_validate "$CONF" || true
    local all="${NY_CFG_ERRORS[*]}"
    [[ "$all" == *"'a' and 'b' both use address 192.168.1.10"* ]]
    [[ "$all" == *"2 nodes have 'init = true'"* ]]
    [[ "$all" == *"vip 192.168.1.10 is also node"* ]]
    [[ "$all" == *"outside the network subnet"* ]]
    [[ "$all" == *"overlaps"* ]]
    [[ "$all" == *"node-range 10.0.0.1-10.0.0.5 is not inside the subnet"* ]]
}

@test "a node name must be a valid hostname" {
    printf '[node "bad name"]\nrole = server\n' >"$CONF"
    ny_cfg_validate "$CONF" || true
    [[ "${NY_CFG_ERRORS[*]}" == *"node name:"* ]]
}
