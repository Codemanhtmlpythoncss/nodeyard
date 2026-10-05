# shellcheck shell=bash
# Minimal JSON writer in pure bash (reading JSON uses jq).

# ny_json_str TEXT -- TEXT as a JSON string literal.
ny_json_str() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/\\r}"
    s="${s//$'\t'/\\t}"
    s="${s//$'\b'/\\b}"
    s="${s//$'\f'/\\f}"
    # Any other control characters are dropped rather than emitted raw.
    s="${s//[$'\001'-$'\037']/}"
    printf '"%s"' "$s"
}

# ny_json_obj FIELD... -- a JSON object. Each FIELD is one of:
#   key=text       string value
#   key:=json      raw JSON value (number, true/false/null, object, array)
#   key?=text      string, or null when text is empty
ny_json_obj() {
    local out="" f key val
    for f in "$@"; do
        if [[ "$f" == *":="* && "${f%%:=*}" != *"="* ]]; then
            key="${f%%:=*}"
            val="${f#*:=}"
            [[ -n "$val" ]] || val="null"
        elif [[ "$f" == *"?="* && "${f%%\?=*}" != *"="* ]]; then
            key="${f%%\?=*}"
            val="${f#*\?=}"
            if [[ -z "$val" ]]; then
                val="null"
            else
                val="$(ny_json_str "$val")"
            fi
        else
            key="${f%%=*}"
            val="$(ny_json_str "${f#*=}")"
        fi
        out+="${out:+,}$(ny_json_str "$key"):${val}"
    done
    printf '{%s}' "$out"
}

# ny_json_arr ITEM... -- a JSON array of raw JSON items.
ny_json_arr() {
    local IFS=','
    printf '[%s]' "$*"
}

# ny_json_arr_str TEXT... -- a JSON array of strings.
ny_json_arr_str() {
    local -a items=()
    local t
    for t in "$@"; do
        items+=("$(ny_json_str "$t")")
    done
    ny_json_arr "${items[@]+"${items[@]}"}"
}

# ny_json_bool 0|1 -- false/true.
ny_json_bool() {
    if [[ "${1:-0}" == 1 || "${1:-}" == true ]]; then
        printf 'true'
    else
        printf 'false'
    fi
}

# ny_json_num VALUE -- VALUE if numeric, otherwise null.
ny_json_num() {
    if [[ "${1:-}" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        printf '%s' "$1"
    else
        printf 'null'
    fi
}
