# shellcheck shell=bash
# Guided wizards. A wizard is a JSON spec (share/nodeyard/wizards/ID.json)
# listing steps; each answer becomes a flag of ONE nodeyard command. The
# summary shows that command's own --dry-run plan, and applying runs the
# command itself, so a wizard can never do anything the command line can't.
# The dashboard renders the same specs.
#
# Spec:
#   { "id", "title", "intro", "command": ["install","master"], "success",
#     "steps": [ { "id", "type": choose|input|secret|yesno|info, "label",
#       "help", "default", "options": [{"value","label"}],
#       "options_from": {"command": [...], "query", "value", "label"},
#       "default_from": {"command": [...], "query"},
#       "validate": TYPE, "flag": "--interface",
#       "flag_mode": value|bool|bool-no|positional|none,
#       "when": {"step": ID, "equals"|"not_equals": V} | {"step", "in": [...]} } ] }

ny_wizard_file() {
    local id="$1"
    [[ "$id" =~ ^[a-z0-9-]+$ ]] || ny_die "Invalid wizard name '${id}'." "" "$NY_E_USAGE"
    printf '%s/wizards/%s.json\n' "$NY_SHARE" "$id"
}

# ny_nodeyard ARGS... -- run nodeyard itself as a separate process.
ny_nodeyard() {
    local -a flags=()
    [[ "$NY_DEMO" -eq 1 ]] && flags+=(--demo)
    NODEYARD_COLOR=never "${NY_HOME}/bin/nodeyard" "${flags[@]+"${flags[@]}"}" "$@"
}

# ny_wizard_when SPEC_JSON STEP_INDEX -- is this step shown given the answers?
ny_wizard_when() {
    local spec="$1" i="$2" cond dep want
    cond="$(jq -c ".steps[$i].when // empty" <<<"$spec")"
    [[ -n "$cond" ]] || return 0
    dep="$(jq -r '.step' <<<"$cond")"
    local have="${NY_WIZ_ANS[$dep]-}"
    if jq -e 'has("equals")' <<<"$cond" >/dev/null; then
        want="$(jq -r '.equals | tostring' <<<"$cond")"
        [[ "$have" == "$want" ]]
    elif jq -e 'has("not_equals")' <<<"$cond" >/dev/null; then
        want="$(jq -r '.not_equals | tostring' <<<"$cond")"
        [[ "$have" != "$want" ]]
    elif jq -e 'has("in")' <<<"$cond" >/dev/null; then
        jq -e --arg v "$have" '.in | map(tostring) | index($v) != null' <<<"$cond" >/dev/null
    else
        return 0
    fi
}

# ny_wizard_options SPEC STEP -- "value<TAB>label" lines for a choose step.
ny_wizard_options() {
    local spec="$1" i="$2" from
    jq -r ".steps[$i].options // [] | .[] | [(.value|tostring), (.label // .value | tostring)] | @tsv" <<<"$spec"
    from="$(jq -c ".steps[$i].options_from // empty" <<<"$spec")"
    [[ -n "$from" ]] || return 0
    local -a cmd=()
    mapfile -t cmd < <(jq -r '.command[]' <<<"$from")
    local query value label out
    query="$(jq -r '.query // ".[]"' <<<"$from")"
    value="$(jq -r '.value // ".value"' <<<"$from")"
    label="$(jq -r '.label // .value // ".label"' <<<"$from")"
    out="$(ny_nodeyard "${cmd[@]}" --json 2>/dev/null || true)"
    [[ -n "$out" ]] || return 0
    jq -r "(${query}) | [((${value})|tostring), ((${label})|tostring)] | @tsv" <<<"$out" 2>/dev/null || true
}

ny_wizard_default() {
    local spec="$1" i="$2" d from
    d="$(jq -r ".steps[$i].default // empty | tostring" <<<"$spec")"
    from="$(jq -c ".steps[$i].default_from // empty" <<<"$spec")"
    if [[ -n "$from" ]]; then
        local -a cmd=()
        mapfile -t cmd < <(jq -r '.command[]' <<<"$from")
        local query out v
        query="$(jq -r '.query // "."' <<<"$from")"
        out="$(ny_nodeyard "${cmd[@]}" --json 2>/dev/null || true)"
        v="$(jq -r "(${query}) // empty | tostring" <<<"$out" 2>/dev/null | head -n1 || true)"
        [[ -n "$v" ]] && d="$v"
    fi
    printf '%s\n' "$d"
}

