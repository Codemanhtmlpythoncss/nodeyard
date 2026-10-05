# shellcheck shell=bash
# Change journal: every system file nodeyard modifies is backed up first and
# recorded, so a single command, a feature, or everything can be undone.
#
# Layout under ${NY_STATE}/journal (root-only, 0700):
#   journal.jsonl            one JSON object per change, oldest first
#   files/<txn>/<abs path>   the file as it was before the change
#
# A "transaction" (txn) is one nodeyard command invocation.

NY_TXN=""

ny_journal_dir() {
    printf '%s/journal\n' "$NY_STATE"
}

# ny_journal_txn_init -- create the current transaction id. Must run in the
# main shell (not inside $(...)), so every change shares one id.
ny_journal_txn_init() {
    [[ -n "$NY_TXN" ]] && return 0
    local slug="${NY_CMD_PATH// /-}"
    slug="${slug//[^a-zA-Z0-9-]/}"
    NY_TXN="$(printf '%(%Y%m%d-%H%M%S)T' -1)-$$${slug:+-$slug}"
}

# ny_journal_txn -- print the current transaction id.
ny_journal_txn() {
    ny_journal_txn_init
    printf '%s\n' "$NY_TXN"
}

ny_journal_ensure() {
    local dir
    dir="$(ny_journal_dir)"
    if [[ ! -d "$dir" ]]; then
        mkdir -p -- "$dir/files"
        chmod 0700 "$dir"
    fi
    return 0
}

# ny_journal_append JSON -- add one entry (fields: op, path, ...).
ny_journal_append() {
    ny_journal_txn_init
    ny_journal_ensure
    local entry="$1"
    # Prepend the common fields to the caller's object.
    local common
    common="$(ny_json_obj "txn=$(ny_journal_txn)" "ts=$(ny_now)" "cmd=${NY_CMD_PATH}" "feature=${NY_FEATURE}")"
    printf '%s,%s\n' "${common%\}}" "${entry#\{}" >>"$(ny_journal_dir)/journal.jsonl"
}

# ny_journal_backup PATH -- copy an existing system file into the journal;
# prints the backup's path relative to the journal dir.
ny_journal_backup() {
    local path="$1" real rel
    real="$(ny_path "$path")"
    ny_journal_ensure
    rel="files/$(ny_journal_txn)${path}"
    local dest
    dest="$(ny_journal_dir)/${rel}"
    if [[ ! -e "$dest" ]]; then
        mkdir -p -- "$(dirname -- "$dest")"
        cp -p -- "$real" "$dest"
    fi
    printf '%s\n' "$rel"
}

