# shellcheck shell=bash
# shellcheck disable=SC2034 # globals here are read by other files
# The cluster config file: one git-config-style INI file that describes the
# whole cluster (nodes, roles, IP plan, and later sites, AI and backups).
#
#   # comment            ; also a comment
#   [cluster]
#   name = homelab
#   [node "pi-1"]
#   address = 192.168.1.10/24
#   groups = storage, low-power        # lists are comma-separated...
#   groups = gpu                       # ...or repeated keys
#
# Section and key names are case-insensitive; subsection names (node names)
# are not. Values may be double-quoted to keep '#', ';' or edge whitespace.
# Plain bash can read this format, including bash 3.2 on a macOS laptop.
# Secrets never go in this file.

declare -gA NY_CFG=()      # "section<US>sub<US>key" -> value (repeats joined by newline)
declare -gA NY_CFG_LINE=() # same key -> line of first occurrence
declare -gA NY_CFG_SECTION_LINE=()
NY_CFG_ORDER=() # "section<US>sub" in file order
NY_CFG_ERRORS=()
NY_CFG_WARNINGS=()
NY_CFG_LOADED_FROM=""
NY_US=$'\x1f'

# --- schema ------------------------------------------------------------------
# NY_CFG_SCHEMA["section.key"] or ["section.*.key"] = "TYPE|FLAGS|description"
# FLAGS: comma-separated; "required". Modules add their own keys with
# ny_cfg_schema_add when they load.
declare -gA NY_CFG_SCHEMA=()
declare -gA NY_CFG_SECTION_KIND=() # section -> single | named

ny_cfg_schema_add() {
    local key="$1" type="$2" flags="$3" desc="$4"
    NY_CFG_SCHEMA["$key"]="${type}|${flags}|${desc}"
}

ny_cfg_section_add() {
    NY_CFG_SECTION_KIND["$1"]="$2"
}

ny_cfg_section_add cluster single
ny_cfg_schema_add cluster.name name "" "Short name for the cluster, e.g. homelab"
ny_cfg_schema_add cluster.domain hostname "" "Local DNS domain for node names, e.g. home.arpa"
ny_cfg_schema_add cluster.vip ipv4 "" "Floating virtual IP for the Kubernetes API and dashboard"
ny_cfg_schema_add cluster.k3s-version k3s-version "" "Exact k3s release, e.g. v1.33.4+k3s1"
ny_cfg_schema_add cluster.k3s-channel enum:stable,latest,testing "" "k3s release channel when no exact version is set"
ny_cfg_schema_add cluster.cluster-cidr cidr4 "" "Pod network range (k3s default 10.42.0.0/16)"
ny_cfg_schema_add cluster.service-cidr cidr4 "" "Service network range (k3s default 10.43.0.0/16)"
ny_cfg_schema_add cluster.tls-san list:host "" "Extra names/addresses for the API server certificate"
ny_cfg_schema_add cluster.disable list:name "" "Bundled k3s components to disable, e.g. traefik, servicelb"

ny_cfg_section_add network single
ny_cfg_schema_add network.subnet cidr4 "" "The LAN all nodes share, e.g. 192.168.1.0/24"
ny_cfg_schema_add network.gateway ipv4 "" "Your router's address, e.g. 192.168.1.1"
ny_cfg_schema_add network.dns list:ipv4 "" "DNS servers nodes should use"
ny_cfg_schema_add network.dhcp-range ipv4-range "" "Addresses your router hands out by DHCP (to keep static ones out of)"
ny_cfg_schema_add network.node-range ipv4-range "" "Addresses reserved for cluster nodes"
ny_cfg_schema_add network.lb-range ipv4-range "" "Addresses reserved for load-balanced services"
ny_cfg_schema_add network.interface iface "" "Default ethernet interface for cluster traffic"