# ny_wizard_build_args SPEC -- prints the command words + flags, one per line.
ny_wizard_build_args() {
    local spec="$1" n i id flag mode v
    jq -r '.command[]' <<<"$spec"
    n="$(jq '.steps | length' <<<"$spec")"
    for ((i = 0; i < n; i++)); do
        ny_wizard_when "$spec" "$i" || continue
        id="$(jq -r ".steps[$i].id" <<<"$spec")"
        flag="$(jq -r ".steps[$i].flag // empty" <<<"$spec")"
        mode="$(jq -r ".steps[$i].flag_mode // \"value\"" <<<"$spec")"
        [[ -n "${NY_WIZ_ANS[$id]+x}" ]] || continue
        v="${NY_WIZ_ANS[$id]}"
        case "$mode" in
            none) ;;
            stdin) [[ -n "$v" ]] && printf '%s\n' "$flag" ;;
            bool) [[ "$v" == yes ]] && printf '%s\n' "$flag" ;;
            bool-no) [[ "$v" == no ]] && printf '%s\n' "$flag" ;;
            positional) [[ -n "$v" ]] && printf '%s\n' "$v" ;;
            *)
                if [[ -n "$v" && -n "$flag" ]]; then
                    printf '%s\n%s\n' "$flag" "$v"
                fi
                ;;
        esac
    done
}

# ny_wizard_stdin SPEC -- the answer of the (single) step passed on stdin.
ny_wizard_stdin() {
    local spec="$1" id
    id="$(jq -r '[.steps[] | select(.flag_mode == "stdin") | .id][0] // empty' <<<"$spec")"
    [[ -n "$id" ]] && printf '%s' "${NY_WIZ_ANS[$id]-}"
    return 0
}

# ny_wizard_exec STDIN ARGS... -- run nodeyard, feeding STDIN if non-empty.
ny_wizard_exec() {
    local input="$1"
    shift
    if [[ -n "$input" ]]; then
        printf '%s\n' "$input" | ny_nodeyard "$@"
    else
        ny_nodeyard "$@"
    fi
}

# ny_wizard_summary ARGS... -- show what the command would do (its dry run).
# Returns 1 if the dry run itself failed.
ny_wizard_summary() {
    local out err rc=0
    err="$(ny_mktemp)"
    out="$(ny_wizard_exec "${NY_WIZ_STDIN:-}" "$@" --dry-run --json 2>"$err")" || rc=$?
    printf '\n%s\n' "$(ny_color bold "Summary")" >&2
    printf '  %s %s\n' "$(ny_color dim "Command:")" "nodeyard $(ny_redact "$(ny_quote_cmd "$@")")" >&2
    local result
    result="$(grep -E '^\{' <<<"$out" | tail -n1 || true)"
    if [[ "$rc" -ne 0 ]]; then
        local msg fix
        msg="$(jq -r '.error.message // empty' <<<"$result" 2>/dev/null || true)"
        fix="$(jq -r '.error.fix // empty' <<<"$result" 2>/dev/null || true)"
        [[ -n "$msg" ]] || msg="$(tail -n 5 "$err")"
        ny_err "This can't be applied as it stands: ${msg}"
        [[ -n "$fix" ]] && ny_hint "Fix: ${fix}"
        return 1
    fi
    local count
    count="$(jq '.plan | length' <<<"$result" 2>/dev/null || echo 0)"
    if [[ "$count" -eq 0 ]]; then
        printf '  %s\n' "Nothing needs to change - this machine already matches." >&2
    else
        printf '  %s\n' "This will:" >&2
        jq -r '.plan[] | .summary' <<<"$result" | head -n 40 | while IFS= read -r line; do
            printf '    %s %s\n' "$(ny_color cyan "$NY_SYM_DOT")" "$line" >&2
        done
        ((count > 40)) && printf '    %s\n' "... and $((count - 40)) more steps" >&2
    fi
    return 0
}

