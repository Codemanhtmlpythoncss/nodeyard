# shellcheck shell=bash
# shellcheck disable=SC2034 # globals here are read by other files
# Base layer: version, paths, global flags and small helpers used everywhere.
#
# Every path that points into the real system goes through ny_path, so tests
# and demo mode can redirect the whole tool into a sandbox by setting
# NODEYARD_ROOT. Nothing in here touches the system.

NY_VERSION="0.1.0"
NY_NAME="nodeyard"
NY_REPO="${NODEYARD_REPO:-Codemanhtmlpythoncss/nodeyard}"

# NY_HOME is set by bin/nodeyard (or the test helper) before this file loads.
: "${NY_HOME:?NY_HOME must be set before loading nodeyard}"
NY_SHARE="${NY_HOME}/share/nodeyard"

# ny_paths_init -- (re)compute every path from the environment. Called at load
# time and again after demo mode points NODEYARD_ROOT at its sandbox.
#   NY_ROOT   filesystem root prefix for every system path (empty normally)
#   NY_ETC, NY_STATE, NY_CONFIG, NY_SECRETS_DIR, NY_LOG_DIR are *real* paths
#   (root prefix included); use ny_unroot to get the system path back.
ny_paths_init() {
    NY_ROOT="${NODEYARD_ROOT:-}"
    NY_ETC="${NODEYARD_ETC:-${NY_ROOT}/etc/nodeyard}"
    NY_STATE="${NODEYARD_STATE:-${NY_ROOT}/var/lib/nodeyard}"
    NY_CONFIG="${NODEYARD_CONFIG:-${NY_ETC}/cluster.conf}"
    NY_SECRETS_DIR="${NY_ETC}/secrets"
    if [[ -n "${NODEYARD_LOG_DIR:-}" ]]; then
        NY_LOG_DIR="$NODEYARD_LOG_DIR"
    elif [[ "${EUID}" -eq 0 || -n "$NY_ROOT" ]]; then
        NY_LOG_DIR="${NY_ROOT}/var/log/nodeyard"
    else
        NY_LOG_DIR="${XDG_STATE_HOME:-${HOME:-/tmp}/.local/state}/nodeyard"
    fi
    NY_LOG_FILE="${NY_LOG_DIR}/nodeyard.log"
    NY_LOG_READY=0
}
ny_paths_init

# Global flags (set by the dispatcher from the command line).
NY_YES=0
NY_DRY_RUN=0
NY_VERBOSE=0
NY_JSON=0
NY_DEMO="${NODEYARD_DEMO:-0}"
NY_NONINTERACTIVE=0
if [[ ! -t 0 ]]; then
    NY_NONINTERACTIVE=1
fi

# The command being run, for logs and the change journal ("install master").
NY_CMD_PATH=""
# Feature area that owns the changes made by this command ("k3s", "net", ...).
NY_FEATURE="core"

# ny_path ABS_PATH -- the real location of a system path (sandbox-aware).
ny_path() {
    printf '%s%s\n' "$NY_ROOT" "$1"
}

# ny_unroot REAL_PATH -- the system path for a sandbox-aware real path
# (the inverse of ny_path; NY_ETC, NY_STATE and NY_CONFIG are real paths).
ny_unroot() {
    printf '%s\n' "${1#"$NY_ROOT"}"
}

# have CMD -- true if CMD is on PATH.
have() {
    command -v "$1" >/dev/null 2>&1
}

ny_is_root() {
    [[ "${EUID}" -eq 0 || "$NY_DEMO" -eq 1 ]]
}

# ny_join SEP WORD... -- join words with SEP.
ny_join() {
    local sep="$1" out="" w
    shift
    for w in "$@"; do
        out+="${out:+$sep}$w"
    done
    printf '%s' "$out"
}

ny_trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

ny_lower() {
    printf '%s' "${1,,}"
}

# ny_in_list NEEDLE ITEM... -- true if NEEDLE equals one of the items.
ny_in_list() {
    local needle="$1" item
    shift
    for item in "$@"; do
        [[ "$item" == "$needle" ]] && return 0
    done
    return 1
}

# ny_csv_split CSV -- print comma-separated items one per line, trimmed.
ny_csv_split() {
    local item
    local -a items=()
    IFS=',' read -r -a items <<<"$1"
    for item in "${items[@]+"${items[@]}"}"; do
        item="$(ny_trim "$item")"
        [[ -n "$item" ]] && printf '%s\n' "$item"
    done
    return 0
}

