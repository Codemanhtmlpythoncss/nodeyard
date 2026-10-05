# shellcheck shell=bash
# Terminal UI primitives with four backends, best first: gum, whiptail,
# dialog, plain prompts. Anything that works over a bare SSH session works
# here. Prompts draw on stderr/the terminal; chosen values go to stdout.
#
# Return codes for interactive prompts: 0 answered, 1 cancelled, 2 back.
# "Back" is only offered while NY_UI_BACK=1 (inside wizards).

NY_UI=""
NY_UI_BACK=0
NY_UI_TITLE="nodeyard"

# ny_ui_init -- pick the backend once: env NODEYARD_UI, config ui.backend, auto.
ny_ui_init() {
    [[ -n "$NY_UI" ]] && return 0
    # NODEYARD_INTERACTIVE=1 forces prompts without a terminal (scripted
    # input, tests); otherwise no terminal means no prompts.
    if [[ "${NODEYARD_INTERACTIVE:-0}" != 1 ]] && [[ "$NY_NONINTERACTIVE" -eq 1 || ! -t 2 ]]; then
        NY_UI="none"
        return 0
    fi
    local pref="${NODEYARD_UI:-}"
    if [[ -z "$pref" ]]; then
        pref="$(ny_cfg_get ui "" backend auto)"
    fi
    local width
    width="$(ny_term_width)"
    case "$pref" in
        gum | whiptail | dialog)
            if have "$pref"; then
                NY_UI="$pref"
            else
                ny_vlog "ui backend '${pref}' is not installed; choosing automatically"
            fi
            ;;
        plain) NY_UI="plain" ;;
    esac
    if [[ -z "$NY_UI" ]]; then
        if [[ "${TERM:-dumb}" == dumb ]]; then
            NY_UI="plain"
        elif have gum; then
            NY_UI="gum"
        elif have whiptail; then
            NY_UI="whiptail"
        elif have dialog; then
            NY_UI="dialog"
        else
            NY_UI="plain"
        fi
    fi
    # Boxed dialogs need room; fall back to plain prompts on narrow terminals.
    if [[ ("$NY_UI" == whiptail || "$NY_UI" == dialog) && "$width" -lt 50 ]]; then
        NY_UI="plain"
    fi
    return 0
}

ny_ui_interactive() {
    ny_ui_init
    [[ "$NY_UI" != none ]]
}

# Box dimensions for whiptail/dialog.
ny_ui_box_size() {
    local lines cols
    cols="$(ny_term_width)"
    lines="$(tput lines 2>/dev/null || echo 24)"
    [[ "$lines" =~ ^[0-9]+$ ]] || lines=24
    NY_UI_H=$((lines - 4))
    ((NY_UI_H > 22)) && NY_UI_H=22
    ((NY_UI_H < 10)) && NY_UI_H=10
    NY_UI_W=$((cols - 6))
    ((NY_UI_W > 78)) && NY_UI_W=78
    return 0
}

# Run whiptail/dialog with its output (the answer) captured from stderr.
ny_ui_box() {
    local rc=0 out
    out="$("$NY_UI" --title "$NY_UI_TITLE" "$@" 3>&1 1>&2 2>&3)" || rc=$?
    printf '%s' "$out"
    return "$rc"
}

# Map a box exit code: OK=0, Cancel(=Back when enabled)=1, Esc=255.
ny_ui_box_rc() {
    case "$1" in
        0) return 0 ;;
        1) [[ "$NY_UI_BACK" -eq 1 ]] && return 2 || return 1 ;;
        *) return 1 ;;
    esac
}

ny_ui_plain_hint() {
    if [[ "$NY_UI_BACK" -eq 1 ]]; then
        printf '%s' "$(ny_color dim " (b = back, q = quit)")"
    else
        printf '%s' "$(ny_color dim " (q = quit)")"
    fi
    return 0
}

