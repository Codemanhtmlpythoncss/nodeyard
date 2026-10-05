# shellcheck shell=bash
# Commands for the cluster config file.

ny_cmd "config show" config_show_cmd "Settings" "Print the cluster config" config json
ny_cmd "config path" config_path_cmd "Settings" "Print where the cluster config lives" config
ny_cmd "config get" config_get_cmd "Settings" "Print one setting, e.g. node.pi-1.address" config json
ny_cmd "config set" config_set_cmd "Settings" "Change one setting (validated, backed up)" config
ny_cmd "config unset" config_unset_cmd "Settings" "Remove one setting" config
ny_cmd "config validate" config_validate_cmd "Settings" "Check the cluster config for mistakes" config json
ny_cmd "config edit" config_edit_cmd "Settings" "Edit the cluster config in \$EDITOR, then validate it" config
ny_cmd "config export" config_export_cmd "Settings" "Write the cluster config to a file or stdout" config
ny_cmd "config import" config_import_cmd "Settings" "Replace the cluster config with a file (validated first)" config
ny_cmd "config drift" config_drift_cmd "Settings" "Report where this node differs from the cluster config" config json

# config_key KEY -- split "section.key" or "section.sub.key" (sub may contain dots).
config_key() {
    local key="$1"
    [[ "$key" == *.* ]] || ny_usage_error "Keys look like section.key or section.name.key (e.g. cluster.vip, node.pi-1.address)."
    CFG_SEC="${key%%.*}"
    local rest="${key#*.}"
    if [[ "$rest" == *.* ]]; then
        CFG_KEY="${rest##*.}"
        CFG_SUB="${rest%.*}"
    else
        CFG_KEY="$rest"
        CFG_SUB=""
    fi
    CFG_SEC="${CFG_SEC,,}"
    CFG_KEY="${CFG_KEY,,}"
}

config_show_cmd() {
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    [[ -r "$NY_CONFIG" ]] || ny_die "There is no cluster config yet (${NY_CONFIG})." "It is created when you set up this node; see examples/cluster.conf." "$NY_E_PRECONDITION"
    if [[ "$NY_JSON" -eq 1 ]]; then
        ny_cfg_parse "$NY_CONFIG" || true
        local k sec sub key
        local -a items=()
        for k in "${!NY_CFG[@]}"; do
            sec="${k%%"$NY_US"*}"
            sub="${k#*"$NY_US"}"
            sub="${sub%%"$NY_US"*}"
            key="${k##*"$NY_US"}"
            items+=("$(ny_json_obj "section=$sec" "name?=$sub" "key=$key" "value=$(ny_cfg_get "$sec" "$sub" "$key")")")
        done
        ny_json_out "$(ny_json_obj ok:=true "path=$(ny_unroot "$NY_CONFIG")" "settings:=$(ny_json_arr "${items[@]+"${items[@]}"}")")"
        return 0
    fi
    cat "$NY_CONFIG"
}

config_path_cmd() {
    printf '%s\n' "$(ny_unroot "$NY_CONFIG")"
}