# ny_now -- timestamp for logs and journal entries (no fork).
ny_now() {
    printf '%(%Y-%m-%dT%H:%M:%S%z)T' -1
}

# ny_sha256 FILE -- hex digest of FILE.
ny_sha256() {
    if have sha256sum; then
        sha256sum -- "$1" | cut -d' ' -f1
    else
        shasum -a 256 -- "$1" | cut -d' ' -f1
    fi
}

# --- temporary files ---------------------------------------------------------

NY_TMPFILES=()

# ny_mktemp [-d] -- a private temp file (or dir) removed when nodeyard exits.
ny_mktemp() {
    local t
    if [[ "${1:-}" == "-d" ]]; then
        t="$(mktemp -d "${TMPDIR:-/tmp}/nodeyard.XXXXXXXX")"
    else
        t="$(mktemp "${TMPDIR:-/tmp}/nodeyard.XXXXXXXX")"
    fi
    NY_TMPFILES+=("$t")
    printf '%s\n' "$t"
}

ny_cleanup_tmp() {
    local t
    for t in "${NY_TMPFILES[@]+"${NY_TMPFILES[@]}"}"; do
        rm -rf -- "$t" 2>/dev/null || true
    done
    NY_TMPFILES=()
}

# --- versions ----------------------------------------------------------------

# ny_version_cmp A B -- prints -1, 0 or 1 (semantic versioning, "v" prefix and
# "+build" metadata ignored, pre-releases sort before the release).
ny_version_cmp() {
    local a="${1#v}" b="${2#v}"
    a="${a%%+*}"
    b="${b%%+*}"
    local a_core="${a%%-*}" b_core="${b%%-*}" a_pre="" b_pre=""
    [[ "$a" == *-* ]] && a_pre="${a#*-}"
    [[ "$b" == *-* ]] && b_pre="${b#*-}"

    local -a va=() vb=()
    IFS=. read -r -a va <<<"$a_core"
    IFS=. read -r -a vb <<<"$b_core"
    local i na nb
    for ((i = 0; i < 3; i++)); do
        na="${va[i]:-0}"
        nb="${vb[i]:-0}"
        na="${na//[^0-9]/}"
        nb="${nb//[^0-9]/}"
        na="${na:-0}"
        nb="${nb:-0}"
        if ((10#$na > 10#$nb)); then
            echo 1
            return 0
        fi
        if ((10#$na < 10#$nb)); then
            echo -1
            return 0
        fi
    done

    if [[ -z "$a_pre" && -z "$b_pre" ]]; then
        echo 0
        return 0
    fi
    if [[ -z "$a_pre" ]]; then
        echo 1
        return 0
    fi
    if [[ -z "$b_pre" ]]; then
        echo -1
        return 0
    fi

    local -a pa=() pb=()
    IFS=. read -r -a pa <<<"$a_pre"
    IFS=. read -r -a pb <<<"$b_pre"
    local n=${#pa[@]} x y
    ((${#pb[@]} > n)) && n=${#pb[@]}
    for ((i = 0; i < n; i++)); do
        x="${pa[i]-}"
        y="${pb[i]-}"
        if [[ -z "$x" ]]; then
            echo -1
            return 0
        fi
        if [[ -z "$y" ]]; then
            echo 1
            return 0
        fi
        if [[ "$x" =~ ^[0-9]+$ && "$y" =~ ^[0-9]+$ ]]; then
            if ((10#$x > 10#$y)); then
                echo 1
                return 0
            fi
            if ((10#$x < 10#$y)); then
                echo -1
                return 0
            fi
        elif [[ "$x" =~ ^[0-9]+$ ]]; then
            echo -1
            return 0
        elif [[ "$y" =~ ^[0-9]+$ ]]; then
            echo 1
            return 0
        elif [[ "$x" > "$y" ]]; then
            echo 1
            return 0
        elif [[ "$x" < "$y" ]]; then
            echo -1
            return 0
        fi
    done
    echo 0
}

# ny_retry MAX DELAY CMD... -- retry a flaky command with exponential backoff.
ny_retry() {
    local max="$1" delay="$2" attempt=1
    shift 2
    until "$@"; do
        if ((attempt >= max)); then
            ny_warn "Gave up after ${attempt} attempts: $(ny_redact "$*")"
            return 1
        fi
        ny_warn "Attempt ${attempt}/${max} failed; retrying in ${delay}s: $(ny_redact "$*")"
        sleep "$delay"
        attempt=$((attempt + 1))
        delay=$((delay < 30 ? delay * 2 : delay))
    done
}