# ny_ui_choose PROMPT DEFAULT ITEM... -- pick one item. Each ITEM is
# "value<TAB>label" or just "value". Prints the chosen value.
ny_ui_choose() {
    local prompt="$1" default="$2"
    shift 2
    local -a values=() labels=()
    local item
    for item in "$@"; do
        if [[ "$item" == *$'\t'* ]]; then
            values+=("${item%%$'\t'*}")
            labels+=("${item#*$'\t'}")
        else
            values+=("$item")
            labels+=("$item")
        fi
    done
    local n=${#values[@]} i def_idx=0
    for ((i = 0; i < n; i++)); do
        [[ "${values[i]}" == "$default" ]] && def_idx=$i
    done

    ny_ui_init
    case "$NY_UI" in
        none)
            if [[ -n "$default" ]]; then
                printf '%s\n' "$default"
                return 0
            fi
            ny_die "A choice is needed: ${prompt}" "Run this interactively, or pass the value as a command-line flag." "$NY_E_CONFIRM"
            ;;
        gum)
            local -a opts=()
            for ((i = 0; i < n; i++)); do
                opts+=("${labels[i]}"$'\t'"${values[i]}")
            done
            local out rc=0
            out="$(gum choose --header "$prompt" --label-delimiter $'\t' --selected "${labels[def_idx]}" -- "${opts[@]}")" || rc=$?
            if [[ "$rc" -ne 0 || -z "$out" ]]; then
                [[ "$NY_UI_BACK" -eq 1 ]] && return 2
                return 1
            fi
            printf '%s\n' "$out"
            ;;
        whiptail | dialog)
            ny_ui_box_size
            local -a args=()
            for ((i = 0; i < n; i++)); do
                args+=("$((i + 1))" "${labels[i]}")
            done
            local list_h=$((NY_UI_H - 8))
            ((list_h > n)) && list_h=$n
            ((list_h < 1)) && list_h=1
            local cancel="Cancel"
            [[ "$NY_UI_BACK" -eq 1 ]] && cancel="Back"
            local -a cl=(--cancel-button "$cancel")
            [[ "$NY_UI" == dialog ]] && cl=(--cancel-label "$cancel")
            local out rc=0
            out="$(ny_ui_box "${cl[@]}" --default-item "$((def_idx + 1))" --menu "$prompt" "$NY_UI_H" "$NY_UI_W" "$list_h" "${args[@]}")" || rc=$?
            ny_ui_box_rc "$rc" || return $?
            printf '%s\n' "${values[out - 1]}"
            ;;
        *)
            local reply
            printf '\n%s\n' "$(ny_color bold "$prompt")" >&2
            for ((i = 0; i < n; i++)); do
                printf '  %s %s\n' "$(ny_color cyan "$(printf '%2d)' "$((i + 1))")")" "${labels[i]}" >&2
            done
            while true; do
                printf '%s%s [%s]: ' "Choice" "$(ny_ui_plain_hint)" "$((def_idx + 1))" >&2
                IFS= read -r reply || return 1
                reply="$(ny_trim "$reply")"
                [[ -z "$reply" ]] && reply="$((def_idx + 1))"
                case "$reply" in
                    q | Q | quit) return 1 ;;
                    b | B | back) [[ "$NY_UI_BACK" -eq 1 ]] && return 2 ;;
                esac
                if [[ "$reply" =~ ^[0-9]+$ ]] && ((reply >= 1 && reply <= n)); then
                    printf '%s\n' "${values[reply - 1]}"
                    return 0
                fi
                for ((i = 0; i < n; i++)); do
                    if [[ "$reply" == "${values[i]}" ]]; then
                        printf '%s\n' "${values[i]}"
                        return 0
                    fi
                done
                printf '%s\n' "$(ny_color yellow "Please enter a number from 1 to ${n}.")" >&2
            done
            ;;
    esac
    return 0
}