ny_cfg_section_add node named
ny_cfg_schema_add node.*.host host "" "Address or hostname used to reach this node over SSH"
ny_cfg_schema_add node.*.address cidr4 "" "Static address for this node, e.g. 192.168.1.10/24"
ny_cfg_schema_add node.*.interface iface "" "Network interface for cluster traffic, e.g. eth0"
ny_cfg_schema_add node.*.role enum:server,agent,standalone,master,worker "" "server (control plane), agent (worker) or standalone"
ny_cfg_schema_add node.*.init bool "" "true for the first server that creates the cluster"
ny_cfg_schema_add node.*.server url "" "API address this node joins, e.g. https://192.168.1.10:6443"
ny_cfg_schema_add node.*.node-ip ipv4 "" "Address k3s was installed on (recorded automatically)"
ny_cfg_schema_add node.*.allow-workloads bool "" "Let pods run on this server node"
ny_cfg_schema_add node.*.groups list:name "" "Groups for placing workloads, e.g. gpu, storage, low-power"
ny_cfg_schema_add node.*.labels list:label "" "Extra Kubernetes labels (key=value)"
ny_cfg_schema_add node.*.taints list:taint "" "Kubernetes taints (key=value:Effect)"
ny_cfg_schema_add node.*.features list:name "" "nodeyard features installed on this node"
ny_cfg_schema_add node.*.tls-san list:host "" "Extra API certificate names for this server"
ny_cfg_schema_add node.*.ssh-user string "" "SSH user for remote management"
ny_cfg_schema_add node.*.ssh-port port "" "SSH port (default 22)"
ny_cfg_schema_add node.*.mac mac "" "MAC address of the cluster interface (for Wake-on-LAN)"

ny_cfg_section_add ui single
ny_cfg_schema_add ui.backend enum:auto,gum,whiptail,dialog,plain "" "Terminal interface style"
ny_cfg_schema_add ui.color enum:auto,always,never "" "Colour output"

ny_cfg_section_add watchdog single
ny_cfg_schema_add watchdog.master host "" "Control-plane address the watchdog checks"
ny_cfg_schema_add watchdog.interval int:5:3600 "" "Seconds between checks"
ny_cfg_schema_add watchdog.threshold int:1:1000 "" "Failed checks before it reports the master as down"

# --- parsing -----------------------------------------------------------------

ny_cfg_reset() {
    NY_CFG=()
    NY_CFG_LINE=()
    NY_CFG_SECTION_LINE=()
    NY_CFG_ORDER=()
    NY_CFG_ERRORS=()
    NY_CFG_WARNINGS=()
}

