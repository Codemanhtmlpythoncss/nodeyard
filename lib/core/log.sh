# shellcheck shell=bash
# Logging and user-facing messages.
#
# Everything is appended to NY_LOG_FILE with secrets redacted. Human messages
# go to stderr so that stdout stays clean for command results and --json.

# Secret values registered at runtime; replaced by [REDACTED] everywhere.
NY_SECRET_VALUES=()

# ny_secret_register VALUE -- never print or log VALUE.
ny_secret_register() {
    local v="$1"
    # Very short values would redact ordinary words; real secrets are longer.
    ((${#v} >= 6)) || return 0
    ny_in_list "$v" "${NY_SECRET_VALUES[@]+"${NY_SECRET_VALUES[@]}"}" && return 0
    NY_SECRET_VALUES+=("$v")
}

# Patterns for secrets that were never registered: KEY=VALUE / KEY: VALUE
# pairs whose key looks secret, bearer tokens and k3s join tokens.
NY_REDACT_KV_RE='((token|password|passwd|secret|api[_-]?key|apikey|private[_-]?key|credential|auth)[A-Za-z0-9_-]*["'\'']?[[:space:]]*[=:][[:space:]]*["'\'']?)([^[:space:]"'\'',;&]+)'
NY_REDACT_BEARER_RE='((bearer|basic)[[:space:]]+)([A-Za-z0-9._~+/=-]{8,})'
NY_REDACT_K3S_RE='()(K10[0-9a-f]{20,}::[A-Za-z0-9._-]+:[A-Za-z0-9._-]+)'

# ny_redact TEXT -- TEXT with known and likely secrets replaced.
ny_redact() {
    local s="$1" v
    for v in "${NY_SECRET_VALUES[@]+"${NY_SECRET_VALUES[@]}"}"; do
        s="${s//"$v"/[REDACTED]}"
    done
    local re out rest match
    local restore_nocase=0
    shopt -q nocasematch || {
        shopt -s nocasematch
        restore_nocase=1
    }
    # Bearer first: "Authorization: Bearer X" must lose X, not just "Bearer".
    for re in "$NY_REDACT_BEARER_RE" "$NY_REDACT_KV_RE" "$NY_REDACT_K3S_RE"; do
        out=""
        rest="$s"
        while [[ "$rest" =~ $re ]]; do
            match="${BASH_REMATCH[0]}"
            if [[ "${BASH_REMATCH[${#BASH_REMATCH[@]} - 1]}" == "[REDACTED]"* ]]; then
                out+="${rest%%"$match"*}${match}"
            else
                out+="${rest%%"$match"*}${BASH_REMATCH[1]}[REDACTED]"
            fi
            rest="${rest#*"$match"}"
        done
        s="${out}${rest}"
    done
    ((restore_nocase)) && shopt -u nocasematch
    printf '%s' "$s"
}

NY_LOG_READY=0

ny_log_init() {
    [[ "$NY_LOG_READY" -eq 1 ]] && return 0
    if mkdir -p -- "$NY_LOG_DIR" 2>/dev/null; then
        chmod 0750 -- "$NY_LOG_DIR" 2>/dev/null || true
        if [[ ! -e "$NY_LOG_FILE" ]]; then
            (umask 027 && : >>"$NY_LOG_FILE") 2>/dev/null || true
        fi
        [[ -w "$NY_LOG_FILE" ]] && NY_LOG_READY=1
    fi
    [[ "$NY_LOG_READY" -eq 1 ]] || NY_LOG_READY=2
    return 0
}

# ny_log LEVEL MESSAGE -- append one redacted line to the log file.
ny_log() {
    local level="$1"
    shift
    ny_log_init
    [[ "$NY_LOG_READY" -eq 1 ]] || return 0
    local msg
    msg="$(ny_redact "$*")"
    msg="${msg//$'\n'/ | }"
    printf '%s %-5s [%s] %s%s\n' "$(ny_now)" "$level" "$$" "${NY_CMD_PATH:+($NY_CMD_PATH) }" "$msg" >>"$NY_LOG_FILE" 2>/dev/null || true
}

# --- messages ----------------------------------------------------------------

ny_say() {
    printf '%s\n' "$(ny_redact "$*")" >&2
}

ny_info() {
    ny_log INFO "$*"
    ny_say "$*"
}

ny_step() {
    ny_log STEP "$*"
    printf '%s %s\n' "$(ny_color blue "$NY_SYM_STEP")" "$(ny_color bold "$(ny_redact "$*")")" >&2
}

ny_ok() {
    ny_log OK "$*"
    printf '%s %s\n' "$(ny_color green "$NY_SYM_OK")" "$(ny_redact "$*")" >&2
}

ny_warn() {
    ny_log WARN "$*"
    printf '%s %s\n' "$(ny_color yellow "$NY_SYM_WARN")" "$(ny_redact "$*")" >&2
}

ny_err() {
    ny_log ERROR "$*"
    printf '%s %s\n' "$(ny_color red "$NY_SYM_FAIL")" "$(ny_redact "$*")" >&2
}

# ny_hint TEXT -- an indented, dimmed follow-up line (fix suggestions etc.).
ny_hint() {
    ny_log HINT "$*"
    printf '  %s\n' "$(ny_color dim "$(ny_redact "$*")")" >&2
}

ny_vlog() {
    ny_log DEBUG "$*"
    [[ "$NY_VERBOSE" -eq 1 ]] || return 0
    printf '%s %s\n' "$(ny_color dim "[verbose]")" "$(ny_redact "$*")" >&2
}