# ny_ui_input PROMPT DEFAULT [TYPE] [HELP] -- free text, validated with
# ny_validate TYPE (re-asks until valid). Prints the value.
ny_ui_input() {
    local prompt="$1" default="${2:-}" type="${3:-string}" help="${4:-}"
    ny_ui_init
    local value rc
    while true; do
        rc=0
        case "$NY_UI" in
            none)
                value="$default"
                ;;
            gum)
                [[ -n "$help" ]] && printf '%s\n' "$(ny_color dim "$help")" >&2
                value="$(gum input --header "$prompt" --value "$default" --placeholder "${default:-type here}")" || rc=$?
                ;;
            whiptail | dialog)
                ny_ui_box_size
                value="$(ny_ui_box --inputbox "${prompt}${help:+

${help}}" "$NY_UI_H" "$NY_UI_W" "$default")" || rc=$?
                if [[ "$rc" -ne 0 ]]; then
                    ny_ui_box_rc "$rc" || return $?
                fi
                ;;
            *)
                [[ -n "$help" ]] && printf '%s\n' "$(ny_color dim "$help")" >&2
                printf '%s%s%s: ' "$(ny_color bold "$prompt")" "${default:+ [$default]}" "$(ny_ui_plain_hint)" >&2
                IFS= read -r value || return 1
                value="$(ny_trim "$value")"
                case "$value" in
                    q | quit) return 1 ;;
                    b | back) [[ "$NY_UI_BACK" -eq 1 ]] && return 2 ;;
                esac
                [[ -z "$value" ]] && value="$default"
                ;;
        esac
        if [[ "$rc" -ne 0 ]]; then
            [[ "$NY_UI_BACK" -eq 1 ]] && return 2
            return 1
        fi
        if ny_validate "$type" "$value"; then
            printf '%s\n' "$value"
            return 0
        fi
        if [[ "$NY_UI" == none ]]; then
            ny_die "Invalid value for '${prompt}': ${NY_VALID_MSG}" "Pass a valid value on the command line." "$NY_E_USAGE"
        fi
        ny_warn "$NY_VALID_MSG"
        default="$value"
    done
}

# ny_ui_secret PROMPT -- read a secret without echo (kept in memory only).
ny_ui_secret() {
    local prompt="$1" value rc=0
    ny_ui_init
    case "$NY_UI" in
        none) ny_die "A secret is needed: ${prompt}" "Run this interactively, or pass the secret through a file option." "$NY_E_CONFIRM" ;;
        gum) value="$(gum input --password --header "$prompt" --placeholder "")" || rc=$? ;;
        whiptail | dialog)
            ny_ui_box_size
            value="$(ny_ui_box --passwordbox "$prompt" 10 "$NY_UI_W")" || rc=$?
            ;;
        *)
            printf '%s: ' "$(ny_color bold "$prompt")" >&2
            IFS= read -rs value || rc=1
            printf '\n' >&2
            ;;
    esac
    [[ "$rc" -eq 0 ]] || return 1
    ny_secret_register "$value"
    printf '%s' "$value"
}

# ny_ui_yesno PROMPT [DEFAULT y|n] -- 0 yes, 1 no, 2 back.
ny_ui_yesno() {
    local prompt="$1" default="${2:-y}" rc=0
    ny_ui_init
    case "$NY_UI" in
        none) [[ "$default" == y ]] ;;
        gum)
            local d="--default=true"
            [[ "$default" == y ]] || d="--default=false"
            gum confirm "$d" "$prompt" || rc=$?
            case "$rc" in
                0) return 0 ;;
                1) return 1 ;;
                *) [[ "$NY_UI_BACK" -eq 1 ]] && return 2 || return 1 ;;
            esac
            ;;
        whiptail | dialog)
            ny_ui_box_size
            local -a d=()
            [[ "$default" == y ]] || d=(--defaultno)
            ny_ui_box "${d[@]+"${d[@]}"}" --yesno "$prompt" 12 "$NY_UI_W" >/dev/null || rc=$?
            case "$rc" in
                0) return 0 ;;
                1) return 1 ;;
                *) [[ "$NY_UI_BACK" -eq 1 ]] && return 2 || return 1 ;;
            esac
            ;;
        *)
            local hint="y/N" reply
            [[ "$default" == y ]] && hint="Y/n"
            while true; do
                printf '%s [%s]%s ' "$(ny_color bold "$prompt")" "$hint" "$( [[ "$NY_UI_BACK" -eq 1 ]] && ny_color dim " (b = back)")" >&2
                IFS= read -r reply || return 1
                reply="$(ny_trim "${reply,,}")"
                [[ -z "$reply" ]] && reply="$default"
                case "$reply" in
                    y | yes) return 0 ;;
                    n | no) return 1 ;;
                    b | back) [[ "$NY_UI_BACK" -eq 1 ]] && return 2 ;;
                esac
                printf '%s\n' "$(ny_color yellow "Please answer y or n.")" >&2
            done
            ;;
    esac
}

