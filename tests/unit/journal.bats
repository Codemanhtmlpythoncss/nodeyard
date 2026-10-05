#!/usr/bin/env bats
# File writes, the change journal, dry-run and undo.

setup() {
    load ../helpers/common
    ny_lib_setup
    NY_CMD_PATH="test change"
    NY_FEATURE="test"
}

@test "writing a new file creates it and records it" {
    printf 'hello\n' | ny_write_file /etc/example.conf
    [ "$(cat "${NODEYARD_ROOT}/etc/example.conf")" = hello ]
    run ny_journal_entries
    assert_output --partial '"op":"write","path":"/etc/example.conf","existed":false'
}

@test "writing identical content is a no-op (idempotent)" {
    printf 'same\n' | ny_write_file /etc/example.conf
    printf 'same\n' | ny_write_file /etc/example.conf
    [ "$(ny_journal_entries | grep -c '"op":"write"')" -eq 1 ]
}

@test "dry-run changes nothing and records a plan" {
    NY_DRY_RUN=1
    run --separate-stderr ny_write_file /etc/new.conf <<<"x"
    [ ! -e "${NODEYARD_ROOT}/etc/new.conf" ]
    [[ "$stderr" == *"[dry-run] would create"* ]]
    printf 'x\n' | ny_write_file /etc/new.conf 2>/dev/null
    [ "${#NY_PLAN[@]}" -eq 1 ]
    [[ "${NY_PLAN[0]}" == *'"type":"write"'* ]]
    [ ! -e "$(ny_journal_dir)/journal.jsonl" ]
}

@test "secret files never show their contents in a dry run" {
    NY_DRY_RUN=1
    run --separate-stderr ny_write_file /etc/nodeyard/secrets/x 0600 <<<"topsecret-value"
    [[ "$stderr" != *"topsecret-value"* ]]
    [[ "$stderr" == *"contents hidden"* ]]
}

@test "undo restores an overwritten file and removes a created one" {
    mkdir -p "${NODEYARD_ROOT}/etc"
    printf 'original\n' >"${NODEYARD_ROOT}/etc/existing.conf"
    printf 'changed\n' | ny_write_file /etc/existing.conf
    printf 'new\n' | ny_write_file /etc/created.conf
    ny_journal_undo_txn "$(ny_journal_txn)"
    [ "$(cat "${NODEYARD_ROOT}/etc/existing.conf")" = original ]
    [ ! -e "${NODEYARD_ROOT}/etc/created.conf" ]
}

@test "undo refuses to clobber a file someone edited afterwards (unless forced)" {
    printf 'mine\n' | ny_write_file /etc/edited.conf
    printf 'hand edit\n' >"${NODEYARD_ROOT}/etc/edited.conf"
    run ny_journal_undo_txn "$(ny_journal_txn)"
    assert_failure
    [ "$(cat "${NODEYARD_ROOT}/etc/edited.conf")" = "hand edit" ]
    ny_journal_undo_txn "$(ny_journal_txn)" 1
    [ ! -e "${NODEYARD_ROOT}/etc/edited.conf" ]
}

@test "an undone change is not undone twice" {
    printf 'a\n' | ny_write_file /etc/once.conf
    local t
    t="$(ny_journal_txn)"
    ny_journal_undo_txn "$t"
    run ny_journal_entries --txn "$t"
    assert_output ""
}

@test "undo by feature only touches that feature" {
    printf 'k\n' | NY_FEATURE=k3s ny_write_file /etc/k3s-thing.conf
    NY_TXN=""
    printf 'o\n' | NY_FEATURE=other ny_write_file /etc/other-thing.conf
    ny_journal_undo_feature k3s
    [ ! -e "${NODEYARD_ROOT}/etc/k3s-thing.conf" ]
    [ -e "${NODEYARD_ROOT}/etc/other-thing.conf" ]
}

@test "removed files come back on undo" {
    mkdir -p "${NODEYARD_ROOT}/etc"
    printf 'keep me\n' >"${NODEYARD_ROOT}/etc/doomed.conf"
    ny_remove_file /etc/doomed.conf
    [ ! -e "${NODEYARD_ROOT}/etc/doomed.conf" ]
    ny_journal_undo_txn "$(ny_journal_txn)"
    [ "$(cat "${NODEYARD_ROOT}/etc/doomed.conf")" = "keep me" ]
}

@test "created directories are recorded and removed when empty" {
    ny_ensure_dir /etc/brand/new/dir
    [ -d "${NODEYARD_ROOT}/etc/brand/new/dir" ]
    ny_journal_undo_txn "$(ny_journal_txn)"
    [ ! -e "${NODEYARD_ROOT}/etc/brand" ]
}

@test "ny_run executes for real outside demo/dry-run, and records commands in dry-run" {
    run ny_run printf 'ran'
    assert_output ran
    NY_DRY_RUN=1
    run --separate-stderr ny_run touch "${BATS_TEST_TMPDIR}/should-not-exist"
    [ ! -e "${BATS_TEST_TMPDIR}/should-not-exist" ]
    [[ "$stderr" == *"would run: touch"* ]]
}

@test "the journal lists one line per command" {
    printf 'a\n' | ny_write_file /etc/a.conf
    printf 'b\n' | ny_write_file /etc/b.conf
    run ny_journal_txns
    [ "$(printf '%s\n' "$output" | grep -c .)" -eq 1 ]
    printf '%s' "$output" | jq -e '.cmd == "test change" and (.paths | index("/etc/a.conf")) != null and (.paths | index("/etc/b.conf")) != null' >/dev/null
}
