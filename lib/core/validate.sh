# shellcheck shell=bash
# Input validation shared by commands, the config file and wizards.
# Each validator returns 0 when valid; otherwise NY_VALID_MSG explains why in
# plain English, with an example of a valid value.

NY_VALID_MSG=""

ny_valid_ipv4() {
    local ip="$1"
    NY_VALID_MSG="'${ip}' is not a valid IPv4 address (expected four numbers 0-255, e.g. 192.168.1.10)."
    [[ "$ip" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
    local i o
    for i in 1 2 3 4; do
        o="${BASH_REMATCH[i]}"
        [[ "$o" =~ ^(0|[1-9][0-9]*)$ ]] || return 1
        ((10#$o <= 255)) || return 1
    done
    NY_VALID_MSG=""
}

ny_valid_cidr4() {
    local v="$1"
    NY_VALID_MSG="'${v}' is not a valid address with prefix length (expected e.g. 192.168.1.10/24)."
    [[ "$v" == */* ]] || return 1
    local ip="${v%/*}" len="${v#*/}"
    ny_valid_ipv4 "$ip" || {
        NY_VALID_MSG="'${v}': ${NY_VALID_MSG}"
        return 1
    }
    [[ "$len" =~ ^[0-9]{1,2}$ ]] && ((10#$len <= 32)) || {
        NY_VALID_MSG="'${v}' has an invalid prefix length '${len}' (expected 0-32, usually 24)."
        return 1
    }
    NY_VALID_MSG=""
}

# ny_valid_ipv4_range "A-B" -- an inclusive range of addresses, A <= B.
ny_valid_ipv4_range() {
    local v="$1"
    NY_VALID_MSG="'${v}' is not a valid address range (expected e.g. 192.168.1.100-192.168.1.199)."
    [[ "$v" == *-* ]] || return 1
    local a="${v%%-*}" b="${v#*-}"
    ny_valid_ipv4 "$a" && ny_valid_ipv4 "$b" || {
        NY_VALID_MSG="'${v}' is not a valid address range (expected e.g. 192.168.1.100-192.168.1.199)."
        return 1
    }
    (($(ny_ip_to_int "$a") <= $(ny_ip_to_int "$b"))) || {
        NY_VALID_MSG="Range '${v}' ends before it starts; put the lower address first."
        return 1
    }
    NY_VALID_MSG=""
}

ny_valid_hostname() {
    local h="$1"
    NY_VALID_MSG="'${h}' is not a valid hostname (use letters, digits and hyphens, up to 63 characters, e.g. pi-node-1)."
    [[ -n "$h" && ${#h} -le 253 ]] || return 1
    local label
    local -a labels=()
    IFS=. read -r -a labels <<<"$h"
    [[ "$h" != *. && "$h" != .* && "$h" != *..* ]] || return 1
    for label in "${labels[@]}"; do
        [[ "$label" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$ ]] || return 1
    done
    NY_VALID_MSG=""
}

# A host: hostname or IPv4 address.
ny_valid_host() {
    if [[ "$1" =~ ^[0-9.]+$ ]]; then
        ny_valid_ipv4 "$1"
    else
        ny_valid_hostname "$1"
    fi
}

ny_valid_port() {
    NY_VALID_MSG="'$1' is not a valid port (expected a number 1-65535)."
    [[ "$1" =~ ^[0-9]{1,5}$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535)) || return 1
    NY_VALID_MSG=""
}

# ny_valid_int VALUE [MIN] [MAX]
ny_valid_int() {
    local v="$1" min="${2:-}" max="${3:-}"
    NY_VALID_MSG="'${v}' is not a whole number${min:+ between ${min} and ${max:-any}}."
    [[ "$v" =~ ^-?[0-9]+$ ]] || return 1
    [[ -z "$min" ]] || ((v >= min)) || return 1
    [[ -z "$max" ]] || ((v <= max)) || return 1
    NY_VALID_MSG=""
}

ny_valid_bool() {
    NY_VALID_MSG="'$1' is not yes/no (use true or false)."
    [[ "${1,,}" =~ ^(true|false|yes|no|on|off|1|0)$ ]] || return 1
    NY_VALID_MSG=""
}

ny_bool() {
    [[ "${1,,}" =~ ^(true|yes|on|1)$ ]]
}

ny_valid_iface() {
    NY_VALID_MSG="'$1' is not a valid network interface name (e.g. eth0, enp3s0)."
    [[ "$1" =~ ^[A-Za-z0-9_.:@-]{1,15}$ ]] || return 1
    NY_VALID_MSG=""
}

# ny_valid_enum VALUE CHOICE...
ny_valid_enum() {
    local v="$1"
    shift
    NY_VALID_MSG="'${v}' is not one of: $(ny_join ', ' "$@")."
    ny_in_list "$v" "$@" || return 1
    NY_VALID_MSG=""
}

ny_valid_url() {
    NY_VALID_MSG="'$1' is not a valid URL (expected e.g. https://192.168.1.10:6443)."
    [[ "$1" =~ ^https?://[A-Za-z0-9._-]+(:[0-9]{1,5})?(/.*)?$ ]] || return 1
    NY_VALID_MSG=""
}

ny_valid_abspath() {
    NY_VALID_MSG="'$1' is not an absolute path without '..' (e.g. /srv/data)."
    [[ "$1" =~ ^/[A-Za-z0-9._/@+-]*$ && "$1" != *..* ]] || return 1
    NY_VALID_MSG=""
}

# Kubernetes label: key=value with DNS-style key and simple value.
ny_valid_label() {
    NY_VALID_MSG="'$1' is not a valid label (expected key=value, e.g. nodeyard.io/group=gpu)."
    [[ "$1" =~ ^([a-z0-9.-]+/)?[A-Za-z0-9]([A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?=([A-Za-z0-9]([A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?)?$ ]] || return 1
    NY_VALID_MSG=""
}

ny_valid_taint() {
    NY_VALID_MSG="'$1' is not a valid taint (expected key=value:Effect, e.g. dedicated=gpu:NoSchedule)."
    [[ "$1" =~ ^[A-Za-z0-9./_-]+(=[A-Za-z0-9._-]*)?:(NoSchedule|PreferNoSchedule|NoExecute)$ ]] || return 1
    NY_VALID_MSG=""
}

# k3s release: v1.33.4+k3s1
ny_valid_k3s_version() {
    NY_VALID_MSG="'$1' is not a k3s version (expected e.g. v1.33.4+k3s1)."
    [[ "$1" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?\+k3s[0-9]+$ ]] || return 1
    NY_VALID_MSG=""
}

ny_valid_mac() {
    NY_VALID_MSG="'$1' is not a valid MAC address (expected six hex pairs, e.g. dc:a6:32:12:34:56)."
    [[ "$1" =~ ^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$ ]] || return 1
    NY_VALID_MSG=""
}

ny_valid_name() {
    NY_VALID_MSG="'$1' is not a valid name (lowercase letters, digits and hyphens, starting with a letter, up to 40 characters)."
    [[ "$1" =~ ^[a-z][a-z0-9-]{0,39}$ ]] || return 1
    NY_VALID_MSG=""
}

# ny_validate TYPE VALUE -- dispatch on a type spec used by the config schema
# and wizard specs: ipv4 cidr4 ipv4-range host hostname port bool iface url
# abspath label taint k3s-version name string int[:MIN[:MAX]] enum:a,b,c
# list:TYPE (comma-separated).
ny_validate() {
    local type="$1" value="$2"
    NY_VALID_MSG=""
    case "$type" in
        list:*)
                local item sub="${type#list:}"
                while IFS= read -r item; do
                    ny_validate "$sub" "$item" || return 1
                done < <(ny_csv_split "$value")
                return 0
                ;;
        enum:*)
                local -a choices=()
                IFS=',' read -r -a choices <<<"${type#enum:}"
                ny_valid_enum "$value" "${choices[@]}"
                ;;
        int*)
                local spec="${type#int}" min="" max=""
                spec="${spec#:}"
                min="${spec%%:*}"
                [[ "$spec" == *:* ]] && max="${spec#*:}"
                ny_valid_int "$value" "$min" "$max"
                ;;
        mac) ny_valid_mac "$value" ;;
        ipv4) ny_valid_ipv4 "$value" ;;
        cidr4) ny_valid_cidr4 "$value" ;;
        ipv4-range) ny_valid_ipv4_range "$value" ;;
        host) ny_valid_host "$value" ;;
        hostname) ny_valid_hostname "$value" ;;
        port) ny_valid_port "$value" ;;
        bool) ny_valid_bool "$value" ;;
        iface) ny_valid_iface "$value" ;;
        url) ny_valid_url "$value" ;;
        abspath) ny_valid_abspath "$value" ;;
        label) ny_valid_label "$value" ;;
        taint) ny_valid_taint "$value" ;;
        k3s-version) ny_valid_k3s_version "$value" ;;
        name) ny_valid_name "$value" ;;
        string | "") return 0 ;;
        *)
            NY_VALID_MSG="internal error: unknown value type '${type}'"
            return 1
            ;;
    esac
}