# ny_confirm PROMPT [DEFAULT] -- the gate before a requested action.
# --yes and --dry-run proceed; non-interactive runs without --yes stop with
# exit code 10 so callers (the dashboard) know confirmation is needed.
ny_confirm() {
    local prompt="$1" default="${2:-y}"
    if [[ "$NY_YES" -eq 1 || "$NY_DRY_RUN" -eq 1 ]]; then
        ny_log CONFIRM "auto-confirmed: ${prompt}"
        return 0
    fi
    if ! ny_ui_interactive; then
        ny_die "Confirmation needed: ${prompt}" "Re-run with --yes to confirm (add --dry-run first to see exactly what will change)." "$NY_E_CONFIRM"
    fi
    local rc=0
    ny_ui_yesno "$prompt" "$default" || rc=$?
    ny_log CONFIRM "${prompt} -> $([[ $rc -eq 0 ]] && echo yes || echo no)"
    return "$rc"
}

# ny_offer PROMPT [DEFAULT] -- an optional extra step. --yes, --dry-run and
# non-interactive runs take the default.
ny_offer() {
    local prompt="$1" default="${2:-n}"
    if [[ "$NY_YES" -eq 1 || "$NY_DRY_RUN" -eq 1 ]] || ! ny_ui_interactive; then
        [[ "$default" == y ]]
        return $?
    fi
    ny_ui_yesno "$prompt" "$default"
}

# ny_ask PROMPT [DEFAULT] [TYPE] -- a value, defaulting when non-interactive.
ny_ask() {
    if ! ny_ui_interactive; then
        printf '%s\n' "${2:-}"
        return 0
    fi
    ny_ui_input "$1" "${2:-}" "${3:-string}"
}

# ny_ui_msg TITLE TEXT -- show a block of text and wait for the user.
ny_ui_msg() {
    local title="$1" text="$2"
    ny_ui_init
    case "$NY_UI" in
        whiptail | dialog)
            ny_ui_box_size
            NY_UI_TITLE="$title" ny_ui_box --msgbox "$text" "$NY_UI_H" "$NY_UI_W" >/dev/null || true
            ;;
        none) printf '%s\n%s\n' "$title" "$text" >&2 ;;
        *)
            printf '\n%s\n%s\n' "$(ny_color bold "$title")" "$text" >&2
            ;;
    esac
    return 0
}

# ny_ui_pause -- "press Enter to continue" (interactive only).
ny_ui_pause() {
    ny_ui_interactive || return 0
    local _
    printf '%s' "$(ny_color dim "Press Enter to continue...")" >&2
    IFS= read -r _ || true
}

# --- progress ----------------------------------------------------------------