# ny_journal_entries [--txn TXN] [--feature F] -- matching entries (JSON lines),
# skipping transactions that were already undone.
ny_journal_entries() {
    local file
    file="$(ny_journal_dir)/journal.jsonl"
    [[ -f "$file" ]] || return 0
    local txn="" feature=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --txn) txn="$2"; shift 2 ;;
            --feature) feature="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    jq -c --arg txn "$txn" --arg feature "$feature" -s '
        (map(select(.op == "undone") | (.undone_txn + "|" + (.undone_feature // "")))) as $undone
        | map(select(.op != "undone"))
        | map(select((.txn + "|") as $all | (.txn + "|" + .feature) as $part
                     | ($undone | any(.[]; . == $all or . == $part)) | not))
        | map(select(($txn == "" or .txn == $txn) and ($feature == "" or .feature == $feature)))
        | .[]' "$file"
}

# ny_journal_undo_feature FEATURE [FORCE] -- undo every change a feature made,
# newest first.
ny_journal_undo_feature() {
    local feature="$1" force="${2:-0}" failed=0 t
    local -a txns=()
    mapfile -t txns < <(ny_journal_entries --feature "$feature" | jq -r '.txn' | awk '!seen[$0]++' | sort -r)
    for t in "${txns[@]+"${txns[@]}"}"; do
        ny_journal_undo_txn "$t" "$force" "$feature" || failed=1
    done
    return "$failed"
}

# ny_journal_txns [--feature F] -- one summary line per transaction (JSON).
ny_journal_txns() {
    local file
    file="$(ny_journal_dir)/journal.jsonl"
    [[ -f "$file" ]] || return 0
    local feature="${2:-}"
    [[ "${1:-}" == "--feature" ]] || feature=""
    jq -c --arg feature "$feature" -s '
        (map(select(.op == "undone" and (.undone_feature // "") == "") | .undone_txn)) as $undone
        | map(select(.op != "undone"))
        | group_by(.txn)
        | map({txn: .[0].txn, ts: .[0].ts, cmd: .[0].cmd,
               features: (map(.feature) | unique),
               changes: length,
               paths: (map(.path // empty) | unique),
               undone: (.[0].txn as $t | $undone | any(.[]; . == $t))})
        | map(select($feature == "" or (.features | index($feature))))
        | sort_by(.ts) | .[]' "$file"
}

# ny_journal_undo_txn TXN [FORCE] [FEATURE] -- reverse one transaction's
# changes (only those of FEATURE, if given). Returns non-zero if any change
# could not be reverted safely.
ny_journal_undo_txn() {
    local txn="$1" force="${2:-0}" only_feature="${3:-}" failed=0
    local -a lines=()
    if [[ -n "$only_feature" ]]; then
        mapfile -t lines < <(ny_journal_entries --txn "$txn" --feature "$only_feature")
    else
        mapfile -t lines < <(ny_journal_entries --txn "$txn")
    fi
    [[ "${#lines[@]}" -gt 0 ]] || {
        ny_warn "Nothing to undo for ${txn}."
        return 0
    }

    local i line op path existed backup sha_after real current
    for ((i = ${#lines[@]} - 1; i >= 0; i--)); do
        line="${lines[i]}"
        op="$(jq -r '.op' <<<"$line")"
        path="$(jq -r '.path // ""' <<<"$line")"
        real="$(ny_path "$path")"
        case "$op" in
            write | delete)
                existed="$(jq -r '.existed' <<<"$line")"
                backup="$(jq -r '.backup // ""' <<<"$line")"
                sha_after="$(jq -r '.sha_after // ""' <<<"$line")"
                current=""
                [[ -f "$real" ]] && current="$(ny_sha256 "$real")"
                if [[ "$op" == "write" && -n "$sha_after" && "$current" != "$sha_after" && "$force" -ne 1 ]]; then
                    ny_warn "${path} was changed by something else after nodeyard wrote it; leaving it alone (use --force to restore the backup anyway)."
                    failed=1
                    continue
                fi
                if [[ "$existed" == "true" ]]; then
                    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
                        ny_info "[dry-run] would restore ${path} from the backup taken at ${txn}"
                    else
                        mkdir -p -- "$(dirname -- "$real")"
                        cp -p -- "$(ny_journal_dir)/${backup}" "$real"
                        ny_ok "Restored ${path}"
                    fi
                elif [[ -e "$real" ]]; then
                    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
                        ny_info "[dry-run] would remove ${path} (nodeyard created it)"
                    else
                        rm -f -- "$real"
                        ny_ok "Removed ${path} (nodeyard created it)"
                    fi
                fi
                ;;
            mkdir)
                if [[ -d "$real" ]]; then
                    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
                        ny_info "[dry-run] would remove directory ${path} if empty"
                    else
                        rmdir -- "$real" 2>/dev/null && ny_ok "Removed empty directory ${path}" || true
                    fi
                fi
                ;;
            run)
                local -a undo=()
                mapfile -t undo < <(jq -r '.undo // [] | .[]' <<<"$line")
                if [[ "${#undo[@]}" -gt 0 ]]; then
                    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
                        ny_info "[dry-run] would run: ${undo[*]}"
                    elif "${undo[@]}"; then
                        ny_ok "Reverted: $(jq -r '.command | join(" ")' <<<"$line")"
                    else
                        ny_warn "Could not revert: $(jq -r '.command | join(" ")' <<<"$line")"
                        failed=1
                    fi
                fi
                ;;
        esac
    done

    if [[ "$NY_DRY_RUN" -ne 1 && "$failed" -eq 0 ]]; then
        ny_journal_append "$(ny_json_obj op=undone "undone_txn=$txn" "undone_feature=$only_feature")"
    fi
    return "$failed"
}
