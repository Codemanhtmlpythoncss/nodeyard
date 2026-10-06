# shellcheck shell=bash
# shellcheck disable=SC2034 # globals here are read by other files
# Errors: plain-English messages with a suggested fix, stable exit codes, and
# a machine-readable form for --json (used by the dashboard agent).

NY_E_FAIL=1         # the action failed
NY_E_USAGE=2        # bad command line
NY_E_PRECONDITION=3 # missing dependency, not root, wrong kind of node...
NY_E_PARTIAL=4      # some targets succeeded, some failed
NY_E_CONFIRM=10     # needs confirmation: re-run with --yes
NY_E_CANCELLED=130  # the user cancelled

# ny_die MESSAGE [FIX] [EXIT_CODE] -- stop with a clear error and a fix.
ny_die() {
    local msg="$1" fix="${2:-}" code="${3:-$NY_E_FAIL}"
    ny_log ERROR "$msg${fix:+ (fix: $fix)}"
    if [[ "$NY_JSON" -eq 1 ]]; then
        printf '%s\n' "$(ny_json_obj ok:=false "error:=$(ny_json_obj "message=$(ny_redact "$msg")" "fix?=$(ny_redact "$fix")" "code:=$code")")" >&"${NY_JSON_FD:-1}"
    fi
    printf '%s %s\n' "$(ny_color red "$NY_SYM_FAIL Error:")" "$(ny_redact "$msg")" >&2
    if [[ -n "$fix" ]]; then
        printf '  %s %s\n' "$(ny_color bold "Fix:")" "$(ny_redact "$fix")" >&2
    fi
    if [[ "$NY_LOG_READY" -eq 1 && "$code" -ne "$NY_E_USAGE" ]]; then
        printf '  %s\n' "$(ny_color dim "Details are in ${NY_LOG_FILE}")" >&2
    fi
    NY_DIED=1
    # In a $(...) subshell NY_DIED can't reach the main shell; $$ is still the
    # main shell's PID there, so leave a marker for ny_on_err to find.
    : >"$(ny_died_marker)" 2>/dev/null || true
    exit "$code"
}

ny_died_marker() {
    printf '%s/nodeyard-died.%s\n' "${TMPDIR:-/tmp}" "$$"
}

# ny_usage_error MESSAGE [USAGE] -- a bad command line.
ny_usage_error() {
    local msg="$1" usage="${2:-}"
    local fix="Run 'nodeyard ${NY_CMD_PATH:+$NY_CMD_PATH }--help' to see the options."
    [[ -n "$usage" ]] && fix="Usage: ${usage}"
    ny_die "$msg" "$fix" "$NY_E_USAGE"
}

# ny_need_root -- most actions change the system and need root.
ny_need_root() {
    ny_is_root && return 0
    ny_die "This needs root privileges." "Run it with sudo: sudo nodeyard ${NY_CMD_PATH}" "$NY_E_PRECONDITION"
}

# ny_need_value FLAG ARGC -- a flag that takes a value was given without one.
ny_need_value() {
    [[ "$2" -ge 2 ]] || ny_usage_error "$1 needs a value."
}

NY_DIED=0
NY_ERR_REPORTED=0

# Unexpected failures (set -e) end up here: log them and point at the log.
ny_on_err() {
    local code=$? line="${BASH_LINENO[0]:-0}" cmd="${BASH_COMMAND:-?}"
    [[ "$NY_DIED" -eq 1 || "$NY_ERR_REPORTED" -eq 1 ]] && return 0
    [[ "${BASHPID:-$$}" == "$$" ]] || return 0
    # A ny_die inside a command substitution already explained itself.
    if [[ -e "$(ny_died_marker)" ]]; then
        rm -f "$(ny_died_marker)"
        NY_DIED=1
        return 0
    fi
    # A command deliberately returning an exit code (e.g. 130 = cancelled)
    # is not an unexpected failure.
    [[ "$cmd" == *'NY_CMD_FN['* ]] && return 0
    # So is a function's explicit "return N", or a cancellation.
    [[ "$cmd" == return* || "$code" -eq "$NY_E_CANCELLED" ]] && return 0
    NY_ERR_REPORTED=1
    ny_log ERROR "unexpected failure (exit ${code}) at ${BASH_SOURCE[1]:-?}:${line}: ${cmd}"
    printf '%s %s\n' "$(ny_color red "$NY_SYM_FAIL A step failed unexpectedly:")" "$(ny_redact "$cmd") (exit ${code})" >&2
    printf '  %s %s\n' "$(ny_color bold "Fix:")" "Run 'sudo nodeyard doctor' to look for common problems; full details are in ${NY_LOG_FILE}" >&2
    if [[ "$NY_JSON" -eq 1 ]]; then
        printf '%s\n' "$(ny_json_obj ok:=false "error:=$(ny_json_obj "message=Unexpected failure: $(ny_redact "$cmd")" "fix=Run 'sudo nodeyard doctor'" "code:=$code")")" >&"${NY_JSON_FD:-1}"
    fi
    return 0
}

ny_on_exit() {
    rm -f "$(ny_died_marker)" 2>/dev/null || true
    ny_cleanup_tmp
}
