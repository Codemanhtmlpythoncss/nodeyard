# shellcheck shell=bash
# shellcheck disable=SC2034 # globals here are read by other files
# The only ways nodeyard changes a system: running a command, writing or
# removing a file, creating a directory. Each one honours --dry-run (prints
# and records the step instead of doing it), never runs real commands in demo
# mode, and records file changes in the journal so they can be undone.

# Steps collected during --dry-run (JSON objects), printed by ny_plan_print.
NY_PLAN=()

# ny_plan_add TYPE SUMMARY [FIELD...] -- record one planned step.
ny_plan_add() {
    local type="$1" summary="$2"
    shift 2
    NY_PLAN+=("$(ny_json_obj "type=$type" "summary=$(ny_redact "$summary")" "$@")")
}

ny_plan_json() {
    ny_json_arr "${NY_PLAN[@]+"${NY_PLAN[@]}"}"
}

# ny_simulating -- true when changes must not touch the real system.
ny_simulating() {
    [[ "$NY_DRY_RUN" -eq 1 || "$NY_DEMO" -eq 1 ]]
}

# ny_json_cmd_redacted ARG... -- a command as a JSON array with secrets redacted.
ny_json_cmd_redacted() {
    local -a redacted=()
    local a
    for a in "$@"; do
        redacted+=("$(ny_redact "$a")")
    done
    ny_json_arr_str "${redacted[@]+"${redacted[@]}"}"
}

ny_quote_cmd() {
    local out="" a
    for a in "$@"; do
        if [[ "$a" =~ ^[A-Za-z0-9_./:=@%+,-]+$ ]]; then
            out+="${out:+ }$a"
        elif [[ "$a" != *"'"* ]]; then
            # Single quotes read better than %q's backslashes.
            out+="${out:+ }'${a}'"
        else
            out+="${out:+ }$(printf '%q' "$a")"
        fi
    done
    printf '%s' "$out"
}

# ny_run CMD... -- run a state-changing command.
ny_run() {
    local shown
    shown="$(ny_quote_cmd "$@")"
    if ny_simulating; then
        ny_plan_add run "Run: ${shown}" "command:=$(ny_json_cmd_redacted "$@")"
        if [[ "$NY_DRY_RUN" -eq 1 ]]; then
            printf '%s %s\n' "$(ny_color cyan "[dry-run] would run:")" "$(ny_redact "$shown")" >&2
        else
            ny_vlog "[demo] simulated: ${shown}"
        fi
        return 0
    fi
    ny_vlog "+ ${shown}"
    ny_log RUN "$shown"
    "$@"
}

