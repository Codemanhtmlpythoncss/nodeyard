#!/usr/bin/env bash
# Remove nodeyard's own files from this machine.
#
# This removes the program, its links and completions, and (unless
# --keep-config) /etc/nodeyard, /var/lib/nodeyard and /var/log/nodeyard.
#
# It does NOT revert the system changes nodeyard made (network settings,
# k3s, firewall rules...). To revert those too, run instead:
#   sudo nodeyard uninstall everything
# which undoes every recorded change and then runs this script.

set -euo pipefail

PREFIX="/usr/local"
YES=0
DRY_RUN=0
KEEP_CONFIG=0
FORCE=0

usage() {
    cat <<'USAGE'
Usage: uninstall.sh [--keep-config] [--force] [--dry-run] [--yes] [--prefix DIR]

  --keep-config   Keep /etc/nodeyard (cluster config and secrets)
  --force         Remove nodeyard even though changes it made are still in place
  --dry-run       Show what would be removed
  --yes, -y       Don't ask for confirmation
  --prefix DIR    Where nodeyard was installed (default /usr/local)
USAGE
}

say() { printf '%s\n' "$*" >&2; }
die() {
    printf 'Error: %s\n' "$1" >&2
    [[ -n "${2:-}" ]] && printf '  Fix: %s\n' "$2" >&2
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep-config)
            KEEP_CONFIG=1
            shift
            ;;
        --force)
            FORCE=1
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --yes | -y)
            YES=1
            shift
            ;;
        --prefix)
            [[ $# -ge 2 ]] || die "--prefix needs a directory"
            PREFIX="${2%/}"
            shift 2
            ;;
        --help | -h)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "Unknown option: $1"
            ;;
    esac
done

ROOT="${NODEYARD_ROOT:-}"
DEST="${PREFIX}/lib/nodeyard"
MANIFEST="${DEST}/.install-manifest"
JOURNAL="${ROOT}/var/lib/nodeyard/journal/journal.jsonl"

[[ "$DRY_RUN" -eq 1 || "${EUID}" -eq 0 || -w "$PREFIX" ]] || die "Uninstalling needs root." "Run it with sudo."

remove() {
    [[ -e "$1" || -L "$1" ]] || return 0
    if [[ "$DRY_RUN" -eq 1 ]]; then
        say "[dry-run] would remove $1"
    else
        rm -rf "$1"
        say "Removed $1"
    fi
}

# Changes nodeyard made that haven't been undone yet.
if [[ -f "$JOURNAL" && "$FORCE" -eq 0 ]] && command -v jq >/dev/null 2>&1; then
    active="$(jq -rs '(map(select(.op == "undone" and (.undone_feature // "") == "") | .undone_txn)) as $u
        | map(select(.op != "undone" and (.txn as $t | $u | index($t) | not))) | map(.txn) | unique | length' "$JOURNAL" 2>/dev/null || echo 0)"
    if [[ "${active:-0}" -gt 0 ]]; then
        die "nodeyard made ${active} change(s) to this system that are still in place (network, k3s, firewall...)." \
            "To revert them and remove nodeyard: sudo nodeyard uninstall everything. To remove only nodeyard and keep those changes: re-run with --force."
    fi
fi

if [[ "$YES" -eq 0 && "$DRY_RUN" -eq 0 ]]; then
    what="nodeyard from ${DEST}"
    [[ "$KEEP_CONFIG" -eq 0 ]] && what="${what}, plus /etc/nodeyard (config and secrets), /var/lib/nodeyard and its logs"
    reply=n
    if [[ -t 0 ]]; then
        read -r -p "Remove ${what}? [y/N] " reply || reply=n
    elif [[ -r /dev/tty ]]; then
        printf 'Remove %s? [y/N] ' "$what" >/dev/tty
        read -r reply </dev/tty || reply=n
    else
        die "Confirmation needed." "Re-run with --yes."
    fi
    [[ "$reply" =~ ^[Yy] ]] || {
        say "Cancelled; nothing was removed."
        exit 0
    }
fi

# Links and completions recorded at install time (only ones still pointing at us).
if [[ -f "$MANIFEST" ]]; then
    while IFS= read -r path; do
        [[ -n "$path" ]] || continue
        if [[ -L "$path" ]]; then
            case "$(readlink "$path")" in
                "${DEST}"/*) remove "$path" ;;
            esac
        elif [[ "$path" == *k3s-manager.legacy ]]; then
            say "Kept ${path} (the old k3s-manager script)."
        fi
    done <"$MANIFEST"
fi

remove "$DEST"
if [[ "$KEEP_CONFIG" -eq 0 ]]; then
    remove "${ROOT}/etc/nodeyard"
    remove "${ROOT}/var/lib/nodeyard"
    remove "${ROOT}/var/log/nodeyard"
else
    say "Kept ${ROOT}/etc/nodeyard."
fi
say "nodeyard has been removed."
