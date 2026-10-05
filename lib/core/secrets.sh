# shellcheck shell=bash
# Secrets (join tokens, passwords, API keys): stored one per file under
# ${NY_SECRETS_DIR} (0700) with mode 0600, never printed or logged, and
# registered for redaction as soon as they are read.

ny_secret_path() {
    printf '%s/%s\n' "${NY_SECRETS_DIR#"$NY_ROOT"}" "$1"
}

ny_secret_valid_name() {
    [[ "$1" =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] ||
        ny_die "Invalid secret name '$1'." "Use lowercase letters, digits, dots, dashes or underscores." "$NY_E_USAGE"
}

ny_secret_exists() {
    ny_secret_valid_name "$1"
    [[ -s "$(ny_path "$(ny_secret_path "$1")")" ]]
}

# ny_secret_get NAME -- print a secret (for piping into files/commands only).
ny_secret_get() {
    ny_secret_valid_name "$1"
    local f v
    f="$(ny_path "$(ny_secret_path "$1")")"
    [[ -r "$f" ]] || ny_die "Secret '$1' is not set." "Set it with: sudo nodeyard secrets set $1" "$NY_E_PRECONDITION"
    v="$(<"$f")"
    ny_secret_register "$v"
    printf '%s' "$v"
}

# ny_secret_set NAME < VALUE -- store a secret from stdin (trailing newline dropped).
ny_secret_set() {
    ny_secret_valid_name "$1"
    local v
    v="$(cat)"
    [[ -n "$v" ]] || ny_die "Refusing to store an empty secret '$1'." "" "$NY_E_USAGE"
    ny_secret_register "$v"
    ny_ensure_dir "${NY_SECRETS_DIR#"$NY_ROOT"}" 0700
    printf '%s' "$v" | ny_write_file "$(ny_secret_path "$1")" 0600
}

# ny_secret_generate NAME [BYTES] -- create a random secret if it doesn't exist.
ny_secret_generate() {
    local name="$1" bytes="${2:-32}"
    ny_secret_exists "$name" && return 0
    ny_random_hex "$bytes" | ny_secret_set "$name"
}

ny_random_hex() {
    local bytes="${1:-32}"
    if have openssl; then
        openssl rand -hex "$bytes"
    else
        head -c "$bytes" /dev/urandom | od -An -tx1 | tr -d ' \n'
    fi
    return 0
}

# ny_secret_list -- "NAME<TAB>MODIFIED" for every stored secret (never values).
ny_secret_list() {
    local d f
    d="$(ny_path "${NY_SECRETS_DIR#"$NY_ROOT"}")"
    [[ -d "$d" ]] || return 0
    for f in "$d"/*; do
        [[ -f "$f" ]] || continue
        printf '%s\t%s\n' "$(basename -- "$f")" "$(date -r "$f" '+%Y-%m-%d %H:%M' 2>/dev/null || echo '?')"
    done
    return 0
}