config_get_cmd() {
    [[ $# -eq 1 ]] || ny_usage_error "Say which setting." "nodeyard config get section.key | section.name.key"
    config_key "$1"
    if ! ny_cfg_has "$CFG_SEC" "$CFG_SUB" "$CFG_KEY"; then
        [[ "$NY_JSON" -eq 1 ]] && ny_json_out "$(ny_json_obj ok:=true "key=$1" value:=null)"
        return 1
    fi
    local v
    v="$(ny_cfg_get "$CFG_SEC" "$CFG_SUB" "$CFG_KEY")"
    if [[ "$NY_JSON" -eq 1 ]]; then
        ny_json_out "$(ny_json_obj ok:=true "key=$1" "value=$v")"
    else
        printf '%s\n' "$v"
    fi
}

config_set_cmd_help() {
    cat <<'HELP'
Usage: nodeyard config set KEY VALUE [--add]

Changes one setting in the cluster config, checking the value first. The
old file is backed up (undo with 'nodeyard undo --last').

  KEY      section.key or section.name.key, e.g. cluster.vip, node.pi-1.address
  --add    Add another value to a list setting instead of replacing it

Examples:
  sudo nodeyard config set network.subnet 192.168.1.0/24
  sudo nodeyard config set node.pi-2.groups storage,low-power
HELP
}

config_set_cmd() {
    ny_need_root
    local add=0
    local -a pos=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --add) add=1; shift ;;
            *) pos+=("$1"); shift ;;
        esac
    done
    [[ "${#pos[@]}" -eq 2 ]] || ny_usage_error "Give a key and a value." "nodeyard config set section.key VALUE"
    config_key "${pos[0]}"
    local value="${pos[1]}"
    local spec="${NY_CFG_SCHEMA["$CFG_SEC.$CFG_KEY"]:-${NY_CFG_SCHEMA["$CFG_SEC.*.$CFG_KEY"]:-}}"
    [[ -n "${NY_CFG_SECTION_KIND[$CFG_SEC]:-}" ]] || ny_usage_error "Unknown section '${CFG_SEC}'." "Sections: $(printf '%s ' "${!NY_CFG_SECTION_KIND[@]}")"
    [[ -n "$spec" ]] || ny_usage_error "Unknown setting '${CFG_KEY}' in [${CFG_SEC}]." "See docs/configuration.md for every setting."
    if [[ "${NY_CFG_SECTION_KIND[$CFG_SEC]}" == named && -z "$CFG_SUB" ]]; then
        ny_usage_error "[${CFG_SEC}] settings need a name, e.g. ${CFG_SEC}.NAME.${CFG_KEY}"
    fi
    ny_validate "${spec%%|*}" "$value" || ny_usage_error "${pos[0]}: ${NY_VALID_MSG}"
    [[ "$CFG_SEC" != node ]] || ny_valid_hostname "$CFG_SUB" || ny_usage_error "Node name: ${NY_VALID_MSG}"
    if [[ "$add" -eq 1 ]]; then
        ny_cfg_add "$CFG_SEC" "$CFG_SUB" "$CFG_KEY" "$value"
    else
        ny_cfg_set "$CFG_SEC" "$CFG_SUB" "$CFG_KEY" "$value"
    fi
    if [[ "$NY_DRY_RUN" -eq 0 ]] && ! ny_cfg_validate "$NY_CONFIG"; then
        local e
        for e in "${NY_CFG_ERRORS[@]}"; do ny_warn "$e"; done
        ny_warn "The config now has problems (above); fix them or undo this change: sudo nodeyard undo --last"
    else
        ny_ok "Set ${pos[0]} = ${value}"
    fi
    return 0
}