# ny_spin TITLE CMD... -- run CMD with a spinner; output is shown only if it
# fails (or with --verbose). Returns CMD's exit code.
ny_spin() {
    local title="$1"
    shift
    if [[ "$NY_VERBOSE" -eq 1 || ! -t 2 || "$NY_JSON" -eq 1 ]]; then
        ny_step "$title"
        "$@"
        return $?
    fi
    local out rc=0 pid i=0
    out="$(ny_mktemp)"
    ("$@") >"$out" 2>&1 &
    pid=$!
    local -a frames=('|' '/' '-' '\')
    [[ "$NY_UTF8" -eq 1 ]] && frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
    printf '\033[?25l' >&2
    while kill -0 "$pid" 2>/dev/null; do
        printf '\r%s %s' "$(ny_color cyan "${frames[i % ${#frames[@]}]}")" "$title" >&2
        i=$((i + 1))
        sleep 0.1
    done
    wait "$pid" || rc=$?
    printf '\r\033[K\033[?25h' >&2
    cat "$out" >>"$NY_LOG_FILE" 2>/dev/null || true
    if [[ "$rc" -eq 0 ]]; then
        ny_ok "$title"
    else
        ny_err "${title} (failed, exit ${rc})"
        tail -n 25 "$out" | sed 's/^/    /' >&2
    fi
    return "$rc"
}

# ny_progress CURRENT TOTAL [LABEL] -- a progress bar on the terminal.
ny_progress() {
    local cur="$1" total="$2" label="${3:-}"
    ((total > 0)) || total=1
    local pct=$((cur * 100 / total))
    if [[ ! -t 2 ]]; then
        [[ "$cur" -eq "$total" ]] && printf '%s %3d%% %s\n' "progress" "$pct" "$label" >&2
        return 0
    fi
    local width=$(($(ny_term_width) - 20 - ${#label}))
    ((width > 40)) && width=40
    ((width < 10)) && width=10
    local filled=$((pct * width / 100)) bar=""
    local full="#" empty="-"
    [[ "$NY_UTF8" -eq 1 ]] && full="█" empty="░"
    local j
    for ((j = 0; j < width; j++)); do
        if ((j < filled)); then bar+="$full"; else bar+="$empty"; fi
    done
    printf '\r%s %3d%% %s\033[K' "$(ny_color cyan "$bar")" "$pct" "$label" >&2
    [[ "$cur" -ge "$total" ]] && printf '\n' >&2
    return 0
}

# --- tables ------------------------------------------------------------------

# ny_status_color WORD -- colour well-known status words.
ny_status_color() {
    local w="$1"
    case "${w,,}" in
        ready | ok | active | running | online | supported | yes | up | pass | healthy | enabled | done | succeeded)
            ny_color green "$w"
            ;;
        notready | failed | fail | error | offline | unsupported | down | issue | inactive | missing | unhealthy)
            ny_color red "$w"
            ;;
        warn | warning | pending | best-effort | degraded | unknown | skipped | partial | maintenance)
            ny_color yellow "$w"
            ;;
        *) printf '%s' "$w" ;;
    esac
    return 0
}

# ny_table [--status COL]... < TSV -- render tab-separated rows (first row is
# the header) as an aligned table that fits the terminal. Columns named with
# --status are coloured by ny_status_color.
ny_table() {
    local -a status_cols=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --status) status_cols+=("$2"); shift 2 ;;
            *) shift ;;
        esac
    done
    local -a rows=()
    mapfile -t rows
    [[ "${#rows[@]}" -gt 0 ]] || return 0
    # Tables go to stdout: no colour codes when stdout is a pipe or file.
    if [[ ! -t 1 ]]; then
        local NY_C_RESET="" NY_C_BOLD="" NY_C_DIM="" NY_C_RED="" NY_C_GREEN="" NY_C_YELLOW="" NY_C_BLUE="" NY_C_CYAN=""
    fi

    local -a header=() widths=()
    IFS=$'\t' read -r -a header <<<"${rows[0]}"
    local ncol=${#header[@]} r c cell
    for ((c = 0; c < ncol; c++)); do widths[c]=${#header[c]}; done
    local -a cells=()
    for r in "${rows[@]:1}"; do
        IFS=$'\t' read -r -a cells <<<"$r"
        for ((c = 0; c < ncol; c++)); do
            cell="$(ny_strip_ansi "${cells[c]:-}")"
            ((${#cell} > widths[c])) && widths[c]=${#cell}
        done
    done

    # Shrink the widest columns until the table fits the terminal.
    local avail total
    avail="$(ny_term_width)"
    while true; do
        total=0
        for ((c = 0; c < ncol; c++)); do total=$((total + widths[c] + 2)); done
        ((total <= avail)) && break
        # Never shrink the first column: it usually holds names/IDs people copy.
        ((ncol > 1)) || break
        local widest=1
        for ((c = 2; c < ncol; c++)); do ((widths[c] > widths[widest])) && widest=$c; done
        ((widths[widest] <= 6)) && break
        widths[widest]=$((widths[widest] - 1))
    done

    local ell="~"
    [[ "$NY_UTF8" -eq 1 ]] && ell="…"
    local first=1 line plain pad is_status
    for r in "${rows[@]}"; do
        IFS=$'\t' read -r -a cells <<<"$r"
        line=""
        for ((c = 0; c < ncol; c++)); do
            cell="${cells[c]:-}"
            plain="$(ny_strip_ansi "$cell")"
            if ((${#plain} > widths[c])); then
                plain="${plain:0:widths[c]-1}${ell}"
                cell="$plain"
            fi
            is_status=0
            ny_in_list "${header[c]}" "${status_cols[@]+"${status_cols[@]}"}" && is_status=1
            pad=$((widths[c] - ${#plain}))
            if [[ "$first" -eq 1 ]]; then
                cell="$(ny_color bold "$cell")"
            elif [[ "$is_status" -eq 1 && "$cell" == "$plain" ]]; then
                cell="$(ny_status_color "$cell")"
            fi
            line+="${cell}$(printf '%*s' "$pad" '')"
            ((c < ncol - 1)) && line+="  "
        done
        printf '%s\n' "${line%"${line##*[![:space:]]}"}"
        first=0
    done
    return 0
}
