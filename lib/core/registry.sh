# shellcheck shell=bash
# shellcheck disable=SC2034 # globals here are read by other files
# Command registry and dispatcher. Every action is one registered command;
# the menu, wizards, dashboard and shell completion all go through here, so
# no interface can do something the command line can't.

declare -gA NY_CMD_FN=() NY_CMD_SUM=() NY_CMD_GROUP=() NY_CMD_FEATURE=() NY_CMD_ALIAS=() NY_CMD_HIDDEN=() NY_CMD_JSON=()
NY_CMD_ORDER=()
NY_GROUP_ORDER=("Start here" "Cluster" "Nodes" "AI" "Network" "Health" "Dashboard" "Backups" "Updates" "Settings" "Tool")
NY_HELP=0
NY_RESULT_PRINTED=0

# ny_cmd PATH FUNCTION GROUP SUMMARY [FEATURE] [FLAGS]
#   FLAGS (comma-separated): hidden = not listed in help;
#   json = the command prints its own --json output (others get a generic
#   {"ok":..,"dry_run":..,"plan":[..]} result and their text goes to stderr).
ny_cmd() {
    local path="$1" opts=",${6:-},"
    NY_CMD_FN["$path"]="$2"
    NY_CMD_GROUP["$path"]="$3"
    NY_CMD_SUM["$path"]="$4"
    NY_CMD_FEATURE["$path"]="${5:-${path%% *}}"
    [[ "$opts" == *,hidden,* ]] && NY_CMD_HIDDEN["$path"]=1
    [[ "$opts" == *,json,* ]] && NY_CMD_JSON["$path"]=1
    NY_CMD_ORDER+=("$path")
}

# ny_json_out JSON -- print a command's --json result (once).
ny_json_out() {
    printf '%s\n' "$1" >&"${NY_JSON_FD:-1}"
    NY_RESULT_PRINTED=1
}

# ny_json_result [FIELD...] -- the standard result object.
# shellcheck disable=SC2120 # extra fields are optional
ny_json_result() {
    ny_json_obj ok:=true "dry_run:=$(ny_json_bool "$NY_DRY_RUN")" "plan:=$(ny_plan_json)" "$@"
}

# ny_cmd_alias ALIAS_PATH TARGET_PATH
ny_cmd_alias() {
    NY_CMD_ALIAS["$1"]="$2"
}