config_unset_cmd() {
    ny_need_root
    [[ $# -eq 1 ]] || ny_usage_error "Say which setting." "nodeyard config unset section.key"
    config_key "$1"
    ny_cfg_has "$CFG_SEC" "$CFG_SUB" "$CFG_KEY" || {
        ny_info "${1} is not set."
        return 0
    }
    ny_cfg_unset "$CFG_SEC" "$CFG_SUB" "$CFG_KEY"
    ny_ok "Removed ${1}"
}

config_print_validation() {
    local e
    for e in "${NY_CFG_ERRORS[@]+"${NY_CFG_ERRORS[@]}"}"; do ny_err "$e"; done
    for e in "${NY_CFG_WARNINGS[@]+"${NY_CFG_WARNINGS[@]}"}"; do ny_warn "$e"; done
}

config_validate_cmd() {
    local file="${1:-$NY_CONFIG}"
    [[ -r "$file" ]] || ny_die "Cannot read ${file}." "Check the path (and run with sudo for /etc/nodeyard/cluster.conf)." "$NY_E_PRECONDITION"
    local ok=1
    ny_cfg_validate "$file" || ok=0
    if [[ "$NY_JSON" -eq 1 ]]; then
        ny_json_out "$(ny_json_obj "ok:=$(ny_json_bool "$ok")" "file=$file" "errors:=$(ny_json_arr_str "${NY_CFG_ERRORS[@]+"${NY_CFG_ERRORS[@]}"}")" "warnings:=$(ny_json_arr_str "${NY_CFG_WARNINGS[@]+"${NY_CFG_WARNINGS[@]}"}")")"
        [[ "$ok" -eq 1 ]] || return 1
        return 0
    fi
    config_print_validation
    if [[ "$ok" -eq 1 ]]; then
        ny_ok "${file} is valid ($(ny_cfg_subs node | grep -c . || true) node(s))."
        return 0
    fi
    ny_die "${file} has ${#NY_CFG_ERRORS[@]} problem(s) (listed above)." "Fix them with: sudo nodeyard config edit" "$NY_E_PRECONDITION"
}

config_edit_cmd() {
    ny_need_root
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    ny_ui_interactive || ny_die "config edit needs an interactive terminal." "Use 'nodeyard config set' or 'config import' in scripts." "$NY_E_PRECONDITION"
    local editor="${VISUAL:-${EDITOR:-}}"
    if [[ -z "$editor" ]]; then
        for editor in nano vim vi; do have "$editor" && break; done
    fi
    have "${editor%% *}" || ny_die "No text editor found." "Set EDITOR, e.g.: sudo EDITOR=nano nodeyard config edit" "$NY_E_PRECONDITION"
    local tmp
    tmp="$(ny_mktemp)"
    [[ -f "$NY_CONFIG" ]] && cat "$NY_CONFIG" >"$tmp"
    while true; do
        $editor "$tmp"
        if ny_cfg_validate "$tmp"; then
            config_print_validation
            break
        fi
        config_print_validation
        ny_ui_yesno "The config has problems. Edit it again? (No discards your edits)" y || {
            ny_info "Discarded; the config was not changed."
            return 0
        }
    done
    ny_write_file "$(ny_unroot "$NY_CONFIG")" 0640 <"$tmp"
    ny_ok "Saved $(ny_unroot "$NY_CONFIG")."
}

config_export_cmd_help() {
    cat <<'HELP'
Usage: nodeyard config export [--out FILE]

Writes the cluster config (which never contains secrets) to FILE, or to
stdout. Use it to rebuild a node or the whole cluster elsewhere.
HELP
}

config_export_cmd() {
    local out=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --out) ny_need_value "$1" $#; out="$2"; shift 2 ;;
            *) ny_usage_error "Unknown option for 'config export': $1" ;;
        esac
    done
    [[ -r "$NY_CONFIG" ]] || ny_die "There is no cluster config to export." "" "$NY_E_PRECONDITION"
    if [[ -z "$out" ]]; then
        printf '# Exported from %s by nodeyard %s on %s\n' "$(ny_self_name)" "$NY_VERSION" "$(ny_now)"
        cat "$NY_CONFIG"
        return 0
    fi
    [[ ! -e "$out" ]] || ny_confirm "Overwrite ${out}?" n || return 0
    { printf '# Exported from %s by nodeyard %s on %s\n' "$(ny_self_name)" "$NY_VERSION" "$(ny_now)"; cat "$NY_CONFIG"; } >"$out"
    ny_ok "Exported to ${out}"
}

config_import_cmd_help() {
    cat <<'HELP'
Usage: nodeyard config import FILE [--yes]

Replaces the cluster config with FILE after validating it and showing the
differences. The current file is backed up (undo with 'nodeyard undo --last').
Importing changes only the description; apply it to machines with the
relevant commands (or 'nodeyard config drift --fix').
HELP
}

