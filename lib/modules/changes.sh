# shellcheck shell=bash
# Commands for the change journal: list what nodeyard changed, and undo it.

ny_cmd "changes" changes_list_cmd "Settings" "List the system changes nodeyard has made (newest last)" changes json
ny_cmd "changes show" changes_show_cmd "Settings" "Show every file and command one change touched" changes json
ny_cmd "undo" changes_undo_cmd "Settings" "Undo a change (or every change a feature made)" changes

changes_list_cmd_help() {
    cat <<'HELP'
Usage: nodeyard changes [--feature NAME] [--all] [--json]

Lists the changes nodeyard made to this machine, one line per command run.
Each can be reverted with 'nodeyard undo ID'. Already-undone changes are
hidden unless --all is given.
HELP
}

changes_list_cmd() {
    ny_need_root
    local feature="" all=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --feature) ny_need_value "$1" $#; feature="$2"; shift 2 ;;
            --all) all=1; shift ;;
            *) ny_usage_error "Unknown option for 'changes': $1" ;;
        esac
    done
    ny_deps_ensure "the change journal" jq
    local -a rows=()
    mapfile -t rows < <(ny_journal_txns --feature "$feature")
    if [[ "$all" -eq 0 ]]; then
        local -a keep=()
        local r
        for r in "${rows[@]+"${rows[@]}"}"; do
            [[ "$(jq -r '.undone' <<<"$r")" == true ]] || keep+=("$r")
        done
        rows=("${keep[@]+"${keep[@]}"}")
    fi
    if [[ "$NY_JSON" -eq 1 ]]; then
        ny_json_out "$(ny_json_obj ok:=true "changes:=$(ny_json_arr "${rows[@]+"${rows[@]}"}")")"
        return 0
    fi
    if [[ "${#rows[@]}" -eq 0 ]]; then
        ny_info "No changes recorded${feature:+ for ${feature}}."
        return 0
    fi
    {
        printf 'ID\tWHEN\tCOMMAND\tFEATURE\tFILES\tSTATE\n'
        printf '%s\n' "${rows[@]}" | jq -r '[.txn, (.ts | sub("T"; " ") | .[0:16]), ("nodeyard " + .cmd), (.features | join(",")), (.paths | length | tostring), (if .undone then "undone" else "active" end)] | @tsv'
    } | ny_table --status STATE
    printf '\n%s\n' "$(ny_color dim "Details: nodeyard changes show ID    Undo: sudo nodeyard undo ID")"
}

changes_show_cmd() {
    ny_need_root
    local txn="${1:-}"
    [[ -n "$txn" ]] || ny_usage_error "Say which change to show." "nodeyard changes show ID"
    ny_deps_ensure "the change journal" jq
    local -a lines=()
    mapfile -t lines < <(ny_journal_entries --txn "$txn")
    [[ "${#lines[@]}" -gt 0 ]] || ny_die "No change with id ${txn} (or it was already undone)." "List changes with: nodeyard changes --all" "$NY_E_USAGE"
    if [[ "$NY_JSON" -eq 1 ]]; then
        ny_json_out "$(ny_json_obj ok:=true "entries:=$(ny_json_arr "${lines[@]}")")"
        return 0
    fi
    printf '%s\n' "${lines[@]}" | jq -r '"  " + .op + "\t" + (.path // (.command // [] | join(" ")))' | column -t -s $'\t' 2>/dev/null || printf '%s\n' "${lines[@]}"
}

changes_undo_cmd_help() {
    cat <<'HELP'
Usage: nodeyard undo ID | --last | --feature NAME  [--force] [--dry-run]

Reverts changes nodeyard made: restores backed-up files, removes files it
created, and reverses commands such as enabling a service.

  ID              A change id from 'nodeyard changes'
  --last          The most recent change
  --feature NAME  Every change made by one feature (e.g. k3s, doctor, firewall)
  --force         Restore files even if something else edited them since
HELP
}

changes_undo_cmd() {
    ny_need_root
    local txn="" last=0 feature="" force=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --last) last=1; shift ;;
            --feature) ny_need_value "$1" $#; feature="$2"; shift 2 ;;
            --force) force=1; shift ;;
            -*) ny_usage_error "Unknown option for 'undo': $1" ;;
            *) txn="$1"; shift ;;
        esac
    done
    ny_deps_ensure "the change journal" jq
    if [[ "$last" -eq 1 ]]; then
        txn="$(ny_journal_txns | jq -r 'select(.undone | not) | select(.cmd != "undo") | .txn' | tail -n1)"
        [[ -n "$txn" ]] || {
            ny_info "There is nothing to undo."
            return 0
        }
    fi
    [[ -n "$txn" || -n "$feature" ]] || ny_usage_error "Say what to undo: an ID, --last or --feature NAME." "nodeyard undo ID|--last|--feature NAME"
    local what="change ${txn}"
    [[ -n "$feature" ]] && what="every change made by '${feature}'"
    if [[ -n "$txn" ]]; then
        local desc
        desc="$(ny_journal_txns | jq -r --arg t "$txn" 'select(.txn == $t) | "nodeyard " + .cmd + " (" + (.paths | join(", ")) + ")"')"
        [[ -n "$desc" ]] || ny_die "No change with id ${txn}." "List changes with: nodeyard changes" "$NY_E_USAGE"
        ny_info "Undo: ${desc}"
    fi
    ny_confirm "Undo ${what}?" y || return 0
    local rc=0
    if [[ -n "$feature" ]]; then
        ny_journal_undo_feature "$feature" "$force" || rc=$?
    else
        ny_journal_undo_txn "$txn" "$force" || rc=$?
    fi
    if [[ "$rc" -eq 0 ]]; then
        ny_ok "Undone: ${what}."
    else
        ny_die "Some changes could not be undone (see above)." "Check those files by hand, or re-run with --force to restore the backups anyway."
    fi
}