# ny_wizard_run ID [STEP=VALUE]... -- run a wizard; prefilled answers skip
# their steps. Returns the command's exit code, or 130 if cancelled.
ny_wizard_run() {
    local id="$1"
    shift
    local file spec
    file="$(ny_wizard_file "$id")"
    [[ -r "$file" ]] || ny_die "Wizard '${id}' not found." "Reinstall nodeyard; ${file} is missing."
    ny_deps_ensure "guided wizards" jq
    spec="$(jq -c . "$file")" || ny_die "Wizard '${id}' is not valid JSON." "Reinstall nodeyard."

    declare -gA NY_WIZ_ANS=()
    local -A prefilled=()
    local kv
    for kv in "$@"; do
        NY_WIZ_ANS["${kv%%=*}"]="${kv#*=}"
        prefilled["${kv%%=*}"]=1
    done

    local title intro n
    title="$(jq -r '.title' <<<"$spec")"
    intro="$(jq -r '.intro // empty' <<<"$spec")"
    n="$(jq '.steps | length' <<<"$spec")"
    NY_UI_TITLE="$title"
    printf '\n%s\n' "$(ny_color bold "$title")" >&2
    [[ -n "$intro" ]] && printf '%s\n' "$intro" >&2

    local -a history=()
    local i=0 rc type sid label help default validate value shown=0
    NY_UI_BACK=1
    while true; do
        while ((i < n)); do
            sid="$(jq -r ".steps[$i].id" <<<"$spec")"
            if [[ -n "${prefilled[$sid]:-}" ]] || ! ny_wizard_when "$spec" "$i"; then
                i=$((i + 1))
                continue
            fi
            type="$(jq -r ".steps[$i].type" <<<"$spec")"
            label="$(jq -r ".steps[$i].label" <<<"$spec")"
            help="$(jq -r ".steps[$i].help // empty" <<<"$spec")"
            validate="$(jq -r ".steps[$i].validate // \"string\"" <<<"$spec")"
            default="${NY_WIZ_ANS[$sid]-$(ny_wizard_default "$spec" "$i")}"
            shown=$((${#history[@]} + 1))
            local step_label="${label}"
            [[ "$NY_UI" == plain || "$NY_UI" == none ]] && step_label="[${shown}] ${label}"
            rc=0
            case "$type" in
                choose)
                    local -a opts=()
                    mapfile -t opts < <(ny_wizard_options "$spec" "$i")
                    if [[ "${#opts[@]}" -eq 0 ]]; then
                        ny_warn "No choices are available for: ${label}"
                        value="$(ny_ui_input "$step_label" "$default" "$validate" "$help")" || rc=$?
                    else
                        [[ -n "$help" ]] && printf '%s\n' "$(ny_color dim "$help")" >&2
                        value="$(ny_ui_choose "$step_label" "$default" "${opts[@]}")" || rc=$?
                    fi
                    ;;
                input) value="$(ny_ui_input "$step_label" "$default" "$validate" "$help")" || rc=$? ;;
                secret) value="$(ny_ui_secret "$step_label")" || rc=$? ;;
                yesno)
                    local d="y"
                    [[ "$default" == no || "$default" == n || "$default" == false ]] && d="n"
                    [[ -n "$help" ]] && printf '%s\n' "$(ny_color dim "$help")" >&2
                    local yrc=0
                    ny_ui_yesno "$step_label" "$d" || yrc=$?
                    case "$yrc" in
                        0) value="yes" ;;
                        1) value="no" ;;
                        *) rc=2 ;;
                    esac
                    ;;
                info)
                    ny_ui_msg "$label" "$help"
                    value=""
                    ;;
                *) ny_die "Wizard '${id}' has a step of unknown type '${type}'." "Reinstall nodeyard." ;;
            esac
            case "$rc" in
                0)
                    NY_WIZ_ANS["$sid"]="$value"
                    history+=("$i")
                    i=$((i + 1))
                    ;;
                2)
                    if [[ "${#history[@]}" -gt 0 ]]; then
                        i="${history[${#history[@]} - 1]}"
                        unset 'history[${#history[@]}-1]'
                    fi
                    ;;
                *)
                    NY_UI_BACK=0
                    if ny_ui_yesno "Cancel this wizard? Nothing has been changed." y; then
                        ny_info "Cancelled. Nothing was changed."
                        return "$NY_E_CANCELLED"
                    fi
                    NY_UI_BACK=1
                    ;;
            esac
        done

        local -a args=()
        mapfile -t args < <(ny_wizard_build_args "$spec")
        NY_WIZ_STDIN="$(ny_wizard_stdin "$spec")"
        NY_UI_BACK=0
        local can_apply=1
        ny_wizard_summary "${args[@]}" || can_apply=0
        local -a choices=()
        [[ "$can_apply" -eq 1 ]] && choices+=($'apply\tApply these changes')
        choices+=($'back\tGo back and change answers' $'cancel\tCancel (nothing is changed)')
        local action
        action="$(ny_ui_choose "What next?" "$([[ $can_apply -eq 1 ]] && echo apply || echo back)" "${choices[@]}")" || action="cancel"
        case "$action" in
            apply)
                local crc=0
                ny_wizard_exec "$NY_WIZ_STDIN" "${args[@]}" --yes || crc=$?
                if [[ "$crc" -eq 0 ]]; then
                    local success
                    success="$(jq -r '.success // empty' <<<"$spec")"
                    ny_ok "${success:-Done.}"
                else
                    ny_err "The command failed (exit ${crc}). Anything already changed is listed by 'nodeyard changes' and can be undone with 'nodeyard undo --last'."
                fi
                ny_ui_pause
                return "$crc"
                ;;
            back)
                if [[ "${#history[@]}" -gt 0 ]]; then
                    i="${history[${#history[@]} - 1]}"
                    unset 'history[${#history[@]}-1]'
                else
                    i=0
                fi
                NY_UI_BACK=1
                ;;
            *)
                ny_info "Cancelled. Nothing was changed."
                return "$NY_E_CANCELLED"
                ;;
        esac
    done
}