# ny_cfg_unquote RAW -- the value part of a key line, with quotes/comments handled.
# Sets NY_CFG_VALUE; returns 1 on a syntax error.
ny_cfg_unquote() {
    local raw="$1"
    NY_CFG_VALUE=""
    if [[ "${raw:0:1}" == '"' ]]; then
        local i=1 c out=""
        while ((i < ${#raw})); do
            c="${raw:i:1}"
            # shellcheck disable=SC1003 # comparing with a literal backslash
            if [[ "$c" == '\' ]]; then
                i=$((i + 1))
                case "${raw:i:1}" in
                    n) out+=$'\n' ;;
                    t) out+=$'\t' ;;
                    *) out+="${raw:i:1}" ;;
                esac
            elif [[ "$c" == '"' ]]; then
                local rest
                rest="$(ny_trim "${raw:i+1}")"
                if [[ -n "$rest" && "${rest:0:1}" != "#" && "${rest:0:1}" != ";" ]]; then
                    return 1
                fi
                NY_CFG_VALUE="$out"
                return 0
            else
                out+="$c"
            fi
            i=$((i + 1))
        done
        return 1
    fi
    # Unquoted: an inline comment starts at whitespace followed by # or ;
    if [[ "$raw" =~ ^(.*[^[:space:]])?[[:space:]]+[#\;].*$ ]]; then
        raw="${BASH_REMATCH[1]}"
    elif [[ "${raw:0:1}" == "#" || "${raw:0:1}" == ";" ]]; then
        raw=""
    fi
    NY_CFG_VALUE="$(ny_trim "$raw")"
}

# ny_cfg_parse FILE -- parse into NY_CFG. Returns 1 if there were syntax errors
# (listed in NY_CFG_ERRORS).
ny_cfg_parse() {
    local file="$1"
    ny_cfg_reset
    [[ -f "$file" ]] || return 0
    local line lineno=0 trimmed section="" sub="" key k
    local sec_re='^\[([A-Za-z][A-Za-z0-9_-]*)([[:space:]]+"([^"]*)")?\]$'
    local key_re='^([A-Za-z][A-Za-z0-9_-]*)[[:space:]]*=[[:space:]]*(.*)$'
    while IFS= read -r line || [[ -n "$line" ]]; do
        lineno=$((lineno + 1))
        trimmed="$(ny_trim "$line")"
        [[ -z "$trimmed" || "${trimmed:0:1}" == "#" || "${trimmed:0:1}" == ";" ]] && continue
        if [[ "$trimmed" =~ $sec_re ]]; then
            section="${BASH_REMATCH[1],,}"
            sub="${BASH_REMATCH[3]}"
            if [[ -z "${NY_CFG_SECTION_LINE["$section$NY_US$sub"]+x}" ]]; then
                NY_CFG_ORDER+=("$section$NY_US$sub")
                NY_CFG_SECTION_LINE["$section$NY_US$sub"]="$lineno"
            fi
            continue
        fi
        if [[ "$trimmed" =~ $key_re ]]; then
            if [[ -z "$section" ]]; then
                NY_CFG_ERRORS+=("line ${lineno}: '${BASH_REMATCH[1]}' is outside any [section]. Fix: put it under a section such as [cluster].")
                continue
            fi
            key="${BASH_REMATCH[1],,}"
            if ! ny_cfg_unquote "${BASH_REMATCH[2]}"; then
                NY_CFG_ERRORS+=("line ${lineno}: unbalanced quotes in the value of '${key}'. Fix: close the quote, or escape a literal quote as \\\".")
                continue
            fi
            k="$section$NY_US$sub$NY_US$key"
            if [[ -n "${NY_CFG[$k]+x}" ]]; then
                NY_CFG["$k"]+=$'\n'"$NY_CFG_VALUE"
            else
                NY_CFG["$k"]="$NY_CFG_VALUE"
                NY_CFG_LINE["$k"]="$lineno"
            fi
            continue
        fi
        NY_CFG_ERRORS+=("line ${lineno}: cannot understand '$(ny_redact "$trimmed")'. Fix: use 'key = value', a [section] header, or start the line with # for a comment.")
    done <"$file"
    [[ "${#NY_CFG_ERRORS[@]}" -eq 0 ]]
}

# ny_cfg_load [FILE] -- load the cluster config (a missing file is empty).
ny_cfg_load() {
    local file="${1:-$NY_CONFIG}"
    if ! ny_cfg_parse "$file"; then
        local e
        for e in "${NY_CFG_ERRORS[@]}"; do
            ny_err "${file}: ${e}"
        done
        ny_die "The config file ${file} has syntax errors (listed above)." "Fix those lines, then check with: nodeyard config validate" "$NY_E_PRECONDITION"
    fi
    NY_CFG_LOADED_FROM="$file"
}

# --- reading -----------------------------------------------------------------

# ny_cfg_get SECTION SUB KEY [DEFAULT] -- the (last) value of a key.
ny_cfg_get() {
    local k="${1,,}$NY_US$2$NY_US${3,,}"
    if [[ -n "${NY_CFG[$k]+x}" ]]; then
        printf '%s\n' "${NY_CFG[$k]##*$'\n'}"
    else
        printf '%s\n' "${4:-}"
    fi
    return 0
}

# ny_cfg_get_list SECTION SUB KEY -- every value, splitting repeats and commas.
ny_cfg_get_list() {
    local k="${1,,}$NY_US$2$NY_US${3,,}" v
    [[ -n "${NY_CFG[$k]+x}" ]] || return 0
    while IFS= read -r v; do
        ny_csv_split "$v"
    done <<<"${NY_CFG[$k]}"
    return 0
}

ny_cfg_has() {
    local k="${1,,}$NY_US$2$NY_US${3,,}"
    [[ -n "${NY_CFG[$k]+x}" ]]
}

ny_cfg_has_section() {
    [[ -n "${NY_CFG_SECTION_LINE["${1,,}$NY_US${2:-}"]+x}" ]]
}

# ny_cfg_subs SECTION -- subsection names of SECTION in file order.
ny_cfg_subs() {
    local s="${1,,}" entry
    for entry in "${NY_CFG_ORDER[@]+"${NY_CFG_ORDER[@]}"}"; do
        [[ "${entry%%"$NY_US"*}" == "$s" ]] || continue
        printf '%s\n' "${entry#*"$NY_US"}"
    done
    return 0
}

# ny_cfg_keys SECTION SUB -- key names present in a section.
ny_cfg_keys() {
    local prefix="${1,,}$NY_US$2$NY_US" k
    for k in "${!NY_CFG[@]}"; do
        [[ "$k" == "$prefix"* ]] && printf '%s\n' "${k#"$prefix"}"
    done | sort
    return 0
}

# --- this node ---------------------------------------------------------------

# ny_self_name -- this machine's node name in the config.
ny_self_name() {
    local f
    f="${NY_ETC}/node-name"
    if [[ -r "$f" ]]; then
        local n
        n="$(head -n1 "$f" 2>/dev/null || true)"
        n="$(ny_trim "$n")"
        if [[ -n "$n" ]]; then
            printf '%s\n' "$n"
            return 0
        fi
    fi
    local h
    h="$(hostname 2>/dev/null || uname -n)"
    printf '%s\n' "${h%%.*}"
}

# ny_role_normalize ROLE -- server | agent | standalone (accepts master/worker).
ny_role_normalize() {
    case "${1,,}" in
        server | master | control-plane) echo server ;;
        agent | worker) echo agent ;;
        standalone) echo standalone ;;
        *) echo "" ;;
    esac
    return 0
}

# --- writing -----------------------------------------------------------------

# ny_cfg_format_value VALUE -- quote a value when it needs it.
ny_cfg_format_value() {
    local v="$1"
    if [[ "$v" =~ [#\;\"\\] || "$v" != "$(ny_trim "$v")" || "$v" == *$'\n'* || "$v" == *$'\t'* ]]; then
        v="${v//\\/\\\\}"
        v="${v//\"/\\\"}"
        v="${v//$'\n'/\\n}"
        v="${v//$'\t'/\\t}"
        printf '"%s"' "$v"
    else
        printf '%s' "$v"
    fi
    return 0
}

ny_cfg_header() {
    if [[ -n "$2" ]]; then
        printf '[%s "%s"]' "$1" "$2"
    else
        printf '[%s]' "$1"
    fi
    return 0
}

# ny_cfg_edit MODE SECTION SUB KEY [VALUE] [FILE]
#   MODE: set (replace all values), add (append a value), unset (remove key),
#   drop (remove the whole section; KEY ignored).
# Comments and layout are preserved. The change is journaled.
ny_cfg_edit() {
    local mode="$1" section="${2,,}" sub="$3" key="${4,,}" value="${5:-}" file="${6:-$NY_CONFIG}"
    local real="$file" batching=0
    # Inside ny_cfg_batch_begin/commit, edits accumulate in a scratch copy
    # and the real file is written (and journaled) once.
    if [[ -n "${NY_CFG_BATCH_TMP:-}" && "$file" == "$NY_CFG_BATCH_TARGET" ]]; then
        real="$NY_CFG_BATCH_TMP"
        batching=1
    fi
    local -a lines=()
    if [[ -f "$real" ]]; then
        mapfile -t lines <"$real"
    fi

    local sec_re='^[[:space:]]*\[([A-Za-z][A-Za-z0-9_-]*)([[:space:]]+"([^"]*)")?\][[:space:]]*$'
    local key_re='^[[:space:]]*([A-Za-z][A-Za-z0-9_-]*)[[:space:]]*='
    local i start=-1 end=${#lines[@]} in_sec=0
    for ((i = 0; i < ${#lines[@]}; i++)); do
        if [[ "${lines[i]}" =~ $sec_re ]]; then
            if [[ "$in_sec" -eq 1 ]]; then
                end=$i
                break
            fi
            if [[ "${BASH_REMATCH[1],,}" == "$section" && "${BASH_REMATCH[3]}" == "$sub" ]]; then
                start=$i
                in_sec=1
            fi
        fi
    done

    local newline=""
    [[ "$mode" == set || "$mode" == add ]] && newline="	${key} = $(ny_cfg_format_value "$value")"

    local -a out=()
    if [[ "$start" -lt 0 ]]; then
        [[ "$mode" == unset || "$mode" == drop ]] && return 0
        out=("${lines[@]+"${lines[@]}"}")
        if [[ "${#out[@]}" -gt 0 && -n "$(ny_trim "${out[${#out[@]} - 1]}")" ]]; then
            out+=("")
        fi
        out+=("$(ny_cfg_header "$section" "$sub")" "$newline")
    else
        local last_key=-1 last_content=$start
        local -a keep=()
        for ((i = start + 1; i < end; i++)); do
            if [[ "${lines[i]}" =~ $key_re && "${BASH_REMATCH[1],,}" == "$key" ]]; then
                last_key=$i
            fi
            [[ -n "$(ny_trim "${lines[i]}")" ]] && last_content=$i
        done
        for ((i = 0; i < ${#lines[@]}; i++)); do
            if [[ "$mode" == drop ]]; then
                if ((i >= start && i < end)); then
                    continue
                fi
                keep+=("${lines[i]}")
                continue
            fi
            if ((i > start && i < end)) && [[ "${lines[i]}" =~ $key_re && "${BASH_REMATCH[1],,}" == "$key" ]]; then
                if [[ "$mode" == set && "$i" -eq "$last_key" ]]; then
                    local indent="${lines[i]%%[![:space:]]*}"
                    keep+=("${indent}${newline#	}")
                elif [[ "$mode" == add ]]; then
                    keep+=("${lines[i]}")
                    [[ "$i" -eq "$last_key" ]] && keep+=("$newline")
                fi
                continue
            fi
            keep+=("${lines[i]}")
            if [[ "$i" -eq "$last_content" && "$last_key" -lt 0 && ("$mode" == set || "$mode" == add) ]]; then
                keep+=("$newline")
            fi
        done
        out=("${keep[@]+"${keep[@]}"}")
    fi

    if [[ "$batching" -eq 1 ]]; then
        printf '%s\n' "${out[@]+"${out[@]}"}" >"$real"
        return 0
    fi
    # A file outside the system root (e.g. one being prepared for import) is
    # simply rewritten; system config goes through the journal.
    if [[ -n "$NY_ROOT" && "$file" != "$NY_ROOT"/* ]]; then
        printf '%s\n' "${out[@]+"${out[@]}"}" >"${file}.nodeyard-new.$$"
        mv -f "${file}.nodeyard-new.$$" "$file"
    else
        # Config edits are journaled as their own feature, so undoing a
        # feature (e.g. uninstalling k3s) never rolls back unrelated config.
        local NY_FEATURE="config"
        printf '%s\n' "${out[@]+"${out[@]}"}" | ny_write_file "$(ny_unroot "$file")" 0640
    fi
    if [[ "$NY_DRY_RUN" -ne 1 && "$NY_CFG_LOADED_FROM" == "$file" ]]; then
        ny_cfg_parse "$file" || true
    fi
    return 0
}

# ny_cfg_batch_begin [FILE] / ny_cfg_batch_commit -- group several edits into
# one write (one backup, one journal entry, one diff in --dry-run).
ny_cfg_batch_begin() {
    NY_CFG_BATCH_TARGET="${1:-$NY_CONFIG}"
    NY_CFG_BATCH_TMP="$(ny_mktemp)"
    if [[ -f "$NY_CFG_BATCH_TARGET" ]]; then
        cat "$NY_CFG_BATCH_TARGET" >"$NY_CFG_BATCH_TMP"
    fi
}

ny_cfg_batch_commit() {
    local tmp="$NY_CFG_BATCH_TMP" target="$NY_CFG_BATCH_TARGET"
    NY_CFG_BATCH_TMP=""
    NY_CFG_BATCH_TARGET=""
    [[ -n "$tmp" ]] || return 0
    local NY_FEATURE="config"
    ny_write_file "$(ny_unroot "$target")" 0640 <"$tmp"
    if [[ "$NY_DRY_RUN" -ne 1 && "$NY_CFG_LOADED_FROM" == "$target" ]]; then
        ny_cfg_parse "$target" || true
    fi
    return 0
}

ny_cfg_set() { ny_cfg_edit set "$@"; }
ny_cfg_add() { ny_cfg_edit add "$@"; }
ny_cfg_unset() { ny_cfg_edit unset "$1" "$2" "$3" "" "${4:-$NY_CONFIG}"; }
ny_cfg_drop_section() { ny_cfg_edit drop "$1" "$2" "" "" "${3:-$NY_CONFIG}"; }

# --- validation --------------------------------------------------------------

# ny_cfg_validate [FILE] -- fills NY_CFG_ERRORS / NY_CFG_WARNINGS; returns 1 on errors.
ny_cfg_validate() {
    local file="${1:-$NY_CONFIG}"
    ny_cfg_parse "$file" || return 1

    local entry section sub k key spec type flags line
    for entry in "${NY_CFG_ORDER[@]+"${NY_CFG_ORDER[@]}"}"; do
        section="${entry%%"$NY_US"*}"
        sub="${entry#*"$NY_US"}"
        line="${NY_CFG_SECTION_LINE[$entry]}"
        local kind="${NY_CFG_SECTION_KIND[$section]:-}"
        if [[ -z "$kind" ]]; then
            NY_CFG_WARNINGS+=("line ${line}: unknown section [${section}] (ignored). Check the spelling against docs/configuration.md.")
            continue
        fi
        if [[ "$kind" == named && -z "$sub" ]]; then
            NY_CFG_ERRORS+=("line ${line}: [${section}] needs a name, e.g. [${section} \"pi-1\"].")
            continue
        fi
        if [[ "$kind" == single && -n "$sub" ]]; then
            NY_CFG_ERRORS+=("line ${line}: [${section}] does not take a name; use plain [${section}].")
            continue
        fi
        if [[ "$section" == node ]] && ! ny_valid_hostname "$sub"; then
            NY_CFG_ERRORS+=("line ${line}: node name: ${NY_VALID_MSG}")
        fi
    done

    for k in "${!NY_CFG[@]}"; do
        section="${k%%"$NY_US"*}"
        sub="${k#*"$NY_US"}"
        sub="${sub%%"$NY_US"*}"
        key="${k##*"$NY_US"}"
        [[ -n "${NY_CFG_SECTION_KIND[$section]:-}" ]] || continue
        line="${NY_CFG_LINE[$k]}"
        spec="${NY_CFG_SCHEMA["$section.$key"]:-${NY_CFG_SCHEMA["$section.*.$key"]:-}}"
        if [[ -z "$spec" ]]; then
            NY_CFG_WARNINGS+=("line ${line}: unknown key '${key}' in $(ny_cfg_header "$section" "$sub") (ignored).")
            continue
        fi
        type="${spec%%|*}"
        local v
        while IFS= read -r v; do
            if ! ny_validate "$type" "$v"; then
                NY_CFG_ERRORS+=("line ${line}: ${key}: ${NY_VALID_MSG}")
            fi
        done <<<"${NY_CFG[$k]}"
    done

    # Required keys.
    for k in "${!NY_CFG_SCHEMA[@]}"; do
        flags="${NY_CFG_SCHEMA[$k]#*|}"
        flags="${flags%%|*}"
        [[ ",$flags," == *",required,"* ]] || continue
        section="${k%%.*}"
        key="${k##*.}"
        if [[ "$k" == *".*."* ]]; then
            while IFS= read -r sub; do
                ny_cfg_has "$section" "$sub" "$key" ||
                    NY_CFG_ERRORS+=("line ${NY_CFG_SECTION_LINE["$section$NY_US$sub"]}: $(ny_cfg_header "$section" "$sub") is missing '${key}'.")
            done < <(ny_cfg_subs "$section")
        elif ny_cfg_has_section "$section" ""; then
            ny_cfg_has "$section" "" "$key" ||
                NY_CFG_ERRORS+=("line ${NY_CFG_SECTION_LINE["$section$NY_US"]}: [${section}] is missing '${key}'.")
        fi
    done

    ny_cfg_validate_cross
    [[ "${#NY_CFG_ERRORS[@]}" -eq 0 ]]
}

# Checks that span several keys: unique addresses, one init server, ranges
# inside the subnet and not overlapping.
ny_cfg_validate_cross() {
    local subnet node addr ip init_count=0
    subnet="$(ny_cfg_get network "" subnet)"
    declare -A seen_ip=()
    while IFS= read -r node; do
        [[ -n "$node" ]] || continue
        if ny_bool "$(ny_cfg_get node "$node" init false)"; then
            init_count=$((init_count + 1))
        fi
        addr="$(ny_cfg_get node "$node" address)"
        { [[ -n "$addr" ]] && ny_valid_cidr4 "$addr"; } || continue
        ip="${addr%/*}"
        if [[ -n "${seen_ip[$ip]:-}" ]]; then
            NY_CFG_ERRORS+=("nodes '${seen_ip[$ip]}' and '${node}' both use address ${ip}. Fix: give each node its own address.")
        fi
        seen_ip[$ip]="$node"
        if [[ -n "$subnet" ]] && ny_valid_cidr4 "$subnet" && ! ny_cidr_contains "$subnet" "$ip"; then
            NY_CFG_ERRORS+=("node '${node}' address ${ip} is outside the network subnet ${subnet}.")
        fi
    done < <(ny_cfg_subs node)

    if ((init_count > 1)); then
        NY_CFG_ERRORS+=("${init_count} nodes have 'init = true'. Fix: exactly one server creates the cluster; set init = false on the others.")
    fi

    local vip
    vip="$(ny_cfg_get cluster "" vip)"
    if [[ -n "$vip" ]] && ny_valid_ipv4 "$vip"; then
        [[ -n "${seen_ip[$vip]:-}" ]] &&
            NY_CFG_ERRORS+=("cluster vip ${vip} is also node '${seen_ip[$vip]}''s address. Fix: pick an unused address for the virtual IP.")
        if [[ -n "$subnet" ]] && ny_valid_cidr4 "$subnet" && ! ny_cidr_contains "$subnet" "$vip"; then
            NY_CFG_ERRORS+=("cluster vip ${vip} is outside the network subnet ${subnet}.")
        fi
    fi

    local -a names=(dhcp-range node-range lb-range)
    local a b ra rb a1 a2 b1 b2
    for a in "${names[@]}"; do
        ra="$(ny_cfg_get network "" "$a")"
        { [[ -n "$ra" ]] && ny_valid_ipv4_range "$ra"; } || continue
        if [[ -n "$subnet" ]] && ny_valid_cidr4 "$subnet"; then
            if ! ny_cidr_contains "$subnet" "${ra%%-*}" || ! ny_cidr_contains "$subnet" "${ra#*-}"; then
                NY_CFG_ERRORS+=("network ${a} ${ra} is not inside the subnet ${subnet}.")
            fi
        fi
        for b in "${names[@]}"; do
            [[ "$a" < "$b" ]] || continue
            rb="$(ny_cfg_get network "" "$b")"
            { [[ -n "$rb" ]] && ny_valid_ipv4_range "$rb"; } || continue
            a1="$(ny_ip_to_int "${ra%%-*}")"
            a2="$(ny_ip_to_int "${ra#*-}")"
            b1="$(ny_ip_to_int "${rb%%-*}")"
            b2="$(ny_ip_to_int "${rb#*-}")"
            if ((a1 <= b2 && b1 <= a2)); then
                NY_CFG_ERRORS+=("network ${a} (${ra}) overlaps ${b} (${rb}). Fix: make the ranges separate.")
            fi
        done
    done
    return 0
}