# ny_run_undoable UNDO_CMD_WORDS -- CMD... -- run CMD and record UNDO for
# 'nodeyard undo'. Example: ny_run_undoable systemctl disable --now x -- systemctl enable --now x
ny_run_undoable() {
    local -a undo=()
    while [[ $# -gt 0 && "$1" != "--" ]]; do
        undo+=("$1")
        shift
    done
    [[ "${1:-}" == "--" ]] && shift
    ny_run "$@" || return $?
    ny_simulating && return 0
    ny_journal_append "$(ny_json_obj op=run "command:=$(ny_json_cmd_redacted "$@")" "undo:=$(ny_json_arr_str "${undo[@]+"${undo[@]}"}")")"
}

# ny_ensure_dir PATH [MODE] -- create a directory (recorded for undo).
ny_ensure_dir() {
    ny_journal_txn_init
    local path="$1" mode="${2:-0755}" real
    real="$(ny_path "$path")"
    [[ -d "$real" ]] && return 0
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        ny_plan_add mkdir "Create directory ${path}" "path=$path"
        printf '%s %s\n' "$(ny_color cyan "[dry-run] would create directory")" "$path" >&2
        return 0
    fi
    # Record each missing parent so undo removes exactly what we created.
    local -a created=()
    local p="$path"
    while [[ -n "$p" && "$p" != "/" && ! -d "$(ny_path "$p")" ]]; do
        created=("$p" "${created[@]+"${created[@]}"}")
        p="$(dirname -- "$p")"
    done
    mkdir -p -- "$real"
    chmod "$mode" "$real"
    for p in "${created[@]+"${created[@]}"}"; do
        ny_journal_append "$(ny_json_obj op=mkdir "path=$p")"
    done
    return 0
}

# ny_write_file PATH [MODE] [OWNER] < CONTENT -- write a system file atomically.
# Idempotent: an identical file is left untouched. Secret files (mode 0600 or
# 0400) never have their contents shown in --dry-run output.
ny_write_file() {
    ny_journal_txn_init
    local path="$1" mode="${2:-0644}" owner="${3:-}"
    local real tmp
    real="$(ny_path "$path")"
    tmp="$(ny_mktemp)"
    cat >"$tmp"

    if [[ -f "$real" ]] && cmp -s -- "$tmp" "$real"; then
        local cur_mode
        cur_mode="$(stat -c '%a' -- "$real" 2>/dev/null || stat -f '%Lp' -- "$real" 2>/dev/null || echo "")"
        if [[ -z "$cur_mode" || "$((8#${cur_mode}))" -eq "$((8#${mode#0}))" ]]; then
            ny_vlog "unchanged: ${path}"
            return 0
        fi
        # Same content, different permissions: only the mode changes.
        if [[ "$NY_DRY_RUN" -eq 1 ]]; then
            ny_plan_add chmod "Set permissions of ${path} to ${mode}" "path=$path" "mode=$mode"
            printf '%s %s %s\n' "$(ny_color cyan "[dry-run] would set permissions of")" "$path" "to ${mode}" >&2
            return 0
        fi
        ny_run_undoable chmod "$cur_mode" "$real" -- chmod "$mode" "$real"
        return 0
    fi

    local secret=0
    [[ "$mode" == "0600" || "$mode" == "600" || "$mode" == "0400" || "$mode" == "400" ]] && secret=1

    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        local verb="create"
        [[ -e "$real" ]] && verb="update"
        ny_plan_add write "${verb^} ${path}" "path=$path" "mode=$mode" "secret:=$(ny_json_bool "$secret")"
        printf '%s %s %s\n' "$(ny_color cyan "[dry-run] would ${verb}")" "$path" "(mode ${mode})" >&2
        if [[ "$secret" -eq 1 ]]; then
            printf '    %s\n' "$(ny_color dim "(contents hidden: this file holds secrets)")" >&2
        elif have diff && [[ "$NY_JSON" -ne 1 ]]; then
            local old="/dev/null"
            [[ -f "$real" ]] && old="$real"
            diff -u --label "$path (current)" --label "$path (new)" -- "$old" "$tmp" 2>/dev/null |
                head -n 60 | sed 's/^/    /' >&2 || true
        fi
        return 0
    fi

    ny_ensure_dir "$(dirname -- "$path")"

    local existed=false backup="" sha_before=""
    if [[ -e "$real" ]]; then
        existed=true
        backup="$(ny_journal_backup "$path")"
        sha_before="$(ny_sha256 "$real")"
    fi

    local staged="${real}.nodeyard-new.$$"
    cp -- "$tmp" "$staged"
    chmod "$mode" "$staged"
    if [[ -n "$owner" ]] && ! ny_simulating; then
        chown "$owner" -- "$staged" 2>/dev/null || ny_warn "Could not set owner ${owner} on ${path}"
    fi
    mv -f -- "$staged" "$real"

    ny_journal_append "$(ny_json_obj op=write "path=$path" "existed:=$existed" "backup?=$backup" "mode=$mode" "sha_before?=$sha_before" "sha_after=$(ny_sha256 "$real")")"
    ny_log WRITE "$path (mode $mode)"
}

# ny_remove_file PATH -- remove a system file (backed up for undo).
ny_remove_file() {
    ny_journal_txn_init
    local path="$1" real
    real="$(ny_path "$path")"
    [[ -e "$real" || -L "$real" ]] || return 0
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        ny_plan_add delete "Remove ${path}" "path=$path"
        printf '%s %s\n' "$(ny_color cyan "[dry-run] would remove")" "$path" >&2
        return 0
    fi
    local backup=""
    [[ -f "$real" ]] && backup="$(ny_journal_backup "$path")"
    rm -f -- "$real"
    ny_journal_append "$(ny_json_obj op=delete "path=$path" existed:=true "backup?=$backup")"
    ny_log DELETE "$path"
}

# ny_symlink TARGET LINK -- create or update a symlink (recorded for undo).
ny_symlink() {
    ny_journal_txn_init
    local target="$1" link="$2" real
    real="$(ny_path "$link")"
    if [[ -L "$real" && "$(readlink -- "$real")" == "$target" ]]; then
        return 0
    fi
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        ny_plan_add symlink "Link ${link} -> ${target}" "path=$link" "target=$target"
        printf '%s %s -> %s\n' "$(ny_color cyan "[dry-run] would link")" "$link" "$target" >&2
        return 0
    fi
    ny_ensure_dir "$(dirname -- "$link")"
    local existed=false backup=""
    if [[ -e "$real" && ! -L "$real" ]]; then
        existed=true
        backup="$(ny_journal_backup "$link")"
    fi
    ln -sfn -- "$target" "$real"
    ny_journal_append "$(ny_json_obj op=write "path=$link" "existed:=$existed" "backup?=$backup" "symlink=$target")"
}

# --- services ----------------------------------------------------------------

ny_systemd_available() {
    [[ "$NY_DEMO" -eq 1 ]] && return 0
    [[ -d "$(ny_path /run/systemd/system)" ]]
}

ny_service_daemon_reload() {
    ny_systemd_available || return 0
    ny_run systemctl daemon-reload
}

# ny_service_enable UNIT [--now] -- enable a unit; undo disables it again
# (only if it was not already enabled).
ny_service_enable() {
    local unit="$1" now="${2:-}"
    local -a flags=()
    [[ "$now" == "--now" ]] && flags=(--now)
    if ! ny_simulating && systemctl is-enabled --quiet "$unit" 2>/dev/null; then
        [[ "$now" == "--now" ]] && ny_run systemctl start "$unit"
        return 0
    fi
    ny_run_undoable systemctl disable "${flags[@]+"${flags[@]}"}" "$unit" -- systemctl enable "${flags[@]+"${flags[@]}"}" "$unit"
}

ny_service_disable() {
    local unit="$1" now="${2:-}"
    local -a flags=()
    [[ "$now" == "--now" ]] && flags=(--now)
    if ! ny_simulating && ! systemctl is-enabled --quiet "$unit" 2>/dev/null; then
        [[ "$now" == "--now" ]] && ny_run systemctl stop "$unit" 2>/dev/null
        return 0
    fi
    ny_run_undoable systemctl enable "${flags[@]+"${flags[@]}"}" "$unit" -- systemctl disable "${flags[@]+"${flags[@]}"}" "$unit"
}
