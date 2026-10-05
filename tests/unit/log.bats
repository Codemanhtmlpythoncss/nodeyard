#!/usr/bin/env bats
# Logging and secret redaction.

setup() {
    load ../helpers/common
    ny_lib_setup
}

@test "registered secrets are redacted" {
    ny_secret_register "hunter2-secret-value"
    run ny_redact "the password is hunter2-secret-value, ok"
    assert_output "the password is [REDACTED], ok"
}

@test "very short values are not registered (they'd redact ordinary words)" {
    ny_secret_register "abc"
    run ny_redact "abc def"
    assert_output "abc def"
}

@test "key=value secrets are redacted without registration" {
    run ny_redact "K3S_TOKEN=abcdef123456 next"
    assert_output "K3S_TOKEN=[REDACTED] next"
    run ny_redact 'api_key: "s3cr3tvalue"'
    assert_output 'api_key: "[REDACTED]"'
    run ny_redact "Password=letmein123 user=bob"
    assert_output "Password=[REDACTED] user=bob"
}

@test "bearer tokens are redacted including the token itself" {
    run ny_redact "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
    refute_output --partial "eyJhbGci"
    assert_output --partial "[REDACTED]"
}

@test "k3s join tokens are redacted by shape" {
    run ny_redact "join with K10a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8::server:0123456789abcdef"
    refute_output --partial "K10a1b2"
    assert_output --partial "[REDACTED]"
}

@test "ordinary text is left alone" {
    run ny_redact "Installed k3s on yard-1 (192.168.1.10)"
    assert_output "Installed k3s on yard-1 (192.168.1.10)"
}

@test "the log file gets redacted lines" {
    ny_secret_register "very-secret-token-42"
    ny_log INFO "using very-secret-token-42 for the join"
    run cat "$NY_LOG_FILE"
    assert_output --partial "using [REDACTED] for the join"
    refute_output --partial "very-secret-token-42"
}

@test "messages go to stderr, not stdout" {
    run --separate-stderr ny_info "hello"
    assert_output ""
    [[ "$stderr" == *hello* ]]
}
