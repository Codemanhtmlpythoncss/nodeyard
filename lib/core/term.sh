# shellcheck shell=bash
# Terminal capabilities: colour (respecting NO_COLOR), width and symbols.

NY_COLOR_MODE="${NODEYARD_COLOR:-auto}" # auto | always | never
NY_COLOR=0
NY_UTF8=0
NY_C_RESET="" NY_C_BOLD="" NY_C_DIM="" NY_C_RED="" NY_C_GREEN="" NY_C_YELLOW="" NY_C_BLUE="" NY_C_CYAN=""
NY_SYM_OK="OK" NY_SYM_FAIL="FAIL" NY_SYM_WARN="!" NY_SYM_STEP=">" NY_SYM_ARROW="->" NY_SYM_DOT="-"

ny_term_init() {
    NY_COLOR=0
    case "$NY_COLOR_MODE" in
        always) NY_COLOR=1 ;;
        never) NY_COLOR=0 ;;
        *)
            if [[ -z "${NO_COLOR:-}" && -t 2 && "${TERM:-dumb}" != "dumb" ]]; then
                NY_COLOR=1
            fi
            ;;
    esac
    if [[ "$NY_COLOR" -eq 1 ]]; then
        NY_C_RESET=$'\033[0m'
        NY_C_BOLD=$'\033[1m'
        NY_C_DIM=$'\033[2m'
        NY_C_RED=$'\033[31m'
        NY_C_GREEN=$'\033[32m'
        NY_C_YELLOW=$'\033[33m'
        NY_C_BLUE=$'\033[34m'
        NY_C_CYAN=$'\033[36m'
    else
        NY_C_RESET="" NY_C_BOLD="" NY_C_DIM="" NY_C_RED="" NY_C_GREEN="" NY_C_YELLOW="" NY_C_BLUE="" NY_C_CYAN=""
    fi

    local lc="${LC_ALL:-${LC_CTYPE:-${LANG:-}}}"
    if [[ "${lc,,}" == *utf-8* || "${lc,,}" == *utf8* ]]; then
        NY_UTF8=1
        NY_SYM_OK="✓" NY_SYM_FAIL="✗" NY_SYM_WARN="⚠" NY_SYM_STEP="▸" NY_SYM_ARROW="→" NY_SYM_DOT="•"
    else
        NY_UTF8=0
        NY_SYM_OK="OK" NY_SYM_FAIL="FAIL" NY_SYM_WARN="!" NY_SYM_STEP=">" NY_SYM_ARROW="->" NY_SYM_DOT="-"
    fi
    return 0
}

# ny_term_width -- usable terminal width (minimum 40).
ny_term_width() {
    local w="${COLUMNS:-}"
    if [[ ! "$w" =~ ^[0-9]+$ ]] && [[ -t 2 ]] && have tput; then
        w="$(tput cols 2>/dev/null || true)"
    fi
    [[ "$w" =~ ^[0-9]+$ ]] || w=80
    ((w < 40)) && w=40
    printf '%s\n' "$w"
}

# ny_strip_ansi TEXT -- TEXT without colour escape codes.
ny_strip_ansi() {
    local s="$1" re=$'\033''\[[0-9;]*m'
    while [[ "$s" =~ $re ]]; do
        s="${s/"${BASH_REMATCH[0]}"/}"
    done
    printf '%s' "$s"
}

# ny_color NAME TEXT -- TEXT wrapped in a colour (no-op without colour).
ny_color() {
    local name="$1" text="$2" code=""
    case "$name" in
        red) code="$NY_C_RED" ;;
        green) code="$NY_C_GREEN" ;;
        yellow) code="$NY_C_YELLOW" ;;
        blue) code="$NY_C_BLUE" ;;
        cyan) code="$NY_C_CYAN" ;;
        bold) code="$NY_C_BOLD" ;;
        dim) code="$NY_C_DIM" ;;
    esac
    printf '%s%s%s' "$code" "$text" "${code:+$NY_C_RESET}"
}