# ny_cmd_resolve WORD... -- sets NY_RESOLVED_PATH and NY_RESOLVED_WORDS (how
# many words matched). Returns 1 if nothing matched.
ny_cmd_resolve() {
    local -a words=("$@")
    local n cand
    NY_RESOLVED_PATH=""
    NY_RESOLVED_WORDS=0
    for n in 3 2 1; do
        ((${#words[@]} >= n)) || continue
        local ok=1 i
        for ((i = 0; i < n; i++)); do
            [[ "${words[i]}" == -* || -z "${words[i]}" ]] && ok=0
        done
        ((ok)) || continue
        cand="$(ny_join ' ' "${words[@]:0:n}")"
        [[ -n "${NY_CMD_ALIAS[$cand]:-}" ]] && cand="${NY_CMD_ALIAS[$cand]}"
        if [[ -n "${NY_CMD_FN[$cand]:-}" ]]; then
            NY_RESOLVED_PATH="$cand"
            NY_RESOLVED_WORDS=$n
            return 0
        fi
    done
    return 1
}

# ny_cmd_children PREFIX -- registered commands under PREFIX ("" for all).
ny_cmd_children() {
    local prefix="$1" p
    for p in "${NY_CMD_ORDER[@]}"; do
        [[ -n "${NY_CMD_HIDDEN[$p]:-}" ]] && continue
        if [[ -z "$prefix" || "$p" == "$prefix "* ]]; then
            printf '%s\n' "$p"
        fi
    done
    return 0
}

ny_help_command() {
    local path="$1" fn="${NY_CMD_FN[$1]}"
    if declare -F "${fn}_help" >/dev/null; then
        "${fn}_help"
    else
        printf 'Usage: nodeyard %s [options]\n\n%s\n' "$path" "${NY_CMD_SUM[$path]}"
    fi
    printf '\nGlobal options: --yes  --dry-run  --json  --verbose  --no-color  --demo  --help\n'
}

ny_help_group() {
    local prefix="$1" p
    printf 'Commands under "nodeyard %s":\n\n' "$prefix"
    while IFS= read -r p; do
        printf '  %-26s %s\n' "$p" "${NY_CMD_SUM[$p]}"
    done < <(ny_cmd_children "$prefix")
    printf '\nRun "nodeyard %s <command> --help" for details.\n' "$prefix"
}

ny_help_main() {
    printf '%s %s - set up and run a homelab cluster of Linux machines.\n\n' "$(ny_color bold nodeyard)" "$NY_VERSION"
    printf 'Usage: nodeyard [command] [options]\n'
    printf 'Run nodeyard with no command for the interactive menu.\n'
    local g p
    for g in "${NY_GROUP_ORDER[@]}"; do
        local -a in_group=()
        for p in "${NY_CMD_ORDER[@]}"; do
            [[ -n "${NY_CMD_HIDDEN[$p]:-}" ]] && continue
            [[ "${NY_CMD_GROUP[$p]}" == "$g" ]] && in_group+=("$p")
        done
        [[ "${#in_group[@]}" -gt 0 ]] || continue
        printf '\n%s\n' "$(ny_color bold "$g")"
        for p in "${in_group[@]}"; do
            printf '  %-26s %s\n' "$p" "${NY_CMD_SUM[$p]}"
        done
    done
    printf '\n%s\n' "$(ny_color bold "Global options (anywhere on the line)")"
    printf '  %-26s %s\n' "--yes, -y" "Answer yes to confirmations (non-interactive)"
    printf '  %-26s %s\n' "--dry-run" "Show what would change without changing anything"
    printf '  %-26s %s\n' "--json" "Machine-readable output where a command reports status"
    printf '  %-26s %s\n' "--verbose, -v" "Show extra detail"
    printf '  %-26s %s\n' "--no-color" "Plain output (NO_COLOR is also respected)"
    printf '  %-26s %s\n' "--demo" "Run against simulated nodes; changes nothing real"
    printf '  %-26s %s\n' "--help, -h" "Help for any command"
    printf '\nDocs: https://github.com/%s/tree/main/docs\n' "$NY_REPO"
}

# ny_cmd_suggest WORD -- commands that look like WORD.
ny_cmd_suggest() {
    local w="$1" p
    for p in "${NY_CMD_ORDER[@]}"; do
        [[ -n "${NY_CMD_HIDDEN[$p]:-}" ]] && continue
        if [[ "$p" == "$w"* || "$p" == *" $w"* || "$w" == "${p%% *}"* ]]; then
            printf '%s\n' "$p"
        fi
    done | head -n 5
    return 0
}

# ny_dispatch WORD... -- run the matching command.
ny_dispatch() {
    if [[ $# -eq 0 ]]; then
        if [[ "$NY_HELP" -eq 1 ]] || ! ny_ui_interactive; then
            ny_help_main
            return 0
        fi
        set -- menu
    fi

    if ny_cmd_resolve "$@"; then
        local path="$NY_RESOLVED_PATH"
        shift "$NY_RESOLVED_WORDS"
        NY_CMD_PATH="$path"
        NY_FEATURE="${NY_CMD_FEATURE[$path]}"
        if [[ "$NY_HELP" -eq 1 ]]; then
            ny_help_command "$path"
            return 0
        fi
        ny_log START "nodeyard ${path} $(ny_quote_cmd "$@")"
        if [[ "$NY_JSON" -eq 1 && -z "${NY_CMD_JSON[$path]:-}" ]]; then
            # Keep stdout pure JSON: the command's text goes to stderr and a
            # standard result object is printed afterwards.
            exec 3>&1 1>&2
            NY_JSON_FD=3
            "${NY_CMD_FN[$path]}" "$@"
            exec 1>&3 3>&-
            NY_JSON_FD=1
            [[ "$NY_RESULT_PRINTED" -eq 1 ]] || ny_json_out "$(ny_json_result)"
            return 0
        fi
        "${NY_CMD_FN[$path]}" "$@"
        local rc=$?
        if [[ "$NY_DRY_RUN" -eq 1 && "$NY_JSON" -eq 0 && "${#NY_PLAN[@]}" -gt 0 ]]; then
            printf '\n%s\n' "$(ny_color cyan "Dry run: nothing was changed. Run the same command without --dry-run to apply ${#NY_PLAN[@]} step(s).")" >&2
        fi
        if [[ "$NY_JSON" -eq 1 && "$NY_RESULT_PRINTED" -eq 0 && "$rc" -eq 0 ]]; then
            ny_json_out "$(ny_json_result)"
        fi
        return "$rc"
    fi

    # A group name on its own ("nodeyard ai") lists its commands.
    local first="$1"
    if [[ -n "$(ny_cmd_children "$first")" ]]; then
        if [[ $# -eq 1 || "$NY_HELP" -eq 1 ]]; then
            ny_help_group "$first"
            return 0
        fi
        NY_CMD_PATH="$first"
        ny_usage_error "Unknown command: nodeyard $(ny_join ' ' "$@")" "nodeyard ${first} <$(ny_cmd_children "$first" | awk '{print $2}' | sort -u | paste -sd '|' -)>"
    fi

    local suggestions
    suggestions="$(ny_cmd_suggest "$first")"
    ny_die "Unknown command: ${first}" \
        "${suggestions:+Did you mean: $(paste -sd ',' - <<<"$suggestions" | sed 's/,/, /g')? }Run 'nodeyard help' to list every command." "$NY_E_USAGE"
}

# ny_complete WORD... -- [--] CURRENT: completion candidates (used by the
# bash/zsh completion scripts; must stay fast and side-effect free).
ny_complete() {
    local -a words=()
    while [[ $# -gt 0 && "$1" != "--" ]]; do
        [[ "$1" == -* ]] || words+=("$1")
        shift
    done
    [[ "${1:-}" == "--" ]] && shift
    local cur="${1:-}"
    local -a cands=()
    local prefix
    prefix="$(ny_join ' ' "${words[@]+"${words[@]}"}")"

    if [[ "$cur" == -* ]]; then
        if ny_cmd_resolve "${words[@]+"${words[@]}"}"; then
            mapfile -t cands < <(ny_help_command "$NY_RESOLVED_PATH" 2>/dev/null | grep -oE -- '--[a-z0-9][a-z0-9-]*' | sort -u)
        else
            cands=(--yes --dry-run --json --verbose --no-color --demo --help)
        fi
    else
        local p next
        for p in "${NY_CMD_ORDER[@]}" "${!NY_CMD_ALIAS[@]}"; do
            [[ -n "${NY_CMD_HIDDEN[$p]:-}" ]] && continue
            if [[ -z "$prefix" ]]; then
                next="${p%% *}"
            elif [[ "$p" == "$prefix "* ]]; then
                next="${p#"$prefix "}"
                next="${next%% *}"
            else
                continue
            fi
            ny_in_list "$next" "${cands[@]+"${cands[@]}"}" || cands+=("$next")
        done
    fi
    local c
    for c in "${cands[@]+"${cands[@]}"}"; do
        [[ "$c" == "$cur"* ]] && printf '%s\n' "$c"
    done
    return 0
}

# ny_main ARGS... -- parse global flags, set up, dispatch.
ny_main() {
    local -a args=()
    local passthrough=0 arg
    if [[ "${1:-}" == "--version" || "${1:-}" == "-V" ]]; then
        printf 'nodeyard %s\n' "$NY_VERSION"
        return 0
    fi
    if [[ "${1:-}" == "__complete" ]]; then
        shift
        ny_complete "$@"
        return 0
    fi
    for arg in "$@"; do
        if [[ "$passthrough" -eq 1 ]]; then
            args+=("$arg")
            continue
        fi
        case "$arg" in
            --)
                passthrough=1
                args+=("$arg")
                ;;
            # Only as the first word: 'install master --version V' is a k3s version.
            --version | -V)
                if [[ "${#args[@]}" -eq 0 ]]; then
                    printf 'nodeyard %s\n' "$NY_VERSION"
                    return 0
                fi
                args+=("$arg")
                ;;
            --yes | -y) NY_YES=1 ;;
            --dry-run) NY_DRY_RUN=1 ;;
            --verbose | -v | --debug) NY_VERBOSE=1 ;;
            --json) NY_JSON=1 ;;
            --no-color | --no-colour) NY_COLOR_MODE="never" ;;
            --demo) NY_DEMO=1 ;;
            --help | -h) NY_HELP=1 ;;
            --ui=*) export NODEYARD_UI="${arg#*=}" ;;
            *) args+=("$arg") ;;
        esac
    done
    set -- "${args[@]+"${args[@]}"}"

    if [[ "$NY_DEMO" -eq 1 ]]; then
        ny_demo_setup
    fi
    ny_term_init
    trap ny_on_err ERR
    trap ny_on_exit EXIT

    # A broken config must not block the commands that repair it, so parse
    # leniently here; commands that depend on it validate it themselves.
    NY_CFG_BROKEN=0
    if [[ -r "$NY_CONFIG" ]]; then
        if ny_cfg_parse "$NY_CONFIG"; then
            NY_CFG_LOADED_FROM="$NY_CONFIG"
        else
            NY_CFG_BROKEN=1
            NY_CFG_LOADED_FROM="$NY_CONFIG"
        fi
    fi
    local c
    c="$(ny_cfg_get ui "" color "")"
    if [[ -n "$c" && "$NY_COLOR_MODE" == auto ]]; then
        NY_COLOR_MODE="$c"
        ny_term_init
    fi

    ny_dispatch "$@"
}
