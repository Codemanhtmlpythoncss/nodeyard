#!/usr/bin/env bats
# Terminal UI: tables, colour handling and plain prompts.

setup() {
    load ../helpers/common
    ny_lib_setup
}

@test "tables align columns" {
    run ny_table <<<$'NAME\tSTATE\nyard-1\tReady\nlonger-name\tNotReady'
    assert_line --index 0 "NAME         STATE"
    assert_line --index 1 "yard-1       Ready"
    assert_line --index 2 "longer-name  NotReady"
}

@test "tables shrink to fit narrow terminals but keep the first column" {
    COLUMNS=40 run ny_table <<<$'ID\tDESCRIPTION\nchange-20261005-123456\tA very long description that will not fit at all'
    local l
    for l in "${lines[@]}"; do ((${#l} <= 40)) || fail "line too long: $l"; done
    assert_line --index 1 --partial "change-20261005-123456"
}

@test "NO_COLOR output has no escape codes" {
    export NO_COLOR=1 NODEYARD_COLOR=auto
    ny_term_init
    run --separate-stderr ny_ok "done"
    [[ "$stderr" != *$'\033'* ]]
}

@test "plain choose accepts a number, a value, or the default" {
    export NODEYARD_UI=plain NODEYARD_INTERACTIVE=1
    NY_UI=""
    run ny_ui_choose "Pick" b $'a\tApple' $'b\tBanana' <<<"1"
    assert_output a
    NY_UI=""
    run ny_ui_choose "Pick" b $'a\tApple' $'b\tBanana' <<<""
    assert_output b
    NY_UI=""
    run ny_ui_choose "Pick" b $'a\tApple' $'b\tBanana' <<<$'9\nb'
    assert_output --partial b
}

@test "plain input re-asks until the value is valid" {
    export NODEYARD_UI=plain NODEYARD_INTERACTIVE=1
    NY_UI=""
    run ny_ui_input "Address" "" ipv4 <<<$'999.1.1.1\n10.0.0.5'
    assert_output --partial "10.0.0.5"
}

@test "q cancels and b goes back (in wizards)" {
    export NODEYARD_UI=plain NODEYARD_INTERACTIVE=1
    NY_UI=""
    run ny_ui_choose "Pick" a $'a\tApple' <<<"q"
    assert_failure 1
    NY_UI=""
    NY_UI_BACK=1
    run ny_ui_input "Name" "x" string <<<"b"
    assert_failure 2
}

@test "without a terminal, confirmations need --yes (exit 10)" {
    NY_UI=""
    run ny_confirm "Do it?"
    assert_failure 10
    NY_YES=1
    run ny_confirm "Do it?"
    assert_success
}

@test "optional offers take their default without a terminal" {
    NY_UI=""
    run ny_offer "Extra?" n
    assert_failure
    run ny_offer "Extra?" y
    assert_success
}