config_import_cmd() {
    ny_need_root
    local file="${1:-}"
    [[ -n "$file" && $# -eq 1 ]] || ny_usage_error "Say which file to import." "nodeyard config import FILE"
    [[ -r "$file" ]] || ny_die "Cannot read ${file}." "" "$NY_E_USAGE"
    if ! ny_cfg_validate "$file"; then
        config_print_validation
        ny_die "${file} is not a valid cluster config (problems above)." "Fix them, then import again." "$NY_E_USAGE"
    fi
    config_print_validation
    if [[ -f "$NY_CONFIG" ]] && have diff; then
        if diff -q "$NY_CONFIG" "$file" >/dev/null 2>&1; then
            ny_ok "The cluster config already matches ${file}."
            return 0
        fi
        diff -u --label current --label "$file" "$NY_CONFIG" "$file" | head -n 80 >&2 || true
    fi
    ny_confirm "Replace the cluster config with ${file}?" y || return 0
    ny_write_file "$(ny_unroot "$NY_CONFIG")" 0640 <"$file"
    ny_ok "Imported ${file}."
}

config_drift_cmd_help() {
    cat <<'HELP'
Usage: nodeyard config drift [--fix] [--json]

Compares this node with its entry in the cluster config and reports what
differs (hostname, role, interface, address). Later releases extend this to
every node and to more settings; see docs/STATUS.md.

  --fix   Repair what can be repaired safely here (currently: the hostname)
HELP
}

config_drift_cmd() {
    local fix=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --fix) fix=1; shift ;;
            *) ny_usage_error "Unknown option for 'config drift': $1" ;;
        esac
    done
    [[ "$fix" -eq 0 ]] || ny_need_root
    local self
    self="$(ny_self_name)"
    local -a items=()
    local drift=0
    config_drift_item() { # name want have fixable
        local st="ok"
        if [[ -n "$2" && "$2" != "$3" ]]; then
            st="drift"
            drift=$((drift + 1))
        fi
        [[ -n "$2" ]] || st="unset"
        items+=("$(ny_json_obj "setting=$1" "expected?=$2" "actual?=$3" "status=$st" "fixable:=$(ny_json_bool "$4")")")
        CFG_DRIFT_LAST="$st"
    }
    if ! ny_cfg_has_section node "$self"; then
        [[ "$NY_JSON" -eq 1 ]] && {
            ny_json_out "$(ny_json_obj ok:=true "node=$self" in_config:=false drift:=0 items:=[])"
            return 0
        }
        ny_info "This node (${self}) has no [node \"${self}\"] section in the cluster config, so there is nothing to compare."
        return 0
    fi
    k3s_load_state
    local want_role have_role="" iface addr want_addr have_addr=""
    want_role="$(ny_role_normalize "$(ny_cfg_get node "$self" role)")"
    if [[ -f "$(ny_path /etc/systemd/system/k3s-agent.service)" ]]; then
        have_role="agent"
    elif [[ -f "$(ny_path /etc/systemd/system/k3s.service)" ]]; then
        have_role="server"
    else
        have_role="standalone"
    fi
    config_drift_item hostname "$self" "$(hostname 2>/dev/null | cut -d. -f1)" 1
    local host_drift="$CFG_DRIFT_LAST"
    config_drift_item role "$want_role" "$have_role" 0
    iface="$(ny_cfg_get node "$self" interface)"
    if [[ -n "$iface" ]]; then
        config_drift_item interface-exists "yes" "$(ny_iface_exists "$iface" && echo yes || echo no)" 0
        want_addr="$(ny_cfg_get node "$self" address)"
        ny_iface_exists "$iface" && have_addr="$(ny_iface_cidr "$iface")"
        config_drift_item address "$want_addr" "$have_addr" 0
    fi

    if [[ "$fix" -eq 1 && "$host_drift" == drift ]]; then
        if have hostnamectl; then
            ny_run_undoable hostnamectl set-hostname "$(hostname)" -- hostnamectl set-hostname "$self"
        else
            printf '%s\n' "$self" | ny_write_file /etc/hostname
            ny_run hostname "$self"
        fi
        ny_ok "Hostname set to ${self}."
    fi

    if [[ "$NY_JSON" -eq 1 ]]; then
        ny_json_out "$(ny_json_obj ok:=true "node=$self" in_config:=true "drift:=$drift" "items:=$(ny_json_arr "${items[@]}")")"
        return 0
    fi
    {
        printf 'SETTING\tEXPECTED\tACTUAL\tSTATUS\n'
        printf '%s\n' "${items[@]}" | jq -r '[.setting, (.expected // "-"), (.actual // "-"), .status] | @tsv'
    } | ny_table --status STATUS
    if [[ "$drift" -gt 0 ]]; then
        ny_warn "${drift} setting(s) differ from the cluster config."
        [[ "$fix" -eq 1 ]] || ny_hint "Repair what's safe with: sudo nodeyard config drift --fix (address and role changes have their own commands)"
    else
        ny_ok "This node matches the cluster config."
    fi
    return 0
}