# --- IPv4 arithmetic -----------------------------------------------------------

ny_ip_to_int() {
    local a b c d
    IFS=. read -r a b c d <<<"$1"
    printf '%s\n' "$(((10#$a << 24) + (10#$b << 16) + (10#$c << 8) + 10#$d))"
}

ny_int_to_ip() {
    local n="$1"
    printf '%s.%s.%s.%s\n' $(((n >> 24) & 255)) $(((n >> 16) & 255)) $(((n >> 8) & 255)) $((n & 255))
}

# ny_cidr_bounds CIDR -- prints "network_int broadcast_int".
ny_cidr_bounds() {
    local ip="${1%/*}" len="${1#*/}" n mask
    n="$(ny_ip_to_int "$ip")"
    if ((len == 0)); then
        mask=0
    else
        mask=$(((0xFFFFFFFF << (32 - len)) & 0xFFFFFFFF))
    fi
    printf '%s %s\n' "$((n & mask))" "$(((n & mask) | (~mask & 0xFFFFFFFF)))"
}

# ny_cidr_contains CIDR IP -- true if IP is inside CIDR.
ny_cidr_contains() {
    local net bc ip
    read -r net bc <<<"$(ny_cidr_bounds "$1")"
    ip="$(ny_ip_to_int "$2")"
    ((ip >= net && ip <= bc))
}
